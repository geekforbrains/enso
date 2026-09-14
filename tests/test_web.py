"""The web viewer's plumbing: config, the read-only database, the process, and the rules.

Page content is ``test_web_pages.py``. Everything here runs against a scratch home; the
process tests spawn the real ``python -m enso.web`` on a free port and always stop it.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import textwrap
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import load_job, write_config, write_job
from typer.testing import CliRunner

from enso import db, runs, tasks, web
from enso.cli import app
from enso.config import Config, Paths, WebConfig, parse_config
from enso.web import server, views
from enso.web.server import CSP, create_app


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


@pytest.fixture
async def client(enso_home: Paths) -> AsyncIterator[TestClient]:
    async with TestClient(TestServer(create_app(enso_home, web.Bind("127.0.0.1", 8787)))) as c:
        yield c


# -- Config ---------------------------------------------------------------------


def test_web_config_defaults_values_and_problems(enso_home: Paths, raw_config: dict) -> None:
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is not None and config.web == WebConfig("127.0.0.1", 8787)

    raw_config["web"] = {"host": "0.0.0.0", "port": 9000}
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is not None and config.web == WebConfig("0.0.0.0", 9000)

    for bad, expected in (
        ({"port": 0}, "web.port must be an integer from 1 through 65535"),
        ({"port": 70000}, "web.port must be an integer from 1 through 65535"),
        ({"port": "8787"}, "web.port must be an integer from 1 through 65535"),
        ({"port": True}, "web.port must be an integer from 1 through 65535"),
        ({"host": ""}, "web.host must be a non-empty string"),
        ({"host": 7}, "web.host must be a non-empty string"),
    ):
        raw_config["web"] = bad
        config, problems, _ = parse_config(raw_config, enso_home)
        assert config is None and problems == [expected], bad
    raw_config["web"] = []
    assert parse_config(raw_config, enso_home)[1] == ["web must be an object"]


def test_bind_resolution_flags_config_defaults(enso_home: Paths, raw_config: dict) -> None:
    assert web.resolve_bind(enso_home) == (web.Bind("127.0.0.1", 8787), [
        f"{enso_home.config} is missing; run `enso setup` first"
    ])  # fmt: skip
    raw_config["web"] = {"host": "localhost", "port": 9000}
    write_config(enso_home, raw_config)
    assert web.resolve_bind(enso_home)[0] == web.Bind("localhost", 9000)
    assert web.resolve_bind(enso_home, port=9001)[0] == web.Bind("localhost", 9001)
    assert web.resolve_bind(enso_home, "0.0.0.0", 1)[0] == web.Bind("0.0.0.0", 1)

    enso_home.config.write_text("{not json")  # invalid config: defaults unless flags say
    bind, problems = web.resolve_bind(enso_home)
    assert bind == web.Bind("127.0.0.1", 8787) and problems and "could not read" in problems[0]
    assert web.resolve_bind(enso_home, port=9002)[0] == web.Bind("127.0.0.1", 9002)
    assert enso_home.web_pid == enso_home.home / "web.pid"
    assert enso_home.web_log == enso_home.home / "web.log"


def test_bind_urls_and_loopback() -> None:
    assert web.Bind("127.0.0.1", 8787).url == "http://127.0.0.1:8787"
    assert web.Bind("0.0.0.0", 80).url == "http://127.0.0.1:80"
    assert web.Bind("::", 80).url == "http://[::1]:80"
    assert web.Bind("::1", 80).url == "http://[::1]:80"
    assert web.Bind("example.internal", 80).url == "http://example.internal:80"
    assert web.Bind("127.0.0.1", 1).loopback and web.Bind("localhost", 1).loopback
    assert web.Bind("::1", 1).loopback
    assert not web.Bind("0.0.0.0", 1).loopback and not web.Bind("10.0.0.5", 1).loopback
    assert not web.Bind("example.internal", 1).loopback
    assert web.Bind("0.0.0.0", 5).connect_address() == ("127.0.0.1", 5)


# -- The read-only database -----------------------------------------------------


def test_reader_is_strictly_read_only(enso_home: Paths, config: Config) -> None:
    elsewhere = Paths(enso_home.home / "never")
    with pytest.raises(db.MissingDatabaseError), db.reader(elsewhere):
        pass
    assert not elsewhere.home.exists()  # never creates the home or the file
    assert not enso_home.db.exists()
    assert runs.list_summaries(enso_home) == [] and runs.count(enso_home) == 0
    assert runs.latest_summaries(enso_home) == {} and runs.job_names(enso_home) == []
    assert runs.get(enso_home, "abc") is None
    assert not enso_home.db.exists()

    db.migrate(enso_home)
    with db.reader(enso_home) as con:
        assert con.execute("PRAGMA query_only").fetchone()[0] == 1
        assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    with pytest.raises(db.UnreadableDatabaseError, match="readonly"), db.reader(enso_home) as con:
        con.execute("INSERT INTO job_state (job) VALUES ('x')")
    with pytest.raises(db.UnreadableDatabaseError, match="readonly"), db.reader(enso_home) as con:
        con.execute("PRAGMA user_version = 1")
    assert db.job_states(enso_home) == {}

    with db.transaction(enso_home) as con:  # a newer Enso wrote this file
        con.execute("PRAGMA user_version = 99")
    with (
        pytest.raises(db.UnreadableDatabaseError, match="schema version 99"),
        db.reader(enso_home),
    ):
        pass
    with pytest.raises(db.UnreadableDatabaseError):
        runs.count(enso_home)

    enso_home.db.write_text("this is not a database")
    with pytest.raises(db.UnreadableDatabaseError), db.reader(enso_home):
        pass


def test_run_summaries_never_carry_output(enso_home: Paths, config: Config) -> None:
    db.migrate(enso_home)
    write_job(enso_home)
    write_job(enso_home, "other")
    job = load_job(enso_home, config)
    other = load_job(enso_home, config, "other")
    first = runs.start(enso_home, job, "manual", effort="high")
    runs.finish(enso_home, first, status="error", exit_code=2, output="x" * 5000, error="e" * 500)
    second = runs.start(enso_home, job, "schedule", effort="high")
    runs.finish(enso_home, second, status="ok", exit_code=0, output="fine")
    third = runs.start(enso_home, other, "schedule", effort="high")

    summaries = runs.list_summaries(enso_home)
    assert [s.id for s in summaries] == [third, second, first]
    assert not hasattr(summaries[0], "output") and "output" not in summaries[0].as_dict()
    failed = summaries[2]
    assert failed.error_preview == "e" * runs.ERROR_PREVIEW and failed.has_output
    assert (failed.status, failed.exit_code, failed.trigger) == ("error", 2, "manual")
    assert not summaries[0].has_output and summaries[0].error_preview is None
    assert [s.id for s in runs.list_summaries(enso_home, job="nightly")] == [second, first]
    assert [s.id for s in runs.list_summaries(enso_home, status="ok")] == [second]
    assert [s.id for s in runs.list_summaries(enso_home, limit=1, offset=1)] == [second]
    assert runs.count(enso_home) == 3 and runs.count(enso_home, job="nightly") == 2
    assert runs.count(enso_home, job="nightly", status="error") == 1
    latest = runs.latest_summaries(enso_home)
    assert {name: s.id for name, s in latest.items()} == {"nightly": second, "other": third}
    assert runs.job_names(enso_home) == ["nightly", "other"]
    full = runs.get(enso_home, first[:6])
    assert full is not None and full.output == "x" * 5000 and full.error == "e" * 500
    assert runs.STATUSES[0] == "running" and runs.PAGE_SIZE == 500


async def test_run_detail_shows_attempts_and_escapes_feedback(
    client: TestClient, enso_home: Paths, config: Config
) -> None:
    db.migrate(enso_home)
    write_job(enso_home)
    run_id = runs.start(enso_home, load_job(enso_home, config), "manual", effort="high")
    runs.record_attempt(
        enso_home,
        run_id,
        number=1,
        status="ok",
        exit_code=0,
        output="First answer",
        error="",
        session_id="same-session",
        duration_ms=123,
        postrun_exit_code=10,
        postrun_output="Commit <script>unsafe()</script> changes.",
    )
    # Completed attempts remain inspectable while the next turn or check is still running.
    ongoing = await client.get(f"/runs/{run_id}")
    ongoing_text = await ongoing.text()
    assert ongoing.status == 200 and "Attempt 1" in ongoing_text
    assert "Follow-up feedback" in ongoing_text and "First answer" in ongoing_text
    assert "&lt;script&gt;unsafe()&lt;/script&gt;" in ongoing_text
    assert "<script>unsafe()</script>" not in ongoing_text
    runs.finish(
        enso_home,
        run_id,
        status="error",
        output="Final answer",
        error="Validation failed",
        postrun_error="Validation failed",
        session_id="same-session",
    )
    final = await client.get(f"/runs/{run_id}")
    final_text = await final.text()
    assert final.status == 200 and "Postrun error" in final_text
    assert "Validation failed" in final_text and "same-session" in final_text
    assert "including hooks" in final_text and "Provider time budget" in final_text
    summary_text = await (await client.get("/runs")).text()
    assert "First answer" not in summary_text and "unsafe()" not in summary_text


# -- Rules: GET only, headers, assets -----------------------------------------


@pytest.mark.parametrize("method", ["HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
@pytest.mark.parametrize("path", ["/health", "/runs", "/static/app.css", "/", "/nope"])
async def test_only_get_is_answered(client: TestClient, method: str, path: str) -> None:
    response = await client.request(method, path, allow_redirects=False)
    assert response.status == 405 and response.headers["Allow"] == "GET"
    assert response.headers["Content-Security-Policy"] == CSP
    if method != "HEAD":
        assert "Method not allowed" in await response.text()


async def test_security_headers_on_every_response(client: TestClient) -> None:
    for path, status in (("/", 302), ("/health", 200), ("/nope", 404), ("/static/app.js", 200)):
        response = await client.get(path, allow_redirects=False)
        assert response.status == status, path
        assert response.headers["Content-Security-Policy"] == CSP
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "Access-Control-Allow-Origin" not in response.headers
    root = await client.get("/", allow_redirects=False)
    assert root.headers["Location"] == "/today" and root.headers["Cache-Control"] == "no-store"
    page = await client.get("/health")
    assert page.headers["Cache-Control"] == "no-store"
    assert page.headers["Content-Type"] == "text/html; charset=utf-8"


async def test_static_assets_are_local_and_revalidated(client: TestClient) -> None:
    css = await client.get("/static/app.css")
    assert css.status == 200 and css.headers["Content-Type"].startswith("text/css")
    assert css.headers["Cache-Control"] == "no-cache" and css.headers["ETag"].startswith('"')
    again = await client.get("/static/app.css", headers={"If-None-Match": css.headers["ETag"]})
    assert again.status == 304 and again.headers["Content-Security-Policy"] == CSP
    js = await client.get("/static/app.js")
    assert js.status == 200 and js.headers["Content-Type"].startswith("text/javascript")
    body = await js.text()
    assert "innerHTML" not in body and "fetch(" not in body and "XMLHttpRequest" not in body
    assert (await client.get("/static/other.css")).status == 404
    assert (await client.get("/static/../server.py")).status in (403, 404)
    page = await (await client.get("/health")).text()
    assert 'href="/static/app.css"' in page and '<script src="/static/app.js" defer>' in page
    assert "http://" not in page.replace("http://127.0.0.1:8787", "")  # nothing remote
    assert "https://" not in page


async def test_home_screen_install_files(client: TestClient) -> None:
    """A phone reads the manifest and icons once to install the viewer as the Enso app."""
    manifest = await client.get("/static/manifest.webmanifest")
    assert manifest.status == 200
    assert manifest.headers["Content-Type"] == "application/manifest+json"
    assert manifest.headers["Cache-Control"] == "no-cache"
    data = json.loads(await manifest.text())
    assert data["name"] == "Enso" and data["short_name"] == "Enso"
    assert data["display"] == "standalone"
    assert data["start_url"] == "/today" and data["scope"] == "/"
    icons = [(icon["src"], icon["sizes"]) for icon in data["icons"]]
    assert icons == [("/static/icon-192.png", "192x192"), ("/static/icon-512.png", "512x512")]
    for src, sizes in [*icons, ("/static/apple-touch-icon.png", "180x180")]:
        response = await client.get(src)
        body = await response.read()
        assert response.status == 200 and response.headers["Content-Type"] == "image/png", src
        assert body.startswith(b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", body[16:24])
        assert f"{width}x{height}" == sizes, src
        # iOS composites a transparent icon over black; colour type 2 is opaque RGB.
        assert body[25] == 2, src
    page = await (await client.get("/health")).text()
    assert '<link rel="manifest" href="/static/manifest.webmanifest" crossorigin=' in page
    assert '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">' in page
    assert '<meta name="apple-mobile-web-app-title" content="Enso">' in page
    assert '<meta name="apple-mobile-web-app-capable" content="yes">' in page
    assert page.count('<meta name="theme-color"') == 2


def test_package_resources_load_the_same_way_everywhere() -> None:
    assert server.static_asset("app.css").startswith(b"/* Enso web viewer")
    assert b"DOMContentLoaded" in server.static_asset("app.js")
    with pytest.raises(FileNotFoundError):
        server.static_asset("../__init__.py")
    env = server.environment()
    assert env.autoescape is True
    assert (
        env.get_template("base.html")
        .render(nav=server.NAV, active="/health", config_problems=[], version="x")
        .startswith("<!doctype html>")
    )
    with pytest.raises(Exception):  # noqa: B017 - a missing template is a loader error
        env.get_template("nope.html")


async def test_failures_render_the_error_page_without_a_traceback(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(paths: Paths, bind: object) -> dict:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(views, "health_model", explode)
    response = await client.get("/health")
    body = await response.text()
    assert response.status == 500 and "Something went wrong" in body
    assert "Traceback" not in body and "secret detail" not in body and "web.log" in body
    assert response.headers["Content-Security-Policy"] == CSP
    missing = await client.get("/no/such/page")
    assert missing.status == 404 and "Not found" in await missing.text()


# -- The pidfile ----------------------------------------------------------------


def test_pidfile_lock_decides_running_versus_stale(enso_home: Paths) -> None:
    paths = enso_home
    assert web.status(paths) == web.Status(running=False)
    paths.home.mkdir(parents=True, exist_ok=True)
    paths.web_pid.write_text('{"pid": 4242, "host": "127.0.0.1", "port": 8787}\n')
    stale = web.status(paths)
    assert stale == web.Status(running=False, pid=4242, host="127.0.0.1", port=8787, stale=True)
    assert web.stop(paths) == "not running" and paths.web_pid.exists()  # never deleted

    pidfile = web.PidFile(paths.web_pid)
    pidfile.acquire()
    pidfile.write(os.getpid(), web.Bind("127.0.0.1", 9000))
    live = web.status(paths)
    assert live.running and not live.stale and live.pid == os.getpid()
    assert live.url == "http://127.0.0.1:9000"
    second = web.PidFile(paths.web_pid)
    with pytest.raises(web.WebError, match="already running"):
        second.acquire()
    pidfile.release()
    assert not paths.web_pid.exists() and web.status(paths) == web.Status(running=False)

    paths.web_pid.write_text("garbage")
    assert web.status(paths) == web.Status(running=False, stale=True)


# -- The process ----------------------------------------------------------------


def test_background_lifecycle(enso_home: Paths) -> None:
    port = free_port()
    started = web.start(enso_home, port=port)
    try:
        assert started.startswith(f"listening on http://127.0.0.1:{port} (pid ")
        status = web.status(enso_home)
        assert status.running and status.port == port and status.host == "127.0.0.1"
        assert json.loads(enso_home.web_pid.read_text())["pid"] == status.pid
        assert web.start(enso_home, port=port).startswith(f"already running pid {status.pid}")
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.sendall(b"GET /health HTTP/1.0\r\nHost: x\r\n\r\n")
            reply = b""
            while chunk := sock.recv(65536):
                reply += chunk
        assert reply.startswith(b"HTTP/1.0 200") and b"<title>Health" in reply
        assert b"Content-Security-Policy" in reply
        assert enso_home.db.exists() is False  # the viewer created nothing
        assert sorted(p.name for p in enso_home.home.iterdir()) == [
            "web.log", "web.pid", "workspaces",
        ]  # fmt: skip
    finally:
        stopped = web.stop(enso_home)
    assert stopped == f"stopped pid {status.pid}"
    assert not enso_home.web_pid.exists() and web.status(enso_home) == web.Status(running=False)
    assert web.stop(enso_home) == "not running"
    log = enso_home.web_log.read_text()
    assert "listening on" in log and "stopping" in log and log.rstrip().endswith("stopped")
    assert "GET /health" not in log  # no access logging


def test_stale_pidfile_is_overwritten_and_bind_failure_reported(enso_home: Paths) -> None:
    enso_home.home.mkdir(parents=True, exist_ok=True)
    enso_home.web_pid.write_text('{"pid": 4242, "host": "127.0.0.1", "port": 1}\n')
    port = free_port()
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        with pytest.raises(web.WebError, match="exited with status 1") as failure:
            web.start(enso_home, port=port)
        assert str(enso_home.web_log) in str(failure.value)
    assert f"cannot bind 127.0.0.1:{port}" in enso_home.web_log.read_text()
    assert web.status(enso_home) == web.Status(running=False)  # the child cleaned up

    started = web.start(enso_home, port=port)
    try:
        assert started.startswith("listening on")
        assert web.status(enso_home).pid != 4242
    finally:
        web.stop(enso_home)


def test_foreground_process_handles_sigterm_cleanly(enso_home: Paths) -> None:
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "enso.web", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert wait_for(lambda: web.status(enso_home).running)
        assert web.status(enso_home).pid == process.pid
        assert wait_for(lambda: web._reachable(web.Bind("127.0.0.1", port)))
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 0, output
    assert "listening on" in output and output.rstrip().endswith("stopped")
    assert not enso_home.web_pid.exists()


def test_invalid_config_still_starts_with_defaults(enso_home: Paths) -> None:
    enso_home.home.mkdir(parents=True, exist_ok=True)
    enso_home.config.write_text("{not json")
    port = free_port()
    started = web.start(enso_home, port=port)  # the flag wins, the bad config is ignored
    try:
        assert started.startswith("listening on")
    finally:
        web.stop(enso_home)


def test_non_loopback_bind_is_logged(enso_home: Paths) -> None:
    port = free_port()
    started = web.start(enso_home, host="0.0.0.0", port=port)
    try:
        assert started.startswith(f"listening on http://127.0.0.1:{port}")
    finally:
        web.stop(enso_home)
    assert "no authentication" in enso_home.web_log.read_text()


# -- The commands ---------------------------------------------------------------


def test_web_commands(enso_home: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    absent = runner.invoke(app, ["web", "status"])
    assert absent.exit_code == 1 and absent.stdout.strip() == "not running"
    stopped = runner.invoke(app, ["web", "stop"])
    assert stopped.exit_code == 0 and stopped.stdout.strip() == "not running"

    enso_home.home.mkdir(parents=True, exist_ok=True)
    enso_home.web_pid.write_text('{"pid": 4242, "host": "127.0.0.1", "port": 1}\n')
    stale = runner.invoke(app, ["web", "status"])
    assert (
        stale.exit_code == 1 and stale.stdout.strip() == "not running (stale web.pid from pid 4242)"
    )

    assert runner.invoke(app, ["web", "start", "--port", "0"]).exit_code == 1
    assert runner.invoke(app, ["web", "start", "--host", " "]).exit_code == 1

    executed: list[list[str]] = []

    def fake_execv(path: str, argv: list[str]) -> None:
        executed.append(argv)
        raise SystemExit(0)

    monkeypatch.setattr(os, "execv", fake_execv)
    foreground = runner.invoke(app, ["web", "start", "--foreground", "--port", "9123"])
    assert foreground.exit_code == 0, foreground.output
    assert executed == [[sys.executable, "-m", "enso.web", "--host", "127.0.0.1", "--port", "9123"]]
    assert "config.json is unusable" in foreground.stderr  # warned, then started anyway

    port = free_port()
    started = runner.invoke(app, ["web", "start", "--port", str(port), "--host", "0.0.0.0"])
    try:
        assert started.exit_code == 0, started.output
        assert "reachable from other machines" in started.stderr
        assert started.stdout.startswith(f"listening on http://127.0.0.1:{port}")
        running = runner.invoke(app, ["web", "status"])
        assert running.exit_code == 0 and running.stdout.startswith("running pid=")
        assert f"at http://127.0.0.1:{port}" in running.stdout
        again = runner.invoke(app, ["web", "start", "--port", str(port)])
        assert again.exit_code == 0 and again.stdout.startswith("already running pid")
    finally:
        stopped = runner.invoke(app, ["web", "stop"])
    assert stopped.exit_code == 0 and stopped.stdout.startswith("stopped pid ")
    assert runner.invoke(app, ["web", "status"]).exit_code == 1


def test_without_the_web_extra_only_start_refuses(enso_home: Paths) -> None:
    script = textwrap.dedent(
        """
        import sys
        sys.modules["aiohttp"] = None  # the extra is not installed
        sys.modules["jinja2"] = None
        import enso.web, enso.cli.web, enso.runs, enso.db
        from typer.testing import CliRunner
        from enso.cli import app
        for argv in (["web", "status"], ["web", "stop"], ["web", "start"], ["workspace", "list"]):
            result = CliRunner().invoke(app, argv)
            output = (result.stdout + result.stderr).strip().replace("\\n", " | ")
            print(argv[-1], result.exit_code, output)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "status 1 not running"
    assert lines[1] == "stop 0 not running"
    assert lines[2].startswith(
        "start 1 error: the web viewer needs aiohttp, jinja2; install the web extra"
    )
    assert "Traceback" not in result.stdout and lines[3].startswith("list 0")

    child = subprocess.run(
        [sys.executable, "-c", "import sys; sys.modules['aiohttp'] = None; "
         "import runpy; runpy.run_module('enso.web', run_name='__main__')"],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert child.returncode == 1 and "install the web extra" in child.stderr
    assert "Traceback" not in child.stderr


def test_the_viewer_never_imports_the_runtime_or_transports() -> None:
    script = (
        "import sys, enso.web.server, enso.web.views, enso.web.files; "
        "print(sorted(m for m in sys.modules if m.startswith('enso.')))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout.replace("'", '"'))
    assert "enso.web.server" in loaded and "enso.audit" in loaded
    forbidden = ("enso.runtime", "enso.transports", "enso.cli")
    assert not any(name.startswith(forbidden) for name in loaded)


def test_config_paths_stay_clear_of_the_real_home(enso_home: Paths) -> None:
    assert not str(enso_home.home).startswith(str(Path.home() / ".enso"))
    assert os.environ["ENSO_HOME"] == str(enso_home.home)
    assert str(Paths.from_env().web_pid).startswith(str(enso_home.home))
    con = sqlite3.connect(":memory:")
    con.close()


# -- Tasks ----------------------------------------------------------------------


USER = "user:gavin"


@dataclass
class Board:
    paths: Paths
    config: Config
    run_id: str  # a live run holding EN-001


async def html(client: TestClient, path: str, status: int = 200) -> str:
    response = await client.get(path)
    body = await response.text()
    assert response.status == status, (path, response.status, body[:300])
    assert "Traceback" not in body
    return body


def task_links(body: str) -> list[str]:
    """The task rows on a list page, in order."""
    return re.findall(r'<a class="row entity" href="/tasks/([A-Z]+-\d+)"', body)


def heading(body: str) -> str:
    """The page's own <h1>, so an assertion about it cannot match the nav or the body."""
    found = re.search(r'<div class="phead"><h1>(.*?)</h1>', body, re.S)
    assert found is not None, body[:300]
    return found.group(1)


def board_groups(body: str) -> list[tuple[str, str, list[str]]]:
    """The board as rendered: each group's heading, its count line, and its rows in order."""
    found = []
    for chunk in body.split('<div class="hourhead">')[1:]:
        head = re.search(r"<b>([^<]+)</b>\s*<span>([^<]+)</span>", chunk)
        assert head is not None, chunk[:200]
        found.append((head.group(1), head.group(2), task_links(chunk)))
    return found


def group_rows(body: str) -> dict[str, list[str]]:
    return {label: rows for label, _count, rows in board_groups(body)}


def git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    done = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True, timeout=30
    )
    return done.stdout.strip()


@pytest.fixture
def board(enso_home: Paths, project_config: Config) -> Board:
    """Every state the board can show: one task each, through the core API only."""
    paths, config = enso_home, project_config
    write_job(paths, "dev-todo")
    run_id = runs.start(paths, load_job(paths, config, "dev-todo"), "manual", effort="high")

    def add(project: str, title: str, **kwargs: object) -> tasks.Task:
        return tasks.create(paths, config, project, title, actor=USER, **kwargs)  # type: ignore[arg-type]

    def move(ref: str, move_id: str, message: str = "done") -> tasks.Task:
        return tasks.move(paths, config, ref, move_id, actor=USER, run_id=None, message=message)

    active = add(
        "EN",
        "Fix labelled Slack code fences",
        body="# Spec\n\n<script>alert(1)</script>\n\n- keep the fence label\n",
    )
    move(active.ref, "advance", "Scope confirmed.\nTouch slack_text.py only.")
    tasks.add_ref(paths, active.ref, "commit", "abc123", actor=USER, run_id=None)
    taken = tasks.take(paths, config, "EN", "todo", run_id=run_id, actor="job:dev-todo")
    assert taken is not None and taken.ref == "EN-001"
    blocked = add("EN", "Needs a decision")
    move(blocked.ref, "block", "Waiting on the API key\nsecond line stays off the row")
    add("EN", "Someday", backlog=True)  # EN-003
    ready = add("EN", "Ready to triage")  # EN-004
    done = add("EN", "Shipped")  # EN-005
    for _ in range(3):
        move(done.ref, "advance")
    dropped = add("EN", "Never mind")  # EN-006
    move(dropped.ref, "drop", "no longer wanted")
    human = add("MKT", "Approve the launch post")  # MKT-001
    move(human.ref, "advance", "drafted")
    flagged = add("EN", "Look at this")  # EN-007
    tasks.note(paths, flagged.ref, actor="slack:U1", run_id=None, message="look", attention=True)
    # EN-004 was held by a run that retention has since pruned: the event outlives the row.
    held = tasks.take(paths, config, "EN", "triage", run_id="deadbeef", actor="job:dev-triage")
    assert held is not None and held.ref == ready.ref
    tasks.release(
        paths,
        ready.ref,
        actor="enso",
        run_id="deadbeef",
        message="run deadbeef ended (error) without a handoff",
        reason="run_ended",
    )
    return Board(paths, config, run_id)


async def test_tasks_board_groups_every_state(client: TestClient, board: Board) -> None:
    """One board: every state lands in its group, in the fixed order, and nothing repeats."""
    body = await html(client, "/tasks")
    assert '<a href="/tasks" aria-current="page">' in body  # the top nav tab
    # Four primary links and the More control carry the phone-sized icons.
    assert body.count('<svg class="icon" width="20"') == 5
    assert 'aria-label="Views"' not in body  # no view tabs; the dropdowns filter instead
    assert board_groups(body) == [
        ("Blocked", "3 tasks · needs you", ["EN-002", "MKT-001", "EN-007"]),
        ("Active", "1 task", ["EN-001"]),
        ("Ready", "1 task", ["EN-004"]),
        ("Backlog", "1 task", ["EN-003"]),
        ("Done", "2 tasks", ["EN-006", "EN-005"]),
    ]  # blocked oldest in stage first; done newest first, cancelled listed with it
    listed = task_links(body)
    assert len(listed) == len(set(listed)) == 8  # every task once, and only once
    assert "8 tasks." in body and "1 completed in the last 7 days" in body
    assert "up to 200 finished and cancelled tasks" in body
    assert f'claimed by run <span class="mono">{board.run_id}</span>' in body
    # The dot is the only place the state appears, and a row never carries a message.
    assert "Waiting on the API key" not in body
    assert "waiting on you" not in body and 'class="err"' not in body
    # An unknown parameter is ignored, so an old bookmarked tab still renders the board.
    assert task_links(await html(client, "/tasks?view=done")) == listed


async def test_ready_group_reads_down_the_pipeline(client: TestClient, board: Board) -> None:
    """Ready is one flat list ordered by project, then that project's own stage order."""
    paths, config = board.paths, board.config

    def advance(ref: str, times: int) -> None:
        for _ in range(times):
            tasks.move(paths, config, ref, "advance", actor=USER, run_id=None, message="on")

    todo = tasks.create(paths, config, "EN", "Middle of the pipeline", actor=USER)  # EN-008
    advance(todo.ref, 1)
    review = tasks.create(paths, config, "EN", "Late in the pipeline", actor=USER)  # EN-009
    advance(review.ref, 2)
    other = tasks.create(paths, config, "MKT", "First draft", actor=USER)  # MKT-002
    body = await html(client, "/tasks")
    assert group_rows(body)["Ready"] == ["EN-004", todo.ref, review.ref, other.ref]
    assert "Enso · triage" not in body  # one list, not a heading per project and stage


async def test_backlog_group_lists_oldest_in_stage_first(client: TestClient, board: Board) -> None:
    older = tasks.create(
        board.paths, board.config, "EN", "Waiting longer", actor=USER, backlog=True
    )
    with sqlite3.connect(board.paths.db) as con:
        con.execute(
            "UPDATE _enso_tasks SET entered_stage_at = '2020-01-01T00:00:00.000000+00:00' "
            "WHERE ref = ?",
            [older.ref],
        )
    assert group_rows(await html(client, "/tasks"))["Backlog"] == [older.ref, "EN-003"]
    # Flagging one moves it to Blocked: a task is in one group, never in two at once.
    tasks.note(board.paths, "EN-003", actor=USER, run_id=None, message="look", attention=True)
    groups = group_rows(await html(client, "/tasks"))
    assert groups["Backlog"] == [older.ref]
    assert groups["Blocked"] == ["EN-002", "EN-003", "MKT-001", "EN-007"]


async def test_flagged_claim_moves_to_blocked_and_keeps_its_run(
    client: TestClient, board: Board
) -> None:
    """A claimed task flagged for attention joins Blocked, warning dot and claim intact."""
    tasks.note(board.paths, "EN-001", actor="slack:U1", run_id=None, message="hm", attention=True)
    body = await html(client, "/tasks")
    groups = group_rows(body)
    assert "EN-001" in groups["Blocked"]
    assert "Active" not in groups  # the emptied group is left out, not shown with a zero
    row = body[body.index('href="/tasks/EN-001"') :]
    assert '<span class="pin warning" role="img" aria-label="needs you"' in row[:180]
    assert f'claimed by run <span class="mono">{board.run_id}</span>' in row


async def test_tasks_board_reads_a_bounded_amount(
    client: TestClient, board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The board never walks a task's events, and reads the finished history by count and page."""

    def no_events(*args: object, **kwargs: object) -> list[tasks.TaskEvent]:
        raise AssertionError("a list page must not read task events")

    monkeypatch.setattr(tasks, "events", no_events)
    for path in ("/tasks", "/tasks?project=EN", "/tasks?stage=done", "/tasks?q=shipped"):
        assert "Tasks could not be read" not in await html(client, path)

    # The count line counts this week in SQL while the Done group lists the newest page: an
    # old finish drops out of the count but stays listed, and the list is cut, the count not.
    with sqlite3.connect(board.paths.db) as con:
        con.execute(
            "UPDATE _enso_tasks SET entered_stage_at = '2020-01-01T00:00:00.000000+00:00' "
            "WHERE ref = 'EN-005'"
        )
    aged = await html(client, "/tasks")
    assert group_rows(aged)["Done"] == ["EN-006", "EN-005"]
    assert "8 tasks." in aged and "0 completed in the last 7 days" in aged

    monkeypatch.setattr(views, "DONE_LIMIT", 1)
    capped = await html(client, "/tasks")
    assert task_links(capped) == [
        "EN-002", "MKT-001", "EN-007", "EN-001", "EN-004", "EN-003", "EN-006"
    ]  # fmt: skip
    # The count still includes the old finish beyond the cap, and the line says so.
    assert "8 tasks, 7 listed." in capped
    assert "Done lists up to 1 finished and cancelled tasks." in capped
    assert task_links(await html(client, "/tasks?q=shipped")) == ["EN-005"]
    assert task_links(await html(client, "/tasks?q=en-5")) == ["EN-005"]
    assert task_links(await html(client, "/tasks?q=%25")) == []  # LIKE is escaped


async def test_tasks_filters_and_empty_states(client: TestClient, board: Board) -> None:
    assert task_links(await html(client, "/tasks?project=MKT")) == ["MKT-001"]
    assert task_links(await html(client, "/tasks?project=mkt")) == ["MKT-001"]
    assert task_links(await html(client, "/tasks?stage=blocked")) == ["EN-002"]
    assert task_links(await html(client, "/tasks?q=fences")) == ["EN-001"]
    assert task_links(await html(client, "/tasks?q=en-3")) == ["EN-003"]
    by_project = await html(client, "/tasks?project=EN")
    assert board_groups(by_project) == [
        ("Blocked", "2 tasks · needs you", ["EN-002", "EN-007"]),
        ("Active", "1 task", ["EN-001"]),
        ("Ready", "1 task", ["EN-004"]),
        ("Backlog", "1 task", ["EN-003"]),
        ("Done", "2 tasks", ["EN-006", "EN-005"]),
    ]
    assert "7 tasks matching the filter." in by_project
    assert '<option value="EN" selected>' in by_project

    assert "No tasks matching “nothing here”." in await html(client, "/tasks?q=nothing+here")
    assert "No tasks in project ZZ." in await html(client, "/tasks?project=zz")
    assert "No tasks in stage review." in await html(client, "/tasks?stage=review")
    assert "No tasks in project MKT in stage backlog." in await html(
        client, "/tasks?project=MKT&stage=backlog"
    )
    assert "No tasks in stage backlog matching “shipped”." in await html(
        client, "/tasks?stage=backlog&q=shipped"
    )
    odd = await html(client, "/tasks?stage=../x&q=%3Cb%3Ebold%3C/b%3E")
    assert task_links(odd) == [] and "<b>bold</b>" not in odd  # bad stage dropped, q escaped


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ("project=MKT", ["MKT-001"]),
        ("stage=backlog", ["EN-003"]),
        ("stage=done", ["EN-005"]),
        ("stage=cancelled", ["EN-006"]),
        ("q=keep+the+fence+label", ["EN-001"]),  # body search
        ("q=++en-0005++", ["EN-005"]),  # the same reference normalization for finished work
        ("q=++en-0003++", ["EN-003"]),
        ("project=EN&stage=done&q=shipped", ["EN-005"]),
        ("project=MKT&stage=done&q=shipped", []),
    ],
)
async def test_board_filters(
    client: TestClient, board: Board, filters: str, expected: list[str]
) -> None:
    page = await html(client, f"/tasks?{filters}")
    assert task_links(page) == expected
    if expected:
        plural = "s" if len(expected) != 1 else ""
        assert f"{len(expected)} task{plural} matching the filter." in page
    else:
        assert board_groups(page) == [] and "No tasks " in page


