"""Expiring, private owner-pairing attempts for native and hosted onboarding.

One child holds the receive lock across credential checks and chat pairing. Short
CLI calls inspect atomic snapshots, so refreshing a wizard cannot create a second
poller. This module imports no optional transport dependency until the worker runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import locks
from .config import (
    CONFIG_VERSION,
    ConfigConflictError,
    ConfigError,
    Paths,
    check_config,
    config_fingerprint,
    config_lock,
    read_raw_config,
)
from .initialization import apply_config, initialize_home
from .providers import PROVIDER_CLASSES
from .transport_registry import TRANSPORTS
from .transports.connection import PairedIdentity, PairingError, PairingRequest, Ready

ATTEMPT_SECONDS = 300
MAX_INPUT = 16384
CREDENTIAL_KEYS = frozenset(c.key for spec in TRANSPORTS.values() for c in spec.credentials)
ACTIVE_STATES = frozenset({"verifying", "waiting"})
PUBLIC_FIELDS = (
    "attempt_id",
    "request_id",
    "transport",
    "state",
    "bot_name",
    "workspace_name",
    "open_url",
    "instruction",
    "expires_at",
    "error",
    "error_code",
    "defaults",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _prepare(paths: Paths) -> None:
    for directory in (paths.home, paths.cache, paths.connection_dir):
        if directory.is_symlink():
            raise PairingError("storage_error", "The connection directory must not be a link.")
        directory.mkdir(parents=True, exist_ok=True)
    paths.connection_dir.chmod(0o700)


def _read(path: Path) -> dict[str, Any]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, "r", encoding="utf-8") as file:
        text = file.read(65537)
    if len(text) > 65536:
        raise PairingError("storage_error", "Connection state is too large.")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise PairingError("storage_error", "Connection state could not be read.")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".connect-", delete=False
        ) as file:
            temporary = Path(file.name)
            os.fchmod(file.fileno(), 0o600)
            json.dump(value, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _open_lock(path: Path, *, create: bool = True) -> int:
    try:
        return locks.open_lock(path, create=create)
    except locks.LockPathError:
        raise PairingError(
            "storage_error", "The connection lock must be a regular file, not a link."
        ) from None


@contextlib.contextmanager
def _control(paths: Paths) -> Iterator[None]:
    _prepare(paths)
    fd = _open_lock(paths.connection_dir / "control.lock")
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PairingError("busy", "Another setup change is in progress. Try again.") from None
        yield
    finally:
        os.close(fd)


def _held(path: Path) -> bool:
    """Whether someone holds this advisory lock; a file nobody created is nobody's lock."""
    try:
        fd = _open_lock(path, create=False)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(fd)


def receiver_active(paths: Paths) -> bool:
    """A receive lock is authoritative; stale PID files never establish a live worker."""
    return _held(paths.connection_dir / "worker.lock")


def _lock_owner(paths: Paths) -> dict[str, Any]:
    """The tag the receive lock's holder wrote: the attempt ``start`` launched, or the
    service's mark. ``start`` tags the lock under the control and config locks, so a reader
    holding either never sees the tag mid-write; content Enso did not write is nobody's attempt.
    """
    try:
        return _read(paths.connection_dir / "worker.lock")
    except PairingError, ValueError:
        return {}


def _attempt_live(paths: Paths, state: dict[str, Any]) -> bool:
    """Whether the hosted attempt recorded in ``state`` still holds the receive lock.

    A held lock alone proves nothing about it: ``enso serve`` holds the same lock, tagged as
    the service, after a worker died without recording a terminal state.
    """
    attempt = state.get("attempt_id")
    return (
        state.get("state") in ACTIVE_STATES
        and attempt is not None
        and receiver_active(paths)
        and _lock_owner(paths).get("attempt_id") == attempt
    )


def pairing_active(paths: Paths) -> bool:
    """Whether a pairing attempt is live: the native wizard, which keeps the setup control
    lock for as long as its receiver waits, or a hosted attempt whose own worker still holds
    the receive lock. The service holding that lock is not a pairing.

    Raises when the recorded state cannot be read, so a caller that must not race a
    pairing can refuse instead of guessing.
    """
    state = _state(paths)
    native = receiver_active(paths) and _held(paths.connection_dir / "control.lock")
    return native or _attempt_live(paths, state)


