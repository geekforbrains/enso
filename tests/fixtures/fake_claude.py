#!/usr/bin/env python3
"""Stand-in for ``claude -p`` used by runtime and job tests.

Streams ``stream-json`` events, or plain text with ``--output-format text`` (jobs).
Prompt directives: ``unrecognized`` emits only an unknown JSON event;
``sleep N`` waits N seconds before answering; ``fail [TEXT]`` prints
TEXT (batch mode) then exits 1.
Environment: ``FAKE_FAIL=1`` exits 2 without printing anything (a launch that never
created its session); ``FAKE_RESPONSES=<dir>`` answers with the directory's files in
name order, one per invocation, until they run out; ``FAKE_SESSION_ID=<id>`` reports that
id in the result event instead of the one Enso assigned.
"""

import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
if os.environ.get("FAKE_FAIL"):
    sys.stderr.write("fake: launch failed\n")
    sys.exit(2)

prompt = args[args.index("--") + 1]
# Directives come from the user's own paragraph: a chat prompt opens with Enso's
# origin block, while a job prompt is nothing but its own text.
directive = prompt.rsplit("\n\n", 1)[-1]
batch = args[args.index("--output-format") + 1] == "text"
session = args[args.index("--session-id") + 1] if "--session-id" in args else None
resumed = args[args.index("--resume") + 1] if "--resume" in args else None
session_id = session or resumed or "none"
announced = os.environ.get("FAKE_SESSION_ID") or session_id
mode = "resumed" if resumed else "new"


def scripted_response(default: str) -> str:
    scripted = sorted(Path(os.environ.get("FAKE_RESPONSES", "/nonexistent")).glob("*"))
    if not scripted:
        return default
    result = scripted[0].read_text()
    scripted[0].unlink()
    return result


if batch:
    print("batch: working", flush=True)
    if directive.startswith("sleep "):
        time.sleep(float(directive.split()[1]))
    if directive.startswith("fail"):
        if payload := directive[4:].strip():
            print(payload, flush=True)  # flushed so it lands before the stderr line
        sys.stderr.write("fake: boom\n")
        sys.exit(1)
    env = os.environ
    print(
        scripted_response(
            f"batch job={env.get('ENSO_JOB', '')} run={env.get('ENSO_RUN_ID', '')} "
            f"workspace={env.get('ENSO_WORKSPACE', '')} prompt={prompt}"
        )
    )
    sys.exit(0)

if directive == "unrecognized":
    print("{}")
    sys.exit(0)

print(json.dumps({"type": "system", "subtype": "init", "session_id": session_id}), flush=True)
print(
    json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}]
            },
        }
    ),
    flush=True,
)
if directive.startswith("sleep "):
    time.sleep(float(directive.split()[1]))
if directive.startswith("fail"):
    sys.stderr.write("fake: boom\n")
    sys.exit(1)
result = scripted_response(
    f"{mode} {session_id} workspace={os.environ.get('ENSO_WORKSPACE', '')} prompt={prompt}"
)
print(json.dumps({"type": "result", "result": result, "session_id": announced}), flush=True)
