"""The web application: explicit routes, shared write protection, templates and process.

Browsing is read-only. Registered form actions require a same-origin form token and
participate in home maintenance admission; every response has shared security headers.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import secrets as tokens
import signal
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime
from importlib import resources
from typing import Any
from urllib.parse import quote, urlsplit

import jinja2
from aiohttp import web

from .. import __version__, maintenance, secrets
from ..config import Paths, check_config
from . import Bind, PidFile, WebError, filters, knowledge, views
from . import tasks as taskviews

log = logging.getLogger("enso.web")

CSP = (
    "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
    "img-src 'self' data:"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
# Everything under ``static/``: the stylesheet and script every page loads, and the files a
# phone reads once to put the viewer on its home screen as an app.
STATIC_TYPES = {
    "app.css": "text/css",
    "app.js": "text/javascript",
    "manifest.webmanifest": "application/manifest+json",
    "apple-touch-icon.png": "image/png",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
}
SHUTDOWN_TIMEOUT = 5.0
NAV = (
    ("Today", "/today"),
    ("Tasks", "/tasks"),
    ("Heartbeats", "/heartbeats"),
    ("Runs", "/runs"),
    ("Knowledge", "/knowledge"),
    ("Jobs", "/jobs"),
    ("Workspaces", "/workspaces"),
    ("Secrets", "/secrets"),
    ("Health", "/health"),
)
# The phone has four primary links and More; the sidebar exposes every destination.
NAV_PRIMARY = NAV[:4]
NAV_MORE = NAV[4:]
# Skills are resolved per workspace, so they belong to the Workspaces tab.
NAV_ALIASES = {"/skills": "/workspaces", "/": "/today"}
LOG_FORMAT = "%(asctime)s %(levelname)-5s %(name)s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

Asset = tuple[bytes, str, str]  # body, ETag, content type
PATHS = web.AppKey[Paths]("paths")
BIND = web.AppKey[Bind | None]("bind")
ENV = web.AppKey[jinja2.Environment]("env")
ASSETS = web.AppKey[dict[str, Asset]]("assets")
CSRF = web.AppKey[str]("csrf")
HOSTS = web.AppKey[frozenset[str]]("hosts")

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


# -- Resources ------------------------------------------------------------------


def _package_text(*parts: str) -> str | None:
    """A file under ``enso/web`` via ``importlib.resources``, the same in every install."""
    try:
        return resources.files("enso.web").joinpath(*parts).read_text("utf-8")
    except FileNotFoundError, OSError:
        return None


def static_asset(name: str) -> bytes:
    """The bytes of ``static/<name>``; raises ``FileNotFoundError`` for anything unknown."""
    if name not in STATIC_TYPES:
        raise FileNotFoundError(name)
    return resources.files("enso.web").joinpath("static", name).read_bytes()


def environment() -> jinja2.Environment:
    """Jinja2 over the packaged templates, autoescaping everything a page interpolates."""
    env = jinja2.Environment(
        loader=jinja2.FunctionLoader(lambda name: _package_text("templates", name)),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(filters.FILTERS)
    env.globals.update(
        nav=NAV,
        nav_primary=NAV_PRIMARY,
        nav_more=NAV_MORE,
        bar_series=filters.bar_series,
        spark_tones=filters.spark_tones,
        version=__version__,
    )
    return env


# -- Rendering ------------------------------------------------------------------


def _active(path: str) -> str:
    """Which top-level tab a path belongs to; sub-sections stay under their parent."""
    head = path.split("/", 2)[1] if path.startswith("/") else ""
    top = f"/{head}" if head else "/"
    return NAV_ALIASES.get(top, top)


def render(
    request: web.Request, template: str, context: dict[str, Any], *, status: int = 200
) -> web.Response:
    env = request.app[ENV]
    html = env.get_template(template).render(
        **{"now": datetime.now().astimezone(), "alarm": False, **context},
        request_path=request.path,
        active=_active(request.path),
        csrf=request.app[CSRF],
    )
    return web.Response(text=html, content_type="text/html", charset="utf-8", status=status)


def error_response(request: web.Request, status: int, title: str, detail: str) -> web.Response:
    """The shared error page; plain text if even that cannot render."""
    context = {"config_problems": [], "status": status, "title": title, "detail": detail}
    try:
        return render(request, "error.html", context, status=status)
    except Exception:  # the page must answer even when templates are broken
        log.exception("could not render the error page")
        return web.Response(text=f"{status} {title}\n{detail}\n", status=status)


def not_found(request: web.Request, detail: str) -> web.Response:
    return error_response(request, 404, "Not found", detail)


def _secure(headers: Any) -> None:
    for key, value in SECURITY_HEADERS.items():
        headers[key] = value
    if "Cache-Control" not in headers:
        headers["Cache-Control"] = "no-store"


def _known_host(request: web.Request) -> bool:
    """Whether the Host header names this viewer, so a rebound DNS name is never an origin.

    An attacker's domain re-pointed at this listener would otherwise be same-origin with
    every page, form token included. An address literal cannot be rebound.
    """
    try:
        name = urlsplit(f"//{request.host}").hostname or ""
    except ValueError:
        return False
    if name == "localhost" or name in request.app[HOSTS]:
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


async def _check_write(request: web.Request) -> None:
    """Every registered write is a form action with an unguessable, same-origin token."""
    origin = request.headers.get("Origin")
    site = request.headers.get("Sec-Fetch-Site")
    if site == "cross-site":
        raise web.HTTPForbidden(reason="Cross-site writes are not allowed")
    # Safari sends an opaque origin under Referrer-Policy: no-referrer. Only accept
    # that case with its same-origin browser signal; the form token is still required.
    if origin is not None and not (origin == "null" and site == "same-origin"):
        try:
            source = urlsplit(origin)
        except ValueError:
            raise web.HTTPForbidden(reason="Invalid origin") from None
        if source.scheme not in ("http", "https") or source.netloc.lower() != request.host.lower():
            raise web.HTTPForbidden(reason="Cross-site writes are not allowed")
    if request.content_type != "application/x-www-form-urlencoded":
        raise web.HTTPUnsupportedMediaType(reason="Expected a form submission")
    form = await request.post()
    supplied = form.getall("_csrf", [])
    if (
        len(supplied) != 1
        or not isinstance(supplied[0], str)
        or not tokens.compare_digest(supplied[0].encode(), request.app[CSRF].encode())
    ):
        raise web.HTTPForbidden(reason="Refresh the page and submit the form again")


@web.middleware
async def guard(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Route methods explicitly; protect writes and stamp all responses, including errors."""
    try:
        if not _known_host(request):
            log.warning("refused a request for unknown host %r", request.host)
            raise web.HTTPMisdirectedRequest(reason="Unknown host")  # before routing or tokens
        if request.match_info.http_exception is not None:
            raise request.match_info.http_exception
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            await _check_write(request)
        response = await handler(request)
    except web.HTTPException as exc:
        if exc.status < 400:
            _secure(exc.headers)
            raise
        response = error_response(
            request,
            exc.status,
            "Not found" if exc.status == 404 else exc.reason,
            "Add this host name to web.hosts in config.json, then restart the viewer."
            if exc.status == 421
            else "The request could not be completed.",
        )
        if "Allow" in exc.headers:
            response.headers["Allow"] = exc.headers["Allow"]
    except maintenance.UpdateError:
        response = error_response(request, 503, "Enso is updating", "Try again after it finishes.")
    except Exception:
        log.exception("%s failed", request.path)
        response = error_response(
            request,
            500,
            "Something went wrong",
            f"The request failed; see {request.app[PATHS].web_log}.",
        )
    _secure(response.headers)
    return response


