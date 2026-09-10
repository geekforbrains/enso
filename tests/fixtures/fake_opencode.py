#!/usr/bin/env python3
"""Stand-in for ``opencode`` used by runtime and job tests.

It emits the JSONL shape from ``opencode run --format json``. A fresh run mints
``NEW_SESSION`` and ``-s <id>`` resumes it. A prompt starting with ``earlyfail`` fails with
an error event before any step, the way a run without credentials does.
``FAKE_OPENCODE_ARGV=<path>`` records one JSON argv list per run. Its answer reports the
project root resolved the way OpenCode 1.18.26 does, so a test can see which directory a run
actually worked in.
"""

import json
import os
import sys
import time

NEW_SESSION = "ses_11111111111111111111111111"

args = sys.argv[1:]

if args[:2] == ["session", "delete"]:
    print(f"Session {args[2]} deleted")
    sys.exit(0)

if argv_log := os.environ.get("FAKE_OPENCODE_ARGV"):
    with open(argv_log, "a") as handle:
        handle.write(json.dumps(args) + "\n")


def value(flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def root() -> str:
    """Resolve the project root as OpenCode does: --dir wins, else $PWD, else the cwd."""
    return os.path.realpath(value("--dir") or os.environ.get("PWD") or os.getcwd())


prompt = args[args.index("--") + 1]
# Directives come from the user's own paragraph: a chat prompt opens with Enso's
# origin block, while a job prompt is nothing but its own text.
directive = prompt.rsplit("\n\n", 1)[-1]
resumed = value("-s")
session = resumed or NEW_SESSION
mode = "resumed" if resumed else "new"

if directive.startswith("earlyfail"):
    # OpenCode created the session, then failed before its first step: the error is the
    # only event carrying the id.
    emit(
        {
            "type": "error",
            "sessionID": session,
            "error": {"name": "FakeError", "data": {"message": "fake: no credentials"}},
        }
    )
    sys.exit(1)

emit({"type": "step_start", "sessionID": session, "part": {"type": "step-start"}})
emit(
    {
        "type": "tool_use",
        "sessionID": session,
        "part": {
            "type": "tool",
            "tool": "read",
            "state": {"status": "completed", "input": {"filePath": "/ws/AGENTS.md"}},
        },
    }
)
if directive.startswith("sleep "):
    time.sleep(float(directive.split()[1]))
if directive.startswith("fail"):
    emit(
        {
            "type": "error",
            "sessionID": session,
            "error": {"name": "FakeError", "data": {"message": "fake: boom"}},
        }
    )
    sys.exit(1)

if os.environ.get("ENSO_JOB"):
    answer = (
        f"batch job={os.environ.get('ENSO_JOB', '')} "
        f"run={os.environ.get('ENSO_RUN_ID', '')} "
        f"workspace={os.environ.get('ENSO_WORKSPACE', '')} root={root()} prompt={prompt}"
    )
else:
    answer = (
        f"{mode} {session} workspace={os.environ.get('ENSO_WORKSPACE', '')} "
        f"root={root()} prompt={prompt}"
    )
emit({"type": "text", "sessionID": session, "part": {"type": "text", "text": answer}})
emit({"type": "step_finish", "sessionID": session, "part": {"type": "step-finish"}})
