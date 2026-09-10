"""OpenRouter model parsing, shared-cache selection, refresh, and CLI output."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import socket
import socketserver
import threading
import time
from http.client import HTTPMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request

import pytest
from typer.testing import CliRunner

from enso import models
from enso.cli import app
from enso.config import Paths

NOW = 2_000_000_000.0
REAL_MONOTONIC = time.monotonic  # the autouse clock replaces it for every test


DEFAULT_OPTIONS = object()


def entry(
    model_id: str,
    *,
    tool_call: bool = True,
    context: int | None = 131_072,
    input_cost: int | float | None = 0.25,
    output_cost: int | float | None = 1.5,
    efforts: list[str | None] | None = None,
    reasoning: bool = False,
    options: object = DEFAULT_OPTIONS,
) -> dict:
    return {
        "id": model_id,
        "tool_call": tool_call,
        "reasoning": reasoning,
        "limit": None if context is None else {"context": context},
        "cost": None
        if input_cost is None and output_cost is None
        else {"input": input_cost, "output": output_cost},
        "reasoning_options": [
            {"type": "toggle"},
            {"type": "effort", "values": efforts or []},
            {"type": "budget_tokens"},
        ]
        if options is DEFAULT_OPTIONS
        else options,
    }


def document(entries: dict[str, object]) -> dict:
    return {"another-provider": {"models": {}}, "openrouter": {"models": entries}}


def one_catalog(model_id: str, **values: object) -> dict:
    return document({model_id: entry(model_id, **values)})


def write_cache(path: Path, catalog: object, modified: float) -> bytes:
    raw = json.dumps(catalog, separators=(",", ":")).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    os.utime(path, (modified, modified))
    return raw


@pytest.fixture(autouse=True)
def isolated_opencode_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "xdg-cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(root))
    return root / "opencode" / "models.json"


class Clock:
    """A monotonic clock tests advance themselves, so no test waits on real seconds."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(models.time, "time", lambda: NOW)
    monkeypatch.setattr(models.time, "monotonic", fake)
    return fake


class DripResponse:
    """A peer that answers every read promptly but never finishes the body."""

    def __init__(self, clock: Clock, seconds: float) -> None:
        self.clock = clock
        self.seconds = seconds
        self.reads = 0

    def read1(self, size: int) -> bytes:
        self.reads += 1
        self.clock.advance(self.seconds)
        return b"x" * min(size, 8)

    def __enter__(self) -> DripResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class Blocker:
    """A call that blocks until the test releases it, standing in for a stuck step."""

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.entered.set()
        self.release.wait(30)
        raise self.failure


def response(catalog: object) -> io.BytesIO:
    return io.BytesIO(json.dumps(catalog).encode())


def test_parse_catalog_preserves_schema_values_and_sorts() -> None:
    catalog = document(
        {
            "z/free": entry(
                "z/free", context=1_000_000, input_cost=0, output_cost=0, efforts=["high", "xhigh"]
            ),
            "no-tools": entry(
                "no-tools", tool_call=False, context=None, input_cost=None, output_cost=None
            ),
            "openrouter/auto": entry("openrouter/auto", input_cost=0.00000125, output_cost=2.75),
        }
    )

    assert [model.as_dict() for model in models.parse_catalog(catalog)] == [
        {
            "id": "openrouter/no-tools",
            "tool_call": False,
            "context": None,
            "cost": {"input": None, "output": None},
            "efforts": [],
        },
        {
            "id": "openrouter/openrouter/auto",
            "tool_call": True,
            "context": 131_072,
            "cost": {"input": 0.00000125, "output": 2.75},
            "efforts": [],
        },
        {
            "id": "openrouter/z/free",
            "tool_call": True,
            "context": 1_000_000,
            "cost": {"input": 0, "output": 0},
            "efforts": ["high", "xhigh"],
        },
    ]


