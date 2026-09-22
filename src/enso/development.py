"""Explicit, local-only checkout refreshes and migrations; never release publication.

The repository scripts run outside Enso's own jobs. Home content changes only through
an explicitly requested migration; ordinary refreshes only change runtime control files
and the CLI launcher. Failed or interrupted operations keep admission closed until recovery.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shlex
import shutil
import stat
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from . import (
    initialization,
    maintenance,
    migrations,
    service,
    update_services,
    update_snapshot,
    updates,
)
from .config import ConfigError, Paths, load_config
from .connection_setup import service_receiver
from .maintenance import UpdateError, read_json, write_bytes, write_json

STATE = "development.json"


def state_path(paths: Paths) -> Path:
    return paths.runtime_dir / STATE


def migration_preview(paths: Paths) -> dict[str, Any]:
    """Read the same ordered registry as a release, without writing a baseline marker."""
    steps = migrations.pending(paths)
    names = [migrations.MARKER, *migrations.plan(paths)]
    if "enso.db" in names:
        names.extend(("enso.db-wal", "enso.db-shm", "enso.db-journal"))
    return {
        "home": str(paths.home),
        "revision": migrations.read_revision(paths),
        "latest": migrations.latest_revision(),
        "pending": [{"revision": step.revision, "name": step.name} for step in steps],
        "paths": update_snapshot.plan(paths, names) if steps else [],
    }


def _directory(paths: Paths, ident: str) -> Path:
    if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{32}", ident):
        raise UpdateError("invalid development operation identity")
    root = paths.runtime_dir / "development"
    directory = root / ident
    if root.is_symlink() or directory.is_symlink():
        raise UpdateError("development recovery paths must not be symbolic links")
    return directory


def _save(paths: Paths, operation: dict[str, Any], phase: str) -> None:
    operation["phase"] = phase
    write_json(_directory(paths, operation["id"]) / "operation.json", operation)
    write_json(state_path(paths), {"id": operation["id"], "phase": phase})


def _operation(paths: Paths) -> dict[str, Any]:
    state = read_json(state_path(paths))
    if not state:
        return {}
    operation = read_json(_directory(paths, state.get("id", "")) / "operation.json")
    services = operation.get("services")
    if (
        operation.get("id") != state["id"]
        or operation.get("phase")
        not in {
            "draining",
            "stopping",
            "switching",
            "migrating",
            "starting",
            "ready",
            "failed",
            "restored",
        }
        or not isinstance(services, dict)
        or any(type(services.get(key)) is not bool for key in ("daemon", "viewer"))
        or not isinstance(operation.get("previous_install"), dict)
    ):
        raise UpdateError("development recovery record is incomplete")
    return operation


def _gate(paths: Paths, operation: dict[str, Any], phase: str) -> None:
    write_json(
        paths.maintenance,
        {"kind": "development", "operation_id": operation["id"], "phase": phase},
    )


def _ungate(paths: Paths, operation: dict[str, Any]) -> None:
    gate = read_json(paths.maintenance)
    if gate and gate.get("operation_id") != operation["id"]:
        raise UpdateError("another operation owns the maintenance gate")
    paths.maintenance.unlink(missing_ok=True)
    maintenance.sync_directory(paths.runtime_dir)


def _validate_home(paths: Paths) -> None:
    if problems := initialization.home_problems(paths):
        raise UpdateError("; ".join(problems))
    if paths.config.exists():
        load_config(paths)


def _checkout(repository: Path, *, clean: bool) -> tuple[str, str]:
    branch = update_services.run_command(["git", "branch", "--show-current"], cwd=repository)
    if branch != "develop":
        raise UpdateError("local refreshes must run from develop")
    if clean and update_services.run_command(["git", "status", "--porcelain"], cwd=repository):
        raise UpdateError("commit the tested checkout before refreshing the live instance")
    commit = update_services.run_command(["git", "rev-parse", "HEAD"], cwd=repository)
    binary = repository / ".venv/bin/enso"
    version = update_services.run_command([str(binary), "--version"], cwd=repository)
    return commit, version.removeprefix("enso ")


def _launcher(paths: Paths, repository: Path, previous: dict[str, Any]) -> Path:
    receipt = updates.installed(paths)
    if receipt:
        launcher = Path(receipt["bin_dir"]) / "enso"
    elif previous.get("launcher"):
        launcher = Path(previous["launcher"])
        if previous.get("repository") != str(repository):
            raise UpdateError("this home is connected to a different development checkout")
    else:
        raise UpdateError("first refresh requires an existing managed installation")
    if launcher.is_symlink() or not launcher.is_file():
        raise UpdateError("the Enso launcher must be a regular file")
    return launcher


def _check_units(services: dict[str, Any], launcher: Path) -> None:
    platform = service.platform_name()
    definitions = [service.AGENT] if services["daemon"] else []
    if services["viewer"]:
        if not services.get("viewer_service"):
            raise UpdateError("install the viewer user service before refreshing local code")
        definitions.append(service.VIEWER.named(services["viewer_service"]))
    for definition in definitions:
        path = service.unit_path(platform, definition=definition)
        if platform == "launchd":
            arguments = plistlib.loads(path.read_bytes()).get("ProgramArguments")
        else:
            from .web.service import _systemd_values

            arguments, _env = _systemd_values(path.read_text())
        if arguments != [str(launcher), *definition.arguments]:
            raise UpdateError(f"{path} must run the stable Enso launcher before a dev refresh")


def _drain(paths: Paths, operation: dict[str, Any], timeout: float) -> None:
    # The first switch still has the old release's CLI admission rules. Require it
    # to be idle before putting up a gate it cannot recognize as a development drain.
    if operation["previous_install"] and maintenance.daemon(paths).get("active", 0):
        raise UpdateError("wait for active work to finish before the first development switch")
    _gate(paths, operation, "draining")
    if not operation["services"]["daemon"] or operation.get("resuming"):
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        running = maintenance.daemon(paths)
        if (
            running.get("pid") == operation["services"]["daemon_pid"]
            and running.get("paused")
            and running.get("active") == 0
        ):
            return
        time.sleep(0.25)
    raise UpdateError("active work did not finish; development refresh deferred")


def _restore(paths: Paths, operation: dict[str, Any]) -> None:
    directory = _directory(paths, operation["id"])
    update_services.stop(paths, operation["services"])
    with service_receiver(paths):
        if (directory / "snapshot.json").exists():
            update_snapshot.restore(paths, directory)
        if (directory / "launcher").exists():
            write_bytes(
                Path(operation["launcher"]),
                (directory / "launcher").read_bytes(),
                operation["launcher_mode"],
            )
        if operation["previous_install"]:
            write_json(paths.runtime_dir / "install.json", operation["previous_install"])
    _save(paths, operation, "restored")
    _gate(paths, operation, "restored")


def recover(paths: Paths) -> dict[str, Any]:
    """Restore only an uncommitted operation; never roll back admitted newer work."""
    with maintenance.lock(paths), maintenance.lock(paths, "worker"):
        operation = _operation(paths)
        gate = read_json(paths.maintenance)
        if operation and operation["phase"] == "draining" and not gate:
            _save(paths, operation, "ready")
            return {"phase": "ready", "home": str(paths.home)}
        if not operation or gate.get("operation_id") != operation["id"]:
            raise UpdateError("no development operation owns the current maintenance gate")
        if operation["phase"] == "ready":
            _ungate(paths, operation)
        else:
            with maintenance.exclusive_access(paths, 30):
                _restore(paths, operation)
        return {"phase": operation["phase"], "home": str(paths.home)}


def _switch(paths: Paths, repository: Path, operation: dict[str, Any]) -> None:
    directory = _directory(paths, operation["id"])
    launcher = Path(operation["launcher"])
    operation["launcher_mode"] = stat.S_IMODE(launcher.stat().st_mode)
    _save(paths, operation, "switching")
    write_bytes(directory / "launcher", launcher.read_bytes())
    content = (
        "#!/bin/sh\n# Enso development launcher\n"
        f"export ENSO_HOME={shlex.quote(str(paths.home))}\n"
        f'exec {shlex.quote(str(repository / ".venv/bin/enso"))} "$@"\n'
    )
    write_bytes(launcher, content.encode(), 0o755)
    # The operation keeps the original receipt and release untouched for recovery.
    # Removing the active receipt makes development explicitly unmanaged.
    (paths.runtime_dir / "install.json").unlink(missing_ok=True)


def _previous(paths: Paths) -> dict[str, Any]:
    previous = _operation(paths)
    gate = read_json(paths.maintenance)
    if previous and previous["phase"] not in ("ready", "restored"):
        raise UpdateError(
            "development maintenance was interrupted; run scripts/dev-refresh --recover"
        )
    if gate and (
        not previous
        or gate.get("kind") != "development"
        or gate.get("operation_id") != previous["id"]
    ):
        raise UpdateError("another maintenance operation is pending")
    pending_update = read_json(paths.update_state)
    if pending_update and pending_update.get("status") not in updates.TERMINAL:
        raise UpdateError("finish the managed update before development maintenance")
    return previous


def _begin(
    paths: Paths, repository: Path, migrate: bool, commit: str, version: str
) -> dict[str, Any]:
    previous = _previous(paths)
    resuming = previous.get("phase") == "restored"
    receipt = updates.installed(paths)
    services = previous["services"] if resuming else update_services.discover(paths)
    if migrate and (services["daemon"] or services["viewer"]):
        if receipt or not previous.get("launcher"):
            raise UpdateError(
                "switch to development first, or test migrations in a stopped scratch home"
            )
        _checkout(repository, clean=True)
    launcher = _launcher(paths, repository, previous) if not migrate else None
    if launcher:
        _check_units(services, launcher)
    operation: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "repository": str(repository),
        "commit": commit,
        "version": version,
        "services": services,
        "previous_install": receipt,
        "launcher": str(launcher) if launcher else previous.get("launcher"),
        "resuming": resuming,
        "migrate": migrate,
    }
    _directory(paths, operation["id"]).mkdir(parents=True, mode=0o700)
    _save(paths, operation, "draining")
    return operation


def run(
    paths: Paths,
    repository: Path,
    *,
    migrate: bool = False,
    drain_timeout: float = 300,
    startup_timeout: float = 60,
) -> dict[str, Any]:
    """Stop admitted writers, optionally migrate, then verify before reopening work."""
    if any(os.environ.get(key) for key in ("ENSO_RUN_ID", "ENSO_TASK", "ENSO_ORIGIN_TRANSPORT")):
        raise UpdateError(
            "run development maintenance from an external terminal, outside Enso jobs"
        )
    preview = migration_preview(paths)
    if preview["pending"] and not migrate:
        raise UpdateError(
            "home migrations are pending; preview scripts/dev-migrate, then use --apply"
        )
    if migrate and not preview["pending"]:
        if maintenance.paused(paths):
            raise UpdateError("maintenance is paused; recover the interrupted operation first")
        return {"ok": True, "message": "No migrations pending.", **preview}
    if not migrate:
        _validate_home(paths)
    commit, version = _checkout(repository, clean=not migrate)
    with maintenance.lock(paths), maintenance.lock(paths, "worker"):
        operation = _begin(paths, repository, migrate, commit, version)
        services = operation["services"]
        directory = _directory(paths, operation["id"])
        try:
            _drain(paths, operation, drain_timeout)
        except Exception:
            _save(paths, operation, "ready")
            _ungate(paths, operation)
            raise
        _gate(paths, operation, "stopping")
        try:
            with maintenance.exclusive_access(paths, drain_timeout):
                _save(paths, operation, "stopping")
                update_services.stop(paths, services)
                if not migrate:
                    update_services.run_command(
                        ["uv", "sync", "--all-extras", "--locked"], cwd=repository, timeout=300
                    )
                    version = update_services.run_command(
                        [str(repository / ".venv/bin/enso"), "--version"], cwd=repository
                    ).removeprefix("enso ")
                    operation["version"] = version
                with service_receiver(paths):
                    if migrate:
                        names = migration_preview(paths)["paths"]
                        update_snapshot.capture(paths, directory, names)
                        _save(paths, operation, "migrating")
                        migrations.apply(paths)
                    _validate_home(paths)
                    if not migrate:
                        _switch(paths, repository, operation)
                _save(paths, operation, "starting")
                binary = repository / ".venv/bin/enso"
                update_services.start(paths, services, binary)
                update_services.healthy(paths, services, version, startup_timeout)
                _save(paths, operation, "ready")
                _ungate(paths, operation)
        except Exception as exc:
            if operation["phase"] == "ready":
                raise  # A committed refresh can never restore pre-admission data.
            # Keep the durable gate and journal. Recovery re-stops any candidate
            # that started, then restores the declared snapshot before another try.
            operation["error"] = str(exc) if isinstance(exc, UpdateError) else type(exc).__name__
            _save(paths, operation, "failed")
            raise UpdateError(
                f"development maintenance failed; work remains paused. "
                f"Run scripts/dev-refresh --recover; details: {directory}"
            ) from None
        if (directory / "backup").exists():
            shutil.rmtree(directory / "backup")
        return {
            "ok": True,
            "commit": commit,
            "version": version,
            "home": str(paths.home),
            "operation": str(directory),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("refresh", "migrate"))
    parser.add_argument("--home", type=Path, help="Defaults to ENSO_HOME or ~/.enso.")
    parser.add_argument("--apply", action="store_true", help="Apply the previewed migration steps.")
    parser.add_argument(
        "--recover",
        action="store_true",
        help="Restore an interrupted operation; keep services stopped.",
    )
    args = parser.parse_args()
    paths = Paths(args.home.expanduser().resolve()) if args.home else Paths.from_env()
    repository = Path(__file__).resolve().parents[2]
    try:
        if args.recover:
            result = recover(paths)
        elif args.command == "migrate" and not args.apply:
            result = migration_preview(paths)
        else:
            result = run(paths, repository, migrate=args.command == "migrate")
        print(json.dumps(result, indent=2))
        return 0
    except (UpdateError, ConfigError, OSError, ValueError, service.ServiceError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
