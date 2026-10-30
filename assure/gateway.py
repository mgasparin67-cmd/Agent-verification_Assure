"""Execution gateway: the agent's only supported route to tools.

For each request: validate the message → validate arguments → derive resources (plan) →
evaluate preventive policy → execute or refuse → append a trusted event to the evidence log.
Agent telemetry arriving on the same channel is stored verbatim (redacted) as untrusted claims.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import ErrorInfo, GatewayEvent, PolicyDecision, Resource, ResultRef, ToolRequest, digest
from .paths import PathError
from .policy import CallRecord, PlannedCall, PolicyEngine, Violation
from .redact import Redactor
from .tools import REGISTRY, RowLimitExceeded, ToolInvalid, Workspace

GENESIS = "sha256:genesis"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class EvidenceLog:
    """Append-only, hash-chained JSONL.

    Threat model: the chain lets the verifier detect truncation or in-place edits of the log made
    by anything that does not also recompute every later hash (accidents, sloppy tooling, a
    partial write). It is NOT a proof of truthfulness: the gateway log is trustworthy only because
    the gateway wrote it, and the agent log is a record of what the agent *said*. Anyone with write
    access to the evidence directory can rebuild a valid chain; the agent has no such access.
    """

    def __init__(self, path: Path):
        self.path = path
        self.prev = GENESIS
        self.count = 0
        path.touch()

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        record = {**record, "prev_hash": self.prev, "hash": ""}
        record["hash"] = digest(record)
        with self.path.open("a") as f:
            f.write(json.dumps(record, sort_keys=True, default=str) + "\n")
            f.flush()
        self.prev = record["hash"]
        self.count += 1
        return record

    @staticmethod
    def verify(path: Path) -> list[str]:
        problems, prev = [], GENESIS
        for n, line in enumerate(path.read_text().splitlines(), 1):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                problems.append(f"line {n}: not JSON")
                break
            claimed = rec.get("hash")
            if rec.get("prev_hash") != prev:
                problems.append(f"line {n}: prev_hash does not chain")
            if digest({**rec, "hash": ""}) != claimed:
                problems.append(f"line {n}: hash mismatch")
            prev = claimed
        return problems


class Gateway:
    def __init__(self, run_id: str, ws: Workspace, engine: PolicyEngine, evidence_dir: Path,
                 redactor: Redactor, deadline_monotonic: float, faults: list[dict[str, Any]] = ()):
        self.run_id = run_id
        self.ws = ws
        self.engine = engine
        self.redactor = redactor
        self.deadline = deadline_monotonic
        self.evidence_dir = evidence_dir
        (evidence_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        self.gw_log = EvidenceLog(evidence_dir / "gateway_events.jsonl")
        self.agent_log = EvidenceLog(evidence_dir / "agent_events.jsonl")
        self.events: list[GatewayEvent] = []
        self.records: list[CallRecord] = []
        self.closed = False
        self.deadline_exceeded = False
        self.faults = list(faults)
        self.permitted: dict[str, int] = {}  # per tool: calls that passed policy

    # ------------------------------------------------------------------ recording

    def record(self, **fields: Any) -> GatewayEvent:
        ev = GatewayEvent(run_id=self.run_id, seq=len(self.events) + 1, **fields)
        stored = self.gw_log.append(ev.model_dump(mode="json"))
        ev = GatewayEvent.model_validate(stored)
        self.events.append(ev)
        return ev

    def _artifact(self, call_id: str, result: dict[str, Any]) -> ResultRef:
        redacted = self.redactor.value(result)
        body = json.dumps(redacted, sort_keys=True, default=str)
        rel = f"artifacts/{call_id}.json"
        (self.evidence_dir / rel).write_text(body)
        summary = ", ".join(f"{k}={v}" if not isinstance(v, (list, dict, str)) else
                            f"{k}=<{type(v).__name__} len {len(v)}>" for k, v in redacted.items())
        return ResultRef(artifact=rel, sha256=digest(redacted), bytes=len(body), summary=summary[:120])

    # ------------------------------------------------------------------ tool requests

    def handle_tool_request(self, raw: dict[str, Any]) -> dict[str, Any]:
        started = now_iso()
        call_id = "c-" + secrets.token_hex(8)
        step_id = raw.get("step_id") if isinstance(raw.get("step_id"), str) else None
        raw_args = raw.get("arguments") if isinstance(raw.get("arguments"), dict) else {}
        base = dict(event_type="tool_call", call_id=call_id, step_id=step_id, action_type="tool_call",
                    tool=str(raw.get("tool"))[:64] if raw.get("tool") is not None else None,
                    parent_step_id=raw.get("parent_step_id") if isinstance(raw.get("parent_step_id"), str) else None,
                    arguments=self.redactor.value(raw_args), arguments_digest=digest(raw_args),
                    started_at=started)

        def finish(status: str, *, planned: PlannedCall | None = None, violations: list[Violation] = (),
                   error: ErrorInfo | None = None, result: dict | None = None, observed: dict | None = None,
                   rule_override: str | None = None) -> dict[str, Any]:
            rule_ids = [v.rule_id for v in violations] + ([rule_override] if rule_override else [])
            decision = PolicyDecision(decision="deny" if status == "denied" else "allow", rule_ids=rule_ids,
                                      reasons=[v.reason for v in violations] + ([error.message] if rule_override and error else []),
                                      tamper=any(v.tamper for v in violations))
            self.record(**base, status=status, ended_at=now_iso(), policy=decision, error=error,
                             resources=planned.resources if planned else
                             ([Resource(kind="tool", target=base["tool"] or "?", access="execute")]),
                             result=self._artifact(call_id, result) if result is not None else None,
                             observed=observed or {})
            self.records.append(CallRecord(
                call_id=call_id, step_id=step_id or "", tool=base["tool"] or "", status=status,
                arguments_digest=base["arguments_digest"],
                paths=[rp.relative for rp, _ in planned.paths if rp.relative] if planned else [],
                table=planned.table if planned else None, where=planned.where if planned else None,
                rows=planned.rows if planned else None,
                arguments=raw_args))
            resp: dict[str, Any] = {"type": "tool_response", "step_id": step_id, "call_id": call_id, "status": status}
            if result is not None:
                resp["result"] = result
            if error is not None:
                resp["error"] = {"type": error.type, "message": error.message, "rule_ids": decision.rule_ids}
            return resp

        try:
            req = ToolRequest.model_validate(raw)
        except ValidationError as e:
            return finish("invalid", error=ErrorInfo(type="malformed_request", message=_short(e)))

        if self.closed or time.monotonic() > self.deadline:
            self.deadline_exceeded = True
            return finish("denied", error=ErrorInfo(type="policy_denied", message="run deadline exceeded"),
                          rule_override="limits.max_wall_seconds")

        tool = REGISTRY.get(req.tool)
        if tool is None:
            v = self.engine.check_tool_name(req.tool, known=False)
            return finish("denied", violations=v, error=ErrorInfo(type="policy_denied", message=v[0].reason))

        try:
            args = tool.parse(req.arguments)
            planned = tool.plan(self.ws, args, req.arguments)
        except (ValidationError, PathError, ValueError) as e:
            return finish("invalid", error=ErrorInfo(type="invalid_arguments", message=_short(e)))

        violations = self.engine.check(planned, self.records)
        if violations:
            return finish("denied", planned=planned, violations=violations,
                          error=ErrorInfo(type="policy_denied", message="; ".join(v.reason for v in violations)))
        self.permitted[req.tool] = n = self.permitted.get(req.tool, 0) + 1
        fault = next((f for f in self.faults if f["tool"] == req.tool and f["nth"] == n), None)
        if fault:  # environment fault from the task spec: nothing executes
            return finish("failed", planned=planned, observed={"injected_fault": True},
                          error=ErrorInfo(type=fault["error_type"], message=fault["message"]))
        try:
            result, observed = tool.execute(self.ws, args, planned,
                                            self.engine.policy.database.max_rows_affected_per_write)
        except RowLimitExceeded as e:
            return finish("denied", planned=planned, error=ErrorInfo(type="policy_denied", message=str(e)),
                          observed={"rows_matched": e.matched, "rows_committed": 0},
                          rule_override="database.max_rows_affected_per_write")
        except ToolInvalid as e:
            return finish("invalid", planned=planned, error=ErrorInfo(type="invalid_arguments", message=str(e)))
        except (sqlite3.Error, OSError) as e:
            return finish("failed", planned=planned, error=ErrorInfo(type=type(e).__name__, message=str(e)[:300]))
        return finish("succeeded", planned=planned, result=result, observed=observed)

    # ------------------------------------------------------------------ agent telemetry

    def handle_agent_event(self, raw: Any) -> None:
        """Store an agent claim. Never validated into trusted state; evaluated after the run."""
        event = raw.get("event") if isinstance(raw, dict) else None
        args = event.get("arguments") if isinstance(event, dict) else None
        self.agent_log.append({
            "receipt_seq": self.agent_log.count + 1,
            "received_at": now_iso(),
            "event": self.redactor.value(event),
            "claimed_arguments_digest": digest(args) if isinstance(args, dict) else None,
        })

    def reject_message(self, line: str, reason: str) -> None:
        self.record(event_type="agent_message_rejected", started_at=now_iso(),
                    detail={"reason": reason, "preview": self.redactor.preview(line, 120)})

    def close(self, how: str, exit_code: int | None) -> None:
        self.closed = True
        self.ws.close()
        self.record(event_type="agent_stopped", started_at=now_iso(), detail={"how": how, "exit_code": exit_code})


def _short(e: Exception) -> str:
    if isinstance(e, ValidationError):
        return "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:5])
    return str(e)[:300]
