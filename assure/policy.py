"""Declarative policy model and deterministic policy engine.

Preventive rules are evaluated by the gateway *before* a tool executes and can block it.
Detective rules are evaluated over the completed trajectory and can only report.
Rules with a `mode` field run preventively when `enforce` and detectively when `detect`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import field_validator

from .models import Resource, Scalar, Strict
from .paths import ResolvedPath, matches_any

Mode = Literal["enforce", "detect"]


class ToolRules(Strict):
    allow: list[str]


class FilesystemRules(Strict):
    read: list[str] = []
    write: list[str] = []
    deny: list[str] = []


class NetworkRules(Strict):
    allow_hosts: list[str] = []

    @field_validator("allow_hosts")
    @classmethod
    def _mvp_has_no_network(cls, v: list[str]) -> list[str]:
        if v:
            raise ValueError(
                "network.allow_hosts is not enforceable in this MVP (no network tools, no egress "
                "proxy). Leave it empty; see README 'requires additional instrumentation'."
            )
        return v


class TableRule(Strict):
    read: bool = True
    write: bool = False
    writable_columns: list[str] = []


class DatabaseRules(Strict):
    tables: dict[str, TableRule] = {}
    max_rows_affected_per_write: int = 1


class ApprovalRequirement(Strict):
    id: str
    tool: str
    tables: list[str] | None = None
    description: str = ""


class RequirePrior(Strict):
    id: str
    mode: Mode = "enforce"
    description: str = ""
    before_tool: str
    require_tool: str
    match: Literal["same_table_and_where"] = "same_table_and_where"


class Matcher(Strict):
    tool: str | None = None
    path_glob: str | None = None
    table: str | None = None
    status: list[str] = ["succeeded"]


class ForbiddenSequence(Strict):
    id: str
    mode: Mode = "enforce"
    description: str = ""
    first: Matcher
    then: Matcher


class Limits(Strict):
    max_tool_calls: int = 50
    max_identical_calls: int = 3
    max_agent_steps: int = 50
    max_wall_seconds: float = 30.0


class ProtectedAssets(Strict):
    path_globs: list[str] = []


class TelemetryRules(Strict):
    require_event_per_call: bool = True
    claims_must_match_observed: bool = True
    require_final_claim: bool = True


# Refusals that may be downgraded to warnings: out-of-scope *reads* only. Secrets (deny globs),
# paths outside the root, writes, unknown tools and tampering always fail the run.
WARNABLE_RULES = {"database.table_not_allowed", "database.read_not_allowed", "filesystem.read_not_allowed"}
READ_ONLY_TOOLS = {"db_query", "fs_read", "fs_list"}


class WarnRule(Strict):
    rule: str
    tools: list[str]

    @field_validator("rule")
    @classmethod
    def _warnable(cls, v: str) -> str:
        if v not in WARNABLE_RULES:
            raise ValueError(f"'{v}' cannot be a warning; only {sorted(WARNABLE_RULES)} can")
        return v

    @field_validator("tools")
    @classmethod
    def _read_only(cls, v: list[str]) -> list[str]:
        if not v or set(v) - READ_ONLY_TOOLS:
            raise ValueError(f"warnings apply only to read-only tools {sorted(READ_ONLY_TOOLS)}")
        return v


class DenialRules(Strict):
    """How refused requests count. Default: every refusal is a policy FAIL."""

    warn: list[WarnRule] = []

    def is_warning(self, tool: str | None, rule_ids: list[str]) -> bool:
        """True only if every rule that refused this call is listed as a warning for this tool."""
        return bool(rule_ids) and all(any(w.rule == r and tool in w.tools for w in self.warn) for r in rule_ids)


class Policy(Strict):
    policy_id: str
    version: str
    tools: ToolRules
    filesystem: FilesystemRules = FilesystemRules()
    network: NetworkRules = NetworkRules()
    database: DatabaseRules = DatabaseRules()
    approvals: list[ApprovalRequirement] = []
    require_prior: list[RequirePrior] = []
    forbidden_sequences: list[ForbiddenSequence] = []
    limits: Limits = Limits()
    protected_assets: ProtectedAssets = ProtectedAssets()
    telemetry: TelemetryRules = TelemetryRules()
    denials: DenialRules = DenialRules()


class GrantScope(Strict):
    tool: str
    table: str | None = None
    columns: list[str] | None = None
    where: dict[str, Scalar] | None = None  # when set, the request's where must be identical


class ApprovalGrant(Strict):
    approval_id: str
    grants: str
    approved_by: str
    scope: GrantScope


def load_policy(path: Path) -> Policy:
    return Policy.model_validate(yaml.safe_load(path.read_text()))


# --------------------------------------------------------------------------- engine


@dataclass
class PlannedCall:
    """What a tool request would touch, derived by the gateway (never declared by the agent)."""

    tool: str
    arguments: dict[str, Any]
    arguments_digest: str
    resources: list[Resource] = field(default_factory=list)
    paths: list[tuple[ResolvedPath, str]] = field(default_factory=list)  # (path, access)
    table: str | None = None
    where: dict[str, Any] | None = None
    set_columns: list[str] | None = None
    rows: list[int] | None = None  # db_query: rowids returned; db_update: rowids the where-clause matches


@dataclass
class CallRecord:
    call_id: str
    step_id: str
    tool: str
    arguments_digest: str
    status: str
    paths: list[str] = field(default_factory=list)
    table: str | None = None
    where: dict[str, Any] | None = None
    rows: list[int] | None = None
    arguments: dict[str, Any] = field(default_factory=dict)  # unredacted, in-memory only


@dataclass
class Violation:
    rule_id: str
    reason: str
    tamper: bool = False
    refs: list[str] = field(default_factory=list)


def _matcher_hits(m: Matcher, tool: str, status: str, paths: list[str], table: str | None) -> bool:
    if m.tool and m.tool != tool:
        return False
    if status not in m.status:
        return False
    if m.table and m.table != table:
        return False
    if m.path_glob and not matches_any_path(paths, m.path_glob):
        return False
    return True


def matches_any_path(paths: list[str], pattern: str) -> bool:
    return any(matches_any(p, [pattern]) for p in paths)


class PolicyEngine:
    def __init__(self, policy: Policy, grants: list[ApprovalGrant], protected_roots: list[Path]):
        self.policy = policy
        self.grants = grants
        self.protected_roots = [Path(p).resolve() for p in protected_roots]

    # ---- preventive

    def check_tool_name(self, tool: str, known: bool) -> list[Violation]:
        out = []
        if not known:
            out.append(Violation("tools.unknown", f"tool '{tool}' is not provided by the gateway"))
        if tool not in self.policy.tools.allow:
            out.append(Violation("tools.allowlist", f"tool '{tool}' is not in the policy allowlist"))
        return out

    def check(self, call: PlannedCall, history: list[CallRecord]) -> list[Violation]:
        v: list[Violation] = []
        v += self.check_tool_name(call.tool, known=True)
        v += self._check_paths(call)
        v += self._check_database(call)
        v += self._check_approvals(call)
        v += self._check_require_prior(call, history, mode="enforce")
        v += self._check_sequences(call, history, mode="enforce")
        v += self._check_limits(call, history)
        return v

    def _is_protected(self, rp: ResolvedPath) -> bool:
        for root in self.protected_roots:
            if rp.absolute == root or root in rp.absolute.parents:
                return True
        probe = [rp.raw.lstrip("/"), rp.absolute.as_posix().lstrip("/")]
        if rp.relative:
            probe.append(rp.relative)
        return any(matches_any(p, self.policy.protected_assets.path_globs) for p in probe)

    def _check_paths(self, call: PlannedCall) -> list[Violation]:
        fs = self.policy.filesystem
        out = []
        for rp, access in call.paths:
            if self._is_protected(rp):
                out.append(Violation(
                    "tamper.protected_asset",
                    f"{access} of '{rp.raw}' targets verification assets (policy/assertions/evidence)",
                    tamper=True,
                ))
            if not rp.inside_root:
                out.append(Violation("filesystem.outside_root", f"'{rp.raw}' resolves outside the workspace"))
                continue
            if (hit := matches_any(rp.relative, fs.deny)) is not None:
                out.append(Violation("filesystem.deny", f"'{rp.relative}' matches deny pattern '{hit}'"))
                continue
            allowed = fs.write if access == "write" else fs.read + fs.write
            if matches_any(rp.relative, allowed) is None:
                out.append(Violation(f"filesystem.{access}_not_allowed",
                                     f"{access} of '{rp.relative}' is not permitted by policy"))
        return out

    def _check_database(self, call: PlannedCall) -> list[Violation]:
        if call.table is None:
            return []
        rule = self.policy.database.tables.get(call.table)
        if rule is None:
            return [Violation("database.table_not_allowed", f"table '{call.table}' is not in policy")]
        out = []
        if call.set_columns is None and not rule.read:
            out.append(Violation("database.read_not_allowed", f"reads of '{call.table}' are not permitted"))
        if call.set_columns is not None:
            if not rule.write:
                out.append(Violation("database.write_not_allowed", f"writes to '{call.table}' are not permitted"))
            bad = sorted(set(call.set_columns) - set(rule.writable_columns))
            if bad:
                out.append(Violation("database.column_not_writable",
                                     f"columns {bad} of '{call.table}' are not writable"))
        return out

    def _check_approvals(self, call: PlannedCall) -> list[Violation]:
        out = []
        for req in self.policy.approvals:
            if req.tool != call.tool or (req.tables and call.table not in req.tables):
                continue
            if not any(self._grant_covers(g, req.id, call) for g in self.grants):
                out.append(Violation(f"approvals.{req.id}",
                                     f"{call.tool} on '{call.table}' requires approval {req.id}; "
                                     "no frozen grant covers this request"))
        return out

    @staticmethod
    def _grant_covers(g: ApprovalGrant, req_id: str, call: PlannedCall) -> bool:
        s = g.scope
        if g.grants != req_id or s.tool != call.tool:
            return False
        if s.table is not None and s.table != call.table:
            return False
        if s.columns is not None and not set(call.set_columns or []) <= set(s.columns):
            return False
        if s.where is not None and s.where != (call.where or {}):
            return False
        return True

    def _check_require_prior(self, call: PlannedCall, history: list[CallRecord], mode: str) -> list[Violation]:
        out = []
        for r in self.policy.require_prior:
            if r.mode != mode or r.before_tool != call.tool:
                continue
            # Satisfied by a prior read with the same where-clause, or by one that returned every
            # row this call targets (e.g. look up by email, then update by id).
            ok = any(h.tool == r.require_tool and h.status == "succeeded" and h.table == call.table
                     and (h.where == call.where or (bool(call.rows) and h.rows is not None
                                                    and set(call.rows) <= set(h.rows)))
                     for h in history)
            if not ok:
                out.append(Violation(r.id, f"{call.tool} requires a prior successful {r.require_tool} "
                                           f"on the same table that read the target row(s) ({r.description})"))
        return out

    def _check_sequences(self, call: PlannedCall, history: list[CallRecord], mode: str,
                         call_status: str = "succeeded") -> list[Violation]:
        out = []
        paths = [rp.relative for rp, _ in call.paths if rp.relative]
        for s in self.policy.forbidden_sequences:
            if s.mode != mode:
                continue
            # For preventive checks the call has not run yet; match `then` as if it would succeed.
            if not _matcher_hits(s.then, call.tool, call_status, paths, call.table):
                continue
            firsts = [h.call_id for h in history if _matcher_hits(s.first, h.tool, h.status, h.paths, h.table)]
            if firsts:
                out.append(Violation(s.id, f"forbidden sequence: {s.description}", refs=firsts))
        return out

    def _check_limits(self, call: PlannedCall, history: list[CallRecord]) -> list[Violation]:
        lim = self.policy.limits
        out = []
        if len(history) + 1 > lim.max_tool_calls:
            out.append(Violation("limits.max_tool_calls", f"call #{len(history) + 1} exceeds {lim.max_tool_calls}"))
        same = sum(1 for h in history if h.tool == call.tool and h.arguments_digest == call.arguments_digest)
        if same + 1 > lim.max_identical_calls:
            out.append(Violation("limits.max_identical_calls",
                                 f"identical {call.tool} call repeated {same + 1} times (max {lim.max_identical_calls})"))
        return out

    # ---- detective

    def detect(self, records: list[CallRecord], agent_step_count: int) -> list[Violation]:
        """Detect-mode rules over the complete trusted trajectory."""
        out: list[Violation] = []
        for i, rec in enumerate(records):
            prior = records[:i]
            planned = PlannedCall(tool=rec.tool, arguments={}, arguments_digest=rec.arguments_digest,
                                  table=rec.table, where=rec.where, rows=rec.rows)
            if rec.status != "succeeded":
                continue
            for v in self._check_require_prior(planned, prior, mode="detect"):
                v.refs.append(rec.call_id)
                out.append(v)
            for s in self.policy.forbidden_sequences:
                if s.mode != "detect" or not _matcher_hits(s.then, rec.tool, rec.status, rec.paths, rec.table):
                    continue
                firsts = [h.call_id for h in prior if _matcher_hits(s.first, h.tool, h.status, h.paths, h.table)]
                if firsts:
                    out.append(Violation(s.id, f"forbidden sequence: {s.description}", refs=firsts + [rec.call_id]))
        if agent_step_count > self.policy.limits.max_agent_steps:
            out.append(Violation("limits.max_agent_steps",
                                 f"agent reported {agent_step_count} steps (max {self.policy.limits.max_agent_steps})"))
        return out
