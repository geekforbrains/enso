"""Small declarative fixtures and result checks for skill evaluations."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from enso import db, tables
from enso.config import Paths

NAME = re.compile(r"[a-z][a-z0-9-]*")


def safe_path(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts or not relative:
        raise ValueError(f"Expected a relative path inside the fixture: {relative}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Fixture path escapes its root: {relative}")
    return path


def load_scenario(path: Path) -> dict[str, Any]:
    """Validate the scenario before any provider is launched."""
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a scenario object")
    for key in ("id", "skill"):
        if not isinstance(value.get(key), str) or not NAME.fullmatch(value[key]):
            raise ValueError(f"{path}: invalid {key}")
    if not isinstance(value.get("prompt"), str) or not value["prompt"].strip():
        raise ValueError(f"{path}: prompt is required")
    if not isinstance(value.get("checks"), list) or not value["checks"]:
        raise ValueError(f"{path}: at least one expected-result check is required")
    if type(value.get("protect_internal_state", True)) is not bool:
        raise ValueError(f"{path}: protect_internal_state must be a boolean")
    for check in value["checks"]:
        if check.get("kind") not in {"sql", "json", "text"} or not check.get("name"):
            raise ValueError(f"{path}: check needs a name and kind (sql/json/text)")
        required = {"sql": "query", "json": "path", "text": "path"}[check["kind"]]
        if not isinstance(check.get(required), str) or "expected" not in check:
            raise ValueError(f"{path}: check needs {required} and expected")
        if required == "path":
            safe_path(Path("/fixture"), check["path"])
    for relative, contents in value.get("files", {}).items():
        safe_path(Path("/fixture"), relative)
        if not isinstance(contents, str):
            raise ValueError(f"{path}: fixture file contents must be text")
    for statement in value.get("setup_sql", []):
        if not isinstance(statement, str):
            raise ValueError(f"{path}: setup_sql must contain SQL strings")
    value["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return value


def protected_state(database: Path) -> str:
    """Fingerprint Enso's state, except the user-table catalog which tasks may update."""
    check_database(database)
    state = []
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as con:
        for name, sql in con.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ):
            if name == "_enso_tables" or not (name.startswith("_enso_") or name in tables.RESERVED):
                continue
            rows = con.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall()
            state.append((name, sql, sorted(rows, key=repr)))
    return hashlib.sha256(repr(state).encode()).hexdigest()


def check_database(database: Path) -> None:
    if database.is_symlink() or database.parent.is_symlink():
        raise ValueError("Fixture database must not be a symlink")


def prepare(home: Path, workspace: Path, scenario: dict[str, Any]) -> str | None:
    """Seed only synthetic state; never initialize a service or copy a real home."""
    home.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    config = {
        "version": 2,
        "transports": {"slack": {"bot_token": "xoxb-eval", "app_token": "xapp-eval"}},
        "bindings": {},
        "defaults": {"provider": "codex", "model": "eval", "effort": "low"},
        "providers": {"codex": {"models": ["eval"], "args": []}},
    }
    (home / "config.json").write_text(json.dumps(config))
    paths = Paths(home)
    db.initialize(paths)
    with sqlite3.connect(paths.db) as con:
        for statement in scenario.get("setup_sql", []):
            con.executescript(statement)
    for table in scenario.get("tables", []):
        tables.register(paths, table["name"], table["description"])
    for relative, contents in scenario.get("files", {}).items():
        path = safe_path(workspace, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    return protected_state(paths.db) if scenario.get("protect_internal_state", True) else None


def check_results(
    home: Path, workspace: Path, scenario: dict[str, Any], protected: str | None
) -> list[dict[str, Any]]:
    """Check final state independently of the model's completion claim."""
    results: list[dict[str, Any]] = []
    actual: Any
    for check in scenario["checks"]:
        try:
            if check["kind"] == "sql":
                check_database(home / "enso.db")
                with sqlite3.connect(f"file:{home / 'enso.db'}?mode=ro", uri=True) as con:
                    con.execute("PRAGMA query_only = ON")
                    con.set_progress_handler(lambda: 1, 100_000)
                    actual = [list(row) for row in con.execute(check["query"]).fetchall()]
            else:
                path = safe_path(workspace, check["path"])
                if path.stat().st_size > 1_000_000:
                    raise ValueError("result file exceeds 1 MB")
                actual = path.read_text()
                if check["kind"] == "json":
                    actual = json.loads(actual)
            results.append(
                {
                    "name": check["name"],
                    "passed": actual == check["expected"],
                    "expected": check["expected"],
                    "actual": actual,
                }
            )
        except (OSError, ValueError, sqlite3.Error) as exc:
            results.append({"name": check["name"], "passed": False, "error": str(exc)})
    if protected is not None:
        try:
            intact = protected_state(home / "enso.db") == protected
        except sqlite3.Error, OSError, ValueError:
            intact = False
        results.append({"name": "Enso internal state preserved", "passed": intact})
    return results