@contextlib.contextmanager
def service_receiver(paths: Paths) -> Iterator[None]:
    """Exclude pairing and duplicate servers until every transport has stopped."""
    _prepare(paths)
    fd = _open_lock(paths.connection_dir / "worker.lock")
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PairingError(
                "busy", "Enso or a pairing connection is already running for this home."
            ) from None
        os.ftruncate(fd, 0)
        os.write(fd, b'{"kind":"service"}')
        yield
    finally:
        os.close(fd)


def _state(paths: Paths, attempt_id: str | None = None) -> dict[str, Any]:
    state = _read(paths.connection_dir / "state.json")
    if attempt_id and state.get("attempt_id") != attempt_id:
        raise PairingError("not_found", "This connection attempt no longer exists.")
    return state


def _terminal(paths: Paths, state: dict[str, Any], status: str, error: PairingError) -> None:
    state.update(state=status, error=str(error), error_code=error.code, open_url="", instruction="")
    state.pop("nonce", None)
    (paths.connection_dir / "credentials.json").unlink(missing_ok=True)
    _write(paths.connection_dir / "state.json", state)


def snapshot(paths: Paths, attempt_id: str | None = None) -> dict[str, Any]:
    """Read a sanitized result; interrupted receivers are repairable by a fresh attempt."""
    state = _state(paths, attempt_id)
    if state.get("state") in ACTIVE_STATES and not _attempt_live(paths, state):
        with _control(paths):
            state = _state(paths, attempt_id)
            if state.get("state") in ACTIVE_STATES and not _attempt_live(paths, state):
                expired = time.time() >= state.get("expires_timestamp", 0)
                _terminal(
                    paths,
                    state,
                    "expired" if expired else "failed",
                    PairingError(
                        "expired" if expired else "connection_failed",
                        "This connection attempt ended. Start a new connection.",
                    ),
                )
    config, _problems, _warnings = check_config(paths)
    fingerprint = config_fingerprint(paths)
    reply = _read(paths.connection_dir / "reply.json")
    if reply.get("config_hash") != fingerprint or reply.get("attempt_id") != state.get(
        "attempt_id"
    ):
        reply = {}
    public = {key: state[key] for key in PUBLIC_FIELDS if key in state} if state else None
    if public and state.get("state") == "paired" and receiver_active(paths):
        public.update(state="verifying", open_url="", instruction="")
    return {
        "version": 1,
        "ok": True,
        "connection": public,
        "config_valid": config is not None,
        "config_hash": fingerprint,
        "first_reply": reply or None,
    }


def _credentials(transport: str, raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) - {"request_id", *CREDENTIAL_KEYS}:
        raise PairingError("invalid_input", "Supply request_id and the selected bot credentials.")
    request_id = raw.get("request_id")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_id):
        raise PairingError("invalid_input", "Supply a unique request_id of 16-128 characters.")
    spec = TRANSPORTS.get(transport)
    if spec is None:
        names = " or ".join(name.capitalize() for name in TRANSPORTS)
        raise PairingError("invalid_input", f"Choose {names}.")
    keys = [credential.key for credential in spec.credentials]
    if any(raw.get(key) for key in set(raw) - {"request_id", *keys}):
        wanted = " and ".join(key.replace("_", " ") for key in keys)
        raise PairingError("invalid_input", f"{transport.capitalize()} needs only its {wanted}.")
    for key in keys:
        value = raw.get(key)
        if (
            not isinstance(value, str)
            or not 8 <= len(value) <= 4096
            or any(c.isspace() for c in value)
        ):
            raise PairingError(
                "invalid_input", "Paste the complete bot credentials without spaces."
            )
    credentials = {key: raw[key] for key in keys}
    return {  # PairingRequest's fields: the contract both receivers share
        "request_id": request_id,
        "bot_token": credentials["bot_token"],
        "app_token": credentials.get("app_token", ""),
    }


