"""``python -m enso.web``: the viewer process itself, never the CLI.

``enso web start`` runs this in the foreground or spawns it in the background, so the
long-running process carries none of the CLI's runtime and transport imports.
"""

from __future__ import annotations

import argparse
import sys

from ..config import Paths, valid_port
from . import INSTALL_HINT, missing_extra, resolve_bind


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m enso.web", description="Serve Enso's web viewer."
    )
    parser.add_argument("--host", help="Bind address; default from config.json, else 127.0.0.1.")
    parser.add_argument("--port", type=int, help="TCP port; default from config.json, else 8787.")
    args = parser.parse_args(argv)
    if args.port is not None and not valid_port(args.port):
        parser.error("--port must be an integer from 1 through 65535")
    missing = missing_extra()
    if missing:
        print(f"error: the web viewer needs {', '.join(missing)}; {INSTALL_HINT}", file=sys.stderr)
        return 1
    paths = Paths.from_env()
    bind, _problems = resolve_bind(paths, args.host, args.port)
    from .server import serve

    return serve(paths, bind)


if __name__ == "__main__":
    sys.exit(main())
