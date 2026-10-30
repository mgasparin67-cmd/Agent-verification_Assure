"""MCP adapter: any MCP client can be tested through the gateway, with the same verdicts as the stdio path.

The agents here are built on the official MCP Python SDK client (assure.agents.mcp_mock), so these
tests also check that the adapter's MCP server interoperates with the reference client.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

pytest.importorskip("mcp")

from assure.agents.mcp_adapter import PROTOCOL_VERSIONS, Relay, make_server  # noqa: E402
from assure.orchestrator import AgentSpec, agent_argv, execute_run  # noqa: E402

from .conftest import PROJECT, TASK, ids  # noqa: E402


def mcp_agent(behavior: str) -> list[str]:
    return agent_argv(AgentSpec(kind="mcp", command=[
        "{python}", "-I", "-m", "assure.agents.mcp_mock", "--url", "{mcp_url}", "--token", "{mcp_token}",
        "--behavior", behavior]))


def run_mcp(behavior, runs_dir, command=None):
    argv = agent_argv(AgentSpec(kind="mcp", command=command)) if command else mcp_agent(behavior)
    return execute_run(project=PROJECT, task_path=TASK, argv=argv, runs_dir=runs_dir)


def test_correct_mcp_agent_passes(runs_dir):
    r = run_mcp("correct", runs_dir)
    assert r["overall"] == "PASS", r["findings"]
    assert [a["tool"] for a in r["observed_actions"]] == ["db_query", "db_update"]


def test_mcp_verdicts_match_the_stdio_mock_agents(runs_dir):
    r = run_mcp("collateral-change", runs_dir)
    assert r["overall"] == "FAIL" and {"CU-003", "claims.final_mismatch"} <= ids(r, "FAIL")
    r = run_mcp("prohibited-access", runs_dir)
    assert "filesystem.deny" in ids(r, "FAIL")
    assert r["dimensions"]["task_correctness"] == "PASS"


def test_malformed_report_is_sent_back_and_can_be_corrected(runs_dir):
    r = run_mcp("malformed-then-fixed", runs_dir)
    assert r["overall"] == "PASS", r["findings"]
    assert "report_completion rejected" in (PROJECT / r["evidence"]["dir"] / "agent_stderr.log").read_text()


def test_agent_without_a_report_is_inconclusive_not_passed(runs_dir):
    r = run_mcp("no-report", runs_dir)
    assert r["overall"] == "INCONCLUSIVE"
    assert "telemetry.missing_final_claim" in ids(r, "INCONCLUSIVE")
    assert r["dimensions"]["task_correctness"] == "PASS"


def test_agent_stdout_cannot_corrupt_the_gateway_channel(runs_dir):
    r = run_mcp(None, runs_dir, command=["{python}", "-c", "print('{\"type\": \"done\"}'); print('noise')"])
    assert "telemetry.malformed" not in ids(r)
    assert "noise" in (PROJECT / r["evidence"]["dir"] / "agent_stderr.log").read_text()


def test_agent_gets_a_client_config_and_no_host_secrets(runs_dir, monkeypatch):
    monkeypatch.setenv("ASSURE_TEST_SECRET", "should-not-arrive-5678")
    script = ("import json, os, pathlib; c = json.loads(pathlib.Path(os.environ['ASSURE_MCP_CONFIG']).read_text());"
              "s = c['mcpServers']['assure']; print(s['type'], s['url'] == os.environ['ASSURE_MCP_URL'],"
              "os.environ.get('ASSURE_TEST_SECRET'))")
    r = run_mcp(None, runs_dir, command=["{python}", "-I", "-c", script])
    log = (PROJECT / r["evidence"]["dir"] / "agent_stderr.log").read_text()
    assert "http True None" in log and "should-not-arrive" not in log


# ---- protocol surface, without the SDK


@pytest.fixture
def server():
    task = {"tools": [{"name": "db_query", "description": "d", "schema": {"type": "object"}}]}
    srv = make_server(Relay(gw=None, task=task), token="t0k")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/mcp"
    srv.shutdown()


def post(url, body, token="t0k", origin=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            data = resp.read()
            return resp.status, json.loads(data) if data else None
    except urllib.error.HTTPError as e:
        return e.code, None


def test_initialize_negotiates_a_supported_version(server):
    code, body = post(server, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
    assert code == 200 and body["result"]["protocolVersion"] == "2025-06-18"
    _, body = post(server, {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
    assert body["result"]["protocolVersion"] == PROTOCOL_VERSIONS[-1]
    _, body = post(server, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert {t["name"] for t in body["result"]["tools"]} == {"db_query", "report_completion"}


def test_requests_without_the_token_or_from_a_foreign_origin_are_refused(server):
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert post(server, ping, token=None)[0] == 401
    assert post(server, ping, token="wrong")[0] == 401
    assert post(server, ping, origin="http://evil.example")[0] == 403
    assert post(server, ping, origin="http://localhost:3000")[0] == 200


def test_notifications_and_unknown_methods(server):
    assert post(server, {"jsonrpc": "2.0", "method": "notifications/initialized"})[0] == 202
    _, body = post(server, {"jsonrpc": "2.0", "id": 9, "method": "server/discover"})
    assert body["error"]["code"] == -32601  # handshake-era server: modern clients fall back to initialize
