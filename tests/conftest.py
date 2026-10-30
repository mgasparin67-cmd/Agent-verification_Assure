import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from assure.orchestrator import execute_run

PROJECT = Path(__file__).resolve().parent.parent
TASK = PROJECT / "specs" / "customer_update" / "task.yaml"


def _make_writable(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o755)
        for f in filenames:
            p = Path(dirpath) / f
            if not p.is_symlink():
                os.chmod(p, 0o644)


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    yield d
    _make_writable(d)  # frozen verifier dirs are read-only


def mock_argv(behavior: str) -> list[str]:
    return [sys.executable, "-I", "-m", "assure.agents.mock_agent", "--behavior", behavior]


def run_behavior(behavior: str, runs_dir: Path, task: Path = TASK, **kw) -> dict:
    return execute_run(project=PROJECT, task_path=task, argv=mock_argv(behavior), runs_dir=runs_dir, **kw)


@pytest.fixture
def custom_task(tmp_path):
    """Copy the customer-update spec and apply overrides to policy/task YAML."""

    def make(policy_patch=None, task_patch=None) -> Path:
        d = tmp_path / f"spec{len(list(tmp_path.glob('spec*')))}"
        shutil.copytree(PROJECT / "specs" / "customer_update", d)
        task = yaml.safe_load((d / "task.yaml").read_text())
        task["fixture"] = str(PROJECT / "fixtures" / "customers_v1")
        policy = yaml.safe_load((d / "policy.yaml").read_text())
        if policy_patch:
            policy_patch(policy)
        if task_patch:
            task_patch(task)
        (d / "task.yaml").write_text(yaml.safe_dump(task))
        (d / "policy.yaml").write_text(yaml.safe_dump(policy))
        return d / "task.yaml"

    return make


def ids(report: dict, outcome: str | None = None) -> set[str]:
    return {f["id"] for f in report["findings"] if outcome is None or f["outcome"] == outcome}