# -- Handlers -------------------------------------------------------------------


async def _model[T](work: Callable[[], T]) -> T:
    """Build a page model off the event loop; the reads are bounded but synchronous."""
    return await asyncio.to_thread(work)


async def _write_model[T](paths: Paths, work: Callable[[], T]) -> T:
    """The worker holds admission until the write finishes, even if its HTTP caller leaves."""

    def apply() -> T:
        access = maintenance.acquire_access(paths) if maintenance.coordinated(paths) else None
        try:
            if maintenance.paused(paths):
                raise maintenance.UpdateError("Enso is updating")
            return work()
        finally:
            if access is not None:
                os.close(access)

    return await _model(apply)


async def index(request: web.Request) -> web.StreamResponse:
    raise web.HTTPFound("/today")


async def today(request: web.Request) -> web.StreamResponse:
    paths = request.app[PATHS]
    section = request.match_info.get("section", "schedule")
    if section not in views.TODAY_SECTIONS:
        return not_found(request, "No such section.")
    requested_range = request.query.get("range")
    chart_back_hours = next(
        (hours for hours in views.CHART_RANGES if str(hours) == requested_range),
        views.CHART_BACK_HOURS,
    )
    model = await _model(lambda: views.today_model(paths, section, chart_back_hours))
    return render(request, "today.html", model)