@pytest.mark.parametrize(
    ("model_id", "reasoning", "options", "efforts"),
    [
        # A published effort list is copied through in registry order, null spelled `none`.
        (
            "openai/gpt-5.1",
            True,
            [{"type": "effort", "values": [None, "low", "high"]}],
            ["none", "low", "high"],
        ),
        # An effort list wins over the other metadata the same model publishes.
        (
            "deepseek/deepseek-v4-flash",
            True,
            [{"type": "toggle"}, {"type": "effort", "values": ["high", "xhigh"]}],
            ["high", "xhigh"],
        ),
        # A model that only publishes a thinking-token budget still names two variants.
        (
            "google/gemini-2.5-pro",
            True,
            [{"type": "budget_tokens", "min": 128, "max": 32768}],
            ["high", "max"],
        ),
        # Toggle-only metadata leaves OpenCode on its family default, which is not empty.
        ("anthropic/claude-haiku-4.5", True, [{"type": "toggle"}], ["low", "medium", "high"]),
        (
            "openai/o3",
            True,
            [{"type": "toggle"}],
            ["none", "minimal", "low", "medium", "high", "xhigh"],
        ),
        # Missing metadata takes the same default, including its per-family exceptions.
        ("z-ai/glm-5.2", True, None, ["high", "xhigh"]),
        # For some families that default is genuinely no variant at all.
        ("qwen/qwen3-max-thinking", True, [{"type": "toggle"}], []),
        ("z-ai/glm-4.6", True, None, []),
        # An empty list advertises none, and a model without reasoning never has one.
        ("qwen/qwen3-vl-235b-a22b-thinking", True, [], []),
        ("openai/gpt-5.2-chat", False, None, []),
    ],
)
def test_efforts_report_the_variants_opencode_exposes(
    model_id: str, reasoning: bool, options: object, efforts: list[str]
) -> None:
    catalog = one_catalog(model_id, reasoning=reasoning, options=options)

    assert [model.efforts for model in models.parse_catalog(catalog)] == [tuple(efforts)]


