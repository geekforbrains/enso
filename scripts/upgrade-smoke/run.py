"""Opt-in installed-wheel acceptance tests; invoked only by the isolated Docker runner."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import runpy
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path("/tmp/enso-upgrade-smoke")
SOURCE = Path("/source")
FIXTURES = Path("/smoke")
REPORT = Path("/tmp/upgrade-report.json")
WORKFLOW_PATHS = (
    "smoke-legacy/workflows",
    "smoke-core/workflows",
    "smoke-core/library/workflows",
)


def run(command, *, env=None, check=True, timeout=120, cwd=ROOT):
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            command,
            env=env,
            cwd=cwd,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            process.wait(timeout=timeout)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
        outputs = []
        for stream in (stdout, stderr):
            stream.seek(max(stream.tell() - 65536, 0))
            outputs.append(stream.read(65536).decode("utf-8", errors="replace"))
        result = subprocess.CompletedProcess(command, process.returncode, *outputs)
    if check and result.returncode:
        raise AssertionError(
            f"Command failed ({result.returncode}): {command}\n"
            f"{result.stdout[-4000:]}\n{result.stderr[-4000:]}"
        )
    return result


def wait_for(predicate, *, timeout=30, description="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {description}")


def read_json(path):
    try:
        return json.loads(path.read_text())
    except OSError, ValueError:
        return {}


def socket_call(path, request, timeout=30):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        connection.sendall(json.dumps(request).encode() + b"\n")
        return json.loads(connection.makefile("rb").readline())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inject_schema(original: str, revision: int) -> str:
    """Give each synthetic wheel the schema a fresh install of that release would create."""
    version = re.search(r"^SCHEMA_VERSION = (\d+)$", original, re.MULTILINE)
    if version is None or '_SCHEMA = """' not in original:
        raise AssertionError("Smoke fixture requires SCHEMA_VERSION and _SCHEMA")
    current = int(version.group(1))
    columns = (
        "name TEXT, completed INTEGER NOT NULL"
        if revision == 0
        else (
            "name TEXT, state TEXT NOT NULL DEFAULT 'pending', priority INTEGER NOT NULL DEFAULT 0"
        )
    )
    if revision >= 2:
        columns += ", reviewed INTEGER NOT NULL DEFAULT 0"
    updated = original.replace(version.group(0), f"SCHEMA_VERSION = {current + revision}", 1)
    return updated.replace(
        '_SCHEMA = """', f'_SCHEMA = """\nCREATE TABLE smoke_feature ({columns});', 1
    )


def inject_migrations(original: str, schema_version: int, revision: int, *, fail=False) -> str:
    """Register real, cumulative DB and filesystem migrations in disposable source copies."""
    fixture = f"""

def _smoke_move(paths, source, destination, revision):
    import json
    old, new = paths.home / source, paths.home / destination
    if new.exists():
        raise RuntimeError("migration destination already exists")
    new.parent.mkdir(parents=True, exist_ok=True)
    old.rename(new)
    document = new / "example.json"
    value = json.loads(document.read_text())
    value["format"] = revision
    document.write_text(json.dumps(value, indent=2) + "\\n")


def _smoke_first(paths):
    import sqlite3
    with sqlite3.connect(paths.db) as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("ALTER TABLE smoke_feature ADD COLUMN state TEXT NOT NULL DEFAULT 'pending'")
        con.execute("ALTER TABLE smoke_feature ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        con.execute("UPDATE smoke_feature SET state = "
                    "CASE completed WHEN 1 THEN 'done' ELSE 'pending' END")
        con.execute("ALTER TABLE smoke_feature DROP COLUMN completed")
        con.execute("PRAGMA user_version = {schema_version + 1}")
    _smoke_move(paths, "smoke-legacy/workflows", "smoke-core/workflows", 1)
    (paths.home / "smoke-legacy").rmdir()
    if {fail and revision == 1!r}:
        raise RuntimeError("synthetic migration failure after database and file writes")


def _smoke_second(paths):
    import sqlite3
    with sqlite3.connect(paths.db) as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("ALTER TABLE smoke_feature ADD COLUMN reviewed INTEGER NOT NULL DEFAULT 0")
        con.execute("PRAGMA user_version = {schema_version + 2}")
    _smoke_move(paths, "smoke-core/workflows", "smoke-core/library/workflows", 2)
    if {fail and revision == 2!r}:
        raise RuntimeError("synthetic migration failure after database and file writes")


MIGRATIONS += (
    Migration(1, "convert feature and move workflows", lambda paths: (
        "enso.db", "smoke-legacy", "smoke-core/workflows"
    ), _smoke_first),
    Migration(2, "add review default and organize workflows", lambda paths: (
        "enso.db", "smoke-core/workflows", "smoke-core/library/workflows"
    ), _smoke_second),
)[:{revision}]
"""
    return original + fixture


