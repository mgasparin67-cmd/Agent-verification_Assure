"""Run lifecycle: create sandbox → freeze spec → snapshot → run agent via gateway → quiesce →
verify → report → destroy sandbox."""

from __future__ import annotations

import json
import secrets
import shutil
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml

from . import __version__
from .assertions import run_suites
from .attribution import unattributed_changes
from .fault_injection import FAULTS
from .gateway import EvidenceLog, Gateway, now_iso
from .models import EVENT_SCHEMA_VERSION, AssertionResult, Finding, Strict
from .policy import ApprovalGrant, PolicyEngine, load_policy
from .redact import Redactor
from .report import EXIT_CODES, assertion_findings, dimension_verdicts, overall_verdict
from .runner import AgentProcess, collect_pass_env, isolation_for, serve
from .sandbox import Fixture, SandboxManager
from .snapshot import DbView, db_diff, files_diff, fs_diff
from .tools import Workspace, describe_tools
from .trajectory import evaluate


class SuiteRef(Strict):
    path: str
    id: str
    version: str


class TaskSpec(Strict):
    task_id: str
    version: str
    instruction: str
    fixture: str
    policy: str
    assertion_suite: SuiteRef
    params: dict[str, Any] = {}
    approvals: list[ApprovalGrant] = []
    assertion_timeout_seconds: float = 10.0
    # Set when the correct outcome is for the agent to report failure (e.g. the target does not exist).
    expected_final_status: Literal["succeeded", "failed"] | None = None
    # Faults the gateway injects. Recorded as such in the evidence; the agent sees only the error.
    tool_faults: list[ToolFault] = []


class ToolFault(Strict):
    """Environment fault: the nth permitted call of `tool` fails without executing (e.g. a lock).

    With `nth_cycle`, the position depends on the run's variant (the bench passes attempt - 1):
    nth_cycle[variant % len]. Repeated runs then hit different writes, reproducibly.
    """

    tool: str
    nth: int = 1
    nth_cycle: list[int] = []
    error_type: str = "OperationalError"
    message: str = "database is locked"

    def resolved(self, variant: int) -> dict[str, Any]:
        nth = self.nth_cycle[variant % len(self.nth_cycle)] if self.nth_cycle else self.nth
        return {"tool": self.tool, "nth": nth, "error_type": self.error_type, "message": self.message}


class AgentSpec(Strict):
    kind: Literal["mock", "command", "claude", "mcp"]  # mcp: `command` is the MCP agent's command template
    behavior: str | None = None
    command: list[str] | None = None
    model: str | None = None


class Scenario(Strict):
    scenario: str
    description: str
    task: str
    agent: AgentSpec
    fault_injection: list[Literal["assertion_crash", "assertion_hard_exit", "assertion_timeout"]] = []
    assertion_timeout_seconds: float | None = None
    pass_env: list[str] = []  # host variables the agent itself needs (e.g. its LLM API key)


def load_scenario(project: Path, name: str) -> Scenario:
    path = project / "scenarios" / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"unknown scenario '{name}' ({path})")
    return Scenario.model_validate(yaml.safe_load(path.read_text()))


def agent_argv(spec: AgentSpec, isolation: str = "local") -> list[str]:
    if spec.kind == "mock":
        if isolation == "docker":  # the agent image's entrypoint is the mock agent
            return ["--behavior", spec.behavior or "correct"]
        return [sys.executable, "-I", "-m", "assure.agents.mock_agent", "--behavior", spec.behavior or "correct"]
    if spec.kind == "claude":
        model = ["--model", spec.model] if spec.model else []
        if isolation == "docker":
            raise ValueError("the Claude bridge needs network egress; docker mode runs with --network none")
        return [sys.executable, "-I", "-m", "assure.agents.claude_agent", *model]
    if not spec.command:
        raise ValueError(f"{spec.kind} agent requires 'command'")
    if spec.kind == "mcp":
        if isolation == "docker":
            raise ValueError("MCP agents run in local mode only for now")
        return [sys.executable, "-I", "-m", "assure.agents.mcp_adapter", "--", *spec.command]
    return list(spec.command)


def new_run_id() -> str:
    return f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"


def _f(id, dim, outcome, control, expected, observed, explanation, refs=(), evidence=None) -> Finding:
    return Finding(id=id, dimension=dim, outcome=outcome, control=control, expected=expected,
                   observed=observed, explanation=explanation, refs=list(refs), evidence=evidence or {})


