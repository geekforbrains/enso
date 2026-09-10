"""Small contracts shared by the two pre-service owner-pairing receivers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass


class PairingError(Exception):
    """A safe, actionable pairing failure; never contains a third-party response."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class PairingRequest:
    bot_token: str
    app_token: str
    nonce: str
    created_at: float


@dataclass(frozen=True)
class PairedIdentity:
    user_id: str
    channel: str


Ready = Callable[[dict[str, str]], Awaitable[None]]
Claim = Callable[[], Awaitable[None]]
