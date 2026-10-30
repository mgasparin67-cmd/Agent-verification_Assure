"""Typed models: the versioned event schema, protocol messages, findings and assertion results.

Gateway events are trusted observations. Agent events are untrusted claims. They are separate
types and are never merged into one stream.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EVENT_SCHEMA_VERSION = "assure.event/v1"

Scalar = str | int | float | bool | None
Dimension = Literal[
    "task_correctness", "policy_compliance", "evidence_completeness", "verification_infrastructure"
]
Verdict = Literal["PASS", "FAIL", "INCONCLUSIVE"]
CallStatus = Literal["succeeded", "failed", "denied", "invalid"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    """Digest of the canonical JSON form. Used to compare claimed and observed arguments."""
    return "sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest()


# --------------------------------------------------------------------------- events


class Resource(Strict):
    kind: Literal["file", "db_table", "network", "tool"]
    target: str
    access: Literal["read", "write", "execute"]
    columns: list[str] | None = None


class ErrorInfo(Strict):
    type: str
    message: str


class ResultRef(Strict):
    artifact: str
    sha256: str
    bytes: int
    summary: str


class PolicyDecision(Strict):
    decision: Literal["allow", "deny"]
    rule_ids: list[str] = []
    reasons: list[str] = []
    tamper: bool = False


class GatewayEvent(Strict):
    """Trusted record written only by the execution gateway."""

    schema_version: Literal["assure.event/v1"] = EVENT_SCHEMA_VERSION
    source: Literal["gateway"] = "gateway"
    run_id: str
    seq: int
    event_type: Literal["run_started", "tool_call", "agent_message_rejected", "agent_stopped"]
    call_id: str | None = None
    step_id: str | None = None
    parent_step_id: str | None = None
    action_type: str | None = None
    tool: str | None = None
    arguments: dict[str, Any] | None = None  # redacted copy
    arguments_digest: str | None = None  # digest of the unredacted arguments
    resources: list[Resource] = []
    policy: PolicyDecision | None = None
    started_at: str | None = None
    ended_at: str | None = None
    status: CallStatus | None = None
    error: ErrorInfo | None = None
    result: ResultRef | None = None
    observed: dict[str, Any] = {}
    detail: dict[str, Any] = {}
    prev_hash: str = ""
    hash: str = ""


class Claim(Strict):
    status: Literal["succeeded", "failed", "denied", "skipped", "unknown"]
    outcome: str = Field(max_length=500)


class ClaimedChange(Strict):
    table: str
    row_id: int | str
    column: str
    new_value: Scalar


class AgentEvent(Strict):
    """Untrusted structured claim the agent must emit after every step.

    There is intentionally no field for reasoning or chain-of-thought; extra fields are rejected.
    """

    schema_version: Literal["assure.event/v1"]
    source: Literal["agent"]
    run_id: str
    seq: int = Field(ge=1)
    event_id: str = Field(min_length=1, max_length=64)
    step_id: str = Field(min_length=1, max_length=64)
    parent_step_id: str | None = None
    action_type: Literal["tool_call", "task_complete", "note"]
    tool: str | None = None
    call_id: str | None = None
    arguments: dict[str, Any] | None = None
    claim: Claim
    claimed_changes: list[ClaimedChange] | None = None

    @model_validator(mode="after")
    def _shape(self) -> "AgentEvent":
        if self.action_type == "tool_call":
            missing = [f for f in ("tool", "call_id", "arguments") if getattr(self, f) is None]
            if missing:
                raise ValueError(f"tool_call event missing {missing}")
        if self.action_type == "task_complete" and self.claimed_changes is None:
            raise ValueError("task_complete event requires claimed_changes (may be empty)")
        return self


# --------------------------------------------------------------------------- protocol


class ToolRequest(Strict):
    type: Literal["tool_request"]
    step_id: str = Field(min_length=1, max_length=64)
    parent_step_id: str | None = None
    tool: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any]


# --------------------------------------------------------------------------- results


class Finding(Strict):
    id: str
    dimension: Dimension
    outcome: Literal["FAIL", "INCONCLUSIVE", "WARN"]  # WARN is recorded but never affects a verdict
    control: Literal["preventive", "detective", "verification", "integrity"]
    expected: str
    observed: str
    refs: list[str] = []
    evidence: dict[str, Any] = {}
    explanation: str


class AssertionResult(Strict):
    id: str
    kind: str
    expected: str
    status: Literal["PASS", "FAIL", "ERROR", "TIMEOUT", "PRECONDITION", "NOT_RUN"]
    observed: str = ""
    evidence: dict[str, Any] = {}
    duration_ms: int = 0
    suite: str = ""
