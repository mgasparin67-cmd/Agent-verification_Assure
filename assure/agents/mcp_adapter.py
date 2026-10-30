"""MCP adapter: lets any MCP-capable agent be tested, unchanged.

The orchestrator starts this module as "the agent" (stdio JSON-lines protocol to the gateway, as
always). It then:

1. serves the gateway's tools, plus `report_completion`, as an MCP server over Streamable HTTP on
   127.0.0.1, behind a random bearer token;
2. writes an MCP client config (`assure-mcp.json`) into the agent's working directory;
3. launches the real agent from a command template, filling in {prompt}, {instruction},
   {mcp_url}, {mcp_token}, {mcp_config} and {python} (each placeholder is replaced inside one argv element;
   no shell is involved);
4. relays every MCP `tools/call` to the gateway and returns the gateway's answer, refusals included.

Per-step agent events are emitted mechanically from each gateway response, as the Claude bridge
does, so step telemetry is complete by construction. The final claim is the agent's own
`report_completion` call. If the agent exits without one, no final claim is sent: the run's
evidence is then incomplete (INCONCLUSIVE), which is the honest result.

The MCP surface is the handshake protocol (initialize, tools/list, tools/call, ping), versions
2024-11-05 to 2025-11-25, answered as JSON (no SSE stream). It is implemented here directly, with no
SDK dependency. The test suite checks it against the official MCP Python SDK client.

The real agent's stdout and stderr go to this process's stderr (captured as agent_stderr.log);
stdout is the gateway channel and must stay clean.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from assure import __version__
from assure.adapter import GatewayClient
from assure.agents.common import COMPLETION_TOOL, SYSTEM, completion_problems

PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
SERVER_NAME = "assure"
CONFIG_NAME = "assure-mcp.json"


def log(msg: str) -> None:
    print(f"[mcp-adapter] {msg}", file=sys.stderr, flush=True)


class Relay:
    """Turns MCP tool calls into gateway tool requests, one at a time."""

    def __init__(self, gw: GatewayClient, task: dict[str, Any]):
        self.gw, self.task = gw, task
        self.lock = threading.Lock()
        self.step, self.prev = 0, None
        self.completion: dict[str, Any] | None = None
        self.tools = [{"name": t["name"], "description": t["description"], "inputSchema": t["schema"]}
                      for t in task["tools"]]
        self.tools.append({"name": COMPLETION_TOOL["name"], "description": COMPLETION_TOOL["description"],
                           "inputSchema": COMPLETION_TOOL["input_schema"]})

    def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Returns (text content, is_error)."""
        with self.lock:
            if name == COMPLETION_TOOL["name"]:
                if self.completion is not None:
                    return "Not recorded: report_completion was already called.", True
                problems = completion_problems(args)
                if problems:
                    log(f"report_completion rejected: {'; '.join(problems)}")
                    return "Not recorded: " + "; ".join(problems) + ". Call report_completion again.", True
                self.completion = args
                return "recorded", False
            self.step += 1
            sid = f"s{self.step}"
            r = self.gw.call_tool(sid, name, args, parent_step_id=self.prev)
            self.gw.report_step(sid, name, args, r, f"{name} returned {r['status']}", parent_step_id=self.prev)
            self.prev = sid
            ok = r["status"] == "succeeded"
            body = r.get("result") if ok else {"status": r["status"], **r.get("error", {})}
            return json.dumps(body, default=str), not ok

    def finish(self) -> None:
        with self.lock:
            if self.completion is not None:
                c = self.completion
                self.gw.complete(f"s{self.step + 1}", c["outcome"][:500], c["changes"], status=c["status"],
                                 parent_step_id=self.prev)
            else:
                log("agent ended without a valid report_completion; no final claim sent")
            self.gw.done()


def _rpc_result(mid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _rpc_error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle_rpc(relay: Relay, msg: dict[str, Any]) -> dict[str, Any] | None:
    """One JSON-RPC message in, one response out (None for notifications)."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:  # notification (e.g. notifications/initialized): nothing to answer
        return None
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1]
        return _rpc_result(mid, {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                                 "serverInfo": {"name": SERVER_NAME, "version": __version__},
                                 "instructions": SYSTEM})
    if method == "ping":
        return _rpc_result(mid, {})
    if method == "tools/list":
        return _rpc_result(mid, {"tools": relay.tools})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return _rpc_error(mid, -32602, "tools/call needs a string name and object arguments")
        text, is_error = relay.call(name, args)
        return _rpc_result(mid, {"content": [{"type": "text", "text": text}], "isError": is_error})
    return _rpc_error(mid, -32601, f"method not found: {method}")


def make_server(relay: Relay, token: str) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, code: int, body: Any = None) -> None:
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(code)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self) -> bool:
            origin = self.headers.get("Origin")
            if origin and not origin.startswith(("http://127.0.0.1", "http://localhost")):
                self._reply(403, _rpc_error(None, -32600, "origin not allowed"))  # DNS-rebinding guard
                return False
            if self.headers.get("Authorization") != f"Bearer {token}":
                self._reply(401, _rpc_error(None, -32600, "missing or wrong bearer token"))
                return False
            if self.path.rstrip("/") != "/mcp":
                self._reply(404)
                return False
            return True

        def do_POST(self):
            if not self._allowed():
                return
            try:
                msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            except (ValueError, json.JSONDecodeError):
                return self._reply(400, _rpc_error(None, -32700, "parse error"))
            if isinstance(msg, list):  # JSON-RPC batch (2025-03-26)
                out = [r for r in (handle_rpc(relay, m) for m in msg if isinstance(m, dict)) if r]
                return self._reply(200, out) if out else self._reply(202)
            if not isinstance(msg, dict):
                return self._reply(400, _rpc_error(None, -32600, "invalid request"))
            resp = handle_rpc(relay, msg)
            return self._reply(202) if resp is None else self._reply(200, resp)

        def do_GET(self):  # no server-initiated stream
            if self._allowed():
                self._reply(405)

        def do_DELETE(self):  # session end: nothing to clean up
            if self._allowed():
                self._reply(200, {})

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def fill(template: list[str], values: dict[str, str]) -> list[str]:
    out = []
    for part in template:
        for k, v in values.items():
            part = part.replace("{" + k + "}", v)
        out.append(part)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Run an MCP agent behind the assure gateway")
    ap.add_argument("command", nargs=argparse.REMAINDER, help="agent command template (after --)")
    a = ap.parse_args()
    template = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not template:
        ap.error("no agent command given")

    gw = GatewayClient()
    task = gw.receive_task()
    relay = Relay(gw, task)
    token = secrets.token_urlsafe(24)
    server = make_server(relay, token)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
    config = Path.cwd() / CONFIG_NAME
    config.write_text(json.dumps({"mcpServers": {SERVER_NAME: {
        "type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}}}, indent=2))
    prompt = f"{SYSTEM}\n\nTask: {task['instruction']}"
    argv = fill(template, {"prompt": prompt, "instruction": task["instruction"], "mcp_url": url,
                           "python": sys.executable,
                           "mcp_token": token, "mcp_config": str(config)})
    log(f"serving MCP at {url}; starting agent: {argv[0]}")
    env = {**os.environ, "ASSURE_MCP_URL": url, "ASSURE_MCP_TOKEN": token, "ASSURE_MCP_CONFIG": str(config)}
    try:
        code = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr, env=env).returncode
        log(f"agent exited with code {code}")
    except OSError as e:
        log(f"agent could not be started: {e}")
    finally:
        server.shutdown()
        relay.finish()


if __name__ == "__main__":
    main()
