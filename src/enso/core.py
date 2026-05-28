"""Enso runtime — the engine that makes agents go."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.resources
import json
import logging
import os
import shlex
import signal
import tempfile
from asyncio.subprocess import Process
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar

from croniter import croniter

from . import messages
from .config import CONFIG_DIR, STATE_FILE
from .jobs import Job, load_jobs
from .logging_config import logging_flags
from .providers import BaseProvider, StreamEvent, get_provider

if TYPE_CHECKING:
    from .transports import BaseTransport, TransportContext

log = logging.getLogger(__name__)


def _status_edit_due(elapsed: int) -> bool:
    """Return True when the status message should be edited at this tick.

    Progressive backoff: every 1s for the first 10s, every 2s until 60s,
    then every 5s after that.  Keeps Telegram happy while still feeling
    responsive at the start of a request.
    """
    if elapsed <= 10:
        return True
    if elapsed <= 60:
        return elapsed % 2 == 0
    return elapsed % 5 == 0


def split_text(text: str, limit: int = 4096) -> list[str]:
    """Split text at line boundaries to fit message size limits."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            if current:
                chunks.append(current)
            remainder = line
            while len(remainder) > limit:
                chunks.append(remainder[:limit])
                remainder = remainder[limit:]
            current = remainder
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)
    return chunks


MAX_QUEUE_SIZE = 5
SESSION_TTL_DAYS = int(os.environ.get("ENSO_SESSION_TTL_DAYS", "30"))
JOB_CONCURRENCY = int(os.environ.get("ENSO_JOB_CONCURRENCY", "2"))
PROCESS_TERMINATE_GRACE_SECS = float(os.environ.get("ENSO_PROCESS_TERMINATE_GRACE_SECS", "5"))


def _redacted_command(cmd: list[str]) -> str:
    """Return a shell-like command string with the prompt argument redacted."""
    if "--" not in cmd:
        return shlex.join(cmd)
    sep = cmd.index("--")
    prompt_chars = sum(len(part) for part in cmd[sep + 1 :])
    return shlex.join([*cmd[: sep + 1], f"<prompt chars={prompt_chars}>"])


@dataclass
class _QueuedItem:
    """A message waiting to be dispatched while another request is running."""

    prompt: str
    ctx: TransportContext
    preview: str