def pair_in_terminal(
    paths: Paths, transport: str, raw: dict[str, str], ready: Ready
) -> PairedIdentity:
    """Use the same challenged receiver in the native wizard without persisting tokens.

    Hold the setup control lock until the receiver closes. Other setup commands may
    inspect status but cannot cancel this process or start another receiver.
    """
    credentials = _credentials(transport, {**raw, "request_id": uuid.uuid4().hex})
    with _control(paths):
        fd = _open_lock(paths.connection_dir / "worker.lock")
        try:
            with config_lock(paths):
                if paths.config.exists():
                    raise PairingError("already_configured", "This home is already configured.")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise PairingError("busy", "A connection is already waiting.") from None
            request = PairingRequest(
                credentials["bot_token"],
                credentials["app_token"],
                secrets.token_urlsafe(24),
                time.time(),
            )

            async def receive() -> PairedIdentity:
                async def claim() -> None:
                    pass  # The sequential receiver returns after its one matching message.

                pair = TRANSPORTS[transport].pair()
                async with asyncio.timeout(ATTEMPT_SECONDS):
                    return await pair(request, ready, claim)

            try:
                return asyncio.run(receive())
            except ImportError:
                raise PairingError(
                    "missing_extra", f"Install enso[{transport}] to connect this bot."
                ) from None
            except TimeoutError:
                raise PairingError(
                    "expired", "The pairing code expired. Run enso setup again."
                ) from None
        finally:
            os.close(fd)


def start(paths: Paths, transport: str, raw: object) -> dict[str, Any]:
    """Create at most one expiring worker; identical requests recover a lost response."""
    credentials = _credentials(transport, raw)
    if not TRANSPORTS[transport].installed():
        raise PairingError("missing_extra", f"Install enso[{transport}] to connect this bot.")
    prepared = initialize_home(paths)
    if not prepared["ok"]:
        raise PairingError("storage_error", "Run enso init to resolve the home layout first.")
    digest = hashlib.sha256(
        json.dumps({**credentials, "transport": transport}, sort_keys=True).encode()
    ).hexdigest()
    with _control(paths), config_lock(paths):
        previous = _state(paths)
        if previous.get("request_id") == credentials["request_id"]:
            if previous.get("request_hash") != digest:
                raise PairingError(
                    "conflict", "This request was already used with different credentials."
                )
            # Snapshot reconciliation takes the control lock only for a dead active worker.
            if previous.get("state") in ACTIVE_STATES and not _attempt_live(paths, previous):
                _terminal(
                    paths,
                    previous,
                    "failed",
                    PairingError("connection_failed", "Start a new connection attempt."),
                )
            return snapshot(paths)
        if paths.config.exists():
            raise PairingError(
                "already_configured",
                "This home is configured. Existing connections were preserved.",
            )
        fd = _open_lock(paths.connection_dir / "worker.lock")
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PairingError(
                    "busy", "A connection is already waiting. Resume or cancel it."
                ) from None
            created = time.time()
            state: dict[str, Any] = {
                "attempt_id": str(uuid.uuid4()),
                "request_id": credentials["request_id"],
                "request_hash": digest,
                "transport": transport,
                "state": "verifying",
                "nonce": secrets.token_urlsafe(24),
                "created_timestamp": created,
                "expires_timestamp": created + ATTEMPT_SECONDS,
                "expires_at": datetime.fromtimestamp(created + ATTEMPT_SECONDS, UTC).isoformat(),
                "bot_name": "",
                "workspace_name": "",
                "open_url": "",
                "instruction": "",
            }
            _write(paths.connection_dir / "credentials.json", credentials)
            _write(paths.connection_dir / "state.json", state)
            try:
                child = subprocess.Popen(
                    [sys.executable, "-m", "enso.connection_setup", state["attempt_id"], str(fd)],
                    cwd=paths.home,
                    env={**os.environ, "ENSO_HOME": str(paths.home.resolve())},
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    pass_fds=(fd,),
                )
                os.ftruncate(fd, 0)
                os.write(
                    fd, json.dumps({"pid": child.pid, "attempt_id": state["attempt_id"]}).encode()
                )
            except OSError:
                _terminal(
                    paths,
                    state,
                    "failed",
                    PairingError("connection_failed", "The connection could not start."),
                )
                raise PairingError("connection_failed", "The connection could not start.") from None
        finally:
            os.close(fd)
    return snapshot(paths)


