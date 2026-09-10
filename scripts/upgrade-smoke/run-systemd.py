"""Actual Linux user-systemd handoff and automatic recovery inside a disposable container."""

import json
import os
import threading
import traceback
from pathlib import Path

from run import ROOT, Feed, Instance, build_release, read_json, run, socket_call, wait_for

REPORT = Path("/home/enso/upgrade-report.json")


def unit_value(instance, unit, property_name):
    return run(
        ["systemctl", "--user", "show", "--value", "-p", property_name, unit],
        env=instance.env,
    ).stdout.strip()


def chat_update(instance, release):
    response = socket_call(
        instance.home / "smoke-transport.sock",
        {"action": "update", "manifest": instance.url(release)},
    )
    assert response["returncode"] == 0, response
    assert response["result"]["ok"], response
    assert "/enso.service" in response["requester_cgroup"], response
    return "enso-update-" + response["result"]["operation"]["id"] + ".service"


def independent_handoff(instance):
    old_daemon = instance.health("0.1.0")["pid"]
    old_viewer = instance.viewer_health()["pid"]
    old_cgroup = Path(f"/proc/{old_daemon}/cgroup").read_text().strip()
    hold = instance.home / "smoke-hold-ready"
    hold.touch()
    instance.feed.hold = True
    instance.feed.release.clear()
    instance.feed.blocked.clear()
    try:
        unit = chat_update(instance, "good")
        assert instance.feed.blocked.wait(15), "helper did not begin download"
        helper = int(unit_value(instance, unit, "MainPID"))
        helper_cgroup = Path(f"/proc/{helper}/cgroup").read_text().strip()
        assert helper_cgroup != old_cgroup and f"/{unit}" in helper_cgroup
    finally:
        instance.feed.hold = False
        instance.feed.release.set()
    wait_for(
        lambda: read_json(instance.home / "smoke-waiting-ready.json").get("version") == "0.2.0",
        description="new daemon waiting for transport readiness",
    )
    assert not Path(f"/proc/{old_daemon}").exists(), "old daemon survived service stop"
    assert not Path(f"/proc/{old_viewer}").exists(), "old viewer survived service stop"
    assert int(unit_value(instance, unit, "MainPID")) == helper, "helper died with daemon cgroup"
    assert Path(f"/proc/{helper}/cgroup").read_text().strip() == helper_cgroup
    hold.unlink()
    socket_call(instance.home / "smoke-transport.sock", {"action": "release_ready"})
    outcome = instance.outcome()
    assert outcome["operation"]["status"] == "succeeded", outcome
    new_daemon = instance.health("0.2.0")["pid"]
    new_viewer = instance.viewer_health()["pid"]
    instance.assert_preserved(migrated=True)
    return {
        "name": "helper_survives_daemon_cgroup_stop",
        "ok": True,
        "daemon_cgroup": old_cgroup,
        "helper_cgroup": helper_cgroup,
        "helper_pid_before_and_after_stop": helper,
        "old_daemon_pid": old_daemon,
        "new_daemon_pid": new_daemon,
        "old_viewer_pid": old_viewer,
        "new_viewer_pid": new_viewer,
    }


def automatic_recovery(instance):
    instance.feed.hold = True
    instance.feed.release.clear()
    instance.feed.blocked.clear()
    try:
        unit = chat_update(instance, "recovery")
        assert instance.feed.blocked.wait(15), "helper did not begin download"
        old_helper = int(unit_value(instance, unit, "MainPID"))
        run(
            ["systemctl", "--user", "kill", "--kill-who=main", "--signal=SIGKILL", unit],
            env=instance.env,
        )

        def restarted():
            pid = unit_value(instance, unit, "MainPID")
            return int(pid) if pid.isdigit() and int(pid) not in (0, old_helper) else None

        new_helper = wait_for(restarted, timeout=20, description="systemd automatic helper restart")
        restarts = int(unit_value(instance, unit, "NRestarts"))
        assert restarts >= 1
    finally:
        instance.feed.hold = False
        instance.feed.release.set()
    outcome = instance.outcome()
    assert outcome["operation"]["status"] == "succeeded", outcome
    instance.health("0.3.0")
    instance.viewer_health()
    instance.assert_preserved(migrated=True)
    return {
        "name": "sigkill_helper_automatically_recovers",
        "ok": True,
        "old_helper_pid": old_helper,
        "new_helper_pid": new_helper,
        "systemd_restart_count": restarts,
        "manual_recover_called": False,
    }


