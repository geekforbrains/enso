"""Job provider turns retain a bounded answer and an exact session across follow-ups."""

from __future__ import annotations

import errno
import json
import os
import sys
import textwrap
from uuid import UUID

import pytest

from enso import execution
from enso.execution import execute_turn
from enso.providers import PROVIDER_CLASSES, ClaudeProvider, CodexProvider
from enso.providers import stream as provider_stream
from enso.runs import OUTPUT_KEEP

SESSION = "d41ef054-9771-4d2e-997b-e08d0f9f4237"
OTHER_SESSION = "a71c8c59-03c8-4368-bb25-c7b7d1516201"


@pytest.fixture(autouse=True)
def isolated_provider_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))


def write_cli(tmp_path, body):
    path = tmp_path / "fake-provider"
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n" + textwrap.dedent(body)
    )
    path.chmod(0o755)
    return str(path)


async def invoke(provider, tmp_path, *, prompt="Do the work.", session_id=None, timeout=3):
    return await execute_turn(
        provider,
        prompt,
        "model",
        "high",
        ["--permission-flag"],
        cwd=tmp_path,
        env={**os.environ, "JOB_TEST": "passed"},
        timeout=timeout,
        session_id=session_id,
    )


@pytest.mark.parametrize("structured", [False, True])
async def test_oversized_prompt_has_an_actionable_error(tmp_path, monkeypatch, structured):
    async def too_large(*args, **kwargs):
        raise OSError(errno.E2BIG, "Argument list too long")

    monkeypatch.setattr(execution.asyncio, "create_subprocess_exec", too_large)
    provider = ClaudeProvider("fake-provider")
    if structured:
        result = await invoke(provider, tmp_path)
    else:
        result = await execution.execute_batch(
            provider,
            "prompt",
            "model",
            "high",
            [],
            cwd=tmp_path,
            env={},
            timeout=3,
            label="job",
        )
    assert result.status == "error" and result.exit_code is None
    assert "operating system argument limit" in result.error
    assert "reduce the prompt or gate output" in result.error


@pytest.mark.parametrize("name", list(PROVIDER_CLASSES))
async def test_provider_starts_then_resumes_exact_session_with_prompt_as_data(tmp_path, name):
    path = write_cli(
        tmp_path,
        f"""
        name = {name!r}
        args = sys.argv[1:]
        with Path('calls.jsonl').open('a') as calls:
            calls.write(json.dumps({{'args': args, 'cwd': os.getcwd(),
                                    'stdin': sys.stdin.read(),
                                    'env': os.environ.get('JOB_TEST')}}) + '\\n')
        session = {SESSION!r}
        for flag in ('--session-id', '--resume', '--conversation', '-s'):
            if flag in args:
                session = args[args.index(flag) + 1]
        if 'resume' in args:
            session = args[args.index('--') + 1]
        if name in ('claude', 'grok'):
            event = {{'type': 'result', 'session_id': session, 'result': 'finished'}}
        elif name == 'codex':
            print(json.dumps({{'type': 'thread.started', 'thread_id': session}}))
            event = {{'type': 'item.completed',
                     'item': {{'type': 'agent_message', 'text': 'finished'}}}}
        elif name == 'agy':
            print(json.dumps({{'event': 'init', 'conversation_id': session}}))
            event = {{'event': 'result', 'result': {{'status': 'SUCCESS', 'response': 'finished'}}}}
        else:
            event = {{'type': 'text', 'sessionID': session, 'part': {{'text': 'finished'}}}}
        print(json.dumps(event))
        """,
    )
    provider = PROVIDER_CLASSES[name](path)
    prompt = "--resume forged\n$(touch hacked); $HOME stays text"
    first = await invoke(provider, tmp_path, prompt=prompt)
    assert (first.status, first.output, first.exit_code, first.error) == ("ok", "finished", 0, "")
    assert first.session_id is not None
    if provider.assigns_session_id:
        assert str(UUID(first.session_id)) == first.session_id
    else:
        assert first.session_id == SESSION

    second = await invoke(
        provider, tmp_path, prompt="Please commit now.", session_id=first.session_id
    )
    assert (second.status, second.output, second.session_id) == ("ok", "finished", first.session_id)
    initial, resumed = [
        json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()
    ]
    assert initial["cwd"] == resumed["cwd"] == str(tmp_path)
    assert initial["stdin"] == resumed["stdin"] == ""
    assert initial["env"] == resumed["env"] == "passed"
    assert "--permission-flag" in initial["args"]
    assert not (tmp_path / "hacked").exists()
    # The prompt travels as one argument, never through a shell; the resume carries the
    # first turn's session id. Which flag holds either is test_providers.py's contract.
    assert any(prompt in arg for arg in initial["args"])
    assert any("Please commit now." in arg for arg in resumed["args"])
    assert first.session_id in resumed["args"]