async def static(request: web.Request) -> web.StreamResponse:
    asset = request.app[ASSETS].get(request.match_info["name"])
    if asset is None:
        return not_found(request, "No such asset.")
    body, etag, content_type = asset
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("If-None-Match") == etag:
        return web.Response(status=304, headers=headers)
    charset = "utf-8" if content_type.startswith("text/") else None
    return web.Response(body=body, content_type=content_type, charset=charset, headers=headers)


async def health(request: web.Request) -> web.StreamResponse:
    paths, bind = request.app[PATHS], request.app[BIND]
    section = request.match_info.get("section", "doctor")
    if section not in views.HEALTH_SECTIONS:
        return not_found(request, "No such section.")
    model = await _model(lambda: views.health_model(paths, bind, section))
    return render(request, "health.html", model)


async def workspaces(request: web.Request) -> web.StreamResponse:
    paths = request.app[PATHS]
    model = await _model(lambda: views.workspaces_model(paths))
    return render(request, "workspaces.html", model)


async def workspace(request: web.Request) -> web.StreamResponse:
    paths, name = request.app[PATHS], request.match_info["name"]
    model = await _model(lambda: views.workspace_model(paths, name))
    if model is None:
        return not_found(request, "No such workspace.")
    return render(request, "workspace.html", model)


async def workspace_files(request: web.Request) -> web.StreamResponse:
    paths = request.app[PATHS]
    name, root = request.match_info["name"], request.match_info["root"]
    relative = request.match_info.get("path", "")
    raw = request.query.get("raw") == "1"
    model = await _model(lambda: views.files_model(paths, name, root, relative, raw=raw))
    if model is None:
        return not_found(request, "No such file or directory under this workspace.")
    return render(request, "files.html", model)


async def knowledge_list(request: web.Request) -> web.StreamResponse:
    query = {
        key: request.query.get(key, "")
        for key in ("scope", "folder", "view", "q", "across", "page")
    }
    model = await _model(lambda: knowledge.listing_model(request.app[PATHS], query))
    if model is None:
        return not_found(request, "No such knowledge scope or folder.")
    return render(request, "knowledge.html", model)


async def knowledge_note(request: web.Request) -> web.StreamResponse:
    model = await _model(
        lambda: knowledge.note_model(
            request.app[PATHS],
            note_id=request.match_info.get("id", ""),
            scope=request.query.get("scope", ""),
            path=request.query.get("path", ""),
            raw=request.query.get("raw") == "1",
        )
    )
    if model is None:
        return not_found(request, "No unique knowledge note matches this location.")
    return render(request, "knowledge_note.html", model)


async def knowledge_asset(request: web.Request) -> web.StreamResponse:
    asset = await _model(
        lambda: knowledge.read_asset(
            request.app[PATHS],
            request.query.get("scope", ""),
            request.query.get("path", ""),
        )
    )
    if asset is None:
        return not_found(
            request, "No readable attachment under this knowledge root (20 MiB limit)."
        )
    body, content_type, name = asset
    headers = {}
    if content_type == "application/octet-stream":
        headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(name, safe="")
    return web.Response(body=body, content_type=content_type, headers=headers)


async def skills(request: web.Request) -> web.StreamResponse:
    paths = request.app[PATHS]
    selected = request.query.get("workspace") or None
    model = await _model(lambda: views.skills_model(paths, selected))
    if model is None:
        return not_found(request, "No such workspace.")
    return render(request, "skills.html", model)


async def skill(request: web.Request) -> web.StreamResponse:
    paths, name = request.app[PATHS], request.match_info["name"]
    selected = request.query.get("workspace") or None
    model = await _model(lambda: views.skill_model(paths, name, selected))
    if model is None:
        return not_found(request, "No such skill.")
    return render(request, "skill.html", model)


async def jobs(request: web.Request) -> web.StreamResponse:
    paths = request.app[PATHS]
    model = await _model(lambda: views.jobs_model(paths))
    return render(request, "jobs.html", model)


async def job(request: web.Request) -> web.StreamResponse:
    paths, name = request.app[PATHS], request.match_info["name"]
    section = request.match_info.get("section", "overview")
    if section not in views.JOB_SECTIONS:
        return not_found(request, "No such section.")
    model = await _model(lambda: views.job_model(paths, name, section))
    if model is None:
        return not_found(request, "No such job.")
    return render(request, "job.html", model)


