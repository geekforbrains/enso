"""Installed-wheel smoke fixtures must support the current runtime contracts."""

import runpy
import sqlite3
from pathlib import Path

import pytest

from enso import db
from enso.cli.common import deliver

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("upload", [False, True])
async def test_smoke_transport_supports_cli_delivery(config, tmp_path, upload):
    transport_class = runpy.run_path(str(ROOT / "scripts/upgrade-smoke/fake_transport.py"))[
        "SlackTransport"
    ]
    transport = transport_class(config.slack, config.paths)
    db.migrate(config.paths)
    attachment = tmp_path / "report.txt"
    attachment.write_text("Report")

    result = await deliver(
        config.paths,
        transport,
        "C1",
        "100.1",
        text="Report",
        file=attachment if upload else None,
    )

    expected = {
        "ok": True,
        "transport": "slack",
        "channel": "C1",
        "ts": None if upload else "1.0",
        "thread_ts": "100.1",
        "permalink": None,
    }
    if upload:
        expected["file"] = "file"
    assert result == expected


@pytest.mark.parametrize("schema_symbol", ["_SCHEMA_V6", "NATIVE_SCHEMA"])
@pytest.mark.parametrize("fail", [False, True])
def test_smoke_migration_injection_uses_registered_schema_symbol(schema_symbol, fail):
    inject = runpy.run_path(str(ROOT / "scripts/upgrade-smoke/run.py"))["inject_migration"]
    source = f"""SCHEMA_VERSION = 6
{schema_symbol} = "PRAGMA user_version = 6;"
def migrate(con):
    for target, schema in (
            (6, {schema_symbol}),
    ):
        con.executescript(schema)
"""
    namespace = {}
    exec(compile(inject(source, fail=fail), "<smoke migration fixture>", "exec"), namespace)
    with sqlite3.connect(":memory:") as con:
        if fail:
            with pytest.raises(sqlite3.OperationalError):
                namespace["migrate"](con)
        else:
            namespace["migrate"](con)
            assert con.execute("PRAGMA user_version").fetchone()[0] == 7
            assert con.execute(
                "SELECT name FROM sqlite_master WHERE name='smoke_migrated_v7'"
            ).fetchone()


def test_smoke_migration_injection_refuses_missing_anchor():
    inject = runpy.run_path(str(ROOT / "scripts/upgrade-smoke/run.py"))["inject_migration"]
    with pytest.raises(AssertionError, match="exactly one schema 6 migration tuple"):
        inject("SCHEMA_VERSION = 6\n")
