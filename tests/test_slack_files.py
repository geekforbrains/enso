"""Slack attachments: where a download may write, and where the bot token may go."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from conftest import FakeSlack
from yarl import URL

from enso import db
from enso.config import Config
from enso.runtime import Runtime
from enso.transports import Reply, Turn
from enso.transports import slack as slack_module
from enso.transports.slack import (
    FILE_DOWNLOAD_LIMIT,
    LOG_VALUE_LIMIT,
    SlackTransport,
    _brief,
    _download_url,
    _local_name,
)

SLACK_URL = "https://files.slack.com/files-pri/T1-F1/notes.txt"
HEX = set("0123456789abcdef")


def transport_for(config: Config) -> SlackTransport:
    assert config.slack is not None
    return SlackTransport(config.slack, config.paths)


def uploads(config: Config) -> Path:
    return config.paths.workspace("default") / "uploads"


def turn_dir(config: Config) -> Path:
    """The single per-turn directory ``download_files`` allocated under ``uploads/``."""
    children = [child for child in sorted(uploads(config).iterdir()) if child.is_dir()]
    assert len(children) == 1, children
    return children[0]


# -- The download endpoint policy itself --


@pytest.mark.parametrize(
    "url",
    [
        "http://files.slack.com/files-pri/T1-F1/notes.txt",  # plaintext
        "https://files.slack.com.evil.example/files-pri/x",  # suffix lookalike
        "https://notslack.com/files-pri/x",  # bare-domain lookalike
        "https://evil.example/files-pri/x",  # somewhere else entirely
        "https://user:secret@files.slack.com/files-pri/x",  # credentials in the url
        "https://files.slack.com:8443/files-pri/x",  # non-default port
        "https://files.slack.com:notaport/files-pri/x",  # malformed authority
        "file:///etc/passwd",
        "/files-pri/x",  # no origin at all
        "https://files.slack.com/" + "a" * 4000,  # unbounded
        "https://hooks.slack.com/services/T1/B1/secret",  # an incoming webhook, not a file
        "https://slack.com/api/files.info?file=F1",  # the web api, not a file
        "https://enterprise.slack.com/files-pri/T1-F1/notes.txt",  # a workspace domain
        "https://files.slack.com/api/files.info",  # right host, not the file endpoint
        "https://files.slack.com/files-pri/../api/files.info",  # climbing back out of it
        "https://files.slack.com/files-pri/%2e%2e/api/files.info",  # encoded, same climb
        "https://files.slack.com/files-pri/%2E%2E%2F%2E%2E/api/x",  # encoded separator too
        "https://files.slack.com/files-pri/./../api/files.info",  # a bare dot on the way
    ],
)
def test_only_slacks_file_download_endpoint_is_fetchable(url: str) -> None:
    assert _download_url({"url_private_download": url}) == ""


@pytest.mark.parametrize(
    "url",
    [
        SLACK_URL,
        "https://files.slack.com:443/files-pri/T1-F1/notes.txt",
        "https://FILES.Slack.com/files-pri/T1-F1/notes.txt",
        "https://files.slack.com/files-pri/T1-F1/download/notes.txt?t=xoxe-1-abc",
    ],
)
def test_slacks_private_file_urls_are_fetchable(url: str) -> None:
    approved = _download_url({"url_private": url})

    assert approved == url
    assert URL(approved).raw_path.startswith("/files-pri/")  # and it is still sent there


# -- The local name --


@pytest.mark.parametrize(
    ("info", "tail"),
    [
        ({"id": "F1", "name": "notes.txt"}, "notes.txt"),
        ({"id": "../../../etc/cron.d/enso", "name": "notes.txt"}, "notes.txt"),
        ({"id": "F1", "name": "/etc/passwd"}, "etc_passwd"),
        ({"id": "F1", "name": "../../escape.sh"}, "escape.sh"),
        ({"id": "F1", "name": "a/b\\c.txt"}, "a_b_c.txt"),
        ({"id": "F1", "name": "we\x00ird\nname.txt"}, "we_ird_name.txt"),
        ({"id": "F1", "name": ".."}, ""),
        ({"id": "F1", "name": "....."}, ""),
        ({"id": "F1", "name": "‮gnp.exe"}, "gnp.exe"),  # right-to-left override
        ({"id": "F1", "title": "from the title.pdf"}, "from_the_title.pdf"),
        ({"id": "F1"}, ""),
        ({"id": "F" * 400, "name": "x" * 400 + ".txt"}, "x" * 48 + ".txt"),
        ({"id": "F1", "name": "notes." + "t" * 100}, "notes." + "t" * 16),
    ],
)
def test_local_names_are_enso_generated_and_bounded(info: dict, tail: str) -> None:
    name = _local_name(info)
    token, _, kept = name.partition("-") if tail else (name, "", "")
    assert len(token) == 32 and set(token) <= HEX
    assert kept == tail  # readable, but only the part Enso chose to keep
    assert _local_name(info) != name  # opaque and fresh on every attachment
    assert Path("/uploads", name).parent == Path("/uploads")  # one ordinary component
    assert len(name) < 100


# -- A loopback stand-in for files.slack.com --


class Server:
    """A loopback file host that records the path and credential of every request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self.body = b"attachment body"
        self.serving = asyncio.Event()
        self.release = asyncio.Event()
        self._server: TestServer | None = None

    async def start(self) -> None:
        app = web.Application()
        app.add_routes(
            [
                web.get("/file", self._file),
                web.get("/elsewhere", self._file),
                web.get("/big", self._big),
                web.get("/slow", self._slow),
                web.get("/redirect", self._redirect),
            ]
        )
        self._server = TestServer(app)
        await self._server.start_server()

    async def stop(self) -> None:
        assert self._server is not None
        self.release.set()
        await self._server.close()

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.port)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}/{path}"

    def _record(self, request: web.Request) -> None:
        self.requests.append((request.path, request.headers.get("Authorization", "")))

    async def _file(self, request: web.Request) -> web.StreamResponse:
        self._record(request)
        return web.Response(body=self.body)

    async def _big(self, request: web.Request) -> web.StreamResponse:
        self._record(request)
        response = web.StreamResponse()
        await response.prepare(request)
        chunk = b"x" * (1 << 20)
        for _ in range(FILE_DOWNLOAD_LIMIT // len(chunk) + 2):
            await response.write(chunk)
        return response

    async def _slow(self, request: web.Request) -> web.StreamResponse:
        """Send one chunk, then hold the connection open until the test lets go."""
        self._record(request)
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b"partial")
        self.serving.set()
        await self.release.wait()
        return response

    async def _redirect(self, request: web.Request) -> web.StreamResponse:
        self._record(request)
        raise web.HTTPFound(location="/elsewhere")


