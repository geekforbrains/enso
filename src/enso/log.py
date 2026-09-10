"""Logging: one rotating file, a stderr mirror on a TTY, and a per-turn context tag."""

from __future__ import annotations

import logging
import os
import re
import shlex
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LoggingConfig, Paths

FORMAT = "%(asctime)s %(levelname)-5s %(short_name)-9s %(ctx)s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "slack_bolt",
    "slack_sdk",
    "telegram",
    "urllib3",
    "h11",
    "h2",
    "hpack",
    "aiohttp",
)
_TOKEN_RE = re.compile(r"^(?:xox[a-z]-|xapp-|\d{6,}:[A-Za-z0-9_-]{30,})")
# Flags that carry the prompt attached to them, so a leading "-" is not read as a flag.
_PROMPT_FLAGS = ("--single=", "--prompt=")

_ctx: ContextVar[str] = ContextVar("enso_log_ctx", default="")


class _ContextFilter(logging.Filter):
    """Attach the short logger name and the current turn/job tag to each record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.short_name = record.name.rsplit(".", 1)[-1][:9]
        record.ctx = _ctx.get()
        return True


def setup(paths: Paths, settings: LoggingConfig, *, debug: bool = False) -> None:
    """Configure the root logger once for a ``serve`` or CLI process."""
    paths.home.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(FORMAT, DATE_FORMAT)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(paths.log, maxBytes=settings.max_bytes, backupCount=settings.backups)
    ]
    if sys.stderr.isatty():
        handlers.append(logging.StreamHandler(sys.stderr))
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(_ContextFilter())
        root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug else settings.level)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def new_turn_id() -> str:
    return uuid.uuid4().hex[:6]


@contextmanager
def context(tag: str) -> Iterator[None]:
    """Tag every log line inside the block, e.g. ``t:8f3a2c`` or ``j:nightly``."""
    token = _ctx.set(f"[{tag}]")
    try:
        yield
    finally:
        _ctx.reset(token)


def _rotated(path: Path) -> Iterator[Path]:
    """The live log, then its backups newest first, stopping at the first gap."""
    yield path
    for index in range(1, 1000):
        backup = path.with_name(f"{path.name}.{index}")
        if not backup.exists():
            return
        yield backup


def tail(paths: Paths, count: int, keep: Callable[[str], bool] = lambda _line: True) -> list[str]:
    """The last ``count`` lines that pass ``keep``, reaching into rotated backups as needed."""
    found: list[str] = []
    for path in _rotated(paths.log):
        if not path.exists():
            break
        need = count - len(found)
        if need <= 0:
            break
        lines = [line for line in path.read_text(errors="replace").splitlines() if keep(line)]
        found = lines[-need:] + found
    return found


def follow(paths: Paths, interval: float = 0.5) -> Iterator[str]:
    """Lines appended from now on, reopening the file when it is rotated or recreated."""
    path = paths.log
    handle = open(path, encoding="utf-8", errors="replace")  # noqa: SIM115 - reopened below
    handle.seek(0, os.SEEK_END)
    partial = ""
    try:
        while True:
            chunk = handle.readline()
            if chunk:
                partial += chunk
                if partial.endswith("\n"):
                    yield partial.rstrip("\n")
                    partial = ""
                continue
            try:
                current = os.stat(path)
            except FileNotFoundError:
                current = None
            if current is not None and (
                current.st_ino != os.fstat(handle.fileno()).st_ino
                or current.st_size < handle.tell()
            ):
                handle.close()
                handle = open(path, encoding="utf-8", errors="replace")  # noqa: SIM115
                partial = ""
                continue
            time.sleep(interval)
    finally:
        handle.close()


def redacted_command(cmd: list[str]) -> str:
    """Render a spawn command for the log with prompts and tokens hidden."""
    redacted = list(cmd)
    for index, part in enumerate(redacted):
        flag = next((flag for flag in _PROMPT_FLAGS if part.startswith(flag)), None)
        if flag:
            redacted[index] = f"{flag}<prompt chars={len(part) - len(flag)}>"
        elif _TOKEN_RE.match(part):
            redacted[index] = "<redacted>"
    if "--" in redacted:
        sep = redacted.index("--")
        prompt_chars = sum(len(part) for part in cmd[sep + 1 :])
        return shlex.join([*redacted[: sep + 1], f"<prompt chars={prompt_chars}>"])
    return shlex.join(redacted)
