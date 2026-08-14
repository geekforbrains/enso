"""Tests for provider abstraction and implementations."""

from __future__ import annotations

import asyncio
import json
import sqlite3

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

import pytest

from enso.instructions import MAX_SHARED_INSTRUCTION_BYTES, InstructionBundle
from enso.providers import (
    PROVIDER_CLASSES,
    PROVIDER_NAMES,
    STATUS_TEXT_LIMIT,
    provider_class,
)
from enso.providers import agy as agy_module
from enso.providers.agy import AGY_MODELS, AgyProvider
from enso.providers.claude import ClaudeProvider
from enso.providers.codex import CODEX_MODEL_ALIASES, CodexProvider

CLAUDE_SESSION_ID = "11111111-1111-4111-8111-111111111111"


def _instruction_bundle(tmp_path, content="Shared operator guidance."):
    source = tmp_path / "AGENTS.md"
    snapshot = tmp_path / "shared-instructions.md"
    source.write_text(content)
    snapshot.write_text(content)
    return InstructionBundle(
        source_path=str(source),
        content=content,
        revision="a" * 64,
        snapshot_path=str(snapshot),
    )

# -- Registry --


def test_supported_provider_names():
    assert PROVIDER_NAMES == ["claude", "codex", "agy"]
    # PROVIDER_NAMES is derived from the registry — the single source of truth.
    assert list(PROVIDER_CLASSES) == PROVIDER_NAMES


def test_provider_class_lookup():
    assert provider_class("claude") is ClaudeProvider
    assert provider_class("codex") is CodexProvider
    assert provider_class("agy") is AgyProvider


def test_provider_class_unknown():
    with pytest.raises(ValueError, match="Unknown provider"):
        provider_class("unknown")


def test_registry_declares_config_defaults():
    """Every provider carries the defaults config derives from the registry."""
    for cls in PROVIDER_CLASSES.values():
        assert cls.default_models
        assert isinstance(cls.env_keys, tuple)


# -- Command building --


def test_claude_build_command_no_session():
    """Without session_id, no --resume or --session-id flags."""
    p = ClaudeProvider("claude")
    cmd = p.build_command("hello", "sonnet")
    assert "--resume" not in cmd
    assert "--session-id" not in cmd
    assert "--continue" not in cmd


def test_claude_build_command_new_session():
    """New session (new: prefix) uses --session-id."""
    p = ClaudeProvider("claude")
    cmd = p.build_command(
        "hello", "sonnet", session_id=f"new:{CLAUDE_SESSION_ID}",
    )
    assert "--session-id" in cmd
    assert CLAUDE_SESSION_ID in cmd
    assert "new:" not in " ".join(cmd)


def test_claude_build_command_resume():
    """Existing session uses --resume."""
    p = ClaudeProvider("claude")
    cmd = p.build_command("hello", "sonnet", session_id=CLAUDE_SESSION_ID)
    assert "--resume" in cmd
    assert CLAUDE_SESSION_ID in cmd


def test_claude_build_batch_command():
    p = ClaudeProvider("claude")
    cmd = p.build_batch_command("hello", "opus")
    assert "text" in cmd
    assert "stream-json" not in cmd
    assert "--verbose" not in cmd
    assert "--continue" not in cmd


@pytest.mark.parametrize("session_id", [None, CLAUDE_SESSION_ID])
def test_claude_interactive_injects_shared_instructions(
    tmp_path, session_id,
):
    bundle = _instruction_bundle(tmp_path)

    cmd = ClaudeProvider("claude").build_command(
        "hello", "sonnet", session_id=session_id, instructions=bundle,
    )

    flag = cmd.index("--append-system-prompt-file")
    assert cmd[flag + 1] == str(bundle.snapshot_path)
    assert flag < cmd.index("--")
    assert cmd[-1] == "hello"


def test_claude_batch_injects_shared_instructions(tmp_path):
    bundle = _instruction_bundle(tmp_path)

    cmd = ClaudeProvider("claude").build_batch_command(
        "hello", "opus", instructions=bundle,
    )

    flag = cmd.index("--append-system-prompt-file")
    assert cmd[flag + 1] == str(bundle.snapshot_path)
    assert flag < cmd.index("--")


