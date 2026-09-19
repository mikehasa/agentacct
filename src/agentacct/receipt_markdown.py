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

from .display_budget import MARKDOWN_CELL_CHARACTERS, truncate_for_display

from collections.abc import Mapping
from typing import Any

from .display_vocabulary import (
    DECISION_LABELS,
    GAP_LABEL_FALLBACK,
    RECEIPT_FIELD_LABELS,
    asserted_by_phrase,
    check_event_status_label,
    decision_label,
    receipt_field_label,
    step_status_label,
    source_label,
    timeline_lane_label,
)
from .plural import count_noun
from .receipt import (
    PROVENANCE_LEGEND,
    check_tally_text,
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
    """A compact relative offset (``+0s`` / ``+0.3s`` / ``+45s`` / ``+2m10s`` /
    ``+1h04m``). Offsets under ten seconds keep one decimal, so events a
    fraction of a second apart never all read ``+0s``."""

    if 0 < seconds < 9.95:
        tenths = round(seconds, 1)
        if tenths != int(tenths):
            return f"+{tenths:.1f}s"
    total = int(round(seconds))
    if total < 0:
        total = 0
    if total < 60:
        return f"+{total}s"
    if total < 3600:
        return f"+{total // 60}m{total % 60:02d}s"
    return f"+{total // 3600}h{(total % 3600) // 60:02d}m"


def receipt_dimension_rows(receipt: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    dims = receipt.get("dimensions", {}) if isinstance(receipt.get("dimensions"), Mapping) else {}

    def _prov(name: str) -> str:
        dim = dims.get(name, {})
        sources = dim.get("provenance") if isinstance(dim, Mapping) else None
        return ", ".join(source_label(source) for source in (sources or []))

    task = dims.get("task", {}) if isinstance(dims.get("task"), Mapping) else {}
    objectives = task.get("objectives") or []
    boundary = task.get("boundary", {}) if isinstance(task.get("boundary"), Mapping) else {}
    # A table cell is read beside other columns, so it is a phrase rather than a
    # sentence. Objectives arrive as full prose, and two of them joined here can
    # exceed the cell budget several times over (see display_budget.py).
    task_summary = truncate_for_display(
        "; ".join(str(o) for o in objectives[:2]) or "no objective recorded",
        limit=MARKDOWN_CELL_CHARACTERS,
    )
    if boundary.get("project"):
        task_summary += f" · project {boundary['project']}"

    actors = dims.get("actors", {}) if isinstance(dims.get("actors"), Mapping) else {}
    actor_summary = " · ".join(
        part
        for part in (
            _text(actors.get("primary_agent")) or None,
            ", ".join(str(m) for m in (actors.get("models") or [])) or None,
            (count_noun(int(actors.get("subagent_session_count") or 0), "subagent") if actors.get("subagent_session_count") else None),
        )
        if part
    ) or "no agent recorded"

    actions = dims.get("actions", {}) if isinstance(dims.get("actions"), Mapping) else {}
    actions_summary = receipt_actions_text(actions)
    if int(actions.get("command_count") or 0):
        actions_summary += f" · ran {count_noun(int(actions.get('command_count') or 0), 'command')}"

    cost = dims.get("cost", {}) if isinstance(dims.get("cost"), Mapping) else {}
    evidence = dims.get("evidence", {}) if isinstance(dims.get("evidence"), Mapping) else {}
    outcome = dims.get("outcome", {}) if isinstance(dims.get("outcome"), Mapping) else {}

    labels = receipt_field_labels(receipt)
    return [
        (labels["task"], task_summary, _prov("task")),
        (labels["agents"], actor_summary, _prov("actors")),
        (labels["actions"], actions_summary, _prov("actions")),
        (labels["cost"], receipt_cost_text(cost), _prov("cost")),
        (
            labels["weekly_plan"],
            _text(cost.get("plan_share_headline")) or plan_share_headline(cost.get("plan_share")),
            "",
        ),
        (
            labels["checks"],
            _text(evidence.get("check_tally_text")) or check_tally_text(evidence),
            _prov("evidence"),
        ),
        (
            labels["decision"],
            f"{decision_label(outcome.get('decision_status'))} · asserted by "
            f"{_text(outcome.get('asserted_by_phrase')) or asserted_by_phrase(outcome.get('asserted_by'))}",
            _prov("outcome"),
        ),
    ]


def receipt_actions_text(actions: Mapping[str, Any]) -> str:
    """The Tool calls row: the synopsis tile (a count or its named absence),
    the category pairs when counted, and the related-path scope (a named
    absence when none was recorded — never ``touched 0 files``)."""

    synopsis = actions.get("actions_synopsis") if isinstance(actions.get("actions_synopsis"), Mapping) else None
    if synopsis is None:
        head = receipt_category_text(actions.get("tool_category_counts") or {})
    else:
        tile = synopsis.get("tile") if isinstance(synopsis.get("tile"), Mapping) else {}
        head = _text(synopsis.get("headline")) or _text(tile.get("absent"))
        counts = actions.get("tool_category_counts") or {}
        if counts:
            head += f" · {receipt_category_text(counts)}"
    count = actions.get("touched_file_count")
    paths = _text(actions.get("related_paths_text"))
    if not paths and isinstance(count, int) and count > 0:
        paths = f"touched {count_noun(count, 'file')}"
    return f"{head} · {paths}" if paths else head


def receipt_field_labels(receipt: Mapping[str, Any]) -> dict[str, str]:
    """The receipt's own field labels, falling back to the shared glossary."""

    shipped = receipt.get("field_labels") if isinstance(receipt.get("field_labels"), Mapping) else {}
    return {key: _text(shipped.get(key)) or label for key, label in RECEIPT_FIELD_LABELS.items()}


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
    task_title = _text(receipt.get("title")).casefold()
    rows: list[tuple[str, str, str, str, str]] = []
    for event in events:
        title = _text(event.get("title"))
        # Printed words only: the reducer's status, session and source labels
        # (a raw lane, status or source key never reaches the table).
        kind = _text(event.get("kind"))
        status = _text(event.get("status_label")) or (
            step_status_label(event.get("status"))
            if kind == "work"
            else check_event_status_label(event.get("status"), superseded=event.get("superseded") is True)
            if kind == "check"
            else decision_label(event.get("status"))
        )
        if kind == "check" and _text(event.get("revision_label")):
            title = f"{title} · {_text(event.get('revision_label'))}"
        rows.append(
            (
                _relative(float(event.get("occurred_at") or 0.0) - base),
                # A session title that only restates the Task title adds
                # nothing; the lane label names the session instead.
                _cell(
                    (session if (session := _text(event.get("session_title"))).casefold() != task_title else "")
                    or _text(event.get("lane_label"))
                    or timeline_lane_label(event.get("lane"))
                ),
                _cell(title),
                _cell(status),
                # A work step is always the agent's report (its raw source is
                # the client name, not a provenance key).
                _cell(_text(event.get("source_label")) or source_label("mcp" if kind == "work" else event.get("source"))),
            )
        )
    return rows


def receipt_attention_lines(receipt: Mapping[str, Any]) -> list[str]:
    """The Attention block as plain lines, in the reducer's own words: the
    reason, then the proof label (empty when the step title is the Task's own), the recorded blocker/finding text, the recorded next
    step and the current disposition. Markdown, the CLI and the TUI all print
    these exact lines (each adds only its own emphasis)."""

    attention = receipt.get("attention") if isinstance(receipt.get("attention"), Mapping) else None
    if not attention:
        return []
    reason = _text(attention.get("reason_label")) or "Needs attention"
    label = _text(attention.get("label"))
    # A failed check's label is reducer-built (``attention_label``): its lead
    # segment is the reason with the check kind folded in (``Failed build
    # check``), so it replaces the bare reason rather than printing beside it.
    # Every other kind's label is the step title the agent wrote, and prints as
    # written.
    if attention.get("kind") == "failed_check":
        head, _, rest = label.partition(" · ")
        reason, label = head or reason, rest
    lines = [reason, label]
    if _text(attention.get("summary")):
        lines.append(_text(attention["summary"]))
    if _text(attention.get("note_text")):
        lines.append(_text(attention["note_text"]))
    if _text(attention.get("next_step")):
        lines.append(f"Recorded next step: {_text(attention['next_step'])}")
    state = _text(attention.get("disposition_state")) or "open"
    note = _text(attention.get("disposition_note"))
    disposition = {
        "open": "Disposition: not yet reviewed by you.",
        "reviewed": "Disposition: reviewed by you; still needs resolving.",
        "resolved": "Disposition: resolved by you.",
    }.get(state, f"Disposition: {state.replace('_', ' ')}.")
    lines.append(disposition + (f" Note: {note}" if note else ""))
    # Every other open attention item behind this lead one, as a count.
    if _text(attention.get("more_text")):
        lines.append(_text(attention["more_text"]))
    return lines


def receipt_lead(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """The receipt's lead facts as plain strings, taken from the payload: the
    verdict headline, its gap line and health window, the decision with who
    asserted it, the agent's outcome summary and recorded next step, the
    handoff marker, and the coverage hero with its ledger. Every text surface
    (Markdown, CLI, TUI) renders these same strings so no two can word the same
    fact differently."""

    axes = receipt.get("axes", {}) if isinstance(receipt.get("axes"), Mapping) else {}
    dims = receipt.get("dimensions", {}) if isinstance(receipt.get("dimensions"), Mapping) else {}
    decision = axes.get("decision_status", {}) if isinstance(axes.get("decision_status"), Mapping) else {}
    evidence = axes.get("evidence_strength", {}) if isinstance(axes.get("evidence_strength"), Mapping) else {}
    handoff = axes.get("handoff", {}) if isinstance(axes.get("handoff"), Mapping) else {}
    verdict = receipt.get("verdict", {}) if isinstance(receipt.get("verdict"), Mapping) else {}
    outcome = dims.get("outcome", {}) if isinstance(dims.get("outcome"), Mapping) else {}
    attention = receipt.get("attention") if isinstance(receipt.get("attention"), Mapping) else None
    labels = receipt_field_labels(receipt)

    # The unproven part under its own label ("Not yet proven" only when a
    # completed checkable step is unchecked); stops and scope stay in the
    # coverage ledger with no proof label; cost absence stays on the Cost row.
    gap_text = _text(verdict.get("gap_text")) if "gap_text" in verdict else _text(verdict.get("gap"))
    health = verdict.get("health_window") if isinstance(verdict.get("health_window"), Mapping) else {}
    next_step = _text(outcome.get("next_step"))
    if attention and _text(attention.get("next_step")):
        next_step = ""  # the attention block already carries it
    handed_off = bool(handoff.get("handed_off")) and _text(decision.get("key")) != "handed_off"
    return {
        "field_labels": labels,
        "headline": _text(verdict.get("headline")),
        "gap_line": f"{_text(verdict.get('gap_label')) or GAP_LABEL_FALLBACK}: {gap_text}" if gap_text else "",
        "health_text": _text(health.get("text")),
        "decision_label": labels["decision"],
        "decision_word": _text(decision.get("label")) or decision_label(decision.get("key")),
        "asserted_by_phrase": _text(decision.get("asserted_by_phrase"))
        or asserted_by_phrase(decision.get("asserted_by")),
        "decision_statement": _text(decision.get("statement")),
        "outcome_summary_line": (
            f"Outcome (agent-reported): {_text(outcome['summary'])}" if _text(outcome.get("summary")) else ""
        ),
        "next_step_line": f"Recorded next step: {next_step}" if next_step else "",
        "handoff_word": f"↗ {DECISION_LABELS['handed_off']}" if handed_off else "",
        "handoff_statement": _text(handoff.get("statement")) if handed_off else "",
        "coverage_label": labels["coverage"],
        "coverage_hero": _text(evidence.get("coverage_hero")) or evidence_coverage_headline(evidence),
        "coverage_ledger": _text(evidence.get("coverage_ledger")) or evidence_coverage_ledger(evidence),
        "coverage_definition": _text(evidence.get("definition")),
        # The scope term's one definition, printed only when the ledger uses it.
        "scope_definition": (
            _text(evidence.get("scope_definition")) if int(evidence.get("not_checkable") or 0) else ""
        ),
    }


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

    hashes = "#" * max(1, min(6, heading_level))
    out: list[str] = []
    out.append(f"{hashes} Work Receipt — {_text(receipt.get('title') or 'Task')}")
    out.append("")
    out.append(_code(receipt.get("task_id") or ""))
    out.append("")

    # The verdict LEADS: one line joining what was claimed with how well it is
    # proven, then the single coverage+cost gap and the time-bounded health
    # claim. The Decision/Evidence breakdown below is the same facts, expanded.
    lead = receipt_lead(receipt)
    if lead["headline"]:
        out.append(f"- **{lead['headline']}**")
        for line in (lead["gap_line"], lead["health_text"]):
            if line:
                out.append(f"  {line}")
        out.append("")

    out.append(f"- **{lead['decision_label']} — {lead['decision_word']}** · asserted by {lead['asserted_by_phrase']}")
    for line in (lead["decision_statement"], lead["outcome_summary_line"], lead["next_step_line"]):
        if line:
            out.append(f"  {line}")
    if lead["handoff_word"]:
        out.append(f"- **Lifecycle — {lead['handoff_word']}**")
        if lead["handoff_statement"]:
            out.append(f"  {lead['handoff_statement']}")
    out.append(f"- **{lead['coverage_label']} — {lead['coverage_hero']}**")
    for line in (lead["coverage_ledger"], lead["scope_definition"], lead["coverage_definition"]):
        if line:
            out.append(f"  {line}")
    out.append("")
    if axes.get("orthogonality_note"):
        out.append(f"> {_text(axes['orthogonality_note'])}")
        out.append("")

    attention_lines = receipt_attention_lines(receipt)
    if attention_lines:
        reason, label, *rest = attention_lines
        out.extend(["**Attention**", "", f"- **{reason}**" + (f" · {label}" if label else "")])
        out.extend(f"  {line}" for line in rest)
        out.append("")

    out.append("| Dimension | Summary | Source |")
    out.append("| --- | --- | --- |")
    for name, summary, source in receipt_dimension_rows(receipt):
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
            out.append("| When | Session | Event | Status | Source |")
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
            label = _text(item.get("dimension_label")) or receipt_field_label(item.get("dimension"))
            out.append(f"- **{label}** — {_text(item.get('reason'))}")
        out.append("")

    legend = dims.get("provenance", {}) if isinstance(dims.get("provenance"), Mapping) else {}
    legend_map = legend.get("legend") if isinstance(legend.get("legend"), Mapping) else {}
    if legend_map:
        out.append("**Provenance**")
        out.append("")
        for source, description in legend_map.items():
            out.append(f"- **{source_label(source)}** — {_text(description)}")
        out.append("")

    # Collapse any trailing blank lines to exactly one terminal newline.
    text = "\n".join(out).rstrip("\n")
    return text + "\n"


__all__ = [
    "receipt_actions_text",
    "receipt_attention_lines",
    "receipt_dimension_rows",
    "receipt_field_labels",
    "receipt_lead",
    "render_receipt_markdown",
]
