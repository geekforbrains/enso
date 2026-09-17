"""Release integrity, scoped download authentication, and failed preparation boundaries."""

import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from enso import releases


def manifest(tmp_path, *, version="0.2.0"):
    wheel = tmp_path / f"enso-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"enso-{version}.dist-info/METADATA",
            f"Name: enso\nVersion: {version}\nRequires-Python: >=3.14\n",
        )
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("typer==0.12.0\n")
    raw = {
        "schema_version": 1,
        "version": version,
        "commit": "a" * 40,
        "requires_python": ">=3.14",
        "wheel": {"url": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
        "constraints": {
            "url": constraints.name,
            "sha256": hashlib.sha256(constraints.read_bytes()).hexdigest(),
        },
    }
    source = tmp_path / "release.json"
    source.write_text(json.dumps(raw))
    return source, raw


def test_local_release_and_pinned_snapshot(tmp_path):
    source, raw = manifest(tmp_path)
    release = releases.load_release(source)
    assert release.to_dict() == raw
    assert releases.download_artifact(release, release.wheel).startswith(b"PK")
    pinned = tmp_path / "operation" / "release.json"
    pinned.parent.mkdir()
    pinned.write_text(json.dumps(release.to_dict(resolved=True)))
    assert releases.download_artifact(
        releases.load_release(pinned), releases.load_release(pinned).wheel
    ).startswith(b"PK")


def test_persisted_local_source_is_canonical_independent_of_later_working_directory(
    tmp_path, monkeypatch
):
    manifest(tmp_path)
    monkeypatch.chdir(tmp_path)
    source = releases.normalize_source("./release.json")
    assert source == str((tmp_path / "release.json").resolve())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert releases.load_release(source).version == "0.2.0"


@pytest.mark.parametrize(
    "source",
    [
        "",
        " ",
        "http://downloads.example.test/release.json",
        "//example.test/release.json",
        "https://token@example.test/release.json",
        "https://example.test/release.json?token=secret",
        "https://example.test/release.json#fragment",
        "file:///tmp/release.json",
    ],
)
def test_normalize_source_rejects_unsafe_persisted_feed(source):
    with pytest.raises(releases.ReleaseError) as error:
        releases.normalize_source(source)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "source", ["https://example.test/stable/release.json", "http://127.0.0.1:8888/release.json"]
)
def test_normalize_source_accepts_https_and_loopback_without_fetching(source, monkeypatch):
    monkeypatch.setattr(
        releases, "_read_bytes", lambda *args, **kwargs: pytest.fail("must not fetch")
    )
    assert releases.normalize_source(source) == source


def test_uv_is_copied_from_resolved_executable_and_survives_source_removal(tmp_path, monkeypatch):
    source = tmp_path / "system/uv-original"
    source.parent.mkdir()
    source.write_text("#!/bin/sh\nprintf 'uv test-version\\n'\n")
    source.chmod(0o755)
    (source.parent / "uv").symlink_to(source)
    monkeypatch.setenv("PATH", str(source.parent))
    runtime = tmp_path / "home/runtime"
    copied = releases.ensure_uv(runtime)
    assert copied == str(runtime / "tools/uv")
    assert not (runtime / "tools/uv").is_symlink()
    assert (runtime / "tools/uv").read_bytes() == source.read_bytes()
    source.unlink()
    monkeypatch.setenv("PATH", "/missing")
    assert releases.ensure_uv(runtime) == copied
    assert releases.run_bounded([copied, "--version"], cwd=runtime) == "uv test-version"
    assert set((runtime / "tools").iterdir()) == {runtime / "tools/uv"}


@pytest.mark.parametrize("kind", ["symlink", "directory", "not-executable"])
def test_uv_refuses_to_replace_an_invalid_existing_home_copy(tmp_path, kind):
    runtime = tmp_path / "runtime"
    destination = runtime / "tools/uv"
    destination.parent.mkdir(parents=True)
    if kind == "symlink":
        destination.symlink_to(tmp_path / "missing")
    elif kind == "directory":
        destination.mkdir()
    else:
        destination.write_text("not executable")
        destination.chmod(0o600)
    with pytest.raises(releases.ReleaseError, match="managed uv"):
        releases.ensure_uv(runtime)
    assert os.path.lexists(destination)