def test_claude_omits_instruction_flag_without_bundle():
    command = ClaudeProvider("claude").build_command("hello", "sonnet")
    batch = ClaudeProvider("claude").build_batch_command("hello", "sonnet")

    assert "--append-system-prompt-file" not in command
    assert "--append-system-prompt-file" not in batch


def test_codex_build_command():
    p = CodexProvider("codex")
    cmd = p.build_command("hello", "gpt-5.3-codex")
    assert cmd[0] == "codex"
    assert "exec" in cmd
    assert "--json" in cmd


def test_codex_build_command_resume():
    p = CodexProvider("codex")
    cmd = p.build_command("hello", "gpt-5.3-codex", session_id="thread_123")
    assert "resume" in cmd
    assert "thread_123" in cmd


def test_codex_build_batch_command():
    p = CodexProvider("codex")
    cmd = p.build_batch_command("hello", "gpt-5.3-codex")
    assert "--json" not in cmd


def _codex_developer_instructions(cmd):
    overrides = [
        cmd[index + 1]
        for index, item in enumerate(cmd[:-1])
        if item == "-c" and cmd[index + 1].startswith("developer_instructions=")
    ]
    assert len(overrides) == 1
    return tomllib.loads(overrides[0])["developer_instructions"]


@pytest.mark.parametrize("mode", ["fresh", "resume", "batch"])
def test_codex_injects_toml_safe_shared_instructions(tmp_path, mode):
    content = 'First line\nA "quoted" path: C:\\\\tools\\enso\nFinal line\t✓'
    bundle = _instruction_bundle(tmp_path, content)
    provider = CodexProvider("codex")

    if mode == "fresh":
        cmd = provider.build_command("hello", "sol", instructions=bundle)
    elif mode == "resume":
        cmd = provider.build_command(
            "hello", "sol", session_id="thread_123", instructions=bundle,
        )
    else:
        cmd = provider.build_batch_command("hello", "sol", instructions=bundle)

    assert _codex_developer_instructions(cmd) == content
    assert cmd.index("-c") < cmd.index("--")
    assert cmd[-1] == "hello"


def test_codex_omits_developer_instructions_without_bundle():
    provider = CodexProvider("codex")

    for cmd in (
        provider.build_command("hello", "sol"),
        provider.build_batch_command("hello", "sol"),
    ):
        assert not any(
            item.startswith("developer_instructions=") for item in cmd
        )


def test_codex_rejects_non_scalar_instruction_text(tmp_path):
    bundle = InstructionBundle(
        source_path=str(tmp_path / "AGENTS.md"),
        content="unsafe surrogate: \ud800",
        revision="a" * 64,
        snapshot_path=str(tmp_path / "shared-instructions.md"),
    )

    with pytest.raises(ValueError, match="Unicode scalar"):
        CodexProvider("codex").build_command(
            "hello", "sol", instructions=bundle,
        )


def test_codex_maximum_shared_instructions_fit_linux_single_argument_limit(tmp_path):
    content = "\x01" * MAX_SHARED_INSTRUCTION_BYTES
    bundle = _instruction_bundle(tmp_path, content)

    cmd = CodexProvider("codex").build_command(
        "hello", "sol", instructions=bundle,
    )
    override = next(
        item for item in cmd if item.startswith("developer_instructions=")
    )

    assert len(override.encode("utf-8")) + 1 < 128 * 1024


def test_codex_model_aliases_apply_to_all_command_modes():
    p = CodexProvider("codex")
    for alias, model_id in CODEX_MODEL_ALIASES.items():
        commands = [
            p.build_command("hello", alias),
            p.build_command("hello", alias, session_id="thread_123"),
            p.build_batch_command("hello", alias),
        ]
        for cmd in commands:
            assert cmd[cmd.index("-m") + 1] == model_id


def test_codex_unknown_model_passes_through():
    p = CodexProvider("codex")
    cmd = p.build_batch_command("hello", "gpt-5.5")
    assert cmd[cmd.index("-m") + 1] == "gpt-5.5"


