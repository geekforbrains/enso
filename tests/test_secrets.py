"""Secret storage, key lifetime, concurrency, and database-only backup protection."""

import concurrent.futures
import json
import os
import sqlite3
import threading
from contextlib import closing

import pytest
from cryptography.fernet import Fernet

from enso import db, migrations, secrets
from enso.config import Paths, parse_config, secret_key_file


def test_roundtrip_is_exact_and_only_ciphertext_reaches_database(enso_home):
    assert secrets.names(enso_home) == []
    assert not enso_home.db.exists()
    assert not secret_key_file(enso_home).exists()
    value = "  synthetic-private-value-8675309\r\nsecond line\n雪\n\n"
    secrets.add(enso_home, "EXAMPLE_TOKEN", value)
    # Keep a connection open to retain and inspect the WAL as well as the main file.
    with db.reader(enso_home):
        secrets.add(enso_home, "EMPTY", "")
        assert secrets.names(enso_home) == ["EMPTY", "EXAMPLE_TOKEN"]
        assert secrets.resolve(enso_home, ["EXAMPLE_TOKEN", "EMPTY"]) == {
            "EXAMPLE_TOKEN": value,
            "EMPTY": "",
        }
        for path in enso_home.home.glob("enso.db*"):
            assert b"synthetic-private-value-8675309" not in path.read_bytes()
    key = secret_key_file(enso_home)
    assert key.stat().st_mode & 0o777 == 0o600
    assert key.parent.stat().st_mode & 0o777 == 0o700
    assert key.stat().st_uid == os.getuid()
    with pytest.raises(secrets.SecretError, match="already exists"):
        secrets.add(enso_home, "EXAMPLE_TOKEN", "replacement")
    secrets.delete(enso_home, "EXAMPLE_TOKEN")
    secrets.add(enso_home, "EXAMPLE_TOKEN", "replacement")
    assert secrets.resolve(enso_home, ["EXAMPLE_TOKEN"])["EXAMPLE_TOKEN"] == "replacement"


@pytest.mark.parametrize("damage", ["missing", "wrong", "malformed"])
def test_initialized_empty_store_never_replaces_its_key(enso_home, damage):
    secrets.add(enso_home, "TOKEN", "original")
    secrets.delete(enso_home, "TOKEN")
    key = secret_key_file(enso_home)
    original = key.read_bytes()
    key.unlink()
    if damage != "missing":
        key.write_bytes(Fernet.generate_key() if damage == "wrong" else b"invalid")
        key.chmod(0o600)
    for operation in (
        lambda: secrets.add(enso_home, "TOKEN", "new"),
        lambda: secrets.names(enso_home),
        lambda: secrets.resolve(enso_home, ["TOKEN"]),
        lambda: secrets.delete(enso_home, "TOKEN"),
    ):
        with pytest.raises(secrets.SecretError, match=r"key|decrypt"):
            operation()
    assert key.read_bytes() != original if key.exists() else damage == "missing"
    key.write_bytes(original)
    key.chmod(0o600)
    secrets.add(enso_home, "TOKEN", "restored")


def test_existing_key_is_used_and_never_overwritten(enso_home):
    key = secret_key_file(enso_home)
    key.parent.mkdir(parents=True, mode=0o700)
    key.write_bytes(Fernet.generate_key())
    key.chmod(0o600)
    original = key.read_bytes()
    secrets.add(enso_home, "TOKEN", "value")
    assert key.read_bytes() == original


@pytest.mark.parametrize("damage", ["ciphertext", "swap", "version"])
def test_tampered_values_fail_without_disclosing_plaintext(enso_home, damage):
    secrets.add(enso_home, "FIRST", "private-first")
    secrets.add(enso_home, "SECOND", "private-second")
    with db.transaction(enso_home) as con:
        original = con.execute(
            "SELECT ciphertext FROM _enso_secrets WHERE name='FIRST'"
        ).fetchone()[0]
        replacement = b"\x02" + original[1:] if damage == "version" else original[:-1] + b"!"
        if damage == "swap":
            replacement = con.execute(
                "SELECT ciphertext FROM _enso_secrets WHERE name='SECOND'"
            ).fetchone()[0]
        con.execute("UPDATE _enso_secrets SET ciphertext=? WHERE name='FIRST'", (replacement,))
    with pytest.raises(secrets.SecretError, match="decrypt") as error:
        secrets.resolve(enso_home, ["FIRST"])
    assert "private-" not in str(error.value)


def test_parallel_first_creations_and_duplicates(enso_home):
    barrier = threading.Barrier(8)

    def create(index):
        barrier.wait()
        try:
            secrets.add(enso_home, f"TOKEN_{index % 4}", f"value-{index % 4}")
            return True
        except secrets.SecretError as exc:
            assert "already exists" in str(exc)
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(create, range(8))) == 4
    assert secrets.resolve(enso_home, secrets.names(enso_home)) == {
        f"TOKEN_{i}": f"value-{i}" for i in range(4)
    }


def test_different_homes_can_initialize_one_shared_key(enso_home, tmp_path):
    other = Paths(tmp_path / "other")
    barrier = threading.Barrier(2)

    def create(paths):
        barrier.wait()
        secrets.add(paths, "TOKEN", "value")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(create, [enso_home, other]))
    assert secrets.resolve(other, ["TOKEN"]) == secrets.resolve(enso_home, ["TOKEN"])