@pytest.fixture
async def server(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Server]:
    """The loopback host, with the download policy repointed at it for the duration.

    The real policy is unit-tested above; this stand-in keeps its shape — one exact
    endpoint and nothing else — so these tests measure what approval and rejection *do*
    rather than re-deciding which endpoints are approved.
    """
    running = Server()
    await running.start()
    approved = f"127.0.0.1:{running.port}"
    monkeypatch.setattr(
        slack_module,
        "_approved_download",
        lambda parts: parts.scheme == "http" and parts.netloc == approved,
    )
    yield running
    await running.stop()


# -- What a download may fetch --


async def test_a_valid_download_is_authenticated_and_streamed(
    config: Config, server: Server
) -> None:
    transport = transport_for(config)
    files = [
        {"id": "F1", "name": "notes.txt", "url_private_download": server.url("file")},
        {"id": "F2", "name": "missing.txt", "url_private_download": "https://example.test"},
    ]

    (path,) = await transport.download_files(files, "default")

    assert server.requests == [("/file", "Bearer xoxb-test")]
    assert Path(path).read_bytes() == server.body
    assert Path(path).parent.parent == uploads(config)
    assert Path(path).name.endswith("-notes.txt")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:{port}/file",  # the same server, an unapproved host
        "http://user:secret@127.0.0.1:{port}/file",  # credential-bearing
        "http://127.0.0.1:notaport/file",  # malformed
        "gopher://127.0.0.1:{port}/file",
        "https://evil.example/file",
        "",
    ],
)
async def test_an_unapproved_url_is_never_requested(
    config: Config, server: Server, url: str
) -> None:
    files = [{"id": "F1", "name": "notes.txt", "url_private": url.format(port=server.port)}]

    assert await transport_for(config).download_files(files, "default") == []
    assert server.requests == []  # no request at all, so no Authorization to disclose
    assert list(turn_dir(config).iterdir()) == []


async def test_a_redirect_is_refused_rather_than_followed(config: Config, server: Server) -> None:
    files = [{"id": "F1", "name": "notes.txt", "url_private_download": server.url("redirect")}]

    assert await transport_for(config).download_files(files, "default") == []
    assert [path for path, _ in server.requests] == ["/redirect"]  # /elsewhere never asked
    assert list(turn_dir(config).iterdir()) == []


async def test_the_streaming_limit_stops_an_oversized_file_and_removes_it(
    config: Config, server: Server, caplog: pytest.LogCaptureFixture
) -> None:
    """The bytes actually streamed decide, not any size the metadata claims."""
    files = [
        {
            "id": "F1",
            "name": "huge.bin",
            "size": 12,
            "url_private_download": server.url("big"),
        }
    ]

    assert await transport_for(config).download_files(files, "default") == []
    assert "exceeds the 100 MiB download limit" in caplog.text
    assert list(turn_dir(config).iterdir()) == []


