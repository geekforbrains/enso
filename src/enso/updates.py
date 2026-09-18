"""Managed release installation and recoverable, user-requested self-upgrades.

Artifacts are staged before stopping work. An independent OS service owns the
transaction, and a durable admission gate prevents new work until readiness has
been verified. Code selection and pre-migration data are recovered together.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import (
    __version__,
    db,
    initialization,
    messages,
    migrations,
    releases,
    update_services,
    update_snapshot,
    web,
    workspaces,
)
from .config import Paths, load_config, resolve_workspace, valid_workspace_name
from .connection_setup import receiver_active, service_receiver
from .maintenance import (
    UpdateError,
    daemon,
    exclusive_access,
    lock,
    paused,
    read_json,
    sync_directory,
    write_bytes,
    write_json,
)

TERMINAL = frozenset({"succeeded", "failed", "rolled_back", "recovery_failed", "deferred"})
# Baseline preparation paths. Future candidates add their complete migration plan
# before the running updater takes a snapshot; an old updater need not know new layouts.
MIGRATION_PATHS = (
    "config.json",
    "enso.db",
    "enso.db-wal",
    "enso.db-shm",
    "enso.db-journal",
    migrations.MARKER,
    "skills",
    "AGENTS.md",
    ".bundles.json",
    "slack",  # An upgrade may retire the previously seeded manifest; rollback needs it.
)
STATE_FIELDS = (
    "id",
    "status",
    "from_version",
    "to_version",
    "error",
    "started_at",
    "updated_at",
    "notification_error",
    "cleanup_error",
)


def _install_path(paths: Paths) -> Path:
    return paths.runtime_dir / "install.json"


def installed(paths: Paths) -> dict[str, Any]:
    receipt = read_json(_install_path(paths))
    if receipt:
        _validate_install(receipt)
    return receipt


def _validate_install(receipt: dict[str, Any]) -> None:
    """Persisted receipts are input, never trusted executable paths or arguments."""
    try:
        version = receipt["version"]
        valid = (
            receipt["schema_version"] == 1
            and isinstance(version, str)
            and re.fullmatch(r"\d+\.\d+\.\d+", version) is not None
            and isinstance(receipt["commit"], str)
            and re.fullmatch(r"[0-9a-f]{40}", receipt["commit"]) is not None
            and isinstance(receipt["release_id"], str)
            and re.fullmatch(re.escape(version) + r"-[0-9a-f]{12}", receipt["release_id"])
            is not None
            and isinstance(receipt["extras"], list)
            and all(
                isinstance(extra, str) and extra in {"slack", "telegram", "web"}
                for extra in receipt["extras"]
            )
            and isinstance(receipt["feed"], str)
            and bool(receipt["feed"])
            and isinstance(receipt["bin_dir"], str)
            and Path(receipt["bin_dir"]).is_absolute()
            and isinstance(receipt.get("viewer_service", ""), str)
            and (receipt.get("token_file") is None or isinstance(receipt["token_file"], str))
            and (receipt.get("token_origin") is None or isinstance(receipt["token_origin"], str))
        )
    except KeyError, TypeError, ValueError:
        valid = False
    if not valid:
        raise UpdateError("managed installation metadata is invalid; restore its original receipt")


def _operation_dir(paths: Paths, operation_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise UpdateError("invalid update operation id")
    directory = paths.runtime_dir / "operations" / operation_id
    if directory.is_symlink() or directory.parent.is_symlink():
        raise UpdateError("update operation directory must not be a symbolic link")
    return directory


def _operation(paths: Paths) -> dict[str, Any]:
    state = read_json(paths.update_state)
    if state:
        _validate_operation(state)
    return state


def _validate_operation(state: dict[str, Any]) -> None:
    try:
        valid = (
            isinstance(state["id"], str)
            and re.fullmatch(r"[0-9a-f]{32}", state["id"]) is not None
            and state["status"]
            in TERMINAL
            | {"queued", "staging", "draining", "stopping", "backing_up", "switching", "starting"}
            and isinstance(state["previous_install"], dict)
            and isinstance(state["manifest"], dict)
            and isinstance(state["source"], str)
            and isinstance(state["services"], dict)
            and type(state["services"]["daemon"]) is bool
            and type(state["services"]["viewer"]) is bool
            and isinstance(state["from_version"], str)
            and isinstance(state["to_version"], str)
            and isinstance(state["drain_timeout"], int | float)
            and 0 < state["drain_timeout"] <= 3600
            and isinstance(state["startup_timeout"], int | float)
            and 0 < state["startup_timeout"] <= 600
        )
    except KeyError, TypeError, ValueError:
        valid = False
    if not valid:
        raise UpdateError("update operation metadata is invalid; inspect its saved recovery files")
    _validate_install(state["previous_install"])


def _save(paths: Paths, state: dict[str, Any], status: str | None = None, **fields: Any) -> None:
    if status:
        state["status"] = status
    state.update(fields, updated_at=time.time())
    write_json(_operation_dir(paths, state["id"]) / "operation.json", state)
    write_json(paths.update_state, state)


def _public(state: dict[str, Any]) -> dict[str, Any] | None:
    return {key: state[key] for key in STATE_FIELDS if key in state} if state else None


def status(paths: Paths) -> dict[str, Any]:
    receipt = installed(paths)
    running = daemon(paths)
    return {
        "ok": True,
        "managed": bool(receipt),
        "installed_version": receipt.get("version"),
        "installed_commit": receipt.get("commit"),
        "running_version": running.get("version"),
        "operation": _public(_operation(paths)),
    }


def _token_origin(source: str) -> str | None:
    parsed = urlsplit(releases.normalize_source(source))
    if not parsed.scheme:
        return None
    host = f"[{parsed.hostname}]" if ":" in (parsed.hostname or "") else parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme}://{host}:{port}"


def _token(receipt: dict[str, Any], source: str) -> Path | None:
    """Use a saved credential only at its installation origin, including explicit overrides."""
    value = receipt.get("token_file")
    if not value:
        return None
    # Receipts predating origin persistence used their manifest as the saved feed.
    authorized = (
        receipt["token_origin"] if "token_origin" in receipt else _token_origin(receipt["feed"])
    )
    return Path(value) if authorized and _token_origin(source) == authorized else None


def _release(paths: Paths, source: str | None = None) -> releases.Release:
    receipt = installed(paths)
    selected = source or receipt.get("feed") or releases.DEFAULT_FEED
    return releases.load_release(selected, token_file=_token(receipt, selected))


def _version_key(value: str) -> tuple[int, int, int]:
    # Stable releases have one unambiguous order. Prerelease feeds can be introduced
    # with an explicit compatibility contract rather than silently following beta tags.
    match = re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value)
    if not match:
        raise UpdateError("managed updates require a stable major.minor.patch release version")
    return int(match[1]), int(match[2]), int(match[3])


def check(paths: Paths, source: str | None = None) -> dict[str, Any]:
    receipt = installed(paths)
    release = _release(paths, source)
    current = receipt.get("version", __version__)
    latest = _version_key(release.version)
    try:
        current_key: tuple[int, int, int] | None = _version_key(current)
    except UpdateError:
        if receipt:
            raise
        current_key = None
    available = current_key is not None and latest > current_key
    if release.version == current and receipt and release.commit != receipt["commit"]:
        raise UpdateError("release version was reused for different code; publish a new version")
    return {
        "ok": True,
        "managed": bool(receipt),
        "current": current,
        "available": release.version,
        "update_available": available,
        "release_notes_url": release.to_dict().get("release_notes_url"),
        "adoption_required": not bool(receipt),
        "development": current_key is None,
    }


def check_message(result: dict[str, Any]) -> str:
    """Describe release availability and the installation's next action together."""
    if result.get("development"):
        return (
            f"Enso {result['current']} is an unmanaged development install; "
            f"the latest published release is {result['available']}. "
            "Adopt a compatible published release when ready."
        )
    if result["update_available"]:
        text = f"Enso {result['available']} is available (you have {result['current']})."
    elif _version_key(result["current"]) > _version_key(result["available"]):
        text = f"Enso {result['current']} is ahead of the latest release, {result['available']}."
    else:
        text = f"Enso {result['current']} is up to date."
    if result["managed"]:
        if result["update_available"]:
            text += " Run enso update apply, or ask me to upgrade when ready."
    elif _version_key(result["current"]) <= _version_key(result["available"]):
        text += (
            " This installation is unmanaged. Stop its services, then run "
            "enso update install --adopt and reinstall the service with enso service install."
        )
    else:
        text += (
            " This installation is unmanaged; adopt a compatible published release when available."
        )
    return text