async def test_empty_board_says_so(client: TestClient, project_config: Config) -> None:
    page = await html(client, "/tasks")
    assert task_links(page) == [] and board_groups(page) == []
    assert "No tasks yet." in page


async def test_task_page_shows_the_record(client: TestClient, board: Board) -> None:
    page = await html(client, "/tasks/en-1")  # the reference is normalised
    assert "Fix labelled Slack code fences" in page and "EN-001" in page
    assert '<a href="/tasks" aria-current="page">' in page
    assert "<h1>Spec</h1>" in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert(1)" not in page
    assert "<code>abc123</code>" in page  # refs
    assert "Scope confirmed.\nTouch slack_text.py only." in page  # the handoff, verbatim
    assert f'<a href="/runs/{board.run_id}"><code>{board.run_id}</code></a>' in page  # claim
    assert f'<a class="row" href="/runs/{board.run_id}">' in page  # the taken event links
    assert f'· run <span class="mono">{board.run_id}</span>' in page  # named in full, in the title
    assert "<b>run</b>" not in page  # the trail is the relative time in every timeline row
    assert "Worktree" not in page  # the project has no repo
    # The stage is the pipeline chip and nothing else: the heading repeated it for nothing.
    assert '<span class="chip current">todo</span>' in page
    assert '<span class="tag' not in heading(page)
    assert "run pruned" not in page

    pruned = await html(client, "/tasks/EN-004")
    assert "run pruned" in pruned and 'href="/runs/deadbeef"' not in pruned
    assert "Run deadbeef ended without a handoff" in pruned  # recovery context
    assert "released (run ended)" in pruned and "taken" in pruned

    blocked = await html(client, "/tasks/EN-002")
    assert '<span class="chip current">blocked</span>' in blocked  # off the pipeline, still shown
    assert "block: triage → blocked" in blocked
    assert "second line stays off the row" in blocked  # the timeline keeps the whole message

    # A stage off the pipeline still shows, and only attention still qualifies the heading.
    assert '<span class="chip current">cancelled</span>' in await html(client, "/tasks/EN-006")
    assert '<span class="chip current">done</span>' in await html(client, "/tasks/EN-005")
    flagged = heading(await html(client, "/tasks/EN-007"))
    assert '<span class="tag tag-warning">needs attention</span>' in flagged
    assert flagged.count('<span class="tag') == 1

    assert "Not found" in await html(client, "/tasks/EN-999", 404)
    assert "Not found" in await html(client, "/tasks/nope", 404)
    assert "Not found" in await html(client, "/tasks/EN-1;DROP", 404)


