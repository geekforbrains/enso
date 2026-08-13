"""Enso runtime — the engine that makes agents go."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.resources
import json
import logging
import os
import re
import shlex
import signal
from asyncio.subprocess import Process
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar

from . import messages
from .config import (
    CONFIG_DIR,
    DEFAULT_AGENT,
    SKILL_TOMBSTONES_DIRNAME,
    STATE_FILE,
    provider_models,
)
from .fsutil import atomic_write_text, regular_file_sha256
from .logging_config import logging_flags
from .outbound import (
    parse_outbound_fallback,
    parse_outbound_message,
    parse_surface_fallback,
    parse_surface_publication,
)
from .providers import PROVIDER_NAMES, BaseProvider, StreamEvent, provider_class
from .teams import load_catalog

if TYPE_CHECKING:
    from .job_runner import JobRunner
    from .policy import Launch
    from .teams import AccessProfile, Workspace
    from .transports import BaseTransport, TransportContext

log = logging.getLogger(__name__)


_LEADING_ERROR_RE = re.compile(r"^(?:error\s*:\s*)+", re.IGNORECASE)


def _format_error(text: str) -> str:
    """Return an error message with exactly one leading ``Error:`` label."""
    body = _LEADING_ERROR_RE.sub("", text.strip()).strip()
    return f"Error: {body}" if body else "Error:"


# Keep the clock visibly alive while most requests are still young, then
# reduce the edit rate to stay comfortably inside transport limits.
STATUS_FAST_UPDATE_SECONDS = 30
STATUS_SLOW_UPDATE_SECONDS = 5
# Consecutive edit failures tolerated before status updates are abandoned for
# the rest of the request. One blip (a transient 429) shouldn't cost the user
# every later update.
STATUS_MAX_EDIT_FAILURES = 3
# Shown as the action line until the provider reports its first real status,
# so the message keeps the same shape from the moment it is posted.
STATUS_INITIAL_ACTION = "Processing"


def format_elapsed(seconds: int) -> str:
    """Render an elapsed duration compactly: 45s, 2m 05s, 1h 12m."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def status_header(provider: str, model: str, effort: str | None = None) -> str:
    """Return the fixed context line identifying what is handling a request."""
    parts = [provider, model]
    if effort:
        parts.append(effort)
    return " · ".join(parts)


def status_text(header: str, elapsed: int, action: str | None = None) -> str:
    """Render the status message: context and clock, plus the current action."""
    line = f"{header} · {format_elapsed(elapsed)}"
    return f"{line}\n↳ {action}" if action else line


def _status_edit_due(elapsed: int) -> bool:
    """Update every second through 30s, then on five-second boundaries."""
    return elapsed <= STATUS_FAST_UPDATE_SECONDS or elapsed % STATUS_SLOW_UPDATE_SECONDS == 0


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
PROCESS_TERMINATE_GRACE_SECS = float(os.environ.get("ENSO_PROCESS_TERMINATE_GRACE_SECS", "5"))

# Upgrade markers for artifacts bundled immediately before the built-in task
# system was removed. Hashes let us recognize pristine installer-owned files
# without retaining obsolete task instructions in the package.
_LEGACY_TASKS_SKILL_SHA256 = "661ffca9a360cc40521c274a295a97c7735123a7c8a44e1d307da046f07735cc"
_LEGACY_TASKS_AGENTS_SHA256 = "ec67ee973a15c38e23451cfc65317643debe0e6e8659589bf0c30433f60a2e4a"
_LEGACY_TASK_RUNNER_STATE_KEY = "__task_runner__"

# Pristine bundled AGENTS.md hashes from prior releases. Exact matches follow
# the bundled template forward; customized copies are preserved.
_PRISTINE_AGENTS_SHA256: frozenset[str] = frozenset(
    {
        _LEGACY_TASKS_AGENTS_SHA256,
        "18e29e570f07237eea24a2b329090ccae9b572cdbc7e35f38e22916f3e5acf7f",
        "b5676b7f1b571a813554c0c580c93a6c9269d82625161f01f2c00e205888da20",
        "8f9bacc078f4b6bd826c59501f7e110de4e2caacc42fd3975ad3aeab624164f7",
    }
)

# Known hashes of pristine bundled skills from prior releases. Exact matches
# can follow the bundled copy forward without overwriting user-customized files.
_BUNDLED_SKILL_PRISTINE_HASHES: dict[tuple[str, str], frozenset[str]] = {
    ("jobs", "SKILL.md"): frozenset(
        {
            "f52f890e467bd212534474b1d0ee913edbf6cc968e010686153044aac13bcd77",
            "8824886bd76e476672395bfcef6d34655b7eeedb40c89cf0fc459706e9ad4cff",
            "cc8d7abc0e550b901644d7c7feee2e3363608adf794a2f885c7421a5cb7fa08b",
            "256ce5a5609551246927c9e19ef0be13f68f630fb343815f303e6f90ab8cb51c",
            "608c4a5d9f34d76ae9143f749fa7b028a4fce413d260e1a5f58d361288730bd8",
            "1756397ae5838a5aba08c6371cb721f9e1b4f815c8b1907a19b017e7aca53be0",
            "dabb0fa66f276cd78c8e88e17c38155ad537aa52938e50622dcc2955b70f036a",
            "ecde110e219de184ccf52594d02d9ae458022a4813dcc8ac417a975c5010f282",
            "1a97b59fb3361cac02af0d62d0cb472ad0315fe710ece162977a602211161e28",
        }
    ),
    ("slack", "SKILL.md"): frozenset(
        {
            "5d9f76e2bcb757b27ab294a6f7322e07a59ebafbe08566f218348f8d15ac178a",
            "646d0ab64c0713baf32eec4f0639b4d709d4b1c7a2a1a320e2eed84d55fc5582",
            "70f2dd312d001f23a49aeadb2f556df76e259b80f8591093eb7d36a6ea56b2bd",
        }
    ),
}

# The pre-0.12 bundled slack skill shipped a Python tool script that the
# `enso slack` CLI replaced. Pristine copies (and their installed
# workspace/tools twins) are removed on setup/serve; customized copies are
# preserved with a warning.
_RETIRED_SKILL_TOOL_HASHES: dict[tuple[str, str], frozenset[str]] = {
    ("slack", "slack_search.py"): frozenset(
        {
            "2a993392c4d58ac7e5ce653ade6070ed9db9baaa6b909ae2167d29de490ded7d",
        }
    ),
}


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
    provider: str
    context: ExecutionContext | None = None