def update_workspace(paths: Paths, workspace: str | None = None) -> str:
    """Home updates use default for terminal notifications; chat keeps its owner."""
    return resolve_workspace(
        paths, workspace if workspace is not None else os.environ.get("ENSO_WORKSPACE") or "default"
    )


def _candidate(paths: Paths, release: releases.Release, receipt: dict[str, Any]) -> Path:
    target = paths.runtime_dir / "releases" / release.release_id
    private_uv = paths.runtime_dir / "tools" / "uv"
    return releases.prepare_release(
        release,
        target,
        extras=tuple(receipt["extras"]),
        token_file=_token(receipt, release.source),
        uv=str(private_uv),
    )


def _select(paths: Paths, target: Path) -> None:
    releases_root = (paths.runtime_dir / "releases").resolve()
    if target.resolve().parent != releases_root or not (target / "bin" / "enso").is_file():
        raise UpdateError("selected runtime is not a prepared Enso release")
    current = paths.runtime_dir / "current"
    if current.exists() and not current.is_symlink():
        raise UpdateError("managed current path must be a symbolic link")
    temporary = paths.runtime_dir / f".current-{uuid.uuid4().hex}"
    try:
        temporary.symlink_to(Path("releases") / target.name)
        os.replace(temporary, current)
        sync_directory(paths.runtime_dir)
    finally:
        temporary.unlink(missing_ok=True)