async def _pair_worker(paths: Paths, state: dict[str, Any]) -> None:
    credentials = _read(paths.connection_dir / "credentials.json")
    request = PairingRequest(
        credentials["bot_token"],
        credentials["app_token"],
        state["nonce"],
        state["created_timestamp"],
    )

    async def ready(info: dict[str, str]) -> None:
        state.update(info, state="waiting")
        state["chat_url"] = info["open_url"].split("?start=", 1)[0]
        await asyncio.to_thread(_write, paths.connection_dir / "state.json", state)

    async def claim() -> None:
        if state.get("consumed"):
            raise PairingError("conflict", "This pairing code has already been used.")
        state.update(consumed=True, instruction="", open_url="")
        await asyncio.to_thread(_write, paths.connection_dir / "state.json", state)

    pair = TRANSPORTS[state["transport"]].pair()
    try:
        async with asyncio.timeout(max(0, state["expires_timestamp"] - time.time())):
            identity: PairedIdentity = await pair(request, ready, claim)
        state.update(
            state="paired",
            user_id=identity.user_id,
            channel=identity.channel,
            instruction="",
            open_url=state["chat_url"],
        )
        state.pop("nonce", None)
        await asyncio.to_thread(_write, paths.connection_dir / "state.json", state)
    except asyncio.CancelledError:
        _terminal(paths, state, "cancelled", PairingError("cancelled", "Connection cancelled."))
    except TimeoutError:
        _terminal(
            paths,
            state,
            "expired",
            PairingError("expired", "The pairing code expired. Connect again."),
        )
    except PairingError as exc:
        _terminal(paths, state, "failed", exc)
    except ImportError:
        _terminal(
            paths,
            state,
            "failed",
            PairingError(
                "missing_extra", f"Install enso[{state['transport']}] to connect this bot."
            ),
        )
    except Exception:
        _terminal(
            paths,
            state,
            "failed",
            PairingError("connection_failed", "The connection failed. Try connecting again."),
        )


def worker(paths: Paths, attempt_id: str, fd: int) -> None:
    """Run with the inherited receive lock; SIGTERM unwinds the transport before release."""
    logging.disable(logging.CRITICAL)
    state = _state(paths, attempt_id)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        assert task is not None
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        try:
            await _pair_worker(paths, state)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)

    try:
        asyncio.run(run())
    finally:
        os.close(fd)


def cancel(paths: Paths, attempt_id: str | None = None) -> dict[str, Any]:
    with _control(paths):
        state = _state(paths, attempt_id)
        if not state or state.get("state") not in ACTIVE_STATES:
            return snapshot(paths)
        # A held lock this attempt did not tag is the service, which took the lock over
        # after the worker died: there is nothing to signal, only a state to close.
        owner = _lock_owner(paths) if receiver_active(paths) else {}
        if owner.get("attempt_id") == state["attempt_id"] and isinstance(owner.get("pid"), int):
            with contextlib.suppress(ProcessLookupError):
                os.kill(owner["pid"], signal.SIGTERM)
            deadline = time.monotonic() + 3
            while receiver_active(paths) and time.monotonic() < deadline:
                time.sleep(0.025)
            if receiver_active(paths):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(owner["pid"], signal.SIGKILL)
                deadline = time.monotonic() + 2
                while receiver_active(paths) and time.monotonic() < deadline:
                    time.sleep(0.025)
            if receiver_active(paths):
                raise PairingError("busy", "The connection is still stopping. Try again.")
        state = _state(paths, attempt_id)
        _terminal(paths, state, "cancelled", PairingError("cancelled", "Connection cancelled."))
    return snapshot(paths)


def _agent(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"provider", "model", "effort"}:
        raise PairingError("invalid_input", "Choose a provider, model and thinking effort.")
    if any(not isinstance(v, str) for v in raw.values()) or raw["provider"] not in PROVIDER_CLASSES:
        raise PairingError("invalid_input", "Choose a supported provider.")
    cls = PROVIDER_CLASSES[raw["provider"]]
    if (
        raw["model"] not in cls.models
        or raw["effort"] not in cls.effort_levels
        or cls.clamp_effort(raw["effort"], raw["model"]) != raw["effort"]
    ):
        raise PairingError("invalid_input", "Choose a supported model and thinking effort.")
    return dict(raw)


def initial_config(
    transport: str, credentials: Mapping[str, str], owner: PairedIdentity
) -> dict[str, Any]:
    """A first config.json: the paired transport, its owner's private chat bound to ``default``."""
    spec = TRANSPORTS[transport]
    binding = spec.binding_key(owner.channel, is_dm=True, user_id=owner.user_id)
    return {
        "version": CONFIG_VERSION,
        "transports": {transport: spec.config_entry(credentials, owner)},
        "bindings": {binding: "default"},
    }