@dataclass(frozen=True)
class ExecutionContext:
    """Immutable execution binding for one conversation's work.

    Telegram conversations bind the global ``working_dir`` with the unrestricted
    invocation and use the conversation ID as their state key. Slack routes and
    jobs bind a named workspace and access profile. The native launch is prepared
    only after the workspace slot is acquired, immediately before the provider
    process starts.
    """

    chat_key: str  # key for all per-chat state: sessions, queues, locks
    path: str  # subprocess cwd — the workspace root
    workspace_id: str | None = None
    launch: Launch | None = None  # None → unrestricted global invocation
    concurrency: int = 1  # max concurrent provider runs sharing the workspace
    workspace: Workspace | None = field(default=None, compare=False, repr=False)
    access: AccessProfile | None = field(default=None, compare=False, repr=False)
    model: str | None = None
    effort: str | None = None
    on_launch: Callable[[Launch], None] | None = field(default=None, compare=False, repr=False)
    # Invoked (in a worker thread) with the turn's terminal outcome once it
    # reaches a terminal state — a real dispatch, a revalidation refusal, or
    # an early rejection (queue full, update in progress) — so audit/ledger
    # bookkeeping records the true outcome and never leaks a pending row.
    # Must be idempotent. Args: (outcome, terminal_reason).
    on_complete: Callable[[str, str | None], None] | None = field(
        default=None, compare=False, repr=False
    )