def _launcher(paths: Paths, bin_dir: Path, *, adopt: bool) -> None:
    target = bin_dir / "enso"
    marker = "# Enso managed launcher\n"
    text = (
        "#!/bin/sh\n"
        + marker
        + f"export ENSO_HOME={shlex.quote(str(paths.home))}\n"
        + f'exec {shlex.quote(str(paths.runtime_dir / "current" / "bin" / "enso"))} "$@"\n'
    )
    if target.exists() or target.is_symlink():
        try:
            ours = not target.is_symlink() and target.read_text("utf-8") == text
        except OSError, UnicodeError:
            ours = False
        if ours:
            return
        if not adopt:
            raise UpdateError(f"{target} already exists; use --adopt to preserve and replace it")
        backup = target.with_name(f"enso.before-managed-{uuid.uuid4().hex[:8]}")
        if target.is_symlink():
            backup.symlink_to(os.readlink(target))
        else:
            shutil.copy2(target, backup)
    write_bytes(target, text.encode(), 0o755)


def install(
    paths: Paths,
    source: str | None = None,
    *,
    bin_dir: Path,
    extras: tuple[str, ...],
    token_file: Path | None = None,
    feed: str | None = None,
    viewer_service: str = "",
    prepared_release: Path | None = None,
    adopt: bool = False,
) -> dict[str, Any]:
    """Select a first managed release; an existing managed install uses apply instead."""
    paths = Paths(paths.home.expanduser().resolve())
    with lock(paths), lock(paths, "worker"):
        if installed(paths):
            raise UpdateError("this home is already managed; use enso update apply")
        if receiver_active(paths) or web.status(paths).running:
            raise UpdateError("stop legacy Enso and its viewer before the one-time migration")
        if paused(paths):
            raise UpdateError("an interrupted update needs recovery before installation")
        release = releases.load_release(source or releases.DEFAULT_FEED, token_file=token_file)
        _version_key(release.version)
        release_feed = releases.normalize_source(feed or releases.DEFAULT_FEED)
        if not set(extras) <= {"slack", "telegram", "web"}:
            raise UpdateError("extras must be slack, telegram or web")
        # Test the external launcher location before doing a network installation.
        target_launcher = bin_dir.expanduser().resolve() / "enso"
        if (target_launcher.exists() or target_launcher.is_symlink()) and not adopt:
            raise UpdateError(f"{target_launcher} already exists; use --adopt")
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "version": release.version,
            "commit": release.commit,
            "release_id": release.release_id,
            "extras": sorted(set(extras)),
            "feed": release_feed,
            "bin_dir": str(bin_dir.expanduser().resolve()),
            "viewer_service": viewer_service,
            "token_file": None,
        }
        if token_file:
            token = token_file.read_bytes()
            if not token.strip() or len(token) > 16384:
                raise UpdateError("release token file is empty or too large")
            saved_token = paths.runtime_dir / "release.token"
            write_bytes(saved_token, token)
            receipt["token_file"] = str(saved_token)
            receipt["token_origin"] = _token_origin(release.source)
        target = paths.runtime_dir / "releases" / release.release_id
        if prepared_release is not None and prepared_release.resolve() != target.resolve():
            raise UpdateError("prepared release must be this home's exact versioned runtime path")
        # prepare_release validates its receipt and installed metadata even when reused.
        releases.ensure_uv(paths.runtime_dir)
        target = _candidate(paths, release, receipt)
        update_services.run_command(
            [str(target / "bin" / "enso"), "update", "_validate-home"],
            cwd=paths.home,
            env={**os.environ, "ENSO_HOME": str(paths.home)},
        )
        _select(paths, target)
        _launcher(paths, Path(receipt["bin_dir"]), adopt=adopt)
        write_json(_install_path(paths), receipt)
        return {
            "ok": True,
            "managed": True,
            "installed_version": release.version,
            "binary": str(target_launcher),
        }


