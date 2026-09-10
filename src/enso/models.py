"""OpenRouter model discovery through the models.dev catalog and compatible caches."""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
import re
import socket
import tempfile
import threading
import time
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException, HTTPMessage, HTTPSConnection
from pathlib import Path
from typing import IO, Any, NoReturn, cast
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
)

from .config import Paths

CATALOG_URL = "https://models.dev/api.json"
CACHE_TTL_SECONDS = 60 * 60
# The whole refresh, from name resolution to the last byte of the body, not an idle
# timeout that a slowly dribbling peer can keep resetting.
FETCH_TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024

type Number = int | float


class ModelsError(Exception):
    """The model catalog could not be loaded or did not match its public schema."""


@dataclass(frozen=True)
class Model:
    """The fields an operator needs to choose an OpenRouter model for OpenCode."""

    id: str
    tool_call: bool
    context: int | None
    input_cost: Number | None
    output_cost: Number | None
    efforts: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool_call": self.tool_call,
            "context": self.context,
            "cost": {"input": self.input_cost, "output": self.output_cost},
            "efforts": list(self.efforts),
        }


@dataclass(frozen=True)
class Catalog:
    """Parsed models plus a warning when a stale cache had to be used."""

    models: tuple[Model, ...]
    warning: str | None = None


@dataclass(frozen=True)
class _CachedCatalog:
    path: Path
    modified: float
    models: tuple[Model, ...]


def _invalid(location: str, expected: str) -> NoReturn:
    raise ModelsError(f"invalid models.dev catalog: {location} must be {expected}")


def _optional_cost(value: object, location: str) -> Number | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid(location, "a non-negative number or null")
    if value < 0 or (isinstance(value, float) and not math.isfinite(value)):
        _invalid(location, "a non-negative number or null")
    return value


def _context(entry: dict[str, Any], location: str) -> int | None:
    limit = entry.get("limit")
    if limit is None:
        return None
    if not isinstance(limit, dict):
        _invalid(f"{location}.limit", "an object or null")
    context = limit.get("context")
    if context is None:
        return None
    if isinstance(context, bool) or not isinstance(context, int) or context < 0:
        _invalid(f"{location}.limit.context", "a non-negative integer or null")
    return context


def _costs(entry: dict[str, Any], location: str) -> tuple[Number | None, Number | None]:
    cost = entry.get("cost")
    if cost is None:
        return None, None
    if not isinstance(cost, dict):
        _invalid(f"{location}.cost", "an object or null")
    return (
        _optional_cost(cost.get("input"), f"{location}.cost.input"),
        _optional_cost(cost.get("output"), f"{location}.cost.output"),
    )


def _reasoning(entry: dict[str, Any], location: str) -> bool:
    value = entry.get("reasoning")
    if value is None:
        return False
    if not isinstance(value, bool):
        _invalid(f"{location}.reasoning", "a boolean or null")
    return value


# OpenCode turns models.dev reasoning metadata into the variants it will accept for a
# model, and falls back to a per-family default when that metadata names none; the
# reference is its provider transform (OpenCode 1.18.26). Enso mirrors only the
# resulting names, because a name is what an operator configures as `effort`.
_BASE_EFFORTS = ("low", "medium", "high")
_BUDGET_EFFORTS = ("high", "max")
_OPENAI_EFFORTS = ("none", "minimal", *_BASE_EFFORTS, "xhigh")
_GPT5_EFFORTS = ("none", *_BASE_EFFORTS)
_GPT5_LATER_EFFORTS = (*_GPT5_EFFORTS, "xhigh")
_CODEX_EFFORTS = (*_BASE_EFFORTS, "xhigh")
_CODEX_LATER_EFFORTS = ("none", *_CODEX_EFFORTS)
_GLM_52_NAMES = ("glm-5.2", "glm-5-2", "glm-5p2")
# Families OpenCode routes through OpenRouter with no selectable variant at all.
_NO_EFFORT_NAMES = (
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-r1",
    "deepseek-v3",
    "minimax",
    "kimi",
    "k2p",
    "qwen",
    "big-pickle",
    "glm",
)
_GPT5 = re.compile(r"(?:^|/)gpt-5(?:[.-]|$)")
_GPT5_POINT = re.compile(r"(?:^|/)gpt-5[.-](\d+)(?:[.-]|$)")
_GPT5_PRO = re.compile(r"(?:^|/)gpt-5[.-]?pro(?:[.-]|$)")
_GPT5_POINT_PRO = re.compile(r"(?:^|/)gpt-5[.-]\d+[.-]pro(?:[.-]|$)")