def test_agy_build_command_creates_resumable_yolo_session_command():
    provider = AgyProvider("agy")
    cmd = provider.build_command(
        "hello",
        "gemini-3.6-flash-high",
        session_id="33333333-3333-4333-8333-333333333333",
        effort="low",
    )
    try:
        assert cmd[0] == "agy"
        assert "--dangerously-skip-permissions" in cmd
        assert cmd[cmd.index("--model") + 1] == "gemini-3.6-flash-high"
        assert "--effort" not in cmd
        assert cmd[cmd.index("--conversation") + 1] == (
            "33333333-3333-4333-8333-333333333333"
        )
        assert cmd[cmd.index("--prompt") + 1] == "hello"
        assert "--log-file" in cmd
    finally:
        provider.finalize_events()


def test_agy_build_command_without_session_starts_fresh():
    provider = AgyProvider("agy")
    cmd = provider.build_command("hello", AGY_MODELS[0])
    try:
        assert "--conversation" not in cmd
        assert "--new-project" in cmd
    finally:
        provider.finalize_events()


def test_agy_batch_command_is_plain_yolo_output():
    cmd = AgyProvider("agy").build_batch_command(
        "hello", "gemini-3.6-flash-low", effort="medium",
    )
    assert cmd == [
        "agy", "--dangerously-skip-permissions",
        "--model", "gemini-3.6-flash-low",
        "--new-project",
        "--prompt", "hello",
    ]


@pytest.mark.parametrize("mode", ["fresh", "resume", "batch"])
def test_agy_wraps_shared_instructions_and_preserves_prompt_verbatim(
    tmp_path, mode,
):
    bundle = _instruction_bundle(tmp_path, "Shared line one.\nShared line two.")
    prompt = "Original prompt.\n\n```py\nprint('unchanged')\n```\n"
    provider = AgyProvider("agy")
    try:
        if mode == "fresh":
            cmd = provider.build_command(
                prompt, AGY_MODELS[0], instructions=bundle,
            )
        elif mode == "resume":
            cmd = provider.build_command(
                prompt,
                AGY_MODELS[0],
                session_id="33333333-3333-4333-8333-333333333333",
                instructions=bundle,
            )
        else:
            cmd = provider.build_batch_command(
                prompt, AGY_MODELS[0], instructions=bundle,
            )
    finally:
        provider.finalize_events()

    wrapped = cmd[cmd.index("--prompt") + 1]
    assert wrapped == (
        "<enso-shared-instructions>\n"
        "Shared line one.\nShared line two.\n"
        "</enso-shared-instructions>\n\n"
        "<enso-user-prompt>\n"
        + prompt
    )
    assert wrapped.endswith(prompt)


def test_agy_preserves_plain_prompt_without_instruction_bundle():
    provider = AgyProvider("agy")
    try:
        cmd = provider.build_command("hello", AGY_MODELS[0])
    finally:
        provider.finalize_events()

    assert cmd[cmd.index("--prompt") + 1] == "hello"


def test_agy_print_timeout_stays_behind_enso_deadline():
    provider = AgyProvider("agy", timeout=900)
    try:
        interactive = provider.build_command(
            "hello",
            AGY_MODELS[0],
            session_id="55555555-5555-4555-8555-555555555555",
        )
        batch = provider.build_batch_command("hello", AGY_MODELS[0])
    finally:
        provider.finalize_events()

    for cmd in (interactive, batch):
        assert cmd[cmd.index("--print-timeout") + 1] == "905s"


def test_agy_disabled_enso_timeout_uses_maximum_cli_duration():
    provider = AgyProvider("agy", timeout=0)
    try:
        cmd = provider.build_command(
            "hello",
            AGY_MODELS[0],
            session_id="66666666-6666-4666-8666-666666666666",
        )
    finally:
        provider.finalize_events()

    assert cmd[cmd.index("--print-timeout") + 1] == (
        "2562047h47m16.854775807s"
    )


def test_agy_timeout_rounds_up_and_clamps_to_cli_maximum():
    rounded = AgyProvider("agy", timeout=0.01).build_batch_command(
        "hello", AGY_MODELS[0],
    )
    clamped = AgyProvider("agy", timeout=10**20).build_batch_command(
        "hello", AGY_MODELS[0],
    )

    assert rounded[rounded.index("--print-timeout") + 1] == "6s"
    assert clamped[clamped.index("--print-timeout") + 1] == (
        "2562047h47m16.854775807s"
    )