async def test_task_page_reports_a_failed_context_read(
    client: TestClient, board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The context read feeds the handoff and the recovery notice; a failure must be visible."""

    def boom(*args: object, **kwargs: object) -> dict:
        raise RuntimeError("context is unreadable")

    monkeypatch.setattr(tasks, "context", boom)
    page = await html(client, "/tasks/EN-001")
    assert "The task could not be read" in page
    assert "RuntimeError: context is unreadable" in page
    assert "<h2>Handoff</h2>" not in page  # the handoff is the part that went missing
    assert "Fix labelled Slack code fences" in page  # the rest of the record still renders


async def test_task_page_worktree_panel(
    client: TestClient, enso_home: Paths, raw_config_projects: dict, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README").write_text("hi\n")
    git(repo, "add", "README")
    git(repo, "commit", "-q", "-m", "first")
    raw_config_projects["projects"]["EN"]["repo"] = str(repo)
    config, problems, _ = parse_config(raw_config_projects, enso_home)
    assert config is not None, problems
    write_config(enso_home, raw_config_projects)
    db.migrate(enso_home)
    task = tasks.create(enso_home, config, "EN", "With a branch", actor=USER)
    assert "Worktree" not in await html(client, f"/tasks/{task.ref}")  # nothing prepared yet

    worktree = enso_home.worktrees / "EN" / task.ref
    worktree.parent.mkdir(parents=True)
    git(repo, "worktree", "add", "-q", "-b", f"enso/{task.ref}", str(worktree))
    (worktree / "change").write_text("x\n")
    git(worktree, "add", "change")
    git(worktree, "commit", "-q", "-m", "work")
    page = await html(client, f"/tasks/{task.ref}")
    assert "Worktree" in page and f"<code>enso/{task.ref}</code>" in page
    assert f"<code>{worktree}</code>" in page and "<code>main</code>" in page
    assert "1 commit ahead of main" in page

    # Git failing must never take the page down: an unreadable worktree still renders.
    for path in sorted(worktree.rglob("*"), reverse=True):
        path.unlink() if path.is_file() or path.is_symlink() else path.rmdir()
    (worktree / "leftover").write_text("")
    broken = await html(client, f"/tasks/{task.ref}")
    assert "Worktree" in broken and "unknown; git could not count them" in broken


async def test_run_page_links_to_its_task(client: TestClient, board: Board) -> None:
    page = await html(client, f"/runs/{board.run_id}")
    assert "<dt>Task</dt>" in page
    assert '<a href="/tasks/EN-001"><code>EN-001</code> Fix labelled Slack code fences</a>' in page
    other = runs.start(
        board.paths, load_job(board.paths, board.config, "dev-todo"), "manual", effort="high"
    )
    assert "<dt>Task</dt>" not in await html(client, f"/runs/{other}")


async def test_task_pages_nest_no_links(client: TestClient, board: Board) -> None:
    """The anchor-depth walk from test_web_pages, over the pages this board adds."""
    for path in (
        "/tasks",
        "/tasks?project=EN",
        "/tasks?stage=done",
        "/tasks?project=EN&stage=blocked&q=x",
        "/tasks/EN-001",
        "/tasks/EN-002",
        "/tasks/EN-004",
        f"/runs/{board.run_id}",
    ):
        body = await html(client, path)
        depth = 0
        for token in re.findall(r"<a\b|</a>", body):
            depth += 1 if token == "<a" else -1
            assert 0 <= depth <= 1, f"nested or unbalanced <a> in {path}"
        assert depth == 0, f"unbalanced <a> in {path}"
        assert "<form" not in body or 'method="get"' in body
        assert "style=" not in body


async def test_stage_job_pages_say_when_work_is_ready(client: TestClient, board: Board) -> None:
    """A stage job with no cron line says so in prose on every page; only cron is a literal."""
    write_job(board.paths, "dev-triage", project="EN", stage="triage", omit=["schedule"])
    job = load_job(board.paths, board.config, "dev-triage")
    run_id = runs.start(board.paths, job, "ready", effort="high")  # so Today's reliability lists it
    runs.finish(board.paths, run_id, status="ok", exit_code=0)
    listing = await html(client, "/jobs")
    assert "<span>when work is ready</span>" in listing
    assert "<code>when work is ready</code>" not in listing
    assert "<code>0 9 * * *</code>" in listing  # the scheduled dev-todo row keeps its literal
    page = await html(client, "/jobs/dev-triage")
    assert "None" not in page  # the missing cron line never leaks as Python's None
    assert "a stage job: it claims a ready task there" in page  # the Stage row
    assert "none; it fires when work is ready" in page
    assert '<a href="/tasks?project=EN&amp;stage=triage">' in page
    assert '<code>project:EN</code> <span class="muted">(the stage job default)</span>' in page
    assert "when a task is ready</span>" in page  # the Next run row
    today = await html(client, "/today/reliability")
    assert "<span>when work is ready</span>" in today and "None" not in today


async def test_tasks_pages_without_a_database(client: TestClient, enso_home: Paths) -> None:
    listing = await html(client, "/tasks")
    assert "Tasks could not be read" in listing and "enso.db does not exist" in listing
    page = await html(client, "/tasks/EN-001")
    assert "The task could not be read" in page and "enso.db does not exist" in page
    assert "Not found" in await html(client, "/tasks/nope", 404)
