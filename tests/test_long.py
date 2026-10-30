"""Long support-queue tasks: the generated spec is reproducible, and partial progress is measured."""

import importlib.util

from assure.bench import Suite, SuiteTask, parse_agent, run_bench

from .conftest import PROJECT

GENERATED = ["fixtures/customers_v3/seed.sql", "fixtures/customers_v3/files/notes/queue.txt",
             "fixtures/customers_v3/files/notes/queue-short.txt", "specs/long/queue_long/task.yaml",
             "specs/long/queue_short/task.yaml"]


def test_build_is_reproducible():
    before = {f: (PROJECT / f).read_text() for f in GENERATED}
    spec = importlib.util.spec_from_file_location("build", PROJECT / "specs/long/build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    build.main()
    assert {f: (PROJECT / f).read_text() for f in GENERATED} == before


def test_partial_progress_is_measured(runs_dir):
    suite = Suite(suite="t", version="0", tasks=[SuiteTask(task="specs/long/queue_long/task.yaml")])
    b = run_bench(project=PROJECT, suite=suite, runs_dir=runs_dir, repeat=1, jobs=2,
                  agents=[parse_agent("mock:queue-correct"), parse_agent("mock:queue-no-retry")])
    good, lossy = b["per_agent"]["mock:queue-correct"], b["per_agent"]["mock:queue-no-retry"]
    assert good["rate"] == 1.0 and good["progress"] == 1.0
    assert lossy["rate"] == 0.0 and lossy["progress"] == 24 / 25  # the one write that hit the lock
    assert lossy["failures_by_kind"]["false_claim"] == 1


def test_fault_position_cycles_with_the_attempt(runs_dir):
    from assure.orchestrator import ToolFault
    f = ToolFault(tool="db_update", nth_cycle=[3, 8])
    assert [f.resolved(v)["nth"] for v in range(3)] == [3, 8, 3]
    assert ToolFault(tool="db_update", nth=5).resolved(4)["nth"] == 5
    suite = Suite(suite="t", version="0", tasks=[SuiteTask(task="specs/long/queue_long/task.yaml")])
    b = run_bench(project=PROJECT, suite=suite, runs_dir=runs_dir, repeat=2, jobs=2,
                  agents=[parse_agent("mock:queue-correct")])
    by_fault = b["per_agent"]["mock:queue-correct"]["by_task"]["long-queue-30"]["by_fault"]
    assert list(by_fault) == ["3", "8"] and all(v == {**v, "passed": 1, "runs": 1} for v in by_fault.values())
    assert by_fault["3"]["hit"] != by_fault["8"]["hit"]  # the lock landed on different writes
