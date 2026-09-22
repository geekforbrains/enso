"""Release migrations preserve data, advance in order, and leave failures recoverable."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from enso import (
    audit_job_migration,
    db,
    frontmatter,
    job_migration,
    knowledge,
    migrations,
    runs,
    update_snapshot,
)
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


# The retired portion of schema 1; every other table has the current layout.
HISTORY_SCHEMA = """
-- Captures are permanent history; reply identity is its addressed parent, not a send ID.
CREATE TABLE _enso_captures (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  transport TEXT NOT NULL, workspace TEXT NOT NULL, conversation TEXT NOT NULL,
  channel TEXT NOT NULL, thread TEXT, message_id TEXT,
  sender_id TEXT NOT NULL, sender_name TEXT NOT NULL, occurred_at TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('addressed', 'ambient', 'reply')),
  parent_id INTEGER UNIQUE,
  text TEXT NOT NULL, truncated INTEGER NOT NULL CHECK (truncated IN (0, 1)),
  attachments TEXT NOT NULL DEFAULT '[]',
  outcome TEXT NOT NULL CHECK (outcome IN
    ('pending', 'completed', 'failed', 'cancelled', 'timed_out', 'dropped', 'empty',
     'interrupted')),
  delivery TEXT NOT NULL CHECK (delivery IN
    ('unattempted', 'sending', 'complete', 'partial', 'failed', 'uncertain')),
  parts TEXT NOT NULL DEFAULT '[]',
  finalized INTEGER NOT NULL CHECK (finalized IN (0, 1)),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  CHECK ((kind = 'reply' AND parent_id IS NOT NULL AND message_id IS NULL)
    OR (kind != 'reply' AND parent_id IS NULL AND message_id IS NOT NULL)),
  UNIQUE (transport, channel, message_id)
);
CREATE INDEX _enso_captures_workspace ON _enso_captures (workspace, id);
CREATE INDEX _enso_captures_conversation
  ON _enso_captures (workspace, conversation, id);

