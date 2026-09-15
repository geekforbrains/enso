"""What every caller may know about a transport without importing its extra.

One ``TransportSpec`` per transport declares the facts the CLI, config parsing, doctor,
routing, and onboarding need: the module its extra installs, what its targets and binding
keys look like, the credentials it pairs with and the wizard copy for them, and its chat
command prefix. Callers look a transport up in ``TRANSPORTS`` instead of comparing names.

The transport class and its pairing receiver load lazily, so importing this module never
imports ``slack_bolt`` or ``python-telegram-bot``. It lives beside ``config`` rather than
under ``transports/`` because ``config`` needs it and the viewer, which imports ``config``,
must never load the transports package.
"""

from __future__ import annotations

import importlib.util
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from .config import Config
    from .transports import Transport
    from .transports.connection import Claim, PairedIdentity, PairingRequest, Ready

    Pair = Callable[[PairingRequest, Ready, Claim], Awaitable[PairedIdentity]]


@dataclass(frozen=True)
class Credential:
    """One secret the setup wizard asks for; ``hint`` is printed first and may name ``{home}``."""

    key: str
    prompt: str
    hint: str


class TransportSpec(ABC):
    """The extra-free facts about one transport."""

    name: str
    module: str  # what ``find_spec`` probes: the top-level import the extra installs
    target_form: str  # what ``canonical_target`` accepts, for error messages
    binding_pattern: str  # the ``bindings`` keys this transport owns, as a regex
    binding_forms: tuple[str, ...]  # the same keys as examples, for error messages
    credentials: tuple[Credential, ...]
    prefix: str  # what starts a chat command: ``!stop``, ``/stop``
    pairing_hint: str  # the wizard's line after ``Open <url>``; may name ``{instruction}``

    def installed(self) -> bool:
        return importlib.util.find_spec(self.module) is not None

    @abstractmethod
    def canonical_target(self, value: object) -> str | None:
        """``value`` as an id this transport can post to, or None when it is not one.

        One definition of "a place a transport can post to", shared by config parsing,
        job validation, and the CLI's ``--to``, so a bad destination fails before a send.
        """

    @abstractmethod
    def binding_key(self, channel: str, *, is_dm: bool = False, user_id: str = "") -> str:
        """The ``bindings`` key for a message's location."""

    @abstractmethod
    def config_entry(self, credentials: Mapping[str, str], owner: PairedIdentity) -> dict[str, Any]:
        """The ``transports.<name>`` object for a freshly paired owner."""

    @abstractmethod
    def build(self, config: Config) -> Transport:
        """The configured transport; raises ImportError when the extra is missing."""

    @abstractmethod
    def pair(self) -> Pair:
        """The pre-service pairing receiver; imports its module on each call."""


class SlackSpec(TransportSpec):
    name = "slack"
    module = "slack_bolt"
    target_form = "a Slack conversation id (C…, G…, or D…)"
    # DM bindings use the message's user id: ``U…`` on standalone workspaces, ``W…`` for
    # Enterprise Grid org-wide ids. DM targets are deliberately not accepted; posting to a
    # DM needs its D… conversation id, not the user id.
    binding_pattern = r"slack:(?:dm:[UW]|[CG])[A-Z0-9]+"
    binding_forms = ("slack:C…", "slack:G…", "slack:dm:U…", "slack:dm:W…")
    credentials = (
        Credential(
            "bot_token",
            "Bot token (xoxb-…)",
            "Create your Slack app at https://api.slack.com/apps?new_app=1\n"
            "Choose From an app manifest and paste the contents of:\n"
            "  {home}/slack/manifest.json\n"
            "Install it to your workspace, then copy its Bot User OAuth Token.",
        ),
        Credential(
            "app_token",
            "App token (xapp-…)",
            "In Basic Information → App-Level Tokens, create a token with connections:write.",
        ),
    )
    prefix = "!"
    pairing_hint = "Send this code in a private message to the bot: {instruction}"
    _target = re.compile(r"[CGD][A-Z0-9]+")

    def canonical_target(self, value: object) -> str | None:
        return value if isinstance(value, str) and self._target.fullmatch(value) else None

    def binding_key(self, channel: str, *, is_dm: bool = False, user_id: str = "") -> str:
        return f"slack:dm:{user_id}" if is_dm else f"slack:{channel}"

    def config_entry(self, credentials: Mapping[str, str], owner: PairedIdentity) -> dict[str, Any]:
        return {
            "bot_token": credentials["bot_token"],
            "app_token": credentials["app_token"],
            "notify": owner.channel,
        }

    def build(self, config: Config) -> Transport:
        from .transports.slack import SlackTransport

        assert config.slack is not None  # callers build only configured transports
        return SlackTransport(config.slack, config.paths)

    def pair(self) -> Pair:
        from .transports import slack_setup

        return slack_setup.pair


class TelegramSpec(TransportSpec):
    name = "telegram"
    module = "telegram"
    target_form = "a positive numeric Telegram user id"
    binding_pattern = r"telegram:[0-9]+"  # private chats only: a group id is negative
    binding_forms = ("telegram:<user id>",)
    credentials = (
        Credential(
            "bot_token",
            "Bot token",
            "Open https://t.me/BotFather, send /newbot, and follow its prompts.",
        ),
    )
    prefix = "/"
    pairing_hint = "Press Start in the bot's private chat."

    def canonical_target(self, value: object) -> str | None:
        """A positive Telegram user id: an int, or its exact decimal string."""
        if isinstance(value, bool):
            return None
        # ``isdecimal`` and not ``isdigit``: ``"²".isdigit()`` is true but ``int`` rejects
        # it, which would escape parsing as a ValueError instead of landing in ``problems``.
        if isinstance(value, str) and value.isdecimal() and str(int(value)) == value:
            value = int(value)
        if isinstance(value, int) and value > 0:
            return str(value)
        return None

    def binding_key(self, channel: str, *, is_dm: bool = False, user_id: str = "") -> str:
        return f"telegram:{channel}"

    def config_entry(self, credentials: Mapping[str, str], owner: PairedIdentity) -> dict[str, Any]:
        return {
            "bot_token": credentials["bot_token"],
            "allowed_users": [owner.user_id],
            "notify": owner.channel,
        }

    def build(self, config: Config) -> Transport:
        from .transports.telegram import TelegramTransport

        assert config.telegram is not None  # callers build only configured transports
        return TelegramTransport(config.telegram, config.paths)

    def pair(self) -> Pair:
        from .transports import telegram_setup

        return telegram_setup.pair


# Slack first: it is the wizard's default and the order configured transports are listed in.
TRANSPORTS: dict[str, TransportSpec] = {spec.name: spec for spec in (SlackSpec(), TelegramSpec())}
