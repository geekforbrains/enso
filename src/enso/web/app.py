"""Starlette web UI for Enso.

Exposes ``create_app(runtime) -> Starlette``. The runtime is stashed on
``app.state.runtime`` and every handler reads configuration via
``runtime.config`` and the working directory via ``runtime.working_dir``.

Data comes from the file/DB-backed modules (``enso.jobs``, ``enso.runs``,
``enso.frontmatter``); this module only renders and mutates — it never owns
any storage of its own. All file writes that target skills, jobs, or
AGENTS.md are path-guarded so a crafted name can never escape the allowed
directory.
"""

from __future__ import annotations

import contextlib
import errno
import functools
import hashlib
import importlib.resources
import logging
import os
import secrets
import shutil
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from starlette.applications import Starlette
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

from .. import frontmatter, runs
from ..config import CONFIG_DIR, JOBS_DIR, SKILL_TOMBSTONES_DIRNAME
from ..jobs import Job, load_jobs

log = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_TEMPLATES_DIR = _HERE / "templates"
_STATIC_DIR = _HERE / "static"

# Cap the run output we inline into a page so a giant transcript can't OOM the
# renderer; the row's ``output_bytes`` still reports the true size.
_OUTPUT_VIEW_CAP = 200_000


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
        total = int(ms)
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
        n = float(size)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


_DOW_NAMES = {
    "0": "Sunday", "1": "Monday", "2": "Tuesday", "3": "Wednesday",
    "4": "Thursday", "5": "Friday", "6": "Saturday", "7": "Sunday",
}
_DOW_ABBR = {
    "0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed",
    "4": "Thu", "5": "Fri", "6": "Sat", "7": "Sun",
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
    "running": (
        "bg-indigo-100 text-indigo-800 animate-pulse "
        "dark:bg-indigo-900/40 dark:text-indigo-300"
    ),
    "ok": "bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300",
    "error": "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300",
    "timeout": "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
    "prerun_error": "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300",
    "prerun_timeout": (
        "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300"
    ),
}
templates.env.globals["run_badges"] = RUN_BADGES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(request, template: str, **ctx) -> Response:
    """Render a template with the request bound (Jinja2Templates convention)."""
    ctx["current_path"] = request.url.path
    ctx["flash"] = request.query_params.get("msg")
    ctx["csrf_token"] = request.app.state.csrf_token
    return templates.TemplateResponse(request, template, ctx)


def _is_hx(request) -> bool:
    """True when the request came from HTMX (wants a fragment, not a redirect)."""
    return request.headers.get("HX-Request") == "true"


def _redirect(url: str) -> RedirectResponse:
    """303 redirect (so a POST turns into a GET)."""
    return RedirectResponse(url, status_code=303)


def _csrf_protected(handler):
    """Require the process-scoped CSRF token before a write handler runs."""

    @functools.wraps(handler)
    async def protected(request):
        form = await request.form()
        supplied = request.headers.get("X-CSRF-Token") or form.get("_csrf")
        expected = request.app.state.csrf_token
        if not isinstance(supplied, str) or not secrets.compare_digest(
            supplied, expected
        ):
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
            host
            for value in configured
            if (host := _normalize_host(value)) and host != "*"
        )
    return frozenset(allowed)


def _within(base: str, target: str) -> bool:
    """True when ``target`` resolves to ``base`` or a path beneath it."""
    base_r = os.path.realpath(base)
    tgt_r = os.path.realpath(target)
    return tgt_r == base_r or tgt_r.startswith(base_r + os.sep)