-- A receipt reserves its sources before Markdown publication. Completion is separate.
CREATE TABLE _enso_memory_receipts (
  id TEXT PRIMARY KEY, workspace TEXT NOT NULL, outputs TEXT NOT NULL,
  completed_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE _enso_memory_inputs (
  capture_id INTEGER PRIMARY KEY, receipt_id TEXT NOT NULL
);
CREATE INDEX _enso_memory_inputs_receipt ON _enso_memory_inputs (receipt_id);
CREATE TABLE _enso_memory_progress (
  workspace TEXT PRIMARY KEY, capture_id INTEGER NOT NULL
);
-- One current job batch per workspace; follow-ups keep the same source budget.
CREATE TABLE _enso_memory_batches (
  workspace TEXT PRIMARY KEY, run_id TEXT NOT NULL, sources TEXT NOT NULL
);
"""


OLD_RUNS = """
CREATE TABLE runs (
  id TEXT PRIMARY KEY, job TEXT NOT NULL, workspace TEXT NOT NULL,
  provider TEXT NOT NULL, model TEXT NOT NULL, effort TEXT NOT NULL,
  trigger TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, duration_ms INTEGER,
  status TEXT NOT NULL, exit_code INTEGER, output TEXT, error TEXT,
  session_id TEXT, postrun_error TEXT);
CREATE INDEX runs_job ON runs (workspace, job, started_at DESC);
CREATE TRIGGER _enso_runs_delete_attempts AFTER DELETE ON runs
BEGIN DELETE FROM _enso_run_attempts WHERE run_id = OLD.id; END;
"""


def old_job_database(paths):
    """Use schema 3's real nonnullable history shape, not a current table with an old marker."""
    db.initialize(paths)
    with sqlite3.connect(paths.db) as connection:
        connection.executescript("DROP TABLE runs; DROP TABLE _enso_job_waiters;" + OLD_RUNS)
        connection.execute("PRAGMA user_version = 3")


@pytest.fixture
def prior_home(enso_home):
    old_job_database(enso_home)
    write_json(enso_home.home / migrations.MARKER, {"revision": 2})
    with sqlite3.connect(enso_home.db) as connection:
        connection.executescript("DROP TABLE _enso_received_messages;" + HISTORY_SCHEMA)
        connection.execute("DROP TABLE _enso_secrets")
        connection.execute("DROP TABLE _enso_secret_store")
        connection.execute("PRAGMA user_version = 1")
        connection.execute(
            "INSERT INTO _enso_captures (transport, workspace, conversation, channel, "
            "message_id, sender_id, sender_name, occurred_at, kind, text, truncated, "
            "outcome, delivery, finalized, created_at, updated_at) VALUES "
            "('slack', 'default', 'slack:D1', 'D1', '1.0', 'U1', 'User', '2026-09-20', "
            "'addressed', 'private transcript', 0, 'completed', 'unattempted', 1, 'now', 'now')"
        )
        connection.execute("CREATE TABLE user_data (value TEXT)")
        connection.execute("INSERT INTO user_data VALUES ('keep this')")
        for number, (workspace, job) in enumerate(
            (("default", "memory"), ("team", "enso-memory"), ("team", "digest"))
        ):
            connection.execute(
                "INSERT INTO runs (id, job, workspace, provider, model, effort, trigger, "
                "started_at, status) VALUES (?, ?, ?, 'codex', 'model', 'low', "
                "'schedule', 'now', 'ok')",
                (str(number), job, workspace),
            )
            connection.execute(
                "INSERT INTO _enso_run_attempts (run_id, number, status, output, error, "
                "postrun_output, postrun_error) VALUES (?, 0, 'ok', '', '', '', '')",
                (str(number),),
            )
            connection.execute(
                "INSERT INTO job_state (workspace, job) VALUES (?, ?)", (workspace, job)
            )
            connection.execute(
                "INSERT INTO messages (created_at, workspace, transport, target, text, "
                "source, status) VALUES ('now', ?, 'slack', 'D1', 'alert', ?, 'sent')",
                ("other", f"job:{workspace}:{job}"),
            )
    for name in (
        "skills/enso-memory/SKILL.md",
        "workspaces/team/jobs/enso-memory/JOB.md",
        "workspaces/default/jobs/memory/batches/old.json",
        "workspaces/team/jobs/digest/JOB.md",
        "workspaces/team/memory/Keep.md",
        "shared/knowledge/Memory/2026-09-20.md",
    ):
        target = enso_home.home / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("user content stays unless its job was retired")
    for parts in (("memory",), ("jobs", "default", "memory"), ("jobs", "team", "enso-memory")):
        enso_home.lock(*parts).touch()
    enso_home.lock("jobs", "team", "digest").touch()
    write_json(
        enso_home.home / ".bundles.json",
        {
            "files": {
                "skills/enso-memory/SKILL.md": "edited",
                "workspaces/team/jobs/enso-memory/JOB.md": "edited",
                "skills/enso-jobs/SKILL.md": "preserved",
            }
        },
    )
    digest = enso_home.workspace_jobs("team") / "digest/JOB.md"
    digest.write_text(LEGACY_JOB)
    return enso_home


def test_upgrade_retires_processing_state_and_jobs_but_preserves_notes(prior_home):
    paths = prior_home
    kept = [
        paths.home / name
        for name in (
            "workspaces/team/memory/Keep.md",
            "shared/knowledge/Memory/2026-09-20.md",
        )
    ]
    before = {path: path.read_bytes() for path in kept}
    assert all("memory/Keep.md" not in name for name in migrations.plan(paths))
    migrations.apply(paths)
    migrations.apply(paths)
    assert migrations.read_revision(paths) == migrations.latest_revision()
    assert {path: path.read_bytes() for path in kept} == before
    assert (
        frontmatter.read(paths.workspace_jobs("team") / "digest/JOB.md").body == "Keep this prompt."
    )
    assert not (paths.skills / "enso-memory").exists()
    assert not (paths.workspace_jobs("team") / "enso-memory").exists()
    assert not (paths.workspace_jobs("default") / "memory").exists()
    assert not (paths.runtime_dir / "locks/memory.lock").exists()
    assert not (paths.runtime_dir / "locks/jobs/default/memory.lock").exists()
    assert not (paths.runtime_dir / "locks/jobs/team/enso-memory.lock").exists()
    assert (paths.runtime_dir / "locks/jobs/team/digest.lock").exists()
    assert json.loads((paths.home / ".bundles.json").read_text()) == {
        "files": {"skills/enso-jobs/SKILL.md": "preserved"}
    }
    with db.reader(paths) as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert not any(name.startswith(("_enso_memory", "_enso_captures")) for name in names)
        assert connection.execute("SELECT value FROM user_data").fetchone()[0] == "keep this"
        for table in ("runs", "job_state"):
            assert [
                tuple(row) for row in connection.execute(f"SELECT workspace, job FROM {table}")
            ] == [("team", "digest")]
        assert [row[0] for row in connection.execute("SELECT run_id FROM _enso_run_attempts")] == [
            "2"
        ]
        assert [row[0] for row in connection.execute("SELECT source FROM messages")] == [
            "job:team:digest"
        ]
    assert not db.admit_message(paths, "slack", "D1", "1.0")
    assert db.admit_message(paths, "slack", "D1", "2.0")


def test_retirement_snapshot_recovers_database_jobs_and_receipts(prior_home, tmp_path):
    paths = prior_home
    directory = tmp_path / "operation"
    directory.mkdir()
    names = update_snapshot.plan(
        paths, [*migrations.plan(paths), migrations.MARKER, "enso.db-wal", "enso.db-shm"]
    )
    update_snapshot.capture(paths, directory, names)
    migrations.apply(paths)
    update_snapshot.restore(paths, directory)
    assert migrations.read_revision(paths) == 2
    assert (paths.skills / "enso-memory/SKILL.md").is_file()
    assert (paths.workspace_jobs("default") / "memory/batches/old.json").is_file()
    with sqlite3.connect(paths.db) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute("SELECT text FROM _enso_captures").fetchone()[0]
            == "private transcript"
        )
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 3
    migrations.apply(paths)
    assert migrations.read_revision(paths) == migrations.latest_revision()