def execute_run(*, project: Path, task_path: Path, argv: list[str], runs_dir: Path, isolation: str = "local",
                scenario: str | None = None, faults: list[str] = (), assertion_timeout: float | None = None,
                keep_sandbox: bool = False, run_id: str | None = None,
                docker_image: str | None = None, pass_env: list[str] = (), variant: int = 0) -> dict[str, Any]:
    run_id = run_id or new_run_id()
    started = now_iso()
    task_src = TaskSpec.model_validate(yaml.safe_load(task_path.read_text()))
    fixture = Fixture.load((project / task_src.fixture).resolve())
    sandbox = SandboxManager(runs_dir)

    if isolation == "docker" and "run_tests" in load_policy(task_path.parent / task_src.policy).tools.allow:
        raise ValueError("run_tests executes agent-written code on the gateway's host; "
                         "it is not available in docker isolation yet")
    if isolation == "docker" and shutil.which("docker") is None:
        raise RuntimeError("docker isolation requested but the docker CLI is not available")
    extra_env = collect_pass_env(list(pass_env))

    # 1. Clean sandbox from the versioned fixture.
    dirs = sandbox.create(run_id, fixture)

    # 2. Freeze human-authored spec (copied read-only, digests recorded) and load from the copy.
    frozen = {"task.yaml": task_path, "policy.yaml": task_path.parent / task_src.policy,
              "assertions.py": task_path.parent / task_src.assertion_suite.path}
    if faults:
        fault_src = dirs.root / "fault_suite.py"
        fault_src.write_text("from assure.fault_injection import " + ", ".join(FAULTS[f] for f in faults) + "\n")
        frozen["fault_suite.py"] = fault_src
    spec_digests = sandbox.freeze(dirs, frozen)
    task = TaskSpec.model_validate(yaml.safe_load((dirs.verifier / "task.yaml").read_text()))
    policy = load_policy(dirs.verifier / "policy.yaml")

    # 3. Record initial state.
    before = sandbox.snapshot(dirs, fixture, "before")

    # 4. Run the agent through the gateway.
    redactor = Redactor(fixture.known_secrets + list(extra_env.values()))
    ws = Workspace(dirs.workspace, fixture.database)
    engine = PolicyEngine(policy, task.approvals,
                          protected_roots=[dirs.verifier, dirs.evidence, project / "specs", project / "assure"])
    deadline = time.monotonic() + policy.limits.max_wall_seconds
    faults_applied = [f.resolved(variant) for f in task.tool_faults]
    gateway = Gateway(run_id, ws, engine, dirs.evidence, redactor, deadline, faults=faults_applied)
    iso = isolation_for(isolation)
    gateway.record(event_type="run_started", started_at=started,
                   detail={"task_id": task.task_id, "policy": f"{policy.policy_id}@{policy.version}",
                           "fixture": f"{fixture.fixture_id}@{fixture.version}", "isolation": iso.mode})
    agent_home = dirs.root / "agent_home"
    agent_home.mkdir()
    try:
        agent = AgentProcess(argv, mode=isolation, cwd=agent_home, docker_image=docker_image,
                             cpu_seconds=int(policy.limits.max_wall_seconds) + 5, extra_env=extra_env)
    except OSError as e:
        ws.close()
        sandbox.destroy(dirs)
        raise RuntimeError(f"agent could not be started ({argv[0]}): {e.strerror or e}") from e
    task_msg = {"type": "task", "run_id": run_id, "task_id": task.task_id, "instruction": task.instruction,
                "event_schema": EVENT_SCHEMA_VERSION, "tools": describe_tools(policy.tools.allow)}
    how = serve(gateway, agent, task_msg, deadline)

    # 5. Stop the agent and quiesce.
    exit_code = agent.stop()
    (dirs.evidence / "agent_stderr.log").write_text(redactor.text("".join(agent.stderr))[:65536])
    gateway.close(how, exit_code)

    # 6. Verify against final state.
    findings: list[Finding] = []
    spec_problems = sandbox.verify_frozen(dirs, spec_digests)
    for p in spec_problems:
        findings.append(_f("integrity.frozen_spec", "verification_infrastructure", "FAIL", "integrity",
                           "frozen spec digests unchanged", p, "Verification assets changed during the run."))
    after = sandbox.snapshot(dirs, fixture, "after")
    bdb, adb = DbView(before["db"]), DbView(after["db"])
    dbd = db_diff(bdb, adb)
    fsd = fs_diff(before["fs"], after["fs"])
    bdb.close()
    adb.close()

    for label, path, dim in (("gateway", dirs.evidence / "gateway_events.jsonl", "policy_compliance"),
                             ("agent", dirs.evidence / "agent_events.jsonl", "evidence_completeness")):
        for p in EvidenceLog.verify(path):
            findings.append(_f(f"integrity.{label}_log", "verification_infrastructure", "FAIL", "integrity",
                               "hash chain verifies", p, f"{label} evidence log failed its integrity check."))
            findings.append(_f(f"integrity.{label}_log", dim, "INCONCLUSIVE", "integrity", "hash chain verifies",
                               p, "Evidence for this dimension cannot be relied on."))

    agent_records = [json.loads(l) for l in (dirs.evidence / "agent_events.jsonl").read_text().splitlines()]
    traj = evaluate(run_id, gateway.events, agent_records, policy.telemetry, dbd["changes"],
                    expected_final_status=task.expected_final_status)

    # Preventive findings: every refused request is a policy failure, even though it had no effect.
    # Unless the policy lists the refusal under denials.warn, in which case it is recorded only.
    for ev in gateway.events:
        if ev.event_type == "tool_call" and ev.status == "denied":
            warn = policy.denials.is_warning(ev.tool, ev.policy.rule_ids)
            for rule in ev.policy.rule_ids:
                findings.append(_f(
                    rule, "policy_compliance", "WARN" if warn else "FAIL", "preventive",
                    f"request permitted by rule {rule}",
                    f"{ev.tool}({json.dumps(ev.arguments, default=str)[:120]}) denied: "
                    + "; ".join(ev.policy.reasons)[:200],
                    ("Tampering attempt against verification assets; blocked before execution."
                     if rule.startswith("tamper.") else
                     "Out-of-scope read blocked before execution; policy denials.warn records it as a "
                     "warning, so it does not affect the verdict." if warn else
                     "Blocked before execution; no side effect occurred."),
                    refs=[ev.call_id, f"step:{ev.step_id}", f"gateway_seq:{ev.seq}"],
                    evidence={"status": ev.status, "side_effect": "none (refused before execution)",
                              "resources": [r.model_dump() for r in ev.resources]}))
    if gateway.deadline_exceeded or how == "deadline":
        findings.append(_f("limits.max_wall_seconds", "policy_compliance", "FAIL", "preventive",
                           f"agent finishes within {policy.limits.max_wall_seconds}s", f"stopped: {how}",
                           "Agent exceeded its time budget and was killed."))
    for v in engine.detect(gateway.records, agent_step_count=len(traj.events)):
        findings.append(_f(v.rule_id, "policy_compliance", "FAIL", "detective", "rule holds over trajectory",
                           v.reason, "Detected after execution; not prevented.", refs=v.refs))
    findings += traj.findings
    findings += unattributed_changes(gateway.records, dbd["rows"], fsd, fixture.database)

    # Trusted deterministic assertions, outside the agent's reach, one subprocess each.
    ctx_file = dirs.evidence / "assertion_ctx.json"
    if "files" in after:
        (dirs.evidence / "workspace.diff").write_text(redactor.text(files_diff(before["files"], after["files"], fsd)))
    ctx_file.write_text(json.dumps({"before_db": str(before["db"]), "after_db": str(after["db"]),
                                    "before_fs": before["fs"], "after_fs": after["fs"], "params": task.params,
                                    "before_files": str(before.get("files") or ""),
                                    "after_files": str(after.get("files") or "")}))
    suites = [dirs.verifier / "assertions.py"] + ([dirs.verifier / "fault_suite.py"] if faults else [])
    timeout = assertion_timeout or task.assertion_timeout_seconds
    if spec_problems:
        results, problems = [], ["frozen spec changed; assertions not run"]
    else:
        results, problems = run_suites(suites, ctx_file, dirs.evidence / "assertions", timeout)
    for p in problems:
        findings.append(_f("assertions.suite_error", "verification_infrastructure", "FAIL", "verification",
                           "assertion suite loads", p, "Assertion suite could not run."))
        findings.append(_f("assertions.suite_error", "task_correctness", "INCONCLUSIVE", "verification",
                           "assertion suite loads", p, "Correctness was not evaluated."))
    findings += assertion_findings(results)

    # 7–8. Aggregate and report.
    dims = dimension_verdicts(findings)
    overall = overall_verdict(dims)
    report = build_report(run_id=run_id, scenario=scenario, started=started, task=task, policy=policy,
                          fixture=fixture, spec_digests=spec_digests, iso=iso, how=how, exit_code=exit_code,
                          gateway=gateway, traj=traj, dbd=dbd, fsd=fsd, results=results, findings=findings,
                          dims=dims, overall=overall, dirs=dirs, pass_env=sorted(extra_env),
                          variant=variant, faults_applied=faults_applied)
    (dirs.evidence / "report.json").write_text(json.dumps(report, indent=2, default=str))

    # 9. Destroy the sandbox; evidence is kept.
    if not keep_sandbox:
        sandbox.destroy(dirs)
    return report