@pytest.mark.parametrize("kind", ["permissions", "directory_permissions", "symlink", "fifo"])
def test_unsafe_key_files_are_refused(enso_home, tmp_path, kind):
    key = secret_key_file(enso_home)
    key.parent.mkdir(parents=True, mode=0o700)
    if kind == "fifo":
        os.mkfifo(key, 0o600)
    elif kind == "symlink":
        target = tmp_path / "key"
        target.write_bytes(Fernet.generate_key())
        target.chmod(0o600)
        key.symlink_to(target)
    else:
        key.write_bytes(Fernet.generate_key())
        key.chmod(0o644 if kind == "permissions" else 0o600)
        if kind == "directory_permissions":
            key.parent.chmod(0o755)
    with pytest.raises(secrets.SecretError):
        secrets.add(enso_home, "TOKEN", "value")


@pytest.mark.parametrize(
    "name",
    [
        "lowercase",
        "_TOKEN",
        "A=B",
        "A\nB",
        "ENSO_HOME",
        "ENSO_RUN_ID",
        "PATH",
        "BASH_ENV",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "PYTHONPATH",
        "NODE_OPTIONS",
    ],
)
def test_invalid_and_reserved_names_are_rejected_everywhere(enso_home, name):
    for operation in (
        lambda: secrets.add(enso_home, name, "value"),
        lambda: secrets.resolve(enso_home, [name]),
        lambda: secrets.delete(enso_home, name),
    ):
        with pytest.raises(secrets.SecretError):
            operation()
    assert not enso_home.db.exists()


@pytest.mark.parametrize("value", ["bad\x00value", "x" * (secrets.MAX_VALUE_BYTES + 1)])
def test_invalid_values_are_not_stored_or_reported(enso_home, value):
    with pytest.raises(secrets.SecretError) as error:
        secrets.add(enso_home, "TOKEN", value)
    assert value not in str(error.value)
    assert not enso_home.db.exists()


def test_restore_database_and_external_key_in_new_home(enso_home, tmp_path):
    secrets.add(enso_home, "TOKEN", "restore-example")
    key = secret_key_file(enso_home)
    original_key = key.read_bytes()
    restored = Paths(tmp_path / "restored")
    restored.home.mkdir()
    with (
        closing(sqlite3.connect(enso_home.db)) as source,
        closing(sqlite3.connect(restored.db)) as target,
    ):
        source.backup(target)
    key.unlink()
    with pytest.raises(secrets.SecretError, match="missing"):
        secrets.resolve(restored, ["TOKEN"])
    replacement_key = tmp_path / "new-account" / "master.key"
    replacement_key.parent.mkdir(mode=0o700)
    replacement_key.write_bytes(original_key)
    replacement_key.chmod(0o600)
    restored.config.write_text('{"secrets":{"key_file":' + json.dumps(str(replacement_key)) + "}}")
    assert secrets.resolve(restored, ["TOKEN"]) == {"TOKEN": "restore-example"}


def test_key_configuration_is_absolute_and_outside_home(enso_home, raw_config, tmp_path):
    for value in ("relative.key", "~/key", str(enso_home.home / "key"), 42):
        raw_config["secrets"] = {"key_file": value}
        config, problems, _ = parse_config(raw_config, enso_home)
        assert config is None and any("secrets.key_file" in p for p in problems)
    raw_config["secrets"] = {"key_file": str(tmp_path / "keys/master.key")}
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is not None and not problems


def test_migration_matches_fresh_schema_and_preserves_existing_content(enso_home, tmp_path):
    db.initialize(enso_home)
    with db.transaction(enso_home) as con:
        con.execute("DROP TABLE _enso_secrets")
        con.execute("DROP TABLE _enso_secret_store")
        con.execute("PRAGMA user_version = 2")
        con.execute("CREATE TABLE personal_notes (body TEXT)")
        con.execute("INSERT INTO personal_notes VALUES ('keep')")
    old_files = enso_home.home / "secrets"
    old_files.mkdir()
    (old_files / "old.env").write_text("IGNORED=value\n")
    migrations.add_secret_store(enso_home)
    migrations.add_secret_store(enso_home)
    fresh = Paths(tmp_path / "fresh")
    db.initialize(fresh)
    # Test this historical step independently of later database migrations.
    with closing(sqlite3.connect(enso_home.db)) as upgraded, db.reader(fresh) as new:
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 3
        assert upgraded.execute("SELECT count(*) FROM _enso_secrets").fetchone()[0] == 0
        query = (
            "SELECT sql FROM sqlite_master WHERE name IN "
            "('_enso_secrets','_enso_secret_store') ORDER BY name"
        )
        assert ["".join(r[0].split()) for r in upgraded.execute(query)] == [
            "".join(r[0].split()) for r in new.execute(query)
        ]
        assert upgraded.execute("SELECT body FROM personal_notes").fetchone()[0] == "keep"
    assert (old_files / "old.env").read_text() == "IGNORED=value\n"
    assert not secret_key_file(enso_home).exists()
