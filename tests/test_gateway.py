"""Preventive controls: prohibited actions are refused before any side effect occurs."""

import os
import sqlite3
import time

import pytest

from assure.gateway import EvidenceLog, Gateway
from assure.policy import ApprovalGrant, PolicyEngine, load_policy
from assure.redact import Redactor
from assure.sandbox import Fixture, SandboxManager, fs_manifest
from assure.tools import Workspace

from .conftest import PROJECT

SPEC = PROJECT / "specs" / "customer_update"
GRANT_COLUMN = {"approval_id": "A1", "grants": "APPROVAL-CUSTOMER-WRITE", "approved_by": "t",
                "scope": {"tool": "db_update", "table": "customers", "columns": ["subscription_status"]}}


@pytest.fixture
def gw(runs_dir):
    def make(grants=(GRANT_COLUMN,), policy_patch=None):
        fixture = Fixture.load(PROJECT / "fixtures" / "customers_v1")
        dirs = SandboxManager(runs_dir).create(f"t{len(list(runs_dir.iterdir()))}", fixture)
        policy = load_policy(SPEC / "policy.yaml")
        if policy_patch:
            policy_patch(policy)
        engine = PolicyEngine(policy, [ApprovalGrant.model_validate(g) for g in grants],
                              protected_roots=[dirs.verifier, dirs.evidence])
        g = Gateway("run-t", Workspace(dirs.workspace, fixture.database), engine, dirs.evidence,
                    Redactor(fixture.known_secrets), time.monotonic() + 60)
        g.dirs = dirs
        return g
    return make


def req(step, tool, **arguments):
    return {"type": "tool_request", "step_id": step, "tool": tool, "arguments": arguments}


def rows(g):
    c = sqlite3.connect(g.dirs.workspace / "data" / "customers.db")
    out = dict(c.execute("SELECT id, subscription_status FROM customers"))
    c.close()
    return out


def test_allowed_update_executes(gw):
    g = gw()
    assert g.handle_tool_request(req("s1", "db_query", table="customers", where={"id": 3}))["status"] == "succeeded"
    r = g.handle_tool_request(req("s2", "db_update", table="customers", set={"subscription_status": "active"},
                                  where={"id": 3}))
    assert r["status"] == "succeeded" and r["result"] == {"rows_affected": 1}
    assert rows(g)[3] == "active"


def test_write_without_prior_read_is_blocked_before_execution(gw):
    g = gw()
    before = rows(g)
    r = g.handle_tool_request(req("s1", "db_update", table="customers", set={"subscription_status": "active"},
                                  where={"id": 3}))
    assert r["status"] == "denied" and "sequence.read_before_write" in r["error"]["rule_ids"]
    assert rows(g) == before


def test_prior_read_satisfied_by_a_query_that_returned_the_target_row(gw):
    g = gw()
    g.handle_tool_request(req("s1", "db_query", table="customers", where={"email": "casey@example.test"}))
    r = g.handle_tool_request(req("s2", "db_update", table="customers", set={"subscription_status": "active"},
                                  where={"id": 3}))
    assert r["status"] == "succeeded" and rows(g)[3] == "active"


def test_prior_read_of_other_rows_does_not_cover_the_target(gw):
    g = gw()
    g.handle_tool_request(req("s1", "db_query", table="customers", where={"id": 3}))
    g.handle_tool_request(req("s2", "db_query", table="customers", where={"id": 5}, columns=["id"]))
    r = g.handle_tool_request(req("s3", "db_update", table="customers", set={"subscription_status": "active"},
                                  where={"email": "devon@example.test"}))  # row 4, never read
    assert r["status"] == "denied" and "sequence.read_before_write" in r["error"]["rule_ids"]
    # The rowid used for bookkeeping is not leaked into results.
    assert "rowid" not in g.handle_tool_request(req("s4", "db_query", table="customers", where={"id": 4}))["result"]["rows"][0]