def test_retirement_refuses_escaping_paths_before_database_changes(prior_home, tmp_path):
    paths = prior_home
    target = paths.skills / "enso-memory"
    target.rename(tmp_path / "outside")
    target.symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(UpdateError, match="symbolic link"):
        migrations.apply(paths)
    with sqlite3.connect(paths.db) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    assert (tmp_path / "outside/SKILL.md").is_file()
    assert migrations.read_revision(paths) == 2


def test_retirement_retries_after_filesystem_failure(prior_home, monkeypatch):
    remove = migrations.shutil.rmtree
    with monkeypatch.context() as patch:
        patch.setattr(
            migrations.shutil, "rmtree", lambda path: (_ for _ in ()).throw(OSError("busy"))
        )
        with pytest.raises(UpdateError, match="busy"):
            migrations.apply(prior_home)
    assert migrations.read_revision(prior_home) == 2
    assert migrations.shutil.rmtree is remove
    migrations.apply(prior_home)
    assert migrations.read_revision(prior_home) == migrations.latest_revision()
    assert not (prior_home.skills / "enso-memory").exists()


LEGACY_JOB = """---
name: Custom report
schedule: '0 * * * *'
provider: claude
model: sonnet
effort: high
enabled: false
---

Keep this prompt.
"""


def legacy_job(paths, *, workspace="default", name="report", fields=None, body=None):
    file = paths.workspace_jobs(workspace) / name / "JOB.md"
    file.parent.mkdir(parents=True, exist_ok=True)
    document, _ = frontmatter.parse(LEGACY_JOB)
    values = document.fields | (fields or {})
    file.write_text(frontmatter.render(values, document.body if body is None else body))
    return file


