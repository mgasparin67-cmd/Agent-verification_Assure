"""Code-task assertion suite (human-authored; hidden tests come from the frozen task params).

The agent never sees the hidden tests. They run against a fresh copy of the final workspace, in a
separate pytest process with the workspace's own conftest files and config ignored. A suite counts
as passing only if every hidden test ran and passed (skips and collection errors count against it).

Running hidden tests executes code the agent may have written. In local isolation that code runs
as your user; for agents you do not trust, use stronger isolation.
"""

import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from assure.assertions import Check, PreconditionError, assertion


def _run_hidden(ctx, source: str, timeout: float = 45) -> Check:
    if ctx.after_files is None:
        raise PreconditionError("fixture keeps no file snapshots (snapshot_files: true is required)")
    expected = len(re.findall(r"^def test_", source, re.M))
    with tempfile.TemporaryDirectory() as tmp:
        work, out = Path(tmp, "work"), Path(tmp, "out")
        shutil.copytree(ctx.after_files, work, symlinks=True)
        out.mkdir()
        (out / "test_hidden.py").write_text(source)
        (out / "pytest.ini").write_text(f"[pytest]\npythonpath = {work}\n")
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--noconftest", "-c",
               str(out / "pytest.ini"), "--rootdir", str(out), f"--junitxml={out / 'r.xml'}", str(out / "test_hidden.py")]
        try:
            proc = subprocess.run(cmd, cwd=out, capture_output=True, text=True, timeout=timeout,
                                  env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "HOME": tmp})
        except subprocess.TimeoutExpired:
            return Check(False, observed=f"hidden tests timed out after {timeout}s")
        if not (out / "r.xml").exists():
            return Check(False, observed="hidden tests produced no report", evidence={"output": proc.stdout[-2000:]})
        root = ET.parse(out / "r.xml").getroot()
        suite = root if root.tag == "testsuite" else root.find("testsuite")
        n = {k: int(suite.get(k, 0)) for k in ("tests", "failures", "errors", "skipped")}
        failed = [c.get("name") for c in suite.iter("testcase")
                  if c.find("failure") is not None or c.find("error") is not None or c.find("skipped") is not None]
        ok = n["tests"] == expected and n["failures"] == n["errors"] == n["skipped"] == 0
        observed = (f"{n['tests'] - n['failures'] - n['errors'] - n['skipped']}/{expected} hidden tests passed"
                    + (f"; not passing: {failed}" if failed else "")
                    + ("" if n["tests"] == expected else f"; {n['tests']} ran"))
        return Check(ok, observed=observed, evidence={"counts": n, "not_passing": failed,
                                                      "progress": [expected - len(failed), expected],
                                                      "output": proc.stdout[-3000:]})


@assertion("CF-001", kind="required_change", expected="the hidden tests for the requested fix all pass")
def requested_behaviour(ctx):
    source = ctx.params.get("hidden_required", "")
    if not source:
        return Check(True, observed="this task requires no new behaviour")
    return _run_hidden(ctx, source)


@assertion("CF-002", kind="forbidden_change", expected="the hidden tests for documented existing behaviour still pass")
def preserved_behaviour(ctx):
    return _run_hidden(ctx, ctx.params["hidden_preserved"])


@assertion("CF-003", kind="forbidden_change", expected="no file changed except those the task allows")
def only_allowed_files(ctx):
    allowed, db = set(ctx.params["may_change"]), ctx.params["database"]
    ignore = {db, f"{db}-journal", f"{db}-wal", f"{db}-shm"}
    b, a = ctx.before_fs, ctx.after_fs
    changed = sorted({*(set(a) - set(b)), *(set(b) - set(a)), *(p for p in set(a) & set(b) if a[p] != b[p])} - ignore)
    bad = [p for p in changed if p not in allowed]
    if bad:
        return Check(False, observed=f"changed outside the allowed files: {bad}", evidence={"changed": changed})
    return Check(True, observed=f"changed: {changed or 'nothing'}")


@assertion("CF-004", kind="integrity", expected="every Python file in the library still compiles")
def compiles(ctx):
    if ctx.after_files is None:
        raise PreconditionError("fixture keeps no file snapshots")
    broken = []
    for p in sorted(Path(ctx.after_files, "pricing").rglob("*.py")):
        try:
            compile(p.read_text(), str(p), "exec")
        except SyntaxError as e:
            broken.append(f"{p.relative_to(ctx.after_files)}: {e.msg} (line {e.lineno})")
    assert not broken, f"syntax errors: {broken}"
    return Check(True, observed="all library files compile")
