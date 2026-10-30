"""CLI entry point.

Exit codes: 0 all required checks passed · 1 definite policy or correctness failure ·
2 inconclusive (no established failure, but something could not be verified, or the run could not
be carried out) · 64 usage error.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

import yaml

from .gateway import EvidenceLog
from .orchestrator import agent_argv, execute_run, load_scenario
from .report import render_terminal

EX_USAGE = 64


def default_project() -> Path:
    here = Path(__file__).resolve().parent.parent
    return here if (here / "scenarios").is_dir() else Path.cwd()


class Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse uses exit 2, which would collide with INCONCLUSIVE
        self.print_usage(sys.stderr)
        print(f"assure: error: {message}", file=sys.stderr)
        sys.exit(EX_USAGE)


def main(argv: list[str] | None = None) -> int:
    ap = Parser(prog="assure", description="Independent Assurance Plane for AI agents")
    ap.add_argument("--project", type=Path, default=None, help="directory with scenarios/, specs/, fixtures/")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run one agent task under the gateway and verify it")
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("--scenario", help="mock-agent scenario name (see `assure scenarios`)")
    src.add_argument("--task", type=Path, help="task.yaml for a real agent (use with --agent-cmd)")
    r.add_argument("--agent-cmd", help="command that starts the agent (speaks the stdio protocol)")
    r.add_argument("--isolation", choices=["local", "docker"], default="local")
    r.add_argument("--docker-image", default="assure-agent:0.1", help="agent image for --isolation docker")
    r.add_argument("--pass-env", action="append", default=[], metavar="NAME",
                   help="pass a host env var to the agent (repeatable), e.g. ANTHROPIC_API_KEY; "
                        "its value is redacted from all evidence")
    r.add_argument("--runs-dir", type=Path, default=None)
    r.add_argument("--keep-sandbox", action="store_true", help="keep the workspace for inspection")
    r.add_argument("--json", action="store_true", help="print the full JSON report instead of a summary")

    b = sub.add_parser("bench", help="run a task suite N times per agent and compare agents")
    b.add_argument("--suite", required=True, help="suite name (suites/<name>.yaml)")
    b.add_argument("--agent", action="append", default=[], metavar="SPEC",
                   help="claude:<model>, mock:<behavior> or mcp:<profile> from agents/ "
                        "(repeatable; the first is the baseline)")
    b.add_argument("--models", help="shorthand: comma-separated Claude models, e.g. claude-opus-5-5,claude-sonnet-5-5")
    b.add_argument("--repeat", type=int, default=5, help="runs per agent per task (default 5)")
    b.add_argument("--jobs", type=int, default=1, help="runs in parallel (separate processes)")
    b.add_argument("--min-pass-rate", type=float, default=None, help="exit 1 if any agent is below this (0-1)")
    b.add_argument("--alpha", type=float, default=0.05, help="significance level for regressions")
    b.add_argument("--pass-env", action="append", default=[], metavar="NAME")
    b.add_argument("--runs-dir", type=Path, default=None)
    b.add_argument("--json", action="store_true", help="print bench.json instead of a summary")

    bs = sub.add_parser("bench-show", help="re-analyse a saved bench.json from its runs' reports")
    bs.add_argument("path", type=Path)
    bs.add_argument("--min-pass-rate", type=float, default=None)
    bs.add_argument("--json", action="store_true")

    cs = sub.add_parser("check-suite", help="run each task's reference agents; confirm the suite discriminates")
    cs.add_argument("--suite", required=True)
    cs.add_argument("--jobs", type=int, default=4)
    cs.add_argument("--runs-dir", type=Path, default=None)

    sub.add_parser("scenarios", help="list scenarios")
    v = sub.add_parser("verify-log", help="check an evidence log's hash chain")
    v.add_argument("path", type=Path)

    a = ap.parse_args(argv)
    project = (a.project or default_project()).resolve()

    if a.cmd == "scenarios":
        for p in sorted((project / "scenarios").glob("*.yaml")):
            s = yaml.safe_load(p.read_text())
            print(f"{s['scenario']:<20} {s['description']}")
        return 0
    if a.cmd == "verify-log":
        problems = EvidenceLog.verify(a.path)
        print("\n".join(problems) if problems else "hash chain OK (integrity only; not proof of truthfulness)")
        return 2 if problems else 0

    if a.cmd == "bench":
        return _bench(a, ap, project)
    if a.cmd == "bench-show":
        from .bench import bench_exit_code, reanalyse, render_bench
        if not a.path.exists():
            ap.error(f"no such file: {a.path}")
        bench = reanalyse(a.path)
        print(json.dumps(bench, indent=2, default=str) if a.json else render_bench(bench))
        return bench_exit_code(bench, a.min_pass_rate)
    if a.cmd == "check-suite":
        from .bench import check_suite, load_suite, render_check
        try:
            suite = load_suite(project, a.suite)
        except FileNotFoundError as e:
            ap.error(str(e))
        result = check_suite(project=project, suite=suite, jobs=a.jobs,
                             runs_dir=(a.runs_dir or project / "runs").resolve() / "check-suite")
        print(render_check(result))
        return 0 if result["valid"] else 1

    try:
        if a.scenario:
            sc = load_scenario(project, a.scenario)
            task_path, argv_ = project / sc.task, agent_argv(sc.agent, a.isolation)
            if a.agent_cmd:
                ap.error("--agent-cmd cannot be combined with --scenario")
            faults, timeout = sc.fault_injection, sc.assertion_timeout_seconds
            pass_env = sorted(set(sc.pass_env) | set(a.pass_env))
        else:
            if not a.agent_cmd:
                ap.error("--task requires --agent-cmd")
            task_path, argv_, faults, timeout = a.task.resolve(), shlex.split(a.agent_cmd), [], None
            pass_env = a.pass_env
        report = execute_run(project=project, task_path=task_path, argv=argv_,
                             runs_dir=(a.runs_dir or project / "runs").resolve(), isolation=a.isolation,
                             scenario=a.scenario, faults=list(faults), assertion_timeout=timeout,
                             keep_sandbox=a.keep_sandbox, docker_image=a.docker_image, pass_env=pass_env)
    except FileNotFoundError as e:
        print(f"assure: {e}", file=sys.stderr)
        return EX_USAGE
    except (RuntimeError, ValueError) as e:
        print(f"assure: run could not be carried out: {e}\nOVERALL: INCONCLUSIVE (exit 2)", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, default=str) if a.json else render_terminal(report))
    return report["exit_code"]


def _bench(a, ap, project: Path) -> int:
    from .bench import bench_exit_code, load_suite, parse_agent, print_progress, render_bench, run_bench

    specs = list(a.agent) + [f"claude:{m.strip()}" for m in (a.models or "").split(",") if m.strip()]
    if not specs:
        ap.error("bench needs at least one --agent or --models")
    if a.repeat < 1 or a.jobs < 1:
        ap.error("--repeat and --jobs must be at least 1")
    try:
        agents = [parse_agent(s, project) for s in specs]
        suite = load_suite(project, a.suite)
    except (ValueError, FileNotFoundError) as e:
        ap.error(str(e))
    total = len(agents) * len(suite.tasks) * a.repeat
    print(f"assure bench: {len(agents)} agent(s) x {len(suite.tasks)} task(s) x {a.repeat} = {total} runs",
          file=sys.stderr)
    bench = run_bench(project=project, suite=suite, agents=agents, repeat=a.repeat,
                      runs_dir=(a.runs_dir or project / "runs").resolve(), jobs=a.jobs,
                      extra_pass_env=a.pass_env, alpha=a.alpha, progress=print_progress)
    print(json.dumps(bench, indent=2, default=str) if a.json else render_bench(bench))
    return bench_exit_code(bench, a.min_pass_rate)


if __name__ == "__main__":
    sys.exit(main())
