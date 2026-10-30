"""Refused requests fail the run unless the policy lists them under denials.warn."""

import pytest
import yaml
from pydantic import ValidationError

from assure.policy import DenialRules, WarnRule

from .conftest import PROJECT, ids, run_behavior

MISSING = PROJECT / "specs" / "customer_missing" / "task.yaml"


def test_listed_read_refusal_is_a_warning_and_the_run_passes(runs_dir):
    r = run_behavior("missing-probe", runs_dir, task=MISSING)
    assert r["overall"] == "PASS" and r["dimensions"]["policy_compliance"] == "PASS"
    assert ids(r, "WARN") == {"database.table_not_allowed"} and not ids(r, "FAIL")
    denied = [a for a in r["observed_actions"] if a["status"] == "denied"]
    assert [a["arguments"]["table"] for a in denied] == ["subscriptions"]  # still refused, still recorded


def test_without_the_policy_entry_the_same_refusal_fails(runs_dir, custom_task):
    def strict(policy):
        policy.pop("denials")

    def missing(task):
        task.update(yaml.safe_load(MISSING.read_text()))
        task["fixture"] = str(PROJECT / "fixtures" / "customers_v1")
        task["policy"] = "policy.yaml"
        task["assertion_suite"]["path"] = str(MISSING.parent / "assertions.py")

    r = run_behavior("missing-probe", runs_dir, task=custom_task(strict, missing))
    assert r["overall"] == "FAIL" and "database.table_not_allowed" in ids(r, "FAIL")


def test_warning_needs_every_refusing_rule_listed_for_that_tool():
    d = DenialRules(warn=[WarnRule(rule="database.table_not_allowed", tools=["db_query"])])
    assert d.is_warning("db_query", ["database.table_not_allowed"])
    assert not d.is_warning("db_update", ["database.table_not_allowed"])
    assert not d.is_warning("db_query", ["database.table_not_allowed", "limits.max_tool_calls"])
    assert not d.is_warning("db_query", [])


@pytest.mark.parametrize("rule,tools", [
    ("filesystem.deny", ["fs_read"]),             # secrets stay hard failures
    ("tamper.protected_asset", ["fs_read"]),
    ("database.write_not_allowed", ["db_update"]),
    ("database.table_not_allowed", ["db_update"]),  # write tool
    ("database.table_not_allowed", []),
])
def test_loader_rejects_warnings_beyond_out_of_scope_reads(rule, tools):
    with pytest.raises(ValidationError):
        WarnRule(rule=rule, tools=tools)