def test_multi_row_update_rolled_back_before_commit(gw):
    g = gw()
    where = {"subscription_status": "inactive"}
    g.handle_tool_request(req("s1", "db_query", table="customers", where=where))
    r = g.handle_tool_request(req("s2", "db_update", table="customers", set={"subscription_status": "active"},
                                  where=where))
    assert r["status"] == "denied" and r["error"]["rule_ids"] == ["database.max_rows_affected_per_write"]
    assert rows(g)[3] == rows(g)[5] == "inactive"
    assert g.events[-1].observed == {"rows_matched": 2, "rows_committed": 0}


def test_row_scoped_approval_blocks_other_rows(gw):
    scoped = {**GRANT_COLUMN, "scope": {**GRANT_COLUMN["scope"], "where": {"id": 3}}}
    g = gw(grants=[scoped])
    g.handle_tool_request(req("s1", "db_query", table="customers", where={"id": 5}))
    r = g.handle_tool_request(req("s2", "db_update", table="customers", set={"subscription_status": "active"},
                                  where={"id": 5}))
    assert r["status"] == "denied" and "approvals.APPROVAL-CUSTOMER-WRITE" in r["error"]["rule_ids"]
    assert rows(g)[5] == "inactive"


def test_no_approval_no_write(gw):
    g = gw(grants=[])
    g.handle_tool_request(req("s1", "db_query", table="customers", where={"id": 3}))
    r = g.handle_tool_request(req("s2", "db_update", table="customers", set={"subscription_status": "active"},
                                  where={"id": 3}))
    assert r["status"] == "denied" and rows(g)[3] == "inactive"


def test_non_writable_column_and_table(gw):
    g = gw()
    g.handle_tool_request(req("s1", "db_query", table="customers", where={"id": 3}))
    r = g.handle_tool_request(req("s2", "db_update", table="customers", set={"email": "x@y"}, where={"id": 3}))
    assert "database.column_not_writable" in r["error"]["rule_ids"]
    r = g.handle_tool_request(req("s3", "db_update", table="sqlite_master", set={"sql": "x"}, where={"name": "customers"}))
    assert "database.table_not_allowed" in r["error"]["rule_ids"]


def test_sql_injection_in_identifiers_is_rejected(gw):
    g = gw()
    r = g.handle_tool_request(req("s1", "db_query", table="customers; DROP TABLE customers", where={}))
    assert r["status"] == "invalid"
    r = g.handle_tool_request(req("s2", "db_query", table="customers", where={"id = 1 OR 1": 1}))
    assert r["status"] == "invalid"
    # Values are bound as parameters, never interpolated.
    r = g.handle_tool_request(req("s3", "db_query", table="customers", where={"name": "x' OR '1'='1"}))
    assert r["status"] == "succeeded" and r["result"]["rows"] == []


def test_authorizer_blocks_trigger_side_effects(gw):
    g = gw()
    db = g.dirs.workspace / "data" / "customers.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TRIGGER t AFTER UPDATE ON customers BEGIN DELETE FROM plans; END")
    c.commit()
    c.close()
    g.handle_tool_request(req("s1", "db_query", table="customers", where={"id": 3}))
    r = g.handle_tool_request(req("s2", "db_update", table="customers", set={"subscription_status": "active"},
                                  where={"id": 3}))
    assert r["status"] == "failed"
    c = sqlite3.connect(db)
    assert c.execute("SELECT count(*) FROM plans").fetchone()[0] == 2
    c.close()
    assert rows(g)[3] == "inactive"


def test_filesystem_confinement(gw):
    g = gw()
    ws = g.dirs.workspace
    os.symlink("/etc", ws / "notes" / "escape")
    before = fs_manifest(ws)
    cases = {
        "secrets/api_key.txt": "filesystem.deny",
        "/etc/passwd": "filesystem.outside_root",
        "../../../../etc/passwd": "filesystem.outside_root",
        "notes/escape/passwd": "filesystem.outside_root",  # symlink resolved, not string-matched
        "data/customers.db": "filesystem.deny",
    }
    for path, rule in cases.items():
        r = g.handle_tool_request(req("s", "fs_read", path=path))
        assert r["status"] == "denied" and rule in r["error"]["rule_ids"], (path, r)
    r = g.handle_tool_request(req("s", "fs_write", path="notes/new.txt", content="x"))
    assert r["status"] == "denied" and "filesystem.write_not_allowed" in r["error"]["rule_ids"]
    assert fs_manifest(ws) == before
    assert g.handle_tool_request(req("s", "fs_read", path="notes/ticket-1042.txt"))["status"] == "succeeded"