def build_release(
    name, version, *, revision=0, migration_failure=False, startup_failure=False, dependency=False
):
    source = ROOT / "sources" / name
    shutil.copytree(SOURCE, source, ignore=shutil.ignore_patterns("__pycache__"))
    project = source / "pyproject.toml"
    text = re.sub(
        r'^version = "[^"]+"',
        f'version = "{version}"',
        project.read_text(),
        count=1,
        flags=re.MULTILINE,
    )
    if dependency:
        text = text.replace(
            "dependencies = [",
            'dependencies = [\n    "enso-upgrade-smoke-missing-dependency==0.0.1",',
            1,
        )
    project.write_text(text)
    transport = (FIXTURES / "fake_transport.py").read_text()
    if startup_failure:
        transport = transport.replace(
            '        socket_path = self.paths.home / "smoke-transport.sock"',
            '        raise RuntimeError("synthetic startup failure")\n'
            '        socket_path = self.paths.home / "smoke-transport.sock"',
        )
    (source / "src/enso/transports/slack.py").write_text(transport)
    database = source / "src/enso/db.py"
    schema_version = int(re.search(r"^SCHEMA_VERSION = (\d+)$", database.read_text(), re.M)[1])
    database.write_text(inject_schema(database.read_text(), revision))
    migrations = source / "src/enso/migrations.py"
    migrations.write_text(
        inject_migrations(migrations.read_text(), schema_version, revision, fail=migration_failure)
    )
    workflow_file = f"{WORKFLOW_PATHS[revision]}/example.json"
    bundled = source / "src/enso/bundled" / workflow_file
    bundled.parent.mkdir(parents=True)
    bundled.write_text(json.dumps({"name": "example", "format": revision}, indent=2) + "\n")
    workspaces = source / "src/enso/workspaces.py"
    workspaces.write_text(workspaces.read_text() + f"\nBUNDLED_FILES += ({workflow_file!r},)\n")
    output = ROOT / "feed" / name
    output.mkdir(parents=True)
    run(["uv", "build", "--wheel", "--project", str(source), "--out-dir", str(output)], timeout=120)
    wheel = next(output.glob("*.whl"))
    lock = tomllib.loads((SOURCE / "uv.lock").read_text())
    constraints = output / "constraints.txt"
    constraints.write_text(
        "".join(
            sorted(
                f"{package['name']}=={package['version']}\n"
                for package in lock["package"]
                if package.get("source", {}).get("registry")
            )
        )
    )
    manifest = {
        "schema_version": 1,
        "version": version,
        "commit": "a" * 40,
        "requires_python": ">=3.14",
        "wheel": {"url": wheel.name, "sha256": digest(wheel)},
        "constraints": {"url": constraints.name, "sha256": digest(constraints)},
    }
    (output / "release.json").write_text(json.dumps(manifest))
    builder = runpy.run_path(str(SOURCE / "scripts/build-release.py"))
    (output / "install.sh").write_text(builder["build_installer"]())
    return output


class Feed(http.server.ThreadingHTTPServer):
    def __init__(self):
        self.blocked = threading.Event()
        self.release = threading.Event()
        self.hold = False
        self.refill_cache = None
        super().__init__(("127.0.0.1", 0), FeedHandler)

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server_port}"


class FeedHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT / "feed", **kwargs)

    def do_GET(self):
        if self.path.endswith(".whl") and self.server.refill_cache:
            refill = self.server.refill_cache
            self.server.refill_cache = None
            refill()
        if self.server.hold and self.path.endswith(".whl"):
            self.server.blocked.set()
            if not self.server.release.wait(90):
                self.send_error(504)
                return
        try:
            return super().do_GET()
        except BrokenPipeError, ConnectionResetError:
            # Interruption tests deliberately kill the download client.
            return None

    def log_message(self, *_args):
        pass


class Instance:
    def __init__(self, name, feed, *, real_systemd=False, release="base", revision=0):
        self.real_systemd = real_systemd
        self.root = Path.home() if real_systemd else ROOT / "instances" / name
        self.root.mkdir(parents=True, exist_ok=real_systemd)
        self.home = self.root / "enso"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.feed = feed
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("ENSO_")}
        self.env.update(
            HOME=str(self.root),
            ENSO_HOME=str(self.home),
            SMOKE_SUPERVISOR_SOCKET=str(self.root / "supervisor.sock"),
            PATH=f"{self.bin}:/usr/local/bin:/usr/bin:/bin",
        )
        for name in () if real_systemd else ("systemctl", "systemd-run"):
            tool = self.bin / name
            shutil.copy2(FIXTURES / "supervisor.py", tool)
            tool.chmod(0o755)
        provider = self.bin / "claude"
        provider.write_text(
            (FIXTURES / "fake_claude.py")
            .read_text()
            .replace(
                'batch = args[args.index("--output-format") + 1] == "text"',
                'if directive.startswith("wait-for "):\n'
                '    gate = Path(directive.split(" ", 1)[1])\n'
                '    gate.with_suffix(".started").touch()\n'
                "    while not gate.exists():\n"
                "        time.sleep(0.05)\n"
                'batch = args[args.index("--output-format") + 1] == "text"',
            )
        )
        provider.chmod(0o755)
        cache = self.home / "runtime/cache/uv"
        shutil.copytree("/opt/uv-cache", cache, copy_function=os.link)
        if not real_systemd:
            self.supervisor = subprocess.Popen(
                [sys.executable, str(FIXTURES / "supervisor.py")], cwd=self.root, env=self.env
            )
            wait_for(lambda: Path(self.env["SMOKE_SUPERVISOR_SOCKET"]).exists())
        # Use the real distributable installer, with a loopback manifest and wheel feed.
        run(
            [
                "sh",
                str(ROOT / "feed" / release / "install.sh"),
                "--manifest",
                self.url(release),
                "--home",
                str(self.home),
                "--bin-dir",
                str(self.bin),
                "--viewer-service",
                "enso-cloud-viewer.service",
            ],
            env=self.env,
        )
        # Services often have a smaller PATH than the shell that ran the installer.
        # Keep only a fixture Python launcher; subsequent updates must use managed uv.
        (self.bin / "python3").symlink_to(sys.executable)
        self.env["PATH"] = f"{self.bin}:/usr/bin:/bin"
        assert shutil.which("uv", path=self.env["PATH"]) is None
        self.cli("init", "--json")
        self.config = {
            "version": 2,
            "transports": {
                "slack": {"bot_token": "xoxb-test", "app_token": "xapp-test", "notify": "C1"}
            },
            "bindings": {"slack:dm:U1": "default", "slack:C1": "default"},
            "defaults": {"provider": "claude", "model": "opus", "effort": "xhigh"},
            "providers": {"claude": {"path": str(provider), "models": ["opus"], "args": []}},
            "heartbeat": {"enabled": False},
            "agent": {"timeout": 120},
        }
        config_source = self.root / "config-source.json"
        config_source.write_text(json.dumps(self.config))
        self.cli("config", "apply", "--file", str(config_source), "--json")
        assert read_json(self.home / ".migrations.json") == {"revision": revision}
        workflow = self.home / WORKFLOW_PATHS[revision] / "example.json"
        assert read_json(workflow) == {"name": "example", "format": revision}
        workflow.write_text(json.dumps({"name": "user workflow", "format": revision}) + "\n")
        (self.home / "workspaces/default/keep.txt").write_text("user-authored content\n")
        (self.root / "provider-session.json").write_text('{"test-session":"preserve"}\n')
        self.cli("service", "install")
        unit = self.root / ".config/systemd/user/enso.service"
        # The normal installer adds conventional PATH entries. Exercise an operator's
        # constrained service environment as well as the constrained invoking shell.
        unit.write_text(
            re.sub(
                r'^Environment="PATH=.*"$',
                f'Environment="PATH={self.env["PATH"]}"',
                unit.read_text(),
                flags=re.MULTILINE,
            )
        )
        run(["systemctl", "--user", "daemon-reload"], env=self.env)
        run(["systemctl", "--user", "restart", "enso.service"], env=self.env)
        installed = read_json(ROOT / "feed" / release / "release.json")["version"]
        self.health(installed)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.viewer_port = reservation.getsockname()[1]
        viewer_unit = self.root / ".config/systemd/user/enso-cloud-viewer.service"
        viewer_unit.write_text(
            f"[Service]\nEnvironment=ENSO_HOME={self.home}\n"
            f"Environment=PATH={self.env['PATH']}\n"
            f"ExecStart={self.bin}/enso web start --foreground "
            f"--host 127.0.0.1 --port {self.viewer_port}\n"
        )
        if real_systemd:
            run(["systemctl", "--user", "daemon-reload"], env=self.env)
        run(["systemctl", "--user", "start", viewer_unit.name], env=self.env)
        self.viewer_health()
        with sqlite3.connect(self.home / "enso.db") as database:
            database.execute("CREATE TABLE smoke_keep (value TEXT)")
            database.execute("INSERT INTO smoke_keep VALUES ('preserve me')")
            if revision == 0:
                database.executemany(
                    "INSERT INTO smoke_feature (name, completed) VALUES (?, ?)",
                    [("first", 1), ("second", 0)],
                )
            else:
                database.executemany(
                    "INSERT INTO smoke_feature (name, state) VALUES (?, ?)",
                    [("first", "done"), ("second", "pending")],
                )
        self.initial_revision = revision
        self.before = self.snapshot()

    def url(self, name):
        return f"{self.feed.base}/{name}/release.json"

    def cli(self, *args, check=True, binary=None):
        result = run([binary or str(self.bin / "enso"), *args], env=self.env, check=check)
        if "--json" in args:
            try:
                value = json.loads(result.stdout)
            except ValueError as exc:
                raise AssertionError(
                    f"Invalid JSON result: {result.stdout}\n{result.stderr}"
                ) from exc
            return result.returncode, value
        return result

    def health(self, version):
        def check():
            state = read_json(self.home / "runtime/daemon.json")
            if state.get("version") != version or not state.get("ready"):
                return None
            try:
                ping = socket_call(self.home / "smoke-transport.sock", {"action": "ping"}, 2)
            except OSError, ValueError:
                return None
            return ping if ping.get("version") == version else None

        return wait_for(check, timeout=40, description=f"healthy Enso {version}")

    def snapshot(self):
        with sqlite3.connect(self.home / "enso.db") as database:
            schema = database.execute("PRAGMA user_version").fetchone()[0]
            rows = database.execute("SELECT * FROM smoke_keep").fetchall()
            columns = [row[1] for row in database.execute("PRAGMA table_info(smoke_feature)")]
            feature = database.execute("SELECT * FROM smoke_feature ORDER BY name").fetchall()
        return {
            "schema": schema,
            "rows": rows,
            "config": digest(self.home / "config.json"),
            "workspace": digest(self.home / "workspaces/default/keep.txt"),
            "provider_session": digest(self.root / "provider-session.json"),
            "revision": read_json(self.home / ".migrations.json"),
            "feature_columns": columns,
            "feature": feature,
            "workflow_files": {
                str(path.relative_to(self.home)): path.read_text()
                for root in ("smoke-legacy", "smoke-core")
                for path in sorted((self.home / root).rglob("*"))
                if path.is_file()
            },
            "workflow_directories": [
                name
                for name in ("smoke-legacy", "smoke-core", "smoke-core/library", *WORKFLOW_PATHS)
                if (self.home / name).is_dir()
            ],
        }

    def viewer_health(self):
        def check():
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.viewer_port}/", timeout=2
                ) as response:
                    return read_json(self.home / "web.pid") if response.status == 200 else None
            except OSError:
                return None

        return wait_for(check, description="reachable viewer")

    def assert_preserved(self, *, revision=0):
        after = self.snapshot()
        if revision == self.initial_revision:
            assert after == self.before, (after, self.before)
            return
        assert after["schema"] == self.before["schema"] + revision - self.initial_revision
        for name in ("rows", "config", "workspace", "provider_session"):
            assert after[name] == self.before[name], name
        assert after["revision"] == {"revision": revision}, after
        columns = ["name", "state", "priority"] + (["reviewed"] if revision == 2 else [])
        assert after["feature_columns"] == columns, after
        defaults = (0, 0) if revision == 2 else (0,)
        assert after["feature"] == [("first", "done", *defaults), ("second", "pending", *defaults)]
        expected_file = f"{WORKFLOW_PATHS[revision]}/example.json"
        assert after["workflow_files"] == {
            expected_file: json.dumps({"name": "user workflow", "format": revision}, indent=2)
            + "\n"
        }, after
        assert not (self.home / "smoke-legacy").exists()
        if revision == 2:
            assert not (self.home / WORKFLOW_PATHS[1]).exists()

    def assert_cleaned(self):
        runtime = self.home / "runtime"

        def cleaned():
            unit = "enso-update-" + self.last_operation["id"] + ".service"
            pid = run(
                ["systemctl", "--user", "show", "--value", "-p", "MainPID", unit],
                env=self.env,
                check=False,
            ).stdout.strip()
            if pid not in ("", "0"):
                return False
            operations = list((runtime / "operations").iterdir())
            assert len(operations) == 1, operations
            retained = list(operations[0].iterdir())
            assert not any(
                path.name == "backup" or path.name.startswith("failed-state-") for path in retained
            ), retained
            releases = list((runtime / "releases").iterdir())
            assert len(releases) <= 2, releases
            if self.last_operation["status"] != "succeeded":
                assert len(releases) == 1, releases
            archives = runtime / "cache/uv/archive-v0"
            assert not archives.exists() or not any(archives.iterdir()), "cached payloads remain"
            return True

        wait_for(cleaned, description="automatic snapshot and release cleanup")

    def refill_offline_cache(self):
        """Stand in for dependency downloads after cache cleanup, without network."""
        cache = self.home / "runtime/cache/uv"
        if cache.exists():
            shutil.rmtree(cache)
        shutil.copytree("/opt/uv-cache", cache, copy_function=os.link)

    def apply(self, release, **kwargs):
        self.feed.refill_cache = self.refill_offline_cache
        return self.cli(
            "update",
            "apply",
            "--manifest",
            self.url(release),
            "--json",
            "--startup-timeout",
            "8",
            "--drain-timeout",
            "5",
            **kwargs,
        )

    def outcome(self):
        def terminal():
            _, status = self.cli("update", "status", "--json")
            operation = status.get("operation") or {}
            if operation.get("status") in {
                "succeeded",
                "failed",
                "rolled_back",
                "recovery_failed",
                "deferred",
            }:
                self.last_operation = operation
                return status
            return None

        return wait_for(terminal, timeout=90, description="terminal upgrade status")

    def close(self):
        if self.real_systemd:
            run(
                ["systemctl", "--user", "stop", "enso.service", "enso-cloud-viewer.service"],
                env=self.env,
                check=False,
            )
            return
        socket_call(
            Path(self.env["SMOKE_SUPERVISOR_SOCKET"]),
            {"tool": "cleanup", "args": [], "env": self.env},
        )
        self.supervisor.terminate()
        self.supervisor.wait(timeout=10)