def test_job_migration_converts_every_workspace_preserving_scripts_and_prompt(enso_home):
    step = migrations.MIGRATIONS[4]
    old_job_database(enso_home)
    first = legacy_job(
        enso_home,
        fields={
            "prerun": "scripts/collect report.sh",
            "prerun_timeout": 47,
            "postrun": "verify's report.sh",
            "postrun_timeout": 51,
            "max_followups": 4,
            "concurrency_group": "reporting",
            "secrets": ["REPORT_TOKEN"],
        },
        body="  Fetch:\n\n{{prerun_output}}\n\nKeep user instructions.",
    )
    second = legacy_job(enso_home, workspace="disabled", name="nightly")
    script = first.parent / "custom.sh"
    script.write_text("printf 'leave me alone'; exit 1\n")
    original = script.read_bytes()
    before = first.read_text().partition("\n---\n")[2]
    first.chmod(0o640)
    assert step.paths(enso_home) == (
        "enso.db",
        ".bundles.json",
        "workspaces/default/jobs",
        "workspaces/disabled/jobs",
    )
    assert "provider: claude" in first.read_text()  # preview is read-only
    step.apply(enso_home)
    document = frontmatter.read(first)
    assert document.fields == {
        "name": "Custom report",
        "schedule": "0 * * * *",
        "enabled": False,
        "secrets": ["REPORT_TOKEN"],
        "agent": {"provider": "claude", "model": "sonnet", "effort": "high", "max_followups": 4},
        "gate": {"command": "bash 'scripts/collect report.sh'", "timeout": 47},
        "postrun": {"command": "bash 'verify'\"'\"'s report.sh'", "timeout": 51},
        "concurrency": {"group": "reporting", "on_busy": "skip"},
    }
    assert first.read_text().partition("\n---\n")[2] == before.replace(
        "{{prerun_output}}", "{{gate_output}}"
    )
    assert first.stat().st_mode & 0o777 == 0o640
    assert frontmatter.read(second).fields["agent"]["provider"] == "claude"
    assert script.read_bytes() == original
    after = {path: path.read_bytes() for path in (first, second, script)}
    step.apply(enso_home)
    assert {path: path.read_bytes() for path in after} == after


def test_job_migration_preserves_project_command_and_integration_ownership(enso_home):
    definition = enso_home.project("default", "EN") / "PROJECT.md"
    definition.parent.mkdir(parents=True)
    definition.write_text(
        frontmatter.render(
            {
                "name": "Enso",
                "stages": [
                    "build",
                    {"name": "test", "command": "bash check.sh"},
                    {"name": "merge", "integrate": True},
                ],
            },
            "User project description.",
        )
    )
    files = [
        legacy_job(enso_home, name=stage, fields={"project": "EN", "stage": stage})
        for stage in ("build", "test", "merge")
    ]
    before = definition.read_bytes()
    migrations.MIGRATIONS[4].apply(enso_home)
    assert "agent" in frontmatter.read(files[0]).fields
    for file in files[1:]:
        fields = frontmatter.read(file).fields
        assert "agent" not in fields and "command" not in fields and "provider" not in fields
        assert fields["project"] == "EN"
    assert definition.read_bytes() == before
    migrations.MIGRATIONS[4].apply(enso_home)


def test_job_migration_rebuilds_history_without_losing_attempts_or_user_objects(enso_home):
    old_job_database(enso_home)
    with sqlite3.connect(enso_home.db) as connection:
        connection.executescript(
            "CREATE TABLE user_history (value TEXT);"
            "INSERT INTO user_history VALUES ('keep');"
            "CREATE VIEW user_run_view AS SELECT id, status FROM runs;"
            "CREATE INDEX user_run_status ON runs(status);"
            "CREATE TRIGGER user_run_deleted AFTER DELETE ON runs BEGIN "
            "INSERT INTO user_history VALUES (OLD.id); END;"
        )
        for number, provider in enumerate(("claude", "command")):
            connection.execute(
                "INSERT INTO runs (id, job, workspace, provider, model, effort, trigger, "
                "started_at, status, output)"
                " VALUES (?, 'report', 'default', ?, 'model', 'high', 'manual', 'now', "
                "'prerun_error', 'kept')",
                (str(number), provider),
            )
            connection.execute(
                "INSERT INTO _enso_run_attempts (run_id, number, status, output, error, "
                "postrun_output, postrun_error) "
                "VALUES (?, 0, 'prerun_error', 'attempt', 'error', '', '')",
                (str(number),),
            )
    migrations.MIGRATIONS[4].apply(enso_home)
    with db.reader(enso_home) as connection:
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT kind, provider, model, effort, status, output FROM runs ORDER BY id"
            )
        ] == [
            ("agent", "claude", "model", "high", "gate_error", "kept"),
            ("command", None, None, None, "gate_error", "kept"),
        ]
        assert connection.execute("SELECT count(*) FROM user_run_view").fetchone()[0] == 2
        assert connection.execute("SELECT value FROM user_history").fetchone()[0] == "keep"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert runs.attempts(enso_home, "0")[0].status == "gate_error"
    with db.transaction(enso_home) as connection:
        connection.execute(
            "INSERT INTO _enso_job_waiters(run_id, workspace, job, group_name) "
            "VALUES ('0', 'default', 'report', 'reporting')"
        )
        connection.execute("DELETE FROM runs WHERE id = '0'")
        assert connection.execute("SELECT count(*) FROM _enso_job_waiters").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM _enso_run_attempts").fetchone()[0] == 1
        assert (
            connection.execute("SELECT value FROM user_history ORDER BY rowid DESC").fetchone()[0]
            == "0"
        )
    migrations.MIGRATIONS[4].apply(enso_home)


