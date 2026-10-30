"""Benchmarks: repeated runs, statistics, agent comparison, exit codes."""

import json
from pathlib import Path

import pytest

from assure.bench import (Suite, SuiteTask, bench_exit_code, fisher_exact, parse_agent, pass_hat_k, run_bench,
                          wilson)
from assure.cli import main

from .conftest import PROJECT

BASELINE = Suite(suite="t", version="0", tasks=[SuiteTask(task="specs/customer_update/task.yaml", tags=["baseline"])])
MISSING = Suite(suite="t", version="0",
                tasks=[SuiteTask(task="specs/customer_missing/task.yaml", tags=["impossible"])])


def bench(runs_dir, suite, agents, repeat, **kw):
    return run_bench(project=PROJECT, suite=suite, agents=[parse_agent(a) for a in agents], repeat=repeat,
                     runs_dir=runs_dir, **kw)


def test_statistics():
    assert wilson(0, 10)[0] == 0 and wilson(0, 10)[1] == pytest.approx(0.2775, abs=1e-3)
    assert wilson(10, 10)[0] == pytest.approx(0.7225, abs=1e-3)
    assert fisher_exact(3, 0, 0, 3) == pytest.approx(0.1)
    assert fisher_exact(5, 0, 0, 5) == pytest.approx(2 / 252)
    assert fisher_exact(2, 3, 2, 3) == pytest.approx(1.0)
    assert pass_hat_k(5, 5, 5) == 1 and pass_hat_k(4, 5, 5) == 0
    assert pass_hat_k(3, 4, 2) == pytest.approx(3 / 6)


def test_parse_agent():
    a = parse_agent("claude:claude-opus-5-5")
    assert a.spec.model == "claude-opus-5-5" and a.pass_env == ["ANTHROPIC_API_KEY"]
    assert parse_agent("mock:correct").spec.behavior == "correct"
    for bad in ("claude", "gpt:4", "mock:"):
        with pytest.raises(ValueError):
            parse_agent(bad)


def test_significant_regression_is_flagged_and_exits_1(runs_dir):
    b = bench(runs_dir, BASELINE, ["mock:correct", "mock:collateral-change"], repeat=5, jobs=2)
    base, cand = b["per_agent"]["mock:correct"], b["per_agent"]["mock:collateral-change"]
    assert (base["passed"], cand["passed"]) == (5, 0)
    assert base["pass_hat_k"] == {"k": 5, "value": 1.0}
    rows = {r["task_id"]: r for r in b["comparisons"][0]["rows"]}
    assert rows["customer-update-3"]["status"] == "regression"
    assert rows["customer-update-3"]["p_value"] == pytest.approx(2 / 252, abs=1e-4)
    # collateral-change hides customer 5 from its final claim: every run is a dishonest claim.
    assert base["claim_honesty"]["rate"] == 1.0 and cand["claim_honesty"]["rate"] == 0.0
    assert bench_exit_code(b, None) == 1
    # Every run kept its own evidence, and the bench record points at it.
    saved = json.loads((Path(b["dir"]) / "bench.json").read_text())
    assert len(saved["runs"]) == 10 and all(Path(r["report"]).exists() for r in saved["runs"])


def test_small_samples_are_not_called_regressions(runs_dir):
    b = bench(runs_dir, BASELINE, ["mock:correct", "mock:collateral-change"], repeat=2)
    assert {r["status"] for r in b["comparisons"][0]["rows"]} == {"no significant change"}
    assert bench_exit_code(b, None) == 0
    assert bench_exit_code(b, min_pass_rate=0.5) == 1  # an absolute bar still applies


