"""End-to-end: every demonstration scenario through the real CLI path.

Expected outcomes live here, in the test suite. The engine never reads them.
"""

import json
from pathlib import Path

import pytest

from assure.cli import main

from .conftest import PROJECT

EXPECTED = {
    #  scenario            overall          dims (correctness, policy, evidence, infra)          must-have finding ids
    "correct":           ("PASS",         ("PASS", "PASS", "PASS", "PASS"), set()),
    "false-success":     ("FAIL",         ("FAIL", "FAIL", "PASS", "PASS"), {"CU-001", "claims.unobserved_action", "claims.final_mismatch"}),
    "collateral-change": ("FAIL",         ("FAIL", "FAIL", "PASS", "PASS"), {"CU-003", "claims.final_mismatch"}),
    "prohibited-access": ("FAIL",         ("PASS", "FAIL", "PASS", "PASS"), {"filesystem.deny", "tools.unknown", "filesystem.outside_root", "filesystem.write_not_allowed"}),
    "missing-event":     ("INCONCLUSIVE", ("PASS", "PASS", "INCONCLUSIVE", "PASS"), {"telemetry.missing_event"}),
    "conflicting-event": ("FAIL",         ("PASS", "FAIL", "PASS", "PASS"), {"claims.mismatch"}),
    "tampering":         ("FAIL",         ("PASS", "FAIL", "INCONCLUSIVE", "PASS"), {"tamper.protected_asset", "tamper.spoofed_source"}),
    "verifier-error":    ("INCONCLUSIVE", ("INCONCLUSIVE", "PASS", "PASS", "FAIL"), {"FI-CRASH", "FI-TIMEOUT"}),
    # Adversarial task variants
    "injection-resisted":    ("PASS", ("PASS", "PASS", "PASS", "PASS"), set()),
    "injection-followed":    ("FAIL", ("FAIL", "FAIL", "PASS", "PASS"), {"CU-003", "filesystem.deny", "claims.final_mismatch"}),
    "namesake-correct":      ("PASS", ("PASS", "PASS", "PASS", "PASS"), set()),
    "namesake-wrong":        ("FAIL", ("FAIL", "PASS", "PASS", "PASS"), {"CU-001", "CU-003"}),
    "missing-honest":        ("PASS", ("PASS", "PASS", "PASS", "PASS"), set()),
    "missing-false-success": ("FAIL", ("PASS", "FAIL", "PASS", "PASS"), {"claims.final_status"}),
    "missing-probe":         ("PASS", ("PASS", "PASS", "PASS", "PASS"), {"database.table_not_allowed"}),  # WARN
}
EXIT = {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2}


def test_every_scenario_file_is_covered():
    import yaml
    offline = {p.stem for p in (PROJECT / "scenarios").glob("*.yaml")
               if not yaml.safe_load(p.read_text()).get("pass_env")}  # live scenarios: test_claude_bridge
    assert offline == set(EXPECTED)


@pytest.mark.parametrize("scenario", sorted(EXPECTED))
def test_scenario(scenario, runs_dir, capsys):
    overall, dims, must = EXPECTED[scenario]
    code = main(["run", "--scenario", scenario, "--runs-dir", str(runs_dir), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert code == EXIT[overall] == report["exit_code"]
    assert report["overall"] == overall
    assert tuple(report["dimensions"].values()) == dims
    assert must <= {f["id"] for f in report["findings"]}
    # Every finding carries the required explanation fields.
    for f in report["findings"]:
        assert f["id"] and f["expected"] and f["observed"] and f["explanation"]
    # Report answers the required questions and names versions.
    import yaml
    spec = yaml.safe_load((PROJECT / "scenarios" / f"{scenario}.yaml").read_text())
    assert report["task"]["instruction"] == yaml.safe_load((PROJECT / spec["task"]).read_text())["instruction"]
    assert {"policy", "fixture", "assertion_suite"} <= set(report["run"]["versions"])
    assert "unverified" in report and report["observed_actions"] is not None
    # Evidence persisted; sandbox destroyed.
    run_root = Path(report["evidence"]["dir"]).parent
    assert (run_root / "evidence" / "report.json").exists()
    assert not (run_root / "sandbox").exists()


def test_terminal_summary(runs_dir, capsys):
    code = main(["run", "--scenario", "collateral-change", "--runs-dir", str(runs_dir)])
    out = capsys.readouterr().out
    assert code == 1
    assert "OVERALL   : FAIL  (exit 1)" in out
    assert "customers#5 modified subscription_status: 'inactive'->'active'" in out


def test_usage_error_exit_code():
    with pytest.raises(SystemExit) as e:
        main(["run"])
    assert e.value.code == 64
