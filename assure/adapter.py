"""Agent-side adapter: the interface a real agent implements to be tested.

Wire protocol (JSON lines over the agent's stdin/stdout; stderr is captured as a log):

  gateway → agent  {"type":"task", run_id, task_id, instruction, event_schema, tools:[{name,description,schema}]}
  agent → gateway  {"type":"tool_request", step_id, parent_step_id?, tool, arguments}
  gateway → agent  {"type":"tool_response", step_id, call_id, status, result? | error?}
  agent → gateway  {"type":"agent_event", "event": <assure.event/v1 AgentEvent>}   (after EVERY step)
  agent → gateway  {"type":"done"}

Rules for agents:
- Tools are reachable only through tool_request. Nothing else is supported.
- After each tool response, emit an agent_event citing the gateway's call_id.
- Finish with one task_complete event listing claimed_changes, then "done".
- Do not send reasoning. Claims are short outcome statements; they never decide the verdict.

This module has no dependency on the verifier and can be vendored into an agent's image.
"""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any, TextIO

SCHEMA = "assure.event/v1"


class GatewayClient:
    def __init__(self, inp: TextIO = sys.stdin, out: TextIO = sys.stdout):
        self.inp, self.out = inp, out
        self.task: dict[str, Any] = {}
        self.seq = 0

    def _send(self, msg: dict[str, Any]) -> None:
        self.out.write(json.dumps(msg) + "\n")
        self.out.flush()

    def _recv(self) -> dict[str, Any]:
        line = self.inp.readline()
        if not line:
            raise EOFError("gateway closed the channel")
        return json.loads(line)

    def receive_task(self) -> dict[str, Any]:
        self.task = self._recv()
        return self.task

    @property
    def run_id(self) -> str:
        return self.task["run_id"]

    def call_tool(self, step_id: str, tool: str, arguments: dict[str, Any],
                  parent_step_id: str | None = None) -> dict[str, Any]:
        self._send({"type": "tool_request", "step_id": step_id, "parent_step_id": parent_step_id,
                    "tool": tool, "arguments": arguments})
        return self._recv()

    def send_raw(self, line: str) -> None:
        self.out.write(line + "\n")
        self.out.flush()

    def emit(self, event: dict[str, Any]) -> None:
        self._send({"type": "agent_event", "event": event})

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def report_step(self, step_id: str, tool: str, arguments: dict[str, Any], response: dict[str, Any],
                    outcome: str, status: str | None = None, parent_step_id: str | None = None) -> None:
        """Emit the per-step claim for a tool call. status defaults to what the gateway returned.

        The gateway's "invalid" (well-formed request that could not run, e.g. reading a directory)
        has no claim status of its own; it is reported as "failed", which the trajectory evaluator
        treats as compatible with "invalid".
        """
        if status is None:
            status = {"invalid": "failed"}.get(response.get("status"), response.get("status", "unknown"))
        self.emit({"schema_version": SCHEMA, "source": "agent", "run_id": self.run_id, "seq": self.next_seq(),
                   "event_id": uuid.uuid4().hex, "step_id": step_id, "parent_step_id": parent_step_id,
                   "action_type": "tool_call", "tool": tool, "call_id": response.get("call_id"),
                   "arguments": arguments, "claim": {"status": status,
                                                     "outcome": outcome}})

    def complete(self, step_id: str, outcome: str, claimed_changes: list[dict[str, Any]],
                 status: str = "succeeded", parent_step_id: str | None = None) -> None:
        self.emit({"schema_version": SCHEMA, "source": "agent", "run_id": self.run_id, "seq": self.next_seq(),
                   "event_id": uuid.uuid4().hex, "step_id": step_id, "parent_step_id": parent_step_id,
                   "action_type": "task_complete", "claim": {"status": status, "outcome": outcome},
                   "claimed_changes": claimed_changes})

    def done(self) -> None:
        self._send({"type": "done"})


class AgentAdapter:
    """Subclass and implement `run`. A real LLM agent maps its tool calls onto `client.call_tool`
    and reports each step with `client.report_step`. Then: `python -m your_agent` as --agent-cmd."""

    def run(self, task: dict[str, Any], client: GatewayClient) -> None:
        raise NotImplementedError

    @classmethod
    def main(cls) -> None:
        client = GatewayClient()
        task = client.receive_task()
        cls().run(task, client)
        client.done()
