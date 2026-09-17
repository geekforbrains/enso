"""The distributable shell installer is self-contained and respects an explicit scratch home."""

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from enso.releases import DEFAULT_FEED, ReleaseError, download_artifact, load_release

ROOT = Path(__file__).resolve().parent.parent


def standalone(tmp_path, **defaults):
    namespace = runpy.run_path(str(ROOT / "scripts/build-release.py"))
    rendered = namespace["build_installer"](**defaults)
    installer = tmp_path / "install.sh"
    installer.write_text(rendered)
    return installer


def test_distributable_contains_valid_canonical_release_code(tmp_path):
    installer = standalone(tmp_path)
    rendered = installer.read_text()
    embedded = rendered.split("<<'ENSO_PYTHON_BOOTSTRAP'\n", 1)[1].split(
        "\nENSO_PYTHON_BOOTSTRAP\n", 1
    )[0]
    assert (ROOT / "src/enso/releases.py").read_text() in embedded
    compile(embedded, "installer.py", "exec")
    subprocess.run(["sh", "-n", str(installer)], check=True, timeout=5)


def test_help_and_invalid_options_do_not_prepare_home(tmp_path):
    installer = standalone(tmp_path)
    scratch_home = tmp_path / "untouched"
    env = dict(os.environ, HOME=str(scratch_home), ENSO_HOME=str(scratch_home))
    help_result = subprocess.run(
        ["sh", str(installer), "--help"], env=env, capture_output=True, text=True, timeout=5
    )
    assert help_result.returncode == 0
    assert "--manifest" in help_result.stdout
    bad_result = subprocess.run(
        ["sh", str(installer), "--wrong"], env=env, capture_output=True, text=True, timeout=5
    )
    assert bad_result.returncode == 2
    assert not scratch_home.exists()


