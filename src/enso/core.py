"""Enso runtime — the engine that makes agents go."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.resources
import json
import logging
import os
import tempfile
from asyncio.subprocess import Process
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from croniter import croniter

from . import messages
from .config import CONFIG_DIR, STATE_FILE
from .jobs import Job, load_jobs
from .providers import BaseProvider, StreamEvent, get_provider

if TYPE_CHECKING:
    from .transports import BaseTransport, TransportContext

log = logging.getLogger(__name__)


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


class Runtime:
    """Central runtime holding all state, process management, and job scheduling."""

    def __init__(self, config: dict):
        self.config = config
        self.working_dir: str = config.get("working_dir", os.getcwd())
        os.makedirs(self.working_dir, exist_ok=True)
        self.models: dict[str, list[str]] = {
            name: pcfg.get("models", [])
            for name, pcfg in config.get("providers", {}).items()
        }
        self.transport: BaseTransport | None = None

        # Per-chat state
        self.active_provider_by_chat: dict[int, str] = {}
        self.active_model_by_chat_provider: dict[tuple[int, str], str] = {}
        self.session_by_chat_provider: dict[tuple[int, str], str] = {}
        self.running_process_by_chat: dict[int, Process] = {}
        self.running_task_by_chat: dict[int, asyncio.Task] = {}
        self.chat_lock_by_chat: dict[int, asyncio.Lock] = {}

        # Job scheduler state
        self._job_last_run: dict[str, datetime] = {}

    # -- Workspace setup --

    def install_system_prompts(self) -> None:
        """Set up working directory, system prompts, skills, and config-level dirs.

        Creates:
        - ~/.enso/jobs/ and ~/.enso/skills/
        - Bundled skills copied to ~/.enso/skills/
        - CLAUDE.md in working_dir (from bundled template, only if missing)
        - AGENTS.md, GEMINI.md as symlinks to CLAUDE.md
        - .claude/skills and .agents/skills symlinked to ~/.enso/skills/
          (so Claude, Codex, and Gemini auto-discover skills)
        """
        from .config import JOBS_DIR

        skills_dir = os.path.join(CONFIG_DIR, "skills")
        for d in (JOBS_DIR, skills_dir):
            os.makedirs(d, exist_ok=True)

        self._install_bundled_skills(skills_dir)

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
            "session_by_chat_provider": {
                f"{cid}:{prov}": sid
                for (cid, prov), sid in self.session_by_chat_provider.items()
            },
            "job_last_run": {
                name: ts.isoformat()
                for name, ts in self._job_last_run.items()
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
                self.active_provider_by_chat[int(k)] = v
            for k, v in data.get("active_model_by_chat_provider", {}).items():
                cid_str, provider = k.split(":", 1)
                self.active_model_by_chat_provider[(int(cid_str), provider)] = v
            for k, v in data.get("session_by_chat_provider", {}).items():
                cid_str, provider = k.split(":", 1)
                self.session_by_chat_provider[(int(cid_str), provider)] = v
            for name, ts in data.get("job_last_run", {}).items():
                self._job_last_run[name] = datetime.fromisoformat(ts)
            log.info(
                "Loaded state: %d providers, %d sessions",
                len(self.active_provider_by_chat),
                len(self.session_by_chat_provider),
            )
        except Exception:
            log.exception("Failed to load state, starting fresh")

    # -- Accessors --

    def get_active_provider(self, chat_id: int) -> str:
        """Return active provider for chat, defaulting to claude."""
        return self.active_provider_by_chat.get(chat_id, "claude")

    def get_active_model(self, chat_id: int, provider: str) -> str:
        """Return active model for chat+provider, defaulting to first in list."""
        stored = self.active_model_by_chat_provider.get((chat_id, provider))
        if stored and stored in self.models.get(provider, []):
            return stored
        models = self.models.get(provider, [])
        return models[0] if models else "default"

    def get_chat_lock(self, chat_id: int) -> asyncio.Lock:
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
        path = providers_cfg.get(provider_name, {}).get("path", provider_name)
        return get_provider(provider_name, path)

    # -- Session management --

    # Providers that support pre-assigned session IDs. For these,
    # Enso generates the ID upfront so it persists across restarts.
    # Other providers (codex, gemini) generate their own IDs which we
    # capture from stream events.
    _SELF_MANAGED_SESSIONS: ClassVar[set[str]] = {"claude"}

    def _get_or_create_session(
        self, chat_id: int, provider_name: str
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

    # -- Process control --

    async def stop_chat(self, chat_id: int) -> tuple[bool, str | None]:
        """Stop running process/task for a chat. Returns (had_something, error_msg)."""
        process = self.running_process_by_chat.get(chat_id)
        task = self.running_task_by_chat.get(chat_id)

        if process is None and task is None:
            return False, None

        try:
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.5)
                except TimeoutError:
                    if process.returncode is None:
                        process.kill()
            if task and not task.done():
                task.cancel()
            return True, None
        except Exception as exc:
            return True, str(exc)

    # -- Core streaming --

    async def run_provider(
        self, provider: BaseProvider, prompt: str, chat_id: int, model: str
    ):
        """Spawn a provider subprocess and yield StreamEvents."""
        session_id = self._get_or_create_session(chat_id, provider.name)
        cmd = provider.build_command(prompt, model, session_id)
        log.info("[%s] Spawning: %s", provider.name, " ".join(cmd[:6]) + " ...")

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

        process = await asyncio.create_subprocess_exec(*cmd, **kwargs)
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
                for stream_event in provider.parse_event(raw):
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
            rc = process.returncode
            log.info("[%s] pid=%s exit=%s events=%d", provider.name, process.pid, rc, event_count)
            if self.running_process_by_chat.get(chat_id) is process:
                self.running_process_by_chat.pop(chat_id, None)

    # -- Request handling --

    async def process_request(
        self,
        provider_name: str,
        prompt: str,
        chat_id: int,
        ctx: TransportContext,
    ) -> None:
        """Run a full provider request with status ticker and response delivery.

        Automatically injects any pending background messages into the prompt.
        """
        # Inject background messages
        bg = messages.consume()
        if bg:
            prompt = f"{messages.format_for_injection(bg)}\n\n{prompt}"

        provider = self.make_provider(provider_name)
        model = self.get_active_model(chat_id, provider_name)
        display = provider_name.capitalize()

        log.info("[%s] chat=%s model=%s: %.80s", provider_name, chat_id, model, prompt)

        status_msg = await ctx.reply_status(f"({display} / 0s) Working...")
        state = {"status": "Working...", "elapsed": 0, "display": display}
        stop = asyncio.Event()
        ticker = asyncio.create_task(self._run_ticker(ctx, status_msg, state, stop))

        response_parts: list[str] = []
        error_text = ""

        try:
            async for event in self.run_provider(provider, prompt, chat_id, model):
                if event.kind == "status":
                    state["status"] = event.text
                elif event.kind == "response":
                    response_parts.append(event.text)
                elif event.kind == "error":
                    error_text = event.text

            stop.set()
            ticker.cancel()
            await ctx.delete_status(status_msg)

            response_text = provider.format_response(response_parts)
            prefix = f"({display} / {state['elapsed']}s)"

            if response_text:
                for chunk in split_text(f"{prefix} {response_text}"):
                    await ctx.reply(chunk)
            elif error_text:
                await ctx.reply(f"{prefix} Error: {error_text[:4000]}")
            else:
                await ctx.reply(f"{prefix} (No response)")

        except asyncio.CancelledError:
            stop.set()
            ticker.cancel()
            with contextlib.suppress(Exception):
                await ctx.edit_status(status_msg, f"({display}) Stopped.")
            raise

        except Exception as exc:
            stop.set()
            ticker.cancel()
            log.error("Error processing %s request: %s", provider_name, exc, exc_info=True)
            prefix = f"({display} / {state['elapsed']}s)"
            try:
                await ctx.edit_status(status_msg, f"{prefix} Error: {str(exc)[:4000]}")
            except Exception:
                for chunk in split_text(f"{prefix} Error: {exc}"):
                    await ctx.reply(chunk)

    async def _run_ticker(
        self, ctx: TransportContext, status_msg: Any, state: dict, stop: asyncio.Event
    ) -> None:
        """Background task that updates [Provider Xs] status every second."""
        while not stop.is_set():
            await asyncio.sleep(1)
            if stop.is_set():
                break
            state["elapsed"] += 1
            text = f"({state['display']} / {state['elapsed']}s) {state['status']}"
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ctx.edit_status(status_msg, text), timeout=5.0)

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
                if self._should_run_job(job, now):
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
        return cron.get_next(datetime) <= now

    async def _execute_job(self, job: Job) -> None:
        """Run a job: prerun gate, provider subprocess, notify, queue message."""
        log.info("Running job: %s (%s/%s)", job.name, job.provider, job.model)

        # Prerun gate
        prerun_output = ""
        if job.prerun:
            prerun_script = os.path.join(job.job_dir, job.prerun)
            if os.path.isfile(prerun_script):
                proc = await asyncio.create_subprocess_exec(
                    "bash", prerun_script,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=job.job_dir,
                )
                stdout, _ = await proc.communicate()
                if proc.returncode != 0:
                    log.info("Job '%s' prerun failed, skipping", job.name)
                    self._job_last_run[job.dir_name] = datetime.now()
                    self.save_state()
                    return
                prerun_output = stdout.decode(errors="replace").strip()

        # Build prompt with prerun output injection
        prompt = job.prompt
        if prerun_output:
            prompt = prompt.replace("{{prerun_output}}", prerun_output)

        # Run provider in batch mode
        provider = self.make_provider(job.provider)
        cmd = provider.build_batch_command(prompt, job.model)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self.working_dir,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode(errors="replace").strip()

        # Only notify on failure — successful jobs handle their own messaging
        if proc.returncode != 0:
            if self.transport:
                label = f"{job.name} (exit {proc.returncode})"
                await self.transport.notify(f"\u26a0\ufe0f [{label}]\n{output}"[:4096])
            messages.send(output, source=f"job:{job.dir_name}")

        self._job_last_run[job.dir_name] = datetime.now()
        self.save_state()
        log.info("Job '%s' completed (exit %s)", job.name, proc.returncode)
