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


@pytest.mark.parametrize(("revision", "fail"), [(1, False), (2, False), (2, True)])
def test_smoke_candidates_use_real_cumulative_migrations(enso_home, monkeypatch, revision, fail):
    import json
    import sys
    import types

    from enso.config import Paths
    from enso.maintenance import UpdateError

    fixture = runpy.run_path(str(ROOT / "scripts/upgrade-smoke/run.py"))

    def module(name, source):
        candidate = types.ModuleType(name)
        candidate.__package__ = "enso"
        monkeypatch.setitem(sys.modules, name, candidate)
        exec(compile(source, "<smoke candidate>", "exec"), candidate.__dict__)
        return candidate

    database_source = (ROOT / "src/enso/db.py").read_text()
    base = module("enso._smoke_base", fixture["inject_schema"](database_source, 0))
    base.initialize(enso_home)
    with sqlite3.connect(enso_home.db) as con:
        con.executemany("INSERT INTO smoke_feature VALUES (?, ?)", [("first", 1), ("second", 0)])
    old = enso_home.home / "smoke-legacy/workflows"
    old.mkdir(parents=True)
    (old / "example.json").write_text('{"name":"custom","format":0}\n')
    migrations = module(
        "enso._smoke_migrations",
        fixture["inject_migrations"](
            (ROOT / "src/enso/migrations.py").read_text(), db.SCHEMA_VERSION, revision, fail=fail
        ),
    )
    assert [step.revision for step in migrations.pending(enso_home)] == list(range(1, revision + 1))
    declared = migrations.plan(enso_home)
    assert "enso.db" in declared and "smoke-legacy" in declared
    assert fixture["WORKFLOW_PATHS"][revision] in declared
    if fail:
        with pytest.raises(UpdateError, match="after database and file writes"):
            migrations.apply(enso_home)
        # This fault occurs after both kinds of durable mutation and the first marker:
        # the container acceptance checks must prove the updater restores all three.
        assert migrations.read_revision(enso_home) == 1
    else:
        migrations.apply(enso_home)
        migrations.apply(enso_home)
        assert migrations.read_revision(enso_home) == revision
    destination = enso_home.home / fixture["WORKFLOW_PATHS"][revision] / "example.json"
    assert json.loads(destination.read_text()) == {"name": "custom", "format": revision}
    assert not old.exists()
    candidate = module("enso._smoke_latest", fixture["inject_schema"](database_source, revision))
    candidate.initialize(enso_home)
    fresh = Paths(enso_home.home.parent / "fresh")
    fresh.home.mkdir()
    candidate.initialize(fresh)
    with sqlite3.connect(enso_home.db) as upgraded, sqlite3.connect(fresh.db) as new:
        assert upgraded.execute("PRAGMA user_version").fetchone() == (db.SCHEMA_VERSION + revision,)
        assert (
            upgraded.execute("PRAGMA table_info(smoke_feature)").fetchall()
            == new.execute("PRAGMA table_info(smoke_feature)").fetchall()
        )
        defaults = (0, 0) if revision == 2 else (0,)
        assert upgraded.execute("SELECT * FROM smoke_feature ORDER BY name").fetchall() == [
            ("first", "done", *defaults),
            ("second", "pending", *defaults),
        ]


def test_smoke_schema_injection_refuses_missing_anchor():
    inject = runpy.run_path(str(ROOT / "scripts/upgrade-smoke/run.py"))["inject_schema"]
    with pytest.raises(AssertionError, match="requires SCHEMA_VERSION and _SCHEMA"):
        inject("SCHEMA_VERSION = 1\n", 1)