def _gpt5_point_release(model_id: str) -> int | None:
    """The N in a gpt-5.N id, which OpenCode reads as a number and ignores when zero."""
    match = _GPT5_POINT.search(model_id)
    if match is None:
        return None
    return int(match[1]) or None


def _openai_efforts(model_id: str) -> tuple[str, ...]:
    """The variants OpenCode exposes for an OpenAI-family OpenRouter model."""
    gpt5 = _GPT5.search(model_id) is not None
    release = _gpt5_point_release(model_id)
    if gpt5 and "-chat" in model_id:
        return ("medium",) if release is not None else ()
    if _GPT5_PRO.search(model_id):
        return ("high",)
    if gpt5 and "codex" in model_id:
        if release is not None and release >= 3:
            return _CODEX_LATER_EFFORTS
        if "codex-max" in model_id or (release is not None and release >= 2):
            return _CODEX_EFFORTS
        return _BASE_EFFORTS
    if _GPT5_POINT_PRO.search(model_id):
        return ("medium", "high", "xhigh")
    if release is None:
        return _OPENAI_EFFORTS
    return _GPT5_EFFORTS if release == 1 else _GPT5_LATER_EFFORTS


def _family_efforts(model_id: str, reasoning: bool) -> tuple[str, ...]:
    """OpenCode's per-family default, used when reasoning metadata names no variant."""
    if not reasoning:
        return ()
    lowered = model_id.lower()
    if any(name in lowered for name in _GLM_52_NAMES):
        return ("high", "xhigh")
    if any(name in lowered for name in _NO_EFFORT_NAMES):
        return ()
    if "grok-3-mini" in lowered:
        return ("low", "high")
    if lowered.startswith("openai/") or "gpt" in lowered:
        return _openai_efforts(lowered)
    return _BASE_EFFORTS


def _efforts(entry: dict[str, Any], model_id: str, location: str) -> tuple[str, ...]:
    """The variant names OpenCode accepts for this model, in the order it lists them."""
    reasoning = _reasoning(entry, location)
    options = entry.get("reasoning_options")
    if options is None:
        return _family_efforts(model_id, reasoning)
    if not isinstance(options, list):
        _invalid(f"{location}.reasoning_options", "an array or null")
    if not options:
        return ()
    listed: tuple[str, ...] | None = None
    budget = False
    for index, option in enumerate(options):
        option_location = f"{location}.reasoning_options[{index}]"
        if not isinstance(option, dict):
            _invalid(option_location, "an object")
        option_type = option.get("type")
        if not isinstance(option_type, str):
            _invalid(f"{option_location}.type", "a string")
        if option_type == "budget_tokens":
            budget = True
            continue
        if option_type != "effort":
            continue
        values = option.get("values")
        if not isinstance(values, list) or any(
            value is not None and (not isinstance(value, str) or not value) for value in values
        ):
            _invalid(f"{option_location}.values", "an array of non-empty strings or null")
        if listed is None:
            # OpenCode reads the first effort list only, and spells a null value `none`.
            listed = tuple("none" if value is None else value for value in values)
    if listed is not None:
        return listed
    if budget:
        # A thinking-token budget becomes exactly these two named variants.
        return _BUDGET_EFFORTS
    # Toggle-only or unrecognised metadata leaves OpenCode on its family default.
    return _family_efforts(model_id, reasoning)


def parse_catalog(raw: object) -> tuple[Model, ...]:
    """Validate and reduce the models.dev root document to sorted OpenRouter rows."""
    if not isinstance(raw, dict):
        _invalid("root", "an object")
    provider = raw.get("openrouter")
    if not isinstance(provider, dict):
        _invalid("openrouter", "an object")
    entries = provider.get("models")
    if not isinstance(entries, dict) or not entries:
        _invalid("openrouter.models", "a non-empty object")

    parsed: list[Model] = []
    for model_id, entry in entries.items():
        if not isinstance(model_id, str) or not model_id:
            _invalid("an openrouter.models key", "a non-empty string")
        location = f"openrouter.models[{model_id!r}]"
        if not isinstance(entry, dict):
            _invalid(location, "an object")
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            _invalid(f"{location}.id", "a non-empty string")
        if entry_id != model_id:
            raise ModelsError(
                f"invalid models.dev catalog: {location}.id does not match its models key"
            )
        tool_call = entry.get("tool_call")
        if not isinstance(tool_call, bool):
            _invalid(f"{location}.tool_call", "a boolean")
        input_cost, output_cost = _costs(entry, location)
        parsed.append(
            Model(
                id=f"openrouter/{entry_id}",
                tool_call=tool_call,
                context=_context(entry, location),
                input_cost=input_cost,
                output_cost=output_cost,
                efforts=_efforts(entry, entry_id, location),
            )
        )
    return tuple(sorted(parsed, key=lambda model: model.id))


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"invalid numeric constant {value}")


