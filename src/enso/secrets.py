"""Named, authenticated secret values; the decryption key lives outside the Enso home.

Only ciphertext reaches SQLite. A permanent encrypted verifier binds even an empty store
to its original key. CLI, web forms and jobs use these same operations; resolved values
live only in the caller's environment, never in a process-global cache.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from . import db
from .config import ConfigError, Paths, secret_key_file
from .maintenance import sync_directory

MAX_VALUE_BYTES = 64 * 1024
NAME_PATTERN = r"[A-Z][A-Z0-9_]*"
_RESERVED = {
    "PATH",
    "HOME",
    "SHELL",
    "ENV",
    "BASH_ENV",
    "IFS",
    "CDPATH",
    "SHELLOPTS",
    "BASHOPTS",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONINSPECT",
    "VIRTUAL_ENV",
    "NODE_OPTIONS",
    "NODE_PATH",
}
_RESERVED_PREFIXES = ("ENSO_", "LD_", "DYLD_")
_VERIFIER = "Enso secret store v1"


class SecretError(Exception):
    """A safe diagnostic that never includes a secret value or key contents."""


def validate_name(name: str) -> None:
    if not re.fullmatch(NAME_PATTERN, name):
        raise SecretError(f"secret names must match {NAME_PATTERN}")
    if name in _RESERVED or name.startswith(_RESERVED_PREFIXES):
        raise SecretError(f"{name} is reserved for process control")


def validate_value(value: str) -> None:
    if "\x00" in value:
        raise SecretError("secret values cannot contain NUL")
    if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
        raise SecretError("secret values must be at most 64 KiB of UTF-8 text")


@contextmanager
def _errors() -> Iterator[None]:
    """Translate storage errors at the shared boundary, before any UI can log them."""
    try:
        yield
    except ConfigError as exc:
        raise SecretError("; ".join(exc.problems)) from None
    except (db.UnsupportedDatabaseError, db.UnreadableDatabaseError) as exc:
        raise SecretError(str(exc)) from None
    except OSError, sqlite3.Error:
        raise SecretError("could not access the secret store or master key") from None


def _private(info: os.stat_result, *, directory: bool) -> bool:
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    mode = 0o700 if directory else 0o600
    return kind(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == mode


def _key(path: Path, *, create: bool) -> Fernet:
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = path.parent.lstat()
    except FileNotFoundError:
        raise SecretError("master key is missing; restore the original key") from None
    if not _private(info, directory=True):
        raise SecretError("master key directory must be owned by this account with mode 0700")
    if create and not path.exists():
        # Publish a complete, synced key without replacing another process's key. This
        # also handles two different Enso homes initializing the shared default key.
        fd, temporary = tempfile.mkstemp(prefix=".master-key-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(Fernet.generate_key())
                file.flush()
                os.fsync(file.fileno())
            with suppress(FileExistsError):
                os.link(temporary, path)
            sync_directory(path.parent)
        finally:
            os.unlink(temporary)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise SecretError("master key is missing; restore the original key") from None
    with os.fdopen(fd, "rb") as file:
        if not _private(os.fstat(file.fileno()), directory=False):
            raise SecretError(
                "master key must be a regular file owned by this account with mode 0600"
            )
        key = file.read(45)
    try:
        if len(key) != 44:
            raise ValueError
        return Fernet(key)
    except ValueError:
        raise SecretError("master key is invalid; restore the original key") from None


def _encrypt(cipher: Fernet, name: str, value: str) -> bytes:
    # The version and encrypted name bind the storage format and row identity as well
    # as the value, so swapping two valid ciphertexts does not swap credentials.
    return b"\x01" + cipher.encrypt((name + "\x00" + value).encode("utf-8"))


def _decrypt(cipher: Fernet, name: str, token: bytes) -> str:
    try:
        if not isinstance(token, bytes) or token[:1] != b"\x01":
            raise ValueError
        stored_name, value = cipher.decrypt(token[1:]).decode("utf-8").split("\x00", 1)
        if stored_name != name:
            raise ValueError
        return value
    except InvalidToken, ValueError:
        raise SecretError(
            "could not decrypt the secret store; check the master key and database"
        ) from None


def _cipher(con: sqlite3.Connection, key_file: Path, *, create: bool = False) -> Fernet | None:
    row = con.execute("SELECT verifier FROM _enso_secret_store WHERE id = 1").fetchone()
    if row is None and not create:
        return None
    cipher = _key(key_file, create=row is None)
    if row is None:
        con.execute(
            "INSERT INTO _enso_secret_store VALUES (1, ?)", (_encrypt(cipher, "", _VERIFIER),)
        )
    elif _decrypt(cipher, "", row[0]) != _VERIFIER:
        raise SecretError("invalid secret store verifier")
    return cipher


def names(paths: Paths) -> list[str]:
    """List names without returning values; an uninitialized store is empty."""
    if not paths.db.exists():
        return []
    with _errors(), db.transaction(paths) as con:
        _cipher(con, secret_key_file(paths))
        return [row[0] for row in con.execute("SELECT name FROM _enso_secrets ORDER BY name")]


def add(paths: Paths, name: str, value: str) -> None:
    """Create once, serializing initialization and duplicate checks across processes."""
    validate_name(name)
    validate_value(value)
    with _errors():
        key_file = secret_key_file(paths)
        db.initialize(paths)
        with db.transaction(paths) as con:
            if con.execute("SELECT 1 FROM _enso_secrets WHERE name = ?", (name,)).fetchone():
                raise SecretError(f"secret already exists: {name}; delete it before replacing it")
            cipher = _cipher(con, key_file, create=True)
            assert cipher is not None
            con.execute(
                "INSERT INTO _enso_secrets VALUES (?, ?)", (name, _encrypt(cipher, name, value))
            )


def delete(paths: Paths, name: str) -> None:
    validate_name(name)
    with _errors():
        if not paths.db.exists():
            raise SecretError(f"secret not found: {name}")
        with db.transaction(paths) as con:
            _cipher(con, secret_key_file(paths))
            if not con.execute("DELETE FROM _enso_secrets WHERE name = ?", (name,)).rowcount:
                raise SecretError(f"secret not found: {name}")


def resolve(paths: Paths, names: Sequence[str], *, key_file: Path | None = None) -> dict[str, str]:
    """Resolve a complete selection from one SQLite snapshot, without changing the parent env."""
    for name in names:
        validate_name(name)
    if not names:
        return {}
    if not paths.db.exists():
        raise SecretError(f"secret not found: {names[0]}")
    # A writable SQLite handle also recreates WAL sidecars after a database-only restore,
    # without requiring the chat service to start first. No plaintext is written here.
    with _errors(), db.transaction(paths) as con:
        cipher = _cipher(con, key_file or secret_key_file(paths))
        values = {}
        for name in dict.fromkeys(names):
            row = con.execute(
                "SELECT ciphertext FROM _enso_secrets WHERE name = ?", (name,)
            ).fetchone()
            if row is None or cipher is None:
                raise SecretError(f"secret not found: {name}")
            values[name] = _decrypt(cipher, name, row[0])
        return values
