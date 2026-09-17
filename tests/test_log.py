"""Reading the log across rotation, and the command redaction that keeps prompts out of it."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from enso.config import Paths
from enso.log import follow, redacted_command, tail

# One of each shape _TOKEN_RE guards: Slack bot, Slack app, and Telegram bot tokens.
TOKENS = ["xoxb-secret", "xapp-secret", "123456:a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6_-"]


def _backup(paths: Paths, index: int) -> Path:
    return paths.log.with_name(f"{paths.log.name}.{index}")


def _append(path: Path, text: str) -> None:
    with path.open("a") as handle:
        handle.write(text)


def _following(
    monkeypatch: pytest.MonkeyPatch, paths: Paths, *on_idle: Callable[[], None]
) -> Iterator[str]:
    """``follow`` whose idle polls run the next scripted action instead of sleeping."""
    actions = iter(on_idle)

    def sleep(_interval: float) -> None:
        action = next(actions, None)
        assert action is not None, "follow polled again with nothing left to happen"
        action()

    monkeypatch.setattr("enso.log.time", SimpleNamespace(sleep=sleep))
    return follow(paths)


def test_tail_stops_at_the_first_missing_backup(enso_home: Paths) -> None:
    assert tail(enso_home, 5) == []
    enso_home.log.write_text("live\n")
    _backup(enso_home, 2).write_text("oldest\n")
    assert tail(enso_home, 5) == ["live"]  # .2 is unreachable while .1 is missing
    _backup(enso_home, 1).write_text("older\n")
    assert tail(enso_home, 5) == ["oldest", "older", "live"]


def test_follow_yields_only_complete_lines_written_after_it_starts(
    enso_home: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    enso_home.log.write_text("before\n")
    first_half = partial(_append, enso_home.log, "half")
    second_half = partial(_append, enso_home.log, " line\n")
    assert next(_following(monkeypatch, enso_home, first_half, second_half)) == "half line"


def test_follow_reads_the_new_file_after_rotation(
    enso_home: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = enso_home.log
    live.write_text("before\n")

    def rotate() -> None:
        _append(live, "last\n")
        live.rename(_backup(enso_home, 1))
        live.write_text("first\n")

    lines = _following(monkeypatch, enso_home, rotate)
    assert [next(lines), next(lines)] == ["last", "first"]


def test_follow_restarts_from_the_top_after_truncation(
    enso_home: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = enso_home.log
    live.write_text("a longer first line\n")
    lines = _following(monkeypatch, enso_home, lambda: live.write_text("short\n"))
    assert next(lines) == "short"


def test_redacted_command_hides_attached_and_separated_prompts() -> None:
    attached = ["grok", "--output-format", "plain", "--model", "grok-4.6", "--single=hi there"]
    assert redacted_command(attached) == (
        "grok --output-format plain --model grok-4.6 '--single=<prompt chars=8>'"
    )
    agy = ["agy", "--output-format", "stream-json", "--new-project", "--prompt=hi there"]
    assert redacted_command(agy) == (
        "agy --output-format stream-json --new-project '--prompt=<prompt chars=8>'"
    )
    separated = ["claude", "-p", "--", "hi", "there"]
    assert redacted_command(separated) == "claude -p -- '<prompt chars=7>'"


@pytest.mark.parametrize("token", TOKENS)
def test_redacted_command_hides_every_token_shape(token: str) -> None:
    assert redacted_command(["claude", "-p", token, "--", "hi"]) == (
        "claude -p '<redacted>' -- '<prompt chars=2>'"
    )


def test_redacted_command_keeps_ordinary_arguments() -> None:
    # A short number is not a Telegram token; over-redacting would make the log useless.
    assert redacted_command(["claude", "-p", "12345", "--", "hi"]) == (
        "claude -p 12345 -- '<prompt chars=2>'"
    )
