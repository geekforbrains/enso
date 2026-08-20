"""Starlette web UI for Enso.

Exposes ``create_app(runtime) -> Starlette``. The runtime is stashed on
``app.state.runtime`` and every handler reads configuration via
``runtime.config``.

Data comes from the file/DB-backed modules (``enso.jobs``, ``enso.runs``,
``enso.tables``, ``enso.frontmatter``); this module only renders and mutates — it never owns
any storage of its own. All file writes that target skills, jobs, or
AGENTS.md are path-guarded so a crafted name can never escape the allowed
directory.
"""

from __future__ import annotations

import contextlib
import errno
import functools
import logging
import os
import secrets
import shutil
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import (
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from .. import docs, frontmatter, runs, slack_cache, sqlite_store, tables
from ..config import (
    CONFIG_DIR,
    JOBS_DIR,
)
from ..fsutil import atomic_write_text, is_within
from ..instructions import MAX_SHARED_INSTRUCTION_BYTES
from ..jobs import Job, load_jobs, load_jobs_with_errors
from ..policy import PolicyCheck, check_provider
from ..secret_refs import resolve_config_secret
from ..teams import load_catalog
from .configuration import (
    ConfigurationView,
    build_configuration_view,
    build_policy_check_view,
    with_policy_checks,
    with_workspace_agents,
)
from .workspace_instructions import (
    AGENT_FILENAME,
    AgentConflict,
    AgentEncodingError,
    AgentFileError,
    AgentFilesystemError,
    AgentIntegrityError,
    AgentListing,
    AgentNotFound,
    AgentTooLarge,
    UnsafeAgentPath,
    discover_agents,
    read_agent,
    write_agent,
)

log = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_TEMPLATES_DIR = _HERE / "templates"
_STATIC_DIR = _HERE / "static"

# Cap the run output we inline into a page so a giant transcript can't OOM the
# renderer; the row's ``output_bytes`` still reports the true size.
_OUTPUT_VIEW_CAP = 200_000
_RUNS_PAGE_SIZE = 50
_MAX_RUNS_PAGE = 100
_TABLE_PAGE_SIZE = tables.DEFAULT_PAGE_SIZE
_MAX_TABLE_PAGE = tables.MAX_OFFSET // _TABLE_PAGE_SIZE + 1
_MAX_TABLE_SCHEMA_SQL_CHARS = 20_000
_MAX_TABLE_INDEXES = 25
_MAX_TABLE_INDEX_SQL_CHARS = 4_000
_MAX_WORKSPACE_INSTRUCTION_BYTES = 128 * 1024


# ---------------------------------------------------------------------------
# Template environment + filters
# ---------------------------------------------------------------------------

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _fmt_ts(value: object) -> str:
    """Render an ISO-8601 UTC timestamp as a friendly *local* time.

    Within 12 hours it reads as relative — ``4s ago`` · ``12m ago`` · ``11h
    ago`` — then falls back to the local calendar form ``Today, 5:30am`` ·
    ``Yesterday, 1:22pm`` · ``Jul 7th, 8:00pm`` (the year is appended when it
    differs from the current one). Falls back to the raw string if unparseable.
    """
    if not value:
        return ""
    text = str(value)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone()  # convert UTC -> the server's local timezone
    now = datetime.now().astimezone()

    # Recent timestamps read as relative ("4s ago", "12m ago", "11h ago") up to
    # 12 hours; older ones use the local calendar format below.
    total = (now - dt).total_seconds()
    if total >= 0:
        if total < 60:
            return f"{int(total)}s ago"
        if total < 3600:
            return f"{int(total // 60)}m ago"
        if total < 12 * 3600:
            return f"{int(total // 3600)}h ago"

    hour12 = dt.hour % 12 or 12
    meridiem = "am" if dt.hour < 12 else "pm"
    clock = f"{hour12}:{dt.minute:02d}{meridiem}"

    day = dt.date()
    if day == now.date():
        return f"Today, {clock}"
    if day == now.date() - timedelta(days=1):
        return f"Yesterday, {clock}"
    stamp = f"{dt.strftime('%b')} {_ordinal(dt.day)}"
    if dt.year != now.year:
        stamp += f" {dt.year}"
    return f"{stamp}, {clock}"


def _fmt_duration(ms: object) -> str:
    """Render a millisecond duration as a compact human string."""
    if ms is None or ms == "":
        return ""
    try:
        # Values arrive straight from SQLite rows, so the except below is the
        # only real guard on the type.
        total = int(ms)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return ""
    if total < 1000:
        return f"{total}ms"
    secs = total / 1000
    if secs < 60:
        return f"{secs:.1f}s"
    minutes, seconds = divmod(int(secs), 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _fmt_bytes(size: object) -> str:
    """Render a byte count as a compact human string."""
    if size is None or size == "":
        return ""
    try:
        # Same untrusted-row caveat as _fmt_duration.
        n = float(size)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


_DOW_NAMES = {
    "0": "Sunday",
    "1": "Monday",
    "2": "Tuesday",
    "3": "Wednesday",
    "4": "Thursday",
    "5": "Friday",
    "6": "Saturday",
    "7": "Sunday",
}
_DOW_ABBR = {
    "0": "Sun",
    "1": "Mon",
    "2": "Tue",
    "3": "Wed",
    "4": "Thu",
    "5": "Fri",
    "6": "Sat",
    "7": "Sun",
}


def _cron_step(field: str) -> int | None:
    """Return N for a ``*/N`` step field, else None."""
    if field.startswith("*/") and field[2:].isdigit():
        return int(field[2:])
    return None


def _ordinal(n: int) -> str:
    """1 -> '1st', 2 -> '2nd', 11 -> '11th'."""
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _clock(hour: int, minute: int) -> str:
    """24h -> 12h clock, e.g. (9, 0) -> '9:00 AM', (18, 30) -> '6:30 PM'."""
    suffix = "AM" if hour < 12 else "PM"
    return f"{hour % 12 or 12}:{minute:02d} {suffix}"


def _describe_dow(field: str) -> str | None:
    """Human phrase for a day-of-week field, or None if not recognised.

    Returns forms like 'weekdays', 'weekends', 'Mondays', 'Mon, Wed, Fri'.
    """
    if field == "1-5":
        return "weekdays"
    if field in _DOW_NAMES and "," not in field:
        return f"{_DOW_NAMES[field]}s"
    parts = field.split(",")
    if parts and all(p in _DOW_ABBR for p in parts):
        if set(parts) == {"0", "6"}:
            return "weekends"
        return ", ".join(_DOW_ABBR[p] for p in parts)
    return None


def _humanize_cron(expr: object) -> str:
    """Render a 5-field cron expression as a human phrase.

    Covers the common shapes Enso jobs use (intervals, hourly, daily, weekday
    and named-day schedules). Anything it doesn't recognise falls back to the
    raw expression, so it is never misleading.
    """
    text = str(expr or "").strip()
    parts = text.split()
    if len(parts) != 5:
        return text
    minute, hour, dom, month, dow = parts

    # Only month-agnostic shapes are humanised; cron's dom/dow OR-semantics get
    # subtle when both are restricted, so don't guess there.
    if month != "*":
        return text
    if dom != "*" and dow != "*":
        return text

    dow_phrase = _describe_dow(dow) if dow != "*" else ""
    if dow != "*" and dow_phrase is None:
        return text

    # Interval minutes: */N * * * *  (and the plain every-minute case)
    m_step = _cron_step(minute)
    if m_step and hour == "*" and dom == "*" and dow == "*":
        return "Every minute" if m_step == 1 else f"Every {m_step} minutes"
    if minute == "*" and hour == "*" and dom == "*" and dow == "*":
        return "Every minute"

    # Interval hours: M */N * * *
    h_step = _cron_step(hour)
    if minute.isdigit() and h_step and dom == "*" and dow == "*":
        base = "Every hour" if h_step == 1 else f"Every {h_step} hours"
        return base if minute == "0" else f"{base} at :{int(minute):02d}"

    # Hourly at a given minute: M * * * *
    if minute.isdigit() and hour == "*" and dom == "*" and dow == "*":
        return "Every hour" if minute == "0" else f"Hourly at :{int(minute):02d}"

    # Specific time of day: M H ...
    if minute.isdigit() and hour.isdigit():
        when = _clock(int(hour), int(minute))
        if dow_phrase:
            label = dow_phrase[0].upper() + dow_phrase[1:]
            return f"{label} at {when}"
        if dom.isdigit():
            return f"Monthly on the {_ordinal(int(dom))} at {when}"
        if dom == "*":
            return f"Daily at {when}"

    return text


templates.env.filters["fmt_ts"] = _fmt_ts
templates.env.filters["fmt_duration"] = _fmt_duration
templates.env.filters["fmt_bytes"] = _fmt_bytes
templates.env.filters["humanize_cron"] = _humanize_cron

# Tailwind class pairs for status badges, shared with templates.
RUN_BADGES = {
    "running": "bg-surface-muted text-ink animate-pulse",
    "ok": "bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300",
    "error": "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300",
    "timeout": "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
    "prerun_error": "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300",
    "prerun_timeout": ("bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300"),
}
templates.env.globals["run_badges"] = RUN_BADGES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(request, template: str, *, status_code: int = 200, **ctx) -> Response:
    """Render a complete server-side page."""
    ctx["current_path"] = request.url.path
    ctx["flash"] = request.query_params.get("msg")
    ctx["csrf_token"] = request.app.state.csrf_token
    return templates.TemplateResponse(
        request,
        template,
        ctx,
        status_code=status_code,
    )


def _redirect(url: str) -> Response:
    """Redirect to the result of a successful write."""
    return RedirectResponse(url, status_code=303)


def _page_url(path: str, page: int, **filters: str | None) -> str:
    """Build a shareable filtered pagination URL, omitting default values."""
    query = [(key, value) for key, value in filters.items() if value]
    if page > 1:
        query.append(("page", str(page)))
    return f"{path}?{urlencode(query)}" if query else path


def _csrf_protected(handler):
    """Require the process-scoped CSRF token before a write handler runs."""

    @functools.wraps(handler)
    async def protected(request):
        form = await request.form()
        supplied = request.headers.get("X-CSRF-Token") or form.get("_csrf")
        expected = request.app.state.csrf_token
        if not isinstance(supplied, str) or not secrets.compare_digest(supplied, expected):
            return PlainTextResponse("Forbidden", status_code=403)
        return await handler(request)

    protected._csrf_protected = True
    return protected


def _normalize_host(value: object) -> str:
    """Normalize a Host header/config value to a canonical hostname or IP."""
    text = str(value or "").strip().lower()
    if text.startswith("["):
        closing = text.find("]")
        return text[1:closing] if closing > 0 else ""
    if text.count(":") == 1:
        text = text.split(":", 1)[0]
    return text.rstrip(".")


def _allowed_web_hosts(web_cfg: dict) -> frozenset[str]:
    """Return explicit request hosts, always including loopback spellings."""
    allowed = {"localhost", "127.0.0.1", "::1"}
    bind_host = _normalize_host(web_cfg.get("host", "127.0.0.1"))
    if bind_host not in {"", "0.0.0.0", "::"}:
        allowed.add(bind_host)
    configured = web_cfg.get("allowed_hosts", [])
    if isinstance(configured, list):
        allowed.update(
            host for value in configured if (host := _normalize_host(value)) and host != "*"
        )
    return frozenset(allowed)


def _find_job(name: str) -> Job | None:
    """Return the job whose ``dir_name`` matches ``name``."""
    return next((j for j in load_jobs() if j.dir_name == name), None)


def _active_config(request) -> dict:
    """Return the exact configuration held by the running service."""
    runtime = request.app.state.runtime
    config = getattr(runtime, "config", None)
    return config if isinstance(config, dict) else {}


def _configuration_view(
    request,
    *,
    loaded_jobs: list[Job] | None = None,
    job_errors: dict[str, tuple[str, ...]] | None = None,
) -> ConfigurationView:
    """Build one request-local, cache-only view of active execution bindings."""
    config = _active_config(request)
    if loaded_jobs is None or job_errors is None:
        loaded_jobs, job_errors = load_jobs_with_errors(config)
    try:
        slack_directory = slack_cache.load()
    except (AttributeError, OSError, TypeError, ValueError):
        log.warning("Could not load Slack directory cache for web UI", exc_info=True)
        slack_directory = {}
    return build_configuration_view(
        config,
        jobs=loaded_jobs,
        job_errors=job_errors,
        slack_directory=slack_directory,
    )


def _safe_name(name: str) -> bool:
    """True when ``name`` is a bare path segment (no traversal, no separators)."""
    return (
        bool(name)
        and name not in (".", "..")
        and "/" not in name
        and "\\" not in name
        and "\0" not in name
    )


class _UnsafePrerunPathError(ValueError):
    """Raised when a configured prerun path cannot be opened safely."""


def _job_prerun_parts(job: Job) -> tuple[str, ...]:
    """Return safe relative path parts for a configured prerun script."""
    if (
        not job.prerun
        or "\0" in job.prerun
        or os.path.isabs(job.prerun)
        or not _safe_name(job.dir_name)
    ):
        raise _UnsafePrerunPathError
    parts = tuple(part for part in job.prerun.split(os.sep) if part not in ("", "."))
    if not parts or ".." in parts:
        raise _UnsafePrerunPathError
    return parts


def _open_job_prerun(job: Job) -> tuple[int, int, str, os.stat_result]:
    """Open a regular prerun file without following any owned-path symlinks."""
    parts = _job_prerun_parts(job)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError("Secure prerun editing is unavailable")

    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    root_fd = os.open(os.path.abspath(JOBS_DIR), os.O_RDONLY | directory | close_on_exec)
    parent_fd = root_fd
    file_fd = -1
    try:
        dir_flags = os.O_RDONLY | directory | nofollow | close_on_exec
        for component in (job.dir_name, *parts[:-1]):
            next_fd = os.open(component, dir_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd

        file_flags = os.O_RDONLY | nofollow | close_on_exec | getattr(os, "O_NONBLOCK", 0)
        file_fd = os.open(parts[-1], file_flags, dir_fd=parent_fd)
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise _UnsafePrerunPathError
        return parent_fd, file_fd, parts[-1], file_stat
    except BaseException:
        if file_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(file_fd)
        with contextlib.suppress(OSError):
            os.close(parent_fd)
        raise


def _atomic_write_text_at(
    parent_fd: int,
    filename: str,
    text: str,
    *,
    mode: int,
    expected: os.stat_result,
) -> None:
    """Atomically replace a held directory's existing file without path races."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    temp_name = ""
    fd = -1
    for _ in range(10):
        temp_name = f".enso-prerun-{secrets.token_hex(16)}.tmp"
        try:
            fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | close_on_exec,
                0o600,
                dir_fd=parent_fd,
            )
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError("Could not allocate a prerun temporary file")

    try:
        stream = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with stream:
            stream.write(text)
            stream.flush()
            # Writing can clear setuid/setgid bits, so restore the full mode last.
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())

        current = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise OSError(errno.EBUSY, "Prerun script changed during save")
        os.replace(
            temp_name,
            filename,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temp_name = ""
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        if temp_name:
            with contextlib.suppress(OSError):
                os.unlink(temp_name, dir_fd=parent_fd)


def _remove_owned_tree(base: str, name: str) -> None:
    """Atomically detach and remove one direct child without following symlinks."""
    if not _safe_name(name):
        raise ValueError("Unsafe directory name")
    base_abs = os.path.abspath(base)
    target = os.path.abspath(os.path.join(base_abs, name))
    if os.path.dirname(target) != base_abs:
        raise ValueError("Directory is outside its owned root")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError("Secure directory deletion is unavailable")

    base_fd = os.open(base_abs, os.O_RDONLY | nofollow | directory)
    detached = f".deleting-{secrets.token_hex(16)}"
    try:
        os.rename(name, detached, src_dir_fd=base_fd, dst_dir_fd=base_fd)
        detached_path = os.path.join(base_abs, detached)
        try:
            mode = os.stat(detached, dir_fd=base_fd, follow_symlinks=False).st_mode
            if stat.S_ISLNK(mode):
                os.unlink(detached, dir_fd=base_fd)
            elif stat.S_ISDIR(mode):
                if not shutil.rmtree.avoids_symlink_attacks:
                    raise OSError("Secure directory deletion is unavailable")
                opened_root = os.fstat(base_fd)
                current_root = os.stat(base_abs, follow_symlinks=False)
                if (opened_root.st_dev, opened_root.st_ino) != (
                    current_root.st_dev,
                    current_root.st_ino,
                ):
                    raise OSError("Owned root changed during deletion")
                # rmtree unlinks nested symlinks; it never recurses into them.
                shutil.rmtree(detached_path)
            else:
                raise FileNotFoundError(detached_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.rename(detached, name, src_dir_fd=base_fd, dst_dir_fd=base_fd)
            raise
    finally:
        os.close(base_fd)


# -- Skill discovery --------------------------------------------------------


def _skills_base() -> str:
    return os.path.join(CONFIG_DIR, "skills")


def _skill_description(path: str) -> str:
    try:
        meta, _ = frontmatter.read(path)
    except (OSError, ValueError):
        return ""
    desc = meta.get("description") if isinstance(meta, dict) else ""
    return str(desc) if desc else ""


def _enso_skills() -> list[dict]:
    base = _skills_base()
    out: list[dict] = []
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            skill_md = os.path.join(base, name, "SKILL.md")
            if os.path.isfile(skill_md):
                out.append(
                    {
                        "name": name,
                        "description": _skill_description(skill_md),
                        "path": skill_md,
                        "editable": True,
                    }
                )
    return out


def _external_skill_roots(request) -> list[str]:
    runtime = request.app.state.runtime
    cfg = getattr(runtime, "config", {}) or {}
    web = cfg.get("web", {}) if isinstance(cfg, dict) else {}
    roots = web.get("external_skill_roots", []) if isinstance(web, dict) else []
    return [os.path.expanduser(r) for r in (roots or [])]


def _external_skills(request, owned_names: set[str] | None = None) -> list[dict]:
    out: list[dict] = []
    # Skill detail routes identify a skill by name alone. Mirror _resolve_skill's
    # precedence here so every listed card resolves back to the source it shows:
    # Enso-owned skills win, followed by the first configured external root.
    seen = (
        set(owned_names) if owned_names is not None else {skill["name"] for skill in _enso_skills()}
    )
    for root in _external_skill_roots(request):
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if name in seen:
                continue
            skill_md = os.path.join(root, name, "SKILL.md")
            if os.path.isfile(skill_md):
                seen.add(name)
                out.append(
                    {
                        "name": name,
                        "description": _skill_description(skill_md),
                        "path": skill_md,
                        "editable": False,
                        "root": root,
                    }
                )
    return out


def _skill_inventory(request) -> tuple[list[dict], list[dict]]:
    """Return the Enso-owned and visible system skill tiers."""
    enso_skills = _enso_skills()
    owned_names = {skill["name"] for skill in enso_skills}
    return enso_skills, _external_skills(request, owned_names)


def _resolve_skill(request, name: str) -> tuple[str | None, bool]:
    """Resolve a skill name to its SKILL.md path and whether it is editable.

    Enso-owned skills (under ``CONFIG_DIR/skills``) win and are editable;
    otherwise the first matching external root is used (read-only).
    """
    if not _safe_name(name):
        return None, False
    enso_md = os.path.join(_skills_base(), name, "SKILL.md")
    if os.path.isfile(enso_md):
        return enso_md, True
    for root in _external_skill_roots(request):
        candidate = os.path.join(root, name, "SKILL.md")
        if os.path.isfile(candidate):
            return candidate, False
    return None, False


# ---------------------------------------------------------------------------
# Routes — dashboard
# ---------------------------------------------------------------------------


async def dashboard(request):
    jobs, job_errors = load_jobs_with_errors(_active_config(request))
    configuration = _configuration_view(
        request,
        loaded_jobs=jobs,
        job_errors=job_errors,
    )
    jobs_enabled = sum(1 for j in jobs if j.enabled)
    enso_skills, system_skills = _skill_inventory(request)
    try:
        latest = await run_in_threadpool(runs.list_runs, limit=6)
        runs_error = None
    except (OSError, sqlite3.Error) as exc:
        log.warning("Could not load recent run history", exc_info=True)
        latest = []
        runs_error = sqlite_store.database_error_kind(exc)
    try:
        table_listing = await run_in_threadpool(tables.list_tables)
        tables_total = sum(1 for item in table_listing.tables if item.available)
        tables_error = None
    except (OSError, sqlite3.Error) as exc:
        log.warning("Could not load table count", exc_info=True)
        tables_total = None
        tables_error = sqlite_store.database_error_kind(exc)
    return _render(
        request,
        "index.html",
        jobs_enabled=jobs_enabled,
        jobs_total=len(jobs),
        skills_total=len(enso_skills) + len(system_skills),
        skills_enso=len(enso_skills),
        skills_system=len(system_skills),
        docs_total=len(docs.load_docs().docs),
        tables_total=tables_total,
        tables_available=tables_error is None,
        tables_error=tables_error,
        latest_runs=latest,
        runs_available=runs_error is None,
        runs_error=runs_error,
        configuration=configuration,
        configuration_summary=configuration.summary,
    )


# ---------------------------------------------------------------------------
# Routes — jobs
# ---------------------------------------------------------------------------


async def jobs_list(request):
    show = request.query_params.get("show") or "all"
    all_jobs = load_jobs()
    counts = {
        "all": len(all_jobs),
        "enabled": sum(1 for j in all_jobs if j.enabled),
        "disabled": sum(1 for j in all_jobs if not j.enabled),
    }
    if show == "enabled":
        jobs = [j for j in all_jobs if j.enabled]
    elif show == "disabled":
        jobs = [j for j in all_jobs if not j.enabled]
    else:
        show = "all"
        jobs = all_jobs
    return _render(request, "jobs.html", jobs=jobs, active_show=show, counts=counts)


async def job_detail(request):
    name = request.path_params["name"]
    job = _find_job(name)
    if job is None:
        return PlainTextResponse("Job not found", status_code=404)
    try:
        meta, _ = frontmatter.read(job.path)
    except (OSError, ValueError):
        meta = {}
    prerun_exists = False
    prerun_content: str | None = None
    prerun_error: str | None = None
    if job.prerun:
        try:
            parent_fd, file_fd, _, _ = _open_job_prerun(job)
        except _UnsafePrerunPathError:
            prerun_error = (
                "This configured path isn't a regular file wholly inside the job "
                "directory, so it can't be edited here."
            )
        except FileNotFoundError:
            prerun_error = "Configured script not found. Create it on disk before editing it here."
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                prerun_error = (
                    "Configured script paths cannot contain symlinks or non-directory "
                    "parent components."
                )
            else:
                prerun_error = "The configured script could not be opened safely."
        else:
            prerun_exists = True
            try:
                stream = os.fdopen(file_fd, encoding="utf-8")
                file_fd = -1
                with stream:
                    prerun_content = stream.read()
            except (OSError, UnicodeError):
                prerun_error = "The configured script could not be read as UTF-8."
            finally:
                if file_fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(file_fd)
                with contextlib.suppress(OSError):
                    os.close(parent_fd)
    try:
        job_runs = await run_in_threadpool(
            runs.list_runs,
            kind="job",
            name=name,
            limit=50,
        )
        runs_error = None
    except (OSError, sqlite3.Error) as exc:
        log.warning("Could not load run history for job %s", name, exc_info=True)
        job_runs = []
        runs_error = sqlite_store.database_error_kind(exc)
    return _render(
        request,
        "job_detail.html",
        job=job,
        meta=meta,
        prerun_exists=prerun_exists,
        prerun_content=prerun_content,
        prerun_error=prerun_error,
        job_runs=job_runs,
        runs_error=runs_error,
    )


async def job_toggle(request):
    name = request.path_params["name"]
    job = _find_job(name)
    if job is None:
        return PlainTextResponse("Job not found", status_code=404)
    # Defence in depth: a JOB.md symlink must not escape the jobs directory.
    if not is_within(JOBS_DIR, job.path):
        return PlainTextResponse("Forbidden", status_code=403)
    # Change only the scalar. Re-serializing the whole block would erase
    # comments and would corrupt legacy jobs whose YAML-like values contain an
    # unquoted colon (which the job loader intentionally still accepts).
    frontmatter.write_scalar(job.path, "enabled", str(not job.enabled).lower())
    return _redirect(f"/jobs/{name}")


async def job_run(request):
    name = request.path_params["name"]
    runtime = request.app.state.runtime
    if runtime is None or not hasattr(runtime, "jobs"):
        return _redirect(f"/jobs/{name}?msg=Run+now+is+unavailable")
    try:
        result = await runtime.jobs.run_now(name)
    except Exception as exc:
        log.warning("run_now failed for %s", name, exc_info=True)
        return _redirect(f"/jobs/{name}?msg=Run+failed:+{exc}")
    if result.run_id:
        return _redirect(f"/runs/{result.run_id}")
    if result.status == "no_work":
        return _redirect(f"/jobs/{name}?msg=No+work;+provider+was+not+run")
    return _redirect(f"/jobs/{name}")


async def job_edit_prompt(request):
    name = request.path_params["name"]
    job = _find_job(name)
    if job is None:
        return PlainTextResponse("Job not found", status_code=404)
    # Defence in depth: the resolved JOB.md must live under JOBS_DIR.
    if not is_within(JOBS_DIR, job.path):
        return PlainTextResponse("Forbidden", status_code=403)
    form = await request.form()
    content = (form.get("content") or "").replace("\r\n", "\n")
    # Keep the fenced prefix byte-for-byte and swap only the prompt body. This
    # remains safe for legacy YAML-like frontmatter accepted by the job loader.
    frontmatter.write_body(job.path, content)
    return _redirect(f"/jobs/{name}")


async def job_edit_prerun(request):
    name = request.path_params["name"]
    job = _find_job(name)
    if job is None or not job.prerun:
        return PlainTextResponse("Prerun script not found", status_code=404)
    try:
        parent_fd, file_fd, filename, file_stat = _open_job_prerun(job)
    except _UnsafePrerunPathError:
        return PlainTextResponse("Forbidden", status_code=403)
    except FileNotFoundError:
        return PlainTextResponse("Prerun script not found", status_code=404)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            return PlainTextResponse("Forbidden", status_code=403)
        return PlainTextResponse("Prerun script unavailable", status_code=503)

    try:
        os.close(file_fd)
        file_fd = -1
        form = await request.form()
        content = (form.get("content") or "").replace("\r\n", "\n")
        try:
            _atomic_write_text_at(
                parent_fd,
                filename,
                content,
                mode=stat.S_IMODE(file_stat.st_mode),
                expected=file_stat,
            )
        except OSError:
            log.warning("Could not edit prerun for job %s", name, exc_info=True)
            return PlainTextResponse("Prerun script unavailable", status_code=503)
        return _redirect(f"/jobs/{name}")
    finally:
        if file_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(file_fd)
        with contextlib.suppress(OSError):
            os.close(parent_fd)


async def job_delete(request):
    name = request.path_params["name"]
    if not _safe_name(name):
        return PlainTextResponse("Job not found", status_code=404)
    if _find_job(name) is None:
        return PlainTextResponse("Job not found", status_code=404)
    try:
        _remove_owned_tree(JOBS_DIR, name)
    except (FileNotFoundError, ValueError):
        return PlainTextResponse("Job not found", status_code=404)
    except OSError:
        log.warning("Could not safely delete job %s", name, exc_info=True)
        return PlainTextResponse("Deletion unavailable", status_code=503)
    return _redirect("/jobs?msg=Job+deleted+from+disk")


# ---------------------------------------------------------------------------
# Routes — runs
# ---------------------------------------------------------------------------


async def runs_list(request):
    name = request.query_params.get("name") or None
    status = request.query_params.get("status") or None
    page = _bounded_page(request.query_params.get("page"), _MAX_RUNS_PAGE)
    offset = (page - 1) * _RUNS_PAGE_SIZE
    filters = {"name": name, "status": status}
    try:
        fetched = await run_in_threadpool(
            runs.list_runs,
            name=name,
            status=status,
            limit=_RUNS_PAGE_SIZE + 1,
            offset=offset,
        )
    except (OSError, sqlite3.Error) as exc:
        log.warning("Could not load run history", exc_info=True)
        return _render(
            request,
            "runs.html",
            status_code=503,
            database_error=sqlite_store.database_error_kind(exc),
            runs=[],
            page=page,
            previous_url=None,
            next_url=None,
            active_status=status or "",
            active_name=name or "",
        )
    has_next = len(fetched) > _RUNS_PAGE_SIZE and page < _MAX_RUNS_PAGE
    rows = fetched[:_RUNS_PAGE_SIZE]
    return _render(
        request,
        "runs.html",
        runs=rows,
        page=page,
        previous_url=_page_url("/runs", page - 1, **filters) if page > 1 else None,
        next_url=_page_url("/runs", page + 1, **filters) if has_next else None,
        active_status=status or "",
        active_name=name or "",
        database_error=None,
    )


async def run_detail(request):
    run_id = request.path_params["id"]
    try:
        run = await run_in_threadpool(runs.get, run_id)
    except (OSError, sqlite3.Error) as exc:
        log.warning("Could not load run %s", run_id, exc_info=True)
        return _database_error_response(
            request,
            sqlite_store.database_error_kind(exc),
            back_url="/runs",
            back_label="Runs",
        )
    if run is None:
        return PlainTextResponse("Run not found", status_code=404)
    output = await run_in_threadpool(
        runs.read_output,
        run_id,
        max_bytes=_OUTPUT_VIEW_CAP,
    )
    total = run.get("output_bytes") or 0
    truncated = bool(total) and total > _OUTPUT_VIEW_CAP
    return _render(
        request,
        "run_detail.html",
        run=run,
        output=output,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Routes — skills
# ---------------------------------------------------------------------------


async def skills_list(request):
    show = request.query_params.get("show") or "all"
    if show not in ("all", "enso", "system"):
        show = "all"
    enso_skills, external_skills = _skill_inventory(request)
    counts = {
        "all": len(enso_skills) + len(external_skills),
        "enso": len(enso_skills),
        "system": len(external_skills),
    }
    return _render(
        request,
        "skills.html",
        enso_skills=enso_skills,
        external_skills=external_skills,
        active_show=show,
        counts=counts,
    )


async def skill_detail(request):
    name = request.path_params["name"]
    path, editable = _resolve_skill(request, name)
    if path is None:
        return PlainTextResponse("Skill not found", status_code=404)
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return PlainTextResponse("Skill not readable", status_code=404)
    return _render(
        request,
        "skill_detail.html",
        name=name,
        path=path,
        editable=editable,
        content=content,
        description=_skill_description(path),
    )


async def skill_edit(request):
    name = request.path_params["name"]
    path, editable = _resolve_skill(request, name)
    if path is None or not editable:
        return PlainTextResponse("Not editable", status_code=403)
    # Defence in depth: the resolved path must live under CONFIG_DIR/skills.
    if not is_within(_skills_base(), path):
        return PlainTextResponse("Forbidden", status_code=403)
    form = await request.form()
    content = (form.get("content") or "").replace("\r\n", "\n")
    atomic_write_text(path, content)
    return _redirect(f"/skills/{name}")


async def skill_delete(request):
    name = request.path_params["name"]
    if not _safe_name(name):
        return PlainTextResponse("Skill not found", status_code=404)
    path, editable = _resolve_skill(request, name)
    if path is None:
        return PlainTextResponse("Skill not found", status_code=404)
    if not editable:
        return PlainTextResponse("Not deletable", status_code=403)

    try:
        _remove_owned_tree(_skills_base(), name)
    except (FileNotFoundError, ValueError):
        return PlainTextResponse("Skill not found", status_code=404)
    except OSError:
        log.warning("Could not safely delete skill %s", name, exc_info=True)
        return PlainTextResponse("Deletion unavailable", status_code=503)
    return _redirect("/skills?msg=Skill+deleted+from+disk")


# ---------------------------------------------------------------------------
# Routes — docs
# ---------------------------------------------------------------------------
#
# Every handler is a thin caller of ``enso.docs``: it owns path validation
# (including symlink containment), listing, and the atomic writes. A rejected
# path is a refused request (403), a missing file is a 404.


async def docs_list(request):
    listing = docs.load_docs()
    return _render(
        request,
        "docs.html",
        groups=docs.group_docs(listing.docs),
        total=len(listing.docs),
        truncated=listing.truncated,
        max_docs=docs.MAX_DOCS,
    )


async def doc_new(request):
    return _render(request, "doc_new.html", path="", name="", error="")


async def doc_create(request):
    form = await request.form()
    path = form.get("path") or ""
    name = form.get("name") or ""
    try:
        doc = docs.create_doc(path, name)
    except (FileExistsError, ValueError) as exc:
        return _render(request, "doc_new.html", path=path, name=name, error=str(exc))
    except OSError:
        log.warning("Could not create doc %s", path, exc_info=True)
        return PlainTextResponse("Doc unavailable", status_code=503)
    return _redirect(f"/docs/{doc.rel_path}")


async def doc_detail(request):
    rel = request.path_params["path"]
    try:
        doc = docs.load_doc(rel)
        content = docs.read_doc(rel)
    except docs.DocPathError:
        return PlainTextResponse("Forbidden", status_code=403)
    except FileNotFoundError:
        return PlainTextResponse("Doc not found", status_code=404)
    except (OSError, UnicodeError):
        return PlainTextResponse("Doc not readable", status_code=404)
    return _render(
        request,
        "doc_detail.html",
        doc=doc,
        content=content,
        breadcrumb=docs.parent_titles(doc.rel_path),
    )


async def doc_edit(request):
    form = await request.form()
    path = form.get("path") or ""
    try:
        rel = docs.write_doc(path, form.get("content") or "")
    except docs.DocPathError:
        return PlainTextResponse("Forbidden", status_code=403)
    except FileNotFoundError:
        return PlainTextResponse("Doc not found", status_code=404)
    except OSError:
        log.warning("Could not edit doc %s", path, exc_info=True)
        return PlainTextResponse("Doc unavailable", status_code=503)
    return _redirect(f"/docs/{rel}")


async def doc_delete(request):
    form = await request.form()
    path = form.get("path") or ""
    try:
        docs.delete_doc(path)
    except docs.DocPathError:
        return PlainTextResponse("Forbidden", status_code=403)
    except FileNotFoundError:
        return PlainTextResponse("Doc not found", status_code=404)
    except OSError:
        log.warning("Could not delete doc %s", path, exc_info=True)
        return PlainTextResponse("Doc unavailable", status_code=503)
    return _redirect("/docs?msg=Doc+deleted+from+disk")


# ---------------------------------------------------------------------------
# Routes — registered data tables (read-only)
# ---------------------------------------------------------------------------


def _database_error_response(
    request,
    database_error: sqlite_store.DatabaseErrorKind,
    *,
    back_url: str,
    back_label: str,
) -> Response:
    return _render(
        request,
        "database_error.html",
        status_code=503,
        database_error=database_error,
        back_url=back_url,
        back_label=back_label,
    )


def _tables_unavailable(
    request,
    database_error: sqlite_store.DatabaseErrorKind,
) -> Response:
    return _render(
        request,
        "tables.html",
        status_code=503,
        database_available=False,
        database_error=database_error,
    )


async def tables_list(request):
    try:
        listing = await run_in_threadpool(tables.list_tables)
    except (OSError, sqlite3.Error) as exc:
        log.warning("Could not list registered data tables", exc_info=True)
        return _tables_unavailable(request, sqlite_store.database_error_kind(exc))
    return _render(
        request,
        "tables.html",
        database_available=True,
        database_error=None,
        tables=listing.tables,
        truncated=listing.truncated,
        max_tables=tables.MAX_TABLES,
    )


async def table_detail(request):
    table_name = request.path_params["name"]
    page = _bounded_page(request.query_params.get("page"), _MAX_TABLE_PAGE)
    offset = (page - 1) * _TABLE_PAGE_SIZE
    try:
        preview = await run_in_threadpool(
            tables.preview_table,
            table_name,
            offset=offset,
            limit=_TABLE_PAGE_SIZE,
        )
    except (tables.TableNameError, tables.TableNotFoundError):
        return PlainTextResponse("Table not found", status_code=404)
    except (OSError, sqlite3.Error) as exc:
        log.warning("Could not preview data table %s", table_name, exc_info=True)
        return _tables_unavailable(request, sqlite_store.database_error_kind(exc))
    rendered_indexes = [
        {
            "sql": index.sql[:_MAX_TABLE_INDEX_SQL_CHARS],
            "sql_truncated": len(index.sql) > _MAX_TABLE_INDEX_SQL_CHARS,
        }
        for index in preview.table.indexes[:_MAX_TABLE_INDEXES]
    ]
    preview_limit_reached = preview.has_next and page >= _MAX_TABLE_PAGE
    return _render(
        request,
        "table_detail.html",
        preview=preview,
        page=page,
        previous_page=page - 1 if preview.has_previous else None,
        next_page=page + 1 if preview.has_next and page < _MAX_TABLE_PAGE else None,
        preview_limit_reached=preview_limit_reached,
        schema_columns=preview.table.columns[: tables.MAX_COLUMNS],
        table_sql=preview.table.sql[:_MAX_TABLE_SCHEMA_SQL_CHARS],
        table_sql_truncated=len(preview.table.sql) > _MAX_TABLE_SCHEMA_SQL_CHARS,
        table_indexes=rendered_indexes,
        table_indexes_truncated=len(preview.table.indexes) > _MAX_TABLE_INDEXES,
        hidden_index_count=max(len(preview.table.indexes) - _MAX_TABLE_INDEXES, 0),
    )


def _bounded_page(value: object, maximum: int) -> int:
    """Clamp an untrusted ``?page=`` value into ``1..maximum``."""
    try:
        page = int(str(value)) if value is not None else 1
    except (TypeError, ValueError):
        return 1
    return min(max(page, 1), maximum)


# ---------------------------------------------------------------------------
# Routes — execution configuration
# ---------------------------------------------------------------------------


def _agent_error_response(error: AgentFileError) -> Response:
    if isinstance(error, (UnsafeAgentPath, AgentIntegrityError)):
        status_code = 403
    elif isinstance(error, AgentNotFound):
        status_code = 404
    elif isinstance(error, AgentConflict):
        status_code = 409
    elif isinstance(error, AgentTooLarge):
        status_code = 413
    elif isinstance(error, AgentEncodingError):
        status_code = 422
    else:
        status_code = 503
        if not isinstance(error, AgentFilesystemError):
            log.warning("Unexpected instruction file failure", exc_info=True)
    return PlainTextResponse(str(error), status_code=status_code)


def _child_agent_listing(listing: AgentListing) -> AgentListing:
    """Remove the root file and its diagnostics from the nested-file list."""
    return AgentListing(
        files=tuple(entry for entry in listing.files if entry.rel_path != AGENT_FILENAME),
        truncated=listing.truncated,
        errors=tuple(error for error in listing.errors if error.rel_path != AGENT_FILENAME),
    )


async def _workspace_agent_inventory(workspace_view, workspace):
    """Safely enrich one workspace view and prepare its detail-page documents."""
    root_editable = workspace_view.usable
    empty_listing = AgentListing(files=(), truncated=False, errors=())
    if not workspace_view.usable:
        return (
            with_workspace_agents(
                workspace_view,
                agent_files=(),
                truncated=False,
                root_editable=False,
            ),
            empty_listing,
            None,
            "Instructions are unavailable until the workspace binding is valid.",
        )
    try:
        listing = await run_in_threadpool(discover_agents, workspace.path)
    except AgentFileError as exc:
        problem = f"Workspace instructions could not be inspected: {exc}"
        return (
            with_workspace_agents(
                workspace_view,
                agent_files=(),
                truncated=False,
                root_editable=False,
                problem=problem,
            ),
            empty_listing,
            None,
            problem,
        )

    root_document = None
    root_problem = None
    try:
        root_document = await run_in_threadpool(
            read_agent,
            workspace.path,
            AGENT_FILENAME,
            _MAX_WORKSPACE_INSTRUCTION_BYTES,
        )
    except AgentNotFound:
        pass
    except AgentFileError as exc:
        root_editable = False
        root_problem = str(exc)

    enriched = with_workspace_agents(
        workspace_view,
        agent_files=[entry.rel_path for entry in listing.files],
        truncated=listing.truncated,
        root_editable=root_editable,
        problem=root_problem,
    )
    return enriched, _child_agent_listing(listing), root_document, root_problem


async def workspaces_list(request):
    configuration = _configuration_view(request)
    catalog = load_catalog(_active_config(request))
    workspaces = []
    for workspace_view in configuration.workspaces:
        workspace = catalog.workspaces.get(workspace_view.name)
        if workspace is None:
            workspaces.append(workspace_view)
            continue
        enriched, _, _, _ = await _workspace_agent_inventory(workspace_view, workspace)
        workspaces.append(enriched)
    return _render(
        request,
        "workspaces.html",
        configuration=configuration,
        workspaces=tuple(workspaces),
        catalog_errors=configuration.catalog_errors,
    )


async def workspace_detail(request):
    name = request.path_params["name"]
    configuration = _configuration_view(request)
    workspace_view = configuration.workspace(name)
    catalog = load_catalog(_active_config(request))
    workspace = catalog.workspaces.get(name)
    if workspace_view is None or workspace is None:
        return PlainTextResponse("Workspace not found", status_code=404)
    workspace_view, agent_listing, root_document, root_problem = await _workspace_agent_inventory(
        workspace_view, workspace
    )
    return _render(
        request,
        "workspace_detail.html",
        configuration=configuration,
        workspace=workspace_view,
        catalog_errors=configuration.catalog_errors,
        agent_listing=agent_listing,
        root_document=root_document,
        root_revision=root_document.revision if root_document is not None else "",
        root_problem=root_problem,
    )


async def workspace_agent_view(request):
    name = request.path_params["name"]
    rel_path = request.path_params["path"]
    if rel_path == AGENT_FILENAME:
        return PlainTextResponse("Instruction file not found", status_code=404)
    config = _active_config(request)
    catalog = load_catalog(config)
    if name not in catalog.workspaces:
        return PlainTextResponse("Workspace not found", status_code=404)
    if not catalog.usable(name):
        return PlainTextResponse("Workspace unavailable", status_code=403)
    workspace = catalog.workspaces[name]
    try:
        document = await run_in_threadpool(
            read_agent,
            workspace.path,
            rel_path,
            _MAX_WORKSPACE_INSTRUCTION_BYTES,
        )
    except AgentFileError as exc:
        return _agent_error_response(exc)
    configuration = _configuration_view(request)
    workspace_view = configuration.workspace(name)
    assert workspace_view is not None
    return _render(
        request,
        "workspace_agents.html",
        configuration=configuration,
        workspace=workspace_view,
        agent_document=document,
    )


async def workspace_agents_edit(request):
    name = request.path_params["name"]
    config = _active_config(request)
    catalog = load_catalog(config)
    workspace = catalog.workspaces.get(name)
    if workspace is None:
        return PlainTextResponse("Workspace not found", status_code=404)
    if not catalog.usable(name):
        return PlainTextResponse("Forbidden", status_code=403)
    form = await request.form()
    content = form.get("content")
    if not isinstance(content, str):
        return PlainTextResponse("Instruction content must be text", status_code=422)
    raw_revision = form.get("revision")
    revision = raw_revision if isinstance(raw_revision, str) and raw_revision else None
    try:
        await run_in_threadpool(
            write_agent,
            workspace.path,
            AGENT_FILENAME,
            content,
            revision,
            _MAX_WORKSPACE_INSTRUCTION_BYTES,
            True,
        )
    except AgentFileError as exc:
        return _agent_error_response(exc)
    return _redirect(f"/workspaces/{name}?msg=Workspace+instructions+saved")


async def policies_list(request):
    configuration = _configuration_view(request)
    return _render(
        request,
        "policies.html",
        configuration=configuration,
        policies=configuration.policies,
        catalog_errors=configuration.catalog_errors,
    )


async def policy_detail(request):
    name = request.path_params["name"]
    config = _active_config(request)
    configuration = _configuration_view(request)
    policy_view = configuration.policy(name)
    if policy_view is None:
        return PlainTextResponse("Policy not found", status_code=404)

    catalog = load_catalog(config)
    execution_policy = catalog.policies.get(name)
    checks = []
    if execution_policy is not None and name not in catalog.policy_errors:
        for workspace_name in policy_view.workspace_names:
            workspace = catalog.workspaces.get(workspace_name)
            if workspace is None or not catalog.usable(workspace_name):
                problems = [*catalog.errors]
                problems.extend(catalog.workspace_errors.get(workspace_name, ()))
                problems.extend(catalog.policy_errors.get(name, ()))
                if not problems:
                    problems.append("workspace binding is not usable")
                for provider in execution_policy.providers:
                    checks.append(
                        build_policy_check_view(
                            workspace_name,
                            PolicyCheck(
                                provider=provider,
                                ok=False,
                                problems=tuple(dict.fromkeys(problems)),
                            ),
                        )
                    )
                continue
            for provider in execution_policy.providers:
                check = await run_in_threadpool(
                    check_provider,
                    workspace,
                    execution_policy,
                    provider,
                )
                checks.append(build_policy_check_view(workspace_name, check))
    policy_view = with_policy_checks(policy_view, checks)
    return _render(
        request,
        "policy_detail.html",
        configuration=configuration,
        policy=policy_view,
        catalog_errors=configuration.catalog_errors,
    )


async def slack_routes(request):
    configuration = _configuration_view(request)
    return _render(
        request,
        "slack_routes.html",
        configuration=configuration,
        slack=configuration.slack,
        routes=configuration.slack.routes,
        catalog_errors=configuration.catalog_errors,
    )


# ---------------------------------------------------------------------------
# Routes — AGENTS.md
# ---------------------------------------------------------------------------


def _agents_path() -> str:
    return os.path.join(CONFIG_DIR, "AGENTS.md")


async def agents_view(request):
    path = _agents_path()
    try:
        document = await run_in_threadpool(
            read_agent,
            CONFIG_DIR,
            AGENT_FILENAME,
            MAX_SHARED_INSTRUCTION_BYTES,
        )
    except AgentNotFound:
        content = ""
        revision = None
        editable = True
        problem = "Shared instruction file is missing; provider launches fail until it is created."
    except AgentFileError as exc:
        content = ""
        revision = None
        editable = False
        problem = str(exc)
    else:
        content = document.content
        revision = document.revision
        editable = True
        problem = None
    return _render(
        request,
        "agents.html",
        path=path,
        content=content,
        revision=revision,
        editable=editable,
        problem=problem,
    )


async def agents_edit(request):
    form = await request.form()
    content = form.get("content")
    if not isinstance(content, str):
        return PlainTextResponse("Instruction content must be text", status_code=422)
    raw_revision = form.get("revision")
    revision = raw_revision if isinstance(raw_revision, str) and raw_revision else None
    try:
        await run_in_threadpool(
            write_agent,
            CONFIG_DIR,
            AGENT_FILENAME,
            content,
            revision,
            MAX_SHARED_INSTRUCTION_BYTES,
            True,
        )
    except AgentFileError as exc:
        return _agent_error_response(exc)
    return _redirect("/agents")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


async def health(request):
    return PlainTextResponse("ok")


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Block framing and keep token-bearing HTML out of browser caches."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response


class HostGuardMiddleware(BaseHTTPMiddleware):
    """Reject arbitrary Host headers so DNS rebinding cannot read local pages."""

    def __init__(self, app, allowed_hosts: frozenset[str]):
        super().__init__(app)
        self.allowed_hosts = allowed_hosts

    async def dispatch(self, request, call_next):
        host = _normalize_host(request.headers.get("host"))
        if not host or host not in self.allowed_hosts:
            return PlainTextResponse("Invalid host header", status_code=400)
        return await call_next(request)


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Gate every request behind the resolved web token when one is configured.

    An empty token disables auth entirely, so non-loopback deployments need a
    trusted external access boundary. A matching ``?token=`` sets a cookie so
    subsequent navigation needs no query string. ``/health`` and ``/static``
    are token-exempt (the Host guard still applies).
    """

    def __init__(self, app, token: str):
        super().__init__(app)
        self.token = token or ""

    async def dispatch(self, request, call_next):
        if not self.token:
            return await call_next(request)
        path = request.url.path
        if path == "/health" or path.startswith("/static"):
            return await call_next(request)
        if request.cookies.get("enso_token") == self.token:
            return await call_next(request)
        if request.query_params.get("token") == self.token:
            response = await call_next(request)
            response.set_cookie("enso_token", self.token, httponly=True, samesite="lax")
            return response
        return PlainTextResponse("Unauthorized", status_code=401)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(runtime) -> Starlette:
    """Build the Starlette app, stashing ``runtime`` on ``app.state``."""
    cfg = getattr(runtime, "config", {}) or {}
    web_cfg = cfg.get("web", {}) if isinstance(cfg, dict) else {}
    if not isinstance(web_cfg, dict):
        web_cfg = {}
    token = resolve_config_secret(web_cfg, "token")
    allowed_hosts = _allowed_web_hosts(web_cfg)

    routes = [
        Route("/", dashboard),
        Route("/health", health),
        Route("/jobs", jobs_list),
        Route("/jobs/{name}", job_detail),
        Route("/jobs/{name}/toggle", _csrf_protected(job_toggle), methods=["POST"]),
        Route("/jobs/{name}/run", _csrf_protected(job_run), methods=["POST"]),
        Route(
            "/jobs/{name}/prompt",
            _csrf_protected(job_edit_prompt),
            methods=["POST"],
        ),
        Route(
            "/jobs/{name}/prerun",
            _csrf_protected(job_edit_prerun),
            methods=["POST"],
        ),
        Route(
            "/jobs/{name}/delete",
            _csrf_protected(job_delete),
            methods=["POST"],
        ),
        Route("/runs", runs_list),
        Route("/runs/{id}", run_detail),
        Route("/skills", skills_list),
        Route("/skills/{name}", skill_detail),
        Route("/skills/{name}/edit", _csrf_protected(skill_edit), methods=["POST"]),
        Route(
            "/skills/{name}/delete",
            _csrf_protected(skill_delete),
            methods=["POST"],
        ),
        Route("/docs", docs_list),
        # Split GET/POST on /docs/new keeps the CSRF wrapper (which awaits the
        # form) off the GET; the catch-all stays last so it never shadows a
        # mutation route.
        Route("/docs/new", doc_new),
        Route("/docs/new", _csrf_protected(doc_create), methods=["POST"]),
        Route("/docs/edit", _csrf_protected(doc_edit), methods=["POST"]),
        Route("/docs/delete", _csrf_protected(doc_delete), methods=["POST"]),
        Route("/docs/{path:path}", doc_detail),
        Route("/tables", tables_list),
        Route("/tables/{name}", table_detail),
        Route("/workspaces", workspaces_list),
        Route(
            "/workspaces/{name}/agents/edit",
            _csrf_protected(workspace_agents_edit),
            methods=["POST"],
        ),
        Route("/workspaces/{name}/agents/{path:path}", workspace_agent_view),
        Route("/workspaces/{name}", workspace_detail),
        Route("/policies", policies_list),
        Route("/policies/{name}", policy_detail),
        Route("/slack", slack_routes),
        Route("/agents", agents_view),
        Route("/agents/edit", _csrf_protected(agents_edit), methods=["POST"]),
        Mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static"),
    ]

    middleware = [
        Middleware(SecurityHeadersMiddleware),
        Middleware(HostGuardMiddleware, allowed_hosts=allowed_hosts),
        Middleware(TokenAuthMiddleware, token=token),
    ]
    app = Starlette(routes=routes, middleware=middleware)
    app.state.runtime = runtime
    app.state.csrf_token = secrets.token_urlsafe(32)
    return app