def _configuration(paths: Paths, state: dict[str, Any], defaults: dict[str, str]) -> dict[str, Any]:
    if state["state"] == "applied":
        raw = read_raw_config(paths)
    else:
        credentials = _read(paths.connection_dir / "credentials.json")
        owner = PairedIdentity(state["user_id"], state["channel"])
        raw = initial_config(state["transport"], credentials, owner)
    name = defaults["provider"]
    cls = PROVIDER_CLASSES[name]
    raw.setdefault("providers", {}).setdefault(
        name,
        {"path": shutil.which(name) or name, "models": cls.models, "args": cls.unattended_args},
    )
    raw["defaults"] = defaults
    return raw


def finish(paths: Paths, payload: object) -> dict[str, Any]:
    """Apply paired identity and defaults locally; repeat or revise defaults with a hash guard."""
    if not isinstance(payload, dict) or set(payload) != {"attempt_id", "defaults", "expected_hash"}:
        raise PairingError("invalid_input", "Supply attempt_id, defaults and expected_hash.")
    defaults = _agent(payload["defaults"])
    if not isinstance(payload["attempt_id"], str) or not payload["attempt_id"]:
        raise PairingError("invalid_input", "Supply the connection attempt ID.")
    if not isinstance(payload["expected_hash"], str):
        raise PairingError("invalid_input", "Supply the expected configuration hash.")
    with _control(paths):
        state = _state(paths, payload["attempt_id"])
        if state.get("state") not in ("paired", "applied"):
            raise PairingError("not_paired", "Connect your chat account before applying setup.")
        fingerprint = config_fingerprint(paths)
        if (
            state.get("state") == "applied"
            and state.get("config_hash") == fingerprint
            and state.get("defaults") == defaults
        ):
            (paths.connection_dir / "credentials.json").unlink(missing_ok=True)
            return snapshot(paths)
        if receiver_active(paths):
            raise PairingError("busy", "Stop Enso before applying changed setup choices.")
        recovering = (
            state.get("pending_hash") == fingerprint and state.get("pending_defaults") == defaults
        )
        if state["state"] == "paired" and fingerprint != "missing" and not recovering:
            raise PairingError("conflict", "A configuration already exists. It was preserved.")
        if payload["expected_hash"] != fingerprint and not recovering:
            raise PairingError(
                "conflict", "Configuration changed. Reload setup before applying it."
            )
        raw = _configuration(paths, state, defaults)
        # Record intent before the atomic write so a lost process/SSH response can be reconciled.
        encoded = (json.dumps(raw, indent=2) + "\n").encode()
        state.update(pending_hash=hashlib.sha256(encoded).hexdigest(), pending_defaults=defaults)
        _write(paths.connection_dir / "state.json", state)
        result = apply_config(paths, raw, expected_hash=fingerprint)
        if not result["ok"]:
            raise PairingError(
                "config_invalid", "Configuration could not be applied. Check enso config check."
            )
        state.update(state="applied", defaults=defaults, config_hash=result["config_hash"])
        _write(paths.connection_dir / "state.json", state)
        (paths.connection_dir / "credentials.json").unlink(missing_ok=True)
    return snapshot(paths)


def record_reply(
    paths: Paths,
    defaults: dict[str, str],
    transport: str,
    user_id: str,
    channel: str,
    runtime_hash: str,
) -> None:
    """Record only a delivered, successful reply for this onboarding owner and config."""
    state = _state(paths)
    if (
        state.get("state") != "applied"
        or state.get("defaults") != defaults
        or state.get("transport") != transport
        or state.get("user_id") != user_id
        or state.get("channel") != channel
        or state.get("config_hash") != runtime_hash
        or config_fingerprint(paths) != runtime_hash
    ):
        return
    _write(
        paths.connection_dir / "reply.json",
        {
            "attempt_id": state["attempt_id"],
            **defaults,
            "transport": transport,
            "user_id": user_id,
            "channel": channel,
            "at": _now(),
            "config_hash": runtime_hash,
        },
    )


def safe_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, PairingError):
        error, code = str(exc), exc.code
    elif isinstance(exc, ConfigConflictError):
        error, code = "Configuration is busy or changed. Reload setup and retry.", "conflict"
    elif isinstance(exc, ConfigError):
        error, code = "Configuration could not be read. Run enso config check.", "config_invalid"
    else:
        error, code = (
            "Connection state could not be saved. Check home permissions and retry.",
            "storage_error",
        )
    return {"version": 1, "ok": False, "error": error, "error_code": code}


if __name__ == "__main__":
    worker(Paths.from_env(), sys.argv[1], int(sys.argv[2]))
