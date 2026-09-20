"""Release migrations preserve data, advance in order, and leave failures recoverable."""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from enso import knowledge, migrations, update_snapshot
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


@pytest.mark.parametrize("content", ["{", "[]"])  # unparseable, and the wrong shape
def test_malformed_marker_refuses_every_operation(enso_home, content):
    marker = enso_home.home / migrations.MARKER
    marker.write_text(content)
    for operation in (migrations.pending, migrations.plan, migrations.apply):
        with pytest.raises(UpdateError, match=r"\.migrations\.json"):
            operation(enso_home)
    assert marker.read_text() == content


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_marker_that_is_not_a_regular_file_is_refused(enso_home, tmp_path, kind):
    marker = enso_home.home / migrations.MARKER
    outside = tmp_path / "marker.json"
    outside.write_text('{"revision": 0}')
    if kind == "symlink":
        marker.symlink_to(outside)
    else:
        marker.mkdir()
    with pytest.raises(UpdateError, match="regular file"):
        migrations.apply(enso_home)
    assert outside.read_text() == '{"revision": 0}'  # never followed, never written through


def test_newer_marker_refuses_downgrade(enso_home):
    write_json(enso_home.home / migrations.MARKER, {"revision": migrations.latest_revision() + 1})
    with pytest.raises(UpdateError, match="newer than this Enso supports"):
        migrations.apply(enso_home)


def test_missing_registry_step_is_rejected_before_any_changes(enso_home, monkeypatch):
    ran = []
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        tuple(
            migrations.Migration(number, "invalid", lambda _: (), lambda _: ran.append(True))
            for number in (1, 3)
        ),
    )
    with pytest.raises(UpdateError, match="consecutive revisions"):
        migrations.apply(enso_home)
    assert ran == [] and not (enso_home.home / migrations.MARKER).exists()


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

    step = migrations.MIGRATIONS[0]  # run this step alone, so later revisions never break it
    assert step.revision == 1 and step.paths(enso_home) == ()
    step.apply(enso_home)
    step.apply(enso_home)  # a retry finds nothing left to do

    assert not any(path.exists() for path in locks)
    assert all(path.exists() for path in kept)
    assert not (home / "heartbeat/.locks").exists() and not (home / ".workflow-locks").exists()
    assert not (runtime / ".concurrency").exists() and not (runtime / "worktree-locks").exists()


NOTE = """---
schema: enso.note/v1
id: 0d8f3b86-5a2e-4a4c-9b7e-1f8c2f0a6d11
created: "2026-01-01T00:00:00Z"
updated: "2026-01-02T00:00:00Z"
---

See [[general:Reference/Topic|the topic]] and [scope](general:Reference/Topic.md#scope).
The catalog strips [a spaced target](< general:Reference/Topic.md>) before its scope.
Prose that says general: stays, and so does `[[general:Reference/Topic]]` in code.
A code span may wrap: `[[general:Reference/Topic]] across
a line break` stays too.

```
[[general:Reference/Topic]]
```

[topic]: general:Reference/Topic.md
"""
RENAMED = (
    NOTE.replace("[[general:Reference/Topic|", "[[shared:Reference/Topic|")
    .replace("(general:", "(shared:")
    .replace("(< general:", "(< shared:")
    .replace("[topic]: general:", "[topic]: shared:")
)
# The catalog reads a leading block that is not a mapping as body, so its links count.
LOOSE = "---\nSee [[general:Reference/Topic]]\n---\nbody\n"
OLD_TIME = 1_600_000_000_000_000_000


def old_layout(paths, *, shared=True):
    """A revision 1 home: top-level knowledge, and notes in every root linking into it."""
    written = {}
    workspace = paths.workspace("default")
    notes = {
        workspace / "knowledge/Page.md": NOTE,
        workspace / "memory/undated/Recall.md": NOTE.replace("\n", "\r\n"),
        workspace / "knowledge/Plain.md": "No scoped links here.\n",
        workspace / "knowledge/Loose.md": LOOSE,
    }
    if shared:
        notes[paths.home / "knowledge/Index.md"] = NOTE
        notes[paths.home / "knowledge/Reference/Topic.md"] = "## Scope\n"
        notes[paths.home / "knowledge/Reference/diagram.png"] = "attachment bytes"
    for path, text in notes.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
        os.utime(path, ns=(OLD_TIME, OLD_TIME))
        written[path] = text
    return written


