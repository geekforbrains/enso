"""Enso runtime — the engine that makes agents go."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.resources
import json
import logging
import os
import re
import shlex
import signal
import tempfile
from asyncio.subprocess import Process
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from croniter import croniter

from . import messages, runs
from .config import (
    CONFIG_DIR,
    DEFAULT_AGENT,
    SKILL_TOMBSTONES_DIRNAME,
    STATE_FILE,
    provider_models,
)
from .jobs import Job, job_config_error, load_jobs
from .logging_config import logging_flags
from .providers import PROVIDER_NAMES, BaseProvider, StreamEvent, provider_class

if TYPE_CHECKING:
    from .transports import BaseTransport, TransportContext

log = logging.getLogger(__name__)


PROGRESS_MESSAGES = (
    "Thinking hard",
    "Crunching data",
    "Poking at files",
    "Grabbing a coffee",
    "Connecting dots",
    "Following breadcrumbs",
    "Turning over rocks",
    "Untangling things",
    "Reading the fine print",
    "Asking electrons nicely",
)


def progress_text(elapsed: int) -> str:
    """Return the provider-neutral progress text for an elapsed second."""
    message = PROGRESS_MESSAGES[elapsed % len(PROGRESS_MESSAGES)]
    return f"({elapsed}s) {message}"


async def _cancel_and_wait(task: asyncio.Task[Any]) -> BaseException | None:
    """Cancel a child task without swallowing cancellation of the caller."""
    task.cancel()
    result = (await asyncio.gather(task, return_exceptions=True))[0]
    return result if isinstance(result, BaseException) else None


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
JOB_FAILURE_RENOTIFY_SECS = int(
    os.environ.get("ENSO_JOB_FAILURE_RENOTIFY_SECS", str(24 * 60 * 60))
)
PRERUN_DIAGNOSTIC_LIMIT = 500

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)([A-Za-z0-9_-]*(?:api[_-]?key|token|secret|password|authorization)"
    r"[A-Za-z0-9_-]*)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_URL_CREDENTIAL_RE = re.compile(r"(?i)(https?://)([^/@\s]+)@")
_SENSITIVE_KEY_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)")

# Upgrade markers for artifacts bundled immediately before the built-in task
# system was removed. Hashes let us recognize pristine installer-owned files
# without retaining obsolete task instructions in the package.
_LEGACY_TASKS_SKILL_SHA256 = (
    "661ffca9a360cc40521c274a295a97c7735123a7c8a44e1d307da046f07735cc"
)
_LEGACY_TASKS_AGENTS_SHA256 = (
    "ec67ee973a15c38e23451cfc65317643debe0e6e8659589bf0c30433f60a2e4a"
)
_LEGACY_TASK_RUNNER_STATE_KEY = "__task_runner__"

# Known hashes of pristine bundled skills from prior releases. Exact matches
# can follow the bundled copy forward without overwriting user-customized files.
_BUNDLED_SKILL_PRISTINE_HASHES: dict[tuple[str, str], frozenset[str]] = {
    ("jobs", "SKILL.md"): frozenset({
        "f52f890e467bd212534474b1d0ee913edbf6cc968e010686153044aac13bcd77",
        "8824886bd76e476672395bfcef6d34655b7eeedb40c89cf0fc459706e9ad4cff",
        "cc8d7abc0e550b901644d7c7feee2e3363608adf794a2f885c7421a5cb7fa08b",
        "256ce5a5609551246927c9e19ef0be13f68f630fb343815f303e6f90ab8cb51c",
        "608c4a5d9f34d76ae9143f749fa7b028a4fce413d260e1a5f58d361288730bd8",
        "1756397ae5838a5aba08c6371cb721f9e1b4f815c8b1907a19b017e7aca53be0",
        "dabb0fa66f276cd78c8e88e17c38155ad537aa52938e50622dcc2955b70f036a",
    }),
}


@dataclass(frozen=True)
class PrerunResult:
    """Structured result from the deterministic gate before a job provider."""

    outcome: Literal["open", "no_work", "error", "timeout"]
    output: str = ""
    diagnostic: str = ""
    exit_code: int | None = None


@dataclass(frozen=True)
class JobRunResult:
    """Terminal result shared by scheduled, CLI, and web job execution."""

    status: Literal[
        "ok", "error", "timeout", "no_work", "prerun_error", "prerun_timeout"
    ]
    run_id: str | None = None
    output: str = ""
    exit_code: int | None = None


def _redacted_command(cmd: list[str]) -> str:
    """Return a shell-like command string with the prompt argument redacted."""
    if "--" in cmd:
        sep = cmd.index("--")
        prompt_chars = sum(len(part) for part in cmd[sep + 1 :])
        return shlex.join([*cmd[: sep + 1], f"<prompt chars={prompt_chars}>"])
    for flag in ("--prompt", "--print", "-p"):
        if flag in cmd:
            prompt_index = cmd.index(flag) + 1
            if prompt_index < len(cmd):
                redacted = list(cmd)
                redacted[prompt_index] = f"<prompt chars={len(cmd[prompt_index])}>"
                return shlex.join(redacted)
    return shlex.join(cmd)


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
        self.models: dict[str, list[str]] = provider_models(config)
        agent_config = config.get("agent")
        timeout = (
            agent_config.get("timeout")
            if isinstance(agent_config, dict)
            else DEFAULT_AGENT["timeout"]
        )
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 0:
            timeout = DEFAULT_AGENT["timeout"]
        self.agent_timeout: int | float = timeout
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
        self._job_failure_alerts: dict[str, dict[str, Any]] = {}
        self._running_job_tasks: dict[str, asyncio.Task] = {}
        self._job_semaphore = asyncio.Semaphore(JOB_CONCURRENCY)

        # Set while the self-updater validates and installs a release. New
        # agent turns and scheduler ticks pause until the operation finishes.
        self._update_in_progress = False

    # -- Workspace setup --

    def install_system_prompts(self) -> None:
        """Set up working directory, system prompts, skills, hooks, and config dirs.

        Creates:
        - ~/.enso/jobs/ and ~/.enso/skills/
        - Bundled skills seeded into ~/.enso/skills/
        - AGENTS.md in working_dir (from bundled template on first install)
        - CLAUDE.md as a symlink to AGENTS.md (Claude reads CLAUDE.md;
          Codex reads AGENTS.md natively)
        - .claude/skills and .agents/skills symlinked to ~/.enso/skills/
          (so Claude and Codex auto-discover skills)
        - Auto-compact notification hooks for Claude
        """
        from .config import JOBS_DIR

        skills_dir = os.path.join(CONFIG_DIR, "skills")
        for d in (JOBS_DIR, skills_dir):
            os.makedirs(d, exist_ok=True)

        self._retire_legacy_tasks_skill(skills_dir)
        self._install_bundled_skills(skills_dir)
        self._install_skill_tools(skills_dir)

        # System prompt. AGENTS.md is canonical; Claude reads CLAUDE.md, so
        # it's symlinked to AGENTS.md. Codex reads AGENTS.md natively, so no
        # further symlinks are needed.
        source = importlib.resources.files("enso").joinpath("prompts", "AGENTS.md")
        content = source.read_text(encoding="utf-8")

        canonical = os.path.join(self.working_dir, "AGENTS.md")
        is_legacy_template = (
            self._regular_file_sha256(canonical) == _LEGACY_TASKS_AGENTS_SHA256
        )
        if not os.path.lexists(canonical) or is_legacy_template:
            try:
                self._atomic_write_text(canonical, content)
                action = "Updated" if is_legacy_template else "Wrote"
                log.info("%s AGENTS.md in %s", action, self.working_dir)
            except OSError:
                log.warning("Could not write AGENTS.md", exc_info=True)
                return
        elif self._contains_legacy_task_instructions(canonical):
            log.warning(
                "Preserving customized AGENTS.md at %s, but it contains retired task "
                "instructions; update or remove those instructions manually",
                canonical,
            )

        self._ensure_symlink(
            os.path.join(self.working_dir, "CLAUDE.md"), "AGENTS.md"
        )

        # Symlink skills into CLI-specific discovery paths
        # .claude/skills -> ~/.enso/skills (Claude Code)
        # .agents/skills -> ~/.enso/skills (Codex)
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
    def _atomic_write_text(path: str, content: str) -> None:
        """Fsync a temporary UTF-8 file, then atomically replace ``path``."""
        directory = os.path.dirname(os.path.abspath(path))
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp)
            raise

    @staticmethod
    def _regular_file_sha256(path: str) -> str | None:
        """Hash a regular, non-symlink file, or return ``None`` safely."""
        if os.path.islink(path) or not os.path.isfile(path):
            return None
        try:
            with open(path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except OSError:
            return None

    @staticmethod
    def _contains_legacy_task_instructions(path: str) -> bool:
        """Recognize strong task-era markers in a customized prompt."""
        if not os.path.isfile(path):
            return False
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeError):
            return False
        return any(marker in content for marker in (
            "enso task create",
            "enso task list",
            "enso task show",
            "use the `tasks` skill",
        ))

    @classmethod
    def _retire_legacy_tasks_skill(cls, skills_dir: str) -> None:
        """Remove the pristine task skill left by the previous release.

        Any changed file, symlink, or additional directory entry makes the
        artifact user-owned and leaves it untouched.
        """
        skill_dir = os.path.join(skills_dir, "tasks")
        if not os.path.lexists(skill_dir):
            return
        warning = (
            "Preserving customized retired tasks skill at %s; update or remove it "
            "manually because the enso task commands no longer exist"
        )
        if os.path.islink(skill_dir) or not os.path.isdir(skill_dir):
            log.warning(warning, skill_dir)
            return
        try:
            entries = os.listdir(skill_dir)
        except OSError:
            log.warning(warning, skill_dir)
            return
        if entries != ["SKILL.md"]:
            log.warning(warning, skill_dir)
            return

        skill_file = os.path.join(skill_dir, "SKILL.md")
        if cls._regular_file_sha256(skill_file) != _LEGACY_TASKS_SKILL_SHA256:
            log.warning(warning, skill_dir)
            return
        try:
            os.remove(skill_file)
            os.rmdir(skill_dir)
            log.info("Removed retired bundled skill: tasks")
        except OSError:
            log.warning("Could not remove retired bundled tasks skill", exc_info=True)

    @classmethod
    def _install_bundled_skills(cls, skills_dir: str) -> None:
        """Seed missing skills and update only known-pristine older copies."""
        bundled = importlib.resources.files("enso").joinpath("skills")
        if not bundled.is_dir():
            return
        tombstones_dir = os.path.join(skills_dir, SKILL_TOMBSTONES_DIRNAME)
        for skill_dir in bundled.iterdir():
            if not skill_dir.is_dir():
                continue
            tombstone = os.path.join(
                tombstones_dir, f"{skill_dir.name}.deleted"
            )
            if os.path.lexists(tombstone):
                log.info("Preserving deleted bundled skill: %s", skill_dir.name)
                continue
            dest = os.path.join(skills_dir, skill_dir.name)
            if os.path.lexists(dest):
                if os.path.islink(dest) or not os.path.isdir(dest):
                    continue
            else:
                os.makedirs(dest)
            for f in skill_dir.iterdir():
                if not f.is_file():
                    continue
                dest_file = os.path.join(dest, f.name)
                action = "Installed"
                if os.path.lexists(dest_file):
                    existing_hash = cls._regular_file_sha256(dest_file)
                    known_pristine = _BUNDLED_SKILL_PRISTINE_HASHES.get(
                        (skill_dir.name, f.name), frozenset()
                    )
                    if existing_hash not in known_pristine:
                        continue
                    action = "Updated pristine"
                try:
                    content = f.read_text(encoding="utf-8")
                    cls._atomic_write_text(dest_file, content)
                    log.info(
                        "%s bundled skill: %s/%s",
                        action,
                        skill_dir.name,
                        f.name,
                    )
                except OSError:
                    log.warning(
                        "Could not install/update bundled skill %s/%s",
                        skill_dir.name,
                        f.name,
                        exc_info=True,
                    )

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
            "job_failure_alerts": dict(self._job_failure_alerts),
            "last_active": {
                cid: ts.isoformat()
                for cid, ts in self._last_active.items()
            },
        }
        tmp: str | None = None
        try:
            fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, STATE_FILE)
        except Exception:
            log.exception("Failed to save state")
        finally:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.remove(tmp)

    def load_state(self) -> None:
        """Load persisted state from disk."""
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            state_changed = False
            for k, v in data.get("active_provider_by_chat", {}).items():
                if v in PROVIDER_NAMES:
                    self.active_provider_by_chat[k] = v
                else:
                    state_changed = True
            for k, v in data.get("active_model_by_chat_provider", {}).items():
                cid, provider = k.split(":", 1)
                # Entries for retired providers or models removed from config
                # are inert (selection falls back anyway) — prune them.
                if v in self.models.get(provider, []):
                    self.active_model_by_chat_provider[(cid, provider)] = v
                else:
                    state_changed = True
            for k, v in data.get("effort_by_chat_provider_model", {}).items():
                parts = k.split(":", 2)
                if len(parts) == 3:
                    cid, provider, model = parts
                    if model in self.models.get(provider, []):
                        self.effort_by_chat_provider_model[(cid, provider, model)] = v
                    else:
                        state_changed = True
            for k, v in data.get("session_by_chat_provider", {}).items():
                cid, provider = k.split(":", 1)
                if provider in PROVIDER_NAMES:
                    self.session_by_chat_provider[(cid, provider)] = v
                else:
                    state_changed = True
            for k, v in data.get("compact_seed_by_chat", {}).items():
                self.compact_seed_by_chat[k] = v
            for name, ts in data.get("job_last_run", {}).items():
                if name == _LEGACY_TASK_RUNNER_STATE_KEY:
                    state_changed = True
                    continue
                self._job_last_run[name] = datetime.fromisoformat(ts)
            failure_alerts = data.get("job_failure_alerts", {})
            if isinstance(failure_alerts, dict):
                for name, alert in failure_alerts.items():
                    if isinstance(alert, dict):
                        self._job_failure_alerts[name] = alert
            for cid, ts in data.get("last_active", {}).items():
                self._last_active[cid] = datetime.fromisoformat(ts)
            log.info(
                "Loaded state: %d providers, %d sessions",
                len(self.active_provider_by_chat),
                len(self.session_by_chat_provider),
            )
            self._prune_stale_sessions()
            if state_changed:
                self.save_state()
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
        provider = self.active_provider_by_chat.get(chat_id)
        return provider if provider in PROVIDER_NAMES else "claude"

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

        Returns ``None`` when the user hasn't picked a level. A stored level
        is clamped to whatever the model actually accepts so callers always
        see the real value in use.
        """
        stored = self.effort_by_chat_provider_model.get((chat_id, provider, model))
        if stored is None:
            return None
        return provider_class(provider).clamp_effort(stored, model)

    def get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        """Get or create a per-chat lock to serialize requests."""
        lock = self.chat_lock_by_chat.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self.chat_lock_by_chat[chat_id] = lock
        return lock

    # -- Provider management --

    def make_provider(self, provider_name: str) -> BaseProvider:
        """Create a fresh provider instance using the configured CLI path."""
        provider_cfg = self.config.get("providers", {}).get(provider_name, {})
        path = provider_cfg.get("path", provider_name)
        return provider_class(provider_name)(path)

    # -- Session management --

    # Providers that support pre-assigned session IDs. For these,
    # Enso generates the ID upfront so it persists across restarts.
    # Codex and Agy generate their own IDs, which we capture after spawning.
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
        if self._update_in_progress:
            await ctx.reply("Enso is updating. Please try again after it restarts.")
            return
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
            model = self.get_active_model(chat_id, provider_name)
            effort = self.get_active_effort(chat_id, provider_name, model)
            provider = self.make_provider(provider_name)

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

        try:
            process = await self._spawn_process(*cmd, **kwargs)
        except BaseException:
            provider.finalize_events()
            raise
        log.info("[%s] pid=%s", provider.name, process.pid)
        self.running_process_by_chat[chat_id] = process

        event_count = 0
        finalized = False

        def remember_session(event: StreamEvent) -> None:
            if event.kind == "session" and event.session_id:
                self.session_by_chat_provider[(chat_id, provider.name)] = event.session_id
                self.save_state()

        try:
            assert process.stdout is not None
            stderr_data: bytes | None = None
            if provider.streaming_output:
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
                        remember_session(stream_event)
                        yield stream_event
                await process.wait()
            else:
                stdout_data, stderr_data = await process.communicate()
                parsed_events = provider.parse_complete_output(
                    stdout_data.decode(errors="replace")
                )
                event_count += len(parsed_events)
                for stream_event in parsed_events:
                    remember_session(stream_event)
                    yield stream_event

            for stream_event in provider.finalize_events():
                event_count += 1
                remember_session(stream_event)
                yield stream_event
            finalized = True

            rc = process.returncode
            if rc and not provider.stderr_to_stdout() and process.stderr:
                if stderr_data is None:
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
            if not finalized:
                for stream_event in provider.finalize_events():
                    remember_session(stream_event)
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
        bg = messages.consume(chat_id)
        if bg:
            prompt = f"{messages.format_for_injection(bg)}\n\n{prompt}"
            log.info("[%s] Injected %d background message(s) into prompt", provider_name, len(bg))

        # Inject compact seed if one is pending for this chat.
        prompt = self._consume_compact_seed(chat_id, prompt, provider_name)

        model = self.get_active_model(chat_id, provider_name)
        effort = self.get_active_effort(chat_id, provider_name, model)
        provider = self.make_provider(provider_name)

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
            status_msg = await ctx.reply_status(progress_text(0))
        except Exception:
            log.warning("Failed to send initial status message for chat %s", chat_id, exc_info=True)
        state = {
            "elapsed": 0,
        }
        stop = asyncio.Event()
        ticker = asyncio.create_task(self._run_ticker(ctx, status_msg, state, stop))

        async def stop_ticker() -> None:
            # Must run before the status message is edited or deleted; a
            # late tick would overwrite the final status.
            stop.set()
            error = await _cancel_and_wait(ticker)
            if error is not None and not isinstance(error, asyncio.CancelledError):
                log.warning(
                    "Status ticker failed while stopping for chat %s: %s",
                    chat_id, error,
                )

        msg_limit = self.transport.message_limit if self.transport else 4096
        response_parts: list[str] = []
        error_text = ""

        async def consume_provider_events() -> None:
            nonlocal error_text
            async for event in self.run_provider(
                provider, prompt, chat_id, model,
                effort=effort, extra_env=origin_env,
            ):
                if self.debug_events:
                    log.debug(
                        "[%s] handling_event kind=%s text_len=%d session=%s",
                        provider_name,
                        event.kind,
                        len(event.text or ""),
                        event.session_id or "-",
                    )
                if event.kind == "response":
                    response_parts.append(event.text)
                elif event.kind == "error":
                    error_text = event.text

        async def run_with_timeout() -> bool:
            """Run the provider and return True only when our deadline expires."""
            if not self.agent_timeout:
                await consume_provider_events()
                return False

            provider_task = asyncio.create_task(consume_provider_events())

            async def cancel_provider() -> None:
                error = await _cancel_and_wait(provider_task)
                if error is not None and not isinstance(error, asyncio.CancelledError):
                    log.warning(
                        "Provider cleanup failed for chat %s: %s",
                        chat_id, error,
                    )

            try:
                done, _ = await asyncio.wait(
                    {provider_task}, timeout=self.agent_timeout,
                )
                if provider_task in done:
                    await provider_task
                    return False
                await cancel_provider()
                return True
            except BaseException:
                if not provider_task.done():
                    await cancel_provider()
                raise

        try:
            if await run_with_timeout():
                await stop_ticker()
                duration = self._format_duration(self.agent_timeout)
                user_notice = f"Stopped after reaching the {duration} timeout."
                agent_notice = (
                    "Enso stopped the previous agent turn after it reached the "
                    f"configured {duration} timeout. Partial work may remain; inspect "
                    "the current workspace and prior session before continuing."
                )
                log.warning(
                    "[%s] request timed out chat=%s after %ss",
                    provider_name, chat_id, self.agent_timeout,
                )
                try:
                    messages.send(
                        agent_notice,
                        source="enso:timeout",
                        conversation_id=chat_id,
                    )
                except Exception:
                    log.exception("Could not persist timeout notice for chat %s", chat_id)

                delivered = False
                if status_msg is not None:
                    try:
                        await ctx.edit_status(status_msg, user_notice)
                        delivered = True
                    except Exception:
                        log.warning(
                            "Could not finalize timeout status for chat %s",
                            chat_id,
                            exc_info=True,
                        )
                if not delivered:
                    try:
                        await ctx.reply(user_notice)
                    except Exception:
                        log.warning(
                            "Could not send timeout notice for chat %s",
                            chat_id,
                            exc_info=True,
                        )
                return

            await stop_ticker()
            if status_msg is not None:
                await ctx.delete_status(status_msg)

            response_text = provider.format_response(response_parts)
            log.info(
                "[%s] request complete chat=%s response_parts=%d "
                "response_len=%d error=%s elapsed=%s",
                provider_name,
                chat_id,
                len(response_parts),
                len(response_text),
                bool(error_text),
                state["elapsed"],
            )

            if response_text:
                for chunk in split_text(response_text, limit=msg_limit):
                    await ctx.reply(chunk)
            elif error_text:
                await ctx.reply(f"Error: {error_text[:4000]}")
            else:
                await ctx.reply("(No response)")

        except asyncio.CancelledError:
            await stop_ticker()
            if status_msg is not None:
                with contextlib.suppress(Exception):
                    await ctx.edit_status(status_msg, "Stopped.")
            raise

        except Exception as exc:
            await stop_ticker()
            log.error("Error processing %s request: %s", provider_name, exc, exc_info=True)
            if status_msg is not None:
                with contextlib.suppress(Exception):
                    await ctx.delete_status(status_msg)
            for chunk in split_text(f"Error: {exc}", limit=msg_limit):
                await ctx.reply(chunk)

    @staticmethod
    def _format_duration(seconds: int | float) -> str:
        """Format a configured timeout as a concise compound modifier."""
        if seconds >= 60 and seconds % 60 == 0:
            minutes = int(seconds // 60)
            return f"{minutes}-minute"
        rendered = f"{seconds:g}"
        return f"{rendered}-second"

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
            if status_updates_enabled:
                text = progress_text(state["elapsed"])
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
        """Check for jobs to run every 60 seconds.

        Runs as a background task inside ``enso serve``. Each tick fires any
        due cron jobs.
        """
        log.info("Job scheduler started")
        while True:
            await asyncio.sleep(60)
            now = datetime.now()

            if self._update_in_progress:
                log.info("Job scheduler paused while Enso updates")
                continue

            for job in load_jobs():
                if not job.enabled:
                    continue
                if job.dir_name in self._running_job_tasks:
                    continue
                if self._should_run_job(job, now):
                    log.info(
                        "[job:%s] scheduler dispatch (schedule=%r)",
                        job.dir_name, job.schedule,
                    )
                    self._job_last_run[job.dir_name] = now
                    self.save_state()
                    task = asyncio.create_task(self._run_job_task(job))
                    self._running_job_tasks[job.dir_name] = task
                    task.add_done_callback(
                        lambda _task, name=job.dir_name: self._running_job_tasks.pop(
                            name, None,
                        )
                    )

    def _runs_cfg(self) -> dict:
        """Return the ``runs`` config block (defensive against bad shapes)."""
        cfg = self.config.get("runs")
        return cfg if isinstance(cfg, dict) else {}

    async def _run_job_task(self, job: Job) -> None:
        """Run one scheduled job without blocking the scheduler loop."""
        async with self._job_semaphore:
            try:
                await self._execute_job(job)
            except Exception:
                log.exception("Job '%s' failed", job.name)

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

    def _sensitive_values(self) -> set[str]:
        """Return configured/environment secret values that diagnostics must redact."""
        values: set[str] = set()

        def visit(value: Any, key: str = "") -> None:
            if isinstance(value, dict):
                for child_key, child_value in value.items():
                    visit(child_value, str(child_key))
            elif (
                _SENSITIVE_KEY_RE.search(key)
                and isinstance(value, (str, int))
                and len(str(value)) >= 4
            ):
                values.add(str(value))

        visit(self.config)
        for key, value in os.environ.items():
            if _SENSITIVE_KEY_RE.search(key) and len(value) >= 4:
                values.add(value)
        return values

    def _sanitize_job_diagnostic(self, text: str) -> str:
        """Redact and bound a one-line diagnostic intended for notifications."""
        cleaned = _ANSI_ESCAPE_RE.sub("", text)
        for secret in sorted(self._sensitive_values(), key=len, reverse=True):
            cleaned = cleaned.replace(secret, "<redacted>")
        cleaned = _BEARER_RE.sub("Bearer <redacted>", cleaned)
        cleaned = _SECRET_ASSIGNMENT_RE.sub(r"\1\2<redacted>", cleaned)
        cleaned = _URL_CREDENTIAL_RE.sub(r"\1<redacted>@", cleaned)
        cleaned = "".join(ch if ch.isprintable() or ch.isspace() else " " for ch in cleaned)
        cleaned = " ".join(cleaned.split())
        if len(cleaned) > PRERUN_DIAGNOSTIC_LIMIT:
            cleaned = cleaned[: PRERUN_DIAGNOSTIC_LIMIT - 1].rstrip() + "…"
        return cleaned

    def _safe_prerun_diagnostic(self, stderr: bytes, fallback: str) -> str:
        """Extract only an explicitly safe ``ENSO_ERROR:`` stderr summary.

        Arbitrary stderr can contain source records or credentials, so it is never
        copied into a notification or run record. Preruns may opt into a useful
        one-line summary by prefixing it with ``ENSO_ERROR:``.
        """
        decoded = stderr.decode(errors="replace")
        for line in decoded.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("ENSO_ERROR:"):
                detail = stripped.removeprefix("ENSO_ERROR:").strip()
                return self._sanitize_job_diagnostic(detail) or fallback
        return fallback

    async def _run_job_prerun(self, job: Job, tag: str) -> PrerunResult:
        """Run a job's deterministic gate and classify its documented outcomes."""
        if not job.prerun:
            return PrerunResult("open")

        script = os.path.join(job.job_dir, job.prerun)
        if not os.path.isfile(script):
            diagnostic = f"Configured prerun script not found: {job.prerun}"
            log.warning("%s prerun error: %s", tag, diagnostic)
            return PrerunResult("error", diagnostic=diagnostic)

        log.info(
            "%s prerun start script=%s timeout=%ss",
            tag, job.prerun, job.prerun_timeout,
        )
        try:
            proc = await self._spawn_process(
                "bash", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=job.job_dir,
            )
            stdout, stderr, timed_out = await self._communicate_with_timeout(
                proc, f"Job '{job.name}' prerun", job.prerun_timeout,
            )
        except Exception as exc:
            detail = self._sanitize_job_diagnostic(f"{type(exc).__name__}: {exc}")
            diagnostic = f"Could not start prerun{f': {detail}' if detail else ''}"
            log.warning("%s prerun error: %s", tag, diagnostic, exc_info=True)
            return PrerunResult("error", diagnostic=diagnostic)

        if timed_out:
            diagnostic = f"Prerun timed out after {job.prerun_timeout}s"
            log.warning("%s %s", tag, diagnostic.lower())
            return PrerunResult("timeout", diagnostic=diagnostic)

        exit_code = proc.returncode if proc.returncode is not None else -1
        if exit_code == 0:
            output = stdout.decode(errors="replace").strip()
            log.info("%s prerun gate open (exit 0) output_len=%d", tag, len(output))
            return PrerunResult("open", output=output, exit_code=0)
        if exit_code == 1:
            log.info("%s prerun gate closed (exit 1) — no work, skipping", tag)
            return PrerunResult("no_work", exit_code=1)

        fallback = f"Prerun exited with status {exit_code}"
        diagnostic = self._safe_prerun_diagnostic(stderr, fallback)
        log.warning("%s prerun error (exit %s): %s", tag, exit_code, diagnostic)
        return PrerunResult("error", diagnostic=diagnostic, exit_code=exit_code)

    def _create_job_run(
        self,
        job: Job,
        trigger: str,
        tag: str,
        started_at: str,
    ) -> str | None:
        """Create a run-history row without allowing telemetry to abort the job."""
        try:
            return runs.create(
                kind="job",
                name=job.dir_name,
                title=job.name,
                trigger=trigger,
                provider=job.provider,
                model=job.model,
                started_at=started_at,
            )
        except Exception:
            log.warning("%s could not create run record", tag, exc_info=True)
            return None

    async def _send_job_notification(self, job: Job, text: str, tag: str) -> bool:
        """Send a job notification without allowing transport faults to abort work."""
        if self.transport is None:
            return False
        try:
            await self.transport.notify(text[:4096], destination=job.notify)
        except Exception:
            log.warning("%s could not send job notification", tag, exc_info=True)
            return False
        return True

    def _failure_alert_fingerprint(
        self,
        job: Job,
        status: str,
        diagnostic: str,
        exit_code: int | None,
    ) -> str:
        transport = self.transport.name if self.transport is not None else ""
        material = "\0".join(
            (
                job.dir_name,
                transport,
                job.notify or "",
                status,
                str(exit_code),
                diagnostic,
            )
        )
        return hashlib.sha256(material.encode()).hexdigest()

    async def _notify_prerun_failure(
        self,
        job: Job,
        status: Literal["prerun_error", "prerun_timeout"],
        diagnostic: str,
        exit_code: int | None,
        tag: str,
    ) -> bool:
        """Notify once per failure fingerprint and re-notify after the cooldown."""
        if self.transport is None:
            return False

        now = datetime.now(timezone.utc)
        fingerprint = self._failure_alert_fingerprint(
            job, status, diagnostic, exit_code,
        )
        previous = self._job_failure_alerts.get(job.dir_name, {})
        last_notified: datetime | None = None
        try:
            if previous.get("last_notified_at"):
                last_notified = datetime.fromisoformat(previous["last_notified_at"])
                if last_notified.tzinfo is None:
                    last_notified = last_notified.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            last_notified = None

        same_failure = previous.get("fingerprint") == fingerprint
        within_cooldown = bool(
            last_notified
            and (now - last_notified).total_seconds() < JOB_FAILURE_RENOTIFY_SECS
        )
        if same_failure and within_cooldown:
            previous["last_seen_at"] = now.isoformat()
            try:
                suppressed = int(previous.get("suppressed", 0)) + 1
            except (TypeError, ValueError):
                suppressed = 1
            previous["suppressed"] = suppressed
            self._job_failure_alerts[job.dir_name] = previous
            self.save_state()
            log.info(
                "%s duplicate prerun alert suppressed count=%s",
                tag, previous["suppressed"],
            )
            return False

        headline = "prerun timed out" if status == "prerun_timeout" else "prerun failed"
        sent = await self._send_job_notification(
            job,
            f"⚠️ [{job.name}] {headline}\n{diagnostic}",
            tag,
        )
        if not sent:
            return False

        self._job_failure_alerts[job.dir_name] = {
            "fingerprint": fingerprint,
            "status": status,
            "destination": job.notify or "",
            "last_notified_at": now.isoformat(),
            "last_seen_at": now.isoformat(),
            "suppressed": 0,
        }
        self.save_state()
        return True

    async def _notify_prerun_recovery(self, job: Job, tag: str) -> bool:
        """Send one recovery after a previously reported prerun failure."""
        if job.dir_name not in self._job_failure_alerts:
            return False
        sent = await self._send_job_notification(
            job,
            f"✅ [{job.name}] prerun recovered",
            tag,
        )
        self._job_failure_alerts.pop(job.dir_name, None)
        self.save_state()
        return sent

    async def _execute_job(
        self,
        job: Job,
        *,
        trigger: Literal["schedule", "manual"] = "schedule",
        notify_failures: bool = True,
    ) -> JobRunResult:
        """Run the shared prerun/provider pipeline and return a terminal result."""
        tag = f"[job:{job.dir_name}]"
        started = datetime.now()
        started_at = datetime.now(timezone.utc).isoformat()
        log.info(
            "%s start name=%r provider=%s model=%s timeout=%ss prerun=%s",
            tag, job.name, job.provider, job.model, job.timeout,
            job.prerun or "-",
        )
        try:
            config_error = job_config_error(job.provider, job.model, self.models)
            if config_error:
                run_id = self._create_job_run(job, trigger, tag, started_at)
                output = f"Invalid job config: {config_error}"
                log.warning("%s %s", tag, output)
                self._record_run_finish(run_id, output, -1, "error", tag)
                if notify_failures:
                    await self._send_job_notification(
                        job, f"⚠️ [{job.name}] {output}", tag,
                    )
                return JobRunResult(
                    "error", run_id=run_id, output=output, exit_code=-1,
                )

            prerun = await self._run_job_prerun(job, tag)
            if prerun.outcome == "no_work":
                if notify_failures:
                    await self._notify_prerun_recovery(job, tag)
                return JobRunResult("no_work", exit_code=1)

            if prerun.outcome in {"error", "timeout"}:
                status: Literal["prerun_error", "prerun_timeout"] = (
                    "prerun_timeout" if prerun.outcome == "timeout" else "prerun_error"
                )
                run_id = self._create_job_run(job, trigger, tag, started_at)
                output = f"{status.replace('_', ' ').title()}: {prerun.diagnostic}"
                self._record_run_finish(
                    run_id, output, prerun.exit_code, status, tag,
                )
                if notify_failures:
                    await self._notify_prerun_failure(
                        job, status, prerun.diagnostic, prerun.exit_code, tag,
                    )
                return JobRunResult(
                    status,
                    run_id=run_id,
                    output=output,
                    exit_code=prerun.exit_code,
                )

            if notify_failures:
                await self._notify_prerun_recovery(job, tag)

            prompt = job.prompt.replace("{{prerun_output}}", prerun.output)

            run_id = self._create_job_run(job, trigger, tag, started_at)
            proc: Process | None = None
            try:
                provider = self.make_provider(job.provider)
                cmd = provider.build_batch_command(prompt, job.model)
                log.info(
                    "%s spawning provider_class=%s cwd=%s prompt_len=%d",
                    tag, provider.__class__.__name__,
                    self.working_dir, len(prompt),
                )
                log.debug("%s command=%s", tag, _redacted_command(cmd))
                proc = await self._spawn_process(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=self.working_dir,
                )
                log.info("%s pid=%s", tag, proc.pid)
                stdout, _, timed_out = await self._communicate_with_timeout(
                    proc, f"Job '{job.name}'", job.timeout,
                )
                elapsed = (datetime.now() - started).total_seconds()
                if timed_out:
                    output = (
                        f"Job timed out after {job.timeout}s; "
                        "process tree was terminated"
                    )
                    log.warning(
                        "%s timeout after %ss (elapsed=%.1fs); process tree terminated",
                        tag, job.timeout, elapsed,
                    )
                    self._record_run_finish(
                        run_id, output, proc.returncode, "timeout", tag,
                    )
                    if notify_failures:
                        await self._send_job_notification(
                            job, f"⚠️ [{job.name}] {output}", tag,
                        )
                    return JobRunResult(
                        "timeout",
                        run_id=run_id,
                        output=output,
                        exit_code=proc.returncode,
                    )
                output = provider.parse_batch_output(stdout.decode(errors="replace"))
            except Exception as exc:
                detail = self._sanitize_job_diagnostic(f"{type(exc).__name__}: {exc}")
                output = f"Job could not start or complete{f': {detail}' if detail else ''}"
                exit_code = proc.returncode if proc and proc.returncode is not None else -1
                log.warning("%s %s", tag, output, exc_info=True)
                self._record_run_finish(run_id, output, exit_code, "error", tag)
                if notify_failures:
                    await self._send_job_notification(
                        job, f"⚠️ [{job.name}] {output}", tag,
                    )
                    messages.send(output, source=f"job:{job.dir_name}")
                return JobRunResult(
                    "error", run_id=run_id, output=output, exit_code=exit_code,
                )

            exit_code = proc.returncode if proc.returncode is not None else -1
            status = "ok" if exit_code == 0 else "error"
            self._record_run_finish(run_id, output, exit_code, status, tag)
            notified = False
            if exit_code != 0 and notify_failures:
                log.warning(
                    "%s nonzero exit=%s output_len=%d notify=%s",
                    tag, exit_code, len(output), job.notify or "default",
                )
                label = f"{job.name} (exit {exit_code})"
                notified = await self._send_job_notification(
                    job, f"⚠️ [{label}]\n{output}", tag,
                )
                messages.send(output, source=f"job:{job.dir_name}")

            elapsed = (datetime.now() - started).total_seconds()
            log.info(
                "%s complete exit=%s duration=%.1fs output_len=%d notified=%s",
                tag, exit_code, elapsed, len(output), notified,
            )
            return JobRunResult(
                status, run_id=run_id, output=output, exit_code=exit_code,
            )
        finally:
            if trigger == "schedule":
                self._job_last_run[job.dir_name] = datetime.now()
                self.save_state()

    # -- Run history instrumentation --

    def _record_run_finish(
        self,
        run_id: str | None,
        output: str,
        exit_code: int | None,
        status: str,
        tag: str,
    ) -> None:
        """Write captured output and mark a run terminal (best-effort).

        Never raises: run instrumentation must not break the job it is
        observing. ``exit_code`` of ``None`` (e.g. a killed process) is stored
        as ``-1`` so the column is always populated.
        """
        if run_id is None:
            return
        try:
            if output:
                runs.append_output(run_id, output)
            runs.finish(run_id, exit_code if exit_code is not None else -1, status)
        except Exception:
            log.warning("%s could not finish run record id=%s", tag, run_id, exc_info=True)
            return

        runs_cfg = self._runs_cfg()
        try:
            keep = max(0, int(runs_cfg.get("keep", 500)))
            max_age_days = max(0, int(runs_cfg.get("max_age_days", 30)))
        except (TypeError, ValueError):
            log.warning("%s invalid run-retention config; using defaults", tag)
            keep, max_age_days = 500, 30
        try:
            runs.prune(keep=keep, max_age_days=max_age_days)
        except Exception:
            log.warning("%s could not prune run history", tag, exc_info=True)

    # -- Run-now (manual triggers from CLI / web) --

    async def run_job_now(self, name: str) -> JobRunResult:
        """Run a job's full pipeline manually without sending notifications."""
        job = next((j for j in load_jobs() if j.dir_name == name), None)
        if job is None:
            raise ValueError(f"No such job: {name}")
        return await self._execute_job(
            job, trigger="manual", notify_failures=False,
        )