@pytest.mark.parametrize("model", AGY_MODELS)
def test_agy_concrete_model_slug_ignores_separate_effort(model):
    """Agy model slugs already encode every supported effort variant."""
    provider = AgyProvider("agy")
    try:
        interactive = provider.build_command(
            "hello",
            model,
            session_id="44444444-4444-4444-8444-444444444444",
            effort="low",
        )
        batch = provider.build_batch_command("hello", model, effort="high")
    finally:
        provider.finalize_events()

    assert interactive[interactive.index("--model") + 1] == model
    assert batch[batch.index("--model") + 1] == model
    assert "--effort" not in interactive
    assert "--effort" not in batch


def test_agy_preserves_multiline_final_output():
    provider = AgyProvider("agy")
    events = provider.parse_complete_output("First paragraph.\n\n```py\nprint('hi')\n```\n")
    assert len(events) == 1
    assert events[0].kind == "response"
    assert events[0].text == "First paragraph.\n\n```py\nprint('hi')\n```"


def test_agy_finalize_captures_authoritative_session_and_removes_log(tmp_path):
    log_file = tmp_path / "agy.log"
    log_file.write_text(
        "Created conversation 11111111-1111-4111-8111-111111111111\n"
        "Print mode: conversation=22222222-2222-4222-8222-222222222222, sending message\n"
    )
    provider = AgyProvider("agy")
    provider._log_path = str(log_file)

    events = provider.finalize_events()

    assert [(event.kind, event.session_id) for event in events] == [
        ("session", "22222222-2222-4222-8222-222222222222"),
    ]
    assert not log_file.exists()


def test_agy_finalize_falls_back_to_created_session(tmp_path):
    log_file = tmp_path / "agy.log"
    log_file.write_text(
        "Created conversation 33333333-3333-4333-8333-333333333333\n"
    )
    provider = AgyProvider("agy")
    provider._log_path = str(log_file)

    events = provider.finalize_events()

    assert events[0].session_id == "33333333-3333-4333-8333-333333333333"
    assert not log_file.exists()


# -- Agy project pinning --
#
# Antigravity pins conversations to a project at creation and print mode
# never derives one from cwd, so fresh conversations must pass --project
# (existing catalog entry) or --new-project (first use of a directory).


def _write_project(projects_dir, filename, project_id, uri, *, git=False):
    projects_dir.mkdir(parents=True, exist_ok=True)
    resource = {"gitFolder": {"folderUri": uri}} if git else {"folderUri": uri}
    (projects_dir / f"{filename}.json").write_text(json.dumps({
        "id": project_id,
        "name": "test-project",
        "projectResources": {"resources": [resource]},
    }))


def _fresh_command(provider, model=AGY_MODELS[0]):
    try:
        return provider.build_command("hello", model)
    finally:
        provider.finalize_events()