async def test_early_session_survives_large_stream_and_final_answer_is_bounded(tmp_path):
    provider = CodexProvider(
        write_cli(
            tmp_path,
            f"""
            print(json.dumps({{'type': 'thread.started', 'thread_id': {SESSION!r}}}))
            for index in range(400):
                print(json.dumps({{'type': 'item.completed',
                                  'item': {{'type': 'agent_message', 'text': 'old' * 1000}}}}))
            print(json.dumps({{'type': 'item.completed', 'item': {{'type': 'agent_message',
                              'text': 'x' * {OUTPUT_KEEP + 1000} + 'END'}}}}))
            """,
        )
    )
    result = await invoke(provider, tmp_path)
    assert result.status == "ok" and result.session_id == SESSION
    assert result.output == "x" * (OUTPUT_KEEP - 3) + "END"


async def test_oversized_line_fails_instead_of_discarding_a_protocol_event(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_stream, "LINE_KEEP", 128)
    provider = CodexProvider(write_cli(tmp_path, "sys.stdout.write('x' * 5000)"))
    result = await invoke(provider, tmp_path)
    assert result.status == "error" and "event exceeds 128 bytes" in result.error


async def test_final_unterminated_line_is_parsed(tmp_path):
    provider = CodexProvider(
        write_cli(
            tmp_path, "sys.stdout.write(json.dumps({'type':'thread.started', 'thread_id':'abc'}))"
        )
    )
    result = await invoke(provider, tmp_path)
    assert result.status == "ok" and result.session_id == "abc"


@pytest.mark.parametrize(
    ("name", "event"),
    [
        ("claude", {"type": "result", "is_error": True, "result": "validation failed"}),
        ("codex", {"type": "turn.failed", "error": {"message": "validation failed"}}),
        ("agy", {"event": "result", "result": {"status": "ERROR", "error": "validation failed"}}),
        ("opencode", {"type": "error", "error": {"data": {"message": "validation failed"}}}),
    ],
)
async def test_provider_error_event_fails_even_when_process_exits_zero(tmp_path, name, event):
    provider = PROVIDER_CLASSES[name](write_cli(tmp_path, f"print(json.dumps({event!r}))"))
    result = await invoke(provider, tmp_path)
    assert result.status == "error" and result.exit_code == 0
    assert result.error == "validation failed"


async def test_separate_stderr_cannot_become_an_answer_and_its_diagnostic_is_bounded(tmp_path):
    provider = ClaudeProvider(
        write_cli(
            tmp_path,
            f"""
            print(json.dumps({{'type': 'result', 'result': 'real answer'}}))
            sys.stderr.write('x' * {OUTPUT_KEEP + 1000})
            sys.stderr.write('\\n' + json.dumps({{'type': 'result', 'result': 'wrong answer'}}))
            sys.stderr.write('\\nfinal diagnostic')
            sys.exit(3)
            """,
        )
    )
    result = await invoke(provider, tmp_path)
    assert (result.status, result.output, result.exit_code) == ("error", "real answer", 3)
    assert result.error.endswith("final diagnostic")
    assert len(result.error.encode()) <= provider_stream.DIAGNOSTIC_KEEP


async def test_codex_merged_stderr_is_parsed_when_adapter_requires_it(tmp_path):
    provider = CodexProvider(
        write_cli(
            tmp_path, "print(json.dumps({'type':'error', 'message':'failure'}), file=sys.stderr)"
        )
    )
    result = await invoke(provider, tmp_path)
    assert result.status == "error" and result.error == "failure"