def test_installer_passes_paths_and_private_token_file_without_shell_evaluation(tmp_path):
    installer = standalone(tmp_path)
    tools = tmp_path / "tools"
    tools.mkdir()
    capture = tmp_path / "captured.json"
    fake_uv = tools / "uv"
    fake_uv.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"with open({str(capture)!r}, 'w') as f:\n"
        "    json.dump({'args':sys.argv[1:], 'home':os.environ['ENSO_HOME'], "
        "'python':os.environ['UV_PYTHON_INSTALL_DIR']}, f)\n"
    )
    fake_uv.chmod(0o755)
    home = tmp_path / "home with 'quotes'"
    bin_dir = tmp_path / "bin with spaces"
    token_file = tmp_path / "token $(touch not-authorized)"
    env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"])
    result = subprocess.run(
        [
            "sh",
            str(installer),
            "--home",
            str(home),
            "--bin-dir",
            str(bin_dir),
            "--manifest",
            "https://example.test/releases/release.json",
            "--token-file",
            str(token_file),
        ],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    recorded = json.loads(capture.read_text())
    assert recorded["home"] == str(home)
    assert recorded["python"] == str(home / "runtime/python")
    assert str(token_file) in recorded["args"]
    assert str(bin_dir) in recorded["args"]
    managed_uv = home / "runtime/tools/uv"
    assert recorded["args"][recorded["args"].index("--uv") + 1] == str(managed_uv)
    assert managed_uv.read_bytes() == fake_uv.read_bytes()
    assert not managed_uv.is_symlink()
    assert not (tmp_path / "not-authorized").exists()
    assert not list((home / "runtime").glob(".bootstrap.*"))


@pytest.mark.parametrize("mode", ["embedded", "override", "official"])
def test_piped_installer_uses_release_defaults_and_accepts_overrides(tmp_path, mode):
    manifest = "https://example.test/releases/v0.1.0/release.json"
    feed = None if mode == "official" else "https://example.test/releases/latest/release.json"
    installer = standalone(tmp_path, manifest=manifest, feed=feed)
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_uv = tools / "uv"
    fake_uv.write_text(
        f"#!{sys.executable}\n"
        "import json, runpy, sys\n"
        "from types import SimpleNamespace\n"
        "start = sys.argv.index('--python') + 2\n"
        "sys.argv = sys.argv[start:]\n"
        "main = runpy.run_path(sys.argv[0])['install_main']\n"
        "main.__globals__['load_release'] = lambda source, **kw: "
        "SimpleNamespace(source=source, release_id='test-release')\n"
        "main.__globals__['prepare_release'] = lambda *args, **kw: None\n"
        "main.__globals__['run_bounded'] = lambda args, **kw: json.dumps(args)\n"
        "sys.exit(main())\n"
    )
    fake_uv.chmod(0o755)
    home = tmp_path / "home"
    env = dict(
        os.environ,
        HOME=str(home),
        ENSO_HOME=str(home),
        PATH=str(tools) + os.pathsep + os.environ["PATH"],
    )
    args = ["sh", "-s", "--"]
    if mode == "override":
        manifest = str(tmp_path / "local release.json")
        feed = "https://mirror.example.test/stable/release.json"
        args.extend(["--manifest", manifest, "--feed", feed, "--extras", ""])
    result = subprocess.run(
        args,
        input=installer.read_text(),
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    command = json.loads(result.stdout)
    assert command[command.index("--manifest") + 1] == manifest
    assert command[command.index("--feed") + 1] == (feed or DEFAULT_FEED)
    assert command[command.index("--bin-dir") + 1] == str(home / ".local/bin")
    assert command[command.index("--extras") + 1] == (
        "" if mode == "override" else "slack,telegram,web"
    )
    assert not list((home / "runtime").glob(".bootstrap.*"))

    # A later invocation keeps the home's uv even when the shell finds another copy first.
    fake_uv.write_text("#!/bin/sh\nexit 99\n")
    repeated = subprocess.run(
        args,
        input=installer.read_text(),
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout) == command


def test_local_bundle_still_requires_manifest_without_preparing_home(tmp_path):
    installer = standalone(tmp_path)
    home = tmp_path / "untouched"
    result = subprocess.run(
        ["sh", str(installer)],
        env=dict(os.environ, HOME=str(home), ENSO_HOME=str(home)),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert "--manifest" in result.stderr
    assert not home.exists()


def test_release_builder_refuses_dirty_checkout_and_nonempty_output(tmp_path, monkeypatch):
    namespace = runpy.run_path(str(ROOT / "scripts/build-release.py"))
    build = namespace["build"]
    monkeypatch.setitem(build.__globals__, "run_bounded", lambda *args, **kwargs: " M source.py")
    output = tmp_path / "release"
    with pytest.raises(ReleaseError, match="clean checkout"):
        build(output)
    output.mkdir()
    sentinel = output / "release.json"
    sentinel.write_text("existing")
    with pytest.raises(ReleaseError, match="must be empty"):
        build(output, allow_dirty=True)
    assert sentinel.read_text() == "existing"


@pytest.fixture
def release_builder(tmp_path, monkeypatch):
    namespace = runpy.run_path(str(ROOT / "scripts/build-release.py"))
    build = namespace["build"]
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nversion = "0.1.0"\nrequires-python = ">=3.14"\n'
    )
    for relative in (
        "scripts/installer-header.sh",
        "scripts/install-release.py",
        "src/enso/releases.py",
    ):
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / relative).read_text())
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        assert kwargs["cwd"] == project
        if args[:2] == ["git", "rev-parse"]:
            return "a" * 40
        if args[:2] == ["uv", "build"]:
            staging = Path(args[args.index("--out-dir") + 1])
            (staging / "enso-0.1.0-py3-none-any.whl").write_bytes(b"wheel contents")
            (staging / ".gitignore").write_text("*")
            (staging / "build-cache").mkdir()
        elif args[:2] == ["uv", "export"]:
            Path(args[args.index("--output-file") + 1]).write_text("typer==0.12.0\n")
        return ""

    monkeypatch.setitem(build.__globals__, "ROOT", project)
    monkeypatch.setitem(build.__globals__, "run_bounded", run)
    return build, commands


@pytest.mark.parametrize(
    "base",
    [
        None,
        "https://github.com/geekforbrains/enso/releases/download/v0.1.0",
        "https://downloads.example.test/releases/0.1.0/",
    ],
)
def test_builder_emits_only_release_artifacts_with_matching_pinned_urls(
    tmp_path, release_builder, base
):
    build, _commands = release_builder
    output = tmp_path / "output"
    raw = build(output, artifact_base=base)
    prefix = base.rstrip("/") + "/" if base else ""
    assert raw["wheel"]["url"] == prefix + "enso-0.1.0-py3-none-any.whl"
    assert raw["constraints"]["url"] == prefix + "constraints.txt"
    assert raw["commit"] == "a" * 40
    assert {path.name for path in output.iterdir()} == {
        "enso-0.1.0-py3-none-any.whl",
        "constraints.txt",
        "release.json",
        "install.sh",
    }
    release = load_release(output / "release.json")
    assert release.to_dict() == raw
    assert (output / "install.sh").stat().st_mode & 0o111
    rendered = (output / "install.sh").read_text()
    if base:
        assert f"set -- --manifest {prefix}release.json" in rendered
    else:
        assert "set -- --manifest" not in rendered
    if base is None:
        assert download_artifact(release, release.wheel) == b"wheel contents"
        assert download_artifact(release, release.constraints) == b"typer==0.12.0\n"


