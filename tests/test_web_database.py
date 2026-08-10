"""Web behavior when the shared SQLite database cannot be read promptly."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("starlette")
pytest.importorskip("jinja2")
httpx = pytest.importorskip("httpx")
web_app = pytest.importorskip("enso.web.app")


_RUN_ID = "1" * 32
# SQLite primary result codes; sqlite3's named constants are absent on Python 3.10.
_SQLITE_BUSY = 5
_SQLITE_CANTOPEN = 14


def _database_web_app(tmp_path: Path, monkeypatch):
    """Build an app with an isolated database and one real job detail page."""
    import enso.config as config_mod
    import enso.jobs as jobs_mod

    config_dir = tmp_path / "enso"
    jobs_dir = config_dir / "jobs"
    workspace = tmp_path / "workspace"
    jobs_dir.mkdir(parents=True)
    workspace.mkdir()
    (jobs_dir / "demo").mkdir()
    (jobs_dir / "demo" / "JOB.md").write_text(
        "---\n"
        "name: Demo\n"
        'schedule: "0 9 * * *"\n'
        "provider: codex\n"
        "model: gpt-test\n"
        "workspace: default\n"
        "access: admin\n"
        "enabled: false\n"
        "---\n\n"
        "Original prompt.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(config_dir))
    for module in (config_mod, jobs_mod, web_app):
        monkeypatch.setattr(module, "JOBS_DIR", str(jobs_dir))
    runtime = SimpleNamespace(working_dir=str(workspace), config={"web": {}})
    return config_dir, web_app.create_app(runtime)


def _sqlite_error(message: str, code: int, name: str) -> sqlite3.OperationalError:
    """Construct the same annotated exception produced by sqlite3 calls."""
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code
    error.sqlite_errorname = name
    return error


def _fail_run_read(monkeypatch, operation: str, error: BaseException) -> None:
    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(web_app.runs, operation, fail)


@pytest.mark.parametrize(
    ("path", "operation"),
    [
        ("/runs", "list_runs"),
        (f"/runs/{_RUN_ID}", "get"),
    ],
)
def test_run_pages_render_database_busy_as_retryable_503(
    tmp_path,
    monkeypatch,
    path,
    operation,
):
    from starlette.testclient import TestClient

    _, app = _database_web_app(tmp_path, monkeypatch)
    _fail_run_read(
        monkeypatch,
        operation,
        _sqlite_error("database is locked sentinel", _SQLITE_BUSY, "SQLITE_BUSY"),
    )
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        raise_server_exceptions=False,
    )

    response = client.get(path)

    assert response.status_code == 503
    assert "<!doctype html>" in response.text
    assert "data-database-busy" in response.text
    assert "Database busy" in response.text
    assert "Try again" in response.text
    assert "sentinel" not in response.text


@pytest.mark.parametrize(
    ("path", "operation"),
    [
        ("/runs", "list_runs"),
        (f"/runs/{_RUN_ID}", "get"),
    ],
)
def test_run_pages_render_database_unavailable_as_503(
    tmp_path,
    monkeypatch,
    path,
    operation,
):
    from starlette.testclient import TestClient

    _, app = _database_web_app(tmp_path, monkeypatch)
    _fail_run_read(
        monkeypatch,
        operation,
        _sqlite_error(
            "unable to open database file sentinel",
            _SQLITE_CANTOPEN,
            "SQLITE_CANTOPEN",
        ),
    )
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        raise_server_exceptions=False,
    )

    response = client.get(path)

    assert response.status_code == 503
    assert "<!doctype html>" in response.text
    assert "data-database-unavailable" in response.text
    assert "Database unavailable" in response.text
    assert "sentinel" not in response.text


@pytest.mark.parametrize(
    ("error", "marker", "heading"),
    [
        (
            _sqlite_error("database is locked sentinel", _SQLITE_BUSY, "SQLITE_BUSY"),
            "data-database-busy",
            "Database busy",
        ),
        (
            _sqlite_error(
                "unable to open database file sentinel",
                _SQLITE_CANTOPEN,
                "SQLITE_CANTOPEN",
            ),
            "data-database-unavailable",
            "Database unavailable",
        ),
    ],
)
def test_job_detail_keeps_non_database_content_when_run_history_fails(
    tmp_path,
    monkeypatch,
    error,
    marker,
    heading,
):
    from starlette.testclient import TestClient

    _, app = _database_web_app(tmp_path, monkeypatch)
    _fail_run_read(monkeypatch, "list_runs", error)
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        raise_server_exceptions=False,
    )

    response = client.get("/jobs/demo")

    assert response.status_code == 200
    assert "Demo" in response.text
    assert "Original prompt." in response.text
    assert "data-runs-unavailable" in response.text
    assert marker in response.text
    assert heading in response.text
    assert "No runs yet" not in response.text
    assert "sentinel" not in response.text


def test_dashboard_keeps_other_sections_and_identifies_busy_run_history(
    tmp_path,
    monkeypatch,
):
    from starlette.testclient import TestClient

    _, app = _database_web_app(tmp_path, monkeypatch)
    _fail_run_read(
        monkeypatch,
        "list_runs",
        _sqlite_error("database is locked sentinel", _SQLITE_BUSY, "SQLITE_BUSY"),
    )
    monkeypatch.setattr(web_app, "load_jobs", lambda: [])
    monkeypatch.setattr(web_app, "_skill_inventory", lambda _request: ([], []))
    monkeypatch.setattr(web_app.docs, "load_docs", lambda: SimpleNamespace(docs=[]))
    monkeypatch.setattr(
        web_app.tables,
        "list_tables",
        lambda: SimpleNamespace(tables=[], truncated=False),
    )
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        raise_server_exceptions=False,
    )

    response = client.get("/")

    assert response.status_code == 200
    assert "Dashboard" in response.text
    assert "data-runs-unavailable" in response.text
    assert "data-database-busy" in response.text
    assert "Database busy" in response.text
    assert "No runs yet" not in response.text
    assert "sentinel" not in response.text


@pytest.mark.asyncio
async def test_held_database_lock_does_not_delay_health_response(tmp_path, monkeypatch):
    """A SQLite busy wait must run outside the ASGI event-loop thread."""
    config_dir, app = _database_web_app(tmp_path, monkeypatch)
    database_path = config_dir / "enso.db"
    locker = sqlite3.connect(database_path, timeout=0, check_same_thread=False)
    locker.execute("CREATE TABLE held_lock (id INTEGER PRIMARY KEY)")
    locker.commit()
    locker.execute("BEGIN EXCLUSIVE")

    released = threading.Event()
    release_guard = threading.Lock()

    def release_lock() -> None:
        with release_guard:
            if released.is_set():
                return
            locker.rollback()
            released.set()

    # Prevent a failing implementation from stalling the suite for sqlite3's
    # default five-second busy timeout. A responsive implementation releases
    # the lock itself immediately after checking /health.
    watchdog = threading.Timer(1.5, release_lock)
    watchdog.start()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
        ) as client:
            started_at = time.monotonic()
            runs_request = asyncio.create_task(client.get("/runs"))
            await asyncio.sleep(0)

            health = await client.get("/health")
            health_elapsed = time.monotonic() - started_at

            release_lock()
            runs_response = await asyncio.wait_for(runs_request, timeout=2)
    finally:
        watchdog.cancel()
        release_lock()
        locker.close()

    assert health.status_code == 200
    assert health.text == "ok"
    assert health_elapsed < 0.5
    assert runs_response.status_code in (200, 503)