@pytest.mark.parametrize("conflict", ["duplicate", "mixed", "symlink", "oversized"])
def test_job_migration_preflights_all_jobs_before_changing_any(enso_home, tmp_path, conflict):
    old_job_database(enso_home)
    first = legacy_job(enso_home, name="aaa")
    bad = legacy_job(enso_home, name="zzz")
    if conflict == "duplicate":
        bad.write_text(LEGACY_JOB.replace("enabled: false", "enabled: false\nenabled: true"))
    elif conflict == "mixed":
        bad.write_text(LEGACY_JOB.replace("enabled: false", "enabled: false\ncommand: echo hi"))
    elif conflict == "symlink":
        outside = tmp_path / "outside.md"
        bad.rename(outside)
        bad.symlink_to(outside)
    else:
        bad.write_text(LEGACY_JOB + "x" * job_migration._MAX_DOCUMENT)
    before = first.read_bytes()
    for action in (migrations.MIGRATIONS[4].paths, migrations.MIGRATIONS[4].apply):
        with pytest.raises(UpdateError):
            action(enso_home)
        assert first.read_bytes() == before
        with sqlite3.connect(enso_home.db) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 3


def test_job_migration_retries_partial_files_and_preserves_bundle_receipts(enso_home, monkeypatch):
    import hashlib

    old_job_database(enso_home)
    files = [legacy_job(enso_home, name=name) for name in ("aaa", "bbb")]
    receipt = {
        file.relative_to(enso_home.home).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in files
    }
    write_json(enso_home.home / ".bundles.json", {"files": receipt})
    write_json(enso_home.home / migrations.MARKER, {"revision": 4})
    publish = job_migration.write_bytes

    def fail_second(path, content, **kwargs):
        if path == files[1]:
            raise OSError("injected disk failure")
        publish(path, content, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(job_migration, "write_bytes", fail_second)
        with pytest.raises(UpdateError, match="injected disk failure"):
            migrations.apply(enso_home)
    assert migrations.read_revision(enso_home) == 4
    assert "agent" in frontmatter.read(files[0]).fields
    assert "provider" in frontmatter.read(files[1]).fields
    migrations.apply(enso_home)
    assert migrations.read_revision(enso_home) == migrations.latest_revision()
    receipts = json.loads((enso_home.home / ".bundles.json").read_text())["files"]
    for file in files:
        assert (
            receipts[file.relative_to(enso_home.home).as_posix()]
            == hashlib.sha256(file.read_bytes()).hexdigest()
        )


def test_job_migration_converts_only_known_bundled_release_check(enso_home):
    fields = frontmatter.parse(LEGACY_JOB)[0].fields | {
        "name": "Enso release check",
        "prerun": "prerun.sh",
        "catch_up": True,
    }
    files = []
    for workspace in ("default", "custom"):
        file = legacy_job(
            enso_home,
            workspace=workspace,
            name="enso-update",
            fields=fields,
            body=job_migration._UPDATE_BODY,
        )
        script = file.parent / "prerun.sh"
        script.write_bytes(
            job_migration._UPDATE_SCRIPT + (b"# Customized\n" if workspace == "custom" else b"")
        )
        files.append(file)
    migrations.MIGRATIONS[4].apply(enso_home)
    converted = frontmatter.read(files[0]).fields
    assert converted["command"] == "enso update check --notify --quiet"
    assert converted["enabled"] is False and converted["schedule"] == "0 * * * *"
    assert "agent" not in converted and "gate" not in converted
    assert not (files[0].parent / "prerun.sh").exists()
    custom = frontmatter.read(files[1]).fields
    assert "command" not in custom and "agent" in custom and "gate" in custom
    assert (files[1].parent / "prerun.sh").is_file()


def test_job_migration_snapshot_restores_database_and_jobs(enso_home, tmp_path):
    old_job_database(enso_home)
    file = legacy_job(enso_home)
    before = file.read_bytes()
    operation = tmp_path / "operation"
    operation.mkdir()
    step = migrations.MIGRATIONS[4]
    names = update_snapshot.plan(enso_home, [*step.paths(enso_home), "enso.db-wal", "enso.db-shm"])
    update_snapshot.capture(enso_home, operation, names)
    step.apply(enso_home)
    update_snapshot.restore(enso_home, operation)
    assert file.read_bytes() == before
    with sqlite3.connect(enso_home.db) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert "kind" not in [row[1] for row in connection.execute("PRAGMA table_info(runs)")]
    step.apply(enso_home)


@pytest.mark.parametrize("customized", ["script", "deleted-script", "prompt"])
def test_job_migration_and_bundle_refresh_preserve_custom_release_checks(enso_home, customized):
    import hashlib

    from enso import workspaces
    from enso.config import Agent

    file = legacy_job(
        enso_home,
        name="enso-update",
        fields={"name": "Enso release check", "prerun": "prerun.sh", "catch_up": True},
        body=job_migration._UPDATE_BODY,
    )
    script = file.parent / "prerun.sh"
    script.write_bytes(job_migration._UPDATE_SCRIPT)
    receipts = {
        path.relative_to(enso_home.home).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (file, script)
    }
    write_json(enso_home.home / ".bundles.json", {"files": receipts})
    if customized == "script":
        script.write_bytes(script.read_bytes() + b"# Operator customization\n")
    elif customized == "deleted-script":
        script.unlink()
    else:
        file.write_text(file.read_text() + "\nOperator-owned instructions.\n")
    expected_script = script.read_bytes() if script.exists() else None
    for _ in range(2):
        migrations.MIGRATIONS[4].apply(enso_home)
    migrated = file.read_bytes()
    for _ in range(2):
        workspaces.reconcile_bundles(enso_home, Agent("claude", "opus", "high"))
    assert file.read_bytes() == migrated
    assert frontmatter.read(file).fields["gate"]["command"] == "bash prerun.sh"
    assert "command" not in frontmatter.read(file).fields
    assert (script.read_bytes() if script.exists() else None) == expected_script
    retained = json.loads((enso_home.home / ".bundles.json").read_text())["files"]
    name = file.relative_to(enso_home.home).as_posix()
    assert retained[name] == receipts[name]
    assert script.relative_to(enso_home.home).as_posix() not in retained


def historical_audit(paths, *, workspace="default", legacy=False, overrides=None):
    """Frozen revision-5 content, independent of later changes to the shipped audit."""
    fixture = Path(__file__).parent / "fixtures" / "audit-job-v5"
    document = frontmatter.read(fixture / "JOB.md")
    fields = (
        document.fields
        | {
            "agent": {"provider": "claude", "model": "opus", "effort": "high"},
        }
        | (overrides or {})
    )
    body = document.body
    script = (fixture / "prerun.sh").read_bytes()
    if legacy:
        fields.update(fields.pop("agent"))
        fields["prerun"] = "prerun.sh"
        fields.pop("gate")
        body = body.replace("{{gate_output}}", "{{prerun_output}}")
        script = script.replace(b"# The gate contract", b"# The prerun contract").replace(
            b"{{gate_output}}", b"{{prerun_output}}"
        )
    file = paths.workspace_jobs(workspace) / "enso-audit" / "JOB.md"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(frontmatter.render(fields, body))
    (file.parent / "prerun.sh").write_bytes(script)
    return file


def audit_receipts(paths, *files):
    import hashlib

    state = {
        "files": {
            path.relative_to(paths.home).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for file in files
            for path in (file, file.parent / "prerun.sh")
        }
    }
    write_json(paths.home / ".bundles.json", state)
    return state


@pytest.mark.parametrize("legacy", [False, True])
def test_audit_command_upgrade_plans_old_layout_and_preserves_operational_settings(
    enso_home, legacy
):
    from enso import workspaces
    from enso.config import Agent

    overrides = {
        "enabled": False,
        "schedule": "15 6 * * 1",
        "timeout": 300,
        "misfire_grace_seconds": 60,
        "notify": "slack:C-AUDITS",
        "concurrency": {"group": "home-audit", "on_busy": "wait"},
    }
    first = historical_audit(enso_home, legacy=legacy, overrides=overrides)
    second = historical_audit(enso_home, legacy=legacy, workspace="disabled")
    first.chmod(0o640)
    audit_receipts(enso_home, first, second)
    if legacy:
        old_job_database(enso_home)
    write_json(enso_home.home / migrations.MARKER, {"revision": 4 if legacy else 5})
    before = first.read_bytes()
    declared = migrations.plan(enso_home)
    assert "workspaces/default/jobs/enso-audit" in declared
    assert "workspaces/disabled/jobs/enso-audit" in declared
    assert first.read_bytes() == before

    migrations.apply(enso_home)
    assert migrations.read_revision(enso_home) == migrations.latest_revision()
    for file in (first, second):
        fields = frontmatter.read(file).fields
        assert fields["command"] == "enso doctor --attention --notify --quiet"
        assert not {"provider", "agent", "gate", "prerun"} & fields.keys()
        assert not (file.parent / "prerun.sh").exists()
    assert first.stat().st_mode & 0o777 == 0o640
    migrated = first.read_bytes()
    for _ in range(2):
        workspaces.reconcile_bundles(enso_home, Agent("claude", "opus", "high"))
        migrations.MIGRATIONS[5].apply(enso_home)
    assert first.read_bytes() == migrated
    assert overrides.items() <= frontmatter.read(first).fields.items()


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("customized", ["prompt", "script", "deleted-script", "gate", "postrun"])
def test_audit_command_upgrade_and_bundle_refresh_preserve_custom_pairs(
    enso_home, legacy, customized
):
    from enso import workspaces
    from enso.config import Agent

    file = historical_audit(enso_home, legacy=legacy)
    script = file.parent / "prerun.sh"
    audit_receipts(enso_home, file)
    if customized == "prompt":
        file.write_text(file.read_text() + "\nKeep the operator's instructions.\n")
    elif customized == "script":
        script.write_bytes(script.read_bytes() + b"# Customized health check.\n")
    elif customized == "deleted-script":
        script.unlink()
    else:
        document = frontmatter.read(file)
        if customized == "gate":
            if legacy:
                document.fields["prerun_timeout"] = 300
            else:
                document.fields["gate"]["timeout"] = 300
        elif legacy:
            document.fields["postrun"] = "operator.sh"
        else:
            document.fields["postrun"] = {"command": "bash operator.sh"}
        file.write_text(frontmatter.render(document.fields, document.body))
    if legacy:
        migrations.MIGRATIONS[4].apply(enso_home)
    expected_job = file.read_bytes()
    expected_script = script.read_bytes() if script.exists() else None
    for _ in range(2):
        migrations.MIGRATIONS[5].apply(enso_home)
        workspaces.reconcile_bundles(enso_home, Agent("claude", "opus", "high"))
    assert file.read_bytes() == expected_job
    assert (script.read_bytes() if script.exists() else None) == expected_script
    receipts = json.loads((enso_home.home / ".bundles.json").read_text())["files"]
    assert file.relative_to(enso_home.home).as_posix() not in receipts
    assert script.relative_to(enso_home.home).as_posix() not in receipts


def test_audit_migration_retains_deleted_definition_and_untracked_operator_files(enso_home):
    from enso import workspaces
    from enso.config import Agent

    deleted = historical_audit(enso_home)
    state = audit_receipts(enso_home, deleted)
    script = deleted.parent / "prerun.sh"
    before = script.read_bytes()
    deleted.unlink()
    custom = historical_audit(enso_home, workspace="custom")
    custom.write_text(custom.read_text() + "\nOperator owns this copy.\n")
    custom_before = custom.read_bytes()
    migrations.MIGRATIONS[5].apply(enso_home)
    workspaces.reconcile_bundles(enso_home, Agent("claude", "opus", "high"))
    assert not deleted.exists()
    assert script.read_bytes() == before
    assert custom.read_bytes() == custom_before
    receipts = json.loads((enso_home.home / ".bundles.json").read_text())["files"]
    name = deleted.relative_to(enso_home.home).as_posix()
    assert receipts[name] == state["files"][name]


@pytest.mark.parametrize("failure", ["first-file", "second-file", "retirement"])
def test_audit_command_migration_retries_partial_publication(enso_home, monkeypatch, failure):
    import hashlib

    from enso import workspaces
    from enso.config import Agent

    first = historical_audit(enso_home)
    second = historical_audit(enso_home, workspace="other")
    audit_receipts(enso_home, first, second)
    write_json(enso_home.home / migrations.MARKER, {"revision": 5})
    publish, unlink = audit_job_migration.write_bytes, Path.unlink

    def fail_write(path, content, **kwargs):
        if path == (first if failure == "first-file" else second):
            raise OSError("injected publication failure")
        publish(path, content, **kwargs)

    def fail_unlink(path, **kwargs):
        if path == first.parent / "prerun.sh":
            raise OSError("injected retirement failure")
        unlink(path, **kwargs)

    with monkeypatch.context() as patch:
        if failure == "retirement":
            patch.setattr(Path, "unlink", fail_unlink)
        else:
            patch.setattr(audit_job_migration, "write_bytes", fail_write)
        with pytest.raises(UpdateError, match="injected"):
            migrations.apply(enso_home)
    assert migrations.read_revision(enso_home) == 5
    migrations.apply(enso_home)
    for file in (first, second):
        assert frontmatter.read(file).fields["command"] == audit_job_migration._COMMAND
        assert not (file.parent / "prerun.sh").exists()
    receipts = json.loads((enso_home.home / ".bundles.json").read_text())["files"]
    for file in (first, second):
        assert (
            receipts[file.relative_to(enso_home.home).as_posix()]
            == hashlib.sha256(file.read_bytes()).hexdigest()
        )
    workspaces.reconcile_bundles(enso_home, Agent("claude", "opus", "high"))
    assert first.read_text() == workspaces._bundled("jobs/enso-audit/JOB.md")
    before = (enso_home.home / ".bundles.json").read_bytes()
    migrations.MIGRATIONS[5].apply(enso_home)
    assert (enso_home.home / ".bundles.json").read_bytes() == before


@pytest.mark.parametrize("conflict", ["yaml", "script-link", "job-link", "oversized"])
def test_audit_migration_preflights_every_workspace_before_any_changes(
    enso_home, tmp_path, conflict
):
    first = historical_audit(enso_home)
    bad = historical_audit(enso_home, workspace="zzz")
    state = audit_receipts(enso_home, first, bad)
    if conflict == "yaml":
        bad.write_text("---\nname: [\n---\n")
    elif conflict == "oversized":
        bad.write_text(bad.read_text() + "x" * (1024 * 1024))
    else:
        path = bad if conflict == "job-link" else bad.parent / "prerun.sh"
        outside = tmp_path / path.name
        path.rename(outside)
        path.symlink_to(outside)
    before = first.read_bytes()
    step = migrations.MIGRATIONS[5]
    for operation in (step.paths, step.apply):
        with pytest.raises(UpdateError):
            operation(enso_home)
        assert first.read_bytes() == before
        assert (first.parent / "prerun.sh").exists()
        assert json.loads((enso_home.home / ".bundles.json").read_text()) == state


def test_audit_migration_snapshot_restores_files_and_bundle_ownership(enso_home, tmp_path):
    file = historical_audit(enso_home)
    script = file.parent / "prerun.sh"
    state = audit_receipts(enso_home, file)
    before = {path: path.read_bytes() for path in (file, script)}
    operation = tmp_path / "audit-migration"
    operation.mkdir()
    step = migrations.MIGRATIONS[5]
    names = update_snapshot.plan(enso_home, list(step.paths(enso_home)))
    update_snapshot.capture(enso_home, operation, names)
    step.apply(enso_home)
    update_snapshot.restore(enso_home, operation)
    assert {path: path.read_bytes() for path in (file, script)} == before
    assert json.loads((enso_home.home / ".bundles.json").read_text()) == state


def test_fresh_home_at_revision_six_seeds_command_audit_without_historical_script(tmp_path):
    from enso import initialization, workspaces
    from enso.config import Agent, Paths

    paths = Paths(tmp_path / "fresh-home")
    assert initialization.initialize_home(paths)["ok"]
    assert migrations.read_revision(paths) == migrations.latest_revision()
    assert migrations.MIGRATIONS[5].revision == 6
    workspaces.seed_jobs(paths, Agent("claude", "opus", "high"))
    job = paths.workspace_jobs("default") / "enso-audit"
    assert frontmatter.read(job / "JOB.md").fields["command"] == audit_job_migration._COMMAND
    assert not (job / "prerun.sh").exists()
    assert migrations.pending(paths) == ()