@pytest.mark.parametrize(
    "base",
    [
        "",
        "/tmp/releases/v0.1.0",
        "http://127.0.0.1/v0.1.0",
        "https://secret@example.test/v0.1.0",
        "https://example.test/v0.1.0?token=secret",
        "https://example.test/v0.1.0?",
        "https://example.test/v0.1.0#secret",
        "https://example.test/../v0.1.0",
        "https://example.test/%2e%2e/v0.1.0",
        "https://example.test/%252e%252e/v0.1.0",
        "https://example.test/a\\b/v0.1.0",
        "https://example.test//v0.1.0",
        "https://example.test/releases/latest",
        "https://example.test/v0.2.0",
    ],
)
def test_builder_rejects_unsafe_or_unpinned_artifact_bases_before_building(
    tmp_path, release_builder, base
):
    build, commands = release_builder
    output = tmp_path / "output"
    with pytest.raises(ReleaseError) as error:
        build(output, artifact_base=base)
    assert "secret" not in str(error.value)
    assert all(command[0] != "uv" for command in commands)
    assert not output.exists()


def test_builder_embeds_update_feed_alongside_pinned_manifest(tmp_path, release_builder):
    build, _commands = release_builder
    output = tmp_path / "output"
    build(
        output,
        artifact_base="https://example.test/v0.1.0",
        feed="https://example.test/latest/release.json",
    )
    assert (
        "set -- --manifest https://example.test/v0.1.0/release.json "
        '--feed https://example.test/latest/release.json "$@"'
    ) in (output / "install.sh").read_text()


@pytest.mark.parametrize(
    ("base", "feed"),
    [
        (None, "https://example.test/latest/release.json"),
        ("https://example.test/v0.1.0", "./release.json"),
        ("https://example.test/v0.1.0", "http://127.0.0.1/release.json"),
        ("https://example.test/v0.1.0", "https://secret@example.test/release.json"),
        ("https://example.test/v0.1.0", "https://example.test/release.json?token=secret"),
    ],
)
def test_builder_rejects_invalid_installer_feeds_before_building(
    tmp_path, release_builder, base, feed
):
    build, commands = release_builder
    output = tmp_path / "output"
    with pytest.raises(ReleaseError) as error:
        build(output, artifact_base=base, feed=feed)
    assert "secret" not in str(error.value)
    assert all(command[0] != "uv" for command in commands)
    assert not output.exists()


@pytest.mark.parametrize("feed", [None, "./stable/release.json"])
def test_bootstrap_defaults_or_normalizes_feed_before_changing_home(tmp_path, monkeypatch, feed):
    namespace = runpy.run_path(str(ROOT / "scripts/install-release.py"))
    main = namespace["install_main"]
    monkeypatch.chdir(tmp_path)
    calls = []
    release = SimpleNamespace(
        release_id="0.2.0-aaaaaaaaaaaa", source=str(tmp_path / "release.json")
    )
    monkeypatch.setitem(main.__globals__, "load_release", lambda *args, **kwargs: release)
    monkeypatch.setitem(main.__globals__, "prepare_release", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        main.__globals__, "ensure_uv", lambda runtime, source: str(runtime / "tools/uv")
    )
    monkeypatch.setitem(
        main.__globals__, "run_bounded", lambda args, **kwargs: calls.append((args, kwargs)) or "{}"
    )
    home = tmp_path / "different-home"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "installer",
            "--manifest",
            "./release.json",
            "--home",
            str(home),
            "--bin-dir",
            str(tmp_path / "bin"),
        ]
        + (["--feed", feed] if feed else []),
    )
    assert main() == 0
    command, options = calls[0]
    assert command[command.index("--feed") + 1] == (
        str(tmp_path / "stable/release.json") if feed else DEFAULT_FEED
    )
    assert options["cwd"] == home
    assert options["env"]["PATH"].split(os.pathsep)[0] == str(home / "runtime/tools")


def test_bootstrap_rejects_invalid_feed_before_downloading_release(tmp_path, monkeypatch, capsys):
    namespace = runpy.run_path(str(ROOT / "scripts/install-release.py"))
    main = namespace["install_main"]
    monkeypatch.setitem(
        main.__globals__, "load_release", lambda *args, **kwargs: pytest.fail("must not download")
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "installer",
            "--manifest",
            "https://example.test/release.json",
            "--home",
            str(tmp_path / "home"),
            "--bin-dir",
            str(tmp_path / "bin"),
            "--feed",
            "https://example.test/release.json?token=private-token",
        ],
    )
    assert main() == 1
    assert "private-token" not in capsys.readouterr().err
    assert not (tmp_path / "home").exists()
