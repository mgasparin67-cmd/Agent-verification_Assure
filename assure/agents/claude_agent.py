"""Claude agent bridge: drives a Claude model through the assure gateway over the stdio protocol.

What the model sees: the task instruction, the tools the gateway offers, and one local tool,
`report_completion`, for its final account of what it changed. It sees nothing of the policy,
the assertions, or the evidence store.

What the bridge does:
- forwards every tool_use to the gateway unchanged and returns the gateway's response to the
  model unchanged (refusals included);
- emits the per-step agent event mechanically from each gateway response (so step telemetry is
  complete and honest by construction);
- turns the model's `report_completion` call into the `task_complete` event. That final claim is
  the model's own statement, and the verifier checks it against the observed database diff.

The API key reaches this process only via `assure run --pass-env ANTHROPIC_API_KEY`. The verifier
never uses it, and its value is redacted from every evidence file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import anthropic

from assure.adapter import GatewayClient
from assure.agents.common import COMPLETION_TOOL, SYSTEM, completion_problems

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

def log(msg: str) -> None:
    print(f"[claude-bridge] {msg}", file=sys.stderr, flush=True)


def gateway_tools(task: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]}
            for t in task["tools"]] + [COMPLETION_TOOL]


def run(task: dict[str, Any], gw: GatewayClient, llm: anthropic.Anthropic, *, model: str, effort: str,
        max_turns: int) -> None:
    tools = gateway_tools(task)
    messages: list[dict[str, Any]] = [{"role": "user", "content": f"Task: {task['instruction']}"}]
    step, prev = 0, None
    completion: dict[str, Any] | None = None
    failure: str | None = None

    for turn in range(1, max_turns + 1):
        try:
            resp = llm.beta.messages.create(
                model=model, max_tokens=16000, system=SYSTEM, tools=tools, messages=messages,
                output_config={"effort": effort}, betas=[FALLBACK_BETA], fallbacks="default",
                cache_control={"type": "ephemeral"},  # automatic caching of the growing conversation
            )
        except anthropic.AuthenticationError:
            failure = "Claude API rejected the credentials"
        except anthropic.RateLimitError:
            failure = "Claude API rate limit exceeded"
        except anthropic.APIStatusError as e:
            failure = f"Claude API error {e.status_code}: {e.message}"
        except anthropic.APIConnectionError:
            failure = "could not connect to the Claude API"
        if failure:
            log(failure)
            break
        log(f"turn {turn}: model={resp.model} stop={resp.stop_reason} "
            f"tokens in={resp.usage.input_tokens} out={resp.usage.output_tokens} "
            f"cache_read={getattr(resp.usage, 'cache_read_input_tokens', None) or 0} "
            f"cache_write={getattr(resp.usage, 'cache_creation_input_tokens', None) or 0}")
        if resp.stop_reason == "refusal":
            failure = f"model refused ({getattr(resp.stop_details, 'category', None)})"
            break
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "pause_turn":
            continue
        uses = [b for b in resp.content if b.type == "tool_use"]
        if not uses:
            break

        results = []
        for use in uses:
            args = dict(use.input)
            if use.name == "report_completion":
                problems = completion_problems(args)
                if problems:
                    log(f"report_completion rejected: {'; '.join(problems)}")
                    results.append({"type": "tool_result", "tool_use_id": use.id, "is_error": True,
                                    "content": "Not recorded: " + "; ".join(problems) + ". Call report_completion again."})
                else:
                    completion = args
                    results.append({"type": "tool_result", "tool_use_id": use.id, "content": "recorded"})
                continue
            step += 1
            sid = f"s{step}"
            r = gw.call_tool(sid, use.name, args, parent_step_id=prev)
            gw.report_step(sid, use.name, args, r, f"{use.name} returned {r['status']}", parent_step_id=prev)
            prev = sid
            ok = r["status"] == "succeeded"
            results.append({"type": "tool_result", "tool_use_id": use.id, "is_error": not ok,
                            "content": json.dumps(r.get("result") if ok else {"status": r["status"], **r.get("error", {})},
                                              default=str)})
        messages.append({"role": "user", "content": results})
        if completion is not None:
            break
    else:
        failure = failure or f"stopped after {max_turns} model turns"

    if completion is not None:
        gw.complete(f"s{step + 1}", completion["outcome"][:500], completion["changes"],
                    status=completion["status"], parent_step_id=prev)
    else:
        gw.complete(f"s{step + 1}", (failure or "model ended without calling report_completion")[:500], [],
                    status="failed", parent_step_id=prev)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.environ.get("ASSURE_CLAUDE_MODEL", DEFAULT_MODEL))
    ap.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--max-turns", type=int, default=100)  # long queues need many turns; policy limits still apply
    a = ap.parse_args()
    gw = GatewayClient()
    task = gw.receive_task()
    run(task, gw, anthropic.Anthropic(), model=a.model, effort=a.effort, max_turns=a.max_turns)
    gw.done()


if __name__ == "__main__":
    main()
