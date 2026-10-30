"""Code-fixing tasks: hidden tests, run_tests through the gateway, file snapshots and diffs."""

import importlib.util
import time

import pytest

from assure.gateway import Gateway
from assure.orchestrator import execute_run
from assure.policy import PolicyEngine, load_policy
from assure.redact import Redactor
from assure.sandbox import Fixture, SandboxManager
from assure.tools import Workspace

from .conftest import PROJECT, ids, mock_argv, run_behavior
from .test_gateway import req

FIX = PROJECT / "specs" / "code" / "fix_rounding" / "task.yaml"
GENERATED = [*(f"fixtures/{v}/files/{f}" for v in ("pricing_bug_v1", "pricing_injected_v1", "pricing_wrongtest_v1")
               for f in ("pricing/discount.py", "tests/test_money.py")),
             "specs/code/fix_rounding/task.yaml", "specs/code/fix_injected/task.yaml", "specs/code/wrong_test/task.yaml"]


def test_build_is_reproducible():
    before = {f: (PROJECT / f).read_text() for f in GENERATED}
    spec = importlib.util.spec_from_file_location("code_build", PROJECT / "specs/code/build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    build.main()
    assert {f: (PROJECT / f).read_text() for f in GENERATED} == before


def test_correct_fix_passes_with_a_diff_in_the_evidence(runs_dir):
    r = run_behavior("code-fix-correct", runs_dir, task=FIX)
    assert r["overall"] == "PASS", r["findings"]
    tests = [a for a in r["observed_actions"] if a["tool"] == "run_tests"]
    assert [t["observed"]["exit_code"] for t in tests] == [1, 0]  # failing before the fix, passing after
    diff = (PROJECT / r["evidence"]["dir"] / "workspace.diff").read_text()
    assert "+++ b/pricing/discount.py" in diff and "ROUND_HALF_UP" in diff and "tests/" not in diff
    progress = next(a for a in r["assertions"] if a["id"] == "CF-001")["evidence"]["progress"]
    assert progress == [6, 6]  # the six hidden tests for the requested fix


def test_test_runs_that_change_the_workspace_are_detected(runs_dir):
    r = run_behavior("code-fix-side-effect", runs_dir, task=FIX)
    assert "boundary.unattributed_change" in ids(r, "FAIL")
    assert any("notes.txt" in f["observed"] for f in r["findings"] if f["id"] == "boundary.unattributed_change")


@pytest.mark.parametrize("path", ["../..", "/tmp", "../verifier"])
def test_run_tests_is_confined_to_the_workspace(path, runs_dir):
    fixture = Fixture.load(PROJECT / "fixtures" / "pricing_bug_v1")
    dirs = SandboxManager(runs_dir).create("t-confine", fixture)
    engine = PolicyEngine(load_policy(PROJECT / "specs/code/policy.yaml"), [], protected_roots=[dirs.verifier, dirs.evidence])
    g = Gateway("run-t", Workspace(dirs.workspace, fixture.database), engine, dirs.evidence, Redactor([]),
                time.monotonic() + 60)
    r = g.handle_tool_request(req("s1", "run_tests", path=path))
    assert r["status"] == "denied"


def test_run_tests_is_refused_in_docker_mode(runs_dir):
    with pytest.raises(ValueError, match="run_tests"):
        execute_run(project=PROJECT, task_path=FIX, argv=mock_argv("code-fix-correct"), runs_dir=runs_dir,
                    isolation="docker")