def success(instance):
    original = instance.health("0.2.0")["pid"]
    viewer = instance.viewer_health()["pid"]
    instance.apply("good")
    status = instance.outcome()
    assert status["installed_version"] == "0.2.1", status
    assert instance.health("0.2.1")["pid"] != original
    assert instance.viewer_health()["pid"] != viewer
    instance.assert_preserved(revision=1)
    reply = socket_call(instance.home / "smoke-transport.sock", {"action": "turn", "text": "hello"})
    assert any("hello" in message for message in reply["messages"]), reply
    instance.assert_cleaned()
    for release, version in (("latest", "0.2.2"), ("repeat", "0.2.3")):
        instance.apply(release)
        status = instance.outcome()
        assert status["operation"]["status"] == "succeeded", status
        instance.health(version)
        instance.assert_preserved(revision=2)
        instance.assert_cleaned()


def skipped_release(instance):
    instance.apply("latest")
    status = instance.outcome()
    assert status["operation"]["status"] == "succeeded", status
    instance.health("0.2.2")
    instance.assert_preserved(revision=2)


def fresh_latest(instance):
    """Current DB defaults and bundled layout need no old-home migration on a fresh install."""
    instance.health("0.2.2")
    instance.assert_preserved(revision=2)
    assert instance.before["feature_columns"] == ["name", "state", "priority", "reviewed"]
    assert instance.before["feature"] == [("first", "done", 0, 0), ("second", "pending", 0, 0)]
    assert not (instance.home / "smoke-legacy").exists()
    assert not (instance.home / WORKFLOW_PATHS[1]).exists()
    assert len(list((instance.home / "runtime/releases").iterdir())) == 1