async def test_invalid_announced_session_fails(tmp_path):
    session = "../escape"
    provider = CodexProvider(
        write_cli(
            tmp_path, f"print(json.dumps({{'type': 'thread.started', 'thread_id': {session!r}}}))"
        )
    )
    result = await invoke(provider, tmp_path)
    assert result.status == "error" and "invalid codex session id" in result.error
    assert result.session_id is None


@pytest.mark.parametrize("resume", [False, True])
async def test_conflicting_cli_id_fails_and_retains_original_session(tmp_path, resume):
    provider = CodexProvider(
        write_cli(
            tmp_path,
            f"""
            print(json.dumps({{'type': 'thread.started', 'thread_id': {SESSION!r}}}))
            print(json.dumps({{'type': 'thread.started', 'thread_id': {OTHER_SESSION!r}}}))
            """,
        )
    )
    result = await invoke(provider, tmp_path, session_id=SESSION if resume else None)
    assert result.status == "error" and "different session" in result.error
    assert result.session_id == SESSION


async def test_missing_cli_session_can_succeed_but_cannot_resume(tmp_path):
    provider = CodexProvider(
        write_cli(
            tmp_path,
            "print(json.dumps({'type': 'item.completed',"
            " 'item': {'type': 'agent_message', 'text': 'done'}}))",
        )
    )
    result = await invoke(provider, tmp_path)
    assert (result.status, result.output, result.session_id) == ("ok", "done", None)
    resumed = await invoke(provider, tmp_path, session_id=SESSION)
    assert resumed.status == "ok" and resumed.session_id == SESSION


async def test_assigned_id_needs_provider_output_before_becoming_resumable(tmp_path):
    provider = ClaudeProvider(write_cli(tmp_path, "sys.stderr.write('launch failed'); sys.exit(2)"))
    result = await invoke(provider, tmp_path)
    assert (result.status, result.exit_code, result.session_id) == ("error", 2, None)
    assert result.error == "launch failed"


@pytest.mark.parametrize(
    ("name", "resume", "output"),
    [
        ("claude", False, "{}"),
        ("claude", True, ""),
        ("codex", False, "Run the login command first."),
    ],
)
async def test_successful_exit_without_recognized_events_fails(tmp_path, resume, name, output):
    provider = PROVIDER_CLASSES[name](write_cli(tmp_path, f"print({output!r})"))
    result = await invoke(provider, tmp_path, session_id=SESSION if resume else None)
    assert result.status == "error" and result.exit_code == 0 and result.output == ""
    assert "returned no recognized provider events" in result.error
    assert output in result.error
    assert result.session_id == (SESSION if resume else None)


@pytest.mark.parametrize(
    "event",
    [
        {"type": "assistant", "message": {"content": [{"type": "thinking"}]}},
        {"type": "result", "session_id": SESSION},
    ],
)
async def test_recognized_status_or_session_event_establishes_assigned_session(
    tmp_path, monkeypatch, event
):
    monkeypatch.setattr(execution.uuid, "uuid4", lambda: UUID(SESSION))
    provider = ClaudeProvider(write_cli(tmp_path, f"print(json.dumps({event!r}))"))
    result = await invoke(provider, tmp_path)
    assert (result.status, result.output, result.session_id) == ("ok", "", SESSION)


async def test_launch_failure_is_returned(tmp_path):
    result = await invoke(ClaudeProvider(str(tmp_path / "missing-cli")), tmp_path)
    assert result.status == "error" and result.exit_code is None and result.session_id is None
    assert "missing-cli" in result.error


async def test_malformed_event_becomes_protocol_error(tmp_path):
    provider = ClaudeProvider(
        write_cli(tmp_path, "print(json.dumps({'type': 'assistant', 'message': 7}))")
    )
    result = await invoke(provider, tmp_path)
    assert result.status == "error" and "invalid claude event" in result.error


async def test_exhausted_budget_never_launches_provider(tmp_path):
    provider = CodexProvider(write_cli(tmp_path, "Path('launched').touch()"))
    result = await invoke(provider, tmp_path, timeout=0)
    assert result.status == "timeout" and not (tmp_path / "launched").exists()