def test_cli_shows_derived_variants_in_text_and_json(
    enso_home: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = document(
        {
            "google/gemini-2.5-pro": entry(
                "google/gemini-2.5-pro",
                context=1_048_576,
                input_cost=1.25,
                output_cost=10,
                reasoning=True,
                options=[{"type": "budget_tokens", "min": 128, "max": 32768}],
            ),
            "qwen/qwen3-max-thinking": entry(
                "qwen/qwen3-max-thinking",
                context=256_000,
                input_cost=1.2,
                output_cost=6,
                reasoning=True,
                options=[{"type": "toggle"}],
            ),
        }
    )
    write_cache(enso_home.models_cache, catalog, NOW)
    monkeypatch.setattr(models, "_open", lambda *args, **kwargs: pytest.fail("fetched"))
    runner = CliRunner()

    text_result = runner.invoke(app, ["models"])
    assert text_result.exit_code == 0 and text_result.stderr == ""
    assert text_result.stdout.splitlines() == [
        "MODEL                               TOOLS  CONTEXT    INPUT $/M  OUTPUT $/M  EFFORTS",
        "openrouter/google/gemini-2.5-pro    yes    1,048,576  1.25       10          high,max",
        "openrouter/qwen/qwen3-max-thinking  yes    256,000    1.2        6           -",
    ]
    json_result = runner.invoke(app, ["models", "--json"])
    assert [item["efforts"] for item in json.loads(json_result.stdout)] == [["high", "max"], []]


@pytest.mark.parametrize(
    ("catalog", "problem"),
    [
        ([], "root"),
        ({}, "openrouter"),
        ({"openrouter": []}, "openrouter"),
        ({"openrouter": {}}, "openrouter.models"),
        (document({}), "openrouter.models"),
        (document({"bad": None}), "models['bad']"),
        (document({"bad": {"id": "bad", "tool_call": 1}}), "tool_call"),
        (document({"bad": {"id": "other", "tool_call": True}}), "does not match"),
        (one_catalog("bad", context=-1), "limit.context"),
        (one_catalog("bad", input_cost=-1), "cost.input"),
    ],
)
def test_parse_catalog_rejects_malformed_documents(catalog: object, problem: str) -> None:
    with pytest.raises(models.ModelsError, match=problem.replace("[", r"\[")):
        models.parse_catalog(catalog)


def test_parse_catalog_rejects_malformed_nested_objects() -> None:
    for field, value, problem in (
        ("limit", [], "limit"),
        ("cost", [], "cost"),
        ("reasoning_options", {}, "reasoning_options"),
        ("reasoning_options", [None], "reasoning_options[0]"),
        ("reasoning_options", [{"type": "effort"}], "values"),
        ("reasoning_options", [{"type": "effort", "values": ["low", 3]}], "values"),
        ("reasoning", "yes", "reasoning"),
    ):
        catalog = one_catalog("bad")
        catalog["openrouter"]["models"]["bad"][field] = value
        with pytest.raises(models.ModelsError, match=problem.replace("[", r"\[")):
            models.parse_catalog(catalog)


def test_newest_valid_fresh_cache_wins(
    enso_home: Paths,
    isolated_opencode_cache: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_cache(enso_home.models_cache, one_catalog("enso"), NOW - 20)
    write_cache(isolated_opencode_cache, one_catalog("opencode"), NOW - 10)
    monkeypatch.setattr(models, "_open", lambda *args, **kwargs: pytest.fail("fetched"))

    assert [model.id for model in models.load(enso_home).models] == ["openrouter/opencode"]

    isolated_opencode_cache.write_text("not json")
    os.utime(isolated_opencode_cache, (NOW, NOW))
    assert [model.id for model in models.load(enso_home).models] == ["openrouter/enso"]


@pytest.mark.parametrize("modified", [NOW - models.CACHE_TTL_SECONDS, NOW + 300])
def test_ttl_boundary_and_future_mtime_are_fresh(
    enso_home: Paths,
    modified: float,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_cache(enso_home.models_cache, one_catalog("cached"), modified)
    monkeypatch.setattr(models, "_open", lambda *args, **kwargs: pytest.fail("fetched"))

    assert [model.id for model in models.load(enso_home).models] == ["openrouter/cached"]


def test_stale_caches_refresh_and_only_enso_cache_is_replaced(
    enso_home: Paths,
    isolated_opencode_cache: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
) -> None:
    write_cache(enso_home.models_cache, one_catalog("old-enso"), NOW - 5_000)
    opencode_raw = write_cache(isolated_opencode_cache, one_catalog("old-opencode"), NOW - 4_000)
    fetched = one_catalog("new", input_cost=0, output_cost=None)
    fetched_raw = json.dumps(fetched).encode()
    calls: list[tuple[Request, float]] = []

    def open_catalog(request: Request, deadline: float) -> io.BytesIO:
        calls.append((request, deadline))
        return io.BytesIO(fetched_raw)

    monkeypatch.setattr(models, "_open", open_catalog)
    catalog = models.load(enso_home)

    assert [model.id for model in catalog.models] == ["openrouter/new"]
    assert catalog.warning is None
    assert enso_home.models_cache.read_bytes() == fetched_raw
    assert isolated_opencode_cache.read_bytes() == opencode_raw
    assert not list(enso_home.cache.glob("*.tmp"))
    assert calls[0][1] == clock.now + models.FETCH_TIMEOUT_SECONDS
    assert calls[0][0].full_url == models.CATALOG_URL
    assert calls[0][0].get_header("User-agent") == "enso-models"


def test_fetch_failure_uses_newest_stale_cache_and_warns(
    enso_home: Paths,
    isolated_opencode_cache: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_cache(enso_home.models_cache, one_catalog("older"), NOW - 6_000)
    write_cache(isolated_opencode_cache, one_catalog("newer"), NOW - 5_000)

    def offline(*args: object, **kwargs: object) -> None:
        raise OSError("offline")

    monkeypatch.setattr(models, "_open", offline)
    result = CliRunner().invoke(app, ["models", "--json"])

    assert result.exit_code == 0
    assert [item["id"] for item in json.loads(result.stdout)] == ["openrouter/newer"]
    assert result.stderr == (
        "warning: could not fetch models.dev catalog: offline; "
        f"using stale cache {isolated_opencode_cache}\n"
    )


def test_fetch_and_atomic_write_failure_still_return_fetched_models(
    enso_home: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(models, "_open", lambda *args, **kwargs: response(one_catalog("new")))

    def refuse_replace(source: Path, destination: Path) -> None:
        raise OSError("read only")

    monkeypatch.setattr(models.os, "replace", refuse_replace)
    catalog = models.load(enso_home)

    assert [model.id for model in catalog.models] == ["openrouter/new"]
    assert catalog.warning == f"could not write model cache {enso_home.models_cache}: read only"
    assert not enso_home.models_cache.exists()
    assert not list(enso_home.cache.glob("*.tmp"))


def test_cli_filters_and_formats_without_touching_config_or_database(
    enso_home: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = document(
        {
            "capable": entry(
                "capable", context=128_000, input_cost=0, output_cost=None, efforts=["low", "high"]
            ),
            "text-only": entry(
                "text-only", tool_call=False, context=None, input_cost=None, output_cost=None
            ),
        }
    )
    write_cache(enso_home.models_cache, catalog, NOW)
    enso_home.config.write_bytes(b"not valid config")
    original = enso_home.config.read_bytes()
    monkeypatch.setattr(models, "_open", lambda *args, **kwargs: pytest.fail("fetched"))
    runner = CliRunner()

    text_result = runner.invoke(app, ["models"])
    assert text_result.exit_code == 0 and text_result.stderr == ""
    assert text_result.stdout.splitlines() == [
        "MODEL               TOOLS  CONTEXT  INPUT $/M  OUTPUT $/M  EFFORTS",
        "openrouter/capable  yes    128,000  0          -           low,high",
    ]
    json_result = runner.invoke(app, ["models", "--all", "--json"])
    assert [item["id"] for item in json.loads(json_result.stdout)] == [
        "openrouter/capable",
        "openrouter/text-only",
    ]
    assert json.loads(json_result.stdout)[1] == {
        "id": "openrouter/text-only",
        "tool_call": False,
        "context": None,
        "cost": {"input": None, "output": None},
        "efforts": [],
    }
    assert enso_home.config.read_bytes() == original
    assert not enso_home.db.exists()


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_reports_fetch_errors(
    enso_home: Paths,
    monkeypatch: pytest.MonkeyPatch,
    as_json: bool,
) -> None:
    def offline(*args: object, **kwargs: object) -> None:
        raise OSError("offline")

    monkeypatch.setattr(models, "_open", offline)
    result = CliRunner().invoke(app, ["models", *(["--json"] if as_json else [])])

    assert result.exit_code == 1
    if as_json:
        assert json.loads(result.stdout) == {
            "ok": False,
            "error": "could not fetch models.dev catalog: offline",
        }
        assert result.stderr == ""
    else:
        assert result.stdout == ""
        assert result.stderr == "error: could not fetch models.dev catalog: offline\n"


def test_oversized_response_and_cache_are_not_loaded(
    enso_home: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    enso_home.models_cache.parent.mkdir(parents=True)
    with enso_home.models_cache.open("wb") as file:
        file.truncate(models.MAX_RESPONSE_BYTES + 1)
    monkeypatch.setattr(
        models,
        "_open",
        lambda *args, **kwargs: io.BytesIO(b"x" * (models.MAX_RESPONSE_BYTES + 1)),
    )

    with pytest.raises(models.ModelsError, match="16 MiB"):
        models.load(enso_home)


def test_slow_drip_response_stops_at_the_total_deadline_and_uses_a_stale_cache(
    enso_home: Paths,
    isolated_opencode_cache: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
) -> None:
    write_cache(isolated_opencode_cache, one_catalog("cached"), NOW - 5_000)
    drip = DripResponse(clock, seconds=1.0)
    monkeypatch.setattr(models, "_open", lambda *args, **kwargs: drip)
    started = clock.now

    catalog = models.load(enso_home)

    assert [model.id for model in catalog.models] == ["openrouter/cached"]
    assert catalog.warning == (
        "could not fetch models.dev catalog: exceeded the 15 second deadline; "
        f"using stale cache {isolated_opencode_cache}"
    )
    assert clock.now - started == models.FETCH_TIMEOUT_SECONDS
    assert drip.reads == models.FETCH_TIMEOUT_SECONDS


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_reports_the_deadline_when_no_cache_can_stand_in(
    enso_home: Paths,
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
    as_json: bool,
) -> None:
    monkeypatch.setattr(models, "_open", lambda *args, **kwargs: DripResponse(clock, seconds=1.0))
    result = CliRunner().invoke(app, ["models", *(["--json"] if as_json else [])])

    message = "could not fetch models.dev catalog: exceeded the 15 second deadline"
    assert result.exit_code == 1
    if as_json:
        assert json.loads(result.stdout) == {"ok": False, "error": message}
        assert result.stderr == ""
    else:
        assert result.stdout == ""
        assert result.stderr == f"error: {message}\n"


def test_every_receive_is_given_only_the_time_left(clock: Clock) -> None:
    """The status line and headers are read this way too, before any response exists."""
    client, server = socket.socketpair()
    stream = models._DeadlineSocket(client, clock.now + models.FETCH_TIMEOUT_SECONDS).makefile()
    with client, server, stream:
        server.sendall(b"HTTP/1.1 200 OK\r\n")
        assert stream.readline() == b"HTTP/1.1 200 OK\r\n"
        assert client.gettimeout() == models.FETCH_TIMEOUT_SECONDS

        clock.advance(9)
        server.sendall(b"Content-Length: 0\r\n")
        assert stream.readline() == b"Content-Length: 0\r\n"
        assert client.gettimeout() == models.FETCH_TIMEOUT_SECONDS - 9

        clock.advance(models.FETCH_TIMEOUT_SECONDS)
        server.sendall(b"\r\n")
        with pytest.raises(models.ModelsError, match="15 second deadline"):
            stream.readline()


def test_only_the_binary_read_mode_http_client_asks_for_is_served(clock: Clock) -> None:
    client, server = socket.socketpair()
    with client, server:
        sock = models._DeadlineSocket(client, clock.now + models.FETCH_TIMEOUT_SECONDS)
        with pytest.raises(ValueError, match="unsupported socket mode 'wb'"):
            sock.makefile("wb")


def test_redirect_hops_are_charged_to_the_fetch_deadline(clock: Clock) -> None:
    handler = models._DeadlineRedirectHandler(clock.now + models.FETCH_TIMEOUT_SECONDS)
    hops: list[float] = []
    handler.parent = SimpleNamespace(open=lambda request, timeout: hops.append(timeout))
    headers = HTTPMessage()
    headers["location"] = "https://models.dev/moved/api.json"
    clock.advance(9)

    handler.http_error_302(Request(models.CATALOG_URL), io.BytesIO(b""), 302, "Found", headers)
    assert hops == [models.FETCH_TIMEOUT_SECONDS - 9]

    clock.advance(models.FETCH_TIMEOUT_SECONDS)
    with pytest.raises(models.ModelsError, match="15 second deadline"):
        handler.http_error_302(Request(models.CATALOG_URL), io.BytesIO(b""), 302, "Found", headers)
    assert len(hops) == 1


class DripHandler(BaseHTTPRequestHandler):
    """A server that stays busy: one byte at a time, never idle, never finished."""

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "10000000")
        self.end_headers()
        with contextlib.suppress(OSError, ValueError):
            while True:
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.02)

    def log_message(self, *args: object) -> None:
        return None


def test_a_drip_feeding_server_is_cut_off_at_the_deadline(
    enso_home: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole mechanism over a real socket, on a deadline short enough to wait for."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), DripHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(models.time, "monotonic", REAL_MONOTONIC)
    monkeypatch.setattr(models, "FETCH_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(models, "CATALOG_URL", f"http://127.0.0.1:{server.server_port}/api.json")
    started = REAL_MONOTONIC()

    try:
        with pytest.raises(models.ModelsError, match=re.escape("exceeded the 0.5 second deadline")):
            models.load(enso_home)
    finally:
        server.shutdown()
        server.server_close()

    assert REAL_MONOTONIC() - started < 5


class SlowHeaderHandler(socketserver.StreamRequestHandler):
    """A server that answers, then feeds its status line one byte at a time, forever."""

    def handle(self) -> None:
        self.rfile.readline()
        with contextlib.suppress(OSError):
            while True:
                self.wfile.write(b"H")
                self.wfile.flush()
                time.sleep(0.02)


def test_a_server_dripping_its_headers_is_cut_off_at_the_deadline(
    enso_home: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Response headers arrive before any response object exists, on the same deadline."""
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), SlowHeaderHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(models.time, "monotonic", REAL_MONOTONIC)
    monkeypatch.setattr(models, "FETCH_TIMEOUT_SECONDS", 0.5)
    port = server.server_address[1]
    monkeypatch.setattr(models, "CATALOG_URL", f"http://127.0.0.1:{port}/api.json")
    started = REAL_MONOTONIC()

    try:
        with pytest.raises(models.ModelsError, match=re.escape("exceeded the 0.5 second deadline")):
            models.load(enso_home)
    finally:
        server.shutdown()
        server.server_close()

    assert REAL_MONOTONIC() - started < 5


def test_a_blocked_exchange_does_not_outlast_the_deadline(
    enso_home: Paths,
    isolated_opencode_cache: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A step that ignores its timeout entirely is abandoned rather than waited on."""
    write_cache(isolated_opencode_cache, one_catalog("cached"), NOW - 5_000)
    blocker = Blocker(OSError("released"))
    monkeypatch.setattr(models, "FETCH_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(models, "_open", blocker)
    started = REAL_MONOTONIC()

    try:
        catalog = models.load(enso_home)

        assert REAL_MONOTONIC() - started < 5
        assert blocker.entered.wait(5) and not blocker.release.is_set()
        assert [model.id for model in catalog.models] == ["openrouter/cached"]
        assert catalog.warning == (
            "could not fetch models.dev catalog: exceeded the 0.2 second deadline; "
            f"using stale cache {isolated_opencode_cache}"
        )
    finally:
        blocker.release.set()


def test_a_blocking_resolver_cannot_outlast_the_deadline(
    enso_home: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Name resolution takes no timeout, so only abandoning it keeps the deadline total."""
    blocker = Blocker(socket.gaierror("released"))
    monkeypatch.setattr(models.time, "monotonic", REAL_MONOTONIC)
    monkeypatch.setattr(models, "FETCH_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(models, "CATALOG_URL", "http://models.dev.invalid/api.json")
    monkeypatch.setattr(socket, "getaddrinfo", blocker)
    started = REAL_MONOTONIC()

    try:
        with pytest.raises(models.ModelsError, match=re.escape("exceeded the 0.5 second deadline")):
            models.load(enso_home)

        assert REAL_MONOTONIC() - started < 5
        assert blocker.entered.wait(5) and not blocker.release.is_set()
    finally:
        blocker.release.set()