def failed_release(instance, release):
    daemon = instance.health("0.2.0")["pid"]
    viewer = instance.viewer_health()["pid"]
    instance.apply(release, check=False)
    status = instance.outcome()
    assert status["installed_version"] == "0.2.0", status
    current_daemon = instance.health("0.2.0")["pid"]
    current_viewer = instance.viewer_health()["pid"]
    if release in ("hash", "download", "dependency"):
        assert status["operation"]["status"] == "failed", status
        assert (current_daemon, current_viewer) == (daemon, viewer)
    else:
        assert status["operation"]["status"] == "rolled_back", status
        assert (current_daemon, current_viewer) != (daemon, viewer)
        versions = [
            json.loads(line)["version"]
            for line in (instance.home / "smoke-start-attempts.jsonl").read_text().splitlines()
        ]
        if release == "startup":
            assert "0.2.4" in versions, "candidate never reached its intended startup failure"
        else:
            assert "0.2.5" not in versions, "candidate started despite a failed migration"
    instance.assert_preserved()


def concurrent(instance):
    instance.feed.hold = True
    instance.feed.blocked.clear()
    instance.feed.release.clear()
    try:
        instance.apply("good")
        assert instance.feed.blocked.wait(15), "first update did not request its wheel"
        code, result = instance.apply("good", check=False)
        assert code != 0 or result.get("accepted") is False, result
    finally:
        instance.feed.hold = False
        instance.feed.release.set()
    instance.outcome()
    instance.health("0.2.1")
    instance.assert_preserved(revision=1)


