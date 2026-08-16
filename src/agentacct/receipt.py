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
    EVIDENCE_GRADE_RANK,
    GRADE_CLAIMED,
    GRADE_EXTERNALLY_VERIFIED,
    GRADE_INDEPENDENTLY_CHECKED,
    GRADE_SELF_CHECKED,
    NON_CHECK_RELEVANT_KINDS,
    latest_task_checks,
    reduce_task_outcome,
    step_evidence_grade,
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
# agentacct's OWN inference from an ambient signal (e.g. a SessionEnd event) —
# not the agent's word, not a check, not a person. The weakest source: it may
# fill a gap honestly but never claims the certainty of a report or a check.
SOURCE_INFERRED = "inferred"
SOURCE_NONE = "none"

PROVENANCE_LEGEND: dict[str, str] = {
    SOURCE_CLIENT_LOG: "Observed in the agent's own local session / usage log.",
    SOURCE_MCP: "Recorded by the agent through agentacct's MCP tools (sections, files, checks).",
    SOURCE_HOOK: "Captured by an agentacct client hook (tool categories, mechanical checks).",
    SOURCE_CI: "Reported by external CI or the model provider.",
    SOURCE_GIT: "Derived from the git repository.",
    SOURCE_HUMAN: "Asserted by a person (review, approval, or finding disposition).",
    SOURCE_INFERRED: "Inferred by agentacct from an ambient signal (e.g. the session ended); not the agent's word.",
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
    # Inferred by agentacct from an ambient SessionEnd event — the weakest
    # provenance, deliberately NOT agent_report (the agent never said this).
    "ended_open": "inferred",
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
    "ended_open": (
        "The session ended with this step still open; agentacct inferred a stop from the session-end "
        "event — not a recorded completion or a deliberate handoff."
    ),
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


# The Actions dimension already CAPTURES every touched artifact path; surfaces
# previewed only the COUNT. This previews a capped slice of the actual paths and
# DISCLOSES the remainder rather than silently truncating (the receipt's honesty
# rule: a bound on coverage is stated, never hidden).
RECEIPT_TOUCHED_FILES_PREVIEW = 12


def touched_files_preview(
    actions: Mapping[str, Any], *, limit: int = RECEIPT_TOUCHED_FILES_PREVIEW
) -> tuple[list[str], int]:
    """Return (paths to show, count elided). Called ONCE, in ``_actions_dimension``,
    which bakes the result into the receipt as ``touched_files_preview`` /
    ``touched_files_elided`` — so every surface (CLI, TUI, macOS app) renders the
    same daemon-provided slice and the cap has a single source of truth."""

    files = [str(path) for path in (actions.get("touched_files") or []) if str(path).strip()]
    shown = files if limit is None or limit < 0 else files[:limit]
    return shown, max(0, len(files) - len(shown))


# The Actions dimension captures every command an execute tool ran; surfaces preview a
# capped slice and disclose the remainder (same honesty rule + single-source-of-truth as
# the touched-file preview). Commands are already single-line + best-effort scrubbed.
RECEIPT_COMMANDS_PREVIEW = 12


def commands_preview(
    actions: Mapping[str, Any], *, limit: int = RECEIPT_COMMANDS_PREVIEW
) -> tuple[list[str], int]:
    """Return (commands to show, count elided). Called ONCE, in ``_actions_dimension``,
    which bakes the result into the receipt as ``commands_preview`` / ``commands_elided``
    so every surface (CLI, TUI, macOS app) renders the same daemon-provided slice."""

    commands = [str(cmd) for cmd in (actions.get("commands") or []) if str(cmd).strip()]
    shown = commands if limit is None or limit < 0 else commands[:limit]
    return shown, max(0, len(commands) - len(shown))


# Tool NAMES are an OPEN set (unlike the 9 fixed categories), so the Actions
# dimension previews the most-used and discloses the remainder — computed ONCE
# here so every surface renders the same ranking, the same way the touched-file
# preview has a single source of truth.
RECEIPT_TOOL_NAMES_PREVIEW = 10


def tool_names_preview(
    actions: Mapping[str, Any], *, limit: int = RECEIPT_TOOL_NAMES_PREVIEW
) -> tuple[list[dict[str, Any]], int]:
    """Return (top tools by count, count of distinct names elided). Sorted by
    count descending, then name ascending for a stable tie-break."""

    counts = actions.get("tool_name_counts")
    counts = counts if isinstance(counts, Mapping) else {}
    ordered = sorted(
        (
            (str(name), int(count))
            for name, count in counts.items()
            if isinstance(count, int) and not isinstance(count, bool) and count > 0
        ),
        key=lambda item: (-item[1], item[0]),
    )
    shown = ordered if limit is None or limit < 0 else ordered[:limit]
    preview = [{"name": name, "count": count} for name, count in shown]
    return preview, max(0, len(ordered) - len(shown))


# --- Evidence axis ------------------------------------------------------------

def _check_source(check: Mapping[str, Any]) -> str:
    # Trust ``source_type`` only. The raw ``source`` is agent-authored and would
    # let an MCP check forge the CI label; see task_outcome._check_independence,
    # which is the load-bearing version of this same rule for the evidence tier.
    source_type = _text(check.get("source_type")).lower()
    if source_type == "client_hook":
        return SOURCE_HOOK
    if source_type in {"ci", "external", "provider"}:
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


_TIER_BY_GRADE = {
    GRADE_EXTERNALLY_VERIFIED: "externally_verified",
    GRADE_INDEPENDENTLY_CHECKED: "independently_checked",
    GRADE_SELF_CHECKED: "self_checked",
}
# strongest first — the coarse tier used for colour and list sort/filter only.
_TIER_ORDER = ("externally_verified", "independently_checked", "self_checked")


def _step_passed_check_ids(item: Mapping[str, Any]) -> set[str]:
    """Event-ids of the passing checks linked to ONE step (its projected latest
    checks, else its raw evidence events)."""

    raw = (
        item.get("current_check_events")
        if isinstance(item.get("current_check_events"), list)
        else item.get("evidence_events")
    )
    ids: set[str] = set()
    if isinstance(raw, list):
        for event in raw:
            if not isinstance(event, Mapping):
                continue
            if _text(event.get("result")).lower() != "passed":
                continue
            event_id = _text(event.get("event_id"))
            if event_id:
                ids.add(event_id)
    return ids


def _evidence_strength(
    task: Mapping[str, Any],
    checks: list[Mapping[str, Any]],
    verification: Mapping[str, Any],
) -> dict[str, Any]:
    """Coverage-first evidence: per-tier RATIOS over the checkable steps, plus an
    honesty LEDGER. There is deliberately NO single collapsed grade word — the
    counts ARE the headline; a categorical label would re-hide exactly the
    variation the ladder exists to show. One coarse ``strongest_tier`` is kept
    for colour + list sort/filter only, never as a headline word.

    Graded from positive proof ONLY (a failing check lands on the decision axis
    as a finding, never here) and from the same disjoint inputs as before, so it
    stays orthogonal to ``decision_status``: an agent's "done" or a human review
    never lifts a tier.
    """

    items = _items(task)
    tiers = {
        "externally_verified": 0,
        "independently_checked": 0,
        "self_checked": 0,
        "unchecked": 0,  # a checkable terminal step with only an agent claim (grade 'claimed')
    }
    not_checkable = 0
    open_or_incomplete = 0
    checkable = 0
    # "Hidden in a subagent" means the step ran in a session that is NOT a root —
    # a genuine subagent. A continuation ROOT (the same agent resuming in a new
    # session) is a root, not a subagent, so its steps must not be counted here,
    # or the ledger would falsely say a continued Task's own work "ran in
    # subagents". root_keys lists every root (primary + continuations).
    root_session_ids = {
        _text(ref.get("client_session_id"))
        for ref in (task.get("root_keys") if isinstance(task.get("root_keys"), list) else [])
        if isinstance(ref, Mapping) and _text(ref.get("client_session_id"))
    }
    primary_session = _text(_mapping(task.get("primary_root")).get("client_session_id"))
    if primary_session:
        root_session_ids.add(primary_session)
    hidden_in_subagents = 0
    for item in items:
        status = _text(item.get("latest_status")).lower()
        if status not in _SUCCESS_STATUSES:
            open_or_incomplete += 1
            continue
        session = _text(item.get("client_session_id"))
        if session and root_session_ids and session not in root_session_ids:
            hidden_in_subagents += 1
        kind = _text(item.get("kind")).lower() or "unknown"
        if kind in NON_CHECK_RELEVANT_KINDS:
            not_checkable += 1
            continue
        checkable += 1
        grade = step_evidence_grade(item)["grade"]
        tiers[_TIER_BY_GRADE.get(grade, "unchecked")] += 1

    strongest_tier = next((tier for tier in _TIER_ORDER if tiers[tier]), None)
    checked_total = sum(tiers[tier] for tier in _TIER_ORDER)

    # Unattributed checks: task-pool passing checks linked to no visible step —
    # they still count toward "N passed" but attach to nothing you can see.
    linked: set[str] = set()
    for item in items:
        linked |= _step_passed_check_ids(item)
    task_passing_ids = {
        _text(event.get("event_id"))
        for event in latest_task_checks(task)
        if isinstance(event, Mapping)
        and _text(event.get("result")).lower() == "passed"
        and _text(event.get("event_id"))
    }
    unattributed_checks = len(task_passing_ids - linked)

    passed = sum(1 for check in checks if _text(check.get("result")).lower() == "passed")
    failed = sum(1 for check in checks if _text(check.get("result")).lower() in {"failed", "error"})

    # Coarse key kept ONLY for colour + list sort/filter. It is an ordinal, not a
    # headline: undefined < unchecked < self_checked < independently < externally.
    if checkable == 0:
        key = "undefined"
    else:
        key = strongest_tier or "unchecked"

    return {
        "key": key,
        "gradeable": checkable > 0,
        "strongest_tier": strongest_tier,
        # the headline — per-tier ratios over checkable steps
        "checkable_total": checkable,
        "checked_total": checked_total,
        "by_tier": dict(tiers),
        # the ledger — what makes the ratio trustworthy
        "not_checkable": not_checkable,
        "open_or_incomplete": open_or_incomplete,
        "hidden_in_subagents": hidden_in_subagents,
        "unattributed_checks": unattributed_checks,
        "total_steps": len(items),
        # check-event tallies + continuity fields for existing readers
        "checks_total": len(checks),
        "checks_passed": passed,
        "checks_failed": failed,
        "verified_step_count": int(verification.get("verified_step_count") or 0),
        "total_step_count": int(verification.get("total_step_count") or 0),
        "agent_reported_step_count": int(verification.get("agent_reported_step_count") or 0),
        "definition": (
            "X of Y checkable steps carry a passing check; the tiers show how independent "
            "that check is. These are counts, not a probability of correctness."
        ),
    }


# The coverage headline + ledger — ONE formatting shared by CLI / TUI / app, so
# no two surfaces can word the same evidence differently (the M1 vocabulary rule).
EVIDENCE_TIER_LABEL = {
    "externally_verified": "externally-verified",
    "independently_checked": "independently-checked",
    "self_checked": "self-checked",
}


def evidence_coverage_headline(evidence: Mapping[str, Any]) -> str:
    """The coverage ratio, tier by tier — the headline IS the counts, never a
    single collapsed grade word. The one non-ratio case is ``Not gradeable`` (no
    checkable step, where a 0/0 ratio would be meaningless)."""

    if not evidence.get("gradeable"):
        return "Not gradeable (no verifiable steps recorded)"
    by_tier = evidence.get("by_tier") or {}
    total = int(evidence.get("checkable_total") or 0)
    parts = [
        f"{int(by_tier[tier])}/{total} {label}"
        for tier, label in EVIDENCE_TIER_LABEL.items()
        if by_tier.get(tier)
    ]
    if by_tier.get("unchecked"):
        parts.append(f"{int(by_tier['unchecked'])} unchecked")
    return " · ".join(parts) if parts else f"0/{total} checked"


def evidence_coverage_ledger(evidence: Mapping[str, Any]) -> str:
    """The honest ledger beneath the ratio: where the evidence is, and what the
    ratio does NOT cover — steps hidden in subagents, checks that attach to no
    step, non-verifiable steps, and still-open steps."""

    bits: list[str] = []
    hidden = int(evidence.get("hidden_in_subagents") or 0)
    if hidden:
        bits.append(f"{hidden} step(s) ran in subagents")
    not_checkable = int(evidence.get("not_checkable") or 0)
    if not_checkable:
        bits.append(f"{not_checkable} non-verifiable (research/docs)")
    unattributed = int(evidence.get("unattributed_checks") or 0)
    if unattributed:
        bits.append(f"{unattributed} check(s) attach to no step")
    still_open = int(evidence.get("open_or_incomplete") or 0)
    if still_open:
        bits.append(f"{still_open} step(s) still open")
    return " · ".join(bits)


# --- Decision axis ------------------------------------------------------------

def _decision_status(
    task: Mapping[str, Any],
    *,
    latest_store_activity_at: float | None,
    canonical: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if canonical is None:
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


def _handoff_marker(canonical: Mapping[str, Any]) -> dict[str, Any]:
    """The handoff LIFECYCLE marker — a signal kept deliberately SEPARATE from the
    decision word, alongside the two axes.

    A Task can read ``finding`` (a red check you must act on) AND still have been
    cleanly handed off: the decision word carries the louder problem, this marker
    carries the deliberate stop, and neither hides the other. ``handed_off`` here
    is the recency-aware disposition from ``reduce_task_outcome`` — true only when
    the handoff is the frontier (nothing still-open is newer than it), so a Task
    that was handed off and then RESUMED does not carry the marker. Surfaces show
    the chip only when it adds information the decision word does not already state
    (i.e. ``handed_off`` is true AND ``decision_status.key != "handed_off"``)."""

    handed_off = bool(canonical.get("handoff_current"))
    return {
        "handed_off": handed_off,
        "statement": _DECISION_STATEMENTS["handed_off"] if handed_off else None,
        "asserted_by": "agent_report",
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
    if asserted_by == "inferred":
        return SOURCE_INFERRED
    return SOURCE_NONE


# --- Dimension builders -------------------------------------------------------

def _boundary(task: Mapping[str, Any]) -> dict[str, Any]:
    sessions = task.get("sessions") if isinstance(task.get("sessions"), list) else []
    project = next(
        (_text(session.get("project")) for session in sessions if isinstance(session, Mapping) and _text(session.get("project"))),
        None,
    )
    project_identity_state = next(
        (
            _text(session.get("project_identity_state"))
            for session in sessions
            if isinstance(session, Mapping) and _text(session.get("project_identity_state"))
        ),
        None,
    )
    namespace_scope = next(
        (
            _text(session.get("identity_scope_state"))
            for session in sessions
            if isinstance(session, Mapping) and _text(session.get("identity_scope_state"))
        ),
        None,
    )
    # An explicitly identified project binds the Task even when it carries no
    # cryptographic org-namespace fingerprint (only codex / org-scoped sessions
    # have one). Report the strongest binding, so a claude-code Task in a known
    # project reads as "project"-scoped rather than "unscoped".
    if project_identity_state == "explicit":
        identity_scope = "project"
    elif namespace_scope:
        identity_scope = namespace_scope
    else:
        identity_scope = "unscoped"
    root_keys = task.get("root_keys") if isinstance(task.get("root_keys"), list) else []
    return {
        "primary_root": _mapping(task.get("primary_root")) or None,
        "root_count": len(root_keys),
        "session_count": int(task.get("session_count") or 0),
        "is_continuation": bool(task.get("continuation_id")),
        "project": project,
        "project_identity_state": project_identity_state or None,
        "identity_scope": identity_scope,
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
        gaps.append("This Task could not be bound to a project or namespace.")
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


def _evidence_touched_files(task: Mapping[str, Any]) -> list[str]:
    """File paths the Task's machine checks recorded — a section often lists no
    files of its own while the checks it ran named the exact paths, so those
    paths would otherwise be dropped from the Actions dimension."""

    paths: list[str] = []
    seen: set[str] = set()
    items = task.get("work_items") if isinstance(task.get("work_items"), list) else []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        for event in item.get("evidence_events") or []:
            if not isinstance(event, Mapping):
                continue
            for candidate in event.get("files") or []:
                path = _text(candidate)
                if path and path not in seen:
                    seen.add(path)
                    paths.append(path)
    return paths


def _actions_dimension(task: Mapping[str, Any]) -> dict[str, Any]:
    actions = _mapping(task.get("actions"))
    counts = _mapping(actions.get("tool_category_counts"))
    name_counts = _mapping(actions.get("tool_name_counts"))
    names_preview, names_elided = tool_names_preview({"tool_name_counts": name_counts})
    section_files = actions.get("touched_files") if isinstance(actions.get("touched_files"), list) else []
    # Commands an execute tool ran (hook-captured; single-line, best-effort scrubbed).
    commands = [cmd for cmd in (_text(x) for x in (actions.get("commands") or [])) if cmd]
    # Union the section-recorded files with the paths the Task's machine checks
    # named: either source alone routinely misses files the other has.
    touched: list[str] = []
    seen: set[str] = set()
    for candidate in (*section_files, *_evidence_touched_files(task)):
        path = _text(candidate)
        if path and path not in seen:
            seen.add(path)
            touched.append(path)
    provenance: list[str] = []
    gaps: list[str] = []
    if touched:
        provenance.append(SOURCE_MCP)
    else:
        gaps.append("No touched files were recorded for this Task's steps.")
    if counts or name_counts or commands:
        provenance.append(SOURCE_HOOK)
    else:
        gaps.append(
            "Tool categories were not instrumented for this session; Actions shows touched files only."
        )
    # Compute the capped preview + disclosed overflow ONCE, here, so every surface
    # (CLI, TUI, and the macOS app) renders the daemon-provided slice and never
    # re-derives the cap client-side — the single source of truth for the cap.
    preview, elided = touched_files_preview({"touched_files": touched})
    commands_shown, commands_elided = commands_preview({"commands": commands})
    return {
        "tool_category_counts": dict(counts),
        "tool_category_total": int(actions.get("tool_category_total") or 0),
        "tool_name_counts": dict(name_counts),
        "tool_name_total": int(actions.get("tool_name_total") or 0),
        "tool_names_preview": names_preview,
        "tool_names_elided": names_elided,
        "touched_files": touched,
        "touched_file_count": len(touched),
        "touched_files_preview": preview,
        "touched_files_elided": elided,
        "commands": commands,
        "command_count": len(commands),
        "commands_preview": commands_shown,
        "commands_elided": commands_elided,
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
    # A complete pricing-table estimate is not a gap — the estimate is present
    # and its basis rides cost_basis / cost_confidence. Only genuinely missing
    # or partial cost is a gap.
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
    # The coverage table (recorded / unavailable / not_recorded per dimension)
    # already ships as its own ``coverage`` block. Folding every non-recorded
    # row into gaps duplicated the per-dimension gaps and drowned the real,
    # per-task ones in structural facts (no org control plane, no artifact
    # store) that are true for every single-machine task — so gaps stay
    # per-dimension only. ``coverage`` is unused here now but kept in the
    # signature so callers do not change.
    _ = coverage
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

def _sessions_block(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The Task's constituent sessions, grouped root -> its members, with
    primary/continuation and root/subagent roles.

    A pure reshape of ``task["root_groups"]`` and ``task["sessions"]`` (no new
    data) into the drill-down index the Work surface expands: each member ref is
    a ``{client, client_session_id}`` the app resolves against the existing
    ``/v1/session`` endpoint for that session's steps/checks/descendants.
    """

    primary = _mapping(task.get("primary_root"))
    primary_key = (_text(primary.get("client")), _text(primary.get("client_session_id")))
    # Only roots carry a ``client_session_title``; a subagent's is null, so the
    # drill-down showed a raw session id until you expanded it. Recover a name
    # from the subagent's FIRST recorded step so the row is legible up front.
    step_title_by_session: dict[str, str] = {}
    for item in _items(task):
        session_id = _text(item.get("client_session_id"))
        title = _text(item.get("title") or item.get("objective") or item.get("summary"))
        if session_id and title and session_id not in step_title_by_session:
            step_title_by_session[session_id] = title
    sessions_by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    raw_sessions = task.get("sessions") if isinstance(task.get("sessions"), list) else []
    for session in raw_sessions:
        if not isinstance(session, Mapping):
            continue
        key = (_text(session.get("client")), _text(session.get("client_session_id")))
        if key[0] and key[1]:
            sessions_by_key[key] = session

    groups: list[dict[str, Any]] = []
    raw_groups = task.get("root_groups") if isinstance(task.get("root_groups"), list) else []
    for group in raw_groups:
        if not isinstance(group, Mapping):
            continue
        root = _mapping(group.get("root"))
        root_key = (_text(root.get("client")), _text(root.get("client_session_id")))
        members: list[dict[str, Any]] = []
        member_refs = group.get("session_keys") if isinstance(group.get("session_keys"), list) else []
        for ref in member_refs:
            if not isinstance(ref, Mapping):
                continue
            key = (_text(ref.get("client")), _text(ref.get("client_session_id")))
            if not key[0] or not key[1]:
                continue
            session = sessions_by_key.get(key, {})
            members.append(
                {
                    "client": key[0],
                    "client_session_id": key[1],
                    "session_kind": _text(session.get("session_kind")) or None,
                    "role": "root" if key == root_key else "subagent",
                    "title": (
                        _text(session.get("client_session_title"))
                        or step_title_by_session.get(key[1])
                        or None
                    ),
                    "project": _text(session.get("project")) or None,
                    "last_activity_at": _number(session.get("last_activity_at")) or None,
                }
            )
        groups.append(
            {
                "root": {"client": root_key[0], "client_session_id": root_key[1]},
                "role": "primary" if root_key == primary_key else "continuation",
                "lineage_state": _text(group.get("lineage_state")) or None,
                "supporting_count": int(group.get("supporting_count") or 0),
                "members": members,
            }
        )
    return groups


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
    evidence_strength = _evidence_strength(task, checks, verification)
    canonical = reduce_task_outcome(task, latest_store_activity_at=latest_store_activity_at)
    decision = _decision_status(
        task, latest_store_activity_at=latest_store_activity_at, canonical=canonical
    )
    handoff = _handoff_marker(canonical)
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
            # A third, orthogonal signal: the deliberate-stop lifecycle marker.
            # Kept out of ``decision_status`` on purpose so a handoff can be shown
            # BESIDE a finding/blocked headline instead of being masked by it.
            "handoff": handoff,
            "orthogonality_note": (
                "Evidence coverage and decision status are separate axes: an agent reporting "
                "'done' never adds a passing check, and a human review or approval never "
                "counts as machine verification."
            ),
        },
        "dimensions": dimensions,
        # The Task's constituent sessions, grouped root -> members, so the Work
        # surface can nest each session's /v1/session drill-down under the
        # Receipt. Additive; existing consumers ignore it.
        "sessions": _sessions_block(task),
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
    evidence_strength = _evidence_strength(task, checks, verification)
    canonical = reduce_task_outcome(task, latest_store_activity_at=latest_store_activity_at)
    decision = _decision_status(
        task, latest_store_activity_at=latest_store_activity_at, canonical=canonical
    )
    usage = _mapping(task.get("usage"))
    return {
        "task_id": public_task_id,
        "title": title,
        "decision_status": {
            "key": decision["key"],
            "label": decision["label"],
            "asserted_by": decision["asserted_by"],
        },
        # The recency-aware handoff lifecycle marker for the list row's parallel
        # chip. A flat bool keeps the row compact; the detail Receipt carries the
        # full ``axes.handoff`` object. Rendered as a chip only when it is not
        # already the decision word (see ``_handoff_marker``).
        "handed_off": bool(canonical.get("handoff_current")),
        "evidence_strength": {
            "key": evidence_strength["key"],
            "gradeable": evidence_strength["gradeable"],
            "strongest_tier": evidence_strength["strongest_tier"],
            "checkable_total": evidence_strength["checkable_total"],
            "checked_total": evidence_strength["checked_total"],
            "by_tier": evidence_strength["by_tier"],
            "hidden_in_subagents": evidence_strength["hidden_in_subagents"],
            "unattributed_checks": evidence_strength["unattributed_checks"],
        },
        "cost": {
            "estimated_cost_usd": usage.get("estimated_cost_usd"),
            "cost_basis": _text(usage.get("cost_basis")) or None,
            "cost_complete": bool(usage.get("cost_complete")),
        },
        "session_count": int(task.get("session_count") or 0),
        # The primary root's {client, client_session_id} — the one id a list row
        # carries, so the Work surface can deep-link a session to its Task.
        "primary_root": _mapping(task.get("primary_root")) or None,
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
    "EVIDENCE_TIER_LABEL",
    "build_receipt",
    "build_receipt_summary",
    "evidence_coverage_headline",
    "evidence_coverage_ledger",
    "latest_store_activity",
]
