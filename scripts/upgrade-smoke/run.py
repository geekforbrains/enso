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


def inject_migration(original: str, *, fail: bool = False) -> str:
    """Insert a synthetic next migration without assuming the current schema's symbol."""
    version = re.search(r"^SCHEMA_VERSION = (\d+)$", original, re.MULTILINE)
    if version is None:
        raise AssertionError("Smoke fixture cannot locate SCHEMA_VERSION")
    current = int(version.group(1))
    target = current + 1
    anchor = re.compile(rf"^([ \t]+)\({current}, [A-Za-z_][A-Za-z_0-9]*\),$", re.MULTILINE)
    if len(list(anchor.finditer(original))) != 1:
        raise AssertionError(
            f"Smoke fixture must find exactly one schema {current} migration tuple"
        )
    statement = f"CREATE TABLE smoke_migrated_v{target} (value TEXT);"
    if fail:
        statement += "\nTHIS IS INVALID SQL;"
    replacement = f'_SCHEMA_V{target} = """\n{statement}\nPRAGMA user_version = {target};\n"""\n\n'
    updated = original.replace(version.group(0), f"SCHEMA_VERSION = {target}\n\n{replacement}", 1)
    updated, count = anchor.subn(
        lambda match: f"{match.group(0)}\n{match.group(1)}({target}, _SCHEMA_V{target}),",
        updated,
        count=1,
    )
    assert count == 1, "Smoke fixture failed to insert its synthetic migration"
    return updated


def build_release(
    name, version, *, migration="none", startup_failure=False, dependency=False, source_root=SOURCE
):
    source = ROOT / "sources" / name
    shutil.copytree(source_root, source, ignore=shutil.ignore_patterns("__pycache__"))
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
    if migration != "none":
        database = source / "src/enso/db.py"
        database.write_text(inject_migration(database.read_text(), fail=migration == "fail"))
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
        super().__init__(("127.0.0.1", 0), FeedHandler)

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server_port}"


class FeedHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT / "feed", **kwargs)

    def do_GET(self):
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
    def __init__(self, name, feed, *, real_systemd=False):
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
                str(ROOT / "feed/base/install.sh"),
                "--manifest",
                self.url("base"),
                "--home",
                str(self.home),
                "--bin-dir",
                str(self.bin),
                "--viewer-service",
                "enso-cloud-viewer.service",
            ],
            env=self.env,
        )
        self.cli("init", "--json")
        self.config = {
            "version": 1,
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
        (self.home / "workspaces/default/keep.txt").write_text("user-authored content\n")
        (self.root / "provider-session.json").write_text('{"test-session":"preserve"}\n')
        self.cli("service", "install")
        self.health("0.1.0")
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
        return {
            "schema": schema,
            "rows": rows,
            "config": digest(self.home / "config.json"),
            "workspace": digest(self.home / "workspaces/default/keep.txt"),
            "provider_session": digest(self.root / "provider-session.json"),
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

    def assert_preserved(self, *, migrated=False):
        after = self.snapshot()
        expected = dict(self.before)
        if migrated:
            expected["schema"] += 1
        assert after == expected, (after, expected)

    def apply(self, release, **kwargs):
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
    original = instance.health("0.1.0")["pid"]
    viewer = instance.viewer_health()["pid"]
    instance.apply("good")
    status = instance.outcome()
    assert status["installed_version"] == "0.2.0", status
    assert instance.health("0.2.0")["pid"] != original
    assert instance.viewer_health()["pid"] != viewer
    instance.assert_preserved(migrated=True)
    reply = socket_call(instance.home / "smoke-transport.sock", {"action": "turn", "text": "hello"})
    assert any("hello" in message for message in reply["messages"]), reply


def failed_release(instance, release):
    daemon = instance.health("0.1.0")["pid"]
    viewer = instance.viewer_health()["pid"]
    instance.apply(release, check=False)
    status = instance.outcome()
    assert status["installed_version"] == "0.1.0", status
    current_daemon = instance.health("0.1.0")["pid"]
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
            assert "0.3.0" in versions, "candidate never reached its intended startup failure"
        else:
            assert "0.4.0" not in versions, "candidate started despite a failed migration"
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
    instance.health("0.2.0")
    instance.assert_preserved(migrated=True)


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
        assert status["installed_version"] == "0.1.0", status
        gate.touch()
        response.result(timeout=10)
    instance.health("0.1.0")
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
    assert status["installed_version"] == "0.2.0", status
    instance.health("0.2.0")
    instance.assert_preserved(migrated=True)


def interrupted_after_switch(instance):
    hold = instance.home / "smoke-hold-ready"
    hold.touch()
    instance.apply("good")
    wait_for(
        lambda: read_json(instance.home / "smoke-waiting-ready.json").get("version") == "0.2.0",
        description="candidate startup after migration",
    )
    assert instance.snapshot()["schema"] == instance.before["schema"] + 1
    kill_helper(instance)
    hold.unlink()
    instance.cli("update", "recover", "--json")
    status = instance.outcome()
    assert status["operation"]["status"] == "rolled_back", status
    instance.health("0.1.0")
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
            ("base", "0.1.0", {}),
            ("good", "0.2.0", {"migration": "good"}),
            ("startup", "0.3.0", {"migration": "good", "startup_failure": True}),
            ("migration", "0.4.0", {"migration": "fail"}),
            ("dependency", "0.5.0", {"dependency": True}),
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
        cases = [("success", success)]
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
                instance = Instance(name, feed)
                test(instance)
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
