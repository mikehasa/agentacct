"""Run real agents over the scenarios and report what they recorded.

    python -m benchmarks.agent_recording.run                       # claude, every scenario
    python -m benchmarks.agent_recording.run --agents claude,codex --trials 2
    python -m benchmarks.agent_recording.run --src ../other-checkout/src --label before

``--src`` chooses which checkout's contract (MCP server and instruction block)
the agents see, so the same scenarios can be run against two versions of it.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import grading, records, sandbox
from .agents import ADAPTERS, AgentRun
from .scenarios import BY_KEY, SCENARIOS, Scenario

REPO_SRC = Path(__file__).resolve().parents[2] / "src"


@dataclass
class Result:
    agent: str
    scenario: str
    trial: int
    run: AgentRun
    record_text: str
    objective: grading.Objective
    verdict: grading.Verdict
    observed: grading.Observed
    workdir: str = ""

    def to_json(self) -> dict:
        return {
            "agent": self.agent, "scenario": self.scenario, "trial": self.trial,
            "run": grading.as_dict(self.run), "record": self.record_text,
            "objective": {"checks": self.objective.checks, "score": round(self.objective.score, 3),
                          "stayed_in_bounds": self.objective.stayed_in_bounds, "notes": self.objective.notes},
            "verdict": grading.as_dict(self.verdict),
            "observed": {"verify_exit_code": self.observed.verify_exit_code,
                         "changed_files": self.observed.changed_files},
            "workdir": self.workdir,
        }


def run_one(
    agent: str, scenario: Scenario, trial: int, *, src: Path, workroot: Path,
    model: str | None, timeout: int, ask: Callable[[str], str],
) -> Result:
    root = workroot / f"{agent}-{scenario.key}-{trial}"
    box = sandbox.build(scenario, root, src=src)
    baseline, _ = sandbox.run_verify(box, scenario)
    if baseline != scenario.baseline_exit_code:
        raise RuntimeError(f"{scenario.key}: baseline exits {baseline}, scenario declares {scenario.baseline_exit_code}")

    run = ADAPTERS[agent].run(box, scenario.task, model=model, timeout=timeout)
    # Capture what the agent did BEFORE undoing any off-limits edit, then judge
    # reality with the task's constraints honoured.
    changed, diff = sandbox.changed_files(box), sandbox.diff_text(box)
    sandbox.restore_off_limits(box, scenario)
    exit_code, tail = sandbox.run_verify(box, scenario)
    observed = grading.Observed(exit_code, tail, changed, diff)
    record = records.read_store(box.store)
    return Result(
        agent, scenario.key, trial, run, record.render(),
        grading.grade_objectively(scenario, record, observed),
        grading.judge(scenario, record, observed, ask), observed, str(root),
    )


@dataclass
class Summary:
    runs: int = 0
    objective: float = 0.0
    judge_total: float = 0.0
    skim_misleads: int = 0
    contradicts_reality: int = 0
    recorded_nothing: int = 0
    refused_calls: int = 0
    out_of_bounds: int = 0
    seconds_to_answer: float = 0.0
    failed_checks: dict[str, int] = field(default_factory=dict)


def summarize(results: list[Result]) -> Summary:
    summary = Summary(runs=len(results))
    if not results:
        return summary
    judged = [result for result in results if not result.verdict.error]
    summary.objective = sum(result.objective.score for result in results) / len(results)
    summary.judge_total = sum(result.verdict.total for result in judged) / max(len(judged), 1)
    summary.seconds_to_answer = sum(result.verdict.seconds_to_answer for result in judged) / max(len(judged), 1)
    for result in results:
        summary.skim_misleads += result.verdict.skim_misleads
        summary.contradicts_reality += result.verdict.contradicts_reality
        summary.recorded_nothing += result.record_text.startswith("(the agent recorded nothing")
        summary.refused_calls += result.run.refused_calls or 0
        summary.out_of_bounds += not result.objective.stayed_in_bounds
        for name, passed in result.objective.checks.items():
            if passed is False:
                summary.failed_checks[name] = summary.failed_checks.get(name, 0) + 1
    return summary


def report_markdown(results: list[Result], *, label: str, src: Path) -> str:
    lines = [f"# Agent recording eval — {label}", "", f"Contract under test: `{src}`", ""]
    lines += ["| agent | scenario | objective | judge /8 | skim misleads | contradicts reality | refused calls | read time |",
              "|---|---|---|---|---|---|---|---|"]
    for result in sorted(results, key=lambda row: (row.agent, row.scenario, row.trial)):
        verdict = result.verdict
        judged = "judge error" if verdict.error else str(verdict.total)
        lines.append(
            f"| {result.agent} | {result.scenario} | {result.objective.passed}/{result.objective.applicable} | {judged} | "
            f"{'YES' if verdict.skim_misleads else 'no'} | {'YES' if verdict.contradicts_reality else 'no'} | "
            f"{result.run.refused_calls if result.run.refused_calls is not None else '—'} | {verdict.seconds_to_answer}s |"
        )
    lines.append("")
    for agent in sorted({result.agent for result in results}):
        summary = summarize([result for result in results if result.agent == agent])
        lines += [
            f"## {agent}", "", f"_Isolation: {ADAPTERS[agent].isolation}._", "",
            f"- runs: {summary.runs} · recorded nothing: {summary.recorded_nothing}",
            f"- objective: {summary.objective:.0%} of applicable checks · judge: {summary.judge_total:.2f}/8 · "
            f"read time {summary.seconds_to_answer:.0f}s",
            f"- skim misleads: {summary.skim_misleads} · contradicts reality: {summary.contradicts_reality} · "
            f"refused recording calls: {summary.refused_calls} · edited off-limits files: {summary.out_of_bounds}",
        ]
        if summary.failed_checks:
            lines.append("- objective checks that failed: " + ", ".join(
                f"{name} ×{count}" for name, count in sorted(summary.failed_checks.items(), key=lambda item: -item[1])))
        lines.append("")
    lines += ["## Records", ""]
    for result in sorted(results, key=lambda row: (row.agent, row.scenario, row.trial)):
        lines += [f"### {result.agent} · {result.scenario} · trial {result.trial}", ""]
        if result.run.error:
            lines += [f"> agent error: {result.run.error[:300]}", ""]
        lines += ["```", result.record_text, "```", ""]
        if result.objective.notes:
            lines += ["objective notes: " + "; ".join(result.objective.notes), ""]
        if result.verdict.note or result.verdict.error:
            lines += [f"judge: {result.verdict.error or result.verdict.note}", ""]
        for refusal in result.run.refusals:
            lines += [f"refused call: {refusal}", ""]
    return "\n".join(lines)


def run_matrix(
    agents: list[str], scenarios: list[Scenario], trials: int, *, src: Path, workroot: Path,
    model: str | None, timeout: int, ask: Callable[[str], str], workers: int,
) -> list[Result]:
    jobs = [(agent, scenario, trial) for agent in agents for scenario in scenarios for trial in range(1, trials + 1)]
    results: list[Result] = []

    def work(job: tuple[str, Scenario, int]) -> Result | None:
        agent, scenario, trial = job
        started = time.monotonic()
        try:
            result = run_one(agent, scenario, trial, src=src, workroot=workroot, model=model, timeout=timeout, ask=ask)
        except Exception as exc:
            print(f"  ! {agent} / {scenario.key} / {trial}: {exc}", file=sys.stderr, flush=True)
            return None
        print(
            f"  · {agent:9s} {scenario.key:12s} #{trial}  objective {result.objective.passed}/{result.objective.applicable}"
            f"  judge {result.verdict.total}/8  {time.monotonic() - started:.0f}s"
            + ("  SKIM-MISLEADS" if result.verdict.skim_misleads else "")
            + (f"  [{result.run.error[:60]}]" if result.run.error else ""),
            flush=True,
        )
        return result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(work, jobs):
            if result is not None:
                results.append(result)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agents", default="claude", help=f"comma-separated, from: {', '.join(ADAPTERS)}")
    parser.add_argument("--scenarios", default="all", help=f"comma-separated, from: {', '.join(BY_KEY)}")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--src", type=Path, default=REPO_SRC, help="the checkout src/ whose contract agents see")
    parser.add_argument("--label", default="this checkout")
    parser.add_argument("--model", default=None, help="subject model, passed through to the agent CLI")
    parser.add_argument("--judge-model", default="opus")
    parser.add_argument("--timeout", type=int, default=600, help="seconds per agent run")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", type=Path, default=None, help="directory for report.json / report.md")
    args = parser.parse_args(argv)

    agents = [name.strip() for name in args.agents.split(",") if name.strip()]
    for name in agents:
        if name not in ADAPTERS:
            parser.error(f"unknown agent {name!r}")
        if not ADAPTERS[name].available():
            parser.error(f"{name}: `{ADAPTERS[name].binary}` is not on PATH")
    scenarios = list(SCENARIOS) if args.scenarios == "all" else [BY_KEY[key.strip()] for key in args.scenarios.split(",")]

    out = args.out or Path(tempfile.mkdtemp(prefix="agent-recording-eval-"))
    out.mkdir(parents=True, exist_ok=True)
    workroot = out / "work"
    workroot.mkdir(exist_ok=True)
    src = args.src.resolve()
    print(f"contract: {src}\nagents: {agents} · scenarios: {[s.key for s in scenarios]} · trials: {args.trials}\n", flush=True)

    results = run_matrix(
        agents, scenarios, args.trials, src=src, workroot=workroot, model=args.model,
        timeout=args.timeout, ask=grading.claude_judge(args.judge_model), workers=args.workers,
    )
    (out / "report.json").write_text(json.dumps(
        {"label": args.label, "src": str(src), "results": [result.to_json() for result in results]}, indent=1))
    (out / "report.md").write_text(report_markdown(results, label=args.label, src=src))

    overall = summarize(results)
    print(f"\n{overall.runs} runs · objective {overall.objective:.0%} · judge {overall.judge_total:.2f}/8 · "
          f"skim misleads {overall.skim_misleads} · contradicts reality {overall.contradicts_reality} · "
          f"recorded nothing {overall.recorded_nothing} · refused calls {overall.refused_calls}")
    print(f"report: {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