def _origin(workspace: str) -> dict[str, str]:
    return {
        "transport": os.environ.get("ENSO_ORIGIN_TRANSPORT", ""),
        "channel": os.environ.get("ENSO_ORIGIN_CHANNEL", ""),
        "thread": os.environ.get("ENSO_ORIGIN_THREAD_TS", ""),
        "workspace": workspace,
    }


def request_apply(
    paths: Paths,
    source: str | None = None,
    *,
    workspace: str | None = None,
    drain_timeout: float = 300,
    startup_timeout: float = 60,
) -> dict[str, Any]:
    selected = update_workspace(paths, workspace)
    with lock(paths), lock(paths, "worker"):
        receipt = installed(paths)
        if not receipt:
            raise UpdateError(
                "this installation is unmanaged; stop its services, run enso update install "
                "--adopt, then enso service install"
            )
        previous = _operation(paths)
        if previous and (previous["status"] not in TERMINAL or paused(paths)):
            raise UpdateError("an update is pending; inspect update status or run update recover")
        if previous:
            if not update_services.cleanup_finished(paths, previous["id"]):
                raise UpdateError("the previous updater is still exiting; retry shortly")
            _cleanup(paths, previous, helper_finished=True)
        release = _release(paths, source)
        if _version_key(release.version) <= _version_key(receipt["version"]):
            if release.version == receipt["version"] and release.commit == receipt["commit"]:
                return {
                    "ok": True,
                    "operation": None,
                    "current": receipt["version"],
                    "update_available": False,
                }
            raise UpdateError("updates must use a newer release; data downgrades are not supported")
        services = update_services.discover(paths, receipt.get("viewer_service", ""))
        operation_id = uuid.uuid4().hex
        directory = _operation_dir(paths, operation_id)
        directory.mkdir(parents=True, mode=0o700)
        state: dict[str, Any] = {
            "id": operation_id,
            "status": "queued",
            "started_at": time.time(),
            "from_version": receipt["version"],
            "to_version": release.version,
            "manifest": release.to_dict(resolved=True),
            "source": str(release.source),
            "previous_install": receipt,
            "services": services,
            "origin": _origin(selected),
            "drain_timeout": drain_timeout,
            "startup_timeout": startup_timeout,
        }
        _save(paths, state)
        python = paths.runtime_dir / "releases" / receipt["release_id"] / "bin" / "python"
        try:
            update_services.launch(paths, operation_id, python)
        except Exception:
            _save(paths, state, "failed", error="could not launch independent updater")
            raise
        return {"ok": True, "operation": _public(state)}


