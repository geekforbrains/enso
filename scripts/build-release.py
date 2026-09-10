#!/usr/bin/env python3
"""Build the immutable release artifacts consumed by native installs and Enso Cloud."""

import argparse
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from enso.releases import ReleaseError, load_release, normalize_source, run_bounded  # noqa: E402


def build_installer(*, manifest: str | None = None, feed: str | None = None) -> str:
    """Embed canonical code at build time so a published installer needs no repository access."""
    header = (ROOT / "scripts/installer-header.sh").read_text()
    release_code = (ROOT / "src/enso/releases.py").read_text()
    bootstrap = (ROOT / "scripts/install-release.py").read_text()
    defaults = []
    if manifest:
        defaults.extend(["--manifest", manifest])
    if feed:
        defaults.extend(["--feed", feed])
    if defaults:
        # Explicit options follow the release defaults, so argparse gives them precedence.
        header = (
            "#!/bin/sh\nset -- "
            + shlex.join(defaults)
            + ' "$@"\n'
            + header.removeprefix("#!/bin/sh\n")
        )
    return (
        header
        + "\ncat > \"$enso_bootstrap/install.py\" <<'ENSO_PYTHON_BOOTSTRAP'\n"
        + release_code
        + "\n"
        + bootstrap
        + "\nENSO_PYTHON_BOOTSTRAP\n"
        + '"$enso_uv" run --no-project --no-config --python 3.14 '
        + '"$enso_bootstrap/install.py" --home "$enso_home" '
        + '--bin-dir "$enso_bin_dir" --uv "$enso_uv" "$@"\n'
    )


def artifact_base_url(value: str | None, version: str) -> str:
    """Keep moving feeds pinned to an explicit, unambiguous release directory."""
    if value is None:
        return ""
    normalize_source(value)
    parsed = urlsplit(value)
    parts = parsed.path.removeprefix("/").removesuffix("/").split("/")
    if (
        parsed.scheme != "https"
        or "?" in value
        or "#" in value
        or "\\" in value
        or any(not re.fullmatch(r"[A-Za-z0-9._~-]+", part) or part in {".", ".."} for part in parts)
        or parts[-1] not in {version, f"v{version}"}
    ):
        raise ReleaseError(
            "Artifact base URL must be HTTPS with an unescaped version directory ending in "
            "the package version or v<version>, without credentials, queries, or fragments."
        )
    return value.removesuffix("/") + "/"


def build(
    output: Path,
    *,
    allow_dirty: bool = False,
    notes: str | None = None,
    artifact_base: str | None = None,
    feed: str | None = None,
) -> dict:
    if output.exists() and any(output.iterdir()):
        raise ReleaseError("Release output directory must be empty.")
    if not allow_dirty and run_bounded(["git", "status", "--porcelain"], cwd=ROOT):
        raise ReleaseError(
            "Release builds require a clean checkout; use --allow-dirty for local tests."
        )
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", project["version"]):
        raise ReleaseError("Managed release builds require a stable major.minor.patch version.")
    base_url = artifact_base_url(artifact_base, project["version"])
    if feed is not None:
        if not base_url:
            raise ReleaseError("An installer feed requires --artifact-base-url.")
        feed = normalize_source(feed)
        if urlsplit(feed).scheme != "https":
            raise ReleaseError("The installer feed must be an HTTPS URL.")
    commit = run_bounded(["git", "rev-parse", "HEAD"], cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix="enso-release-") as temporary:
        staging = Path(temporary)
        run_bounded(["uv", "build", "--wheel", "--out-dir", str(staging)], cwd=ROOT)
        wheels = list(staging.glob("*.whl"))
        if len(wheels) != 1:
            raise ReleaseError("Release build must produce exactly one wheel.")
        wheel = wheels[0]
        constraints = staging / "constraints.txt"
        run_bounded(
            [
                "uv",
                "export",
                "--locked",
                "--all-extras",
                "--no-dev",
                "--no-emit-project",
                "--no-hashes",
                "--no-header",
                "--no-annotate",
                "--output-file",
                str(constraints),
            ],
            cwd=ROOT,
        )
        manifest = {
            "schema_version": 1,
            "version": project["version"],
            "commit": commit,
            "requires_python": project["requires-python"],
            "wheel": {
                "url": base_url + wheel.name,
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            },
            "constraints": {
                "url": base_url + constraints.name,
                "sha256": hashlib.sha256(constraints.read_bytes()).hexdigest(),
            },
        }
        if notes:
            manifest["release_notes_url"] = notes
        manifest_path = staging / "release.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        load_release(manifest_path)
        installer = staging / "install.sh"
        installer.write_text(
            build_installer(manifest=base_url + "release.json" if base_url else None, feed=feed)
        )
        installer.chmod(0o755)
        subprocess.run(["sh", "-n", str(installer)], check=True, cwd=ROOT, timeout=10)
        output.mkdir(parents=True, exist_ok=True)
        for artifact in (wheel, constraints, manifest_path, installer):
            shutil.copy2(artifact, output / artifact.name)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--allow-dirty", action="store_true", help="Local tests only, never publish"
    )
    parser.add_argument("--release-notes-url")
    parser.add_argument(
        "--artifact-base-url", help="Immutable HTTPS directory ending in this version or v<version>"
    )
    parser.add_argument("--feed-url", help="HTTPS update feed to save in the generated installer")
    args = parser.parse_args()
    try:
        manifest = build(
            args.output.resolve(),
            allow_dirty=args.allow_dirty,
            notes=args.release_notes_url,
            artifact_base=args.artifact_base_url,
            feed=args.feed_url,
        )
    except (ReleaseError, OSError, subprocess.SubprocessError) as exc:
        print(f"Release build failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"version": manifest["version"], "output": str(args.output.resolve())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
