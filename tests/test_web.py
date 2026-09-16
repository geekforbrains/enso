"""The web viewer's plumbing: config, the read-only database, the process, and the rules.

Page content is in ``test_web_pages.py`` and ``test_web_tasks.py``. Everything here runs
against a scratch home; process tests spawn the real ``python -m enso.web`` on a free port
and always stop it.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import struct
import subprocess
import sys
import textwrap
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import load_job, write_config, write_job
from typer.testing import CliRunner

from enso import db, runs, web
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

    db.initialize(enso_home)
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
    db.initialize(enso_home)
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
    assert [s.id for s in runs.list_summaries(enso_home, job="default:nightly")] == [second, first]
    assert [s.id for s in runs.list_summaries(enso_home, status="ok")] == [second]
    assert [s.id for s in runs.list_summaries(enso_home, limit=1, offset=1)] == [second]
    assert runs.count(enso_home) == 3 and runs.count(enso_home, job="default:nightly") == 2
    assert runs.count(enso_home, job="default:nightly", status="error") == 1
    latest = runs.latest_summaries(enso_home)
    assert {name: s.id for name, s in latest.items()} == {
        "default:nightly": second,
        "default:other": third,
    }
    assert runs.job_names(enso_home) == ["default:nightly", "default:other"]
    full = runs.get(enso_home, first[:6])
    assert full is not None and full.output == "x" * 5000 and full.error == "e" * 500
    assert runs.STATUSES[0] == "running" and runs.PAGE_SIZE == 500


async def test_run_detail_shows_attempts_and_escapes_feedback(
    client: TestClient, enso_home: Paths, config: Config
) -> None:
    db.initialize(enso_home)
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
    assert web.stop(paths) == "not running"
    assert not paths.web_pid.exists()
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


def test_pidfile_refuses_a_symbolic_link(enso_home: Paths, tmp_path: Path) -> None:
    paths = enso_home
    paths.home.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.pid"
    outside.write_text('{"pid": 4242}\n')
    paths.web_pid.symlink_to(outside)
    with pytest.raises(web.WebError, match="symbolic link"):
        web.PidFile(paths.web_pid).acquire()
    assert outside.read_text() == '{"pid": 4242}\n'


@pytest.mark.parametrize("command", ["status", "stop"])
def test_pidfile_readers_refuse_a_locked_symbolic_link(
    enso_home: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    outside = tmp_path / "outside.pid"
    holder = web.PidFile(outside)
    holder.acquire()
    try:
        holder.write(4242, web.Bind("127.0.0.1", 8787))
        original = outside.read_bytes()
        enso_home.web_pid.symlink_to(outside)

        def refuse_signal(pid: int, signum: signal.Signals) -> None:
            pytest.fail(f"signalled unrelated pid {pid} with {signum}")

        monkeypatch.setattr(web, "_signal", refuse_signal)
        with pytest.raises(web.WebError, match="symbolic link"):
            getattr(web, command)(enso_home)
        result = CliRunner().invoke(app, ["web", command])
        assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
        assert "error: lock must not be a symbolic link" in result.stderr
        assert outside.read_bytes() == original
    finally:
        holder.release()


def test_pidfile_readers_refuse_a_fifo_without_blocking(enso_home: Paths) -> None:
    os.mkfifo(enso_home.web_pid)
    script = textwrap.dedent(
        """
        from enso import web
        from enso.config import Paths
        for operation in (web.status, web.stop):
            try:
                operation(Paths.from_env())
            except web.WebError as exc:
                assert "regular file" in str(exc), str(exc)
            else:
                raise AssertionError(f"{operation.__name__} accepted a FIFO")
        """
    )
    # A blocking open must fail the test instead of hanging the suite.
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr


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
        "import sys, enso.web.server; "
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