def _drain(paths: Paths, state: dict[str, Any]) -> None:
    write_json(paths.maintenance, {"operation_id": state["id"]})
    if not state["services"]["daemon"]:
        return
    deadline = time.monotonic() + state["drain_timeout"]
    while time.monotonic() < deadline:
        running = daemon(paths)
        if (
            running.get("pid") == state["services"]["daemon_pid"]
            and running.get("paused")
            and running.get("active") == 0
        ):
            return
        time.sleep(0.25)
    raise UpdateError("active work did not finish before the drain deadline; update deferred")


def _check_snapshot_parents(paths: Paths) -> None:
    parents = [paths.home, paths.workspaces, paths.workspace("default")]
    if paths.workspaces.is_dir() and not paths.workspaces.is_symlink():
        parents.extend(p for p in paths.workspaces.iterdir() if valid_workspace_name(p.name))
    for parent in parents:
        if parent.is_symlink():
            raise UpdateError(f"{parent} must not be a symbolic link during a managed update")


def migration_plan(paths: Paths) -> list[str]:
    """Return the candidate's complete read-only snapshot contract for the old updater."""
    _check_snapshot_parents(paths)
    names = [
        *MIGRATION_PATHS,
        *(
            f"workspaces/{name}/jobs"
            for name in sorted({"default", *workspaces.list_workspaces(paths)})
        ),
        *(name.split("/")[0] for name in workspaces.BUNDLED_FILES),
        *(migrations.plan(paths) if not initialization.is_fresh_home(paths) else ()),
    ]
    return update_snapshot.plan(paths, names)


def _snapshot(paths: Paths, state: dict[str, Any]) -> None:
    _check_snapshot_parents(paths)
    names = state["migration_paths"] if "migration_paths" in state else migration_plan(paths)
    update_snapshot.capture(paths, _operation_dir(paths, state["id"]), names)


def _restore(paths: Paths, state: dict[str, Any]) -> None:
    _check_snapshot_parents(paths)
    update_snapshot.restore(paths, _operation_dir(paths, state["id"]))


def validate_home(paths: Paths) -> None:
    """Adoption selects compatible homes; managed apply owns structural conversion."""
    if problems := initialization.home_problems(paths):
        raise UpdateError(
            "Cannot adopt this release: "
            + "; ".join(problems)
            + ". Adopt a compatible release first, then run enso update apply."
        )


def prepare_home(paths: Paths) -> None:
    """Run in the candidate interpreter while all receivers and admissions are stopped."""
    if not paused(paths):
        raise UpdateError("release preparation requires an active update gate")
    fresh = initialization.is_fresh_home(paths)
    with service_receiver(paths):
        if fresh:
            write_json(paths.home / migrations.MARKER, {"revision": migrations.latest_revision()})
        else:
            migrations.apply(paths)
        if paths.config.exists():
            config = load_config(paths)
            db.initialize(paths)
            workspaces.reconcile_bundles(
                paths,
                config.defaults,
                workspace_agents={
                    name: settings.agent or config.defaults
                    for name, settings in config.workspaces.items()
                },
            )


def _finish(paths: Paths, state: dict[str, Any], status: str, **fields: Any) -> None:
    _save(paths, state, status, **fields)
    gate = read_json(paths.maintenance)
    if gate and gate.get("operation_id") != state["id"]:
        raise UpdateError("another operation owns the maintenance gate")
    paths.maintenance.unlink(missing_ok=True)
    sync_directory(paths.runtime_dir)