def _atomic_write_text(path: str, text: str) -> None:
    """Atomically write UTF-8 text: temp file in the same dir, fsync, os.replace."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        stream = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def _find_job(name: str) -> Job | None:
    """Return the job whose ``dir_name`` matches ``name``."""
    return next((j for j in load_jobs() if j.dir_name == name), None)


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
        if (
            not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
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
            mode = os.stat(
                detached, dir_fd=base_fd, follow_symlinks=False
            ).st_mode
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


def _is_bundled_skill(name: str) -> bool:
    bundled = importlib.resources.files("enso").joinpath("skills", name)
    return bundled.is_dir()


def _create_skill_tombstone(name: str) -> None:
    """Create a bundle-deletion marker without following directory symlinks."""
    if not _safe_name(name) or name == SKILL_TOMBSTONES_DIRNAME:
        raise ValueError("Unsafe skill name")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError("Secure tombstone creation is unavailable")

    skills_fd = os.open(
        _skills_base(), os.O_RDONLY | nofollow | directory
    )
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir(SKILL_TOMBSTONES_DIRNAME, mode=0o700, dir_fd=skills_fd)
        tombstones_fd = os.open(
            SKILL_TOMBSTONES_DIRNAME,
            os.O_RDONLY | nofollow | directory,
            dir_fd=skills_fd,
        )
        try:
            marker = f"{name}.deleted"
            try:
                marker_fd = os.open(
                    marker,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
                    0o600,
                    dir_fd=tombstones_fd,
                )
            except FileExistsError:
                return
            try:
                os.fsync(marker_fd)
            finally:
                os.close(marker_fd)
            os.fsync(tombstones_fd)
        finally:
            os.close(tombstones_fd)
    finally:
        os.close(skills_fd)


def _regular_file_sha256(path: str) -> str | None:
    if os.path.islink(path) or not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _skill_tool_cleanup_candidates(request, name: str) -> list[tuple[str, str]]:
    """Find unmodified installed tools owned only by the skill being deleted."""
    runtime = request.app.state.runtime
    working_dir = getattr(runtime, "working_dir", None)
    if not isinstance(working_dir, str) or not working_dir:
        return []
    tools_dir = os.path.join(working_dir, "tools")
    if os.path.islink(tools_dir) or not os.path.isdir(tools_dir):
        return []

    skill_dir = os.path.join(_skills_base(), name)
    try:
        filenames = os.listdir(skill_dir)
        other_skills = [
            entry
            for entry in os.listdir(_skills_base())
            if entry not in {name, SKILL_TOMBSTONES_DIRNAME}
        ]
    except OSError:
        return []

    candidates: list[tuple[str, str]] = []
    for filename in filenames:
        if not filename.endswith(".py"):
            continue
        source = os.path.join(skill_dir, filename)
        source_hash = _regular_file_sha256(source)
        if source_hash is None:
            continue
        if any(
            os.path.isfile(os.path.join(_skills_base(), other, filename))
            for other in other_skills
        ):
            continue
        installed = os.path.join(tools_dir, filename)
        if _regular_file_sha256(installed) == source_hash:
            candidates.append((installed, source_hash))
    return candidates


def _remove_installed_skill_tools(candidates: list[tuple[str, str]]) -> None:
    for path, expected_hash in candidates:
        if _regular_file_sha256(path) != expected_hash:
            continue
        with contextlib.suppress(OSError):
            os.remove(path)


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
            if name == SKILL_TOMBSTONES_DIRNAME:
                continue
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
        set(owned_names)
        if owned_names is not None
        else {skill["name"] for skill in _enso_skills()}
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
    if not _safe_name(name) or name == SKILL_TOMBSTONES_DIRNAME:
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
    jobs = load_jobs()
    jobs_enabled = sum(1 for j in jobs if j.enabled)
    enso_skills, system_skills = _skill_inventory(request)
    latest = runs.list_runs(limit=6)
    return _render(
        request,
        "index.html",
        jobs_enabled=jobs_enabled,
        jobs_total=len(jobs),
        skills_total=len(enso_skills) + len(system_skills),
        skills_enso=len(enso_skills),
        skills_system=len(system_skills),
        latest_runs=latest,
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
            prerun_error = (
                "Configured script not found. Create it on disk before editing it here."
            )
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
    job_runs = runs.list_runs(kind="job", name=name, limit=50)
    return _render(
        request,
        "job_detail.html",
        job=job,
        meta=meta,
        prerun_exists=prerun_exists,
        prerun_content=prerun_content,
        prerun_error=prerun_error,
        job_runs=job_runs,
    )


async def job_toggle(request):
    name = request.path_params["name"]
    job = _find_job(name)
    if job is None:
        return PlainTextResponse("Job not found", status_code=404)
    # Defence in depth: a JOB.md symlink must not escape the jobs directory.
    if not _within(JOBS_DIR, job.path):
        return PlainTextResponse("Forbidden", status_code=403)
    # Change only the scalar. Re-serializing the whole block would erase
    # comments and would corrupt legacy jobs whose YAML-like values contain an
    # unquoted colon (which the job loader intentionally still accepts).
    frontmatter.write_scalar(job.path, "enabled", str(not job.enabled).lower())
    if _is_hx(request):
        fresh = _find_job(name)
        return templates.TemplateResponse(
            request,
            "_job_toggle.html",
            {"job": fresh, "csrf_token": request.app.state.csrf_token},
        )
    return _redirect(f"/jobs/{name}")


async def job_run(request):
    name = request.path_params["name"]
    runtime = request.app.state.runtime
    if runtime is None or not hasattr(runtime, "run_job_now"):
        return _redirect(f"/jobs/{name}?msg=Run+now+is+unavailable")
    try:
        result = await runtime.run_job_now(name)
    except Exception as exc:
        log.warning("run_job_now failed for %s", name, exc_info=True)
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
    if not _within(JOBS_DIR, job.path):
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
    kind = request.query_params.get("kind") or None
    name = request.query_params.get("name") or None
    status = request.query_params.get("status") or None
    rows = runs.list_runs(kind=kind, name=name, status=status, limit=200)
    return _render(
        request,
        "runs.html",
        runs=rows,
        active_kind=kind or "",
        active_status=status or "",
        active_name=name or "",
    )


async def run_detail(request):
    run_id = request.path_params["id"]
    run = runs.get(run_id)
    if run is None:
        return PlainTextResponse("Run not found", status_code=404)
    output = runs.read_output(run_id, max_bytes=_OUTPUT_VIEW_CAP)
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
    if not _within(_skills_base(), path):
        return PlainTextResponse("Forbidden", status_code=403)
    form = await request.form()
    content = (form.get("content") or "").replace("\r\n", "\n")
    _atomic_write_text(path, content)
    return _redirect(f"/skills/{name}")


async def skill_delete(request):
    name = request.path_params["name"]
    if not _safe_name(name) or name == SKILL_TOMBSTONES_DIRNAME:
        return PlainTextResponse("Skill not found", status_code=404)
    path, editable = _resolve_skill(request, name)
    if path is None:
        return PlainTextResponse("Skill not found", status_code=404)
    if not editable:
        return PlainTextResponse("Not deletable", status_code=403)

    if _is_bundled_skill(name):
        try:
            _create_skill_tombstone(name)
        except (OSError, ValueError):
            log.warning("Could not safely tombstone skill %s", name, exc_info=True)
            return PlainTextResponse("Forbidden", status_code=403)
    tool_candidates = _skill_tool_cleanup_candidates(request, name)
    try:
        _remove_owned_tree(_skills_base(), name)
    except (FileNotFoundError, ValueError):
        return PlainTextResponse("Skill not found", status_code=404)
    except OSError:
        log.warning("Could not safely delete skill %s", name, exc_info=True)
        return PlainTextResponse("Deletion unavailable", status_code=503)
    _remove_installed_skill_tools(tool_candidates)
    return _redirect("/skills?msg=Skill+deleted+from+disk")


# ---------------------------------------------------------------------------
# Routes — AGENTS.md
# ---------------------------------------------------------------------------


def _agents_path(request) -> str:
    runtime = request.app.state.runtime
    return os.path.join(runtime.working_dir, "AGENTS.md")


async def agents_view(request):
    path = _agents_path(request)
    content = ""
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except OSError:
            content = ""
    return _render(request, "agents.html", path=path, content=content)


async def agents_edit(request):
    path = _agents_path(request)
    form = await request.form()
    content = (form.get("content") or "").replace("\r\n", "\n")
    # Write the symlink target directly; the CLAUDE.md -> AGENTS.md symlink is
    # left untouched (os.replace onto the resolved regular file).
    _atomic_write_text(path, content)
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
    """Gate every request behind ``web.token`` when one is configured.

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
            response.set_cookie(
                "enso_token", self.token, httponly=True, samesite="lax"
            )
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
    token = web_cfg.get("token", "")
    allowed_hosts = _allowed_web_hosts(web_cfg)

    routes = [
        Route("/", dashboard),
        Route("/health", health),
        Route("/jobs", jobs_list),
        Route("/jobs/{name}", job_detail),
        Route(
            "/jobs/{name}/toggle", _csrf_protected(job_toggle), methods=["POST"]
        ),
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