def test_shared_knowledge_moves_and_general_links_are_renamed_in_every_root(enso_home):
    step = migrations.MIGRATIONS[1]  # run this step alone, so later revisions never break it
    old_layout(enso_home)
    shared, workspace = enso_home.knowledge, enso_home.workspace("default")
    shared.mkdir(parents=True)  # an empty root, as a fixing audit creates, is no conflict
    assert step.revision == 2 and step.paths(enso_home) == (
        "knowledge",
        "shared",
        "workspaces/default/knowledge",
        "workspaces/default/memory",
    )

    step.apply(enso_home)

    assert not (enso_home.home / "knowledge").exists()
    assert (shared / "Index.md").read_bytes() == RENAMED.encode()
    assert (workspace / "knowledge/Page.md").read_bytes() == RENAMED.encode()
    assert (workspace / "knowledge/Loose.md").read_text() == LOOSE.replace("general:", "shared:")
    # Only the link targets change: line endings, metadata, and ``updated`` stay.
    recall = workspace / "memory/undated/Recall.md"
    assert recall.read_bytes() == RENAMED.replace("\n", "\r\n").encode()
    assert (shared / "Reference/diagram.png").read_text() == "attachment bytes"
    for path in (
        shared / "Index.md",
        shared / "Reference/Topic.md",
        shared / "Reference/diagram.png",
        workspace / "knowledge/Page.md",
        workspace / "knowledge/Plain.md",
        workspace / "knowledge/Loose.md",
        recall,
    ):
        assert path.stat().st_mtime_ns == OLD_TIME, path
    catalog = knowledge.scan(enso_home)
    assert [r.scope for r in catalog.roots] == ["shared", "workspace:default"]
    assert not [p for p in catalog.audit() if "link" in p["problem"]]

    before = {p: p.read_bytes() for p in enso_home.home.rglob("*.md")}
    step.apply(enso_home)  # a retry recognizes the completed move and finds nothing to rename
    assert {p: p.read_bytes() for p in enso_home.home.rglob("*.md")} == before
    assert step.paths(enso_home) == ("knowledge", "shared")


def test_shared_knowledge_without_an_old_root_is_created(enso_home):
    step = migrations.MIGRATIONS[1]
    old_layout(enso_home, shared=False)
    step.apply(enso_home)
    assert enso_home.knowledge.is_dir() and not list(enso_home.knowledge.iterdir())
    page = enso_home.workspace("default") / "knowledge/Page.md"
    assert page.read_bytes() == RENAMED.encode()


@pytest.mark.parametrize("conflict", ["both roots", "shared file", "move recovery"])
def test_shared_knowledge_conflicts_stop_before_any_change(enso_home, conflict):
    step = migrations.MIGRATIONS[1]
    written = old_layout(enso_home)
    if conflict == "both roots":
        (enso_home.knowledge / "Newer.md").parent.mkdir(parents=True)
        (enso_home.knowledge / "Newer.md").write_text("keep me\n")
        match = "both knowledge/ and shared/knowledge/ exist"
    elif conflict == "shared file":
        enso_home.shared.write_text("my shared list\n")
        match = "shared is not a directory"
    else:
        (enso_home.home / ".knowledge-move-abc123").mkdir()
        match = r"\.knowledge-move-\* recovery directory"
    for run in (step.paths, step.apply):  # the preview refuses as the step itself does
        with pytest.raises(UpdateError, match=match):
            run(enso_home)
    assert all(path.read_bytes() == text.encode() for path, text in written.items())
    assert enso_home.shared.exists() == (conflict != "move recovery")


def test_shared_knowledge_declared_paths_restore_the_old_layout(enso_home, tmp_path):
    step = migrations.MIGRATIONS[1]
    written = old_layout(enso_home)
    operation = tmp_path / "operation"
    operation.mkdir()
    names = update_snapshot.plan(enso_home, list(step.paths(enso_home)))
    update_snapshot.capture(enso_home, operation, names)
    step.apply(enso_home)
    update_snapshot.restore(enso_home, operation)
    assert not enso_home.shared.exists()
    assert all(path.read_bytes() == text.encode() for path, text in written.items())