async def runs(request: web.Request) -> web.StreamResponse:
    paths = request.app[PATHS]
    query = {key: request.query.get(key, "") for key in ("view", "source", "job", "status", "page")}
    model = await _model(lambda: views.runs_model(paths, query))
    return render(request, "runs.html", model)


async def run(request: web.Request) -> web.StreamResponse:
    paths, run_id = request.app[PATHS], request.match_info["id"]
    model = await _model(lambda: views.run_model(paths, run_id))
    if model is None:
        return not_found(request, "No run matches that id.")
    return render(request, "run.html", model)


async def tasks(request: web.Request) -> web.StreamResponse:
    paths = request.app[PATHS]
    query = {key: request.query.get(key, "") for key in ("workspace", "project", "stage", "q")}
    model = await _model(lambda: taskviews.tasks_model(paths, query))
    return render(request, "tasks.html", model)


async def task(request: web.Request) -> web.StreamResponse:
    paths, ref = request.app[PATHS], request.match_info["ref"]
    model = await _model(lambda: taskviews.task_model(paths, ref))
    if model is None:
        return not_found(request, "No task has that reference.")
    return render(request, "task.html", model)


async def heartbeats(request: web.Request) -> web.StreamResponse:
    paths = request.app[PATHS]
    query = {key: request.query.get(key, "") for key in ("view", "state", "attention", "page")}
    model = await _model(lambda: views.heartbeats_model(paths, query))
    return render(request, "heartbeats.html", model)


async def heartbeat(request: web.Request) -> web.StreamResponse:
    paths, ref = request.app[PATHS], request.match_info["ref"]
    section = request.match_info.get("section", "overview")
    if section not in views.HEARTBEAT_SECTIONS:
        return not_found(request, "No such section.")
    query = {key: request.query.get(key, "") for key in ("page",)}
    model = await _model(lambda: views.heartbeat_model(paths, ref, section, query))
    if model is None:
        return not_found(request, "No beat has that reference.")
    return render(request, "heartbeat.html", model)


async def heartbeat_run(request: web.Request) -> web.StreamResponse:
    paths, run_id = request.app[PATHS], request.match_info["run_id"]
    model = await _model(lambda: views.heartbeat_run_model(paths, run_id))
    if model is None:
        return not_found(request, "No heartbeat run matches that id.")
    return render(request, "heartbeat_run.html", model)


async def secret_list(
    request: web.Request,
    *,
    view: str | None = None,
    name: str = "",
    error: str = "",
    status: int = 200,
) -> web.StreamResponse:
    view = "saved" if (view or request.query.get("view")) == "saved" else "add"
    try:
        names = await _write_model(request.app[PATHS], lambda: secrets.names(request.app[PATHS]))
    except secrets.SecretError as exc:
        names, error, status = [], str(exc), 400
    notice = {"added": "Secret added.", "deleted": "Secret deleted."}.get(
        request.query.get("notice", ""), ""
    )
    return render(
        request,
        "secrets.html",
        {
            "names": names,
            "view": view,
            "name": name,
            "notice": notice if not error else "",
            "error": error,
            "config_problems": [],
        },
        status=status,
    )


async def secret_add(request: web.Request) -> web.StreamResponse:
    form = await request.post()
    name = ""
    try:
        if set(form) != {"name", "value", "_csrf"} or any(len(form.getall(k)) != 1 for k in form):
            raise secrets.SecretError("supply one name and one value")
        submitted, value = form["name"], form["value"]
        if not isinstance(submitted, str) or not isinstance(value, str):
            raise secrets.SecretError("supply a text name and value")
        name = submitted
        # HTML form submission rewrites every textarea line break as CRLF, so the original
        # ending is unknowable here. Store LF; the CLI's --stdin keeps exact bytes.
        text = value.replace("\r\n", "\n")
        await _write_model(request.app[PATHS], lambda: secrets.add(request.app[PATHS], name, text))
    except secrets.SecretError as exc:
        return await secret_list(request, view="add", name=name, error=str(exc), status=400)
    raise web.HTTPSeeOther("/secrets?notice=added")


