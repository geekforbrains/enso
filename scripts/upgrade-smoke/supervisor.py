#!/usr/bin/env python3
"""Test-only service manager: independent subprocess groups behind systemctl-shaped calls."""

from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import socketserver
import subprocess
import sys
from pathlib import Path


class Supervisor(socketserver.UnixStreamServer):
    def __init__(self, path):
        self.children = {}
        self.logs = []
        super().__init__(str(path), Handler)

    def start(self, name, env, command=None):
        previous = self.children.get(name)
        if previous and previous.poll() is None:
            return previous.pid
        if command is None:
            unit = Path(env["HOME"]) / ".config/systemd/user" / name
            for line in unit.read_text().splitlines():
                if line.startswith("ExecStart="):
                    command = shlex.split(line.removeprefix("ExecStart="))
                if line.startswith("Environment="):
                    for item in shlex.split(line.removeprefix("Environment=")):
                        key, _, value = item.partition("=")
                        env[key] = value
        if not command:
            raise ValueError("no command in unit")
        log = (Path(env["HOME"]) / f"smoke-{name}.log").open("ab")
        self.logs.append(log)
        child = subprocess.Popen(
            command,
            env=env,
            cwd=env["HOME"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.children[name] = child
        return child.pid

    def stop(self, name):
        child = self.children.get(name)
        if child and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=10)

    def invoke(self, request):
        args, env = request["args"], request["env"]
        if request["tool"] == "inspect":
            return 0, json.dumps(
                {name: child.pid for name, child in self.children.items() if child.poll() is None}
            )
        if request["tool"] == "cleanup":
            for name in self.children:
                self.stop(name)
            return 0, ""
        if request["tool"] == "systemd-run":
            if "--" in args:
                command = args[args.index("--") + 1 :]
            else:
                index = next(i for i, arg in enumerate(args) if arg.startswith("/"))
                command = args[index:]
            name = next(
                (arg.split("=", 1)[1] for arg in args if arg.startswith("--unit=")),
                "enso-updater.service",
            )
            if "--unit" in args:
                name = args[args.index("--unit") + 1]
            self.start(name, env, command)
            return 0, ""
        verb = next((arg for arg in args if not arg.startswith("-")), "")
        name = next((arg for arg in args if arg.endswith(".service")), "enso.service")
        child = self.children.get(name)
        alive = child is not None and child.poll() is None
        if verb == "show":
            return 0, str(child.pid if alive else 0) + "\n"
        if verb in ("is-active", "is-enabled"):
            return (0 if alive else 3), ("active\n" if verb == "is-active" else "enabled\n")
        if verb in ("stop", "restart", "disable"):
            self.stop(name)
        if verb in ("start", "restart", "enable"):
            self.start(name, env)
        return 0, ""


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            code, output = self.server.invoke(json.loads(self.rfile.readline(65536)))
            result = {"code": code, "output": output}
        except Exception as exc:
            result = {"code": 1, "output": str(exc)}
        self.wfile.write(json.dumps(result).encode() + b"\n")


def main():
    path = Path(os.environ["SMOKE_SUPERVISOR_SOCKET"])
    if Path(sys.argv[0]).name in ("systemctl", "systemd-run"):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(path))
            connection.sendall(
                json.dumps(
                    {"tool": Path(sys.argv[0]).name, "args": sys.argv[1:], "env": dict(os.environ)}
                ).encode()
                + b"\n"
            )
            result = json.loads(connection.makefile("rb").readline())
        sys.stdout.write(result["output"])
        raise SystemExit(result["code"])
    with Supervisor(path) as server:
        try:
            server.serve_forever()
        finally:
            for name in server.children:
                server.stop(name)


if __name__ == "__main__":
    main()