class Runtime:
    """Central runtime holding all state and process management.

    Job scheduling and execution live on ``self.jobs`` (see
    :class:`~enso.job_runner.JobRunner`).
    """

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
        # Strong references to fire-and-forget queue drains (kick_queue)
        self._kicked_queue_tasks: set[asyncio.Task] = set()

        # Process-local concurrency shared by Slack chats, compaction, and jobs
        # bound to the same workspace. Separate Enso processes do not share it.
        self._workspace_sems: dict[str, asyncio.Semaphore] = {}

        # Compact-command seeds: per-chat prior-session summaries waiting to
        # be prepended to the next user prompt. Set by /compact; consumed in
        # process_request the next time that chat dispatches a message.
        self.compact_seed_by_chat: dict[str, str] = {}

        # Activity tracking for session pruning
        self._last_active: dict[str, datetime] = {}

        # Set while the self-updater validates and installs a release. New
        # agent turns and scheduler ticks pause until the operation finishes.
        self.update_in_progress = False

        # Job scheduling and execution. Imported here because job_runner
        # imports this module for the runtime pieces the pipeline reuses.
        from .job_runner import JobRunner

        self.jobs: JobRunner = JobRunner(self)

    # -- Workspace setup --

    def install_system_prompts(self) -> None:
        """Set up working directory, system prompts, skills, hooks, and config dirs.

        Creates:
        - ~/.enso/docs/, ~/.enso/jobs/, and ~/.enso/skills/
        - Bundled skills seeded into ~/.enso/skills/
        - AGENTS.md in working_dir (from bundled template on first install)
        - CLAUDE.md as a symlink to AGENTS.md (Claude reads CLAUDE.md;
          Codex reads AGENTS.md natively)
        - .claude/skills and .agents/skills symlinked to ~/.enso/skills/
          (so Claude and Codex auto-discover skills)
        - Auto-compact notification hooks for Claude
        """
        from .config import DOCS_DIR, JOBS_DIR

        skills_dir = os.path.join(CONFIG_DIR, "skills")
        for d in (DOCS_DIR, JOBS_DIR, skills_dir):
            os.makedirs(d, exist_ok=True)

        self._retire_legacy_tasks_skill(skills_dir)
        self._retire_legacy_skill_tools(skills_dir)
        self._install_bundled_skills(skills_dir)
        self._install_skill_tools(skills_dir)

        # System prompt. AGENTS.md is canonical; Claude reads CLAUDE.md, so
        # it's symlinked to AGENTS.md. Codex reads AGENTS.md natively, so no
        # further symlinks are needed.
        source = importlib.resources.files("enso").joinpath("prompts", "AGENTS.md")
        content = source.read_text(encoding="utf-8")

        canonical = os.path.join(self.working_dir, "AGENTS.md")
        is_pristine_template = regular_file_sha256(canonical) in _PRISTINE_AGENTS_SHA256
        if not os.path.lexists(canonical) or is_pristine_template:
            try:
                atomic_write_text(canonical, content)
                action = "Updated" if is_pristine_template else "Wrote"
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

        self._ensure_symlink(os.path.join(self.working_dir, "CLAUDE.md"), "AGENTS.md")

        # Symlink skills into CLI-specific discovery paths
        # .claude/skills -> ~/.enso/skills (Claude Code)
        # .agents/skills -> ~/.enso/skills (Codex)
        for cli_dir in (".claude", ".agents"):
            parent = os.path.join(self.working_dir, cli_dir)
            os.makedirs(parent, exist_ok=True)
            self._ensure_symlink(os.path.join(parent, "skills"), skills_dir)

        # Auto-compact notification hooks — lets the user know via the
        # configured chat transport when a provider is compacting context
        # (which can be slow).
        # Claude: PreCompact with "auto" matcher
        # Codex: no compaction hooks available
        notify_cmd = "enso message send 'Autocompacting context, this might take a moment...'"
        self._ensure_hook_entry(
            os.path.join(self.working_dir, ".claude", "settings.json"),
            event="PreCompact",
            matcher="auto",
            command=notify_cmd,
        )

    def install_workspaces(self) -> None:
        """Bootstrap every structurally valid configured workspace.

        Creates the workspace directory with the bundled system prompt.
        Instructions and skills are owned by the workspace and discovered by
        the provider CLIs themselves; jobs, config, and shared skill roots are
        never linked in.
        """
        catalog = load_catalog(self.config)
        for name, workspace in catalog.workspaces.items():
            if name in catalog.workspace_errors or not workspace.path:
                continue
            try:
                self._install_workspace(workspace)
            except OSError:
                log.warning("Could not bootstrap workspace %s", name, exc_info=True)

    def _install_workspace(self, workspace) -> None:
        os.makedirs(workspace.path, exist_ok=True)
        os.makedirs(os.path.join(workspace.path, "uploads"), exist_ok=True)

        canonical = os.path.join(workspace.path, "AGENTS.md")
        if not os.path.lexists(canonical):
            source = importlib.resources.files("enso").joinpath("prompts", "WORKSPACE_AGENTS.md")
            atomic_write_text(canonical, source.read_text(encoding="utf-8"))
            log.info("Wrote AGENTS.md in workspace %s", workspace.name)
        self._ensure_symlink(os.path.join(workspace.path, "CLAUDE.md"), "AGENTS.md")

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

        event_hooks.append(
            {
                "matcher": matcher,
                "hooks": [{"type": "command", "command": command, "async": True}],
            }
        )

        try:
            atomic_write_text(settings_path, json.dumps(settings, indent=2) + "\n")
            log.info("Installed %s hook in %s", event, settings_path)
        except OSError:
            log.warning("Could not write %s", settings_path, exc_info=True)

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
        return any(
            marker in content
            for marker in (
                "enso task create",
                "enso task list",
                "enso task show",
                "use the `tasks` skill",
            )
        )

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
        if regular_file_sha256(skill_file) != _LEGACY_TASKS_SKILL_SHA256:
            log.warning(warning, skill_dir)
            return
        try:
            os.remove(skill_file)
            os.rmdir(skill_dir)
            log.info("Removed retired bundled skill: tasks")
        except OSError:
            log.warning("Could not remove retired bundled tasks skill", exc_info=True)

    def _retire_legacy_skill_tools(self, skills_dir: str) -> None:
        """Remove retired pristine bundled tool scripts and their installed copies.

        Removing the skill copy alone is not enough: ``_install_skill_tools``
        reinstalls any ``.py`` it finds in a skill dir, so the stale
        ``workspace/tools/`` twin is removed first.
        """
        for (skill_name, filename), pristine in _RETIRED_SKILL_TOOL_HASHES.items():
            skill_copy = os.path.join(skills_dir, skill_name, filename)
            tool_copy = os.path.join(self.working_dir, "tools", filename)
            skill_hash = regular_file_sha256(skill_copy)
            if regular_file_sha256(tool_copy) in pristine:
                with contextlib.suppress(OSError):
                    os.remove(tool_copy)
                    log.info("Removed retired installed tool: %s", filename)
            if skill_hash in pristine:
                with contextlib.suppress(OSError):
                    os.remove(skill_copy)
                    log.info(
                        "Removed retired bundled skill tool: %s/%s",
                        skill_name,
                        filename,
                    )
            elif skill_hash is not None:
                log.warning(
                    "Preserving customized retired skill tool at %s; the "
                    "`enso slack` CLI replaced it — update or remove it manually",
                    skill_copy,
                )

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
            tombstone = os.path.join(tombstones_dir, f"{skill_dir.name}.deleted")
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
                    existing_hash = regular_file_sha256(dest_file)
                    known_pristine = _BUNDLED_SKILL_PRISTINE_HASHES.get(
                        (skill_dir.name, f.name), frozenset()
                    )
                    if existing_hash not in known_pristine:
                        continue
                    action = "Updated pristine"
                try:
                    content = f.read_text(encoding="utf-8")
                    atomic_write_text(dest_file, content)
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
            "version": 2,
            "active_provider_by_chat": {str(k): v for k, v in self.active_provider_by_chat.items()},
            # Conversation keys are opaque and may contain colons. Store
            # compound keys as records instead of inventing a delimiter.
            "active_model_by_chat_provider": [
                {"chat": cid, "provider": prov, "model": model}
                for (cid, prov), model in self.active_model_by_chat_provider.items()
            ],
            "effort_by_chat_provider_model": [
                {"chat": cid, "provider": prov, "model": model, "effort": eff}
                for (cid, prov, model), eff in self.effort_by_chat_provider_model.items()
            ],
            "session_by_chat_provider": [
                {"chat": cid, "provider": prov, "session": sid}
                for (cid, prov), sid in self.session_by_chat_provider.items()
            ],
            "compact_seed_by_chat": dict(self.compact_seed_by_chat),
            "job_last_run": {name: ts.isoformat() for name, ts in self.jobs.last_run.items()},
            "job_failure_alerts": dict(self.jobs.failure_alerts),
            "last_active": {cid: ts.isoformat() for cid, ts in self._last_active.items()},
        }
        try:
            atomic_write_text(STATE_FILE, json.dumps(data))
        except Exception:
            log.exception("Failed to save state")

    def load_state(self, *, persist: bool = True) -> None:
        """Load persisted state from disk.

        ``persist=False`` (used by secondary processes like the dashboard
        and the job-run CLI) skips writing pruned or migrated state back,
        so a stale snapshot can never clobber the serve process's live
        state.json.
        """
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
            raw_models = data.get("active_model_by_chat_provider", [])
            if isinstance(raw_models, dict):
                # v1 migration: provider was the final delimiter-separated
                # component; split from the right so ``teams:<digest>`` works.
                model_rows = (
                    {"chat": k.rsplit(":", 1)[0], "provider": k.rsplit(":", 1)[1], "model": v}
                    for k, v in raw_models.items()
                    if ":" in k
                )
                state_changed = True
            elif isinstance(raw_models, list):
                model_rows = iter(raw_models)
            else:
                model_rows = iter(())
                state_changed = True
            for row in model_rows:
                if not isinstance(row, dict):
                    state_changed = True
                    continue
                cid = row.get("chat")
                provider = row.get("provider")
                v = row.get("model")
                # Entries for retired providers or models removed from config
                # are inert (selection falls back anyway) — prune them.
                if (
                    isinstance(cid, str)
                    and isinstance(provider, str)
                    and v in self.models.get(provider, [])
                ):
                    self.active_model_by_chat_provider[(cid, provider)] = v
                else:
                    state_changed = True
            raw_efforts = data.get("effort_by_chat_provider_model", [])
            if isinstance(raw_efforts, dict):
                effort_rows = []
                for key, value in raw_efforts.items():
                    parts = key.rsplit(":", 2)
                    if len(parts) == 3:
                        effort_rows.append(
                            {
                                "chat": parts[0],
                                "provider": parts[1],
                                "model": parts[2],
                                "effort": value,
                            }
                        )
                state_changed = True
            elif isinstance(raw_efforts, list):
                effort_rows = raw_efforts
            else:
                effort_rows = []
                state_changed = True
            for row in effort_rows:
                if not isinstance(row, dict):
                    state_changed = True
                    continue
                cid = row.get("chat")
                provider = row.get("provider")
                model = row.get("model")
                effort = row.get("effort")
                if (
                    isinstance(cid, str)
                    and isinstance(provider, str)
                    and isinstance(model, str)
                    and isinstance(effort, str)
                    and model in self.models.get(provider, [])
                    and provider_class(provider).effort_levels
                ):
                    self.effort_by_chat_provider_model[(cid, provider, model)] = effort
                else:
                    state_changed = True
            raw_sessions = data.get("session_by_chat_provider", [])
            if isinstance(raw_sessions, dict):
                session_rows = (
                    {"chat": k.rsplit(":", 1)[0], "provider": k.rsplit(":", 1)[1], "session": v}
                    for k, v in raw_sessions.items()
                    if ":" in k
                )
                state_changed = True
            elif isinstance(raw_sessions, list):
                session_rows = iter(raw_sessions)
            else:
                session_rows = iter(())
                state_changed = True
            for row in session_rows:
                if not isinstance(row, dict):
                    state_changed = True
                    continue
                cid = row.get("chat")
                provider = row.get("provider")
                sid = row.get("session")
                if (
                    isinstance(cid, str)
                    and isinstance(provider, str)
                    and isinstance(sid, str)
                    and provider in PROVIDER_NAMES
                ):
                    self.session_by_chat_provider[(cid, provider)] = sid
                else:
                    state_changed = True
            for k, v in data.get("compact_seed_by_chat", {}).items():
                self.compact_seed_by_chat[k] = v
            for name, ts in data.get("job_last_run", {}).items():
                if name == _LEGACY_TASK_RUNNER_STATE_KEY:
                    state_changed = True
                    continue
                self.jobs.last_run[name] = datetime.fromisoformat(ts)
            failure_alerts = data.get("job_failure_alerts", {})
            if isinstance(failure_alerts, dict):
                for name, alert in failure_alerts.items():
                    if isinstance(alert, dict):
                        self.jobs.failure_alerts[name] = alert
            for cid, ts in data.get("last_active", {}).items():
                self._last_active[cid] = datetime.fromisoformat(ts)
            log.info(
                "Loaded state: %d providers, %d sessions",
                len(self.active_provider_by_chat),
                len(self.session_by_chat_provider),
            )
            self._prune_stale_sessions(persist=persist)
            if state_changed and persist:
                self.save_state()
        except Exception:
            log.exception("Failed to load state, starting fresh")

    def touch_session(self, chat_key: str) -> None:
        """Mark a state key active so stale-session pruning can reach it.

        The teams router writes a durable provider-selection key that is not a
        dispatch chat_key; without a last-active stamp it would never prune.
        """
        self._last_active[chat_key] = datetime.now()

    def _prune_stale_sessions(self, *, persist: bool = True) -> None:
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
        if persist:
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
        self,
        chat_id: str,
        provider: str,
        model: str,
    ) -> str | None:
        """Return the effective effort level for chat+provider+model.

        Returns ``None`` when the user hasn't picked a level. A stored level
        is clamped to whatever the model actually accepts so callers always
        see the real value in use.
        """
        stored = self.effort_by_chat_provider_model.get((chat_id, provider, model))
        provider_cls = provider_class(provider)
        if stored is None or not provider_cls.effort_levels:
            return None
        return provider_cls.clamp_effort(stored, model)

    def get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        """Get or create a per-chat lock to serialize requests."""
        lock = self.chat_lock_by_chat.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self.chat_lock_by_chat[chat_id] = lock
        return lock

    # -- Provider management --

    def global_context(self, conv_id: str) -> ExecutionContext:
        """The Telegram execution binding: global working_dir, unrestricted."""
        return ExecutionContext(chat_key=conv_id, path=self.working_dir)

    @contextlib.asynccontextmanager
    async def _workspace_slot(self, context: ExecutionContext):
        """Hold the workspace's concurrency slot for the duration of a run.

        A no-op for global (non-workspace) contexts, which stay unbounded.
        The semaphore is shared across Slack chats, compaction, and jobs bound
        to the same workspace; the default limit is one active writer.
        """
        workspace_id = context.workspace_id
        if workspace_id is None:
            yield
            return
        sem = self._workspace_sems.get(workspace_id)
        if sem is None:
            sem = asyncio.Semaphore(max(1, context.concurrency))
            self._workspace_sems[workspace_id] = sem
        async with sem:
            yield

    async def _prepare_execution_context(
        self, provider: str, context: ExecutionContext
    ) -> ExecutionContext:
        """Resolve a teams profile into a native launch at the spawn boundary."""
        if context.launch is not None or context.access is None:
            return context
        if context.workspace is None:
            raise RuntimeError("access profile is missing its workspace binding")
        from .policy import prepare_launch

        launch = await asyncio.to_thread(
            prepare_launch, context.workspace, context.access, provider
        )
        if context.on_launch is not None:
            await asyncio.to_thread(context.on_launch, launch)
        return replace(context, launch=launch)

    def make_provider(
        self,
        provider_name: str,
        *,
        timeout: int | float | None = None,
        context: ExecutionContext | None = None,
    ) -> BaseProvider:
        """Create a fresh provider instance using the configured CLI path."""
        provider_cfg = self.config.get("providers", {}).get(provider_name, {})
        path = provider_cfg.get("path", provider_name)
        effective_timeout = self.agent_timeout if timeout is None else timeout
        working_dir = context.path if context is not None else self.working_dir
        return provider_class(provider_name)(
            path,
            working_dir=working_dir,
            timeout=effective_timeout,
        )

    # -- Session management --

    # Providers that support pre-assigned session IDs. For these,
    # Enso generates the ID upfront so it persists across restarts.
    # Codex and Agy generate their own IDs, which we capture after spawning.
    _SELF_MANAGED_SESSIONS: ClassVar[set[str]] = {"claude"}

    def _get_or_create_session(self, chat_id: str, provider_name: str) -> str | None:
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
                provider_name,
                chat_id,
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
        context: ExecutionContext | None = None,
    ) -> None:
        """Dispatch a prompt, queuing if a request is already running.

        ``context`` is the resolved execution binding; None binds the global
        context (global working_dir, conversation-keyed state).
        """
        context = context or self.global_context(conversation_id)
        if self.update_in_progress:
            await ctx.reply("Enso is updating. Please try again after it restarts.")
            await self._finalize_unrun(context, "update_in_progress")
            return
        chat_key = context.chat_key
        self._last_active[chat_key] = datetime.now()
        lock = self.get_chat_lock(chat_key)
        provider = self.get_active_provider(chat_key)

        if lock.locked():
            queue = self._queue_by_conversation.setdefault(chat_key, deque())
            if len(queue) >= MAX_QUEUE_SIZE:
                await ctx.reply(f"Queue full ({MAX_QUEUE_SIZE}).")
                await self._finalize_unrun(context, "queue_full")
                return
            queue.append(
                _QueuedItem(
                    prompt=prompt,
                    ctx=ctx,
                    preview=preview,
                    provider=provider,
                    context=context,
                )
            )
            pos = len(queue)
            label = f"{preview}\u2026" if len(preview) == 50 else preview
            await ctx.reply(f"Queued (#{pos}): {label}")
            # Teams-mode logs are metadata-only; the preview is content.
            logged = "<redacted>" if context.workspace_id is not None else preview
            log.info("Queued #%d for %s: %s", pos, conversation_id, logged)
            return

        log.info(
            "Dispatch: conv=%s provider=%s prompt_len=%d",
            conversation_id,
            provider,
            len(prompt),
        )
        if context.workspace_id is None:
            log.debug("Dispatch prompt:\n%s", prompt)

        async with lock:
            await self._run_request(provider, prompt, ctx, context)
            await self._drain_queue(chat_key)

    async def _finalize_unrun(
        self,
        context: ExecutionContext,
        reason: str,
    ) -> None:
        """Finalize a turn rejected before it ran (queue full, update).

        The audit turn and ledger claim were created at intake, so an
        early rejection must still mark them terminal or they leak as
        ``pending`` until a restart falsifies them.
        """
        if context.on_complete is None:
            return
        try:
            await asyncio.to_thread(context.on_complete, "blocked", reason)
        except Exception:
            log.exception("on_complete failed for conv=%s", context.chat_key)

    async def _run_request(
        self,
        provider: str,
        prompt: str,
        ctx: TransportContext,
        context: ExecutionContext,
    ) -> None:
        """Run a single provider request, tracking the task for cancellation."""
        outcome, reason = "error", None
        task = asyncio.create_task(self._run_request_inner(provider, prompt, ctx, context))
        self.running_task_by_chat[context.chat_key] = task
        try:
            outcome, reason = await task
        except asyncio.CancelledError:
            log.info("Task cancelled for conv=%s", context.chat_key)
            outcome, reason = "stopped", "cancelled"
        finally:
            if self.running_task_by_chat.get(context.chat_key) is task:
                self.running_task_by_chat.pop(context.chat_key, None)
            if context.on_complete is not None:
                try:
                    await asyncio.to_thread(context.on_complete, outcome, reason)
                except Exception:
                    log.exception("on_complete failed for conv=%s", context.chat_key)

    async def _run_request_inner(
        self,
        provider: str,
        prompt: str,
        ctx: TransportContext,
        context: ExecutionContext,
    ) -> tuple[str, str | None]:
        """Run one turn; return its (outcome, terminal_reason) for on_complete."""
        chat_key = context.chat_key
        async with self._workspace_slot(context):
            try:
                prepared = await self._prepare_execution_context(provider, context)
            except Exception:
                log.exception("Native policy launch failed for conv=%s", chat_key)
                with contextlib.suppress(Exception):
                    await ctx.reply(
                        "This conversation isn't fully configured for Enso — "
                        "ask an admin to run `enso config check`."
                    )
                return "blocked", "policy_unavailable"
            return await self.process_request(provider, prompt, chat_key, ctx, context=prepared)

    async def _drain_queue(self, chat_key: str) -> None:
        """Process queued messages one by one until the queue is empty."""
        queue = self._queue_by_conversation.get(chat_key)
        if not queue:
            return
        while queue:
            item = queue.popleft()
            log.info(
                "Dequeuing for conv=%s (%d remaining): %s",
                chat_key,
                len(queue),
                "<redacted>" if item.context and item.context.workspace_id else item.preview,
            )
            context = item.context or self.global_context(chat_key)
            await self._run_request(item.provider, item.prompt, item.ctx, context)

    def kick_queue(self, conv_id: str) -> None:
        """Schedule a queue drain outside dispatch (e.g. after /compact).

        Messages queued while a non-dispatch flow held the chat lock would
        otherwise sit until the next user message.
        """
        if not self._queue_by_conversation.get(conv_id):
            return
        task = asyncio.create_task(self._resume_queue(conv_id))
        self._kicked_queue_tasks.add(task)
        task.add_done_callback(self._kicked_queue_tasks.discard)

    async def _resume_queue(self, conv_id: str) -> None:
        lock = self.get_chat_lock(conv_id)
        if lock.locked():
            # An active dispatch will drain the queue itself.
            return
        try:
            async with lock:
                await self._drain_queue(conv_id)
        except Exception:
            log.exception("Queue drain failed for conv=%s", conv_id)

    def get_queue(self, conv_id: str) -> list[str]:
        """Return preview strings for queued items."""
        queue = self._queue_by_conversation.get(conv_id)
        if not queue:
            return []
        return [item.preview for item in queue]

    async def clear_queue(self, conv_id: str) -> int:
        """Clear the queue for a conversation. Returns count of items cleared."""
        queue = self._queue_by_conversation.get(conv_id)
        if not queue:
            return 0
        items = list(queue)
        queue.clear()
        for item in items:
            if item.context is not None:
                await self._finalize_unrun(item.context, "queue_cleared")
        return len(items)

    async def remove_from_queue(self, conv_id: str, index: int) -> bool:
        """Remove item at index from the queue. Returns True if removed."""
        queue = self._queue_by_conversation.get(conv_id)
        if queue and 0 <= index < len(queue):
            item = queue[index]
            del queue[index]
            if item.context is not None:
                await self._finalize_unrun(item.context, "queue_removed")
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
        self,
        chat_id: str,
        prompt: str,
        provider_name: str,
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
            provider_name,
            len(seed),
            chat_id,
        )
        return (
            "[Continuing from a previous session — that conversation was "
            "compacted to save tokens. Summary of prior context:\n\n"
            f"{seed}\n\n"
            "---\n"
            "End of prior-session summary. Below is the user's next message.]"
            f"\n\n{prompt}"
        )

    async def run_compaction(
        self,
        chat_id: str,
        provider_name: str,
        *,
        context: ExecutionContext | None = None,
    ) -> str:
        """Run a hidden summarisation pass and return the summary text.

        Called by /compact. Drives ``run_provider`` directly so the user
        sees nothing in chat — no status messages, no ticker, no replies.
        Holds the chat lock itself for the duration (bailing out early if a
        request is already running) and honors ``agent.timeout`` like
        interactive turns. Returns an empty string on error or timeout.
        """
        lock = self.get_chat_lock(chat_id)
        if lock.locked():
            log.warning("run_compaction skipped — chat %s is busy", chat_id)
            return ""

        slot = self._workspace_slot(context) if context is not None else contextlib.nullcontext()
        async with lock, slot:
            if context is not None:
                try:
                    context = await self._prepare_execution_context(provider_name, context)
                except Exception:
                    log.exception(
                        "Native policy launch failed during compaction for chat=%s",
                        chat_id,
                    )
                    return ""
            model = (
                context.model
                if context is not None and context.model is not None
                else self.get_active_model(chat_id, provider_name)
            )
            effort = (
                context.effort
                if context is not None and context.model is not None
                else self.get_active_effort(chat_id, provider_name, model)
            )
            provider = self.make_provider(
                provider_name, timeout=self.agent_timeout, context=context
            )

            log.info(
                "[%s] Compacting chat=%s model=%s effort=%s",
                provider_name,
                chat_id,
                model,
                effort or "-",
            )

            response_parts: list[str] = []
            failed = False

            async def consume() -> None:
                nonlocal failed
                async for event in self.run_provider(
                    provider,
                    self._COMPACTION_PROMPT,
                    chat_id,
                    model,
                    effort=effort,
                    context=context,
                ):
                    if event.kind == "response":
                        response_parts.append(event.text)
                    elif event.kind == "error":
                        log.warning(
                            "Compaction error in chat %s: %s",
                            chat_id,
                            event.text,
                        )
                        failed = True
                        return

            task = asyncio.create_task(consume())
            try:
                done, _ = await asyncio.wait(
                    {task},
                    timeout=self.agent_timeout or None,
                )
                if task not in done:
                    await _cancel_and_wait(task)
                    log.warning(
                        "Compaction timed out for chat %s after %ss",
                        chat_id,
                        self.agent_timeout,
                    )
                    return ""
                await task
            except Exception:
                log.exception("Compaction failed for chat %s", chat_id)
                return ""

            if failed:
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
            "Terminating process tree for %s pid=%s pgid=%s",
            label,
            process.pid,
            pgid,
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
                label,
                process.pid,
                pgid,
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
                process.communicate(),
                timeout=timeout_secs,
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
                    process,
                    f"chat {chat_id}",
                    grace=0.5,
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
        context: ExecutionContext | None = None,
    ):
        """Spawn a provider subprocess and yield StreamEvents.

        ``extra_env`` is merged on top of the base environment for the
        subprocess only (parent env is never mutated). Typically carries
        ``ENSO_ORIGIN_*`` so commands like ``enso message send`` can route
        back to the triggering conversation without an explicit ``--to``.

        ``context`` selects the execution binding: cwd, the policy launch
        (non-bypass flags), and — for policy launches — the allowlisted
        minimal environment instead of the full parent environment.
        """
        context = context or self.global_context(chat_id)
        launch = context.launch
        session_id = self._get_or_create_session(chat_id, provider.name)
        cmd = provider.build_command(prompt, model, session_id, effort=effort, launch=launch)
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

        # Strip new: prefix immediately so future calls use --resume even if
        # this run crashes mid-turn (the CLI has created the session by then).
        # ``promoted_from`` lets the failure paths below revert when the CLI
        # never got far enough to create the session, so the next turn does
        # not --resume a session that never existed.
        key = (chat_id, provider.name)
        promoted_from: str | None = None
        if session_id and session_id.startswith("new:"):
            promoted_from = session_id
            self.session_by_chat_provider[key] = session_id.removeprefix("new:")
            self.save_state()

        def revert_unused_session() -> None:
            if promoted_from and self.session_by_chat_provider.get(
                key
            ) == promoted_from.removeprefix("new:"):
                self.session_by_chat_provider[key] = promoted_from
                self.save_state()
                log.info(
                    "[%s] Reverted unused session for chat %s",
                    provider.name,
                    chat_id,
                )

        stderr = (
            asyncio.subprocess.STDOUT if provider.stderr_to_stdout() else asyncio.subprocess.PIPE
        )
        kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": stderr,
            "cwd": context.path,
        }
        limit = provider.stdout_limit()
        if limit:
            kwargs["limit"] = limit
        # A policy launch supplies a complete allowlisted environment; the
        # unrestricted/legacy path inherits the parent environment as before.
        base_env = launch.env if launch is not None and launch.env is not None else None
        if base_env is not None or extra_env:
            merged = dict(base_env) if base_env is not None else os.environ.copy()
            merged.update(extra_env or {})
            kwargs["env"] = merged
        log.debug(
            "[%s] subprocess cwd=%s launch=%s extra_env_keys=%s",
            provider.name,
            context.path,
            launch.mode if launch is not None else "legacy",
            sorted(extra_env) if extra_env else [],
        )

        try:
            process = await self._spawn_process(*cmd, **kwargs)
        except BaseException:
            revert_unused_session()
            provider.finalize_events()
            raise
        log.info("[%s] pid=%s", provider.name, process.pid)
        self.running_process_by_chat[chat_id] = process

        event_count = 0
        finalized = False
        emitted_error = False
        unparsed_output: list[str] = []
        unparsed_chars = 0

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
                        if unparsed_chars < 2000:
                            remaining = 2000 - unparsed_chars
                            kept = decoded[:remaining]
                            unparsed_output.append(kept)
                            unparsed_chars += len(kept)
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
                        if stream_event.kind == "error":
                            emitted_error = True
                        remember_session(stream_event)
                        yield stream_event
                await process.wait()
            else:
                # stdout arrives in one piece at the end, so progress has to
                # be drained from the provider's own side channel while the
                # process runs. Both feed one queue; the sentinel marks the
                # process exiting, never the poller.
                queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
                captured: dict[str, bytes] = {}

                async def run_to_completion() -> None:
                    try:
                        out, err = await process.communicate()
                        captured["stdout"], captured["stderr"] = out, err
                    finally:
                        await queue.put(None)

                async def pump_progress() -> None:
                    try:
                        async for status_event in provider.poll_progress():
                            await queue.put(status_event)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # Progress is decorative; the elapsed ticker carries on.
                        log.debug(
                            "[%s] progress polling stopped early",
                            provider.name,
                            exc_info=True,
                        )

                runner = asyncio.create_task(run_to_completion())
                poller = asyncio.create_task(pump_progress())
                try:
                    while True:
                        queued = await queue.get()
                        if queued is None:
                            break
                        yield queued
                    await runner
                finally:
                    await _cancel_and_wait(poller)
                    if not runner.done():
                        await _cancel_and_wait(runner)

                stderr_data = captured.get("stderr")
                parsed_events = provider.parse_complete_output(
                    captured.get("stdout", b"").decode(errors="replace")
                )
                event_count += len(parsed_events)
                for stream_event in parsed_events:
                    if stream_event.kind == "error":
                        emitted_error = True
                    remember_session(stream_event)
                    yield stream_event

            for stream_event in provider.finalize_events():
                event_count += 1
                if stream_event.kind == "error":
                    emitted_error = True
                remember_session(stream_event)
                yield stream_event
            finalized = True

            rc = process.returncode
            if rc and not emitted_error:
                error_text = ""
                if not provider.stderr_to_stdout() and process.stderr:
                    if stderr_data is None:
                        stderr_data = await process.stderr.read()
                    if stderr_data:
                        error_text = stderr_data.decode(errors="replace").strip()[:2000]
                elif unparsed_output:
                    error_text = "\n".join(unparsed_output).strip()[:2000]
                if not error_text:
                    error_text = f"{provider.name} exited with status {rc}"
                log.error("[%s] provider error: %s", provider.name, error_text)
                yield StreamEvent(kind="error", text=error_text)
        finally:
            if process.returncode is None:
                await self._terminate_process_tree(
                    process,
                    f"{provider.name} chat {chat_id}",
                    grace=1.0,
                )
            if not finalized:
                for stream_event in provider.finalize_events():
                    remember_session(stream_event)
            rc = process.returncode
            if rc and not event_count:
                # The CLI exited immediately (bad path, auth error, invalid
                # model) without creating the session; don't --resume it.
                revert_unused_session()
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
        *,
        context: ExecutionContext | None = None,
    ) -> tuple[str, str | None]:
        """Run a full provider request with status ticker and response delivery.

        Automatically injects any pending background messages into the prompt.
        Returns the terminal ``(outcome, terminal_reason)`` so the caller can
        record it on the audit turn: ``completed``, ``error``, ``timeout``, or
        (via cancellation in the caller) ``stopped``.
        """
        context = context or self.global_context(chat_id)
        prompt, output_instructions, surface_instructions = self._assemble_prompt(
            prompt, chat_id, provider_name, ctx, context
        )
        provider, model, effort, origin_env = self._resolve_request(
            provider_name, prompt, chat_id, ctx, context
        )

        await ctx.send_typing()
        header = status_header(provider_name, model, effort)
        status_msg = await self._post_initial_status(ctx, chat_id, header)
        state: dict[str, Any] = {
            "elapsed": 0,
            "header": header,
            "action": STATUS_INITIAL_ACTION,
        }
        stop = asyncio.Event()
        ticker = asyncio.create_task(self._run_ticker(ctx, status_msg, state, stop))
        msg_limit = self.transport.message_limit if self.transport else 4096

        try:
            response_parts, error_text, timed_out = await self._collect_provider_output(
                provider,
                prompt,
                chat_id,
                model,
                effort=effort,
                origin_env=origin_env,
                context=context,
                state=state,
            )
            if timed_out:
                await self._stop_ticker(ticker, stop, chat_id)
                await self._report_timeout(ctx, chat_id, provider_name, status_msg)
                return "timeout", None

            await self._stop_ticker(ticker, stop, chat_id)
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

            if error_text:
                await ctx.reply(_format_error(error_text[:4000]))
            elif response_text:
                await self._deliver_response(
                    ctx,
                    response_text,
                    output_instructions=output_instructions,
                    surface_instructions=surface_instructions,
                    msg_limit=msg_limit,
                )
            else:
                await ctx.reply("(No response)")
            return ("error", "provider_error") if error_text else ("completed", None)

        except asyncio.CancelledError:
            await self._stop_ticker(ticker, stop, chat_id)
            if status_msg is not None:
                with contextlib.suppress(Exception):
                    await ctx.edit_status(status_msg, "Stopped.")
            raise

        except Exception as exc:
            await self._stop_ticker(ticker, stop, chat_id)
            log.error("Error processing %s request: %s", provider_name, exc, exc_info=True)
            if status_msg is not None:
                with contextlib.suppress(Exception):
                    await ctx.delete_status(status_msg)
            for chunk in split_text(_format_error(str(exc)), limit=msg_limit):
                await ctx.reply(chunk)
            return "error", "exception"

    def _assemble_prompt(
        self,
        prompt: str,
        chat_id: str,
        provider_name: str,
        ctx: TransportContext,
        context: ExecutionContext,
    ) -> tuple[str, str, str]:
        """Build the prompt to send, with the transport instructions it carries.

        The instruction strings come back alongside the prompt because response
        delivery may only honour a fence the agent was actually told to emit.
        """
        # Inject background messages. Teams executions consume only messages
        # explicitly addressed to them — a global message must not leak into
        # an arbitrary route's context.
        bg = messages.consume(chat_id, include_global=context.workspace_id is None)
        if bg:
            prompt = f"{messages.format_for_injection(bg)}\n\n{prompt}"
            log.info("[%s] Injected %d background message(s) into prompt", provider_name, len(bg))

        # Inject compact seed if one is pending for this chat.
        prompt = self._consume_compact_seed(chat_id, prompt, provider_name)

        output_instructions = ctx.get_output_instructions().strip()
        if output_instructions:
            prompt = f"{prompt}\n\n{output_instructions}"

        surface_instructions = ctx.get_surface_instructions().strip()
        if surface_instructions:
            prompt = f"{prompt}\n\n{surface_instructions}"

        return prompt, output_instructions, surface_instructions

    def _resolve_request(
        self,
        provider_name: str,
        prompt: str,
        chat_id: str,
        ctx: TransportContext,
        context: ExecutionContext,
    ) -> tuple[BaseProvider, str, str | None, dict[str, str]]:
        """Resolve what to run this request with, and log that decision."""
        model = (
            context.model
            if context.model is not None
            else self.get_active_model(chat_id, provider_name)
        )
        effort = (
            context.effort
            if context.model is not None
            else self.get_active_effort(chat_id, provider_name, model)
        )
        provider = self.make_provider(provider_name, timeout=self.agent_timeout, context=context)

        try:
            origin_env = ctx.get_origin_env()
        except Exception:
            log.warning("get_origin_env failed for chat %s", chat_id, exc_info=True)
            origin_env = {}

        # Named-workspace operational logs are metadata-only; global Telegram
        # work retains the existing optional prompt logging behavior.
        is_bound = context.workspace_id is not None
        preview = "<redacted>" if is_bound else f"{prompt[:120]}"
        log.info(
            "[%s] request chat=%s provider_class=%s model=%s effort=%s prompt_len=%d preview=%s",
            provider_name,
            chat_id,
            provider.__class__.__name__,
            model,
            effort or "-",
            len(prompt),
            preview,
        )
        log.debug("[%s] origin_env_keys=%s", provider_name, sorted(origin_env))
        if self.debug_prompts and not is_bound:
            log.debug("[%s] full_prompt:\n%s", provider_name, prompt)
        return provider, model, effort, origin_env

    async def _post_initial_status(
        self, ctx: TransportContext, chat_id: str, header: str
    ) -> Any | None:
        """Post the status message the ticker will edit, or None if it fails.

        A transport that cannot post status must not fail the whole request;
        the ticker then runs typing-only.
        """
        try:
            return await ctx.reply_status(status_text(header, 0, STATUS_INITIAL_ACTION))
        except Exception:
            log.warning("Failed to send initial status message for chat %s", chat_id, exc_info=True)
            return None

    async def _stop_ticker(
        self, ticker: asyncio.Task, stop: asyncio.Event, chat_id: str
    ) -> None:
        """Halt the status ticker.

        Must run before the status message is edited or deleted; a late tick
        would overwrite the final status.
        """
        stop.set()
        error = await _cancel_and_wait(ticker)
        if error is not None and not isinstance(error, asyncio.CancelledError):
            log.warning(
                "Status ticker failed while stopping for chat %s: %s",
                chat_id,
                error,
            )

    async def _collect_provider_output(
        self,
        provider: BaseProvider,
        prompt: str,
        chat_id: str,
        model: str,
        *,
        effort: str | None,
        origin_env: dict[str, str],
        context: ExecutionContext,
        state: dict[str, Any],
    ) -> tuple[list[str], str, bool]:
        """Stream the provider to completion under the configured deadline.

        Returns the response parts, the last error text, and whether *our*
        deadline expired — which is not the same as a provider-reported
        timeout, and must not be labelled as one.
        """
        response_parts: list[str] = []
        error_text = ""

        async def consume_provider_events() -> None:
            nonlocal error_text
            async for event in self.run_provider(
                provider,
                prompt,
                chat_id,
                model,
                effort=effort,
                extra_env=origin_env,
                context=context,
            ):
                if self.debug_events:
                    log.debug(
                        "[%s] handling_event kind=%s text_len=%d session=%s",
                        provider.name,
                        event.kind,
                        len(event.text or ""),
                        event.session_id or "-",
                    )
                if event.kind == "response":
                    response_parts.append(event.text)
                elif event.kind == "status":
                    # The ticker owns delivery; this only records the latest.
                    state["action"] = event.text
                elif event.kind == "error":
                    error_text = event.text

        timed_out = await self._run_until_deadline(consume_provider_events(), chat_id)
        return response_parts, error_text, timed_out

    async def _run_until_deadline(
        self, work: Coroutine[Any, Any, None], chat_id: str
    ) -> bool:
        """Run ``work`` and return True only when our deadline expires."""
        if not self.agent_timeout:
            await work
            return False

        provider_task = asyncio.create_task(work)

        async def cancel_provider() -> None:
            error = await _cancel_and_wait(provider_task)
            if error is not None and not isinstance(error, asyncio.CancelledError):
                log.warning(
                    "Provider cleanup failed for chat %s: %s",
                    chat_id,
                    error,
                )

        try:
            done, _ = await asyncio.wait(
                {provider_task},
                timeout=self.agent_timeout,
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

    async def _report_timeout(
        self,
        ctx: TransportContext,
        chat_id: str,
        provider_name: str,
        status_msg: Any | None,
    ) -> None:
        """Queue the agent-facing timeout notice and tell the user about it."""
        duration = self._format_duration(self.agent_timeout)
        user_notice = f"Stopped after reaching the {duration} timeout."
        agent_notice = (
            "Enso stopped the previous agent turn after it reached the "
            f"configured {duration} timeout. Partial work may remain; inspect "
            "the current workspace and prior session before continuing."
        )
        log.warning(
            "[%s] request timed out chat=%s after %ss",
            provider_name,
            chat_id,
            self.agent_timeout,
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

    async def _deliver_response(
        self,
        ctx: TransportContext,
        response_text: str,
        *,
        output_instructions: str,
        surface_instructions: str,
        msg_limit: int,
    ) -> None:
        """Send a completed response over the richest channel it qualifies for.

        A persistent-surface draft outranks a structured message, which
        outranks plain text. Each parser only runs when its instructions were
        injected — a fence the agent was never told to emit stays literal text.
        """

        async def reply_in_chunks(text: str) -> None:
            for chunk in split_text(text, limit=msg_limit):
                await ctx.reply(chunk)

        if surface_instructions:
            publication = parse_surface_publication(response_text)
            if publication is not None:
                await ctx.offer_surface_draft(publication, response_text)
                return
            surface_fallback = parse_surface_fallback(response_text)
            if surface_fallback is not None:
                await reply_in_chunks(surface_fallback)
                return

        if output_instructions:
            message = parse_outbound_message(response_text)
            if message is not None:
                await ctx.reply_message(message)
                return
            fallback_text = parse_outbound_fallback(response_text)
            if fallback_text is not None:
                await reply_in_chunks(fallback_text)
                return

        if ctx.rich_markdown_enabled:
            await ctx.reply_markdown(response_text)
        else:
            await reply_in_chunks(response_text)

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
        """Background task that updates status and typing indicator.

        The clock advances visibly every second through the first 30 seconds,
        then updates every five seconds to conserve transport rate limits.
        Each edit includes the latest provider activity available at that tick.
        """
        status_updates_enabled = status_msg is not None
        failures = 0
        while not stop.is_set():
            await asyncio.sleep(1)
            if stop.is_set():
                break
            state["elapsed"] += 1
            elapsed = state["elapsed"]
            action = state.get("action")
            if status_updates_enabled and _status_edit_due(elapsed):
                text = status_text(state["header"], elapsed, action)
                try:
                    await asyncio.wait_for(ctx.edit_status(status_msg, text), timeout=5.0)
                    failures = 0
                except Exception:
                    failures += 1
                    if failures >= STATUS_MAX_EDIT_FAILURES:
                        status_updates_enabled = False
                        log.warning(
                            "Disabling status updates for current request after "
                            "%d consecutive edit failures",
                            failures,
                            exc_info=True,
                        )
                    else:
                        log.debug("Status edit failed; will retry", exc_info=True)
            # Refresh typing indicator every 4s (expires after 5s)
            if elapsed % 4 == 0:
                with contextlib.suppress(Exception):
                    await ctx.send_typing()