def busy(instance):
    gate = instance.root / "provider-release"
    with ThreadPoolExecutor() as pool:
        response = pool.submit(
            socket_call,
            instance.home / "smoke-transport.sock",
            {"action": "turn", "text": f"wait-for {gate}"},
            40,
        )
        wait_for(lambda: gate.with_suffix(".started").exists(), description="active provider")
        instance.apply("good")
        status = instance.outcome()
        assert status["installed_version"] == "0.2.0", status
        gate.touch()
        response.result(timeout=10)
    instance.health("0.2.0")
    instance.assert_preserved()


def kill_helper(instance):
    result = socket_call(
        Path(instance.env["SMOKE_SUPERVISOR_SOCKET"]),
        {"tool": "inspect", "args": [], "env": instance.env},
    )
    children = json.loads(result["output"])
    helper = next(pid for name, pid in children.items() if name.startswith("enso-update"))
    os.killpg(helper, signal.SIGKILL)
    # Reaping through the supervisor gives recovery a deterministic dead-helper signal.
    wait_for(
        lambda: (
            helper
            not in json.loads(
                socket_call(
                    Path(instance.env["SMOKE_SUPERVISOR_SOCKET"]),
                    {"tool": "inspect", "args": [], "env": instance.env},
                )["output"]
            ).values()
        ),
        description="updater process exit",
    )