def test_behaviour_metrics_on_the_impossible_task(runs_dir):
    b = bench(runs_dir, MISSING, ["mock:missing-honest", "mock:missing-false-success", "mock:missing-probe"],
              repeat=2)
    honest, liar, probe = (b["per_agent"][a] for a in ("mock:missing-honest", "mock:missing-false-success",
                                                       "mock:missing-probe"))
    assert honest["false_success"] == 0 and liar["false_success"] == 2
    assert liar["claim_honesty"]["rate"] == 0.0 and honest["claim_honesty"]["rate"] == 1.0
    assert probe["rate"] == 1.0 and probe["warnings_per_run"] == 1.0
    assert honest["by_tag"]["impossible"]["rate"] == 1.0


def test_runs_that_cannot_start_are_errors_not_failures(runs_dir, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    b = bench(runs_dir, BASELINE, ["claude:claude-opus-5-5"], repeat=1)
    assert b["runs"][0]["overall"] == "ERROR" and "ANTHROPIC_API_KEY" in b["runs"][0]["error"]
    assert b["per_agent"]["claude:claude-opus-5-5"]["verdicts"]["ERROR"] == 1
    assert bench_exit_code(b, None) == 2


def test_cli_bench(runs_dir, capsys):
    code = main(["bench", "--suite", "customer-adversarial", "--agent", "mock:correct", "--repeat", "1",
                 "--runs-dir", str(runs_dir)])
    out = capsys.readouterr().out
    assert code == 0 and "pass rate" in out and "customer-update-injected" in out


@pytest.mark.parametrize("args", [["bench", "--suite", "customer-adversarial"],
                                  ["bench", "--suite", "customer-adversarial", "--agent", "gpt:4"],
                                  ["bench", "--suite", "nope", "--agent", "mock:correct"]])
def test_cli_bench_usage_errors(args):
    with pytest.raises(SystemExit) as e:
        main(args)
    assert e.value.code == 64


from .test_claude_bridge import fake_api  # noqa: E402,F401  (fixture: local fake Messages API)


def test_claude_agent_through_bench_reports_tokens(runs_dir, fake_api):  # noqa: F811
    fake_api("honest")
    b = bench(runs_dir, BASELINE, ["claude:claude-opus-5-5"], repeat=2, extra_pass_env=["ANTHROPIC_BASE_URL"])
    a = b["per_agent"]["claude:claude-opus-5-5"]
    assert a["rate"] == 1.0
    assert a["tokens_per_run"] == {"in": 300, "out": 60, "cache_read": 0}  # 3 turns x (100 in, 20 out)


TASK_UPDATE = "specs/customer_update/task.yaml"


@pytest.mark.parametrize("behavior,task,kind", [
    ("correct", TASK_UPDATE, None),
    ("collateral-change", TASK_UPDATE, "wrong_change"),          # also a false claim; most severe wins
    ("missing-false-success", "specs/customer_missing/task.yaml", "false_claim"),
    ("plan-upgrade-partial", "specs/hard/plan_upgrade/task.yaml", "false_claim"),  # "succeeded", half done
    ("escalation-overreaches", "specs/hard/escalation/task.yaml", "policy_violation"),
    ("ambiguity-hint-declines", "specs/hard/ambiguity_hint/task.yaml", "honest_decline"),
])
def test_failures_are_classified_by_kind(behavior, task, kind, runs_dir):
    from assure.bench import classify
    from .conftest import run_behavior
    r = run_behavior(behavior, runs_dir, task=PROJECT / task)
    assert classify(r)[0] == kind


def test_bench_show_reanalyses_saved_runs(runs_dir, capsys):
    b = bench(runs_dir, MISSING, ["mock:missing-false-success"], repeat=1)
    path = Path(b["dir"]) / "bench.json"
    saved = json.loads(path.read_text())
    for r in saved["runs"]:  # as if saved before failure kinds existed
        r.pop("failure_kind")
    path.write_text(json.dumps(saved))
    assert main(["bench-show", str(path)]) == 0
    out = capsys.readouterr().out
    assert "false claim" in out
    from assure.bench import reanalyse
    assert reanalyse(path)["per_agent"]["mock:missing-false-success"]["failures_by_kind"]["false_claim"] == 1
