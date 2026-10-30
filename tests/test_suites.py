"""Every suite must discriminate: each task's passing reference agent PASSes and each failing one FAILs."""

import pytest

from assure.bench import Reference, Suite, SuiteTask, check_suite, load_suite

from .conftest import PROJECT, run_behavior

SUITES = sorted(p.stem for p in (PROJECT / "suites").glob("*.yaml"))


@pytest.mark.parametrize("name", SUITES)
def test_suite_is_valid(name, runs_dir):
    result = check_suite(project=PROJECT, suite=load_suite(PROJECT, name), runs_dir=runs_dir, jobs=4)
    assert result["valid"], result["problems"]
    assert all(t.reference for t in load_suite(PROJECT, name).tasks)


def _suite(reference):
    return Suite(suite="t", version="0",
                 tasks=[SuiteTask(task="specs/customer_update/task.yaml", reference=reference)])


def test_a_failing_reference_that_passes_makes_the_suite_invalid(runs_dir):
    ref = Reference.model_validate({"pass": "mock:correct", "fail": ["mock:correct"]})
    result = check_suite(project=PROJECT, suite=_suite(ref), runs_dir=runs_dir)
    assert not result["valid"] and "expected FAIL, got PASS" in result["problems"][0]


def test_an_inconclusive_failing_reference_does_not_count(runs_dir):
    ref = Reference.model_validate({"pass": "mock:correct", "fail": ["mock:missing-event"]})
    result = check_suite(project=PROJECT, suite=_suite(ref), runs_dir=runs_dir)
    assert not result["valid"] and "got INCONCLUSIVE" in result["problems"][0]


def test_tasks_without_references_and_live_references_are_rejected(runs_dir):
    assert not check_suite(project=PROJECT, suite=_suite(None), runs_dir=runs_dir)["valid"]
    live = Reference.model_validate({"pass": "claude:claude-opus-5-5", "fail": ["mock:collateral-change"]})
    with pytest.raises(ValueError, match="deterministic mock"):
        check_suite(project=PROJECT, suite=_suite(live), runs_dir=runs_dir)


def test_injected_tool_fault_is_in_the_evidence_and_nothing_executed(runs_dir):
    r = run_behavior("tool-failure-retries", runs_dir, task=PROJECT / "specs/hard/tool_failure/task.yaml")
    updates = [a for a in r["observed_actions"] if a["tool"] == "db_update"]
    assert [u["status"] for u in updates] == ["failed", "succeeded"]
    assert updates[0]["observed"] == {"injected_fault": True}
    assert updates[0]["error"]["message"] == "database is locked"
    assert r["task"]["tool_faults"][0]["tool"] == "db_update"
    assert r["overall"] == "PASS"
