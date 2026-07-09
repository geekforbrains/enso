"""Starlette web UI for Enso.

Exposes ``create_app(runtime) -> Starlette``. The runtime is stashed on
``app.state.runtime`` and every handler reads configuration via
``runtime.config`` and the working directory via ``runtime.working_dir``.

Data comes from the file/DB-backed modules (``enso.tasks``, ``enso.jobs``,
``enso.runs``, ``enso.frontmatter``); this module only renders and mutates —
it never owns any storage of its own. All file writes that target skills or
AGENTS.md are path-guarded so a crafted name can never escape the allowed
directory.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import (
    FileResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from .. import frontmatter, runs
from ..config import CONFIG_DIR
from ..jobs import Job, load_jobs
from ..tasks import (
    STATUSES,
    add_attachment,
    create_task,
    delete_attachment,
    delete_task,
    get_task,
    load_tasks,
    save_task,
    set_status,
)

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
TASK_BADGES = {
    "todo": "bg-gray-100 text-gray-700 dark:bg-neutral-700 dark:text-neutral-300",
    "in_progress": "bg-indigo-100 text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-300",
    "blocked": "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
    "done": "bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300",
    "cancelled": (
        "bg-gray-100 text-gray-500 line-through "
        "dark:bg-neutral-700 dark:text-neutral-400"
    ),
}
RUN_BADGES = {
    "running": (
        "bg-indigo-100 text-indigo-800 animate-pulse "
        "dark:bg-indigo-900/40 dark:text-indigo-300"
    ),
    "ok": "bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300",
    "error": "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300",
    "timeout": "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
}
templates.env.globals["task_badges"] = TASK_BADGES
templates.env.globals["run_badges"] = RUN_BADGES
templates.env.globals["statuses"] = STATUSES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(request, template: str, **ctx) -> Response:
    """Render a template with the request bound (Jinja2Templates convention)."""
    ctx["current_path"] = request.url.path
    ctx["flash"] = request.query_params.get("msg")
    return templates.TemplateResponse(request, template, ctx)


def _is_hx(request) -> bool:
    """True when the request came from HTMX (wants a fragment, not a redirect)."""
    return request.headers.get("HX-Request") == "true"


def _redirect(url: str) -> RedirectResponse:
    """303 redirect (so a POST turns into a GET)."""
    return RedirectResponse(url, status_code=303)


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
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def _providers(request) -> dict:
    """Return the configured providers mapping (name -> cfg)."""
    runtime = request.app.state.runtime
    cfg = getattr(runtime, "config", {}) or {}
    providers = cfg.get("providers", {})
    return providers if isinstance(providers, dict) else {}


def _all_models(providers: dict) -> list[str]:
    """Flat sorted list of every model across providers (for datalists)."""
    seen: list[str] = []
    for pcfg in providers.values():
        for m in (pcfg or {}).get("models", []) or []:
            if m not in seen:
                seen.append(m)
    return seen


def _find_job(name: str) -> Job | None:
    """Return the job whose ``dir_name`` matches ``name``."""
    return next((j for j in load_jobs() if j.dir_name == name), None)


def _safe_name(name: str) -> bool:
    """True when ``name`` is a bare path segment (no traversal, no separators)."""
    return bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name


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


def _external_skills(request) -> list[dict]:
    out: list[dict] = []
    for root in _external_skill_roots(request):
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            skill_md = os.path.join(root, name, "SKILL.md")
            if os.path.isfile(skill_md):
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
    tasks = load_tasks()
    counts = Counter(t.status for t in tasks)
    status_counts = [(s, counts.get(s, 0)) for s in STATUSES]
    jobs = load_jobs()
    jobs_enabled = sum(1 for j in jobs if j.enabled)
    latest = runs.list_runs(limit=10)
    return _render(
        request,
        "index.html",
        total_tasks=len(tasks),
        status_counts=status_counts,
        jobs_enabled=jobs_enabled,
        jobs_total=len(jobs),
        latest_runs=latest,
    )


# ---------------------------------------------------------------------------
# Routes — tasks
# ---------------------------------------------------------------------------


async def tasks_list(request):
    status = request.query_params.get("status") or ""
    tag = request.query_params.get("tag") or ""
    tasks = load_tasks()
    all_tags = sorted({t for task in tasks for t in task.tags})
    if status:
        tasks = [t for t in tasks if t.status == status]
    if tag:
        tasks = [t for t in tasks if tag in t.tags]
    return _render(
        request,
        "tasks.html",
        tasks=tasks,
        all_tags=all_tags,
        active_status=status,
        active_tag=tag,
    )


async def task_new_form(request):
    providers = _providers(request)
    return _render(
        request,
        "task_new.html",
        providers=providers,
        models=_all_models(providers),
    )


async def task_new_submit(request):
    form = await request.form()
    title = (form.get("title") or "").strip()
    if not title:
        return _redirect("/tasks/new?msg=Title+is+required")
    description = (form.get("description") or "").replace("\r\n", "\n")
    tags = [t.strip() for t in (form.get("tags") or "").split(",") if t.strip()]
    notify = form.get("notify") is not None
    provider = (form.get("provider") or "").strip() or None
    model = (form.get("model") or "").strip() or None
    task = create_task(
        title=title,
        description=description,
        tags=tags,
        notify=notify,
        provider=provider,
        model=model,
    )
    upload = form.get("file")
    if upload is not None and getattr(upload, "filename", ""):
        data = await upload.read()
        if data:
            add_attachment(task.slug, upload.filename, data)
    return _redirect(f"/tasks/{task.slug}")


async def task_detail(request):
    slug = request.path_params["slug"]
    task = get_task(slug)
    if task is None:
        return PlainTextResponse("Task not found", status_code=404)
    providers = _providers(request)
    task_runs = runs.list_runs(kind="task", name=slug, limit=50)
    return _render(
        request,
        "task_detail.html",
        task=task,
        attachments=task.attachment_names(),
        task_runs=task_runs,
        providers=providers,
        models=_all_models(providers),
    )


async def task_edit(request):
    slug = request.path_params["slug"]
    task = get_task(slug)
    if task is None:
        return PlainTextResponse("Task not found", status_code=404)
    form = await request.form()
    title = (form.get("title") or "").strip()
    if title:
        task.title = title
    task.description = (form.get("description") or "").replace("\r\n", "\n")
    task.tags = [t.strip() for t in (form.get("tags") or "").split(",") if t.strip()]
    task.notify = form.get("notify") is not None
    task.provider = (form.get("provider") or "").strip() or None
    task.model = (form.get("model") or "").strip() or None
    save_task(task)
    return _redirect(f"/tasks/{slug}")


async def task_status(request):
    slug = request.path_params["slug"]
    form = await request.form()
    status = (form.get("status") or "").strip()
    reason = (form.get("reason") or "").strip() or None
    if status not in STATUSES:
        return _redirect(f"/tasks/{slug}?msg=Invalid+status")
    try:
        set_status(slug, status, reason)
    except (FileNotFoundError, ValueError) as exc:
        return _redirect(f"/tasks/{slug}?msg={exc}")
    if _is_hx(request):
        task = get_task(slug)
        return templates.TemplateResponse(request, "_task_status.html", {"task": task})
    return _redirect(f"/tasks/{slug}")


async def task_attachment_upload(request):
    slug = request.path_params["slug"]
    if get_task(slug) is None:
        return PlainTextResponse("Task not found", status_code=404)
    form = await request.form()
    upload = form.get("file")
    if upload is not None and getattr(upload, "filename", ""):
        data = await upload.read()
        if data:
            add_attachment(slug, upload.filename, data)
    return _redirect(f"/tasks/{slug}")


async def task_attachment_download(request):
    slug = request.path_params["slug"]
    name = request.path_params["name"]
    task = get_task(slug)
    if task is None or not _safe_name(name):
        return PlainTextResponse("Not found", status_code=404)
    path = os.path.join(task.attachments_dir, os.path.basename(name))
    if not os.path.isfile(path) or not _within(task.attachments_dir, path):
        return PlainTextResponse("Not found", status_code=404)
    return FileResponse(path, filename=os.path.basename(name))


async def task_attachment_delete(request):
    slug = request.path_params["slug"]
    name = request.path_params["name"]
    task = get_task(slug)
    if task is None:
        return PlainTextResponse("Task not found", status_code=404)
    if _safe_name(name):
        delete_attachment(slug, name)
    if _is_hx(request):
        task = get_task(slug)
        return templates.TemplateResponse(
            request,
            "_attachments.html",
            {"task": task, "attachments": task.attachment_names()},
        )
    return _redirect(f"/tasks/{slug}")


async def task_run(request):
    slug = request.path_params["slug"]
    runtime = request.app.state.runtime
    if runtime is None or not hasattr(runtime, "run_task_now"):
        return _redirect(f"/tasks/{slug}?msg=Run+now+is+unavailable")
    try:
        run_id = await runtime.run_task_now(slug)
    except Exception as exc:
        log.warning("run_task_now failed for %s", slug, exc_info=True)
        return _redirect(f"/tasks/{slug}?msg=Run+failed:+{exc}")
    if run_id:
        return _redirect(f"/runs/{run_id}")
    return _redirect(f"/tasks/{slug}")


async def task_delete(request):
    slug = request.path_params["slug"]
    delete_task(slug)
    return _redirect("/tasks")


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
    prerun_exists = bool(job.prerun) and os.path.isfile(
        os.path.join(job.job_dir, job.prerun or "")
    )
    job_runs = runs.list_runs(kind="job", name=name, limit=50)
    return _render(
        request,
        "job_detail.html",
        job=job,
        meta=meta,
        prerun_exists=prerun_exists,
        job_runs=job_runs,
    )


async def job_toggle(request):
    name = request.path_params["name"]
    job = _find_job(name)
    if job is None:
        return PlainTextResponse("Job not found", status_code=404)
    meta, body = frontmatter.read(job.path)
    meta["enabled"] = not job.enabled
    frontmatter.write(job.path, meta, body)
    if _is_hx(request):
        fresh = _find_job(name)
        return templates.TemplateResponse(request, "_job_toggle.html", {"job": fresh})
    return _redirect(f"/jobs/{name}")


async def job_run(request):
    name = request.path_params["name"]
    runtime = request.app.state.runtime
    if runtime is None or not hasattr(runtime, "run_job_now"):
        return _redirect(f"/jobs/{name}?msg=Run+now+is+unavailable")
    try:
        run_id = await runtime.run_job_now(name)
    except Exception as exc:
        log.warning("run_job_now failed for %s", name, exc_info=True)
        return _redirect(f"/jobs/{name}?msg=Run+failed:+{exc}")
    if run_id:
        return _redirect(f"/runs/{run_id}")
    return _redirect(f"/jobs/{name}")


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
    enso_skills = _enso_skills()
    external_skills = _external_skills(request)
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


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Gate every request behind ``web.token`` when one is configured.

    An empty token disables auth entirely (localhost trust). A matching
    ``?token=`` sets a cookie so subsequent navigation needs no query string.
    ``/health`` and ``/static`` are always open.
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
    token = web_cfg.get("token", "") if isinstance(web_cfg, dict) else ""

    routes = [
        Route("/", dashboard),
        Route("/health", health),
        Route("/tasks", tasks_list),
        Route("/tasks/new", task_new_form),
        Route("/tasks/new", task_new_submit, methods=["POST"]),
        Route("/tasks/{slug}", task_detail),
        Route("/tasks/{slug}/edit", task_edit, methods=["POST"]),
        Route("/tasks/{slug}/status", task_status, methods=["POST"]),
        Route(
            "/tasks/{slug}/attachments",
            task_attachment_upload,
            methods=["POST"],
        ),
        Route(
            "/tasks/{slug}/attachments/{name}",
            task_attachment_download,
        ),
        Route(
            "/tasks/{slug}/attachments/{name}/delete",
            task_attachment_delete,
            methods=["POST"],
        ),
        Route("/tasks/{slug}/run", task_run, methods=["POST"]),
        Route("/tasks/{slug}/delete", task_delete, methods=["POST"]),
        Route("/jobs", jobs_list),
        Route("/jobs/{name}", job_detail),
        Route("/jobs/{name}/toggle", job_toggle, methods=["POST"]),
        Route("/jobs/{name}/run", job_run, methods=["POST"]),
        Route("/runs", runs_list),
        Route("/runs/{id}", run_detail),
        Route("/skills", skills_list),
        Route("/skills/{name}", skill_detail),
        Route("/skills/{name}/edit", skill_edit, methods=["POST"]),
        Route("/agents", agents_view),
        Route("/agents/edit", agents_edit, methods=["POST"]),
        Mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static"),
    ]

    middleware = [Middleware(TokenAuthMiddleware, token=token)]
    app = Starlette(routes=routes, middleware=middleware)
    app.state.runtime = runtime
    return app