def _recover(paths: Paths, state: dict[str, Any]) -> None:
    try:
        with exclusive_access(paths, state["drain_timeout"]):
            _restore_runtime(paths, state)
    except UpdateError:
        if state["status"] == "rolled_back":
            raise
        _save(
            paths,
            state,
            "recovery_failed",
            error="recovery could not acquire exclusive home access",
        )


def _restore_runtime(paths: Paths, state: dict[str, Any]) -> None:
    previous = state["previous_install"]
    previous_dir = paths.runtime_dir / "releases" / previous["release_id"]
    try:
        update_services.stop(paths, state["services"])
        if state.get("snapshot_complete"):
            _restore(paths, state)
        _select(paths, previous_dir)
        write_json(_install_path(paths), previous)
        update_services.start(paths, state["services"], previous_dir / "bin" / "enso")
        update_services.healthy(
            paths, state["services"], previous["version"], state["startup_timeout"]
        )
        _finish(paths, state, "rolled_back")
    except Exception as exc:
        if state["status"] == "rolled_back":
            raise
        # Keep the gate and both state copies. Removing it here could admit writes
        # into an unverified or partially recovered database.
        _save(paths, state, "recovery_failed", error=f"recovery failed: {type(exc).__name__}")


def run_update(paths: Paths, operation_id: str) -> None:
    """Independent helper entry; a restarted helper recovers a partially committed update."""
    with lock(paths, "worker"):
        state = read_json(_operation_dir(paths, operation_id) / "operation.json")
        if state:
            _validate_operation(state)
        if not state or _operation(paths).get("id") != operation_id:
            raise UpdateError("update operation is missing or has been superseded")
        if state["status"] in TERMINAL - {"recovery_failed"}:
            _finish(paths, state, state["status"])
            _cleanup(paths, state)
            _notify_outcome(paths, state)
            return
        if state["status"] == "draining":
            _finish(
                paths,
                state,
                "deferred",
                error="updater was interrupted before stopping; active work was preserved",
            )
            _cleanup(paths, state)
            _notify_outcome(paths, state)
            return
        if state["status"] not in ("queued", "staging"):
            _recover(paths, state)
            _cleanup(paths, state)
            _notify_outcome(paths, state)
            return
        _perform(paths, state)
        _cleanup(paths, state)
        _notify_outcome(paths, state)


def _perform(paths: Paths, state: dict[str, Any]) -> None:
    try:
        _save(paths, state, "staging")
        release = releases.parse_release(state["manifest"], state["source"])
        receipt = state["previous_install"]
        staging = paths.runtime_dir / "releases" / release.release_id
        if state.get("staging_owned") == release.release_id:
            if staging.is_symlink():
                raise UpdateError("partial release must not be a symbolic link")
            if staging.exists() and not (staging / "installation_receipt.json").exists():
                if (paths.runtime_dir / "current").resolve() == staging.resolve():
                    raise UpdateError("cannot remove the active release during staging recovery")
                shutil.rmtree(staging)
        elif not staging.exists():
            _save(paths, state, staging_owned=release.release_id)
        target = _candidate(paths, release, receipt)
        _save(paths, state, "draining")
        _drain(paths, state)
        with exclusive_access(paths, state["drain_timeout"]):
            _commit_release(paths, state, release, target)
    except Exception as exc:
        if state["status"] == "succeeded":
            raise
        state["error"] = str(exc) if isinstance(exc, UpdateError) else type(exc).__name__
        if state["status"] in ("queued", "staging"):
            _finish(paths, state, "failed")
        elif state["status"] == "draining":
            _finish(paths, state, "deferred")
        else:
            _save(paths, state)
            _recover(paths, state)


def _candidate_plan(paths: Paths, binary: Path, env: dict[str, str]) -> list[str]:
    declared = update_services.run_command(
        [str(binary), "update", "_migration-plan"],
        cwd=paths.home,
        env=env,
    )
    try:
        names = update_snapshot.plan(paths, json.loads(declared))
    except (ValueError, RecursionError) as exc:
        raise UpdateError("the candidate returned an invalid migration plan") from exc
    return names


