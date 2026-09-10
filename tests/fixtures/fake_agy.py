#!/usr/bin/env python3
"""Stand-in for ``agy`` used by runtime tests; the real CLI is never run by a test.

Streams Antigravity's ``stream-json`` envelope, or prints the plain answer with
``--output-format text`` (jobs). A fresh launch mints ``NEW_CONVERSATION``; ``--conversation
<id>`` keeps that id, which is how a resume is told apart.
Environment: ``FAKE_AGY_ARGV=<path>`` appends one JSON argv list per invocation, so a test
can assert the project and conversation flags; ``FAKE_RESPONSES=<dir>`` answers with the
directory's files in name order, one per invocation; ``FAKE_CONVERSATION=<id>`` mints that
id instead of ``NEW_CONVERSATION``.
"""

import json
import os
import sys
import time
from pathlib import Path

NEW_CONVERSATION = "11111111-1111-1111-1111-111111111111"

args = sys.argv[1:]
if argv_log := os.environ.get("FAKE_AGY_ARGV"):
    with open(argv_log, "a") as handle:
        handle.write(json.dumps(args) + "\n")


def value(flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


prompt = next(arg[len("--prompt=") :] for arg in args if arg.startswith("--prompt="))
# Directives come from the user's own paragraph: a chat prompt opens with Enso's
# origin block, while a job prompt is nothing but its own text.
directive = prompt.rsplit("\n\n", 1)[-1]
batch = value("--output-format") == "text"
resumed = value("--conversation")
conversation = resumed or os.environ.get("FAKE_CONVERSATION") or NEW_CONVERSATION


def scripted_response(default: str) -> str:
    scripted = sorted(Path(os.environ.get("FAKE_RESPONSES", "/nonexistent")).glob("*"))
    if not scripted:
        return default
    result = scripted[0].read_text()
    scripted[0].unlink()
    return result


answer = scripted_response(
    f"{'resumed' if resumed else 'new'} {conversation} "
    f"workspace={os.environ.get('ENSO_WORKSPACE', '')} prompt={prompt}"
)

if batch:
    if directive.startswith("sleep "):
        time.sleep(float(directive.split()[1]))
    print(answer)
    sys.exit(0)


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


emit({"event": "init", "conversation_id": conversation, "init": {"cwd": os.getcwd()}})
emit(
    {
        "event": "step_update",
        "step_update": {
            "conversation_id": conversation,
            "step_index": 1,
            "state": "ACTIVE",
            "step_type": "tool",
            "tool_name": "view_file",
            "tool_info": {"name": "view_file", "parameters": {"AbsolutePath": "/ws/AGENTS.md"}},
        },
    }
)
if directive.startswith("sleep "):
    time.sleep(float(directive.split()[1]))
if directive.startswith("fail"):
    emit({"event": "result", "result": {"conversation_id": conversation, "status": "ERROR",
                                        "response": "", "error": "fake: boom"}})  # fmt: skip
    sys.exit(1)
emit(
    {
        "event": "result",
        "result": {"conversation_id": conversation, "status": "SUCCESS", "response": answer},
    }
)
