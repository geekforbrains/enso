"""Fresh private challenges, platform conflicts, and transport cleanup during pairing."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import UTC, datetime
from types import SimpleNamespace

import aiohttp
import pytest
from slack_sdk.errors import SlackApiError
from telegram.error import Conflict, Forbidden

from enso.transports import slack_setup, telegram_setup
from enso.transports.connection import PairedIdentity, PairingError, PairingRequest

REQUEST = PairingRequest("bot-secret-token", "app-secret-token", "fresh_random_code", 1000.0)


def telegram_message(**changes):
    values = {
        "from_user": SimpleNamespace(id=123, is_bot=False),
        "chat": SimpleNamespace(id=123, type="private"),
        "forward_origin": None,
        "date": datetime.fromtimestamp(1001, UTC),
        "text": f"/start {REQUEST.nonce}",
    }
    return SimpleNamespace(**{**values, **changes})


def slack_event(**changes):
    return {
        "type": "message",
        "channel_type": "im",
        "user": "U1",
        "channel": "D1",
        "ts": "1001.0",
        "text": f"ENSO-{REQUEST.nonce}",
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"text": "/start an_old_code"},
        {"text": "Hello 👋"},
        {"date": datetime.fromtimestamp(999, UTC)},
        {"from_user": SimpleNamespace(id=123, is_bot=True)},
        {"chat": SimpleNamespace(id=-123, type="group")},
        {"chat": SimpleNamespace(id=456, type="private")},
        {"forward_origin": object()},
        {"text": None},
    ],
)
def test_telegram_ignores_unrelated_messages(changes):
    assert not telegram_setup.matching_message(telegram_message(**changes), REQUEST)


@pytest.mark.parametrize(
    "changes",
    [
        {"text": "ENSO-old_code"},
        {"text": "Hello 👋"},
        {"ts": "999.0"},
        {"channel_type": "channel"},
        {"bot_id": "B1"},
        {"subtype": "message_changed"},
        {"thread_ts": "1001.0"},
        {"user": "malformed"},
        {"channel": "C1"},
        {"ts": None},
    ],
)
def test_slack_ignores_unrelated_events(changes):
    assert not slack_setup.matching_event(slack_event(**changes), REQUEST)


class FakeTelegramBot:
    def __init__(self, updates=(), *, webhook="", poll_error=None, send_error=None):
        self.updates = deque(updates)
        self.webhook = webhook
        self.poll_error = poll_error
        self.send_error = send_error
        self.polls = []
        self.sent = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def get_me(self, **kwargs):
        return SimpleNamespace(username="enso_test_bot")

    async def get_webhook_info(self, **kwargs):
        return SimpleNamespace(url=self.webhook)

    async def get_updates(self, **kwargs):
        self.polls.append(kwargs)
        if self.poll_error:
            raise self.poll_error
        if kwargs.get("timeout") == 0:
            return []
        assert self.updates, "unexpected extra polling loop"
        return self.updates.popleft()

    async def send_message(self, **kwargs):
        if self.send_error:
            raise self.send_error
        self.sent.append(kwargs)


def callbacks():
    ready_calls, claims = [], []

    async def ready(info):
        ready_calls.append(info)

    async def claim():
        claims.append(True)

    return ready, claim, ready_calls, claims


async def test_telegram_pairs_only_fresh_nonce_and_consumes_update_before_return(monkeypatch):
    bot = FakeTelegramBot(
        [
            [SimpleNamespace(update_id=3, message=telegram_message(text="/start old"))],
            [SimpleNamespace(update_id=4, message=telegram_message())],
        ]
    )
    monkeypatch.setattr("telegram.Bot", lambda token: bot)
    ready, claim, seen, claims = callbacks()
    identity = await telegram_setup.pair(REQUEST, ready, claim)
    assert identity == PairedIdentity("123", "123") and bot.closed
    assert claims == [True] and len(bot.sent) == 1
    assert seen[0]["open_url"].endswith(f"?start={REQUEST.nonce}")
    assert [call["offset"] for call in bot.polls] == [None, 4, 5]
    assert bot.polls[-1]["timeout"] == 0


@pytest.mark.parametrize("kind", ["webhook", "polling", "delivery"])
async def test_telegram_conflicts_and_delivery_failure_never_pair(monkeypatch, kind):
    bot = FakeTelegramBot(
        [[SimpleNamespace(update_id=4, message=telegram_message())]],
        webhook="https://already.example.test/hook" if kind == "webhook" else "",
        poll_error=Conflict("another getUpdates process with private token")
        if kind == "polling"
        else None,
        send_error=Forbidden("private token") if kind == "delivery" else None,
    )
    monkeypatch.setattr("telegram.Bot", lambda token: bot)
    ready, claim, seen, claims = callbacks()
    with pytest.raises(PairingError) as failure:
        await telegram_setup.pair(REQUEST, ready, claim)
    assert bot.closed and "private token" not in str(failure.value)
    if kind == "webhook":
        assert failure.value.code == "webhook_conflict" and not seen and not bot.polls
    if kind == "polling":
        assert failure.value.code == "polling_conflict" and len(bot.polls) == 1 and not claims
    if kind == "delivery":
        assert claims == [True] and not bot.sent


async def test_telegram_cancellation_closes_bot(monkeypatch):
    polling = asyncio.Event()
    bot = FakeTelegramBot()

    async def blocked_poll(**kwargs):
        polling.set()
        await asyncio.Event().wait()

    bot.get_updates = blocked_poll
    monkeypatch.setattr("telegram.Bot", lambda token: bot)
    ready, claim, _, claims = callbacks()
    task = asyncio.create_task(telegram_setup.pair(REQUEST, ready, claim))
    await asyncio.wait_for(polling.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bot.closed and not claims


class FakeSlackSocket:
    def __init__(self, events=()):
        self.hello = {"type": "hello", "connection_info": {"app_id": "A1"}}
        self.events = deque(events)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def receive_json(self):
        return self.hello

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(self.events.popleft()))

    async def send_json(self, data):
        """Envelope acknowledgement; the receiver's own bookkeeping, nothing asserts it."""


