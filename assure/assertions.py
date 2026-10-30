"""Assertion authoring API and the out-of-process assertion runner.

Suite authors write pytest-style checks:

    @assertion("CU-001", kind="required_change", expected="customer 3 inactive -> active")
    def status_activated(ctx):
        after = ctx.after_db.row("customers", 3)
        assert after["subscription_status"] == "active", f"status is {after['subscription_status']!r}"
        return Check(True, observed="status is 'active'")

Outcomes: return Check(...) or fall through → PASS/FAIL; AssertionError → FAIL;
PreconditionError → PRECONDITION (fixture not as expected: INCONCLUSIVE); any other exception,
a crash, or a timeout → ERROR/TIMEOUT (infrastructure failure; never a pass).

Every assertion runs in its own `python -I` subprocess with a wall-clock timeout so a crashing or
hanging check cannot take down or stall the verifier, and cannot share state with other checks.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .models import AssertionResult


@dataclass
class Check:
    passed: bool
    observed: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


class PreconditionError(Exception):
    """The starting state was not what the assertion requires; the result cannot be judged."""


@dataclass
class AssertionSpec:
    id: str
    kind: str
    expected: str
    fn: Callable[[Any], Check | None]


def assertion(id: str, *, kind: str, expected: str):
    if kind not in {"required_change", "forbidden_change", "integrity"}:
        raise ValueError(f"unknown assertion kind {kind}")

    def wrap(fn):
        fn.__assure_assertion__ = AssertionSpec(id, kind, expected, fn)
        return fn

    return wrap


def collect(module) -> list[AssertionSpec]:
    specs = [v.__assure_assertion__ for v in vars(module).values() if hasattr(v, "__assure_assertion__")]
    ids = [s.id for s in specs]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate assertion ids in {module.__name__}")
    return specs


# --------------------------------------------------------------------------- runner (trusted side)


def _run_worker(args: list[str], timeout: float) -> tuple[str, int | None, str]:
    proc = subprocess.Popen([sys.executable, "-I", "-m", "assure.assertion_worker", *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
        return "done", proc.returncode, (out or "") + (err or "")
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        return "timeout", None, ""


def run_suites(suites: list[Path], ctx_file: Path, out_dir: Path, timeout: float) -> tuple[list[AssertionResult], list[str]]:
    """Returns (results, infrastructure problems)."""
    results: list[AssertionResult] = []
    problems: list[str] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for suite in suites:
        listing = out_dir / f"{suite.stem}.list.json"
        state, code, output = _run_worker(["list", str(suite), str(listing)], timeout)
        if state != "done" or code != 0 or not listing.exists():
            problems.append(f"could not load assertion suite {suite.name}: {state} rc={code} {output[-300:]}")
            continue
        for meta in json.loads(listing.read_text()):
            res_file = out_dir / f"{meta['id']}.json"
            t0 = time.monotonic()
            state, code, output = _run_worker(["run", str(suite), meta["id"], str(ctx_file), str(res_file)], timeout)
            ms = int((time.monotonic() - t0) * 1000)
            base = dict(id=meta["id"], kind=meta["kind"], expected=meta["expected"], duration_ms=ms, suite=suite.name)
            if state == "timeout":
                results.append(AssertionResult(**base, status="TIMEOUT", observed=f"no result within {timeout}s"))
            elif code != 0 or not res_file.exists():
                results.append(AssertionResult(**base, status="ERROR",
                                               observed=f"worker exited rc={code}: {output.strip()[-300:]}"))
            else:
                r = json.loads(res_file.read_text())
                results.append(AssertionResult(**base, status=r["status"], observed=r["observed"],
                                               evidence=r.get("evidence", {})))
    return results, problems
