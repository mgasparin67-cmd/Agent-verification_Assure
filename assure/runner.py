"""Agent process runner and the gateway message loop.

Two isolation modes:
- local  : agent is a child process (own session, scrubbed env, empty cwd, CPU rlimit). The
           gateway being the only route to tools is a CONVENTION here: the child runs as the same
           OS user and could open workspace files directly. Reports mark boundary_enforced=false.
- docker : agent runs in a container with no volumes, no network, read-only rootfs, all caps
           dropped, non-root user, pid/memory/cpu limits, and no Docker socket. Its only channel
           is stdio to the host gateway, so the gateway boundary is enforced by construction.
           (Implemented, but not exercised in the environment this MVP was built in.)
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .gateway import Gateway

MAX_LINE = 64 * 1024
MAX_MESSAGES = 2000


@dataclass
class Isolation:
    mode: str
    boundary_enforced: bool
    network_isolated: bool
    notes: list[str]


class AgentProcess:
    def __init__(self, argv: list[str], *, mode: str, cwd: Path, docker_image: str | None = None,
                 cpu_seconds: int = 60, extra_env: dict[str, str] | None = None):
        # extra_env: explicitly named host variables passed through to the agent (e.g. its own LLM
        # API key). Never the verifier's credentials; values are registered for redaction by caller.
        extra_env = extra_env or {}
        self.mode = mode
        self.container = f"assure-agent-{uuid.uuid4().hex[:12]}" if mode == "docker" else None
        if mode == "docker":
            if shutil.which("docker") is None:
                raise RuntimeError("docker isolation requested but the docker CLI is not available")
            argv = ["docker", "run", "--rm", "-i", "--name", self.container,
                    "--network", "none", "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges", "--pids-limit", "64",
                    "--memory", "256m", "--cpus", "1", "--user", "65534:65534",
                    "--tmpfs", "/tmp:rw,size=16m",
                    # `-e NAME` (no value) makes docker read the value from its own environment, so
                    # secrets never appear on a command line.
                    *[a for name in sorted(extra_env) for a in ("-e", name)],
                    docker_image or "assure-agent:0.1", *argv]
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **extra_env}
            preexec = None
        else:
            env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8",
                   "HOME": str(cwd), **extra_env}

            def preexec():
                import resource
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     cwd=cwd, env=env, start_new_session=True, preexec_fn=preexec,
                                     text=True, bufsize=1)
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.stderr: list[str] = []
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _pump_stderr(self) -> None:
        size = 0
        for line in self.proc.stderr:
            if size < MAX_LINE:
                self.stderr.append(line)
                size += len(line)

    def send(self, msg: dict[str, Any]) -> bool:
        try:
            self.proc.stdin.write(json.dumps(msg, default=str) + "\n")
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def recv(self, timeout: float) -> tuple[str, str | None]:
        try:
            line = self.lines.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return "timeout", None
        return ("eof", None) if line is None else ("line", line)

    def stop(self) -> int | None:
        """Quiesce: kill the agent and everything in its process group / container."""
        if self.container:
            subprocess.run(["docker", "kill", self.container], capture_output=True, timeout=15)
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            code = self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            code = None
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except Exception:
                pass
        return code


def isolation_for(mode: str) -> Isolation:
    if mode == "docker":
        return Isolation("docker", True, True, [
            "Agent container has no volumes, --network none, read-only rootfs, caps dropped, non-root.",
            "Standard containers share the host kernel; not a guarantee against hostile workloads.",
        ])
    return Isolation("local", False, False, [
        "SIMULATED boundary: the agent is a same-user child process and could bypass the gateway.",
        "Network is not isolated in local mode.",
        "Use --isolation docker for an enforced tool boundary.",
    ])


def collect_pass_env(names: list[str]) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise RuntimeError(f"--pass-env variables not set in the host environment: {missing}")
    return {n: os.environ[n] for n in names}


def serve(gateway: Gateway, agent: AgentProcess, task_msg: dict[str, Any], deadline: float) -> str:
    """Relay messages between the agent and the gateway until done, EOF, or deadline."""
    if not agent.send(task_msg):
        return "agent_unreachable"
    for _ in range(MAX_MESSAGES):
        kind, line = agent.recv(deadline - time.monotonic())
        if kind == "timeout":
            gateway.deadline_exceeded = True
            return "deadline"
        if kind == "eof":
            return "eof"
        if len(line) > MAX_LINE:
            gateway.reject_message(line, "message exceeds size limit")
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            gateway.reject_message(line, "not JSON")
            continue
        mtype = msg.get("type") if isinstance(msg, dict) else None
        if mtype == "tool_request":
            agent.send(gateway.handle_tool_request(msg))
        elif mtype == "agent_event":
            gateway.handle_agent_event(msg)
        elif mtype == "done":
            return "done"
        else:
            gateway.reject_message(line, f"unknown message type {mtype!r}")
    return "message_limit"
