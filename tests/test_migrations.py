"""Release migrations preserve data, advance in order, and leave failures recoverable."""

from __future__ import annotations

import json
import sqlite3

import pytest

from enso import migrations
from enso.maintenance import UpdateError, write_json


@pytest.fixture(autouse=True)
def unmarked(enso_home):
    """These homes predate the marker, as a 0.2.0 home does."""
    (enso_home.home / migrations.MARKER).unlink()


def upgrade_database(paths):
    with sqlite3.connect(paths.db) as connection:
        # Explicit BEGIN includes DDL and user_version in the same transaction.
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ALTER TABLE example ADD COLUMN state TEXT NOT NULL DEFAULT 'open'")
        connection.execute("ALTER TABLE example ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        connection.execute("UPDATE example SET state = 'done' WHERE completed = 1")
        connection.execute("ALTER TABLE example DROP COLUMN completed")
        connection.execute("PRAGMA user_version = 2")


def move_workflows(paths):
    source, target = paths.home / "old-workflows", paths.home / "workflows"
    if target.exists():
        raise ValueError("workflows destination already exists")
    source.rename(target)
    workflow = target / "daily.json"
    document = json.loads(workflow.read_text())
    document["enabled"] = document.pop("active")
    workflow.write_text(json.dumps(document, indent=2) + "\n")


@pytest.fixture
def release(enso_home, monkeypatch):
    with sqlite3.connect(enso_home.db) as connection:
        connection.execute("CREATE TABLE example (name TEXT, completed INTEGER NOT NULL)")
        connection.executemany("INSERT INTO example VALUES (?, ?)", [("one", 1), ("two", 0)])
        connection.execute("PRAGMA user_version = 1")
    old = enso_home.home / "old-workflows"
    old.mkdir()
    (old / "daily.json").write_text('{"active": true, "prompt": "preserve this"}\n')
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        (
            migrations.Migration(
                1, "task state and priority", lambda _: ("enso.db",), upgrade_database
            ),
            migrations.Migration(
                2,
                "move and format workflows",
                lambda _: ("old-workflows", "workflows"),
                move_workflows,
            ),
        ),
    )
    return enso_home


def test_skipped_releases_run_db_and_file_steps_in_order_once(release):
    assert not (release.home / migrations.MARKER).exists()
    assert [step.revision for step in migrations.pending(release)] == [1, 2]
    assert migrations.plan(release) == ("enso.db", "old-workflows", "workflows")
    assert not (release.home / migrations.MARKER).exists()  # planning changes nothing

    migrations.apply(release)

    with sqlite3.connect(release.db) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute(
            "SELECT name, state, priority FROM example ORDER BY name"
        ).fetchall() == [
            ("one", "done", 0),
            ("two", "open", 0),
        ]
        assert [row[1] for row in connection.execute("PRAGMA table_info(example)")] == [
            "name",
            "state",
            "priority",
        ]
    assert not (release.home / "old-workflows").exists()
    assert json.loads((release.home / "workflows/daily.json").read_text()) == {
        "enabled": True,
        "prompt": "preserve this",
    }
    assert migrations.read_revision(release) == 2
    marker = release.home / migrations.MARKER
    before = marker.stat().st_mtime_ns
    migrations.apply(release)  # Neither destructive rename nor ALTER TABLE repeats.
    assert migrations.pending(release) == () and migrations.plan(release) == ()
    assert marker.stat().st_mtime_ns == before
    assert marker.stat().st_mode & 0o777 == 0o600


def test_only_steps_after_saved_revision_run(release):
    upgrade_database(release)
    write_json(release.home / migrations.MARKER, {"revision": 1})
    assert migrations.plan(release) == ("old-workflows", "workflows")
    migrations.apply(release)
    assert migrations.read_revision(release) == 2
    assert (release.home / "workflows/daily.json").is_file()


def test_failed_step_never_advances_its_marker_and_can_resume(release):
    target = release.home / "workflows"
    target.mkdir()
    (target / "keep.txt").write_text("user content")
    with pytest.raises(UpdateError, match=r"migration 2 .* destination already exists"):
        migrations.apply(release)
    assert migrations.read_revision(release) == 1
    assert (release.home / "old-workflows/daily.json").is_file()
    assert (target / "keep.txt").read_text() == "user content"
    target.rename(release.home / "preserved-workflows")
    migrations.apply(release)
    assert migrations.read_revision(release) == 2


