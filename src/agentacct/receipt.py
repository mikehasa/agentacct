"""The Receipt: one canonical answer to the 8 questions about one Task.

A Receipt is the product's core object — "a local receipt for every coding-agent
task: what ran, what passed, what it cost, and what remains unproven." It is a
projection over ONE converged Task (see ``task_projection``/``task_identity``)
that a person can read in under a minute and know whether to trust, without
reopening the transcript.

It answers eight questions, each carrying its own provenance and gaps:

 1. task       — what was asked, and the boundary of this Task
 2. actors     — which agents / models / subagent sessions did the work
 3. actions    — which KINDS of tools ran, and which artifacts were touched
 4. cost       — tokens and dollars, and on what BASIS that number rests
 5. evidence   — which checks actually ran, with exit codes and scope
 6. outcome    — the decision status (reported / verified / blocked / …)
 7. gaps       — what could NOT be proven (unlinked work, missing checks, …)
 8. provenance — where each field came from (client log / MCP / hook / CI / …)

Two axes are kept deliberately SEPARATE and named explicitly:

  * ``decision_status``  — what a human or agent SAYS happened.
  * ``evidence_strength``— how well that is actually PROVEN.

An agent reporting "done" never raises evidence strength, and a human review or
approval never counts as machine verification (a finding disposition carries
``authoritative_for_check_result=False``). The two are computed from disjoint
inputs so the separation cannot silently erode: evidence strength is derived
ONLY from recorded checks and per-step verification; decision status is derived
from work statuses, blockers, and human dispositions.

This module reuses ``build_task_intelligence`` (states / decision brief /
verification / findings / coverage / lanes / timeline) rather than reinventing
it, and adds the two missing layers M1 requires: a unified per-field provenance
map and a unified gaps block.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .task_intelligence import build_task_intelligence
from .task_outcome import (
    latest_task_checks,
    reduce_task_outcome,
    step_verification_counts,
    task_newest_event_at,
)


# frozen: stored/emitted receipts carry this schema string; surfaces pin it.
RECEIPT_SCHEMA_VERSION = "agentacct.receipt.v1"

# The user-facing provenance vocabulary — ONE list every surface shares. These
# answer "where did this fact come from", not "how strong is it" (that is the
# evidence axis). ``human`` is first-class here (it is not, in the lower-level
# envelope-authority model, where it is folded into "none").
SOURCE_CLIENT_LOG = "client_log"
SOURCE_MCP = "mcp"
SOURCE_HOOK = "hook"
SOURCE_CI = "ci"
SOURCE_GIT = "git"
SOURCE_HUMAN = "human"
SOURCE_NONE = "none"

PROVENANCE_LEGEND: dict[str, str] = {
    SOURCE_CLIENT_LOG: "Observed in the agent's own local session / usage log.",
    SOURCE_MCP: "Recorded by the agent through agentacct's MCP tools (sections, files, checks).",
    SOURCE_HOOK: "Captured by an agentacct client hook (tool categories, mechanical checks).",
    SOURCE_CI: "Reported by external CI or the model provider.",
    SOURCE_GIT: "Derived from the git repository.",
    SOURCE_HUMAN: "Asserted by a person (review, approval, or finding disposition).",
    SOURCE_NONE: "Not captured by any source — recorded here as a gap, never guessed.",
}

# decision_status.key -> who ASSERTS that status. Kept separate from evidence:
# a machine-verified outcome is asserted by the machine; a blocker or a plain
# "done" is an agent report; a reviewed/resolved finding is a human assertion.
_DECISION_ASSERTED_BY: dict[str, str] = {
    "verified": "machine",
    "finding": "machine",
    "finding_superseded": "machine",
    # ``failed`` is refined out of ``blocked`` from an agent-recorded work status,
    # not a machine check — so it is an agent report, exactly like ``blocked``.
    "failed": "agent_report",
    "blocked": "agent_report",
    "reported": "agent_report",
    "resolved": "agent_report",
    "mostly_done": "agent_report",
    "handed_off": "agent_report",
    "in_progress": "agent_report",
    "observed": "none",
    "unknown": "none",
}

_DECISION_STATEMENTS: dict[str, str] = {
    "verified": "Recorded machine evidence verifies the latest outcome.",
    "finding": "A recorded check found an issue in the work being reviewed.",
    "finding_superseded": (
        "A recorded check failed, but a later same-scope check passed; the finding is kept "
        "in history and is not a verified outcome."
    ),
    "failed": "A step was recorded as failed.",
    "blocked": "The agent recorded a blocker for this Task.",
    "resolved": "A later passed check reports the exact blocker resolved; this is not a verified completion.",
    "reported": "The agent reported completing work; no linked passing check verifies it yet.",
    "handed_off": "The work was cleanly handed off (continued elsewhere); a deliberate stop, not a completion.",
    "mostly_done": "Recorded steps completed while one or more were left open; not a claim the Task is finished.",
    "in_progress": "This Task is still in progress.",
    "observed": "agentacct observed this Task but no outcome was recorded.",
    "unknown": "agentacct observed this Task but no outcome was recorded.",
}

_SUCCESS_STATUSES = {"completed", "passed", "resolved"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _items(task: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = task.get("work_items") if isinstance(task.get("work_items"), list) else []
    return [row for row in rows if isinstance(row, Mapping)]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# --- Evidence axis ------------------------------------------------------------

def _check_source(check: Mapping[str, Any]) -> str:
    source_type = _text(check.get("source_type")).lower()
    source = _text(check.get("source")).lower()
    if source_type == "client_hook":
        return SOURCE_HOOK
    if source_type in {"ci", "external", "provider"} or source in {"ci", "github_actions"}:
        return SOURCE_CI
    return SOURCE_MCP


def _project_checks(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Shape the Task's latest-per-identity checks for the Evidence dimension."""

    shaped: list[dict[str, Any]] = []
    for check in latest_task_checks(task):
        if not isinstance(check, Mapping):
            continue
        shaped.append(
            {
                "kind": _text(check.get("evidence_type")) or "check",
                "name": _text(check.get("name") or check.get("summary")) or "recorded check",
                "result": _text(check.get("result")).lower() or "unknown",
                "exit_code": _int_or_none(check.get("exit_code")),
                "scope": _text(
                    check.get("resolution_scope")
                    or check.get("project_identity")
                    or check.get("project_dir")
                )
                or None,
                "source": _check_source(check),
                "superseded": _text(check.get("supersession_state")).lower() == "superseded",
                "at": _number(check.get("created_at") or check.get("occurred_at")) or None,
            }
        )
    return shaped