def build_report(*, run_id, scenario, started, task, policy, fixture, spec_digests, iso, how, exit_code,
                 gateway: Gateway, traj, dbd, fsd, results: list[AssertionResult], findings: list[Finding],
                 dims, overall, dirs, pass_env=(), variant=0, faults_applied=()) -> dict[str, Any]:
    calls = [e for e in gateway.events if e.event_type == "tool_call"]
    unverified = sorted({f"{f.id}: {f.observed}" for f in findings if f.outcome == "INCONCLUSIVE"})
    unverified += [f"isolation: {n}" for n in iso.notes if not iso.boundary_enforced]
    unverified += ["Side effects outside the monitored workspace and SQLite database are not observed.",
                   "Properties not covered by an assertion are not verified."]
    return {
        "report_schema": "assure.report/v1",
        "run": {
            "run_id": run_id, "scenario": scenario, "started_at": started, "finished_at": now_iso(),
            "assure_version": __version__, "event_schema": EVENT_SCHEMA_VERSION,
            "versions": {"task": f"{task.task_id}@{task.version}", "policy": f"{policy.policy_id}@{policy.version}",
                         "fixture": f"{fixture.fixture_id}@{fixture.version}",
                         "assertion_suite": f"{task.assertion_suite.id}@{task.assertion_suite.version}"},
            "frozen_spec_sha256": spec_digests,
            "isolation": {"mode": iso.mode, "boundary_enforced": iso.boundary_enforced,
                          "network_isolated": iso.network_isolated, "notes": iso.notes},
            "agent_stop": how, "agent_exit_code": exit_code,
            "agent_env_passthrough": list(pass_env),  # names only; values are redacted everywhere
            "variant": variant,
        },
        "task": {"task_id": task.task_id, "instruction": task.instruction, "params": task.params,
                 "tool_faults": [f.model_dump() for f in task.tool_faults],
                 "tool_faults_applied": list(faults_applied),
                 "expected_final_status": task.expected_final_status,
                 "approvals": [a.model_dump() for a in task.approvals]},
        "claims": {
            "note": "Untrusted agent statements. They have no authority over any verdict.",
            "steps": [{"seq": e.seq, "step_id": e.step_id, "action_type": e.action_type, "tool": e.tool,
                       "call_id": e.call_id, "claim": e.claim.model_dump()} for e in traj.events],
            "final": ({"outcome": traj.final_claim.claim.outcome, "status": traj.final_claim.claim.status,
                       "claimed_changes": [c.model_dump() for c in traj.final_claim.claimed_changes]}
                      if traj.final_claim else None),
        },
        "observed_actions": [{"seq": e.seq, "step_id": e.step_id, "call_id": e.call_id, "tool": e.tool,
                              "arguments": e.arguments, "status": e.status,
                              "policy": e.policy.model_dump() if e.policy else None,
                              "started_at": e.started_at, "ended_at": e.ended_at,
                              "result": e.result.model_dump() if e.result else None,
                              "observed": e.observed, "error": e.error.model_dump() if e.error else None}
                             for e in calls],
        "stats": {"call_status": dict(Counter(e.status for e in calls))},
        "sandbox_changes": {"db": {"schema_changed": dbd["schema_changed"], "rows": dbd["rows"],
                                   **({"schema": dbd["schema"]} if "schema" in dbd else {})},
                            "fs": fsd},
        "assertions": [r.model_dump() for r in results],
        "findings": [f.model_dump() for f in findings],
        "unverified": unverified,
        "dimensions": dims,
        "overall": overall,
        "exit_code": EXIT_CODES[overall],
        "evidence": {
            "dir": str(dirs.evidence), "report": str(dirs.evidence / "report.json"),
            "gateway_log": {"path": str(dirs.evidence / "gateway_events.jsonl"), "head": gateway.gw_log.prev,
                            "records": gateway.gw_log.count},
            "agent_log": {"path": str(dirs.evidence / "agent_events.jsonl"), "head": gateway.agent_log.prev,
                          "records": gateway.agent_log.count},
            "hash_chain_threat_model": "Detects truncation/edits by parties that do not recompute the chain. "
                                       "Does not make agent claims true or complete.",
        },
    }