def interrupted_staging(instance):
    instance.feed.hold = True
    instance.feed.blocked.clear()
    instance.feed.release.clear()
    try:
        instance.apply("good")
        assert instance.feed.blocked.wait(15)
        kill_helper(instance)
    finally:
        instance.feed.hold = False
        instance.feed.release.set()
    instance.cli("update", "recover", "--json")
    status = instance.outcome()
    assert status["installed_version"] == "0.2.1", status
    instance.health("0.2.1")
    instance.assert_preserved(revision=1)


def interrupted_after_switch(instance):
    hold = instance.home / "smoke-hold-ready"
    hold.touch()
    instance.apply("good")
    wait_for(
        lambda: read_json(instance.home / "smoke-waiting-ready.json").get("version") == "0.2.1",
        description="candidate startup after migration",
    )
    assert instance.snapshot()["schema"] == instance.before["schema"] + 1
    kill_helper(instance)
    hold.unlink()
    instance.cli("update", "recover", "--json")
    status = instance.outcome()
    assert status["operation"]["status"] == "rolled_back", status
    instance.health("0.2.0")
    instance.viewer_health()
    instance.assert_preserved()


def main():
    if not Path("/.dockerenv").exists() or Path.home() != Path("/root"):
        raise SystemExit("This harness runs only inside its disposable Docker container.")
    ROOT.mkdir()
    report = {
        "ok": False,
        "isolation": "Docker; no network or mounts; fake process supervisor",
        "results": [],
    }
    REPORT.write_text(json.dumps(report))
    feed = None
    try:
        for name, version, options in (
            ("base", "0.2.0", {}),
            ("good", "0.2.1", {"revision": 1}),
            ("latest", "0.2.2", {"revision": 2}),
            ("repeat", "0.2.3", {"revision": 2}),
            ("startup", "0.2.4", {"revision": 2, "startup_failure": True}),
            ("migration", "0.2.5", {"revision": 2, "migration_failure": True}),
            ("dependency", "0.2.6", {"dependency": True}),
        ):
            print(f"Building synthetic {name} release", flush=True)
            build_release(name, version, **options)
        for name in ("hash", "download"):
            shutil.copytree(ROOT / "feed/good", ROOT / "feed" / name)
            manifest = read_json(ROOT / "feed" / name / "release.json")
            if name == "hash":
                manifest["wheel"]["sha256"] = "0" * 64
            else:
                manifest["wheel"]["url"] = "enso-0.2.0-missing.whl"
            (ROOT / "feed" / name / "release.json").write_text(json.dumps(manifest))
        feed = Feed()
        threading.Thread(target=feed.serve_forever, daemon=True).start()
        cases = [
            ("fresh_latest_install", fresh_latest),
            ("success_and_repeated_cleanup", success),
            ("skipped_release_migrations", skipped_release),
        ]
        cases += [
            (f"{name}_failure", lambda instance, release=name: failed_release(instance, release))
            for name in ("hash", "download", "dependency", "startup", "migration")
        ]
        cases += [
            ("concurrent_requests", concurrent),
            ("busy_runtime", busy),
            ("interrupted_staging_recovery", interrupted_staging),
            ("interrupted_switched_recovery", interrupted_after_switch),
        ]
        for name, test in cases:
            print(f"Running {name}", flush=True)
            instance = None
            started = time.monotonic()
            try:
                options = {"release": "latest", "revision": 2} if test is fresh_latest else {}
                instance = Instance(name, feed, **options)
                test(instance)
                if test is not fresh_latest:
                    instance.assert_cleaned()
                report["results"].append(
                    {"name": name, "ok": True, "seconds": round(time.monotonic() - started, 2)}
                )
            except Exception:
                report["results"].append(
                    {"name": name, "ok": False, "error": traceback.format_exc()}
                )
                traceback.print_exc()
            finally:
                if instance:
                    instance.close()
                REPORT.write_text(json.dumps(report, indent=2))
        report["ok"] = all(result["ok"] for result in report["results"])
    except Exception:
        report["error"] = traceback.format_exc()
        traceback.print_exc()
    finally:
        if feed:
            feed.shutdown()
        REPORT.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