def test_agy_fresh_conversation_reuses_project_mapped_to_working_dir(
    tmp_path, agy_projects_dir,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_project(agy_projects_dir, "aaa", "proj-123", workspace.as_uri())

    cmd = _fresh_command(AgyProvider("agy", working_dir=str(workspace)))

    assert cmd[cmd.index("--project") + 1] == "proj-123"
    assert "--new-project" not in cmd


def test_agy_fresh_conversation_matches_git_folder_projects(
    tmp_path, agy_projects_dir,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_project(
        agy_projects_dir, "aaa", "proj-git", workspace.as_uri(), git=True,
    )

    cmd = _fresh_command(AgyProvider("agy", working_dir=str(workspace)))

    assert cmd[cmd.index("--project") + 1] == "proj-git"


def test_agy_resume_never_passes_project_flags(tmp_path, agy_projects_dir):
    """Resume re-pins from the conversation; project flags stay off."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_project(agy_projects_dir, "aaa", "proj-123", workspace.as_uri())

    provider = AgyProvider("agy", working_dir=str(workspace))
    try:
        cmd = provider.build_command("hello", AGY_MODELS[0], "session-1")
    finally:
        provider.finalize_events()

    assert "--project" not in cmd
    assert "--new-project" not in cmd


def test_agy_batch_command_reuses_mapped_project(tmp_path, agy_projects_dir):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_project(agy_projects_dir, "aaa", "proj-123", workspace.as_uri())

    cmd = AgyProvider("agy", working_dir=str(workspace)).build_batch_command(
        "hello", "gemini-3.6-flash-low",
    )

    assert cmd[cmd.index("--project") + 1] == "proj-123"
    assert "--new-project" not in cmd


def test_agy_project_lookup_resolves_encoded_and_symlinked_paths(
    tmp_path, agy_projects_dir,
):
    workspace = tmp_path / "work space"
    workspace.mkdir()
    link = tmp_path / "link"
    link.symlink_to(workspace)
    # as_uri() percent-encodes the space; trailing slash mimics hand-edited
    # catalog entries.
    _write_project(agy_projects_dir, "aaa", "proj-enc", workspace.as_uri() + "/")

    cmd = _fresh_command(AgyProvider("agy", working_dir=str(link)))

    assert cmd[cmd.index("--project") + 1] == "proj-enc"


def test_agy_project_lookup_skips_malformed_entries(tmp_path, agy_projects_dir):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agy_projects_dir.mkdir(parents=True)
    (agy_projects_dir / "broken.json").write_text("{not json")
    (agy_projects_dir / "no-id.json").write_text(json.dumps({
        "projectResources": {"resources": [{"folderUri": workspace.as_uri()}]},
    }))
    # Shape of Antigravity's own default-cli-project.json.
    (agy_projects_dir / "default-cli-project.json").write_text(json.dumps({
        "id": "default-cli-project", "projectResources": {},
    }))

    cmd = _fresh_command(AgyProvider("agy", working_dir=str(workspace)))

    assert "--new-project" in cmd
    assert "--project" not in cmd


def test_agy_project_lookup_is_stable_across_duplicates(
    tmp_path, agy_projects_dir,
):
    """Duplicate projects for one directory resolve by filename order."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_project(agy_projects_dir, "bbb", "proj-b", workspace.as_uri())
    _write_project(agy_projects_dir, "aaa", "proj-a", workspace.as_uri())

    cmd = _fresh_command(AgyProvider("agy", working_dir=str(workspace)))

    assert cmd[cmd.index("--project") + 1] == "proj-a"


def test_agy_without_working_dir_falls_back_to_cwd(
    tmp_path, agy_projects_dir, monkeypatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_project(agy_projects_dir, "aaa", "proj-cwd", workspace.as_uri())
    monkeypatch.chdir(workspace)

    cmd = _fresh_command(AgyProvider("agy"))

    assert cmd[cmd.index("--project") + 1] == "proj-cwd"


# -- Event parsing --


def test_claude_parse_result():
    p = ClaudeProvider("claude")
    events = p.parse_event({"type": "result", "result": "Hello!"})
    assert len(events) == 1
    assert events[0].kind == "response"
    assert events[0].text == "Hello!"


def test_claude_parse_api_error_assistant_message():
    """Claude marks synthetic API failures on the assistant envelope."""
    p = ClaudeProvider("claude")
    events = p.parse_event({
        "type": "assistant",
        "error": "model_not_found",
        "is_api_error_message": True,
        "message": {
            "model": "<synthetic>",
            "content": [{"type": "text", "text": "Model is unavailable."}],
        },
    })
    assert [(event.kind, event.text) for event in events] == [
        ("error", "Model is unavailable."),
    ]


def test_claude_parse_error_result_preserves_session():
    """A result can fail with no stderr, so its envelope is authoritative."""
    p = ClaudeProvider("claude")
    events = p.parse_event({
        "type": "result",
        "is_error": True,
        "terminal_reason": "api_error",
        "result": "Model is unavailable.",
        "session_id": CLAUDE_SESSION_ID,
    })
    assert [(event.kind, event.text, event.session_id) for event in events] == [
        ("error", "Model is unavailable.", None),
        ("session", "", CLAUDE_SESSION_ID),
    ]


def test_claude_parse_error_result_without_text_uses_terminal_reason():
    p = ClaudeProvider("claude")
    events = p.parse_event({
        "type": "result", "is_error": True, "terminal_reason": "api_error",
    })
    assert [(event.kind, event.text) for event in events] == [
        ("error", "api_error"),
    ]


def test_claude_parse_tool_use():
    p = ClaudeProvider("claude")
    events = p.parse_event({
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/foo.py"}}
            ]
        },
    })
    assert [(e.kind, e.text) for e in events] == [("status", "Reading foo.py")]


def test_claude_tool_status_prefers_the_models_own_description():
    """Bash carries a written description that beats echoing the command."""
    p = ClaudeProvider("claude")
    events = p.parse_event({
        "type": "assistant",
        "message": {
            "content": [{
                "type": "tool_use",
                "name": "Bash",
                "input": {
                    "command": "ls -la | rg foo",
                    "description": "List files in current directory",
                },
            }],
        },
    })
    assert [(e.kind, e.text) for e in events] == [
        ("status", "List files in current directory")
    ]


def test_claude_reports_thinking_even_when_the_text_is_redacted():
    """Recent models return empty thinking text; the activity still counts."""
    p = ClaudeProvider("claude")
    events = p.parse_event({
        "type": "assistant",
        "message": {"content": [{"type": "thinking", "thinking": ""}]},
    })
    assert [(e.kind, e.text) for e in events] == [("status", "Thinking")]


def test_claude_tool_status_truncates_a_long_command():
    p = ClaudeProvider("claude")
    events = p.parse_event({
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "x" * 500}}
            ]
        },
    })
    assert len(events[0].text) <= STATUS_TEXT_LIMIT
    assert events[0].text.endswith("…")