def _decode(raw: bytes) -> tuple[Model, ...]:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ModelsError("models.dev catalog exceeds the 16 MiB limit")
    try:
        document = json.loads(raw, parse_constant=_reject_constant)
    except (UnicodeError, ValueError) as exc:
        raise ModelsError(f"invalid models.dev catalog: not valid JSON ({exc})") from exc
    return parse_catalog(document)


def _opencode_cache_path() -> Path:
    cache_home = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    return Path(cache_home).expanduser() / "opencode" / "models.json"


def _read_cache(path: Path) -> _CachedCatalog | None:
    try:
        stat = path.stat()
        if stat.st_size > MAX_RESPONSE_BYTES:
            return None
        raw = path.read_bytes()
        models = _decode(raw)
    except OSError, ModelsError:
        return None
    return _CachedCatalog(path, stat.st_mtime, models)


def _expired() -> ModelsError:
    return ModelsError(
        f"could not fetch models.dev catalog: exceeded the {FETCH_TIMEOUT_SECONDS} second deadline"
    )


def _remaining(deadline: float) -> float:
    """The seconds left in the fetch budget, refusing to start a step once none are."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _expired()
    return remaining


class _DeadlineRedirectHandler(HTTPRedirectHandler):
    """Charge each redirect hop to the fetch deadline instead of restarting its timeout.

    `urllib` reopens a redirect with the original request's timeout, so a chain of slow
    hops can outlive the deadline many times over. Shrinking the timeout to what is left
    keeps the whole chain inside one budget.
    """

    def __init__(self, deadline: float) -> None:
        self.deadline = deadline

    def http_error_302(
        self, req: Request, fp: IO[bytes], code: int, msg: str, headers: HTTPMessage
    ) -> Any:
        req.timeout = _remaining(self.deadline)
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


class _DeadlineSocket:
    """A socket that re-arms its timeout with the time left before every receive.

    A socket timeout bounds one blocking call, so a peer sending a byte at a time resets it
    on every packet and never trips it however long the transfer runs. Re-arming with what
    is left of the budget is what makes those per-call timeouts add up to one deadline, and
    doing it on the socket covers the status line and headers `http.client` reads before any
    response object exists as well as the body read afterwards.
    """

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self._sock = sock
        self._deadline = deadline

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sock, name)

    def recv_into(self, buffer: Any, *args: Any) -> int:
        self._sock.settimeout(_remaining(self._deadline))
        return self._sock.recv_into(buffer, *args)

    def makefile(self, mode: str = "rb", *args: Any, **kwargs: Any) -> IO[bytes]:
        """The reader `http.client` wraps a response in, receiving through the deadline.

        `socket.makefile` would bind its reader to the socket underneath, which receives
        without ever consulting the deadline. Only the binary read mode `http.client` asks
        for is served, so a caller wanting anything else fails rather than losing the bound.
        """
        if mode != "rb":
            raise ValueError(f"unsupported socket mode {mode!r}")
        underlying: Any = self._sock
        reader = io.BufferedReader(socket.SocketIO(cast("socket.socket", self), "rb"))
        # As `socket.makefile` does: the socket is not really closed until its readers are.
        underlying._io_refs += 1
        return reader


def _deadline_connection(base: Any, deadline: float) -> Any:
    """A connection class of `base` whose socket charges every receive to `deadline`.

    The socket is wrapped once `connect()` has returned, because a TLS handshake replaces
    the socket it was handed and would otherwise discard the wrapper.
    """

    class Connection(base):
        sock: Any

        def connect(self) -> None:
            super().connect()
            self.sock = _DeadlineSocket(self.sock, deadline)

    return Connection


class _DeadlineHTTPHandler(HTTPHandler):
    """Open plain HTTP through a connection bound to the fetch deadline."""

    def __init__(self, deadline: float) -> None:
        super().__init__()
        self._connection = _deadline_connection(HTTPConnection, deadline)

    def http_open(self, req: Request) -> Any:
        return self.do_open(self._connection, req)


class _DeadlineHTTPSHandler(HTTPSHandler):
    """The same for HTTPS, which also hands the connection its TLS context."""

    def __init__(self, deadline: float) -> None:
        super().__init__()
        self._connection = _deadline_connection(HTTPSConnection, deadline)

    def https_open(self, req: Request) -> Any:
        # `HTTPSHandler.https_open` reads the same private attribute for the context it
        # built in `__init__`; there is no public accessor to read it from.
        return self.do_open(self._connection, req, context=self._context)  # type: ignore[attr-defined]


def _open(request: Request, deadline: float) -> Any:
    """Send the request with the time left, redirects and header reads included."""
    opener = build_opener(
        _DeadlineHTTPHandler(deadline),
        _DeadlineHTTPSHandler(deadline),
        _DeadlineRedirectHandler(deadline),
    )
    return opener.open(request, timeout=_remaining(deadline))


def _read_within(response: Any, deadline: float) -> bytes:
    """Read one byte past the size cap at most, with every read bounded by the deadline."""
    chunks: list[bytes] = []
    allowed = MAX_RESPONSE_BYTES + 1
    while allowed > 0:
        # The socket bounds each receive; this refuses to start a read with nothing left,
        # and `read1` returns what has already arrived rather than waiting for a full chunk.
        _remaining(deadline)
        chunk = response.read1(min(READ_CHUNK_BYTES, allowed))
        if not chunk:
            break
        chunks.append(chunk)
        allowed -= len(chunk)
    return b"".join(chunks)


class _Exchange(threading.Thread):
    """The request and its response, run where the caller can stop waiting for them.

    Timeouts only bound steps that are waiting on a socket. Name resolution is not: it
    blocks in C with no timeout to hand it, so any clock check runs after `getaddrinfo`
    has already returned and cannot cut it short. Leaving the exchange on a thread of its
    own is what makes the deadline total, because the caller can abandon it. The socket
    deadline still applies in here, so an abandoned exchange stops on its own rather than
    holding a connection open behind a command that has already reported the timeout.
    """

    def __init__(self, request: Request, deadline: float) -> None:
        super().__init__(name="enso-models-fetch", daemon=True)
        self._request = request
        self._deadline = deadline
        self.raw = b""
        self.failure: BaseException | None = None

    def run(self) -> None:
        try:
            with _open(self._request, self._deadline) as response:
                self.raw = _read_within(response, self._deadline)
        # Anything at all is carried back, for the calling thread to raise as its own.
        except BaseException as exc:
            self.failure = exc


def _receive(request: Request, deadline: float) -> bytes:
    """The whole response body, or a `ModelsError` once the deadline has passed."""
    exchange = _Exchange(request, deadline)
    budget = _remaining(deadline)
    exchange.start()
    exchange.join(budget)
    if exchange.is_alive():
        raise _expired()
    failure = exchange.failure
    if isinstance(failure, (OSError, HTTPException)):
        # Every timeout in this fetch is the time that was left, so a failure at or past
        # the deadline is the deadline rather than an ordinary network error.
        if time.monotonic() >= deadline:
            raise _expired() from failure
        raise ModelsError(f"could not fetch models.dev catalog: {failure}") from failure
    if failure is not None:
        raise failure
    return exchange.raw


def _fetch() -> tuple[tuple[Model, ...], bytes]:
    request = Request(
        CATALOG_URL,
        headers={"Accept": "application/json", "User-Agent": "enso-models"},
    )
    deadline = time.monotonic() + FETCH_TIMEOUT_SECONDS
    raw = _receive(request, deadline)
    return _decode(raw), raw


def _write_cache(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as file:
            temporary = Path(file.name)
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


def load(paths: Paths) -> Catalog:
    """Use the newest fresh compatible cache, otherwise refresh with stale fallback."""
    cached = [
        catalog
        for path in (paths.models_cache, _opencode_cache_path())
        if (catalog := _read_cache(path)) is not None
    ]
    now = time.time()
    fresh = [catalog for catalog in cached if now - catalog.modified <= CACHE_TTL_SECONDS]
    if fresh:
        return Catalog(max(fresh, key=lambda catalog: catalog.modified).models)

    try:
        models, raw = _fetch()
    except ModelsError as exc:
        if not cached:
            raise
        stale = max(cached, key=lambda catalog: catalog.modified)
        return Catalog(stale.models, f"{exc}; using stale cache {stale.path}")

    try:
        _write_cache(paths.models_cache, raw)
    except OSError as exc:
        return Catalog(models, f"could not write model cache {paths.models_cache}: {exc}")
    return Catalog(models)