def failed_candidate(instance, release):
    before = instance.snapshot()
    old_daemon = instance.health("0.3.0")["pid"]
    old_viewer = instance.viewer_health()["pid"]
    chat_update(instance, release)
    outcome = instance.outcome()
    assert outcome["operation"]["status"] == "rolled_back", outcome
    assert outcome["installed_version"] == "0.3.0", outcome
    new_daemon = instance.health("0.3.0")["pid"]
    new_viewer = instance.viewer_health()["pid"]
    assert new_daemon != old_daemon and new_viewer != old_viewer
    assert instance.snapshot() == before, "rollback changed pre-upgrade data or schema"
    attempted = [
        json.loads(line)["version"]
        for line in (instance.home / "smoke-start-attempts.jsonl").read_text().splitlines()
    ]
    if release == "startup":
        assert "0.4.0" in attempted, "startup failure never reached candidate transport"
    else:
        assert "0.5.0" not in attempted, "candidate started after migration failure"
    return {
        "name": f"{release}_failure_restores_code_database_and_viewer",
        "ok": True,
        "restored_version": "0.3.0",
        "restored_schema": before["schema"],
        "old_daemon_pid": old_daemon,
        "new_daemon_pid": new_daemon,
        "old_viewer_pid": old_viewer,
        "new_viewer_pid": new_viewer,
    }


def interrupted_candidate(instance):
    before = instance.snapshot()
    hold = instance.home / "smoke-hold-ready"
    hold.touch()
    unit = chat_update(instance, "interrupted")
    wait_for(
        lambda: read_json(instance.home / "smoke-waiting-ready.json").get("version") == "0.6.0",
        description="candidate migration before readiness",
    )
    assert instance.snapshot()["schema"] == before["schema"] + 1
    helper = int(unit_value(instance, unit, "MainPID"))
    run(
        ["systemctl", "--user", "kill", "--kill-who=main", "--signal=SIGKILL", unit],
        env=instance.env,
    )
    hold.unlink()
    outcome = instance.outcome()
    assert outcome["operation"]["status"] == "rolled_back", outcome
    assert outcome["installed_version"] == "0.3.0", outcome
    instance.health("0.3.0")
    instance.viewer_health()
    assert instance.snapshot() == before
    return {
        "name": "sigkill_after_migration_automatically_restores_previous_release",
        "ok": True,
        "killed_helper_pid": helper,
        "restored_version": "0.3.0",
        "restored_schema": before["schema"],
        "manual_recover_called": False,
    }


def main():
    if (
        not Path("/.dockerenv").exists()
        or Path("/proc/1/comm").read_text().strip() != "systemd"
        or os.getuid() != 1000
        or Path.home() != Path("/home/enso")
    ):
        raise SystemExit(
            "Run only as the disposable container's dedicated enso user under systemd."
        )
    ROOT.mkdir()
    report = {
        "ok": False,
        "isolation": "Docker; private cgroups; privileged; no host mounts/network; uid 1000",
        "systemd": run(["systemd", "--version"]).stdout.splitlines()[0],
        "results": [],
    }
    REPORT.write_text(json.dumps(report))
    feed = None
    instance = None
    try:
        for name, version in (("base", "0.1.0"), ("good", "0.2.0"), ("recovery", "0.3.0")):
            print(f"Building synthetic {name} release", flush=True)
            build_release(name, version, migration="none" if name == "base" else "good")
        for name, version in (("startup", "0.4.0"), ("migration", "0.5.0")):
            print(f"Building synthetic {name} release", flush=True)
            build_release(
                name,
                version,
                migration="fail" if name == "migration" else "good",
                startup_failure=name == "startup",
                source_root=ROOT / "sources/good",
            )
        build_release("interrupted", "0.6.0", migration="good", source_root=ROOT / "sources/good")
        feed = Feed()
        threading.Thread(target=feed.serve_forever, daemon=True).start()
        instance = Instance("systemd", feed, real_systemd=True)
        for case in (independent_handoff, automatic_recovery):
            print(f"Running {case.__name__}", flush=True)
            report["results"].append(case(instance))
            REPORT.write_text(json.dumps(report, indent=2))
        for release in ("startup", "migration"):
            print(f"Running {release}_failure", flush=True)
            report["results"].append(failed_candidate(instance, release))
            REPORT.write_text(json.dumps(report, indent=2))
        print("Running interrupted_candidate", flush=True)
        report["results"].append(interrupted_candidate(instance))
        report["ok"] = True
    except Exception:
        report["error"] = traceback.format_exc()
        traceback.print_exc()
        if instance:
            print(
                run(
                    ["journalctl", "--user", "--no-pager", "-n", "80"],
                    env=instance.env,
                    check=False,
                ).stdout
            )
    finally:
        if instance:
            instance.close()
        if feed:
            feed.shutdown()
        REPORT.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