def test_uv_missing_from_home_and_path_explains_repair_without_creating_files(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PATH", "/missing")
    runtime = tmp_path / "runtime"
    with pytest.raises(releases.ReleaseError, match="rerun the release installer"):
        releases.ensure_uv(runtime)
    assert not runtime.exists()


def test_manifest_reports_independent_errors(tmp_path):
    source, raw = manifest(tmp_path)
    raw.update(schema_version=True, version="../../evil", commit="not-a-commit")
    raw["wheel"]["sha256"] = "wrong"
    with pytest.raises(releases.ReleaseError) as error:
        releases.parse_release(raw, str(source))
    assert all(
        name in str(error.value) for name in ("schema_version", "version", "commit", "sha256")
    )


@pytest.mark.parametrize(
    "url", ["../escape.whl", "%2e%2e/escape.whl", "//evil.test/file.whl", "a\\b.whl"]
)
def test_artifact_cannot_escape_manifest_directory(tmp_path, url):
    source, raw = manifest(tmp_path)
    raw["wheel"]["url"] = url
    with pytest.raises(releases.ReleaseError, match="artifact paths"):
        releases.parse_release(raw, str(source))


@pytest.mark.parametrize(
    "url",
    ["http://example.com/release.json", "https://secret@example.com/release.json", "file:///tmp/x"],
)
def test_unsafe_manifest_url_rejected_before_fetch(url, monkeypatch):
    monkeypatch.setattr(releases, "_read_bytes", lambda *a, **kw: pytest.fail("must not fetch"))
    with pytest.raises(releases.ReleaseError):
        releases.load_release(url)


def test_changed_wheel_fails_hash_before_creating_environment(tmp_path, monkeypatch):
    source, raw = manifest(tmp_path)
    (tmp_path / raw["wheel"]["url"]).write_bytes(b"tampered")
    monkeypatch.setattr(releases, "run_bounded", lambda *a, **kw: pytest.fail("must not install"))
    destination = tmp_path / "runtime/releases/candidate"
    with pytest.raises(releases.ReleaseError, match="SHA256"):
        releases.prepare_release(releases.load_release(source), destination)
    assert not destination.exists()


def test_manifest_download_has_size_limit(tmp_path, monkeypatch):
    path = tmp_path / "release.json"
    path.write_bytes(b" " * 101)
    monkeypatch.setattr(releases, "MANIFEST_LIMIT", 100)
    with pytest.raises(releases.ReleaseError, match="size limit"):
        releases.load_release(path)


def test_wheel_metadata_must_match_manifest(tmp_path, monkeypatch):
    source, raw = manifest(tmp_path)
    raw["version"] = "0.3.0"
    source.write_text(json.dumps(raw))
    monkeypatch.setattr(releases, "run_bounded", lambda *a, **kw: pytest.fail("must not install"))
    destination = tmp_path / "runtime/releases/candidate"
    with pytest.raises(releases.ReleaseError, match="version does not match"):
        releases.prepare_release(releases.load_release(source), destination)
    assert not destination.exists()


def test_failed_install_removes_candidate_and_preserves_active(tmp_path, monkeypatch):
    source, _ = manifest(tmp_path)
    runtime = tmp_path / "runtime"
    active = runtime / "releases/old"
    active.mkdir(parents=True)
    (active / "user-data").write_text("old")
    (runtime / "current").symlink_to(active)
    destination = runtime / "releases/candidate"

    def fail(*args, **kwargs):
        (destination / "partial").write_text("partial")
        raise releases.ReleaseError("Dependency install failed")

    monkeypatch.setattr(releases, "run_bounded", fail)
    with pytest.raises(releases.ReleaseError, match="Dependency"):
        releases.prepare_release(releases.load_release(source), destination)
    assert not destination.exists()
    assert (runtime / "current").resolve() == active
    assert (active / "user-data").read_text() == "old"


def test_existing_candidate_never_overwritten(tmp_path):
    source, _ = manifest(tmp_path)
    candidate = tmp_path / "existing"
    candidate.mkdir()
    (candidate / "keep").write_text("keep")
    with pytest.raises(releases.ReleaseError, match="already exists"):
        releases.prepare_release(releases.load_release(source), candidate)
    assert (candidate / "keep").read_text() == "keep"


def test_completed_release_can_be_reused_but_extras_cannot_change(tmp_path, monkeypatch):
    source, _ = manifest(tmp_path)
    release = releases.load_release(source)
    candidate = tmp_path / "runtime/releases/candidate"

    def fake_run(args, **kwargs):
        if "venv" in args:
            (candidate / "bin").mkdir()
            (candidate / "bin/enso").touch()
        if "list" in args:
            return json.dumps([{"name": "enso", "version": release.version}])
        return release.version if "-c" in args else ""

    monkeypatch.setattr(releases, "run_bounded", fake_run)
    releases.prepare_release(release, candidate, extras=("slack",))
    monkeypatch.setattr(
        releases, "download_artifact", lambda *a, **kw: pytest.fail("reuse needs no download")
    )
    assert releases.prepare_release(release, candidate, extras=("slack",)) == candidate
    with pytest.raises(releases.ReleaseError, match="matching completed install"):
        releases.prepare_release(release, candidate, extras=("telegram",))
    assert (candidate / "bin/enso").exists()


@pytest.mark.parametrize(
    "line", ["--index-url https://example.test", "enso @ file:///evil", "foo>=1", "-r evil"]
)
def test_constraints_allow_only_exact_pins(line):
    with pytest.raises(releases.ReleaseError, match="exact package version pins"):
        releases._validate_constraints(line.encode())


def test_dependency_verification_rejects_missing_or_changed_transitive_pin():
    versions = releases._validate_constraints(b"Foo_Bar==1.2.3\n")
    installed = [{"name": "enso", "version": "0.2.0"}, {"name": "foo-bar", "version": "1.2.3"}]
    releases._validate_installed(json.dumps(installed), versions, "0.2.0")
    for incorrect in (set(), {("foo-bar", "1.2.4")}):
        with pytest.raises(releases.ReleaseError, match="not fully pinned"):
            releases._validate_installed(json.dumps(installed), incorrect, "0.2.0")


def test_fetch_bounds_size_and_deadline_and_reports_unfollowed_redirects(monkeypatch):
    opener = SimpleNamespace(open=lambda request, timeout: io.BytesIO(b"too many bytes"))
    monkeypatch.setattr(releases.urllib.request, "build_opener", lambda *handlers: opener)
    with pytest.raises(releases.FetchError, match="size limit"):
        releases.fetch("https://example.test/a", 3, time.monotonic() + 10, headers={})
    with pytest.raises(releases.FetchError, match="timed out"):
        releases.fetch("https://example.test/a", 3, 0, headers={})

    def redirect(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 302, "redirect", {"Location": "/moved"}, None
        )

    opener.open = redirect
    with pytest.raises(releases.FetchError, match="HTTP 302") as error:
        releases.fetch("https://example.test/a", 100, time.monotonic() + 10, headers={})
    assert error.value.redirect == "https://example.test/moved"


def test_remote_download_stops_after_the_redirect_limit(monkeypatch):
    hops = []

    def redirect(request, timeout):
        hops.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 302, "redirect", {"Location": "next"}, None)

    monkeypatch.setattr(
        releases.urllib.request, "build_opener", lambda *handlers: SimpleNamespace(open=redirect)
    )
    with pytest.raises(releases.ReleaseError, match="redirect limit"):
        releases.load_release("https://example.test/release.json")
    assert len(hops) == 6