def _commit_release(
    paths: Paths,
    state: dict[str, Any],
    release: releases.Release,
    target: Path,
) -> None:
    _save(paths, state, "stopping")
    update_services.stop(paths, state["services"])
    binary = target / "bin" / "enso"
    env = {**os.environ, "ENSO_HOME": str(paths.home), "ENSO_UPDATE_INTERNAL": state["id"]}
    names = _candidate_plan(paths, binary, env)
    _save(paths, state, "backing_up", migration_paths=names)
    _snapshot(paths, state)
    _save(paths, state, "switching", snapshot_complete=True)
    _select(paths, target)
    update_services.run_command(
        [str(binary), "update", "_prepare-home"],
        cwd=paths.home,
        env=env,
    )
    _save(paths, state, "starting")
    update_services.start(paths, state["services"], binary)
    update_services.healthy(paths, state["services"], release.version, state["startup_timeout"])
    write_json(
        _install_path(paths),
        {
            **state["previous_install"],
            "version": release.version,
            "commit": release.commit,
            "release_id": release.release_id,
        },
    )
    _finish(paths, state, "succeeded")


def _remove_owned(path: Path) -> None:
    if path.is_symlink():
        raise UpdateError("cleanup refused a symbolic link in managed runtime storage")
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _prune_interpreters(paths: Paths, releases_root: Path, keep: set[str]) -> None:
    # Keep Python interpreters still referenced by retained environments.
    python_root = paths.runtime_dir / "python"
    if python_root.is_symlink():
        raise UpdateError("Python storage must not be a symbolic link")
    interpreters = [(releases_root / name / "bin/python").resolve() for name in keep]
    if python_root.is_dir():
        for interpreter in python_root.iterdir():
            if re.fullmatch(r"cpython-\d+\.\d+\.\d+-.+", interpreter.name) and not any(
                binary.is_relative_to(interpreter) for binary in interpreters
            ):
                _remove_owned(interpreter)


def _cleanup(paths: Paths, state: dict[str, Any], *, helper_finished: bool = False) -> None:
    """Discard rollback data only after a durable terminal result and an open gate.

    The old helper runs from the previous environment, so successful updates retain
    it until the next apply confirms that helper exited. Cleanup never rolls back success.
    """
    if state["status"] not in TERMINAL - {"recovery_failed"} or paused(paths):
        return
    try:
        directory = _operation_dir(paths, state["id"])
        _remove_owned(directory / "backup")
        for failed in directory.glob("failed-state-*"):
            if re.fullmatch(r"failed-state-[0-9a-f]{8}", failed.name):
                _remove_owned(failed)
        active = installed(paths)["release_id"]
        keep = {active}
        if not helper_finished:
            keep.add(state["previous_install"]["release_id"])
        releases_root = paths.runtime_dir / "releases"
        if releases_root.is_symlink():
            raise UpdateError("release storage must not be a symbolic link")
        for release_dir in releases_root.iterdir():
            if re.fullmatch(r"\d+\.\d+\.\d+-[0-9a-f]{12}", release_dir.name):
                if release_dir.name not in keep:
                    _remove_owned(release_dir)
            elif re.fullmatch(r"\.download-[a-z0-9_]+", release_dir.name):
                _remove_owned(release_dir)
        _prune_interpreters(paths, releases_root, keep)
        uv = paths.runtime_dir / "tools/uv"
        cache = paths.runtime_dir / "cache/uv"
        if uv.is_file() and cache.is_dir():
            if uv.is_symlink() or cache.is_symlink() or cache.parent.is_symlink():
                raise UpdateError("managed cache tools must not be symbolic links")
            update_services.run_command(
                [str(uv), "--no-config", "cache", "clean", "--cache-dir", str(cache)],
                cwd=paths.home,
            )
        _prune_operations(paths, state)
        if state.pop("cleanup_error", None):
            _save(paths, state)
    except Exception:
        _save(paths, state, cleanup_error="cleanup incomplete; the next update will retry")


