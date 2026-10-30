"""Verifier fault injection, used to prove that a broken verifier can never produce a PASS.

These are assertions that fail *as code*, not as checks. A scenario opts in by listing them under
`fault_injection`; the orchestrator then freezes a suite that imports only the selected faults.
"""

import os
import time

from .assertions import assertion


@assertion("FI-CRASH", kind="integrity", expected="fault injection: assertion raises an exception")
def assertion_crash(ctx):
    raise RuntimeError("injected verifier fault")


@assertion("FI-HARD-EXIT", kind="integrity", expected="fault injection: worker process dies")
def assertion_hard_exit(ctx):
    os._exit(3)


@assertion("FI-TIMEOUT", kind="integrity", expected="fault injection: assertion never returns")
def assertion_timeout(ctx):
    while True:
        time.sleep(1)


FAULTS = {"assertion_crash": "assertion_crash", "assertion_hard_exit": "assertion_hard_exit",
          "assertion_timeout": "assertion_timeout"}
