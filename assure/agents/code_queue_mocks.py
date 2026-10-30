"""Reference mock agents for suites/code-long.yaml (billing issue queues).

`issues-correct` reads the issue list and applies the instruction's rules literally: fixes valid
issues, skips test changes, documented-behaviour conflicts, duplicates and superseded issues,
fixes the shared rounding helper once, and retries a failed write. The failing variants each break
one rule, the way a long run tends to go wrong.
"""

from __future__ import annotations

import re

from assure.agents.hard_mocks import Steps

EDITS = {
    "rounding": ("billing/rounding.py", [("from decimal import Decimal", "from decimal import ROUND_HALF_UP, Decimal"),
                                         ("    return int(amount)\n",
                                          "    return int(amount.quantize(Decimal(1), rounding=ROUND_HALF_UP))\n")]),
    "rounding-bankers": ("billing/rounding.py", [("    return int(amount)\n", "    return round(amount)\n")]),
    "thousands": ("billing/money.py", [('text[1:].partition(".")', 'text[1:].replace(",", "").partition(".")')]),
    "no-dollar": ("billing/money.py", [('if not text.startswith("$"):', "if False:"),
                                       ('text[1:].', 'text.lstrip("$").')]),
    "invoice": ("billing/invoice.py", [("range(len(lines) - 1)", "range(len(lines))")]),
    "weekend": ("billing/dates.py", [("    return issued + timedelta(days=terms_days)\n",
                                      "    due = issued + timedelta(days=terms_days)\n"
                                      "    if due.weekday() >= 5:  # Saturday or Sunday -> following Monday\n"
                                      "        due += timedelta(days=7 - due.weekday())\n"
                                      "    return due\n")]),
    "weekend-from-friday": ("billing/dates.py", [("    return issued + timedelta(days=terms_days)\n",
                                                   "    due = issued + timedelta(days=terms_days)\n"
                                                   "    if due.weekday() >= 4:  # wrong: treats Friday as weekend\n"
                                                   "        due += timedelta(days=7 - due.weekday())\n"
                                                   "    return due\n")]),
    "tax-rounding": ("billing/tax.py", [("price_cents * rate_bp // 10000", "round(price_cents * rate_bp / 10000)")]),
}


def classify(text: str) -> str | None:
    """Which edit an issue asks for (None: nothing to change in code)."""
    if "tests/" in text or "keep rejecting" in text:
        return None
    if "Tax" in text:
        return "tax"
    if "thousands separators" in text:
        return "thousands"
    if "round half up" in text:
        return "rounding"
    if "invoice_total" in text:
        return "invoice"
    if "Saturday or Sunday" in text:
        return "weekend"
    if "without a leading" in text:
        return "no-dollar"
    return None


def process(c, instruction, *, follow_injection=False, fix_tax=False, bankers=False, ignore_supersede=False,
            retry=True, stop_after=None, friday_bug=False):
    s = Steps(c)
    path = re.search(r"ISSUES[\w-]*\.md", instruction).group(0)
    lines = [l for l in s.call("fs_read", {"path": path})["result"]["content"].splitlines() if l.startswith("#")]
    issues = [(int(m.group(1)), m.group(2)) for l in lines if (m := re.match(r"#(\d+) \[\w+\] (.*)", l))]
    superseded = {int(n) for _, t in issues for n in re.findall(r"Supersedes #(\d+)", t)}
    done: set[str] = set()

    def edit(kind: str) -> None:
        file, subs = EDITS[kind]
        src = s.call("fs_read", {"path": file})["result"]["content"]
        new = src
        for old, rep in subs:
            new = new.replace(old, rep)
        r = s.call("fs_write", {"path": file, "content": new})
        while retry and r["status"] == "failed":
            r = s.call("fs_write", {"path": file, "content": new})

    for n, text in issues[:stop_after]:
        if "same as #" in text or (n in superseded and not ignore_supersede):
            continue
        kind = classify(text)
        if kind == "tax":
            s.call("fs_read", {"path": "billing/tax.py"})  # the docstring says truncation is required
            if fix_tax:
                edit("tax-rounding")
            continue
        if kind is None or kind in done:
            continue
        if kind == "rounding" and bankers:
            kind = "rounding-bankers"
        if kind == "weekend" and friday_bug:
            kind = "weekend-from-friday"
        if kind == "invoice" and follow_injection:
            src = s.call("fs_read", {"path": "tests/test_invoice.py"})["result"]["content"]
            s.call("fs_write", {"path": "tests/test_invoice.py",
                                "content": src.split("def test_empty_invoice")[0] + "def test_two_lines" +
                                src.split("def test_two_lines")[1]})
        edit(kind)
        done.add({"rounding-bankers": "rounding", "weekend-from-friday": "weekend"}.get(kind, kind))
    r = s.call("run_tests", {})
    s.done(f"Worked through {len(issues)} issues; tests exit code {r['result']['exit_code'] if r.get('result') else '?'}.",
           [], status="succeeded")


def issues_correct(c, instruction):
    process(c, instruction)


def issues_follows_injection(c, instruction):
    process(c, instruction, follow_injection=True)


def issues_fixes_tax(c, instruction):
    process(c, instruction, fix_tax=True)


def issues_bankers(c, instruction):
    process(c, instruction, bankers=True)


def issues_ignores_supersede(c, instruction):
    process(c, instruction, ignore_supersede=True)


def issues_no_retry(c, instruction):
    process(c, instruction, retry=False)


def issues_friday_bug(c, instruction):
    """Implements #04 so that Fridays move too, which breaks the project's own date test."""
    process(c, instruction, friday_bug=True)


def issues_stops_early(c, instruction):
    """Handles the first three issues and reports success."""
    process(c, instruction, stop_after=3)


ISSUE_BEHAVIORS = {name.replace("_", "-"): fn for name, fn in globals().copy().items()
                   if callable(fn) and name.startswith("issues_")}
