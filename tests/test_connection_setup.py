"""Connection attempts remain private, single-owner, retryable, and revision guarded."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from enso import connection_setup as setup
from enso.cli import app
from enso.config import config_fingerprint, load_config, save_config
from enso.initialization import apply_config, initialize_home
from enso.transports.connection import PairedIdentity, PairingError

TOKENS = {"request_id": "request_1234567890", "bot_token": "123456:private-bot-token"}
DEFAULTS = {"provider": "claude", "model": "opus", "effort": "high"}


@pytest.fixture
def child_process(monkeypatch):
    """Retain a duplicate of the inherited lock, without spawning or signalling a process."""
    state = SimpleNamespace(fd=None, starts=[], signals=[])

    def close():
        if state.fd is not None:
            os.close(state.fd)
            state.fd = None

    def start(argv, **kwargs):
        assert state.fd is None
        state.fd = os.dup(kwargs["pass_fds"][0])
        state.starts.append((argv, kwargs))
        return SimpleNamespace(pid=424242)

    def kill(pid, sig):
        assert pid == 424242
        state.signals.append(sig)
        close()

    state.close = close
    monkeypatch.setattr(
        setup, "subprocess", SimpleNamespace(Popen=start, DEVNULL=setup.subprocess.DEVNULL)
    )
    monkeypatch.setattr(setup.os, "kill", kill)
    yield state
    close()


def mark_paired(paths, child_process):
    state = setup._state(paths)
    child_process.close()
    state.update(
        state="paired",
        user_id="123",
        channel="123",
        bot_name="enso_test_bot",
        open_url="https://t.me/enso_test_bot",
        instruction="",
    )
    state.pop("nonce", None)
    setup._write(paths.connection_dir / "state.json", state)
    return state


def finish_payload(state, *, defaults=None, fingerprint="missing"):
    return {
        "attempt_id": state["attempt_id"],
        "defaults": defaults or DEFAULTS,
        "expected_hash": fingerprint,
    }


def test_cli_accepts_attempt_id_for_status_and_cancel(enso_home, child_process):
    """Hosted cancellation passes the ID positionally, including after a failed attempt."""
    started = setup.start(enso_home, "telegram", TOKENS)
    attempt = started["connection"]["attempt_id"]
    runner = CliRunner()
    status = runner.invoke(app, ["connect", "status", attempt, "--json"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.stdout)["connection"]["attempt_id"] == attempt
    cancelled = runner.invoke(app, ["connect", "cancel", attempt, "--json"])
    assert cancelled.exit_code == 0, cancelled.output
    assert json.loads(cancelled.stdout)["connection"]["state"] == "cancelled"
    assert not setup.receiver_active(enso_home)
    repeated = runner.invoke(app, ["connect", "cancel", attempt, "--json"])
    assert repeated.exit_code == 0, repeated.output
    missing = runner.invoke(app, ["connect", "status", "old-attempt", "--json"])
    assert missing.exit_code == 1
    assert json.loads(missing.stdout)["error_code"] == "not_found"


def test_start_is_idempotent_and_never_exposes_tokens_or_unready_nonce(enso_home, child_process):
    first = setup.start(enso_home, "telegram", TOKENS)
    again = setup.start(enso_home, "telegram", TOKENS)
    assert first["connection"]["attempt_id"] == again["connection"]["attempt_id"]
    assert len(child_process.starts) == 1 and setup.receiver_active(enso_home)
    private = setup._state(enso_home)
    public = json.dumps(first)
    assert TOKENS["bot_token"] not in public and private["nonce"] not in public
    assert first["connection"]["state"] == "verifying" and first["connection"]["open_url"] == ""
    assert not enso_home.config.exists()
    assert enso_home.connection_dir.stat().st_mode & 0o777 == 0o700
    assert (enso_home.connection_dir / "credentials.json").stat().st_mode & 0o777 == 0o600
    argv, options = child_process.starts[0]
    assert TOKENS["bot_token"] not in json.dumps(argv)
    assert options["cwd"] == enso_home.home and options["start_new_session"]


def test_start_rejects_reused_request_with_different_tokens_or_competing_receiver(
    enso_home, child_process
):
    setup.start(enso_home, "telegram", TOKENS)
    with pytest.raises(PairingError, match="different credentials"):
        setup.start(enso_home, "telegram", {**TOKENS, "bot_token": "123456:changed-token"})
    with pytest.raises(PairingError) as failure:
        setup.start(enso_home, "telegram", {**TOKENS, "request_id": "new_request_123456"})
    assert failure.value.code == "busy" and len(child_process.starts) == 1


def test_start_preserves_preexisting_config(enso_home, raw_config, child_process):
    save_config(enso_home, raw_config)
    previous = enso_home.config.read_bytes()
    with pytest.raises(PairingError) as failure:
        setup.start(enso_home, "telegram", TOKENS)
    assert failure.value.code == "already_configured" and child_process.starts == []
    assert enso_home.config.read_bytes() == previous


@pytest.mark.parametrize(
    ("transport", "raw", "message"),
    [
        ("discord", TOKENS, "Choose Slack or Telegram."),
        ("telegram", {**TOKENS, "app_token": "xapp-1"}, "Telegram needs only its bot token."),
        ("slack", TOKENS, "Paste the complete bot credentials without spaces."),
    ],
)
def test_credentials_follow_the_transport_declaration(transport, raw, message):
    with pytest.raises(PairingError) as failure:
        setup._credentials(transport, raw)
    assert (failure.value.code, str(failure.value)) == ("invalid_input", message)
    accepted = setup._credentials("slack", {**TOKENS, "app_token": "xapp-private-app-token"})
    assert accepted["app_token"] == "xapp-private-app-token"
    assert setup._credentials("telegram", TOKENS)["app_token"] == ""  # PairingRequest's field


def test_cancel_stops_receiver_then_clears_code_and_private_credentials(enso_home, child_process):
    started = setup.start(enso_home, "telegram", TOKENS)
    cancelled = setup.cancel(enso_home, started["connection"]["attempt_id"])
    assert cancelled["connection"]["state"] == "cancelled" and not setup.receiver_active(enso_home)
    assert child_process.signals == [signal.SIGTERM]
    assert not (enso_home.connection_dir / "credentials.json").exists()
    assert "nonce" not in setup._state(enso_home)
    assert not enso_home.config.exists()
    assert setup.cancel(enso_home)["connection"]["state"] == "cancelled"


@pytest.mark.parametrize("expired", [False, True])
def test_dead_receiver_reconciles_to_terminal_state_and_removes_secrets(
    enso_home, child_process, expired
):
    setup.start(enso_home, "telegram", TOKENS)
    child_process.close()
    if expired:
        state = setup._state(enso_home)
        state["expires_timestamp"] = time.time() - 1
        setup._write(enso_home.connection_dir / "state.json", state)
    report = setup.snapshot(enso_home)
    assert report["connection"]["state"] == ("expired" if expired else "failed")
    assert not (enso_home.connection_dir / "credentials.json").exists()
    assert "nonce" not in setup._state(enso_home)
    assert not child_process.signals


def test_spawn_failure_cleans_private_credentials(enso_home, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("private token in platform diagnostic")

    monkeypatch.setattr(
        setup, "subprocess", SimpleNamespace(Popen=fail, DEVNULL=setup.subprocess.DEVNULL)
    )
    with pytest.raises(PairingError) as failure:
        setup.start(enso_home, "telegram", TOKENS)
    assert failure.value.code == "connection_failed" and "private token" not in str(failure.value)
    assert not setup.receiver_active(enso_home)
    assert not (enso_home.connection_dir / "credentials.json").exists()


def test_finish_pairs_owner_and_repeating_lost_response_is_idempotent(enso_home, child_process):
    setup.start(enso_home, "telegram", TOKENS)
    state = mark_paired(enso_home, child_process)
    result = setup.finish(enso_home, finish_payload(state))
    config = load_config(enso_home)
    assert config.raw["transports"]["telegram"] == {
        "bot_token": TOKENS["bot_token"],
        "notify": "123",
    }
    assert config.telegram.notify == "123"
    assert config.bindings == {"telegram:123": "default"}
    assert config.telegram.bot_token == TOKENS["bot_token"]
    assert result["config_valid"] and result["connection"]["state"] == "applied"
    assert not (enso_home.connection_dir / "credentials.json").exists()
    first_bytes = enso_home.config.read_bytes()
    repeated = setup.finish(enso_home, finish_payload(state))
    assert (
        repeated["config_hash"] == result["config_hash"]
        and enso_home.config.read_bytes() == first_bytes
    )


def test_finish_refuses_existing_or_stale_config_and_preserves_unrelated_fields(
    enso_home, child_process
):
    setup.start(enso_home, "telegram", TOKENS)
    state = mark_paired(enso_home, child_process)
    applied = setup.finish(enso_home, finish_payload(state))
    raw = load_config(enso_home).raw
    raw["logging"] = {"level": "DEBUG"}
    raw["agent"] = {"timeout": 1234}
    save_config(enso_home, raw)
    current = config_fingerprint(enso_home)
    desired = {**DEFAULTS, "effort": "low"}
    with pytest.raises(PairingError) as failure:
        setup.finish(
            enso_home, finish_payload(state, defaults=desired, fingerprint=applied["config_hash"])
        )
    assert failure.value.code == "conflict"
    assert load_config(enso_home).defaults.effort == "high"
    revised = setup.finish(enso_home, finish_payload(state, defaults=desired, fingerprint=current))
    assert revised["config_valid"] and load_config(enso_home).raw["logging"]["level"] == "DEBUG"
    assert load_config(enso_home).agent_timeout == 1234


def test_finish_never_overwrites_a_config_created_after_pairing(
    enso_home, raw_config, child_process
):
    setup.start(enso_home, "telegram", TOKENS)
    state = mark_paired(enso_home, child_process)
    save_config(enso_home, raw_config)
    before = enso_home.config.read_bytes()
    with pytest.raises(PairingError) as failure:
        setup.finish(enso_home, finish_payload(state, fingerprint=config_fingerprint(enso_home)))
    assert failure.value.code == "conflict" and enso_home.config.read_bytes() == before


def test_finish_recovers_when_process_dies_after_config_write(
    enso_home, child_process, monkeypatch
):
    setup.start(enso_home, "telegram", TOKENS)
    state = mark_paired(enso_home, child_process)
    original = setup._write
    failed = False

    def interrupted(path, value):
        nonlocal failed
        if path.name == "state.json" and value.get("state") == "applied" and not failed:
            failed = True
            raise OSError("process lost after atomic config write")
        return original(path, value)

    monkeypatch.setattr(setup, "_write", interrupted)
    with pytest.raises(OSError):
        setup.finish(enso_home, finish_payload(state))
    saved = enso_home.config.read_bytes()
    recovered = setup.finish(enso_home, finish_payload(state))
    assert recovered["connection"]["state"] == "applied" and enso_home.config.read_bytes() == saved
    assert not (enso_home.connection_dir / "credentials.json").exists()


def test_finish_retry_cleans_credentials_after_applied_state_was_saved(
    enso_home, child_process, monkeypatch
):
    setup.start(enso_home, "telegram", TOKENS)
    state = mark_paired(enso_home, child_process)
    original = Path.unlink
    failed = False

    def interrupted(path, *args, **kwargs):
        nonlocal failed
        if path.name == "credentials.json" and not failed:
            failed = True
            raise OSError("interrupted after applied state was saved")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupted)
    with pytest.raises(OSError):
        setup.finish(enso_home, finish_payload(state))
    assert setup._state(enso_home)["state"] == "applied"
    assert (enso_home.connection_dir / "credentials.json").exists()
    recovered = setup.finish(enso_home, finish_payload(state))
    assert recovered["connection"]["state"] == "applied"
    assert not (enso_home.connection_dir / "credentials.json").exists()


@pytest.mark.parametrize(
    "outcome", ["success", "replay", "expired", "cancelled", "platform_failure"]
)
async def test_worker_consumption_expiry_and_failure_cleanup(enso_home, monkeypatch, outcome):
    assert initialize_home(enso_home)["ok"]
    setup._prepare(enso_home)
    state = {
        "attempt_id": "test-attempt",
        "transport": "telegram",
        "state": "verifying",
        "nonce": "random-private-code",
        "created_timestamp": time.time(),
        "expires_timestamp": time.time() + 30,
    }
    setup._write(enso_home.connection_dir / "credentials.json", {**TOKENS, "app_token": ""})
    events = []

    async def pair(request, ready, claim):
        await ready(
            {
                "bot_name": "test_bot",
                "workspace_name": "",
                "open_url": f"https://t.me/test_bot?start={request.nonce}",
                "instruction": "Tap Start",
            }
        )
        if outcome == "expired":
            raise TimeoutError
        if outcome == "cancelled":
            raise asyncio.CancelledError
        if outcome == "platform_failure":
            raise RuntimeError("raw error with private token")
        await claim()
        events.append("claimed")
        if outcome == "replay":
            await claim()
        events.append("receiver closed")
        return PairedIdentity("123", "123")

    monkeypatch.setattr("enso.transports.telegram_setup.pair", pair)
    await setup._pair_worker(enso_home, state)
    saved = setup._state(enso_home)
    assert "nonce" not in saved
    if outcome == "success":
        assert saved["state"] == "paired" and saved["user_id"] == "123" and saved["consumed"]
        assert events == ["claimed", "receiver closed"]
        assert (enso_home.connection_dir / "credentials.json").exists()
    else:
        assert saved["state"] == (outcome if outcome in ("expired", "cancelled") else "failed")
        assert not (enso_home.connection_dir / "credentials.json").exists()
        assert "private token" not in json.dumps(saved)


async def test_worker_enforces_its_deadline_even_when_transport_never_returns(
    enso_home, monkeypatch
):
    setup._prepare(enso_home)
    state = {
        "attempt_id": "expired-attempt",
        "transport": "telegram",
        "state": "verifying",
        "nonce": "expired-private-code",
        "created_timestamp": time.time() - 60,
        "expires_timestamp": time.time() - 1,
    }
    setup._write(enso_home.connection_dir / "credentials.json", {**TOKENS, "app_token": ""})
    closed = []

    async def blocked_pair(*args):
        try:
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    monkeypatch.setattr("enso.transports.telegram_setup.pair", blocked_pair)
    await asyncio.wait_for(setup._pair_worker(enso_home, state), timeout=1)
    assert setup._state(enso_home)["state"] == "expired" and closed == [True]
    assert not (enso_home.connection_dir / "credentials.json").exists()


def test_first_reply_requires_same_selected_provider_owner_and_config(enso_home, child_process):
    setup.start(enso_home, "telegram", TOKENS)
    state = mark_paired(enso_home, child_process)
    applied = setup.finish(enso_home, finish_payload(state))
    revision = applied["config_hash"]
    setup.record_reply(
        enso_home, {**DEFAULTS, "provider": "codex"}, "telegram", "123", "123", revision
    )
    setup.record_reply(enso_home, DEFAULTS, "telegram", "456", "123", revision)
    setup.record_reply(enso_home, DEFAULTS, "telegram", "123", "123", "stale")
    assert setup.snapshot(enso_home)["first_reply"] is None
    setup.record_reply(enso_home, DEFAULTS, "telegram", "123", "123", revision)
    assert setup.snapshot(enso_home)["first_reply"]["provider"] == "claude"
    raw = load_config(enso_home).raw
    raw["defaults"]["effort"] = "low"
    save_config(enso_home, raw)
    assert setup.snapshot(enso_home)["first_reply"] is None


@pytest.mark.parametrize(
    "content", ['{"bot_token":"private-token",broken}', '{"bot_token":"private-token' + "x" * 16384]
)
def test_json_boundary_returns_one_safe_failure_for_bad_input(enso_home, content):
    result = CliRunner().invoke(
        app,
        ["connect", "start", "--transport", "telegram", "--file", "-", "--json"],
        input=content,
    )
    report = json.loads(result.stdout)
    assert result.exit_code == 1 and result.stderr == ""
    assert report["version"] == 1 and not report["ok"] and "private-token" not in result.stdout
    assert report["error_code"] == "invalid_input"


def test_paired_result_stays_hidden_until_receiver_lock_is_released(enso_home, child_process):
    setup.start(enso_home, "telegram", TOKENS)
    state = setup._state(enso_home)
    state.update(state="paired", user_id="123", channel="123", open_url="https://t.me/test_bot")
    setup._write(enso_home.connection_dir / "state.json", state)
    pending = setup.snapshot(enso_home)
    assert pending["connection"]["state"] != "paired"
    assert pending["connection"]["open_url"] == "" and pending["connection"]["instruction"] == ""
    with pytest.raises(PairingError) as failure:
        setup.finish(enso_home, finish_payload(state))
    assert failure.value.code == "busy"
    child_process.close()
    assert setup.snapshot(enso_home)["connection"]["state"] == "paired"


def test_native_pairing_holds_receiver_and_control_locks_without_persisting_credentials(
    enso_home, raw_config, monkeypatch
):
    assert initialize_home(enso_home)["ok"]
    seen = []

    async def ready(info):
        seen.append(info)

    async def pair(request, report_ready, claim):
        assert setup.receiver_active(enso_home)
        assert request.bot_token == TOKENS["bot_token"] and len(request.nonce) >= 24
        assert not apply_config(enso_home, raw_config)["applied"]
        for action in (
            lambda: setup.start(enso_home, "telegram", TOKENS),
            lambda: setup.cancel(enso_home),
        ):
            with pytest.raises(PairingError) as failure:
                action()
            assert failure.value.code == "busy"
        await report_ready({"open_url": "https://t.me/test_bot", "instruction": "Start"})
        await claim()
        return PairedIdentity("123", "123")

    monkeypatch.setattr("enso.transports.telegram_setup.pair", pair)
    owner = setup.pair_in_terminal(enso_home, "telegram", {"bot_token": TOKENS["bot_token"]}, ready)
    assert owner == PairedIdentity("123", "123") and seen
    assert not setup.receiver_active(enso_home)
    assert not (enso_home.connection_dir / "credentials.json").exists()
    assert not (enso_home.connection_dir / "state.json").exists()
    assert not enso_home.config.exists()


@pytest.mark.parametrize("outcome", ["expired", "missing_extra", "platform_failure"])
def test_native_pairing_deadline_and_failures_release_both_locks(enso_home, monkeypatch, outcome):
    closed = []

    async def ready(info):
        pass

    async def pair(*args):
        try:
            if outcome == "missing_extra":
                raise ImportError("telegram unavailable")
            if outcome == "platform_failure":
                raise PairingError("invalid_token", "Token not accepted.")
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    monkeypatch.setattr("enso.transports.telegram_setup.pair", pair)
    if outcome == "expired":
        monkeypatch.setattr(setup, "ATTEMPT_SECONDS", 0)
    with pytest.raises(PairingError) as failure:
        setup.pair_in_terminal(enso_home, "telegram", {"bot_token": TOKENS["bot_token"]}, ready)
    assert failure.value.code == {"platform_failure": "invalid_token"}.get(outcome, outcome)
    assert closed == [True] and not setup.receiver_active(enso_home)
    with setup._control(enso_home):
        pass
    assert not (enso_home.connection_dir / "credentials.json").exists()


def test_native_pairing_refuses_an_existing_hosted_receiver(enso_home, child_process, monkeypatch):
    setup.start(enso_home, "telegram", TOKENS)
    called = []

    async def pair(*args):
        called.append(True)

    async def ready(info):
        pass

    monkeypatch.setattr("enso.transports.telegram_setup.pair", pair)
    with pytest.raises(PairingError) as failure:
        setup.pair_in_terminal(enso_home, "telegram", {"bot_token": TOKENS["bot_token"]}, ready)
    assert failure.value.code == "busy" and not called
    assert setup.receiver_active(enso_home)


def test_service_excludes_pairing_even_when_its_config_is_moved_aside(
    enso_home, raw_config, child_process
):
    assert initialize_home(enso_home)["ok"]
    save_config(enso_home, raw_config)
    with setup.service_receiver(enso_home):
        enso_home.config.rename(enso_home.home / "config.saved.json")
        with pytest.raises(PairingError) as failure:
            setup.start(enso_home, "telegram", TOKENS)
        assert failure.value.code == "busy" and not child_process.starts
        assert setup.receiver_active(enso_home)
    assert not setup.receiver_active(enso_home)
    assert (enso_home.home / "config.saved.json").exists()


def test_cancel_does_not_signal_a_service_that_replaced_a_dead_pairing_worker(
    enso_home, child_process
):
    started = setup.start(enso_home, "telegram", TOKENS)
    child_process.close()
    with setup.service_receiver(enso_home):
        cancelled = setup.cancel(enso_home, started["connection"]["attempt_id"])
        assert cancelled["connection"]["state"] == "cancelled" and child_process.signals == []
        assert setup.receiver_active(enso_home)
    assert not (enso_home.connection_dir / "credentials.json").exists()


def test_service_refuses_an_existing_receiver_and_releases_after_failure(enso_home, child_process):
    setup.start(enso_home, "telegram", TOKENS)
    owner_before = setup._read(enso_home.connection_dir / "worker.lock")
    with pytest.raises(PairingError) as failure, setup.service_receiver(enso_home):
        pytest.fail("service entered while a pairing receiver owns the lock")
    assert failure.value.code == "busy"
    assert setup._read(enso_home.connection_dir / "worker.lock") == owner_before
    child_process.close()
    with pytest.raises(RuntimeError), setup.service_receiver(enso_home):
        assert setup.receiver_active(enso_home)
        raise RuntimeError("service failed")
    assert not setup.receiver_active(enso_home)


def test_apply_refuses_only_a_live_pairing_attempt(enso_home, raw_config, child_process):
    setup.start(enso_home, "telegram", TOKENS)
    refused = apply_config(enso_home, raw_config)
    assert not refused["applied"] and "pairing attempt is active" in refused["problems"][0]
    assert not enso_home.config.exists()
    child_process.close()  # the receiver died: its recorded state is stale, not live
    assert apply_config(enso_home, raw_config)["ok"]
    with setup.service_receiver(enso_home):  # a service holding the lock is not the attempt
        assert not setup.pairing_active(enso_home)
        assert apply_config(enso_home, raw_config)["ok"]
        assert setup.snapshot(enso_home)["connection"]["state"] == "failed"


def test_finish_repeat_succeeds_while_service_runs_but_changes_are_refused(
    enso_home, child_process
):
    setup.start(enso_home, "telegram", TOKENS)
    state = mark_paired(enso_home, child_process)
    applied = setup.finish(enso_home, finish_payload(state))
    with setup.service_receiver(enso_home):
        repeated = setup.finish(enso_home, finish_payload(state))
        assert repeated["config_hash"] == applied["config_hash"]
        with pytest.raises(PairingError) as failure:
            setup.finish(
                enso_home,
                finish_payload(
                    state,
                    defaults={**DEFAULTS, "effort": "low"},
                    fingerprint=applied["config_hash"],
                ),
            )
        assert failure.value.code == "busy"
        assert load_config(enso_home).defaults.effort == "high"