def _evidence_strength(verification: Mapping[str, Any], checks: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Grade positive proof ONLY. Never raised by agent claims or human review.

    A failing check is not positive proof, so it never lifts the grade — it
    lands on the decision axis as a finding instead.
    """

    verified = int(verification.get("verified_step_count") or 0)
    total = int(verification.get("total_step_count") or 0)
    reported = int(verification.get("agent_reported_step_count") or 0)
    passed = sum(1 for check in checks if _text(check.get("result")).lower() == "passed")
    failed = sum(1 for check in checks if _text(check.get("result")).lower() in {"failed", "error"})

    if total > 0 and verified == total:
        key = "verified"
    elif verified > 0 or passed > 0:
        key = "checked"
    elif reported > 0:
        key = "reported"
    else:
        key = "none"

    strongest = next(
        (check for check in checks if _text(check.get("result")).lower() == "passed"),
        None,
    )
    return {
        "key": key,
        "label": key.title(),
        "verified_step_count": verified,
        "total_step_count": total,
        "agent_reported_step_count": reported,
        "checks_total": len(checks),
        "checks_passed": passed,
        "checks_failed": failed,
        "strongest_proof": (_text(strongest.get("name") or strongest.get("summary")) or None)
        if strongest is not None
        else None,
    }


# --- Decision axis ------------------------------------------------------------

def _decision_status(
    task: Mapping[str, Any],
    *,
    latest_store_activity_at: float | None,
) -> dict[str, Any]:
    canonical = reduce_task_outcome(task, latest_store_activity_at=latest_store_activity_at)
    key = _text(canonical.get("key")) or "observed"

    # Refine ``failed`` out of ``blocked`` at the Receipt layer without changing
    # the task_outcome contract: a step recorded literally ``failed`` with no
    # blocker text is a failure, not an agent-declared blocker.
    if key == "blocked":
        statuses = [_text(item.get("latest_status")).lower() for item in _items(task)]
        has_failed = any(status == "failed" for status in statuses)
        has_blocker = any(_text(item.get("blocker")) for item in _items(task))
        if has_failed and not has_blocker:
            key = "failed"

    attention = _text(canonical.get("finding_attention_state"))
    asserted_by = _DECISION_ASSERTED_BY.get(key, "none")
    # A finding a human has reviewed or resolved is now a HUMAN assertion — and
    # (crucially) this never touches evidence strength.
    if key == "finding" and attention in {"reviewed", "resolved"}:
        asserted_by = "human"

    statement = _DECISION_STATEMENTS.get(key, _DECISION_STATEMENTS["unknown"])
    if key == "finding" and attention == "resolved":
        statement = "You marked the recorded finding resolved; this is not machine verification."
    elif key == "finding" and attention == "reviewed":
        statement = "Every current finding was reviewed; no passing check has replaced the failed evidence."

    return {
        "key": key,
        "label": key.replace("_", " ").title(),
        "statement": statement,
        "asserted_by": asserted_by,
        "finding_attention_state": attention or None,
    }


def _asserted_by_source(asserted_by: str, checks: list[Mapping[str, Any]]) -> str:
    if asserted_by == "human":
        return SOURCE_HUMAN
    if asserted_by == "machine":
        sources = {check.get("source") for check in checks}
        if SOURCE_HOOK in sources:
            return SOURCE_HOOK
        if SOURCE_CI in sources:
            return SOURCE_CI
        return SOURCE_MCP
    if asserted_by == "agent_report":
        return SOURCE_MCP
    return SOURCE_NONE


# --- Dimension builders -------------------------------------------------------

def _boundary(task: Mapping[str, Any]) -> dict[str, Any]:
    sessions = task.get("sessions") if isinstance(task.get("sessions"), list) else []
    project = next(
        (_text(session.get("project")) for session in sessions if isinstance(session, Mapping) and _text(session.get("project"))),
        None,
    )
    scope = next(
        (
            _text(session.get("identity_scope_state"))
            for session in sessions
            if isinstance(session, Mapping) and _text(session.get("identity_scope_state"))
        ),
        None,
    )
    root_keys = task.get("root_keys") if isinstance(task.get("root_keys"), list) else []
    return {
        "primary_root": _mapping(task.get("primary_root")) or None,
        "root_count": len(root_keys),
        "session_count": int(task.get("session_count") or 0),
        "is_continuation": bool(task.get("continuation_id")),
        "project": project,
        "identity_scope": scope or "unscoped",
    }


def _task_dimension(task: Mapping[str, Any], title: str) -> dict[str, Any]:
    objectives: list[str] = []
    seen: set[str] = set()
    for item in _items(task):
        objective = _text(item.get("objective") or item.get("title") or item.get("summary"))
        if objective and objective not in seen:
            seen.add(objective)
            objectives.append(objective)
    boundary = _boundary(task)
    gaps: list[str] = []
    if not objectives:
        gaps.append("No explicit objective was recorded for this Task.")
    if boundary["identity_scope"] == "unscoped":
        gaps.append("Task identity is unscoped (no org/project namespace to bind it).")
    provenance = [SOURCE_MCP] if objectives else []
    if boundary["session_count"]:
        provenance.append(SOURCE_CLIENT_LOG)
    return {
        "title": title,
        "objectives": objectives,
        "boundary": boundary,
        "provenance": sorted(set(provenance)) or [SOURCE_NONE],
        "gaps": gaps,
    }


def _actors_dimension(task: Mapping[str, Any], intelligence: Mapping[str, Any]) -> dict[str, Any]:
    lanes = intelligence.get("lanes") if isinstance(intelligence.get("lanes"), list) else []
    models = list(intelligence.get("models") or ())
    primary = next((lane for lane in lanes if isinstance(lane, Mapping) and lane.get("role") == "primary"), None)
    supporting = [lane for lane in lanes if isinstance(lane, Mapping) and lane.get("role") == "supporting"]
    gaps: list[str] = []
    if not models:
        gaps.append("No model was observed for this Task's usage.")
    if supporting and not any(lane.get("session_kinds") for lane in supporting):
        gaps.append("Subagent roles were not scanned, so supporting sessions show as counts only.")
    return {
        "primary_agent": _text(primary.get("client")) if isinstance(primary, Mapping) else None,
        "models": models,
        "lanes": lanes,
        "subagent_session_count": int(task.get("supporting_count") or 0),
        "child_session_count": int(task.get("child_count") or 0),
        "provenance": [SOURCE_CLIENT_LOG],
        "gaps": gaps,
    }


def _actions_dimension(task: Mapping[str, Any]) -> dict[str, Any]:
    actions = _mapping(task.get("actions"))
    counts = _mapping(actions.get("tool_category_counts"))
    touched = actions.get("touched_files") if isinstance(actions.get("touched_files"), list) else []
    provenance: list[str] = []
    gaps: list[str] = []
    if touched:
        provenance.append(SOURCE_MCP)
    else:
        gaps.append("No touched files were recorded for this Task's steps.")
    if counts:
        provenance.append(SOURCE_HOOK)
    else:
        gaps.append(
            "Tool categories were not instrumented for this session; Actions shows touched files only."
        )
    return {
        "tool_category_counts": dict(counts),
        "tool_category_total": int(actions.get("tool_category_total") or 0),
        "touched_files": list(touched),
        "touched_file_count": int(actions.get("touched_file_count") or len(touched)),
        "provenance": sorted(set(provenance)) or [SOURCE_NONE],
        "gaps": gaps,
    }


def _cost_dimension(task: Mapping[str, Any]) -> dict[str, Any]:
    usage = _mapping(task.get("usage"))
    cost_complete = bool(usage.get("cost_complete"))
    estimated = usage.get("estimated_cost_usd")
    cost_basis = _text(usage.get("cost_basis")) or None
    cost_confidence = _text(usage.get("cost_confidence")) or None
    gaps: list[str] = []
    if not cost_complete:
        gaps.append("Cost is incomplete: some usage rows are unpriced or excluded.")
    if cost_confidence in {"estimated_from_tokens", "estimated"}:
        gaps.append("Cost is a pricing-table estimate from token counts, not a billed amount.")
    if estimated is None and int(usage.get("rows") or 0) == 0:
        gaps.append("No usage was recorded for this Task.")
    return {
        "estimated_cost_usd": estimated,
        "cost_basis": cost_basis,
        "cost_confidence": cost_confidence,
        "cost_complete": cost_complete,
        "tokens": {
            "fresh": int(usage.get("fresh_tokens") or 0),
            "cache_creation": int(usage.get("cache_creation_tokens") or 0),
            "cache_read": int(usage.get("cache_read_tokens") or 0),
            "total": int(usage.get("total_tokens") or 0),
        },
        "provenance": [SOURCE_CLIENT_LOG] if int(usage.get("rows") or 0) else [SOURCE_NONE],
        "gaps": gaps,
    }


def _evidence_dimension(checks: list[Mapping[str, Any]], strength: Mapping[str, Any]) -> dict[str, Any]:
    sources = sorted({_text(check.get("source")) for check in checks if _text(check.get("source"))})
    gaps: list[str] = []
    if not checks:
        gaps.append("No machine checks were recorded for this Task.")
    reported_only = int(strength.get("agent_reported_step_count") or 0)
    if reported_only:
        gaps.append(f"{reported_only} completed step(s) have no linked passing check.")
    return {
        "checks": list(checks),
        "checks_total": int(strength.get("checks_total") or 0),
        "checks_passed": int(strength.get("checks_passed") or 0),
        "checks_failed": int(strength.get("checks_failed") or 0),
        "provenance": sources or [SOURCE_NONE],
        "gaps": gaps,
    }


def _outcome_dimension(
    decision: Mapping[str, Any],
    verification: Mapping[str, Any],
    decision_brief: Mapping[str, Any],
    checks: list[Mapping[str, Any]],
) -> dict[str, Any]:
    asserted_by = _text(decision.get("asserted_by")) or "none"
    gaps: list[str] = []
    if decision.get("key") in {"in_progress", "observed", "unknown"}:
        gaps.append("No terminal outcome has been recorded for this Task yet.")
    return {
        "decision_status": decision.get("key"),
        "statement": decision.get("statement"),
        "asserted_by": asserted_by,
        "per_step": {
            "verified": int(verification.get("verified_step_count") or 0),
            "total": int(verification.get("total_step_count") or 0),
            "agent_reported": int(verification.get("agent_reported_step_count") or 0),
        },
        "owner": decision_brief.get("owner"),
        "next_action": decision_brief.get("next_action"),
        "provenance": [_asserted_by_source(asserted_by, checks)],
        "gaps": gaps,
    }


# --- Roll-ups (dimensions 7 & 8) ---------------------------------------------

def _roll_up_gaps(
    dimensions: Mapping[str, Mapping[str, Any]],
    coverage: list[Mapping[str, Any]],
    task: Mapping[str, Any],
) -> dict[str, Any]:
    items: list[dict[str, str]] = []
    for name, dimension in dimensions.items():
        for reason in dimension.get("gaps", []) or []:
            items.append({"dimension": name, "reason": reason})
    # Coverage rows the intelligence layer already computed that aren't fully
    # recorded are honest gaps too (kept distinct from the per-dimension ones).
    for row in coverage:
        if not isinstance(row, Mapping):
            continue
        state = _text(row.get("state"))
        if state and state not in {"recorded"}:
            items.append(
                {
                    "dimension": "coverage",
                    "reason": f"{_text(row.get('dimension')) or 'dimension'} is {state}.",
                }
            )
    unlinked = int(task.get("session_unlinked_work_count") or 0)
    if unlinked:
        items.append(
            {
                "dimension": "actors",
                "reason": f"{unlinked} work item(s) could not be tied to an exact session.",
            }
        )
    return {"items": items, "count": len(items)}


def _roll_up_provenance(dimensions: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    by_field: dict[str, list[str]] = {}
    present: set[str] = set()
    for name, dimension in dimensions.items():
        sources = [source for source in (dimension.get("provenance") or []) if source]
        by_field[name] = sources or [SOURCE_NONE]
        present.update(by_field[name])
    return {
        "by_dimension": by_field,
        "sources_present": sorted(present),
        "legend": {source: PROVENANCE_LEGEND[source] for source in sorted(present) if source in PROVENANCE_LEGEND},
    }


# --- Public entry point -------------------------------------------------------

def build_receipt(
    task: Mapping[str, Any],
    *,
    public_task_id: str,
    title: str,
    control: Mapping[str, Any] | None = None,
    timeline_limit: int = 50,
    latest_store_activity_at: float | None = None,
) -> dict[str, Any]:
    """Project one enriched Task into a canonical ``agentacct.receipt.v1``.

    ``task`` is a single element of a decorated, evidence-attached Task
    projection (``build_store_task_projection(...)["tasks"][i]`` after
    ``_attach_evidence_to_task_projection``). ``build_task_intelligence`` is
    reused for the states / decision brief / verification / findings / coverage
    / lanes / timeline; this function adds the two orthogonal axes and the
    unified provenance and gaps roll-ups.
    """

    intelligence = build_task_intelligence(
        task,
        public_task_id=public_task_id,
        title=title,
        control=control,
        timeline_limit=timeline_limit,
        latest_store_activity_at=latest_store_activity_at,
    )
    verification = step_verification_counts(task)
    checks = _project_checks(task)
    evidence_strength = _evidence_strength(verification, checks)
    decision = _decision_status(task, latest_store_activity_at=latest_store_activity_at)
    decision_brief = _mapping(intelligence.get("decision_brief"))

    dimensions: dict[str, dict[str, Any]] = {
        "task": _task_dimension(task, title),
        "actors": _actors_dimension(task, intelligence),
        "actions": _actions_dimension(task),
        "cost": _cost_dimension(task),
        "evidence": _evidence_dimension(checks, evidence_strength),
        "outcome": _outcome_dimension(decision, verification, decision_brief, checks),
    }
    coverage = intelligence.get("coverage") if isinstance(intelligence.get("coverage"), list) else []
    # Roll both meta-dimensions up over ONLY the six content dimensions, before
    # inserting them — ``gaps`` and ``provenance`` carry no provenance of their
    # own, so including them would manufacture a spurious ``none`` source.
    gaps = _roll_up_gaps(dimensions, coverage, task)
    provenance = _roll_up_provenance(dimensions)
    dimensions["gaps"] = gaps
    dimensions["provenance"] = provenance

    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "task_id": public_task_id,
        "title": title,
        "axes": {
            "decision_status": decision,
            "evidence_strength": evidence_strength,
            "orthogonality_note": (
                "Evidence strength and decision status are separate axes: an agent reporting "
                "'done' never raises evidence strength, and a human review or approval never "
                "counts as machine verification."
            ),
        },
        "dimensions": dimensions,
        # Rich sub-objects reused verbatim by the CLI / app / TUI surfaces.
        "lanes": intelligence.get("lanes"),
        "timeline": intelligence.get("timeline"),
        "findings": intelligence.get("findings"),
        "coverage": intelligence.get("coverage"),
        "duration_seconds": intelligence.get("duration_seconds"),
        "models": intelligence.get("models"),
        "usage": intelligence.get("usage"),
        "raw_evidence": intelligence.get("raw_evidence"),
    }


def build_receipt_summary(
    task: Mapping[str, Any],
    *,
    public_task_id: str,
    title: str,
    latest_store_activity_at: float | None = None,
) -> dict[str, Any]:
    """A compact Receipt row for a task LIST — the two axes plus cost/activity.

    Shares the exact decision/evidence reducers with ``build_receipt`` so the
    list and the detail can never disagree, without paying for the full 8
    dimensions per row.
    """

    verification = step_verification_counts(task)
    checks = _project_checks(task)
    evidence_strength = _evidence_strength(verification, checks)
    decision = _decision_status(task, latest_store_activity_at=latest_store_activity_at)
    usage = _mapping(task.get("usage"))
    return {
        "task_id": public_task_id,
        "title": title,
        "decision_status": {
            "key": decision["key"],
            "label": decision["label"],
            "asserted_by": decision["asserted_by"],
        },
        "evidence_strength": {
            "key": evidence_strength["key"],
            "label": evidence_strength["label"],
            "verified_step_count": evidence_strength["verified_step_count"],
            "total_step_count": evidence_strength["total_step_count"],
        },
        "cost": {
            "estimated_cost_usd": usage.get("estimated_cost_usd"),
            "cost_basis": _text(usage.get("cost_basis")) or None,
            "cost_complete": bool(usage.get("cost_complete")),
        },
        "session_count": int(task.get("session_count") or 0),
        "last_activity_at": _number(task.get("last_activity_at")) or None,
    }


def latest_store_activity(tasks: list[Mapping[str, Any]]) -> float | None:
    """The newest event time across every Task — the deterministic 'now' the
    outcome reducer compares against (never the wall clock). See
    ``reduce_task_outcome``'s ``latest_store_activity_at``."""

    newest = max(
        (task_newest_event_at(task) for task in tasks if isinstance(task, Mapping)),
        default=0.0,
    )
    return newest or None


__all__ = [
    "RECEIPT_SCHEMA_VERSION",
    "PROVENANCE_LEGEND",
    "build_receipt",
    "build_receipt_summary",
    "latest_store_activity",
]