async def secret_delete(request: web.Request) -> web.StreamResponse:
    try:
        await _write_model(
            request.app[PATHS],
            lambda: secrets.delete(request.app[PATHS], request.match_info["name"]),
        )
    except secrets.SecretError as exc:
        return await secret_list(request, view="saved", error=str(exc), status=400)
    raise web.HTTPSeeOther("/secrets?view=saved&notice=deleted")


ROUTES: tuple[tuple[str, str, Handler], ...] = (
    ("GET", "/", index),
    ("GET", "/static/{name}", static),
    ("GET", "/today", today),
    ("GET", "/today/{section}", today),
    ("GET", "/health", health),
    ("GET", "/health/{section}", health),
    ("GET", "/workspaces", workspaces),
    ("GET", "/workspaces/{name}", workspace),
    ("GET", "/workspaces/{name}/files/{root}", workspace_files),
    ("GET", "/workspaces/{name}/files/{root}/{path:.*}", workspace_files),
    ("GET", "/knowledge", knowledge_list),
    ("GET", "/knowledge/notes/{id}", knowledge_note),
    ("GET", "/knowledge/file", knowledge_note),
    ("GET", "/knowledge/asset", knowledge_asset),
    ("GET", "/skills", skills),
    ("GET", "/skills/{name}", skill),
    ("GET", "/jobs", jobs),
    ("GET", "/jobs/{name}", job),
    ("GET", "/jobs/{name}/{section}", job),
    ("GET", "/runs", runs),
    ("GET", "/runs/{id}", run),
    ("GET", "/tasks", tasks),
    ("GET", "/tasks/{ref}", task),
    ("GET", "/heartbeats", heartbeats),
    ("GET", "/heartbeats/runs/{run_id}", heartbeat_run),
    ("GET", "/heartbeats/{ref}", heartbeat),
    ("GET", "/heartbeats/{ref}/{section}", heartbeat),
    ("GET", "/secrets", secret_list),
    ("POST", "/secrets", secret_add),
    ("POST", "/secrets/{name}/delete", secret_delete),
)


def create_app(paths: Paths, bind: Bind | None = None) -> web.Application:
    """The viewer over ``paths``; ``bind`` only tells the Health page where it listens."""
    app = web.Application(middlewares=[guard], client_max_size=256 * 1024)
    app[CSRF] = tokens.token_urlsafe(32)
    app[PATHS] = paths
    app[BIND] = bind
    config, _problems, _warnings = check_config(paths)
    names = set(config.web.hosts) if config is not None else set()
    if bind is not None:
        names.add(bind.host.lower())
    app[HOSTS] = frozenset(names)
    app[ENV] = environment()
    assets: dict[str, Asset] = {}
    for name, content_type in STATIC_TYPES.items():
        body = static_asset(name)
        assets[name] = (body, f'"{hashlib.sha256(body).hexdigest()[:16]}"', content_type)
    app[ASSETS] = assets
    for method, path, handler in ROUTES:
        app.router.add_route(method, path, handler)
    return app


# -- The process ----------------------------------------------------------------


def serve(paths: Paths, bind: Bind) -> int:
    """Run the viewer until SIGINT/SIGTERM; the exit status for ``python -m enso.web``."""
    logging.basicConfig(
        level=logging.INFO, format=LOG_FORMAT, datefmt=DATE_FORMAT, stream=sys.stderr, force=True
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    pidfile = PidFile(paths.web_pid)
    try:
        pidfile.acquire()
    except WebError as exc:
        log.error("%s", exc)
        return 1
    try:
        return asyncio.run(_serve(paths, bind, pidfile))
    finally:
        pidfile.release()


async def _serve(paths: Paths, bind: Bind, pidfile: PidFile) -> int:
    runner = web.AppRunner(create_app(paths, bind), handle_signals=False, access_log=None)
    await runner.setup()
    site = web.TCPSite(
        runner, bind.host, bind.port, shutdown_timeout=SHUTDOWN_TIMEOUT, reuse_address=True
    )
    try:
        await site.start()
    except OSError as exc:
        log.error("cannot bind %s:%d: %s", bind.host, bind.port, exc)
        await runner.cleanup()
        return 1
    pidfile.write(os.getpid(), bind)
    if not bind.loopback:
        log.warning(
            "listening on %s, which other machines may reach; the viewer has no authentication",
            bind.host,
        )
    log.info("enso %s web viewer listening on %s (pid %d)", __version__, bind.url, os.getpid())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        await stop.wait()
    finally:
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)
        log.info("stopping")
        await runner.cleanup()
        log.info("stopped")
    return 0
