"""Render one ``agentacct.receipt.v1`` as GitHub-flavored Markdown.

The CLI prints a Work Receipt as Rich terminal text; this renders the SAME
receipt as Markdown a person can paste into a PR, an issue, or the docs. It
reuses the receipt module's shared headline helpers (evidence coverage, cost
grammar, plan share) so no two surfaces can word the same fact differently, and
— unlike the terminal render — it includes the event timeline, since a
pasted-into-a-doc receipt is where the trajectory (a failed check, a re-run, a
later edit) earns its place.

Nothing here invents data: it only reshapes the fields ``build_receipt``
already produced, and it renders relative timestamps (``+2m10s`` from the
Task's first event) so the same receipt renders byte-for-byte identically
regardless of when it is generated — which is what lets the docs generators
commit a stable, regenerable example.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .receipt import (
    PROVENANCE_LEGEND,
    evidence_coverage_headline,
    evidence_coverage_ledger,
    plan_share_headline,
    receipt_category_text,
    receipt_cost_text,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _cell(value: Any) -> str:
    """Escape a value for a Markdown table cell: pipes break columns, newlines
    break rows."""

    return _text(value).replace("|", "\\|").replace("\n", "<br>")


def _code(value: Any) -> str:
    """Wrap a value in backticks for a table/inline cell, neutralizing any pipe
    and collapsing internal backticks so the span never breaks."""

    return "`" + _text(value).replace("`", "'").replace("|", "\\|") + "`"


def _relative(seconds: float) -> str:
    """A compact relative offset (``+0s`` / ``+45s`` / ``+2m10s`` / ``+1h04m``)."""

    total = int(round(seconds))
    if total < 0:
        total = 0
    if total < 60:
        return f"+{total}s"
    if total < 3600:
        return f"+{total // 60}m{total % 60:02d}s"
    return f"+{total // 3600}h{(total % 3600) // 60:02d}m"


def _dimension_rows(receipt: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    dims = receipt.get("dimensions", {}) if isinstance(receipt.get("dimensions"), Mapping) else {}

    def _prov(name: str) -> str:
        dim = dims.get(name, {})
        sources = dim.get("provenance") if isinstance(dim, Mapping) else None
        return ", ".join(str(source) for source in (sources or []))

    task = dims.get("task", {}) if isinstance(dims.get("task"), Mapping) else {}
    objectives = task.get("objectives") or []
    boundary = task.get("boundary", {}) if isinstance(task.get("boundary"), Mapping) else {}
    task_summary = "; ".join(str(o) for o in objectives[:2]) or "no objective recorded"
    if boundary.get("project"):
        task_summary += f" · project {boundary['project']}"

    actors = dims.get("actors", {}) if isinstance(dims.get("actors"), Mapping) else {}
    actor_summary = " · ".join(
        part
        for part in (
            _text(actors.get("primary_agent")) or None,
            ", ".join(str(m) for m in (actors.get("models") or [])) or None,
            (f"{actors.get('subagent_session_count')} subagents" if actors.get("subagent_session_count") else None),
        )
        if part
    ) or "—"

    actions = dims.get("actions", {}) if isinstance(dims.get("actions"), Mapping) else {}
    actions_summary = receipt_category_text(actions.get("tool_category_counts") or {})
    actions_summary += f" · touched {int(actions.get('touched_file_count') or 0)} file(s)"
    if int(actions.get("command_count") or 0):
        actions_summary += f" · ran {int(actions.get('command_count') or 0)} command(s)"

    cost = dims.get("cost", {}) if isinstance(dims.get("cost"), Mapping) else {}
    evidence = dims.get("evidence", {}) if isinstance(dims.get("evidence"), Mapping) else {}
    outcome = dims.get("outcome", {}) if isinstance(dims.get("outcome"), Mapping) else {}

    return [
        ("Task", task_summary, _prov("task")),
        ("Actors", actor_summary, _prov("actors")),
        ("Actions", actions_summary, _prov("actions")),
        ("Cost", receipt_cost_text(cost), _prov("cost")),
        ("Weekly plan", plan_share_headline(cost.get("plan_share")), ""),
        (
            "Evidence",
            f"{int(evidence.get('checks_total') or 0)} checks · "
            f"{int(evidence.get('checks_passed') or 0)} passed · "
            f"{int(evidence.get('checks_failed') or 0)} failed",
            _prov("evidence"),
        ),
        (
            "Outcome",
            f"{_text(outcome.get('decision_status'))} · asserted by {_text(outcome.get('asserted_by')) or 'none'}",
            _prov("outcome"),
        ),
    ]


def _actions_detail(receipt: Mapping[str, Any]) -> list[str]:
    dims = receipt.get("dimensions", {}) if isinstance(receipt.get("dimensions"), Mapping) else {}
    actions = dims.get("actions", {}) if isinstance(dims.get("actions"), Mapping) else {}
    lines: list[str] = []

    names = actions.get("tool_names_preview") or []
    if names:
        rendered = ", ".join(f"{_code(p.get('name'))}×{int(p.get('count') or 0)}" for p in names)
        elided = int(actions.get("tool_names_elided") or 0)
        if elided:
            rendered += f" … +{elided} more"
        lines.append(f"- **Tools:** {rendered}")

    files = actions.get("touched_files_preview") or []
    if files:
        rendered = ", ".join(_code(path) for path in files)
        elided = int(actions.get("touched_files_elided") or 0)
        if elided:
            rendered += f" … +{elided} more"
        lines.append(f"- **Files touched:** {rendered}")

    commands = actions.get("commands_preview") or []
    if commands:
        rendered = ", ".join(_code(cmd) for cmd in commands)
        elided = int(actions.get("commands_elided") or 0)
        if elided:
            rendered += f" … +{elided} more"
        lines.append(f"- **Commands run:** {rendered}")

    return lines


def _timeline_rows(receipt: Mapping[str, Any]) -> list[tuple[str, str, str, str, str]]:
    timeline = receipt.get("timeline", {}) if isinstance(receipt.get("timeline"), Mapping) else {}
    events = [e for e in (timeline.get("events") or []) if isinstance(e, Mapping)]
    if not events:
        return []
    base = min(float(e.get("occurred_at") or 0.0) for e in events)
    rows: list[tuple[str, str, str, str, str]] = []
    for event in events:
        rows.append(
            (
                _relative(float(event.get("occurred_at") or 0.0) - base),
                _text(event.get("lane")),
                _cell(event.get("title")),
                _text(event.get("status")),
                _cell(event.get("source")),
            )
        )
    return rows


def render_receipt_markdown(
    receipt: Mapping[str, Any],
    *,
    include_timeline: bool = True,
    heading_level: int = 3,
) -> str:
    """Render one receipt as Markdown. ``heading_level`` sets the ``#`` depth of
    the title so an example page can nest the receipt under its own headings."""

    axes = receipt.get("axes", {}) if isinstance(receipt.get("axes"), Mapping) else {}
    dims = receipt.get("dimensions", {}) if isinstance(receipt.get("dimensions"), Mapping) else {}
    decision = axes.get("decision_status", {}) if isinstance(axes.get("decision_status"), Mapping) else {}
    evidence = axes.get("evidence_strength", {}) if isinstance(axes.get("evidence_strength"), Mapping) else {}
    handoff = axes.get("handoff", {}) if isinstance(axes.get("handoff"), Mapping) else {}

    hashes = "#" * max(1, min(6, heading_level))
    out: list[str] = []
    out.append(f"{hashes} Work Receipt — {_text(receipt.get('title') or 'Task')}")
    out.append("")
    out.append(_code(receipt.get("task_id") or ""))
    out.append("")

    out.append(
        f"- **Decision status — `{_text(decision.get('key') or 'unknown').upper()}`** · "
        f"asserted by `{_text(decision.get('asserted_by') or 'none')}`"
    )
    if decision.get("statement"):
        out.append(f"  {_text(decision['statement'])}")
    if handoff.get("handed_off") and _text(decision.get("key")) != "handed_off":
        out.append("- **Lifecycle — ↗ Handed off**")
        if handoff.get("statement"):
            out.append(f"  {_text(handoff['statement'])}")
    out.append(f"- **Evidence coverage — {evidence_coverage_headline(evidence)}**")
    ledger = evidence_coverage_ledger(evidence)
    if ledger:
        out.append(f"  {ledger}")
    if evidence.get("definition"):
        out.append(f"  {_text(evidence['definition'])}")
    out.append("")
    if axes.get("orthogonality_note"):
        out.append(f"> {_text(axes['orthogonality_note'])}")
        out.append("")

    out.append("| Dimension | Summary | Source |")
    out.append("| --- | --- | --- |")
    for name, summary, source in _dimension_rows(receipt):
        out.append(f"| {name} | {_cell(summary)} | {_cell(source)} |")
    out.append("")

    detail = _actions_detail(receipt)
    if detail:
        out.append("**What ran**")
        out.append("")
        out.extend(detail)
        out.append("")

    if include_timeline:
        rows = _timeline_rows(receipt)
        if rows:
            timeline = receipt.get("timeline", {})
            shown = int(timeline.get("shown") or len(rows))
            total = int(timeline.get("total") or len(rows))
            suffix = f" ({shown} of {total} shown)" if total > shown else ""
            out.append(f"**Timeline**{suffix}")
            out.append("")
            out.append("| When | Lane | Event | Status | Source |")
            out.append("| --- | --- | --- | --- | --- |")
            for when, lane, title, status, source in rows:
                out.append(f"| {when} | {lane} | {title} | {status} | {source} |")
            out.append("")

    gaps = dims.get("gaps", {}) if isinstance(dims.get("gaps"), Mapping) else {}
    gap_items = gaps.get("items") or []
    if gap_items:
        out.append(f"**Gaps ({int(gaps.get('count') or len(gap_items))})** — what could not be proven")
        out.append("")
        for item in gap_items:
            if not isinstance(item, Mapping):
                continue
            out.append(f"- **{_text(item.get('dimension'))}** — {_text(item.get('reason'))}")
        out.append("")

    legend = dims.get("provenance", {}) if isinstance(dims.get("provenance"), Mapping) else {}
    legend_map = legend.get("legend") if isinstance(legend.get("legend"), Mapping) else {}
    if legend_map:
        out.append("**Provenance**")
        out.append("")
        for source, description in legend_map.items():
            out.append(f"- `{_text(source)}` — {_text(description)}")
        out.append("")

    # Collapse any trailing blank lines to exactly one terminal newline.
    text = "\n".join(out).rstrip("\n")
    return text + "\n"


__all__ = ["render_receipt_markdown"]
