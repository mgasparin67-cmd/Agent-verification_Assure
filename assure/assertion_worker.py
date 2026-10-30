"""Subprocess entry point that loads one frozen suite and runs one assertion.

    python -I -m assure.assertion_worker list <suite.py> <out.json>
    python -I -m assure.assertion_worker run  <suite.py> <assertion_id> <ctx.json> <out.json>
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from .assertions import Check, PreconditionError, collect
from .snapshot import DbView


class Ctx:
    def __init__(self, data: dict):
        self.before_db = DbView(data["before_db"])
        self.after_db = DbView(data["after_db"])
        self.before_fs = data["before_fs"]
        self.after_fs = data["after_fs"]
        self.params = data["params"]


def load(path: str):
    spec = importlib.util.spec_from_file_location(f"assure_suite_{Path(path).stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str]) -> int:
    cmd = argv[0]
    module = load(argv[1])
    specs = {s.id: s for s in collect(module)}
    if cmd == "list":
        Path(argv[2]).write_text(json.dumps(
            [{"id": s.id, "kind": s.kind, "expected": s.expected} for s in specs.values()]))
        return 0
    spec = specs[argv[2]]
    ctx = Ctx(json.loads(Path(argv[3]).read_text()))
    try:
        res = spec.fn(ctx)
        if res is None:
            res = Check(True, observed="all assertions held")
        out = {"status": "PASS" if res.passed else "FAIL", "observed": res.observed, "evidence": res.evidence}
    except AssertionError as e:
        out = {"status": "FAIL", "observed": str(e) or "assertion failed", "evidence": {}}
    except PreconditionError as e:
        out = {"status": "PRECONDITION", "observed": str(e), "evidence": {}}
    except Exception as e:  # noqa: BLE001 - any verifier bug is reported as ERROR, never as a pass
        out = {"status": "ERROR", "observed": f"{type(e).__name__}: {e}"[:300], "evidence": {}}
    Path(argv[4]).write_text(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
