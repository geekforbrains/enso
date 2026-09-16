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
    db.initialize(config.paths)
    attachment = tmp_path / "report.txt"
    attachment.write_text("Report")

    result = await deliver(
        config.paths,
        transport,
        "C1",
        "100.1",
        workspace="default",
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


@pytest.mark.parametrize("fail", [False, True])
def test_smoke_candidate_exercises_real_schema_preparation(enso_home, fail):
    from enso import db

    inject = runpy.run_path(str(ROOT / "scripts/upgrade-smoke/run.py"))["inject_migration"]
    source = (ROOT / "src/enso/db.py").read_text()
    namespace = {"__name__": "enso._smoke_db", "__package__": "enso"}
    # Dataclass decorators require their defining module to be registered.
    import sys
    import types

    module = types.ModuleType("enso._smoke_db")
    module.__dict__.update(namespace)
    sys.modules[module.__name__] = module
    try:
        exec(compile(inject(source, fail=fail), "<smoke candidate>", "exec"), module.__dict__)
        db.initialize(enso_home)
        if fail:
            with pytest.raises(sqlite3.OperationalError):
                module.initialize(enso_home)
        else:
            module.initialize(enso_home)
            module.initialize(enso_home)
        with sqlite3.connect(enso_home.db) as con:
            assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION + (
                not fail
            )
            assert bool(
                con.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = ?",
                    (f"smoke_migrated_v{db.SCHEMA_VERSION + 1}",),
                ).fetchone()
            ) is (not fail)
        if not fail:
            # The interrupted-update fixture builds from an already upgraded source tree.
            twice = inject(inject(source))
            exec(compile(twice, "<next smoke candidate>", "exec"), module.__dict__)
            module.initialize(enso_home)
            with sqlite3.connect(enso_home.db) as con:
                assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION + 2
    finally:
        del sys.modules[module.__name__]


def test_smoke_migration_injection_refuses_missing_anchor():
    inject = runpy.run_path(str(ROOT / "scripts/upgrade-smoke/run.py"))["inject_migration"]
    with pytest.raises(AssertionError, match="requires SCHEMA_VERSION and initialize"):
        inject("SCHEMA_VERSION = 1\n")