def test_database_failure_rolls_back_schema_rows_and_version(enso_home, monkeypatch):
    with sqlite3.connect(enso_home.db) as connection:
        connection.execute("CREATE TABLE example (name TEXT)")
        connection.execute("INSERT INTO example VALUES ('original')")
        connection.execute("PRAGMA user_version = 1")

    def fail_database(paths):
        with sqlite3.connect(paths.db) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ALTER TABLE example ADD COLUMN priority INTEGER DEFAULT 0")
            connection.execute("UPDATE example SET name = 'changed'")
            connection.execute("PRAGMA user_version = 2")
            raise ValueError("simulated migration failure")

    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        (migrations.Migration(1, "failing database", lambda _: ("enso.db",), fail_database),),
    )
    with pytest.raises(UpdateError, match="simulated migration failure"):
        migrations.apply(enso_home)
    with sqlite3.connect(enso_home.db) as connection:
        assert connection.execute("SELECT * FROM example").fetchall() == [("original",)]
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    assert not (enso_home.home / migrations.MARKER).exists()


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "[]",
        "{}",
        '{"revision": true}',
        '{"revision": -1}',
        '{"revision": "0"}',
        '{"revision": 0, "other": 1}',
        "[" * 1500,
    ],
)
def test_malformed_marker_refuses_every_operation(enso_home, content):
    marker = enso_home.home / migrations.MARKER
    marker.write_text(content)
    for operation in (migrations.pending, migrations.plan, migrations.apply):
        with pytest.raises(UpdateError, match=r"\.migrations\.json"):
            operation(enso_home)
    assert marker.read_text() == content


def test_newer_marker_refuses_downgrade(enso_home):
    write_json(enso_home.home / migrations.MARKER, {"revision": migrations.latest_revision() + 1})
    with pytest.raises(UpdateError, match="newer than this Enso supports"):
        migrations.apply(enso_home)


@pytest.mark.parametrize("revisions", [(2,), (1, 3), (1, 1)])
def test_missing_registry_step_is_rejected_before_any_changes(enso_home, monkeypatch, revisions):
    ran = []
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        tuple(
            migrations.Migration(number, "invalid", lambda _: (), lambda _: ran.append(True))
            for number in revisions
        ),
    )
    with pytest.raises(UpdateError, match="consecutive revisions"):
        migrations.apply(enso_home)
    assert ran == [] and not (enso_home.home / migrations.MARKER).exists()


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_marker_must_be_a_regular_file(enso_home, tmp_path, kind):
    marker = enso_home.home / migrations.MARKER
    if kind == "symlink":
        outside = tmp_path / "marker.json"
        outside.write_text('{"revision": 0}')
        marker.symlink_to(outside)
    else:
        marker.mkdir()
    with pytest.raises(UpdateError, match="must be a regular file"):
        migrations.apply(enso_home)


def test_empty_registry_records_baseline_without_other_changes(enso_home, monkeypatch):
    monkeypatch.setattr(migrations, "MIGRATIONS", ())
    migrations.apply(enso_home)
    assert json.loads((enso_home.home / migrations.MARKER).read_text()) == {"revision": 0}


def test_scattered_locks_are_removed_and_everything_else_is_kept(enso_home):
    home, runtime = enso_home.home, enso_home.runtime_dir
    job = enso_home.workspace_jobs("default") / "digest"
    gate = enso_home.workspace_heartbeat("default") / "HB-001"
    for directory in (job, gate, home / "heartbeat/.locks", home / ".workflow-locks"):
        directory.mkdir(parents=True)
    for directory in (runtime / ".concurrency", runtime / "worktree-locks"):
        directory.mkdir(parents=True)
    locks = [
        *(home / name for name in (".config.lock", ".skills.lock", ".knowledge.lock")),
        home / ".memory.lock",
        home / "heartbeat/.locks/HB-001.lock",
        home / ".workflow-locks/events.lock",
        runtime / ".concurrency/abc.lock",
        runtime / "worktree-locks/EN-001.lock",
        job / ".run.lock",
    ]
    kept = [
        job / "JOB.md",
        gate / "gate.sh",
        home / "heartbeat/notes.txt",
        runtime / "control.lock",
    ]
    for path in (*locks, *kept):
        path.touch()

    assert migrations.plan(enso_home) == ()
    migrations.apply(enso_home)
    migrations.remove_scattered_locks(enso_home)  # a retry finds nothing left to do

    assert not any(path.exists() for path in locks)
    assert all(path.exists() for path in kept)
    assert not (home / "heartbeat/.locks").exists() and not (home / ".workflow-locks").exists()
    assert not (runtime / ".concurrency").exists() and not (runtime / "worktree-locks").exists()
    assert migrations.read_revision(enso_home) == 1
