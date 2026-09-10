"""Container-only Slack replacement: real runtime turns over a private local socket."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from enso import __version__
from enso.transports import Reply, Transport, Turn


class SmokeReply(Reply):
    def __init__(self):
        self.messages = []

    async def send(self, text):
        self.messages.append(text)
        return str(len(self.messages))

    async def send_file(self, path, caption=""):
        return "file"

    async def status_post(self, text):
        return "status"

    async def status_edit(self, message_id, text):
        pass

    async def status_delete(self, message_id):
        pass


class SlackTransport(Transport):
    name = "slack"

    def __init__(self, config, paths):
        self.paths = paths

    async def start(self, runtime):
        with (self.paths.home / "smoke-start-attempts.jsonl").open("a") as output:
            output.write(json.dumps({"version": __version__, "pid": os.getpid()}) + "\n")
        socket_path = self.paths.home / "smoke-transport.sock"
        socket_path.unlink(missing_ok=True)
        release_ready = asyncio.Event()

        async def handle(reader, writer):
            try:
                request = json.loads(await reader.readline())
                if request.get("action") == "release_ready":
                    release_ready.set()
                    result = {"ok": True}
                elif request.get("action") == "update":
                    process = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "enso.cli",
                        "update",
                        "apply",
                        "--manifest",
                        request["manifest"],
                        "--startup-timeout",
                        "30",
                        "--json",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        start_new_session=True,
                    )
                    stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
                    result = {
                        "returncode": process.returncode,
                        "result": json.loads(stdout),
                        "stderr": stderr.decode(),
                        "requester_cgroup": Path("/proc/self/cgroup").read_text(),
                    }
                elif request.get("action") == "turn":
                    reply = SmokeReply()
                    await runtime.handle(
                        Turn(
                            transport="slack",
                            channel="D1",
                            thread=None,
                            message_id="1.0",
                            user_id="U1",
                            user_name="smoke",
                            text=request["text"],
                            is_dm=True,
                        ),
                        reply,
                    )
                    result = {"messages": reply.messages}
                else:
                    result = {"version": __version__, "pid": os.getpid()}
                writer.write(json.dumps(result).encode() + b"\n")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_unix_server(handle, path=socket_path)
        try:
            async with server:
                if (self.paths.home / "smoke-hold-ready").exists():
                    (self.paths.home / "smoke-waiting-ready.json").write_text(
                        json.dumps({"version": __version__, "pid": os.getpid()})
                    )
                    await release_ready.wait()
                runtime.transport_ready(self.name)
                await server.serve_forever()
        finally:
            socket_path.unlink(missing_ok=True)

    async def send(self, target, text, *, thread=None):
        with (self.paths.home / "smoke-messages.jsonl").open("a") as output:
            output.write(json.dumps({"target": target, "text": text}) + "\n")
        return "1.0"

    async def send_file(self, target, path, *, caption="", thread=None):
        return "file"

    async def edit(self, target, message_id, text):
        pass

    async def delete(self, target, message_id):
        pass

    async def fetch_thread(self, target, thread):
        return []
