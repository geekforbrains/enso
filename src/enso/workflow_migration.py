"""Preserve the old workflow engine's material and make it inert in a stopped home."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from .config import Paths
from .maintenance import UpdateError


def pause_legacy_workflows(paths: Paths) -> None:
    """Called under migration's drain, service stop, exclusive lock and database snapshot."""
    if not paths.db.exists():
        return
    with closing(sqlite3.connect(paths.db)) as con, con:
        con.execute("BEGIN IMMEDIATE")
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if con.execute("PRAGMA application_id").fetchone()[0] != 0x454E534F or version not in (
            5,
            6,
        ):
            raise UpdateError("expected an Enso database at schema 5 or 6")
        if version == 6:
            return
        con.execute(
            "ALTER TABLE _enso_tasks ADD COLUMN workflow_version INTEGER NOT NULL DEFAULT 1"
        )
        con.execute("ALTER TABLE _enso_tasks ADD COLUMN route TEXT")
        con.execute(
            "CREATE TABLE _enso_workflow_outputs ("
            "id TEXT PRIMARY KEY, task_ref TEXT NOT NULL, stage TEXT NOT NULL, "
            "revision INTEGER NOT NULL, transaction_id TEXT, data TEXT NOT NULL, "
            "inputs TEXT NOT NULL, digest TEXT NOT NULL, valid INTEGER NOT NULL DEFAULT 1, "
            "actor TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(task_ref,stage,revision))"
        )
        con.execute(
            "CREATE INDEX _enso_workflow_outputs_task ON _enso_workflow_outputs (task_ref,valid)"
        )
        for ident, status, raw in con.execute(
            "SELECT id,status,data FROM _enso_workflow_transactions"
        ).fetchall():
            if status not in ("working", "submitted", "checking", "repairing"):
                continue
            data = json.loads(raw)
            data.update(
                status="legacy",
                error="Legacy execution interrupted at engine activation; preserved, not accepted",
            )
            con.execute(
                "UPDATE _enso_workflow_transactions SET status='legacy',data=? WHERE id=?",
                (json.dumps(data), ident),
            )
        for ident, status, raw in con.execute(
            "SELECT id,status,data FROM _enso_workflow_events"
        ).fetchall():
            if status == "delivered":
                continue
            data = json.loads(raw)
            data.update(
                status="legacy",
                previous_status=status,
                error="Legacy lifecycle delivery paused; inspect external effects before replacing",
            )
            con.execute(
                "UPDATE _enso_workflow_events SET status='legacy',data=? WHERE id=?",
                (json.dumps(data), ident),
            )
        # Keep the old owner and stage in an event before releasing an interrupted execution.
        for ident, stage, run_id in con.execute(
            "SELECT id,stage,claim_run_id FROM _enso_tasks WHERE claim_run_id IS NOT NULL"
        ).fetchall():
            con.execute(
                "INSERT INTO _enso_task_events "
                "(task_id,kind,actor,run_id,from_stage,to_stage,message,payload,created_at) "
                "VALUES (?,'legacy_paused','enso',?,?,?,'Legacy execution stopped; work "
                "preserved', '{}',datetime('now'))",
                (ident, run_id, stage, stage),
            )
        con.execute(
            "UPDATE _enso_tasks SET claim_run_id=NULL,claim_actor=NULL,claim_at=NULL,attention=1 "
            "WHERE stage NOT IN ('done','cancelled')"
        )
        con.execute("PRAGMA user_version=6")