def test_download_never_forwards_bearer_token_cross_origin(tmp_path):
    _source, raw = manifest(tmp_path)
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.server.server_port, self.path, self.headers.get("Authorization")))
            if self.path == "/release.json":
                data = json.dumps(raw).encode()
            elif self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{second.server_port}/wheel")
                self.end_headers()
                return
            else:
                data = (tmp_path / raw["wheel"]["url"]).read_bytes()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    first = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    second = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threads = [threading.Thread(target=server.serve_forever) for server in (first, second)]
    for thread in threads:
        thread.start()
    try:
        token_file = tmp_path / "release.token"
        token_file.write_text("private-beta-token")
        release = releases.load_release(
            f"http://127.0.0.1:{first.server_port}/release.json", token_file=token_file
        )
        redirected = releases.Artifact("redirect", release.wheel.sha256)
        assert releases.download_artifact(release, redirected, token_file=token_file).startswith(
            b"PK"
        )
        assert seen == [
            (first.server_port, "/release.json", "Bearer private-beta-token"),
            (first.server_port, "/redirect", "Bearer private-beta-token"),
            (second.server_port, "/wheel", None),
        ]
    finally:
        for server in (first, second):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()


def test_installer_errors_do_not_expose_output(tmp_path):
    with pytest.raises(releases.ReleaseError) as error:
        releases.run_bounded(
            [sys.executable, "-c", "print('secret-token'); raise SystemExit(7)"], cwd=tmp_path
        )
    assert str(error.value) == "Release installation failed (exit 7)."


def test_installer_output_is_bounded_and_timeout_reaps_child(tmp_path):
    output = releases.run_bounded([sys.executable, "-c", "print('x' * 50000)"], cwd=tmp_path)
    assert len(output) <= 16384
    pid_path = tmp_path / "pid"
    with pytest.raises(releases.ReleaseError, match="timed out"):
        releases.run_bounded(
            [
                sys.executable,
                "-c",
                f"import os, time; open({str(pid_path)!r}, 'w').write(str(os.getpid())); "
                "time.sleep(60)",
            ],
            cwd=tmp_path,
            timeout=0.2,
        )
    assert subprocess.run(["kill", "-0", pid_path.read_text()], capture_output=True).returncode != 0


def test_macos_zombie_group_permission_error_is_ignored_only_after_reaping_and_proving_absence(
    monkeypatch,
):
    signals = []
    reaped = []

    def killpg(pid, sent_signal):
        signals.append(sent_signal)
        if sent_signal:
            raise PermissionError("zombie group")
        raise ProcessLookupError("group reaped")

    monkeypatch.setattr(releases.os, "killpg", killpg)
    process = SimpleNamespace(pid=123, wait=lambda **kwargs: reaped.append(True))
    releases._kill_installer_group(process)
    assert reaped == [True]
    assert signals == [releases.signal.SIGKILL, 0]


def test_real_installer_group_permission_denial_is_not_suppressed(monkeypatch):
    def killpg(pid, sent_signal):
        if sent_signal:
            raise PermissionError("real permission failure")

    monkeypatch.setattr(releases.os, "killpg", killpg)
    process = SimpleNamespace(pid=123, wait=lambda **kwargs: None)
    with pytest.raises(PermissionError, match="real permission failure"):
        releases._kill_installer_group(process)
