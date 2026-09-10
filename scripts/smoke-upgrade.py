#!/usr/bin/env python3
"""Run opt-in upgrade acceptance tests inside an isolated disposable Docker container."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path


def command(args: list[str], *, timeout: int, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=Path(__file__).resolve().parents[1],
        timeout=timeout,
        check=check,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Save the JSON test report at this path.")
    parser.add_argument("--keep", action="store_true", help="Keep this run's container and image.")
    parser.add_argument(
        "--systemd",
        action="store_true",
        help="Use actual user systemd in a privileged disposable container with private cgroups.",
    )
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    identity = "enso-upgrade-smoke-" + uuid.uuid4().hex[:12]
    report = args.output or Path(tempfile.gettempdir()) / f"{identity}.json"
    command(["docker", "version", "--format", "{{.Server.Version}}"], timeout=30)
    result = None
    try:
        with tempfile.TemporaryDirectory(prefix=identity + "-") as scratch:
            context = Path(scratch)
            # An explicit allowlist keeps .git, homes, credentials and local artifacts out.
            for name in ("pyproject.toml", "uv.lock", "README.md"):
                shutil.copy2(source / name, context / name)
            shutil.copytree(
                source / "src", context / "src", ignore=shutil.ignore_patterns("__pycache__")
            )
            shutil.copytree(
                source / "scripts/upgrade-smoke",
                context / "smoke",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            (context / "scripts").mkdir()
            for name in ("build-release.py", "installer-header.sh", "install-release.py"):
                shutil.copy2(source / "scripts" / name, context / "scripts" / name)
            shutil.copy2(source / "tests/fixtures/fake_claude.py", context / "smoke/fake_claude.py")
            command(
                [
                    "docker",
                    "build",
                    "--label",
                    "io.enso.upgrade-smoke=true",
                    "-t",
                    identity,
                    "-f",
                    str(context / "smoke/Dockerfile"),
                    str(context),
                ],
                timeout=900,
            )
            if args.systemd:
                command(
                    [
                        "docker",
                        "build",
                        "--label",
                        "io.enso.upgrade-smoke=true",
                        "--build-arg",
                        f"BASE_IMAGE={identity}",
                        "-t",
                        identity + "-systemd",
                        "-f",
                        str(context / "smoke/Dockerfile.systemd"),
                        str(context),
                    ],
                    timeout=900,
                )
        if args.systemd:
            command(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--name",
                    identity,
                    "--label",
                    "io.enso.upgrade-smoke=true",
                    "--network",
                    "none",
                    "--privileged",
                    "--cgroupns",
                    "private",
                    "--tmpfs",
                    "/run",
                    "--tmpfs",
                    "/run/lock",
                    "--tmpfs",
                    "/tmp",
                    "--pids-limit",
                    "256",
                    "--memory",
                    "2g",
                    identity + "-systemd",
                ],
                timeout=30,
            )
            command(
                ["docker", "exec", identity, "systemctl", "start", "user@1000.service"], timeout=60
            )
            result = command(
                [
                    "docker",
                    "exec",
                    "--user",
                    "1000:1000",
                    "--env",
                    "HOME=/home/enso",
                    "--env",
                    "XDG_RUNTIME_DIR=/run/user/1000",
                    "--env",
                    "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
                    identity,
                    "python",
                    "/smoke/run-systemd.py",
                ],
                timeout=600,
                check=False,
            )
        else:
            result = command(
                [
                    "docker",
                    "run",
                    "--name",
                    identity,
                    "--label",
                    "io.enso.upgrade-smoke=true",
                    "--network",
                    "none",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--pids-limit",
                    "256",
                    "--memory",
                    "2g",
                    identity,
                ],
                timeout=1200,
                check=False,
            )
        report.parent.mkdir(parents=True, exist_ok=True)
        container_report = (
            "/home/enso/upgrade-report.json" if args.systemd else "/tmp/upgrade-report.json"
        )
        copied = command(
            ["docker", "cp", f"{identity}:{container_report}", str(report)],
            timeout=30,
            check=False,
        )
        if copied.returncode == 0:
            print(json.dumps({"report": str(report.resolve()), "exit_code": result.returncode}))
        elif result.returncode == 0:
            raise SystemExit("Tests passed, but the report could not be copied from the container.")
    finally:
        if args.keep:
            print(f"Preserved container and image: {identity}")
            if args.systemd:
                print(f"Preserved systemd image: {identity}-systemd")
        else:
            command(["docker", "rm", "--force", identity], timeout=30, check=False)
            if args.systemd:
                command(["docker", "image", "rm", identity + "-systemd"], timeout=60, check=False)
            command(["docker", "image", "rm", identity], timeout=60, check=False)
    raise SystemExit(result.returncode if result else 1)


if __name__ == "__main__":
    main()
