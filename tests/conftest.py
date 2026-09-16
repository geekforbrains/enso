"""Shared fixtures: a scratch ``ENSO_HOME``, a valid config document, a fake-CLI runtime."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path

import pytest
import yaml

from enso import db
from enso.config import Config, Paths, parse_config
from enso.jobs import Job, find_job, render
from enso.runtime import Runtime, origin_block
from enso.transports import Reply, Transport, Turn

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CLAUDE = FIXTURES / "fake_claude.py"
FAKE_AGY = FIXTURES / "fake_agy.py"
FAKE_OPENCODE = FIXTURES / "fake_opencode.py"

VALID_CONFIG: dict = {
    "version": 2,
    "transports": {
        "slack": {"bot_token": "xoxb-test", "app_token": "xapp-test", "notify": "C1"},
    },
    "bindings": {"slack:dm:U1": "default", "slack:C1": "default"},
    "defaults": {"provider": "claude", "model": "opus", "effort": "xhigh"},
    "providers": {
        # haiku is absent from Claude's cap table, so it tops out at high
        "claude": {
            "path": sys.executable,
            "models": ["opus", "sonnet", "haiku"],
            "args": ["--skip"],
        },
        "codex": {"path": sys.executable, "models": ["astra", "sol", "terra", "luna"], "args": []},
        "grok": {"path": sys.executable, "models": ["grok-4.6"], "args": ["--always-approve"]},
    },
    "agent": {"timeout": 30},
}
TELEGRAM_CONFIG: dict = {
    "bot_token": "123456:token",
    "notify": "123",
}
# Two projects: a plain agent pipeline, and one with a human stage in the middle.
PROJECTS: dict = {
    "EN": {"name": "Enso", "workspace": "default", "stages": ["triage", "todo", "review"]},
    "MKT": {
        "name": "Marketing",
        "workspace": "default",
        "stages": ["draft", "approve:human", "release"],
    },
}


class FakeReply(Reply):
    """Records everything the runtime sends for one turn."""

    limit = 4000

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.status: list[str] = []
        self.deleted = False

    async def send(self, text: str) -> str:
        self.sent.append(text)
        return str(len(self.sent))

    async def send_file(self, path: str, caption: str = "") -> str:
        return "f"

    async def status_post(self, text: str) -> str:
        self.status.append(text)
        return "status"

    async def status_edit(self, message_id: str, text: str) -> None:
        self.status.append(text)

    async def status_delete(self, message_id: str) -> None:
        self.deleted = True

    def origin_env(self) -> dict[str, str]:
        return {"ENSO_ORIGIN_TRANSPORT": "slack"}


class FakeTransport(Transport):
    """Records sends; ``start`` returns at once, or blocks until cancelled and notes its cleanup."""

    def __init__(self, name: str = "slack", *, block: bool = False):
        self.name = name
        self.block = block
        self.started = False
        self.cleaned_up = False
        self.sent: list[tuple[str, str]] = []

    async def start(self, runtime: Runtime) -> None:
        self.started = True
        if not self.block:
            return
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.02)  # a stop that yields, like Telegram's last API call
            self.cleaned_up = True

    async def send(self, target: str, text: str, *, thread: str | None = None) -> str:
        self.sent.append((target, text))
        return str(len(self.sent))

    async def send_file(
        self, target: str, path: str, *, caption: str = "", thread: str | None = None
    ) -> str:
        return ""

    async def edit(self, target: str, message_id: str, text: str) -> None: ...

    async def delete(self, target: str, message_id: str) -> None: ...

    async def fetch_thread(self, target: str, thread: str) -> list[dict]:
        return []

    async def receipt(
        self, target: str, message_id: str | None, thread: str | None, *, file: bool
    ) -> dict:
        return {"ok": True, "transport": self.name, "target": target, "message_id": message_id}


class FakeSlack:
    """Stands in for ``AsyncWebClient``: records every call, answers from canned responses.

    A response may be a dict (merged over ``{"ok": True}``), a callable taking the
    call's kwargs, or an exception to raise.
    """

    def __init__(self, **responses: object):
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, object] = {
            "chat_postMessage": {"ts": "100.1"},
            "chat_getPermalink": {"permalink": "https://x.slack.com/archives/C1/p1001"},
            "files_upload_v2": {"file": {"id": "F1"}},
            **responses,
        }

    def __getattr__(self, method: str) -> Callable[..., Awaitable[dict]]:
        async def call(**kwargs: object) -> dict:
            self.calls.append((method, kwargs))
            response = self.responses.get(method, {})
            if isinstance(response, BaseException):
                raise response
            if callable(response):
                response = response(kwargs)
            return {"ok": True, **response}  # type: ignore[dict-item]

        return call

    def sent(self, method: str) -> list[dict]:
        return [kwargs for name, kwargs in self.calls if name == method]


def make_turn(text: str, thread: str | None = None, *, transport: str = "slack") -> Turn:
    """A DM turn on ``slack:D1`` (user U1) or ``telegram:123``."""
    channel, user = ("123", "123") if transport == "telegram" else ("D1", "U1")
    return Turn(
        transport=transport,
        channel=channel,
        thread=thread,
        message_id="1.0",
        user_id=user,
        user_name="gavin",
        text=text,
        is_dm=True,
    )


def chat_prompt(text: str, thread: str | None = None, *, transport: str = "slack") -> str:
    """What a ``make_turn`` message reaches a provider as: the origin block, then the text."""
    return f"{origin_block(make_turn(text, thread, transport=transport))}\n\n{text}"


@pytest.fixture
def enso_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Paths:
    """Scratch Enso and user homes; user-service units live outside ``ENSO_HOME``."""
    user_home = tmp_path / "isolated-user-home"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    home = tmp_path / "enso"
    (home / "workspaces" / "default").mkdir(parents=True)
    for key in [key for key in os.environ if key.startswith("ENSO_")]:
        monkeypatch.delenv(key)  # a run started from a chat turn or job must not leak in
    monkeypatch.setenv("ENSO_HOME", str(home))
    return Paths(home)


@pytest.fixture
def raw_config() -> dict:
    return copy.deepcopy(VALID_CONFIG)


@pytest.fixture
def raw_config_both(raw_config: dict) -> dict:
    """Slack plus Telegram, with the Telegram chat bound to ``default``."""
    raw_config["transports"]["telegram"] = copy.deepcopy(TELEGRAM_CONFIG)
    raw_config["bindings"]["telegram:123"] = "default"
    return raw_config


@pytest.fixture
def raw_config_projects(raw_config: dict) -> dict:
    raw_config["projects"] = copy.deepcopy(PROJECTS)
    return raw_config


@pytest.fixture
def project_config(enso_home: Paths, raw_config_projects: dict) -> Config:
    """A config with the two projects, the schema initialized, and the file written."""
    config, problems, _ = parse_config(raw_config_projects, enso_home)
    assert config is not None, problems
    write_config(enso_home, raw_config_projects)
    db.initialize(enso_home)
    return config


@pytest.fixture
def config(enso_home: Paths, raw_config: dict):
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is not None, problems
    return config


@pytest.fixture
def config_both(enso_home: Paths, raw_config_both: dict):
    config, problems, _ = parse_config(raw_config_both, enso_home)
    assert config is not None, problems
    return config


def write_workspace(paths: Paths, name: str, fields: dict) -> Path:
    """Write settings in an existing scratch workspace through the real Markdown format."""
    path = paths.workspace_settings(name)
    path.write_text("---\n" + yaml.safe_dump(fields, sort_keys=False) + "---\n", "utf-8")
    return path


def write_config(paths: Paths, raw: dict) -> None:
    paths.home.mkdir(parents=True, exist_ok=True)
    paths.config.write_text(json.dumps(raw))


@pytest.fixture
def fake_claude() -> str:
    """Path of the executable stand-in for the Claude CLI."""
    os.chmod(FAKE_CLAUDE, 0o755)
    return str(FAKE_CLAUDE)


@pytest.fixture
def fake_agy() -> str:
    """Path of the executable stand-in for the Antigravity CLI."""
    os.chmod(FAKE_AGY, 0o755)
    return str(FAKE_AGY)


@pytest.fixture
def fake_opencode() -> str:
    """Path of the executable stand-in for the OpenCode CLI."""
    os.chmod(FAKE_OPENCODE, 0o755)
    return str(FAKE_OPENCODE)


@pytest.fixture
def fake_config(enso_home: Paths, raw_config_both: dict, fake_claude: str) -> Config:
    """Both transports, ``claude`` pointing at the fake CLI, a 5 s turn timeout, schema ready."""
    raw_config_both["providers"]["claude"]["path"] = fake_claude
    raw_config_both["agent"]["timeout"] = 5
    config, problems, _ = parse_config(raw_config_both, enso_home)
    assert config is not None, problems
    db.initialize(enso_home)
    return config


def script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *responses: str) -> None:
    """Make the fake CLI answer with these texts, one per invocation, then echo again."""
    directory = tmp_path / "responses"
    directory.mkdir(exist_ok=True)
    for index, text in enumerate(responses):
        (directory / f"{index}.txt").write_text(text)
    monkeypatch.setenv("FAKE_RESPONSES", str(directory))


@pytest.fixture
def runtime(fake_config: Config) -> Runtime:
    return Runtime(fake_config)


JOB_FIELDS: dict[str, object] = {
    "name": "Nightly",
    "schedule": "0 9 * * *",
    "provider": "claude",
    "model": "opus",
    "effort": "high",
    "enabled": True,
}


def write_job(
    paths: Paths,
    dir_name: str = "nightly",
    *,
    prompt: str = "Say hi.",
    workspace: str = "default",
    omit: Iterable[str] = (),
    **fields: object,
) -> Path:
    """Write a workspace ``jobs/<dir_name>/JOB.md`` with sensible fields, overridden by ``fields``.

    The frontmatter is rendered as YAML, so a value's Python type is the type the parser
    sees: pass ``enabled=False`` for the boolean and ``timeout="60"`` for the quoted string
    a strict parser must refuse. ``omit`` drops fields entirely, which is not the same as
    giving one an empty value.
    """
    given = {**JOB_FIELDS, **fields}
    front = {key: value for key, value in given.items() if key not in set(omit)}
    path = paths.workspace_jobs(workspace) / dir_name / "JOB.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(front, prompt))
    return path


def load_job(paths: Paths, config: Config, dir_name: str = "nightly") -> Job:
    job, problems = find_job(paths, config, dir_name if ":" in dir_name else f"default:{dir_name}")
    assert job is not None and not problems, problems
    return job


# -- Git repositories for worktree and stage-run tests -------------------------


def git(cwd: Path, *args: str) -> str:
    """Run one Git command in ``cwd`` and return its stdout; raises on failure."""
    done = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=60
    )
    return done.stdout


def commit_file(cwd: Path, name: str, content: str, message: str) -> str:
    """Write ``name``, stage it, commit, and return the new HEAD sha."""
    (cwd / name).parent.mkdir(parents=True, exist_ok=True)
    (cwd / name).write_text(content)
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", message)
    return git(cwd, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A main checkout on ``main`` with one commit, isolated from the user's Git config.

    Holds ``AGENTS.md`` (project instructions), a ``.gitignore`` for ``.env`` and the
    ``setup.txt`` the tests' setup commands write (a real setup's ``.venv`` is ignored the same
    way), and an untracked ``.env`` so ``copy`` has something gitignored to carry into a
    worktree.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(key, "Test")
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "test@example.test")
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / ".gitignore").write_text(".env\nsetup.txt\n")
    (path / "README.md").write_text("# Repo\n")
    (path / "AGENTS.md").write_text("# Project rules\n\nRun the checks before handing off.\n")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "init")
    (path / ".env").write_text("SECRET=1\n")
    return path
