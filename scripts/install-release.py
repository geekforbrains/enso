"""Bootstrap a managed Enso install using the same release code shipped in Enso."""

import argparse
import sys
from pathlib import Path

if "ReleaseError" not in globals():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from enso.releases import (
        ReleaseError,
        load_release,
        normalize_source,
        prepare_release,
        run_bounded,
    )


def install_main() -> int:
    parser = argparse.ArgumentParser(description="Install a verified Enso release with uv.")
    parser.add_argument("--manifest", required=True, help="Trusted release.json path or HTTPS URL")
    parser.add_argument("--home", required=True)
    parser.add_argument("--bin-dir", required=True)
    parser.add_argument("--token-file")
    parser.add_argument("--feed")
    parser.add_argument("--viewer-service")
    parser.add_argument("--extras", default="slack,telegram,web")
    parser.add_argument("--adopt", action="store_true")
    parser.add_argument("--uv", default="uv")
    args = parser.parse_args()
    home = Path(args.home).expanduser().resolve()
    bin_dir = Path(args.bin_dir).expanduser().resolve()
    try:
        if args.feed is not None:
            args.feed = normalize_source(args.feed)
        release = load_release(args.manifest, token_file=args.token_file)
        release_dir = home / "runtime" / "releases" / release.release_id
        extras = tuple(item.strip() for item in args.extras.split(",") if item.strip())
        prepare_release(release, release_dir, extras=extras, uv=args.uv, token_file=args.token_file)
        command = [
            str(release_dir / "bin/enso"),
            "update",
            "install",
            "--manifest",
            release.source,
            "--prepared-release",
            str(release_dir),
            "--bin-dir",
            str(bin_dir),
            "--extras",
            ",".join(extras),
            "--json",
        ]
        for name in ("token_file", "feed", "viewer_service"):
            if value := getattr(args, name):
                command.extend(["--" + name.replace("_", "-"), value])
        if args.adopt:
            command.append("--adopt")
        import os

        env = dict(os.environ, ENSO_HOME=str(home))
        env["PATH"] = str(Path(args.uv).resolve().parent) + os.pathsep + env.get("PATH", "")
        result = run_bounded(command, cwd=home, env=env)
        print(result)
        return 0
    except (ReleaseError, OSError) as exc:
        print(f"Enso install failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(install_main())