def _prune_operations(paths: Paths, state: dict[str, Any]) -> None:
    """Retain only the latest operation, after confirming older helpers have exited."""
    for directory in _operation_dir(paths, state["id"]).parent.iterdir():
        if directory.name == state["id"] or not re.fullmatch(r"[0-9a-f]{32}", directory.name):
            continue
        directory = _operation_dir(paths, directory.name)
        old = read_json(directory / "operation.json")
        _validate_operation(old)
        if old["id"] != directory.name:
            raise UpdateError("operation directory does not match its saved identity")
        if old["status"] in TERMINAL - {"recovery_failed"}:
            if not update_services.cleanup_finished(paths, old["id"]):
                raise UpdateError("an older update helper has not finished")
            _remove_owned(directory)


def recover(paths: Paths) -> dict[str, Any]:
    """Retry recovery after a helper/host interruption, never overwrite a running operation."""
    with lock(paths), lock(paths, "worker"):
        state = _operation(paths)
        if not state or (state["status"] in TERMINAL and not paused(paths)):
            raise UpdateError("there is no interrupted update to recover")
        # Persist the request before starting a helper. A repeat follows the same
        # operation and snapshot, rather than taking a new snapshot of failed state.
        python = (
            paths.runtime_dir
            / "releases"
            / state["previous_install"]["release_id"]
            / "bin"
            / "python"
        )
        update_services.launch(paths, state["id"], python)
        return {"ok": True, "operation": _public(state)}


def _send(paths: Paths, text: str, origin: dict[str, str]) -> None:
    receipt = installed(paths)
    command = (
        [str(paths.runtime_dir / "releases" / receipt["release_id"] / "bin" / "enso")]
        if receipt
        else [sys.executable, "-m", "enso.cli"]
    )
    command.extend(["message", "send", text, "--json"])
    env = messages.without_identity(os.environ)
    env["ENSO_WORKSPACE"] = origin["workspace"]
    if origin.get("transport") and origin.get("channel"):
        env.update(
            {
                "ENSO_ORIGIN_TRANSPORT": origin["transport"],
                "ENSO_ORIGIN_CHANNEL": origin["channel"],
                "ENSO_ORIGIN_THREAD_TS": origin.get("thread", ""),
            }
        )
    update_services.run_command(
        command, cwd=paths.home, env={**env, "ENSO_HOME": str(paths.home)}, timeout=30
    )


def _notify_outcome(paths: Paths, state: dict[str, Any]) -> None:
    if not paths.config.exists() or state.get("outcome_notified") or paused(paths):
        return
    if state["status"] == "succeeded":
        text = f"Enso was upgraded to {state['to_version']} and is ready."
    elif state["status"] == "rolled_back":
        text = (
            f"Enso could not upgrade to {state['to_version']}; "
            f"restored {state['from_version']} and its data."
        )
    else:
        text = f"Enso update to {state['to_version']} {state['status']}. {state.get('error', '')}"
    try:
        _send(paths, text, state["origin"])
        _save(paths, state, outcome_notified=True)
    except Exception:
        _save(
            paths,
            state,
            notification_error="could not deliver update outcome; inspect update status",
        )


def notify_available(paths: Paths, result: dict[str, Any], *, workspace: str) -> bool:
    """Announce each release or unmanaged state once, recording successful delivery."""
    if result["managed"] and not result["update_available"]:
        return False
    with lock(paths, "notification"):
        receipt_path = paths.runtime_dir / "notification.json"
        receipt = read_json(receipt_path)
        notice = {"version": result["available"], "managed": result["managed"]}
        if all(receipt.get(key) == value for key, value in notice.items()):
            return False
        text = check_message(result)
        if result.get("release_notes_url"):
            text += f" Release notes: {result['release_notes_url']}"
        _send(paths, text, {"workspace": workspace})
        write_json(receipt_path, {**notice, "sent_at": time.time()})
        return True