def slack_envelope(event, team="T1", envelope="envelope-1"):
    return {
        "type": "events_api",
        "envelope_id": envelope,
        "payload": {"team_id": team, "event": event},
    }


@pytest.fixture
def fake_slack(monkeypatch):
    state = SimpleNamespace(
        socket=FakeSlackSocket([slack_envelope(slack_event())]),
        posted=[],
        send_error=None,
        api_error=None,
        closed=False,
    )

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            state.closed = True

        def ws_connect(self, url, **kwargs):
            assert url == "wss://wss-primary.slack.com/link"
            return state.socket

    class Client:
        def __init__(self, token, **kwargs):
            self.token = token

        async def auth_test(self):
            return {"bot_id": "B1", "team_id": "T1", "user": "enso", "team": "Test workspace"}

        async def bots_info(self, **kwargs):
            return {"bot": {"app_id": "A1"}}

        async def api_call(self, method, **kwargs):
            assert self.token == REQUEST.app_token and method == "apps.connections.open"
            if state.api_error:
                raise state.api_error
            return {"url": "wss://wss-primary.slack.com/link"}

        async def users_info(self, user):
            return {"user": {"is_bot": user == "UBOT"}}

        async def chat_postMessage(self, **kwargs):  # noqa: N802 - Slack SDK method name
            if state.send_error:
                raise state.send_error
            state.posted.append(kwargs)
            return {"ts": "1002.0"}

    monkeypatch.setattr("aiohttp.ClientSession", Session)
    monkeypatch.setattr("slack_sdk.web.async_client.AsyncWebClient", Client)
    return state


async def test_slack_pairs_exact_private_event_in_verified_workspace_and_closes(fake_slack):
    fake_slack.socket.events = deque(
        [
            slack_envelope(slack_event(), team="TOTHER", envelope="other-team"),
            slack_envelope(slack_event(user="UBOT"), envelope="bot"),
            slack_envelope(slack_event(text="Hello 👋"), envelope="greeting"),
            slack_envelope(slack_event(), envelope="owner"),
        ]
    )
    ready, claim, seen, claims = callbacks()
    identity = await slack_setup.pair(REQUEST, ready, claim)
    assert identity == PairedIdentity("U1", "D1")
    assert fake_slack.socket.closed and fake_slack.closed and claims == [True]
    assert seen[0]["instruction"] == f"ENSO-{REQUEST.nonce}" and len(fake_slack.posted) == 1


@pytest.mark.parametrize("kind", ["wrong_app", "handshake", "missing_scope", "delivery"])
async def test_slack_rejects_wrong_app_bad_handshake_scopes_and_failed_delivery(fake_slack, kind):
    if kind == "wrong_app":
        fake_slack.socket.hello["connection_info"]["app_id"] = "AOTHER"
    if kind == "handshake":
        fake_slack.socket.hello["type"] = "disconnect"
    if kind == "missing_scope":
        fake_slack.api_error = SlackApiError("private app token", {"error": "missing_scope"})
    if kind == "delivery":
        fake_slack.send_error = SlackApiError("private bot token", {"error": "channel_not_found"})
    ready, claim, seen, claims = callbacks()
    with pytest.raises(PairingError) as failure:
        await slack_setup.pair(REQUEST, ready, claim)
    assert fake_slack.closed and "private" not in str(failure.value)
    assert not fake_slack.posted
    if kind == "wrong_app":
        assert failure.value.code == "mismatched_app"
    if kind != "delivery":
        assert not seen and not claims
    else:
        assert claims == [True]


async def test_slack_cancellation_closes_socket_and_session(fake_slack):
    waiting = asyncio.Event()
    _, claim, _, claims = callbacks()

    async def ready(info):
        waiting.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(slack_setup.pair(REQUEST, ready, claim))
    await asyncio.wait_for(waiting.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake_slack.socket.closed and fake_slack.closed and not claims


@pytest.mark.parametrize(
    "url",
    [
        "wss://attacker.test/link",
        "https://wss-primary.slack.com/link",
        "wss://slack.com.attacker.test/link",
        "wss://user:password@wss-primary.slack.com/link",
        "wss://wss-primary.slack.com:444/link",
    ],
)
def test_slack_socket_credentials_cannot_be_sent_to_another_origin(url):
    with pytest.raises(PairingError):
        slack_setup.socket_url(url)