def test_codex_item_started_reports_the_unwrapped_command():
    """Codex wraps commands in a login shell; the status shows the real one."""
    p = CodexProvider("codex")
    events = p.parse_event({
        "type": "item.started",
        "item": {
            "type": "command_execution",
            "command": "/bin/zsh -lc \"sed -n '1,240p' notes.txt\"",
        },
    })
    assert [(e.kind, e.text) for e in events] == [
        ("status", "Running sed -n '1,240p' notes.txt")
    ]


def test_codex_item_started_reports_file_changes():
    p = CodexProvider("codex")
    events = p.parse_event({
        "type": "item.started",
        "item": {
            "type": "file_change",
            "changes": [
                {"path": "/repo/report.md", "kind": "add"},
                {"path": "/repo/other.md", "kind": "update"},
            ],
        },
    })
    assert [(e.kind, e.text) for e in events] == [
        ("status", "Writing report.md (+1 more)")
    ]


def test_codex_item_started_ignores_items_with_nothing_to_show():
    p = CodexProvider("codex")
    assert p.parse_event({"type": "item.started", "item": {"type": "agent_message"}}) == []


# -- Antigravity live progress --


def _write_trajectory(path, actions):
    """Build a stand-in for Antigravity's conversation trajectory store."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE steps (idx integer PRIMARY KEY, step_payload blob)")
    for idx, action in enumerate(actions):
        # The real payload is protobuf with an embedded tool-call JSON blob.
        payload = (
            b"\x08\x0f\x20\x03*\x02"
            + b'{"AbsolutePath":"/x","toolAction":"'
            + action.encode()
            + b'","toolSummary":"ignored"}'
        )
        con.execute("INSERT INTO steps VALUES (?, ?)", (idx, payload))
    con.commit()
    con.close()


async def _take(agen, count):
    collected = []
    try:
        for _ in range(count):
            collected.append(await asyncio.wait_for(agen.__anext__(), timeout=5))
    finally:
        await agen.aclose()
    return collected


@pytest.mark.asyncio
async def test_agy_poll_progress_reports_each_new_tool_action(tmp_path, monkeypatch):
    """Antigravity prints only a final answer; progress comes from the store."""
    conversation_id = "11111111-2222-3333-4444-555555555555"
    # Each tool writes an intent step and an execution step with one label.
    _write_trajectory(
        tmp_path / f"{conversation_id}.db",
        ["Reading notes file", "Reading notes file", "Listing files", "Writing report.md"],
    )
    monkeypatch.setattr(agy_module, "_CONVERSATIONS_DIR", tmp_path)

    provider = AgyProvider("agy")
    provider._session_id = conversation_id
    events = await _take(provider.poll_progress(), 3)

    assert [(e.kind, e.text) for e in events] == [
        ("status", "Reading notes file"),
        ("status", "Listing files"),
        ("status", "Writing report.md"),
    ]


@pytest.mark.asyncio
async def test_agy_poll_progress_discovers_the_conversation_from_the_log(
    tmp_path, monkeypatch,
):
    """A fresh conversation only learns its ID once the CLI announces it."""
    conversation_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    _write_trajectory(tmp_path / f"{conversation_id}.db", ["Counting lines"])
    monkeypatch.setattr(agy_module, "_CONVERSATIONS_DIR", tmp_path)

    log_path = tmp_path / "run.log"
    log_path.write_text(f"Created conversation {conversation_id.upper()}\n")

    provider = AgyProvider("agy")
    provider._log_path = str(log_path)
    events = await _take(provider.poll_progress(), 1)

    assert [e.text for e in events] == ["Counting lines"]


@pytest.mark.asyncio
async def test_agy_poll_progress_ends_quietly_without_a_conversation():
    """No conversation to watch must end the stream, not raise."""
    provider = AgyProvider("agy")
    provider._log_path = None
    assert [event async for event in provider.poll_progress()] == []


@pytest.mark.asyncio
async def test_agy_poll_progress_waits_out_a_missing_store(tmp_path, monkeypatch):
    """The trajectory appears a moment after launch; polling must not raise."""
    conversation_id = "99999999-8888-7777-6666-555555555555"
    monkeypatch.setattr(agy_module, "_CONVERSATIONS_DIR", tmp_path)
    monkeypatch.setattr(agy_module, "_PROGRESS_POLL_SECS", 0.01)

    provider = AgyProvider("agy")
    provider._session_id = conversation_id
    agen = provider.poll_progress()
    pending = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0.05)  # poll against a store that does not exist yet
    assert not pending.done()

    _write_trajectory(tmp_path / f"{conversation_id}.db", ["Running tests"])
    event = await asyncio.wait_for(pending, timeout=5)
    assert event.text == "Running tests"
    await agen.aclose()


# -- Batch (job) output parsing --


def test_base_provider_parse_batch_output_passthrough():
    """Non-streaming providers keep raw batch stdout."""
    provider = CodexProvider("codex")
    assert provider.parse_batch_output("  hello world \n") == "hello world"


def test_codex_parse_agent_message():
    p = CodexProvider("codex")
    events = p.parse_event({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "Done!"},
    })
    assert [(e.kind, e.text) for e in events] == [("response", "Done!")]


def test_codex_parse_session():
    p = CodexProvider("codex")
    events = p.parse_event({"type": "thread.started", "thread_id": "t_123"})
    assert any(e.kind == "session" and e.session_id == "t_123" for e in events)


def test_codex_parse_nested_turn_failure():
    p = CodexProvider("codex")
    events = p.parse_event({
        "type": "turn.failed", "error": {"message": "Model is unavailable."},
    })
    assert [(event.kind, event.text) for event in events] == [
        ("error", "Model is unavailable."),
    ]


def test_codex_parse_turn_failure_legacy_message_fallback():
    p = CodexProvider("codex")
    events = p.parse_event({"type": "turn.failed", "message": "Turn failed."})
    assert [(event.kind, event.text) for event in events] == [
        ("error", "Turn failed."),
    ]


# -- Stream buffering --


def test_stdout_limit_generous_default():
    """One long JSON event line (e.g. a full response) must not overrun the buffer."""
    assert ClaudeProvider("claude").stdout_limit() == 10 * 1024 * 1024
    assert CodexProvider("codex").stdout_limit() == 10 * 1024 * 1024


def test_agy_effort_levels_and_models():
    assert AgyProvider.effort_levels == []
    assert AgyProvider.default_models == AGY_MODELS


# -- Effort: command building --


def test_claude_build_command_with_effort():
    p = ClaudeProvider("claude")
    cmd = p.build_command("hi", "opus", effort="xhigh")
    assert "--effort" in cmd
    assert cmd[cmd.index("--effort") + 1] == "xhigh"


def test_claude_build_command_without_effort():
    p = ClaudeProvider("claude")
    cmd = p.build_command("hi", "opus")
    assert "--effort" not in cmd


def test_claude_build_batch_command_with_effort():
    p = ClaudeProvider("claude")
    cmd = p.build_batch_command("hi", "opus", effort="max")
    assert "--effort" in cmd
    assert cmd[cmd.index("--effort") + 1] == "max"
    # Prompt is still last and sentinel is preserved
    assert cmd[-1] == "hi"
    assert cmd[-2] == "--"


def test_codex_build_commands_with_effort():
    c = CodexProvider("codex")
    commands = [
        c.build_command("hi", "sol", effort="ultra"),
        c.build_command("hi", "terra", session_id="thread_123", effort="ultra"),
        c.build_batch_command("hi", "luna", effort="max"),
    ]
    for cmd in commands[:2]:
        assert cmd[cmd.index("-c") + 1] == 'model_reasoning_effort="ultra"'
    assert commands[2][commands[2].index("-c") + 1] == 'model_reasoning_effort="max"'
    assert all("--effort" not in cmd for cmd in commands)


def test_codex_build_command_without_effort_has_no_override():
    c = CodexProvider("codex")
    assert "model_reasoning_effort" not in " ".join(c.build_command("hi", "sol"))
    assert "model_reasoning_effort" not in " ".join(c.build_batch_command("hi", "sol"))


# -- Effort: clamping --


def test_effort_levels_ordered():
    assert ClaudeProvider.effort_levels == ["low", "medium", "high", "xhigh", "max"]


def test_max_effort_opus_is_max():
    assert ClaudeProvider.max_effort_for_model("opus") == "max"
    assert ClaudeProvider.max_effort_for_model("claude-opus-4-7") == "max"


def test_max_effort_sonnet_is_max():
    assert ClaudeProvider.max_effort_for_model("sonnet") == "max"
    assert ClaudeProvider.max_effort_for_model("claude-sonnet-5") == "max"


def test_max_effort_other_models_capped_at_high():
    assert ClaudeProvider.max_effort_for_model("haiku") == "high"
    assert ClaudeProvider.max_effort_for_model("unknown-model") == "high"


def test_clamp_effort_within_cap():
    # Opus and Sonnet support everything — no clamping.
    for model in ("opus", "sonnet"):
        assert ClaudeProvider.clamp_effort("max", model) == "max"
        assert ClaudeProvider.clamp_effort("xhigh", model) == "xhigh"
        assert ClaudeProvider.clamp_effort("low", model) == "low"


def test_clamp_effort_degrades_to_cap():
    # Haiku caps at high — xhigh/max clamp down.
    assert ClaudeProvider.clamp_effort("max", "haiku") == "high"
    assert ClaudeProvider.clamp_effort("xhigh", "haiku") == "high"
    assert ClaudeProvider.clamp_effort("high", "haiku") == "high"
    assert ClaudeProvider.clamp_effort("medium", "haiku") == "medium"


def test_clamp_effort_unknown_level_passthrough():
    assert ClaudeProvider.clamp_effort("bogus", "opus") == "bogus"


def test_codex_effort_levels_ordered():
    assert CodexProvider.effort_levels == ["low", "medium", "high", "xhigh", "max", "ultra"]


def test_codex_model_effort_caps():
    assert CodexProvider.max_effort_for_model("sol") == "ultra"
    assert CodexProvider.max_effort_for_model("gpt-5.6-terra") == "ultra"
    assert CodexProvider.max_effort_for_model("luna") == "max"
    assert CodexProvider.max_effort_for_model("gpt-5.5") == "xhigh"


def test_codex_clamp_effort():
    assert CodexProvider.clamp_effort("ultra", "sol") == "ultra"
    assert CodexProvider.clamp_effort("ultra", "luna") == "max"
    assert CodexProvider.clamp_effort("max", "gpt-5.5") == "xhigh"
    assert CodexProvider.clamp_effort("high", "luna") == "high"