async def test_a_rejected_file_does_not_cost_the_safe_ones_in_the_same_message(
    config: Config, server: Server
) -> None:
    files = [
        {"id": "F1", "name": "first.txt", "url_private_download": server.url("file")},
        {"id": "../../../etc/cron.d/x", "name": "/etc/passwd", "url_private": "http://evil/x"},
        {"id": "F3", "name": "third.txt", "url_private_download": server.url("file")},
    ]

    paths = await transport_for(config).download_files(files, "default")

    assert [Path(p).name.split("-", 1)[1] for p in paths] == ["first.txt", "third.txt"]
    assert [path for path, _ in server.requests] == ["/file", "/file"]


# -- What a download, or its cleanup, may touch --


def sentinels(config: Config, *, in_uploads: bool = True) -> list[Path]:
    """Files a hostile name, or a careless cleanup, would love to reach."""
    home = config.paths.home
    targets = [home / "config.json", home / "workspaces" / "note.md"]
    if in_uploads:
        targets.append(uploads(config) / "keep.txt")
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("sentinel")
    return targets


async def test_hostile_metadata_cannot_reach_a_file_outside_the_uploads_directory(
    config: Config, server: Server
) -> None:
    guarded = sentinels(config)
    files = [
        {
            "id": f"../../../../{target.relative_to(config.paths.home.parent)}",
            "name": f"../../{target.name}",
            "url_private_download": server.url("file"),
        }
        for target in guarded
    ]

    paths = await transport_for(config).download_files(files, "default")

    assert [target.read_text() for target in guarded] == ["sentinel"] * len(guarded)
    assert all(Path(path).parent.parent == uploads(config) for path in paths)


async def test_a_destination_outside_the_turn_directory_is_refused_before_the_request(
    config: Config, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The containment proof, not the generated name, is what keeps cleanup honest."""
    guarded = sentinels(config)
    monkeypatch.setattr(slack_module, "_local_name", lambda info: f"../../../{guarded[0].name}")
    files = [{"id": "F1", "name": "notes.txt", "url_private_download": server.url("file")}]

    assert await transport_for(config).download_files(files, "default") == []
    assert server.requests == []  # refused before the token could move
    assert guarded[0].read_text() == "sentinel"


async def test_cancellation_removes_this_turns_files_and_nothing_else(
    config: Config, server: Server
) -> None:
    guarded = sentinels(config, in_uploads=False)
    files = [
        {"id": "F1", "name": "first.txt", "url_private_download": server.url("file")},
        {"id": "F2", "name": "second.txt", "url_private_download": server.url("slow")},
    ]
    task = asyncio.create_task(transport_for(config).download_files(files, "default"))
    await asyncio.wait_for(server.serving.wait(), 5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [target.read_text() for target in guarded] == ["sentinel"] * len(guarded)
    assert list(uploads(config).iterdir()) == []  # the finished file and the partial one


# -- What reaches the log --


def test_a_log_line_is_bounded_and_never_carries_a_credential() -> None:
    rendered = _brief("GET https://files.slack.com/x?t=xoxe-1-secret\nAuthorization: Bearer xoxb-9")

    assert "xox" not in rendered and rendered.count("[redacted]") == 2
    assert len(_brief("x" * 500)) == LOG_VALUE_LIMIT + 1  # bounded, plus the ellipsis


async def test_a_files_info_failure_is_logged_without_the_token_or_a_traceback(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """A Slack SDK error quotes the request it made, which carries the credential."""
    assert config.slack is not None
    failure = RuntimeError(
        f"files.info failed; sent Authorization: Bearer {config.slack.bot_token} " + "detail " * 60
    )
    transport = transport_for(config)
    transport._client = FakeSlack(files_info=failure)  # type: ignore[assignment]
    files = [{"id": "F1", "name": "notes.txt", "file_access": "check_file_info"}]

    assert await transport.download_files(files, "default") == []

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "files.info failed for F1" in logged
    assert config.slack.bot_token not in logged and "[redacted]" in logged
    assert len(logged) < 300 and not any(record.exc_info for record in caplog.records)


# -- What the agent is told --


async def test_a_message_whose_only_file_is_rejected_still_says_so_in_the_prompt(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.initialize(config.paths)
    runtime = Runtime(config)
    transport = transport_for(config)
    transport.runtime = runtime
    transport.bot_user_id = "UBOT"
    transport._users["U1"] = "gavin"
    transport._channels["C1"] = "#general"
    transport._client = FakeSlack()  # type: ignore[assignment]
    turns: list[Turn] = []
    ran = asyncio.Event()

    async def run_turn(conversation: str, turn: Turn, reply: Reply) -> None:
        turns.append(turn)
        ran.set()

    monkeypatch.setattr(runtime, "_run_turn", run_turn)

    await transport._handle_event(
        {
            "channel": "C1",
            "channel_type": "channel",
            "ts": "500.001",
            "user": "U1",
            "text": "<@UBOT> have a look",
            "files": [{"id": "F1", "name": "notes.txt", "url_private": "http://evil.example/x"}],
        },
        mentioned=True,
    )
    await asyncio.wait_for(ran.wait(), 5)

    (turn,) = turns
    assert turn.files == ()
    assert "A file was attached but could not be downloaded." in turn.text
    assert list(turn_dir(config).iterdir()) == []
