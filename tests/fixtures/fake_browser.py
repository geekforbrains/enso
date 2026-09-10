"""Executable Chrome/Playwright stand-ins for installed browser-helper tests.

Copied under the names ``chrome`` and ``node`` with the test Python's shebang. All
requests stay on loopback; the MCP stand-in exercises HTTP discovery, not WebSockets.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


def record(kind, **values):
    target = Path(os.environ["ENSO_BROWSER_TEST_EVENTS"]) / f"{kind}-{os.getpid()}.json"
    pending = target.with_suffix(".tmp")
    pending.write_text(json.dumps({"kind": kind, "pid": os.getpid(), **values}))
    pending.replace(target)


def fetch(address, path):
    parsed = urlsplit(address)
    assert parsed.hostname == "127.0.0.1"
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=20)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = json.loads(response.read())
        if response.status != 200:
            raise RuntimeError(body)
        return body
    finally:
        connection.close()


def chrome():
    assert "--remote-debugging-port=0" in sys.argv
    data = Path(
        next(arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--user-data-dir="))
    )
    websocket = f"/devtools/browser/fake-{os.getpid()}"
    ready = os.environ.get("ENSO_BROWSER_TEST_READY")
    tabs = [{"url": "about:blank"}]
    stopped = False

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal stopped
            status = 200
            if self.path == "/json/version":
                if ready and not Path(ready).exists():
                    record("chrome-unready", data=str(data), port=server.server_port)
                    status = 503
                    result = {"error": "test Chrome has not become ready"}
                else:
                    result = {
                        "webSocketDebuggerUrl": f"ws://127.0.0.1:{server.server_port}{websocket}"
                    }
            elif self.path == "/json/list":
                result = tabs
            elif self.path == "/__test/stop":
                stopped = True
                result = {"stopped": True}
            elif self.command == "PUT" and self.path.startswith("/json/new?"):
                result = {"url": unquote(self.path.split("?", 1)[1])}
                tabs.append(result)
            else:
                self.send_error(404)
                return
            encoded = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_PUT(self):
            self.do_GET()

        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        record("chrome", data=str(data), port=server.server_port)
        (data / "DevToolsActivePort").write_text(f"{server.server_port}\n{websocket}\n")
        while not stopped:
            server.handle_request()


def mcp():
    address = sys.argv[sys.argv.index("--cdp-endpoint") + 1]
    output = sys.argv[sys.argv.index("--output-dir") + 1]
    record("mcp", address=address, output=output, cwd=str(Path.cwd()))
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        method = message["method"]
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}
        elif method == "tools/list":
            result = {"tools": [{"name": "browser_tabs", "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":
            try:
                path = urlsplit(address).path.rstrip("/") + "/json/version/"
                discovery = fetch(address, path)
                tabs = fetch(discovery["webSocketDebuggerUrl"], "/json/list")
                result = {"content": [{"type": "text", "text": json.dumps(tabs)}]}
            except (RuntimeError, OSError, http.client.HTTPException) as exc:
                result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
        else:
            raise AssertionError(f"unexpected method: {method}")
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)


if __name__ == "__main__":
    chrome() if Path(sys.argv[0]).name == "chrome" else mcp()
