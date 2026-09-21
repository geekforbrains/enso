"""Real command injection preserves exit behavior and never needs a running service."""

import os
import signal
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from enso import secrets
from enso.cli import app


def cli(*args, input=None):
    return subprocess.run(
        [sys.executable, "-m", "enso.cli", "secret", *args],
        input=input,
        capture_output=True,
        timeout=10,
    )


def test_cli_exact_text_and_name_only_management(enso_home):
    value = b"  private-cli-value\r\nsecond\n\n"
    result = cli("add", "TOKEN", "--stdin", input=value)
    assert result.returncode == 0, result.stderr
    assert value not in result.stdout + result.stderr
    assert cli("get", "TOKEN").stdout == value
    assert cli("list").stdout == b"TOKEN\n"
    assert cli("add", "TOKEN", "--stdin", input=b"replacement").returncode == 1
    assert cli("get", "TOKEN").stdout == value
    assert cli("delete", "TOKEN").returncode == 0
    assert cli("get", "TOKEN").returncode == 1
    assert cli("list").stdout == b""


def test_cli_hidden_prompt(enso_home, monkeypatch):
    monkeypatch.setattr("typer.testing._NamedTextIOWrapper.isatty", lambda _: True)
    monkeypatch.setattr("enso.cli.secrets.getpass.getpass", lambda _: "hidden-value")
    result = CliRunner().invoke(app, ["secret", "add", "TOKEN"])
    assert result.exit_code == 0, result.output
    assert "hidden-value" not in result.output
    assert secrets.resolve(enso_home, ["TOKEN"]) == {"TOKEN": "hidden-value"}


def test_reset_is_the_explicit_way_past_a_lost_key(enso_home, monkeypatch, tmp_path):
    key = tmp_path / "keys" / "master.key"
    enso_home.config.write_text(f'{{"secrets": {{"key_file": "{key}"}}}}')
    secrets.add(enso_home, "TOKEN", "lost-value")
    key.unlink()
    for args in (["list"], ["add", "TOKEN", "--stdin"], ["delete", "TOKEN"]):
        assert cli(*args, input=b"x").returncode == 1  # never a silent replacement key
    refused = cli("reset", input=b"y\n")  # a pipe is not a confirmation
    assert refused.returncode == 1 and b"--yes" in refused.stderr and not key.exists()

    monkeypatch.setattr("typer.testing._NamedTextIOWrapper.isatty", lambda _: True)
    declined = CliRunner().invoke(app, ["secret", "reset"], input="n\n")
    assert declined.exit_code == 1 and "permanently deletes" in declined.output
    confirmed = CliRunner().invoke(app, ["secret", "reset"], input="y\n")
    assert confirmed.exit_code == 0 and "Deleted 1 secret;" in confirmed.output

    assert secrets.names(enso_home) == [] and not key.exists()
    secrets.add(enso_home, "TOKEN", "new-value")  # a fresh store generates its own key
    assert secrets.resolve(enso_home, ["TOKEN"]) == {"TOKEN": "new-value"}
    assert cli("reset", "--yes").stdout == b"Deleted 1 secret; the store is uninitialized.\n"


@pytest.mark.parametrize("outcome", [0, 23, "signal"])
def test_run_injects_multiple_names_and_preserves_exit(enso_home, monkeypatch, outcome):
    secrets.add(enso_home, "FIRST_TOKEN", "first-private")
    secrets.add(enso_home, "SECOND_TOKEN", "second-private")
    monkeypatch.setenv("FIRST_TOKEN", "parent")
    code = (
        "import os,sys,signal; "
        "assert os.environ['FIRST_TOKEN']=='first-private'; "
        "assert os.environ['SECOND_TOKEN']=='second-private'; "
        "assert sys.argv[1:]==['--secret','child-argument']; "
    )
    code += (
        "os.kill(os.getpid(), signal.SIGTERM)" if outcome == "signal" else f"sys.exit({outcome})"
    )
    result = cli(
        "run",
        "--secret",
        "FIRST_TOKEN",
        "--secret",
        "SECOND_TOKEN",
        "--",
        sys.executable,
        "-c",
        code,
        "--secret",
        "child-argument",
    )
    assert result.returncode == (-signal.SIGTERM if outcome == "signal" else outcome), result.stderr
    assert result.stdout == result.stderr == b""
    assert os.environ["FIRST_TOKEN"] == "parent"


def test_run_missing_name_never_starts_command(enso_home, tmp_path):
    marker = tmp_path / "started"
    result = cli(
        "run",
        "--secret",
        "MISSING",
        "--",
        sys.executable,
        "-c",
        f"open({str(marker)!r},'w').close()",
    )
    assert result.returncode == 1 and b"secret not found: MISSING" in result.stderr
    assert not marker.exists()


def test_old_env_files_are_not_loaded(enso_home):
    old = enso_home.home / "secrets"
    old.mkdir()
    (old / "ignored.env").write_text("OLD_SECRET=old-private\n")
    assert cli("list").stdout == b""
    assert cli("get", "OLD_SECRET").returncode == 1
    assert (old / "ignored.env").read_text() == "OLD_SECRET=old-private\n"


def test_stdin_rejects_invalid_text_without_echoing_it(enso_home):
    for value in (b"private\x00value", b"private\xffvalue"):
        result = cli("add", "TOKEN", "--stdin", input=value)
        assert result.returncode == 1
        assert b"private" not in result.stdout + result.stderr