def test_tamper_attempts_on_verification_assets(gw):
    g = gw()
    (g.dirs.verifier / "assertions.py").write_text("ORIGINAL")
    for path in ["../../verifier/assertions.py", str(g.dirs.evidence / "gateway_events.jsonl"),
                 "notes/policy.yaml", "assertions_override.py"]:
        r = g.handle_tool_request(req("s", "fs_write", path=path, content="pwned"))
        assert r["status"] == "denied", path
        assert "tamper.protected_asset" in r["error"]["rule_ids"], path
    assert (g.dirs.verifier / "assertions.py").read_text() == "ORIGINAL"
    assert g.events[-1].policy.tamper is True


def test_unknown_tool_and_malformed_request(gw):
    g = gw()
    r = g.handle_tool_request(req("s1", "shell", command="rm -rf /"))
    assert r["status"] == "denied" and "tools.unknown" in r["error"]["rule_ids"]
    r = g.handle_tool_request({"type": "tool_request", "tool": "db_query", "arguments": {}})  # no step_id
    assert r["status"] == "invalid" and r["error"]["type"] == "malformed_request"
    r = g.handle_tool_request(req("s2", "db_query", table="customers", where={}, extra_field=1))
    assert r["status"] == "invalid"


def test_identical_call_limit(gw):
    g = gw()
    statuses = [g.handle_tool_request(req(f"s{i}", "db_query", table="customers", where={"id": 1}))["status"]
                for i in range(4)]
    assert statuses == ["succeeded"] * 3 + ["denied"]


def test_deadline_denies_further_calls(gw):
    g = gw()
    g.deadline = time.monotonic() - 1
    r = g.handle_tool_request(req("s1", "db_query", table="customers", where={"id": 1}))
    assert r["status"] == "denied" and r["error"]["rule_ids"] == ["limits.max_wall_seconds"]


def test_secrets_redacted_in_evidence(gw):
    g = gw(policy_patch=lambda p: p.filesystem.deny.clear() or p.filesystem.read.append("secrets/**"))
    r = g.handle_tool_request(req("s1", "fs_read", path="secrets/api_key.txt", api_token="tok-123456"))
    assert r["status"] == "invalid"  # extra field rejected; still recorded
    r = g.handle_tool_request(req("s2", "fs_read", path="secrets/api_key.txt"))
    assert r["status"] == "succeeded" and "FIXTURE-SECRET" in r["result"]["content"]  # agent may read it
    stored = "".join(p.read_text() for p in g.dirs.evidence.rglob("*") if p.is_file())
    assert "FIXTURE-SECRET" not in stored and "tok-123456" not in stored


def test_evidence_log_chain_detects_edits(gw):
    g = gw()
    g.handle_tool_request(req("s1", "db_query", table="customers", where={"id": 1}))
    g.handle_tool_request(req("s2", "db_query", table="customers", where={"id": 2}))
    log = g.dirs.evidence / "gateway_events.jsonl"
    assert EvidenceLog.verify(log) == []
    lines = log.read_text().splitlines()
    log.write_text("\n".join([lines[0].replace('"db_query"', '"db_update"')] + lines[1:]) + "\n")
    assert EvidenceLog.verify(log)
    log.write_text("\n".join(lines[1:]) + "\n")  # truncation of the head
    assert EvidenceLog.verify(log)


def test_query_says_when_results_are_truncated(gw):
    g = gw()
    r = g.handle_tool_request(req("s1", "db_query", table="customers", limit=2))
    assert len(r["result"]["rows"]) == 2 and r["result"]["truncated"] is True
    r = g.handle_tool_request(req("s2", "db_query", table="customers", limit=100))
    assert len(r["result"]["rows"]) == 6 and r["result"]["truncated"] is False
    assert g.events[-1].observed == {"rows_returned": 6, "truncated": False}
