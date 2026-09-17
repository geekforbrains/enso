"""Provider contracts: command lines, recorded event parsing, effort clamping."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest
from conftest import FIXTURES

from enso.config import Paths
from enso.providers import (
    PROVIDER_CLASSES,
    SESSION_ID_MAX,
    SessionIdError,
    StreamEvent,
    make_provider,
    session_path,
    stored_session_id_ok,
)
from enso.providers.agy import CONVERSATIONS_DIR, PROJECTS_DIR, AgyProvider
from enso.providers.claude import ClaudeProvider, project_dir
from enso.providers.codex import CodexProvider
from enso.providers.grok import GrokProvider, sessions_dir
from enso.providers.opencode import CLEAR_SESSION_TIMEOUT, OpenCodeProvider
from enso.providers.stream import DIAGNOSTIC_KEEP, ProviderStream


def _recorded(name: str) -> list[tuple[dict, list]]:
    lines = (FIXTURES / f"{name}_events.jsonl").read_text().splitlines()
    return [(json.loads(line)["event"], json.loads(line)["expect"]) for line in lines if line]


@pytest.mark.parametrize("name", list(PROVIDER_CLASSES))
def test_parse_event_matches_recorded_fixtures(name: str) -> None:
    provider = PROVIDER_CLASSES[name](name)
    for event, expect in _recorded(name):
        got = [
            [e.kind, e.session_id if e.kind == "session" else e.text]
            for e in provider.parse_event(event)
        ]
        assert got == expect, event


def test_parse_line_skips_non_json_noise() -> None:
    provider = CodexProvider("codex")
    assert provider.parse_line("Reading prompt from stdin...") is None
    assert provider.parse_line("") is None
    assert provider.parse_line('{"type": "error", "message": "x"}') == {
        "type": "error",
        "message": "x",
    }
    assert provider.parse_line("[1, 2]") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [("", "provider reported an error"), ("x" * (DIAGNOSTIC_KEEP + 1), "x" * DIAGNOSTIC_KEEP)],
)
def test_stream_normalizes_error_without_changing_parser_event(monkeypatch, text, expected) -> None:
    provider = CodexProvider("codex")
    parsed = StreamEvent(kind="error", text=text)
    monkeypatch.setattr(provider, "parse_event", lambda _event: [parsed])
    stream = ProviderStream(provider, None, new_session=False)

    [emitted] = stream.feed(b'{"type":"error"}')

    assert emitted.text == stream.error == expected
    assert parsed.text == text


def test_claude_commands() -> None:
    p = ClaudeProvider("/bin/claude")
    new = p.command("hi", "opus", "xhigh", ["--skip"], session_id="S", new_session=True)
    assert new == [
        "/bin/claude", "-p", "--output-format", "stream-json", "--verbose", "--skip",
        "--model", "opus", "--effort", "xhigh", "--session-id", "S", "--", "hi",
    ]  # fmt: skip
    resumed = p.command("-x", "opus", "high", [], session_id="S")
    assert resumed[-4:] == ["--resume", "S", "--", "-x"]
    batch = p.command("job", "sonnet", "high", ["--skip"], session_id="S", batch=True)
    assert batch == [
        "/bin/claude", "-p", "--output-format", "text", "--skip",
        "--model", "sonnet", "--effort", "high", "--", "job",
    ]  # fmt: skip


def test_codex_commands() -> None:
    p = CodexProvider("codex")
    fresh = p.command("hi", "sol", "xhigh", ["--bypass"])
    assert fresh == [
        "codex", "exec", "--bypass", "--json", "-m", "gpt-5.6-sol",
        "-c", 'model_reasoning_effort="xhigh"', "--", "hi",
    ]  # fmt: skip
    resumed = p.command("hi", "terra", "high", [], session_id="t_1")
    assert resumed[:3] == ["codex", "exec", "resume"]
    assert resumed[-3:] == ["--", "t_1", "hi"]
    batch = p.command("job", "luna", "max", ["--bypass"], session_id="t_1", batch=True)
    assert batch == [
        "codex", "exec", "--bypass", "-m", "gpt-5.6-luna",
        "-c", 'model_reasoning_effort="max"', "--", "job",
    ]  # fmt: skip
    assert p.stderr_to_stdout() and not CodexProvider.assigns_session_id


def test_opencode_commands() -> None:
    p = OpenCodeProvider("/bin/opencode")
    fresh = p.command(
        "-hyphen first",
        "openrouter/deepseek/deepseek-v4-flash",
        "xhigh",
        ["--auto"],
        cwd="/ws",
    )
    assert fresh == [
        "/bin/opencode", "run", "-m", "openrouter/deepseek/deepseek-v4-flash",
        "--variant", "xhigh", "--auto", "--format", "json", "--dir", "/ws",
        "--", "-hyphen first",
    ]  # fmt: skip
    resumed = p.command("again", "provider/model", "minimal", [], session_id="ses_123", cwd="/ws")
    assert resumed == [
        "/bin/opencode", "run", "-m", "provider/model", "--variant", "minimal",
        "--format", "json", "--dir", "/ws", "-s", "ses_123", "--", "again",
    ]  # fmt: skip
    batch = p.command(
        "job", "provider/model", "none", ["--auto"], session_id="ses_123", batch=True, cwd="/ws"
    )
    assert batch == [
        "/bin/opencode", "run", "-m", "provider/model", "--variant", "none", "--auto",
        "--format", "json", "--dir", "/ws", "--", "job",
    ]  # fmt: skip
    # No cwd, no root to pin: the flag needs a value, so it is left out entirely.
    assert "--dir" not in p.command("job", "provider/model", "none", [])
    assert not OpenCodeProvider.assigns_session_id


def test_opencode_formats_json_batch_output_without_a_finish_event() -> None:
    p = OpenCodeProvider("opencode")
    output = "\n".join(
        [
            '{"type":"step_start","sessionID":"ses_123","part":{}}',
            '{"type":"text","sessionID":"ses_123","part":{"text":"Done."}}',
        ]
    )
    assert p.format_batch_output(output) == "Done."
    assert (
        p.format_batch_output('{"type":"error","error":{"data":{"message":"No model."}}}')
        == "No model."
    )
    assert p.format_batch_output("plain startup failure\n") == "plain startup failure"


def test_grok_commands_attach_the_prompt() -> None:
    p = GrokProvider("grok")
    new = p.command(
        "-hyphen first", "grok-4.6", "xhigh", ["--always-approve"], session_id="S", new_session=True
    )
    # No --rules= part: Grok reads the home AGENTS.md itself by walking up to the Git root;
    # test_make_provider_binds_only_the_cli_path proves make_provider no longer reads it.
    assert new == [
        "grok", "--output-format", "streaming-messages-json", "--always-approve",
        "--model", "grok-4.6", "--effort", "xhigh", "--session-id", "S",
        "--single=-hyphen first",
    ]  # fmt: skip
    resumed = p.command("-x", "grok-4.6", "high", [], session_id="S")
    assert resumed[-3:] == ["--resume", "S", "--single=-x"]
    batch = p.command("job", "grok-4.6", "high", [], batch=True)
    assert batch == [
        "grok", "--output-format", "plain", "--model", "grok-4.6", "--effort", "high",
        "--single=job",
    ]  # fmt: skip
    assert p.retryable_error("Error: Not signed in") and not p.retryable_error("timeout")


def _catalog(
    home: Path,
    project: str,
    *folders: str,
    git: bool = False,
    resources: list[dict] | None = None,
) -> None:
    """Write one Antigravity project entry: folders in the plain or nested git shape.

    Paths are percent-encoded the way the real catalog writes them; ``resources`` replaces
    the generated list outright, for shapes agy writes that are not local folders.
    """
    directory = home / PROJECTS_DIR.relative_to("~")
    directory.mkdir(parents=True, exist_ok=True)
    listed = resources or [
        {"gitFolder": {"folderUri": f"file://{quote(folder)}"}}
        if git
        else {"folderUri": f"file://{quote(folder)}"}
        for folder in folders
    ]
    entry = {"id": project, "name": "ws", "projectResources": {"resources": listed}}
    (directory / f"{project}.json").write_text(json.dumps(entry))


def test_agy_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    p = AgyProvider("agy")

    # No project registered yet, and a prompt that opens with a markdown bullet.
    fresh = p.command(
        "-hyphen first", "gemini-3.8-flash-low", "low", ["--dangerously-skip-permissions"]
    )
    assert fresh == [
        "agy", "--print-timeout", "24h", "--output-format", "stream-json",
        "--dangerously-skip-permissions", "--model", "gemini-3.8-flash-low", "--new-project",
        "--prompt=-hyphen first",
    ]  # fmt: skip
    batch = p.command("job", "gemini-3.1-pro-high", "high", [], batch=True)
    assert batch == [
        "agy", "--print-timeout", "24h", "--output-format", "text",
        "--model", "gemini-3.1-pro-high", "--new-project", "--prompt=job",
    ]  # fmt: skip
    # The effort never reaches the CLI: it is already in the model id, and agy rejects both
    # the conflict and the flag itself. Nor does Enso add a permission flag of its own.
    assert "--effort" not in fresh and "--effort" not in batch
    assert fresh.count("--dangerously-skip-permissions") == 1
    assert "--dangerously-skip-permissions" not in batch


def test_agy_pins_the_workspace_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _catalog(tmp_path, "plain-id", str(workspace))

    def pinned() -> list[str]:
        return AgyProvider("agy").command("hi", "claude-sonnet-4-6", "high", [], cwd=str(workspace))

    assert pinned()[-3:] == ["--project", "plain-id", "--prompt=hi"]
    assert "--new-project" not in pinned()

    # A project created from a Git checkout nests the same folder one level down.
    (tmp_path / PROJECTS_DIR.relative_to("~") / "plain-id.json").unlink()
    _catalog(tmp_path, "git-id", str(workspace), git=True)
    assert pinned()[-3:] == ["--project", "git-id", "--prompt=hi"]

    # A URI is a URI: a home or workspace with a space in it is escaped in the catalog and
    # has to be decoded back before the directories can be compared.
    spaced = tmp_path / "my ws"
    spaced.mkdir()
    _catalog(tmp_path, "spaced-id", str(spaced))
    assert "my%20ws" in (tmp_path / PROJECTS_DIR.relative_to("~") / "spaced-id.json").read_text()
    escaped = AgyProvider("agy").command("hi", "claude-sonnet-4-6", "high", [], cwd=str(spaced))
    assert escaped[-3:] == ["--project", "spaced-id", "--prompt=hi"]


def test_agy_falls_back_to_new_project_when_nothing_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    p = AgyProvider("agy")

    def pins(**kwargs: object) -> list[str]:
        return p.command("hi", "gemini-3.1-pro-low", "low", [], cwd=str(workspace), **kwargs)  # type: ignore[arg-type]

    # An empty catalog, a malformed entry, another directory's project, and a multi-root
    # project whose first folder is elsewhere: none of them may pin this launch.
    assert "--new-project" in pins()
    catalog = tmp_path / PROJECTS_DIR.relative_to("~")
    catalog.mkdir(parents=True, exist_ok=True)
    (catalog / "broken.json").write_text("{not json")
    _catalog(tmp_path, "elsewhere", str(tmp_path / "other"))
    _catalog(tmp_path, "multi", str(tmp_path / "other"), str(workspace))
    # A first resource that is not a local folder rules the entry out; it must not hand the
    # place of "first folder" to the one behind it, which is some other project's directory.
    _catalog(
        tmp_path,
        "remote",
        resources=[
            {"folderUri": "vscode-remote://ssh-remote+host/elsewhere"},
            {"folderUri": f"file://{workspace}"},
        ],
    )
    assert "--new-project" in pins() and "--project" not in pins()
    # Without a working directory there is nothing to match, and nothing to guess from.
    assert "--new-project" in p.command("hi", "gemini-3.1-pro-low", "low", [])


def test_agy_resume_uses_the_conversation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _catalog(tmp_path, "ws-id", str(workspace))
    p = AgyProvider("agy")

    resumed = p.command("hi", "claude-sonnet-4-6", "high", [], session_id="C1", cwd=str(workspace))
    assert resumed[-5:] == ["--project", "ws-id", "--conversation", "C1", "--prompt=hi"]
    # A conversation is pinned to its project when it is created, so an unresolved catalog
    # must not register another one on every turn.
    (tmp_path / PROJECTS_DIR.relative_to("~") / "ws-id.json").unlink()
    lost = p.command("hi", "claude-sonnet-4-6", "high", [], session_id="C1", cwd=str(workspace))
    assert "--new-project" not in lost and lost[-3:] == ["--conversation", "C1", "--prompt=hi"]
    # Batch runs never resume: a job is a fresh conversation every time.
    assert "--conversation" not in p.command(
        "job", "claude-sonnet-4-6", "high", [], session_id="C1", batch=True, cwd=str(workspace)
    )


def test_make_provider_binds_only_the_cli_path(enso_home: Paths) -> None:
    # A real home carries an AGENTS.md; Grok reads it by walking up to the Git root,
    # so nothing from the file may reach the command line any more.
    enso_home.agents_md.write_text("# Enso\n- be brief\n")
    provider = make_provider("grok", "/bin/grok")
    assert isinstance(provider, GrokProvider) and provider.path == "/bin/grok"
    assert provider.command("hi", "grok-4.6", "high", [], batch=True) == [
        "/bin/grok", "--output-format", "plain", "--model", "grok-4.6", "--effort", "high",
        "--single=hi",
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("provider", "model", "effort", "expected"),
    [
        ("claude", "opus", "max", "max"),
        ("claude", "haiku", "max", "high"),
        ("claude", "haiku", "low", "low"),
        ("claude", "opus", "weird", "weird"),
        ("codex", "sol", "ultra", "ultra"),
        ("codex", "luna", "ultra", "max"),
        ("codex", "gpt-5.6-luna", "ultra", "max"),
        ("codex", "other", "ultra", "xhigh"),
        ("grok", "grok-4.6", "xhigh", "xhigh"),
        ("opencode", "provider/model", "none", "none"),
        ("opencode", "provider/model", "max", "max"),
        # Antigravity reports the effort in the model id, even above the request.
        ("agy", "gemini-3.8-flash-low", "high", "low"),
        ("agy", "gemini-3.8-flash-high", "low", "high"),
        ("agy", "gemini-3.8-flash-medium", "low", "medium"),
        ("agy", "gemini-3.8-flash-medium", "high", "medium"),
        ("agy", "gemini-3.1-pro-high", "high", "high"),
        # Models without an effort suffix retain the requested level.
        ("agy", "claude-sonnet-4-6", "low", "low"),
        ("agy", "claude-sonnet-4-6", "medium", "medium"),
        ("agy", "claude-sonnet-4-6", "high", "high"),
        ("agy", "claude-opus-4-6-thinking", "low", "low"),
        ("agy", "claude-opus-4-6-thinking", "medium", "medium"),
        ("agy", "claude-opus-4-6-thinking", "high", "high"),
    ],
)
def test_clamp_effort(provider: str, model: str, effort: str, expected: str) -> None:
    assert PROVIDER_CLASSES[provider].clamp_effort(effort, model) == expected


# A real id from each CLI's own output: a UUID for Claude, Grok and agy, an opaque token
# for the two that mint their own.
SESSION = "abcdefab-1111-4222-8333-444444444444"
SHORT = SESSION[:8]

# Nothing here may be stored, resumed, or used to name a deletion.
HOSTILE_IDS = [
    "",
    ".",
    "..",
    "../../etc/passwd",
    "/etc/passwd",
    "~/.ssh/id_rsa",
    f"{SESSION}/../../escape",
    f"..{os.sep}{SESSION}",
    f"{SESSION}\\escape",
    f"{SESSION}\n{SESSION}",
    f"{SESSION}\x00",
    "-rf",
    "--help",
    f"-{SESSION}",
    f".{SESSION}",
    f"{SESSION} extra",
    "a" * (SESSION_ID_MAX + 1),
]


@pytest.mark.parametrize("name", list(PROVIDER_CLASSES))
def test_session_ids_outside_the_contract_are_rejected(name: str) -> None:
    cls = PROVIDER_CLASSES[name]
    for hostile in HOSTILE_IDS:
        assert not cls.valid_session_id(hostile), hostile
        assert not stored_session_id_ok(name, hostile), hostile
        with pytest.raises(SessionIdError):
            cls.check_session_id(hostile)


def test_each_provider_accepts_the_ids_its_cli_emits() -> None:
    # Claude, Grok and agy identify a session by UUID; Codex and OpenCode mint opaque tokens.
    for cls in (ClaudeProvider, GrokProvider, AgyProvider):
        assert cls.check_session_id(SESSION) == SESSION
        assert cls.check_session_id(SESSION.upper()) == SESSION.upper()
        assert not cls.valid_session_id("t_123")
    assert CodexProvider.check_session_id("t_123") == "t_123"
    assert CodexProvider.check_session_id(SESSION) == SESSION
    opencode = "ses_11111111111111111111111111"
    assert OpenCodeProvider.check_session_id(opencode) == opencode
    # A provider name Enso no longer knows has no contract to judge its rows by.
    assert stored_session_id_ok("retired", "anything at all")


def test_session_path_stays_inside_its_store(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    assert session_path(root, f"{SESSION}.jsonl") == root / f"{SESSION}.jsonl"
    # The defence for a row written before the contract existed, or edited by hand.
    for escape in ("", ".", "..", "../elsewhere", str(tmp_path / "elsewhere")):
        with pytest.raises(SessionIdError, match="escapes"):
            session_path(root, escape)


def test_clear_session_deletes_local_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok"))
    cwd = str(tmp_path / "ws")
    transcript = project_dir(cwd) / f"{SESSION}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("")
    grok_session = sessions_dir(cwd) / SESSION
    (grok_session / "turns").mkdir(parents=True)
    conversations = CONVERSATIONS_DIR.expanduser()
    conversations.mkdir(parents=True)
    store = [conversations / f"{SESSION}.db{tail}" for tail in ("", "-wal", "-shm")]
    for path in [*store, conversations / "conversation_summaries.db"]:
        path.write_text("")

    assert ClaudeProvider("claude").clear_session(SESSION, cwd) == f"deleted session {SHORT}"
    assert GrokProvider("grok").clear_session(SESSION, cwd) == f"deleted session {SHORT}"
    assert AgyProvider("agy").clear_session(SESSION, cwd) == f"deleted session {SHORT}"
    assert not transcript.exists() and not grok_session.exists()
    # The write-ahead log and the shared-memory file go with it; agy's own index does not.
    assert not any(path.exists() for path in store)
    assert (conversations / "conversation_summaries.db").exists()
    assert (
        ClaudeProvider("claude").clear_session(SESSION, cwd) == f"session {SHORT} (no file found)"
    )
    assert (
        GrokProvider("grok").clear_session(SESSION, cwd) == f"session {SHORT} (no directory found)"
    )
    assert AgyProvider("agy").clear_session(SESSION, cwd) == f"session {SHORT} (no file found)"


def _sentinels(tmp_path: Path) -> tuple[Path, Path]:
    """A file and a directory just outside every session store, with content to lose."""
    outside = tmp_path / "outside"
    (outside / "keep").mkdir(parents=True)
    (outside / "keep" / "notes.md").write_text("mine")
    (outside / "keep.jsonl").write_text("mine")
    return outside / "keep.jsonl", outside / "keep"


def test_clear_session_refuses_a_hostile_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok"))
    cwd = str(tmp_path / "ws")
    file_sentinel, dir_sentinel = _sentinels(tmp_path)
    project_dir(cwd).mkdir(parents=True)
    sessions_dir(cwd).mkdir(parents=True)
    CONVERSATIONS_DIR.expanduser().mkdir(parents=True)

    providers = [ClaudeProvider("claude"), GrokProvider("grok"), AgyProvider("agy")]
    aimed = [
        str(file_sentinel),
        str(dir_sentinel),
        f"../../outside/{dir_sentinel.name}",
        *HOSTILE_IDS,
    ]
    for provider in providers:
        for hostile in aimed:
            with pytest.raises(SessionIdError):
                provider.clear_session(hostile, cwd)
    assert file_sentinel.read_text() == "mine"
    assert (dir_sentinel / "notes.md").read_text() == "mine"


def test_clear_session_stops_at_a_link_out_of_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt store, not a corrupt id: the entry itself points outside."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok"))
    cwd = str(tmp_path / "ws")
    file_sentinel, dir_sentinel = _sentinels(tmp_path)
    project_dir(cwd).mkdir(parents=True)
    (project_dir(cwd) / f"{SESSION}.jsonl").symlink_to(file_sentinel)
    sessions_dir(cwd).mkdir(parents=True)
    (sessions_dir(cwd) / SESSION).symlink_to(dir_sentinel)
    conversations = CONVERSATIONS_DIR.expanduser()
    conversations.mkdir(parents=True)
    (conversations / f"{SESSION}.db").symlink_to(file_sentinel)

    for provider in (ClaudeProvider("claude"), GrokProvider("grok"), AgyProvider("agy")):
        with pytest.raises(SessionIdError, match="escapes"):
            provider.clear_session(SESSION, cwd)
    assert file_sentinel.read_text() == "mine"
    assert (dir_sentinel / "notes.md").read_text() == "mine"


def test_opencode_clear_session_refuses_a_hostile_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """No path here, but an option-like id would be read as a flag by the CLI."""

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"the CLI must not run: {argv}")

    monkeypatch.setattr("enso.providers.opencode.subprocess.run", run)
    for hostile in ("--help", "-rf", "../../etc/passwd", ""):
        with pytest.raises(SessionIdError):
            OpenCodeProvider("/bin/opencode").clear_session(hostile, "/tmp")


def test_opencode_clear_session_uses_the_cli_with_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict]] = []
    results = iter(
        [
            subprocess.CompletedProcess([], 0, "Session abc deleted\n", ""),
            subprocess.CompletedProcess([], 1, "", "\x1b[31mSession not found: abc\x1b[0m\n"),
            subprocess.CompletedProcess([], 2, "", "database unavailable\n"),
        ]
    )

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return next(results)

    monkeypatch.setattr("enso.providers.opencode.subprocess.run", run)
    provider = OpenCodeProvider("/bin/opencode")
    cwd = str(tmp_path)
    assert provider.clear_session("abc", cwd) == "deleted session abc"
    assert provider.clear_session("abc", cwd) == "session abc (not found)"
    with pytest.raises(RuntimeError, match="session delete failed: database unavailable"):
        provider.clear_session("abc", cwd)
    assert [argv for argv, _ in calls] == [
        ["/bin/opencode", "session", "delete", "abc"],
        ["/bin/opencode", "session", "delete", "abc"],
        ["/bin/opencode", "session", "delete", "abc"],
    ]
    assert all(
        kwargs
        == {
            "cwd": cwd,
            "capture_output": True,
            "text": True,
            "check": False,
            "timeout": CLEAR_SESSION_TIMEOUT,
        }
        for _, kwargs in calls
    )

    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args[0], CLEAR_SESSION_TIMEOUT)

    monkeypatch.setattr("enso.providers.opencode.subprocess.run", timeout)
    with pytest.raises(RuntimeError, match="session delete timed out after 10s"):
        provider.clear_session("abc", cwd)


def test_format_response_keeps_the_last_part() -> None:
    p = ClaudeProvider("claude")
    assert p.format_response(["draft", "final"]) == "final"
    assert p.format_response([]) == ""
    assert StreamEvent(kind="status", text="x").session_id is None