class Runtime:
    """Central runtime holding all state, process management, and job scheduling."""

    def __init__(self, config: dict):
        self.config = config
        flags = logging_flags(config)
        self.debug_prompts: bool = flags["debug_prompts"]
        self.debug_events: bool = flags["debug_events"]
        self.working_dir: str = config.get("working_dir", os.getcwd())
        os.makedirs(self.working_dir, exist_ok=True)
        self.models: dict[str, list[str]] = {
            name: pcfg.get("models", [])
            for name, pcfg in config.get("providers", {}).items()
        }
        self.transport: BaseTransport | None = None

        # Per-chat state (keyed by conversation ID — str for all transports)
        self.active_provider_by_chat: dict[str, str] = {}
        self.active_model_by_chat_provider: dict[tuple[str, str], str] = {}
        self.effort_by_chat_provider_model: dict[tuple[str, str, str], str] = {}
        self.session_by_chat_provider: dict[tuple[str, str], str] = {}
        self.running_process_by_chat: dict[str, Process] = {}
        self.running_task_by_chat: dict[str, asyncio.Task] = {}
        self.chat_lock_by_chat: dict[str, asyncio.Lock] = {}

        # Dispatch queue (per-conversation)
        self._queue_by_conversation: dict[str, deque[_QueuedItem]] = {}

        # Compact-command seeds: per-chat prior-session summaries waiting to
        # be prepended to the next user prompt. Set by /compact; consumed in
        # process_request the next time that chat dispatches a message.
        self.compact_seed_by_chat: dict[str, str] = {}

        # Activity tracking for session pruning
        self._last_active: dict[str, datetime] = {}

        # Job scheduler state
        self._job_last_run: dict[str, datetime] = {}
        self._running_job_tasks: dict[str, asyncio.Task] = {}
        self._job_semaphore = asyncio.Semaphore(JOB_CONCURRENCY)

    # -- Workspace setup --

    def install_system_prompts(self) -> None:
        """Set up working directory, system prompts, skills, hooks, and config dirs.

        Creates:
        - ~/.enso/jobs/ and ~/.enso/skills/
        - Bundled skills copied to ~/.enso/skills/
        - CLAUDE.md in working_dir (from bundled template, only if missing)
        - AGENTS.md, GEMINI.md as symlinks to CLAUDE.md
        - .claude/skills and .agents/skills symlinked to ~/.enso/skills/
          (so Claude, Codex, and Gemini auto-discover skills)
        - Auto-compact notification hooks for Claude and Gemini
        """
        from .config import JOBS_DIR

        skills_dir = os.path.join(CONFIG_DIR, "skills")
        for d in (JOBS_DIR, skills_dir):
            os.makedirs(d, exist_ok=True)

        self._install_bundled_skills(skills_dir)
        self._install_skill_tools(skills_dir)

        # System prompt
        source = importlib.resources.files("enso").joinpath("system_prompt.md")
        content = source.read_text(encoding="utf-8")

        canonical = os.path.join(self.working_dir, "CLAUDE.md")
        if not os.path.exists(canonical):
            try:
                with open(canonical, "w") as f:
                    f.write(content)
                log.info("Wrote CLAUDE.md to %s", self.working_dir)
            except OSError:
                log.warning("Could not write CLAUDE.md", exc_info=True)
                return

        # Symlink agent instruction files to CLAUDE.md
        for name in ("AGENTS.md", "GEMINI.md"):
            self._ensure_symlink(
                os.path.join(self.working_dir, name), "CLAUDE.md"
            )

        # Symlink skills into CLI-specific discovery paths
        # .claude/skills -> ~/.enso/skills (Claude Code)
        # .agents/skills -> ~/.enso/skills (Codex + Gemini)
        for cli_dir in (".claude", ".agents"):
            parent = os.path.join(self.working_dir, cli_dir)
            os.makedirs(parent, exist_ok=True)
            self._ensure_symlink(
                os.path.join(parent, "skills"), skills_dir
            )

        # Auto-compact notification hooks — lets the user know via Telegram
        # when a provider is compacting context (which can be slow).
        # Claude: PreCompact with "auto" matcher
        # Codex: no compaction hooks available
        # Gemini: skipped — the CLI fires PreCompress unconditionally on
        # every turn (before its own threshold check), so the hook is pure
        # noise. Restore once upstream fixes the timing.
        notify_cmd = (
            "enso message send"
            " 'Autocompacting context, this might take a moment...'"
        )
        self._ensure_hook_entry(
            os.path.join(self.working_dir, ".claude", "settings.json"),
            event="PreCompact",
            matcher="auto",
            command=notify_cmd,
        )

    @staticmethod
    def _ensure_symlink(link_path: str, target: str) -> None:
        """Create a symlink if it doesn't already exist."""
        if os.path.exists(link_path) or os.path.islink(link_path):
            return
        try:
            os.symlink(target, link_path)
            log.info("Symlinked %s -> %s", link_path, target)
        except OSError:
            log.warning("Could not symlink %s", link_path, exc_info=True)

    @staticmethod
    def _ensure_hook_entry(
        settings_path: str,
        *,
        event: str,
        matcher: str,
        command: str,
    ) -> None:
        """Ensure a specific hook exists in a CLI settings file.

        Reads the file, checks whether the exact command is already
        present under the given event, and appends a new entry only
        if missing.  Other hooks and settings are left untouched.
        """
        settings: dict = {}
        if os.path.exists(settings_path):
            try:
                with open(settings_path) as f:
                    settings = json.load(f)
            except (json.JSONDecodeError, OSError):
                log.warning(
                    "Could not read %s, skipping hook install",
                    settings_path,
                )
                return

        hooks = settings.setdefault("hooks", {})
        event_hooks = hooks.setdefault(event, [])

        # Check if this exact hook command is already installed
        for entry in event_hooks:
            for h in entry.get("hooks", []):
                if h.get("command") == command:
                    return

        event_hooks.append({
            "matcher": matcher,
            "hooks": [{"type": "command", "command": command, "async": True}],
        })

        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        try:
            with open(settings_path, "w") as f:
                json.dump(settings, f, indent=2)
                f.write("\n")
            log.info("Installed %s hook in %s", event, settings_path)
        except OSError:
            log.warning("Could not write %s", settings_path, exc_info=True)

    @staticmethod
    def _install_bundled_skills(skills_dir: str) -> None:
        """Copy bundled skills to the skills directory, updating stale files."""
        bundled = importlib.resources.files("enso").joinpath("skills")
        if not bundled.is_dir():
            return
        for skill_dir in bundled.iterdir():
            if not skill_dir.is_dir():
                continue
            dest = os.path.join(skills_dir, skill_dir.name)
            os.makedirs(dest, exist_ok=True)
            for f in skill_dir.iterdir():
                if not f.is_file():
                    continue
                dest_file = os.path.join(dest, f.name)
                content = f.read_text(encoding="utf-8")
                # Skip if unchanged
                if os.path.exists(dest_file):
                    with open(dest_file) as existing:
                        if existing.read() == content:
                            continue
                with open(dest_file, "w") as out:
                    out.write(content)
                log.info("Updated bundled skill: %s/%s", skill_dir.name, f.name)

    def _install_skill_tools(self, skills_dir: str) -> None:
        """Copy executable tool scripts from skills into workspace/tools/."""
        tools_dir = os.path.join(self.working_dir, "tools")
        for entry in os.listdir(skills_dir):
            skill_path = os.path.join(skills_dir, entry)
            if not os.path.isdir(skill_path):
                continue
            for fname in os.listdir(skill_path):
                if not fname.endswith(".py"):
                    continue
                src = os.path.join(skill_path, fname)
                os.makedirs(tools_dir, exist_ok=True)
                dest = os.path.join(tools_dir, fname)
                try:
                    with open(src) as f:
                        content = f.read()
                    if os.path.exists(dest):
                        with open(dest) as f:
                            if f.read() == content:
                                continue
                    with open(dest, "w") as f:
                        f.write(content)
                    os.chmod(dest, 0o755)
                    log.info("Installed tool: %s", fname)
                except OSError:
                    log.warning("Could not install tool %s", fname, exc_info=True)

    # -- State persistence --

    def save_state(self) -> None:
        """Atomically persist session and job state to disk."""
        data: dict[str, Any] = {
            "active_provider_by_chat": {
                str(k): v for k, v in self.active_provider_by_chat.items()
            },
            "active_model_by_chat_provider": {
                f"{cid}:{prov}": model
                for (cid, prov), model in self.active_model_by_chat_provider.items()
            },
            "effort_by_chat_provider_model": {
                f"{cid}:{prov}:{model}": eff
                for (cid, prov, model), eff in self.effort_by_chat_provider_model.items()
            },
            "session_by_chat_provider": {
                f"{cid}:{prov}": sid
                for (cid, prov), sid in self.session_by_chat_provider.items()
            },
            "compact_seed_by_chat": dict(self.compact_seed_by_chat),
            "job_last_run": {
                name: ts.isoformat()
                for name, ts in self._job_last_run.items()
            },
            "last_active": {
                cid: ts.isoformat()
                for cid, ts in self._last_active.items()
            },
        }
        try:
            fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, STATE_FILE)
        except Exception:
            log.exception("Failed to save state")

    def load_state(self) -> None:
        """Load persisted state from disk."""
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            for k, v in data.get("active_provider_by_chat", {}).items():
                self.active_provider_by_chat[k] = v
            for k, v in data.get("active_model_by_chat_provider", {}).items():
                cid, provider = k.split(":", 1)
                self.active_model_by_chat_provider[(cid, provider)] = v
            for k, v in data.get("effort_by_chat_provider_model", {}).items():
                parts = k.split(":", 2)
                if len(parts) == 3:
                    cid, provider, model = parts
                    self.effort_by_chat_provider_model[(cid, provider, model)] = v
            for k, v in data.get("session_by_chat_provider", {}).items():
                cid, provider = k.split(":", 1)
                self.session_by_chat_provider[(cid, provider)] = v
            for k, v in data.get("compact_seed_by_chat", {}).items():
                self.compact_seed_by_chat[k] = v
            for name, ts in data.get("job_last_run", {}).items():
                self._job_last_run[name] = datetime.fromisoformat(ts)
            for cid, ts in data.get("last_active", {}).items():
                self._last_active[cid] = datetime.fromisoformat(ts)
            log.info(
                "Loaded state: %d providers, %d sessions",
                len(self.active_provider_by_chat),
                len(self.session_by_chat_provider),
            )
            self._prune_stale_sessions()
        except Exception:
            log.exception("Failed to load state, starting fresh")

    def _prune_stale_sessions(self) -> None:
        """Remove state entries for conversations inactive beyond SESSION_TTL_DAYS."""
        cutoff = datetime.now() - timedelta(days=SESSION_TTL_DAYS)
        stale = [cid for cid, ts in self._last_active.items() if ts < cutoff]
        if not stale:
            return
        for cid in stale:
            self.active_provider_by_chat.pop(cid, None)
            for key in [k for k in self.session_by_chat_provider if k[0] == cid]:
                self.session_by_chat_provider.pop(key)
            for key in [k for k in self.active_model_by_chat_provider if k[0] == cid]:
                self.active_model_by_chat_provider.pop(key)
            for key in [k for k in self.effort_by_chat_provider_model if k[0] == cid]:
                self.effort_by_chat_provider_model.pop(key)
            self.compact_seed_by_chat.pop(cid, None)
            self._last_active.pop(cid)
        log.info("Pruned %d stale conversation(s) (>%dd)", len(stale), SESSION_TTL_DAYS)
        self.save_state()

    # -- Accessors --

    def get_active_provider(self, chat_id: str) -> str:
        """Return active provider for chat, defaulting to claude."""
        return self.active_provider_by_chat.get(chat_id, "claude")

    def get_active_model(self, chat_id: str, provider: str) -> str:
        """Return active model for chat+provider, defaulting to first in list."""
        stored = self.active_model_by_chat_provider.get((chat_id, provider))
        if stored and stored in self.models.get(provider, []):
            return stored
        models = self.models.get(provider, [])
        return models[0] if models else "default"

    def get_active_effort(
        self, chat_id: str, provider: str, model: str,
    ) -> str | None:
        """Return the effective effort level for chat+provider+model.

        Returns ``None`` when the provider doesn't support effort or the user
        hasn't picked a level. A stored level is clamped to whatever the model
        actually accepts so callers always see the real value in use.
        """
        if provider != "claude":
            return None
        stored = self.effort_by_chat_provider_model.get((chat_id, provider, model))
        if stored is None:
            return None
        from .providers.claude import clamp_effort
        return clamp_effort(stored, model)

    def get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        """Get or create a per-chat lock to serialize requests."""
        lock = self.chat_lock_by_chat.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self.chat_lock_by_chat[chat_id] = lock
        return lock

    # -- Provider management --

    def make_provider(self, provider_name: str) -> BaseProvider:
        """Create a fresh provider instance."""
        providers_cfg = self.config.get("providers", {})
        provider_cfg = providers_cfg.get(provider_name, {})
        path = provider_cfg.get("path", provider_name)
        provider = get_provider(provider_name, path, provider_cfg)
        log.debug(
            "Resolved provider name=%s class=%s path=%s runner=%s",
            provider_name,
            provider.__class__.__name__,
            provider.path,
            provider_cfg.get("runner", "default"),
        )
        return provider

    # -- Session management --

    # Providers that support pre-assigned session IDs. For these,
    # Enso generates the ID upfront so it persists across restarts.
    # Other providers (codex, gemini) generate their own IDs which we
    # capture from stream events.
    _SELF_MANAGED_SESSIONS: ClassVar[set[str]] = {"claude"}

    def _get_or_create_session(
        self, chat_id: str, provider_name: str
    ) -> str | None:
        """Get existing session ID, or generate one for providers that support it.

        For self-managed providers (Claude), generates a UUID upfront and
        stores it. The first call uses --session-id to create the session;
        subsequent calls use --resume. We track this by prefixing new
        (unused) session IDs with 'new:'.
        """
        key = (chat_id, provider_name)
        session_id = self.session_by_chat_provider.get(key)
        if session_id:
            return session_id
        if provider_name in self._SELF_MANAGED_SESSIONS:
            import uuid

            session_id = "new:" + str(uuid.uuid4())
            self.session_by_chat_provider[key] = session_id
            self.save_state()
            log.info(
                "[%s] Created session for chat %s",
                provider_name, chat_id,
            )
            return session_id
        return None

    # -- Dispatch & queue --

    async def dispatch(
        self,
        conversation_id: str,
        prompt: str,
        ctx: TransportContext,
        *,
        preview: str = "",
    ) -> None:
        """Dispatch a prompt, queuing if a request is already running."""
        self._last_active[conversation_id] = datetime.now()
        lock = self.get_chat_lock(conversation_id)

        if lock.locked():
            queue = self._queue_by_conversation.setdefault(
                conversation_id, deque()
            )
            if len(queue) >= MAX_QUEUE_SIZE:
                await ctx.reply(f"Queue full ({MAX_QUEUE_SIZE}).")
                return
            queue.append(_QueuedItem(prompt=prompt, ctx=ctx, preview=preview))
            pos = len(queue)
            label = f"{preview}\u2026" if len(preview) == 50 else preview
            await ctx.reply(f"Queued (#{pos}): {label}")
            log.info("Queued #%d for %s: %s", pos, conversation_id, preview)
            return

        provider = self.get_active_provider(conversation_id)
        log.info(
            "Dispatch: conv=%s provider=%s prompt_len=%d",
            conversation_id, provider, len(prompt),
        )
        log.debug("Dispatch prompt:\n%s", prompt)

        async with lock:
            await self._run_request(provider, prompt, conversation_id, ctx)
            await self._drain_queue(conversation_id)

    async def _run_request(
        self, provider: str, prompt: str, conv_id: str, ctx: TransportContext,
    ) -> None:
        """Run a single provider request, tracking the task for cancellation."""
        task = asyncio.create_task(
            self.process_request(provider, prompt, conv_id, ctx)
        )
        self.running_task_by_chat[conv_id] = task
        try:
            await task
        except asyncio.CancelledError:
            log.info("Task cancelled for conv=%s", conv_id)
        finally:
            if self.running_task_by_chat.get(conv_id) is task:
                self.running_task_by_chat.pop(conv_id, None)

    async def _drain_queue(self, conv_id: str) -> None:
        """Process queued messages one by one until the queue is empty."""
        queue = self._queue_by_conversation.get(conv_id)
        if not queue:
            return
        while queue:
            item = queue.popleft()
            provider = self.get_active_provider(conv_id)
            log.info(
                "Dequeuing for conv=%s (%d remaining): %s",
                conv_id, len(queue), item.preview,
            )
            await self._run_request(provider, item.prompt, conv_id, item.ctx)

    def get_queue(self, conv_id: str) -> list[str]:
        """Return preview strings for queued items."""
        queue = self._queue_by_conversation.get(conv_id)
        if not queue:
            return []
        return [item.preview for item in queue]

    def clear_queue(self, conv_id: str) -> int:
        """Clear the queue for a conversation. Returns count of items cleared."""
        queue = self._queue_by_conversation.get(conv_id)
        if not queue:
            return 0
        count = len(queue)
        queue.clear()
        return count

    def remove_from_queue(self, conv_id: str, index: int) -> bool:
        """Remove item at index from the queue. Returns True if removed."""
        queue = self._queue_by_conversation.get(conv_id)
        if queue and 0 <= index < len(queue):
            del queue[index]
            log.info("Removed queue item %d for conv=%s", index, conv_id)
            return True
        return False

    # -- Compaction --

    _COMPACTION_PROMPT: ClassVar[str] = (
        "[System task — context compaction. This is not a user message; "
        "the user has asked to compact this conversation.]\n\n"
        "Produce a concise summary of our conversation so far for handoff "
        "to a fresh session. Cover:\n"
        "- Key decisions and conclusions reached\n"
        "- In-progress work and its current state\n"
        "- Important file paths, IDs, URLs, commands, or names referenced\n"
        "- Open questions or pending follow-ups\n"
        "- User preferences or constraints established\n\n"
        "Skip the mechanics of tool calls — focus on outcomes. Aim for "
        "300-600 words.\n\n"
        "Respond with ONLY the summary text. No preamble, no sign-off, no "
        "meta-commentary."
    )

    def _consume_compact_seed(
        self, chat_id: str, prompt: str, provider_name: str,
    ) -> str:
        """Prepend any pending compact seed to ``prompt`` and consume it.

        Seeds are stashed by /compact and primed onto the very next user
        turn for the chat — one-shot consumption so they don't keep
        re-injecting on every message. Returns the prompt unchanged when
        no seed is pending.
        """
        seed = self.compact_seed_by_chat.pop(chat_id, None)
        if not seed:
            return prompt
        self.save_state()
        log.info(
            "[%s] Injected compact seed (%d chars) for chat %s",
            provider_name, len(seed), chat_id,
        )
        return (
            "[Continuing from a previous session — that conversation was "
            "compacted to save tokens. Summary of prior context:\n\n"
            f"{seed}\n\n"
            "---\n"
            "End of prior-session summary. Below is the user's next message.]"
            f"\n\n{prompt}"
        )

    async def run_compaction(self, chat_id: str, provider_name: str) -> str:
        """Run a hidden summarisation pass and return the summary text.

        Called by /compact. Drives ``run_provider`` directly so the user
        sees nothing in chat — no status messages, no ticker, no replies.
        The caller (cmd_compact_async) holds the chat lock for the duration
        so no parallel dispatch races us. Returns an empty string on error.
        """
        lock = self.get_chat_lock(chat_id)
        if lock.locked():
            log.warning("run_compaction skipped — chat %s is busy", chat_id)
            return ""

        async with lock:
            provider = self.make_provider(provider_name)
            model = self.get_active_model(chat_id, provider_name)
            effort = self.get_active_effort(chat_id, provider_name, model)

            log.info(
                "[%s] Compacting chat=%s model=%s effort=%s",
                provider_name, chat_id, model, effort or "-",
            )

            response_parts: list[str] = []
            try:
                async for event in self.run_provider(
                    provider, self._COMPACTION_PROMPT, chat_id, model,
                    effort=effort,
                ):
                    if event.kind == "response":
                        response_parts.append(event.text)
                    elif event.kind == "error":
                        log.warning(
                            "Compaction error in chat %s: %s", chat_id, event.text,
                        )
                        return ""
            except Exception:
                log.exception("Compaction failed for chat %s", chat_id)
                return ""

            return provider.format_response(response_parts).strip()

    # -- Process control --

    async def _spawn_process(self, *cmd: str, **kwargs: Any) -> Process:
        """Spawn a subprocess isolated in its own process group when possible."""
        if os.name != "nt":
            kwargs.setdefault("start_new_session", True)
        return await asyncio.create_subprocess_exec(*cmd, **kwargs)

    async def _terminate_process_tree(
        self,
        process: Process,
        label: str,
        *,
        grace: float = PROCESS_TERMINATE_GRACE_SECS,
    ) -> None:
        """Terminate a subprocess and any children in its process group."""
        if process.returncode is not None:
            return

        pgid: int | None = None
        if os.name != "nt":
            try:
                pgid = os.getpgid(process.pid)
            except ProcessLookupError:
                pgid = None

        def signal_process(sig: signal.Signals) -> None:
            try:
                if pgid is not None:
                    os.killpg(pgid, sig)
                elif sig == signal.SIGTERM:
                    process.terminate()
                else:
                    process.kill()
            except ProcessLookupError:
                pass

        log.info(
            "Terminating process tree for %s pid=%s pgid=%s", label, process.pid, pgid,
        )
        signal_process(signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=grace)
            return
        except asyncio.TimeoutError:
            pass

        log.warning("Process tree for %s did not exit after %.1fs; killing", label, grace)
        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
        signal_process(kill_signal)
        try:
            await asyncio.wait_for(process.wait(), timeout=grace)
        except asyncio.TimeoutError:
            log.error(
                "Process tree for %s still did not exit after SIGKILL pid=%s pgid=%s",
                label, process.pid, pgid,
            )

    async def _communicate_with_timeout(
        self,
        process: Process,
        label: str,
        timeout_secs: int,
    ) -> tuple[bytes, bytes | None, bool]:
        """Communicate with a child process and kill its tree on timeout."""
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout_secs,
            )
            return stdout, stderr, False
        except asyncio.CancelledError:
            log.warning("%s was cancelled; terminating process tree", label)
            await self._terminate_process_tree(process, label)
            raise
        except asyncio.TimeoutError:
            log.warning("%s timed out after %ds", label, timeout_secs)
            await self._terminate_process_tree(process, label)
            with contextlib.suppress(Exception):
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=1)
                return stdout, stderr, True
            return b"", b"", True

    async def stop_chat(self, chat_id: str) -> tuple[bool, str | None]:
        """Stop running process/task for a chat. Returns (had_something, error_msg)."""
        process = self.running_process_by_chat.get(chat_id)
        task = self.running_task_by_chat.get(chat_id)

        if process is None and task is None:
            return False, None

        try:
            if process and process.returncode is None:
                await self._terminate_process_tree(
                    process, f"chat {chat_id}", grace=0.5,
                )
            if task and not task.done():
                task.cancel()
            return True, None
        except Exception as exc:
            return True, str(exc)

    # -- Core streaming --

    async def run_provider(
        self,
        provider: BaseProvider,
        prompt: str,
        chat_id: str,
        model: str,
        *,
        effort: str | None = None,
        extra_env: dict[str, str] | None = None,
    ):
        """Spawn a provider subprocess and yield StreamEvents.

        ``extra_env`` is merged on top of ``os.environ`` for the subprocess
        only (parent env is never mutated). Typically carries
        ``ENSO_ORIGIN_*`` so commands like ``enso message send`` can route
        back to the triggering conversation without an explicit ``--to``.
        """
        session_id = self._get_or_create_session(chat_id, provider.name)
        cmd = provider.build_command(prompt, model, session_id, effort=effort)
        log.info(
            "[%s] spawning class=%s chat=%s model=%s effort=%s session=%s prompt_len=%d",
            provider.name,
            provider.__class__.__name__,
            chat_id,
            model,
            effort or "-",
            session_id or "-",
            len(prompt),
        )
        log.debug("[%s] command=%s", provider.name, _redacted_command(cmd))

        # Strip new: prefix immediately so future calls use --resume
        # even if this run crashes before emitting a result event.
        key = (chat_id, provider.name)
        if session_id and session_id.startswith("new:"):
            self.session_by_chat_provider[key] = session_id.removeprefix("new:")
            self.save_state()

        stderr = (
            asyncio.subprocess.STDOUT
            if provider.stderr_to_stdout()
            else asyncio.subprocess.PIPE
        )
        kwargs: dict[str, Any] = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": stderr,
            "cwd": self.working_dir,
        }
        limit = provider.stdout_limit()
        if limit:
            kwargs["limit"] = limit
        if extra_env:
            merged = os.environ.copy()
            merged.update(extra_env)
            kwargs["env"] = merged
            log.debug(
                "[%s] subprocess cwd=%s extra_env_keys=%s",
                provider.name,
                self.working_dir,
                sorted(extra_env),
            )
        else:
            log.debug("[%s] subprocess cwd=%s extra_env_keys=[]", provider.name, self.working_dir)

        process = await self._spawn_process(*cmd, **kwargs)
        log.info("[%s] pid=%s", provider.name, process.pid)
        self.running_process_by_chat[chat_id] = process

        event_count = 0
        try:
            assert process.stdout is not None
            async for line in process.stdout:
                decoded = line.decode(errors="replace").strip()
                if not decoded:
                    continue
                raw = provider.parse_line(decoded)
                if raw is None:
                    continue
                event_count += 1
                if self.debug_events:
                    log.debug(
                        "[%s] raw_event=%d type=%s status=%s keys=%s",
                        provider.name,
                        event_count,
                        raw.get("type", "-"),
                        raw.get("status", "-"),
                        sorted(raw),
                    )
                parsed_events = provider.parse_event(raw)
                if self.debug_events and not parsed_events:
                    log.debug(
                        "[%s] raw_event=%d emitted no stream events",
                        provider.name,
                        event_count,
                    )
                for stream_event in parsed_events:
                    if self.debug_events:
                        log.debug(
                            "[%s] stream_event raw=%d kind=%s text_len=%d session=%s usage=%s",
                            provider.name,
                            event_count,
                            stream_event.kind,
                            len(stream_event.text or ""),
                            stream_event.session_id or "-",
                            stream_event.usage or "-",
                        )
                    if stream_event.kind == "session" and stream_event.session_id:
                        self.session_by_chat_provider[
                            (chat_id, provider.name)
                        ] = stream_event.session_id
                        self.save_state()
                    yield stream_event

            # Surface stderr as an error when the process fails
            await process.wait()
            rc = process.returncode
            if rc and not provider.stderr_to_stdout() and process.stderr:
                stderr_data = await process.stderr.read()
                if stderr_data:
                    stderr_text = stderr_data.decode(errors="replace").strip()[:2000]
                    log.error("[%s] stderr: %s", provider.name, stderr_text)
                    yield StreamEvent(kind="error", text=stderr_text)
        finally:
            if process.returncode is None:
                await self._terminate_process_tree(
                    process, f"{provider.name} chat {chat_id}", grace=1.0,
                )
            rc = process.returncode
            log.info("[%s] pid=%s exit=%s events=%d", provider.name, process.pid, rc, event_count)
            if self.running_process_by_chat.get(chat_id) is process:
                self.running_process_by_chat.pop(chat_id, None)

    # -- Request handling --

    async def process_request(
        self,
        provider_name: str,
        prompt: str,
        chat_id: str,
        ctx: TransportContext,
    ) -> None:
        """Run a full provider request with status ticker and response delivery.

        Automatically injects any pending background messages into the prompt.
        """
        # Inject background messages
        bg = messages.consume()
        if bg:
            prompt = f"{messages.format_for_injection(bg)}\n\n{prompt}"
            log.info("[%s] Injected %d background message(s) into prompt", provider_name, len(bg))

        # Inject compact seed if one is pending for this chat.
        prompt = self._consume_compact_seed(chat_id, prompt, provider_name)

        provider = self.make_provider(provider_name)
        model = self.get_active_model(chat_id, provider_name)
        effort = self.get_active_effort(chat_id, provider_name, model)
        display = provider_name.capitalize()
        effort_part = f" / {effort}" if effort else ""

        try:
            origin_env = ctx.get_origin_env()
        except Exception:
            log.warning("get_origin_env failed for chat %s", chat_id, exc_info=True)
            origin_env = {}

        log.info(
            "[%s] request chat=%s provider_class=%s model=%s effort=%s "
            "prompt_len=%d preview=%.120s",
            provider_name,
            chat_id,
            provider.__class__.__name__,
            model,
            effort or "-",
            len(prompt),
            prompt,
        )
        log.debug("[%s] origin_env_keys=%s", provider_name, sorted(origin_env))
        if self.debug_prompts:
            log.debug("[%s] full_prompt:\n%s", provider_name, prompt)

        await ctx.send_typing()
        status_msg = None
        try:
            status_msg = await ctx.reply_status(
                f"({display}{effort_part} / 0s) Working…"
            )
        except Exception:
            log.warning("Failed to send initial status message for chat %s", chat_id, exc_info=True)
        state = {
            "status": "Working…",
            "elapsed": 0,
            "display": display,
            "effort_part": effort_part,
        }
        stop = asyncio.Event()
        ticker = asyncio.create_task(self._run_ticker(ctx, status_msg, state, stop))

        response_parts: list[str] = []
        error_text = ""
        usage_pct: int | None = None

        try:
            async for event in self.run_provider(
                provider, prompt, chat_id, model,
                effort=effort, extra_env=origin_env,
            ):
                if self.debug_events:
                    log.debug(
                        "[%s] handling_event kind=%s text_len=%d session=%s usage=%s",
                        provider_name,
                        event.kind,
                        len(event.text or ""),
                        event.session_id or "-",
                        event.usage or "-",
                    )
                if event.kind == "status":
                    state["status"] = event.text
                elif event.kind == "response":
                    response_parts.append(event.text)
                elif event.kind == "error":
                    error_text = event.text
                elif event.kind == "usage" and event.usage:
                    usage_pct = event.usage.get("pct")

            stop.set()
            ticker.cancel()
            if status_msg is not None:
                await ctx.delete_status(status_msg)

            response_text = provider.format_response(response_parts)
            log.info(
                "[%s] request complete chat=%s response_parts=%d "
                "response_len=%d error=%s usage_pct=%s elapsed=%s",
                provider_name,
                chat_id,
                len(response_parts),
                len(response_text),
                bool(error_text),
                usage_pct if usage_pct is not None else "-",
                state["elapsed"],
            )
            usage_part = f" / {usage_pct}%" if usage_pct is not None else ""
            prefix = f"({display}{effort_part}{usage_part} / {state['elapsed']}s)"

            msg_limit = self.transport.message_limit if self.transport else 4096
            if response_text:
                for chunk in split_text(f"{prefix}\n{response_text}", limit=msg_limit):
                    await ctx.reply(chunk)
            elif error_text:
                await ctx.reply(f"{prefix} Error: {error_text[:4000]}")
            else:
                await ctx.reply(f"{prefix} (No response)")

        except asyncio.CancelledError:
            stop.set()
            ticker.cancel()
            if status_msg is not None:
                with contextlib.suppress(Exception):
                    await ctx.edit_status(status_msg, f"({display}{effort_part}) Stopped.")
            raise

        except Exception as exc:
            stop.set()
            ticker.cancel()
            log.error("Error processing %s request: %s", provider_name, exc, exc_info=True)
            prefix = f"({display}{effort_part} / {state['elapsed']}s)"
            try:
                if status_msg is not None:
                    await ctx.edit_status(status_msg, f"{prefix} Error: {str(exc)[:4000]}")
                    return
            except Exception:
                pass
            msg_limit = self.transport.message_limit if self.transport else 4096
            for chunk in split_text(f"{prefix} Error: {exc}", limit=msg_limit):
                await ctx.reply(chunk)

    async def _run_ticker(
        self, ctx: TransportContext, status_msg: Any | None, state: dict, stop: asyncio.Event
    ) -> None:
        """Background task that updates status and typing indicator."""
        status_updates_enabled = status_msg is not None
        while not stop.is_set():
            await asyncio.sleep(1)
            if stop.is_set():
                break
            state["elapsed"] += 1
            if status_updates_enabled and _status_edit_due(state["elapsed"]):
                text = (
                    f"({state['display']}{state.get('effort_part', '')} / "
                    f"{state['elapsed']}s) {state['status']}"
                )
                try:
                    await asyncio.wait_for(ctx.edit_status(status_msg, text), timeout=5.0)
                except Exception:
                    status_updates_enabled = False
                    log.warning(
                        "Disabling status updates for current request after edit failure",
                        exc_info=True,
                    )
            # Refresh typing indicator every 4s (expires after 5s)
            if state["elapsed"] % 4 == 0:
                with contextlib.suppress(Exception):
                    await ctx.send_typing()

    # -- Job scheduler --

    async def run_job_scheduler(self) -> None:
        """Check for jobs to run every 60 seconds. Runs as a background task."""
        log.info("Job scheduler started")
        while True:
            await asyncio.sleep(60)
            now = datetime.now()
            for job in load_jobs():
                if not job.enabled:
                    continue
                if job.dir_name in self._running_job_tasks:
                    continue
                if self._should_run_job(job, now):
                    self._job_last_run[job.dir_name] = now
                    self.save_state()
                    task = asyncio.create_task(self._run_job_task(job))
                    self._running_job_tasks[job.dir_name] = task
                    task.add_done_callback(
                        lambda _task, name=job.dir_name: self._running_job_tasks.pop(
                            name, None,
                        )
                    )

    async def _run_job_task(self, job: Job) -> None:
        """Run one scheduled job without blocking the scheduler loop."""
        async with self._job_semaphore:
            try:
                await self._execute_job(job)
            except Exception:
                log.exception("Job '%s' failed", job.name)
                self._job_last_run[job.dir_name] = datetime.now()
                self.save_state()

    def _should_run_job(self, job: Job, now: datetime) -> bool:
        """Check if a job should run based on its cron schedule."""
        last_run = self._job_last_run.get(job.dir_name)
        if last_run is None:
            # First time seeing this job — record now, don't fire immediately
            self._job_last_run[job.dir_name] = now
            self.save_state()
            return False
        cron = croniter(job.schedule, last_run)
        next_run = cron.get_next(datetime)
        if next_run > now:
            return False
        if (
            not job.catch_up
            and (now - next_run).total_seconds() > job.misfire_grace_seconds
        ):
            log.warning(
                "Job '%s' missed scheduled run at %s by more than %ss; skipping catch-up",
                job.name, next_run.isoformat(), job.misfire_grace_seconds,
            )
            self._job_last_run[job.dir_name] = now
            self.save_state()
            return False
        return True

    async def _execute_job(self, job: Job) -> None:
        """Run a job: prerun gate, provider subprocess, notify, queue message."""

        # Prerun gate
        prerun_output = ""
        if job.prerun:
            prerun_script = os.path.join(job.job_dir, job.prerun)
            if os.path.isfile(prerun_script):
                proc = await self._spawn_process(
                    "bash", prerun_script,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=job.job_dir,
                )
                stdout, stderr, timed_out = await self._communicate_with_timeout(
                    proc, f"Job '{job.name}' prerun", job.prerun_timeout,
                )
                if timed_out:
                    self._job_last_run[job.dir_name] = datetime.now()
                    self.save_state()
                    return
                if proc.returncode != 0:
                    if proc.returncode == 1:
                        log.debug("Job '%s' prerun: no work, skipping", job.name)
                    else:
                        err = stderr.decode(errors="replace").strip()
                        log.warning(
                            "Job '%s' prerun error (exit %s): %s",
                            job.name, proc.returncode, err or "(no stderr)",
                        )
                    self._job_last_run[job.dir_name] = datetime.now()
                    self.save_state()
                    return
                prerun_output = stdout.decode(errors="replace").strip()

        log.info("Running job: %s (%s/%s)", job.name, job.provider, job.model)

        # Build prompt with prerun output injection
        prompt = job.prompt
        if prerun_output:
            prompt = prompt.replace("{{prerun_output}}", prerun_output)

        # Run provider in batch mode
        provider = self.make_provider(job.provider)
        cmd = provider.build_batch_command(prompt, job.model)
        log.debug(
            "Job '%s' command provider_class=%s cwd=%s cmd=%s",
            job.name,
            provider.__class__.__name__,
            self.working_dir,
            _redacted_command(cmd),
        )

        proc = await self._spawn_process(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self.working_dir,
        )
        stdout, _, timed_out = await self._communicate_with_timeout(
            proc, f"Job '{job.name}'", job.timeout,
        )
        if timed_out:
            output = f"Job timed out after {job.timeout}s; process tree was terminated"
            if self.transport:
                await self.transport.notify(
                    f"\u26a0\ufe0f [{job.name}] {output}",
                    destination=job.notify,
                )
            self._job_last_run[job.dir_name] = datetime.now()
            self.save_state()
            return
        output = stdout.decode(errors="replace").strip()

        # Only notify on failure — successful jobs handle their own messaging
        if proc.returncode != 0:
            if self.transport:
                label = f"{job.name} (exit {proc.returncode})"
                await self.transport.notify(
                    f"\u26a0\ufe0f [{label}]\n{output}"[:4096],
                    destination=job.notify,
                )
            messages.send(output, source=f"job:{job.dir_name}")

        self._job_last_run[job.dir_name] = datetime.now()
        self.save_state()
        log.info("Job '%s' completed (exit %s)", job.name, proc.returncode)
