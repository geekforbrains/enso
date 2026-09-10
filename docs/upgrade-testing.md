# Upgrade acceptance tests

The opt-in Docker smoke test checks the installed updater against real wheels and
live Enso processes, without accessing a developer's home or installed service:

```bash
python3 scripts/smoke-upgrade.py --output /tmp/enso-upgrade-report.json
```

Docker must be running. The script builds a uniquely named image and container,
runs the tests, saves a JSON report, and removes only that run's container and
image. `--keep` preserves both for diagnosis; the script prints their exact name.
The command can take several minutes on its first run.

The build context contains only the package source, package metadata, lockfile,
README and explicit test fixtures. It excludes Git metadata, local artifacts,
credentials and homes. The running container has no network, bind mounts,
privileged mode or Linux capabilities. No existing Docker volumes are used.
Dependency downloads happen during the image build; runtime installations use a
prewarmed uv cache and a release feed on the container's loopback interface.

The harness builds synthetic versions from temporary source copies. It replaces
Slack with a local socket transport and Claude with the existing fake provider;
the installed CLI, runtime, updater, database migrations and uv environments are
real. Temporary candidate releases contain deliberately broken dependencies,
migrations or startup code. These faults never enter production source or the
working checkout's version metadata.

The cases check:

- a successful upgrade changes the running process and version, migrates the
  database, preserves user data, and answers a new provider turn;
- a wrong checksum, missing download or dependency-resolution failure preserves
  the working installation;
- startup and migration failures restore compatible software and database state;
- concurrent requests cannot install competing releases;
- busy provider work prevents an unsafe swap; and
- a killed updater can recover without losing the selected release or user data.

A small test-only process supervisor implements the service-manager command
boundary and starts independent real subprocess groups. This tests process
handoff and recovery without granting Docker access to the host's service manager.
It does **not** validate systemd cgroup membership or launchd behavior.

The separate Linux service-manager lane runs real systemd as the container's first
process, with Enso and its updater running as a dedicated unprivileged user:

```bash
python3 scripts/smoke-upgrade.py --systemd --output /tmp/enso-systemd-report.json
```

This opt-in lane uses a privileged disposable container so systemd can manage its
private cgroup namespace. It has no host bind mounts, Docker socket, existing
volumes or network, and uses temporary filesystems for `/run` and `/tmp`. Docker
Desktop runs that privileged container inside its Linux VM; the default lane
above remains unprivileged. The runner removes only its uniquely named container
and both of its images after copying the report.

The installed daemon itself launches an upgrade through its local test transport.
The checks verify that the actual updater unit occupies a different cgroup and
keeps the same process ID while systemd stops the old daemon and viewer and starts
their replacements. A second update kills the helper with `SIGKILL` and verifies
that systemd automatically restarts it and finishes the upgrade without a manual
recovery command. Both retain the real wheel installer, migration and user-data
checks. Startup and migration failures then verify rollback of the installed code
and database with both services restored. Killing the helper after migration but
before readiness verifies that its automatic restart restores the previous release
and schema. The report records service versions, cgroups, process IDs and restart
count.

Before a release, also exercise real transport authentication and macOS service
replacement on disposable accounts. The automated suite mocks launchd; a scoped
native helper probe can verify launch, retry and cleanup without using Enso's
normal service label. Never run service checks against the operator's active home.

Normal unit and integration tests remain in the default suite; this slower,
installed-wheel lane runs only when explicitly requested. [Installation](install.md)
owns the user-facing update behavior.
