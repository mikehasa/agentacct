"""Single source of truth for joining imported usage rows to MCP work context.

Canonical attribution (work_ledger.build_attributions), the join inspector
(work_ledger.build_join_inspector), and the advisory context bridge
(context_bridge.build_usage_context_bridge) all match through this module so
the three surfaces can never disagree about a join.

Provenance tiers per id key (client_session_id / client_transcript_id):

    tier          source of the id on the section/context      confidence  strategy prefix
    ----------    -------------------------------------------  ----------  ---------------
    explicit      validated tool argument, server-stamped in    exact       exact_
                  metadata.client_context_keys_authored
    hook          inherited from the Claude Code hook context   high        client_derived_
                  (client-derived, TTL-fresh, not session-bound)
    log_evidenced implied at read time from the client's own    high        client_log_evidenced_
                  session log (creation-tool response pairing,
                  log_evidence.py); client-derived truth but
                  paired at import time, not authored in-session
    attach        inherited from a prior attach on the same     medium      inherited_
                  MCP server (freshness unproven)
    unverified    id present but never server-stamped (legacy   high (cap)  unverified_
                  events, generic record_event, HTTP/CLI paths)

Honesty rules encoded here:
- Missing attribution always beats wrong attribution.
- Inherited ids are never `exact`; ids without verifiable provenance are
  capped at `high` and can never earn `exact`.
- log_evidenced ids are never `exact` either: the client's own log is ground
  truth, but the pairing happens at import time (post-hoc), not as in-session
  authorship — the inherited-never-exact precedent holds. The marker is
  derived fresh on every build and never read from stored metadata, so no
  writer can forge the tier.
- Conflicting evidence vetoes a join: a session-id match with a conflicting
  client_transcript_id (or a transcript match with a conflicting
  client_session_id) is treated as no id-key match at all. A snapshot whose
  agent-claimed ids conflict with its log-evidenced session/transcript/client
  (log_evidence_conflict) vetoes EVERY id-key equality — both the claimed and
  the evidenced id lose join power ("veto both"); the veto token names the key
  that actually conflicted so the reason string is truthful.
- A log-evidenced allocation refuses when the donor session's evidence links
  more than one distinct section (``log_evidence_cohort_size`` > 1): the
  session genuinely spans several sections and its tokens cannot be divided,
  so it refuses as ambiguous even when only one section survived the vetoes.
- run_id and parent/child session relationships are non-allocating grouping
  hints (`low`); they never attach usage to a section.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

JOIN_RANK = {"unjoined": 0, "low": 1, "medium": 2, "high": 3, "exact": 4}

ID_JOIN_KEYS = ("client_transcript_id", "client_session_id")

TIER_CONFIDENCE = {"explicit": "exact", "hook": "high", "log_evidenced": "high", "attach": "medium", "unverified": "high"}
TIER_STRATEGY_PREFIX = {
    "explicit": "exact",
    "hook": "client_derived",
    "log_evidenced": "client_log_evidenced",
    "attach": "inherited",
    "unverified": "unverified",
}


def annotate_usage_source_namespace_ambiguity(
    usage_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Derive raw join keys that collide across agent data homes.

    The annotation is read-time only. It is shared by canonical attribution,
    the join inspector, and the context bridge so no secondary surface can
    resurrect a raw-id join that the canonical ledger refused.
    """

    sources_by_carrier: dict[tuple[str, str, str], set[str | None]] = {}
    for usage in usage_records:
        client = str(usage.get("client") or "").strip()
        if not client:
            continue
        source = str(usage.get("source_namespace_fingerprint") or "").strip()
        source_value = source or None
        for key in ID_JOIN_KEYS:
            value = str(usage.get(key) or "").strip()
            if value:
                sources_by_carrier.setdefault((client, key, value), set()).add(
                    source_value
                )

    annotated: list[dict[str, Any]] = []
    for usage in usage_records:
        record = dict(usage)
        client = str(record.get("client") or "").strip()
        ambiguous = [
            key
            for key in ID_JOIN_KEYS
            if client
            and (value := str(record.get(key) or "").strip())
            and len(sources_by_carrier.get((client, key, value), set())) > 1
        ]
        if ambiguous:
            record["source_namespace_ambiguous_join_keys"] = ambiguous
        else:
            record.pop("source_namespace_ambiguous_join_keys", None)
        annotated.append(record)
    return annotated

PARENT_CHILD_HINT_REASON = (
    "child/parent session relationship groups this usage with the section's session; "
    "agentacct does not allocate section-level usage across sessions"
)
RUN_ID_HINT_REASON = "usage and section share agentacct run_id only; not allocating to a section"
UNJOINED_REASON = "no section shared client_session_id, client_transcript_id, run_id, or parent session"


def _namespace_fingerprint(value: Mapping[str, Any]) -> str:
    return str(
        value.get("namespace_fingerprint")
        or value.get("session_namespace_fingerprint")
        or ""
    ).strip()


def namespace_join_compatible(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    allow_legacy_unscoped: bool = False,
) -> bool:
    """Return whether two session-scoped facts may share an identity.

    A namespace fingerprint is an explicit issuer assertion. Different
    assertions never join. An explicit/missing pair also fails closed unless
    the caller is reading a project-scoped legacy store, where the store
    boundary itself may safely provide the missing scope. Two genuinely
    legacy unscoped facts remain compatible. A row claiming ``explicit``
    scope without its fingerprint is internally incomplete and never earns
    the legacy exception.
    """

    left_fingerprint = _namespace_fingerprint(left)
    right_fingerprint = _namespace_fingerprint(right)
    if left_fingerprint and right_fingerprint:
        return left_fingerprint == right_fingerprint
    if left_fingerprint or right_fingerprint:
        if not allow_legacy_unscoped:
            return False
        missing = right if left_fingerprint else left
        return str(missing.get("identity_scope_state") or "").strip() != "explicit"
    return not (
        str(left.get("identity_scope_state") or "").strip() == "explicit"
        or str(right.get("identity_scope_state") or "").strip() == "explicit"
    )


def _namespace_join_veto(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    allow_legacy_unscoped: bool,
) -> str | None:
    if namespace_join_compatible(
        left,
        right,
        allow_legacy_unscoped=allow_legacy_unscoped,
    ):
        return None
    if _namespace_fingerprint(left) and _namespace_fingerprint(right):
        return "namespace_fingerprint_conflict"
    return "namespace_fingerprint_missing"


def _log_evidenced_source_namespace_veto(
    usage: Mapping[str, Any],
    work: Mapping[str, Any],
) -> str | None:
    """Fail closed when post-hoc log evidence came from another client home."""

    evidenced = str(
        work.get("log_evidenced_source_namespace_fingerprint") or ""
    ).strip()
    if not evidenced:
        return None
    usage_source = str(usage.get("source_namespace_fingerprint") or "").strip()
    if not usage_source:
        return "log_evidenced_source_namespace_missing"
    if usage_source != evidenced:
        return "log_evidenced_source_namespace_conflict"
    return None


def _source_ambiguous_key_veto(
    usage: Mapping[str, Any],
    work: Mapping[str, Any],
    key: str,
) -> str | None:
    """Require client-log source proof for a raw id shared across homes."""

    ambiguous = usage.get("source_namespace_ambiguous_join_keys")
    if not isinstance(ambiguous, (list, tuple, set)) or key not in ambiguous:
        return None
    usage_source = str(usage.get("source_namespace_fingerprint") or "").strip()
    work_source = str(
        work.get("log_evidenced_source_namespace_fingerprint") or ""
    ).strip()
    if usage_source and work_source == usage_source:
        return None
    return f"{key}_source_namespace_ambiguous"


def work_key(client: Any, session_scope: Any, section_id: str) -> str:
    """Composite work-item identity: client::session::section_id.

    Sections that share a raw section_id but come from different clients or
    sessions must never merge into one work item; the raw section_id stays
    available separately for display and legacy lookups.
    """

    client_scope = str(client) if client else "unknown-client"
    session = str(session_scope) if session_scope else "-"
    return f"{client_scope}::{session}::{section_id}"


def id_key_tier(work: dict[str, Any], key: str) -> str:
    """Provenance tier of an id key on a work event snapshot, work item, or context event.

    Work items carry per-key inheritance sources (inherited_key_sources); raw
    event snapshots fall back to the event-level client_context_source.
    """

    inherited = set(work.get("inherited_join_keys") or [])
    if key in inherited:
        sources = work.get("inherited_key_sources")
        if isinstance(sources, dict) and key in sources:
            source = sources.get(key)
        else:
            source = work.get("client_context_source")
        return "hook" if source == "claude_code_hook" else "attach"
    authored = set(work.get("authored_join_keys") or [])
    if key in authored:
        return "explicit"
    # log_evidenced comes AFTER inherited/authored (the applier only implants
    # a key the snapshot did not have, so a truthful collision is impossible
    # and hook/explicit provenance can never be displaced) and BEFORE
    # unverified. The marker exists only on derived snapshots
    # (log_evidence.apply_log_evidence_to_snapshots); stored event metadata
    # can never populate it.
    log_evidenced = set(work.get("log_evidenced_join_keys") or [])
    if key in log_evidenced:
        return "log_evidenced"
    return "unverified"


# One veto token per key that can conflict with the client-log evidence. The
# token drives the id-free veto reason string; every one triggers the same
# "veto both" behaviour (every id-key equality against the snapshot is dropped).
_LOG_EVIDENCE_CONFLICT_VETOES = {
    "client_session_id": "log_evidenced_session_id_conflict",
    "client_transcript_id": "log_evidenced_transcript_id_conflict",
    "client": "log_evidenced_client_conflict",
}


def _log_evidence_conflict_veto(conflict: Any) -> str | None:
    """Veto token naming the key that conflicts with the log evidence, or None.

    A conflict marker records which key(s) actually disagreed
    (``conflicting_keys``). Session conflicts take precedence for the reason
    string (the most severe), then transcript, then client. Absent/empty means
    no log-evidence conflict on this snapshot.
    """

    if not conflict:
        return None
    if isinstance(conflict, dict):
        keys = conflict.get("conflicting_keys")
        if isinstance(keys, (list, tuple)) and keys:
            for key in ("client_session_id", "client_transcript_id", "client"):
                if key in keys:
                    return _LOG_EVIDENCE_CONFLICT_VETOES[key]
    # Truthy but shapeless (legacy/forged): fall back to the session wording.
    return _LOG_EVIDENCE_CONFLICT_VETOES["client_session_id"]


def _log_evidence_cohort_size(work: dict[str, Any]) -> int:
    """Distinct sections the snapshot's donor session links (log_evidence.py).

    Stamped only on log-evidenced snapshots whose donor session spans more than
    one section; absent/invalid means a single-section donor (cohort 1). The
    marker is derived fresh by the applier and never read from stored metadata.
    """

    value = work.get("log_evidence_cohort_size")
    try:
        size = int(value)
    except (TypeError, ValueError):
        return 1
    return size if size > 1 else 1


def _suffixed_join_key(key: str, tier: str) -> str:
    if tier == "explicit":
        return key
    if tier == "hook":
        return f"{key}_client_derived"
    if tier == "log_evidenced":
        return f"{key}_log_evidenced"
    if tier == "attach":
        return f"{key}_inherited"
    return f"{key}_unverified"


def pair_match(
    usage: dict[str, Any],
    work: dict[str, Any],
    *,
    allow_legacy_unscoped_namespace: bool = False,
) -> dict[str, Any] | None:
    """Match one usage row against one section/context candidate.

    Returns None when nothing links the pair, otherwise a PairMatch dict:
    {"join_keys": [...], "id_tiers": {key: tier}, "vetoes": [...]}.

    Vetoes (1c): an id-key equality is discarded when the OTHER id key is
    present on both sides with different values — conflicting evidence never
    earns a join. Grouping hints (run_id, parent/child) are kept: they never
    allocate, and parent/child pairs inherently have differing session ids.
    """

    if usage.get("client") and work.get("client") and usage.get("client") != work.get("client"):
        return None
    namespace_veto = _namespace_join_veto(
        usage,
        work,
        allow_legacy_unscoped=allow_legacy_unscoped_namespace,
    )
    source_namespace_veto = _log_evidenced_source_namespace_veto(usage, work)
    conflicts = {
        key: bool(usage.get(key) and work.get(key) and usage.get(key) != work.get(key))
        for key in ID_JOIN_KEYS
    }
    # Log-evidence conflict ("veto both"): the snapshot's agent-claimed ids
    # disagreed with the session evidenced by the client's own log
    # (log_evidence.py moved the conflicting claim to claimed_* and regrouped
    # the snapshot under the evidenced session for display). EVERY id-key
    # equality against such a snapshot is vetoed — the evidenced key never
    # allocates from a conflicted snapshot, and the fabricated claim already
    # lost its live key structurally. Grouping hints survive, as with the
    # existing vetoes. The veto token names the key that actually conflicted
    # (session / transcript / client) so the reason string reads correctly.
    log_evidence_conflict_veto = _log_evidence_conflict_veto(work.get("log_evidence_conflict"))
    join_keys: list[str] = []
    id_tiers: dict[str, str] = {}
    vetoes: list[str] = []
    for key, other in (("client_session_id", "client_transcript_id"), ("client_transcript_id", "client_session_id")):
        if usage.get(key) and usage.get(key) == work.get(key):
            if log_evidence_conflict_veto is not None:
                vetoes.append(log_evidence_conflict_veto)
                continue
            source_ambiguous_veto = _source_ambiguous_key_veto(
                usage,
                work,
                key,
            )
            if source_ambiguous_veto is not None:
                vetoes.append(source_ambiguous_veto)
                continue
            if conflicts[other]:
                vetoes.append(f"{other}_conflict")
                continue
            tier = id_key_tier(work, key)
            id_tiers[key] = tier
            join_keys.append(_suffixed_join_key(key, tier))
    if usage.get("run_id") and usage.get("run_id") == work.get("run_id"):
        join_keys.append("run_id")
    if usage.get("parent_client_session_id") and usage.get("parent_client_session_id") == work.get("client_session_id"):
        join_keys.append("parent_client_session_id")
    if usage.get("client_session_id") and usage.get("client_session_id") == work.get("parent_client_session_id"):
        if "parent_client_session_id" not in join_keys:
            join_keys.append("parent_client_session_id")
    if namespace_veto is not None and join_keys:
        # A shared raw session/run key is not enough when the issuer scopes
        # disagree. Keep the veto observable, but remove every allocating and
        # grouping key so downstream surfaces cannot accidentally coalesce
        # the pair.
        vetoes.append(namespace_veto)
        join_keys = []
        id_tiers = {}
    if source_namespace_veto is not None and join_keys:
        vetoes.append(source_namespace_veto)
        join_keys = []
        id_tiers = {}
    if not join_keys and not vetoes:
        return None
    return {"join_keys": join_keys, "id_tiers": id_tiers, "vetoes": sorted(set(vetoes))}


# The id keys pair_match() joins on. A usage row U and a work candidate W can
# only produce a non-None pair_match — a join key OR a veto — when at least one
# of these equalities holds:
#     U.client_session_id        == W.client_session_id
#     U.client_transcript_id     == W.client_transcript_id
#     U.run_id                   == W.run_id
#     U.parent_client_session_id == W.client_session_id
#     U.client_session_id        == W.parent_client_session_id
# Every veto pair_match can emit is gated behind one of these equalities (the
# session/transcript equality branches, or namespace/source vetoes that only
# fire once join_keys is non-empty), so a pair sharing NONE of them contributes
# nothing to attribution — pair_match returns None. Indexing candidates by these
# fields lets the attribution loops pair_match only the ~1% of candidates that
# could possibly match, with byte-identical results.
#
# Each entry is (key_read_on_the_query_row, field_the_index_is_keyed_on). The
# relation is structurally symmetric, so the SAME table drives both directions
# (usage->work candidates and work->usage candidates). Keep it in lockstep with
# pair_match().
_JOIN_INDEX_LOOKUPS: tuple[tuple[str, str], ...] = (
    ("client_session_id", "client_session_id"),
    ("parent_client_session_id", "client_session_id"),
    ("client_transcript_id", "client_transcript_id"),
    ("run_id", "run_id"),
    ("client_session_id", "parent_client_session_id"),
)
_JOIN_INDEX_FIELDS: tuple[str, ...] = (
    "client_session_id",
    "client_transcript_id",
    "run_id",
    "parent_client_session_id",
)


class JoinCandidateIndex:
    """Index a candidate list by the id keys pair_match() joins on.

    Lets the two O(usage x work) attribution loops pair_match only the
    candidates that could possibly match a given row, instead of scanning the
    whole list (the dominant cost of build_work_ledger). Provably lossless: a
    candidate sharing none of _JOIN_INDEX_LOOKUPS' keys returns None from
    pair_match (no join key, no veto), so skipping it cannot change any
    decision. ``matches`` returns the subset in the candidates' ORIGINAL order,
    so the caller sees the identical match/veto/candidate sequence a full scan
    would produce.
    """

    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        self._by: dict[str, dict[Any, list[tuple[int, dict[str, Any]]]]] = {
            field: {} for field in _JOIN_INDEX_FIELDS
        }
        for position, candidate in enumerate(candidates):
            for field in _JOIN_INDEX_FIELDS:
                value = candidate.get(field)
                if value:
                    self._by[field].setdefault(value, []).append((position, candidate))

    def matches(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        gathered: dict[int, dict[str, Any]] = {}
        for row_key, index_field in _JOIN_INDEX_LOOKUPS:
            value = row.get(row_key)
            if not value:
                continue
            for position, candidate in self._by[index_field].get(value, ()):
                gathered[position] = candidate
        return [candidate for _position, candidate in sorted(gathered.items())]


def pair_confidence(match: dict[str, Any]) -> str:
    """Display confidence for a single pair match (used by the join inspector)."""

    best = "unjoined"
    for tier in (match.get("id_tiers") or {}).values():
        confidence = TIER_CONFIDENCE[tier]
        if JOIN_RANK[confidence] > JOIN_RANK[best]:
            best = confidence
    if best != "unjoined":
        return best
    if match.get("join_keys"):
        return "low"
    return "unjoined"


def _allocation_reason(key: str, tier: str) -> str:
    if tier == "explicit":
        return f"usage and section share the only matching {key}"
    if tier == "hook":
        return (
            f"usage and section share the only matching {key} captured from the Claude Code hook; "
            "client-derived but not session-bound, so not exact"
        )
    if tier == "log_evidenced":
        return (
            f"usage and section share the only matching {key}; the section was created by a tool call recorded "
            "in this client session's own log (client-log evidenced) — client-derived but paired at import time, "
            "so not exact"
        )
    if tier == "attach":
        return (
            f"usage and section share the only matching {key}, but the section inherited it from attach context; "
            "freshness is unproven"
        )
    return (
        f"usage and section share the only matching {key}, but the id's provenance is unverified "
        "(recorded before provenance stamping or via a generic recording path); not eligible for exact"
    )


def _veto_reason(vetoes: list[str]) -> str:
    parts: list[str] = []
    for veto in sorted(set(vetoes)):
        if veto == "namespace_fingerprint_conflict":
            parts.append("usage and section assert different session namespaces")
            continue
        if veto == "namespace_fingerprint_missing":
            parts.append("one side asserts a session namespace while the other side lacks one")
            continue
        if veto == "log_evidenced_source_namespace_conflict":
            parts.append(
                "usage and client-log evidence came from different agent data homes"
            )
            continue
        if veto == "log_evidenced_source_namespace_missing":
            parts.append(
                "client-log evidence asserts an agent data home but usage lacks one"
            )
            continue
        if veto.endswith("_source_namespace_ambiguous"):
            key = veto.removesuffix("_source_namespace_ambiguous")
            parts.append(
                f"the raw {key} exists in multiple agent data homes without matching client-log source evidence"
            )
            continue
        # Log-evidence conflicts (id-free by design — reason strings render in
        # HTML title attrs): name the key that actually conflicted with the
        # client's own log so the message is truthful for each case.
        if veto == "log_evidenced_session_id_conflict":
            parts.append(
                "the section's agent-claimed session id conflicts with the session evidenced by the client's own log"
            )
            continue
        if veto == "log_evidenced_transcript_id_conflict":
            parts.append(
                "the section's agent-claimed transcript id conflicts with the transcript evidenced by the client's own log"
            )
            continue
        if veto == "log_evidenced_client_conflict":
            parts.append(
                "the section's agent-claimed client conflicts with the client evidenced by its own log"
            )
            continue
        conflict_key = veto.removesuffix("_conflict")
        shared_key = "client_session_id" if conflict_key == "client_transcript_id" else "client_transcript_id"
        parts.append(f"a section shares this usage's {shared_key} but carries a conflicting {conflict_key}")
    return "; ".join(parts) + "; conflicting evidence vetoes the join"


def decide_attribution(
    usage: dict[str, Any],
    work_candidates: list[dict[str, Any]],
    *,
    allow_legacy_unscoped_namespace: bool = False,
) -> dict[str, Any]:
    """Decide the canonical attribution for one usage row.

    Returns a JoinDecision dict:
    {"work": dict|None, "strategy": str, "confidence": str, "reason": str,
     "candidate_work_ids": list[str], "vetoes": list[str]}.

    Ordered strategies (the only ordering anywhere): transcript-id equality,
    session-id equality, run_id grouping hint, parent/child grouping hint.
    Only the id-equality strategies may allocate, and only when exactly one
    SECTION survives the vetoes with any id-key match at all.

    The ambiguity guard is section-scoped, not key-scoped: a co-session
    section that matched this usage row on client_session_id blocks an
    allocation via client_transcript_id just as hard as a second transcript
    match would. Deciding per key would let a section that merely OMITS the
    transcript id become invisible to the guard, silently handing the whole
    row to whichever section happens to carry the transcript — wrong
    attribution, which always loses to missing attribution. Conflict-vetoed
    sections (empty id_tiers) are correctly NOT candidates: conflicting
    evidence already ruled them out.
    """

    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    vetoes: list[str] = []
    for work in work_candidates:
        match = pair_match(
            usage,
            work,
            allow_legacy_unscoped_namespace=allow_legacy_unscoped_namespace,
        )
        if match is None:
            continue
        vetoes.extend(match["vetoes"])
        if match["join_keys"]:
            matches.append((work, match))

    id_matches = [(work, match) for work, match in matches if match["id_tiers"]]
    if len(id_matches) == 1:
        work, match = id_matches[0]
        # Strongest key on the single surviving section: transcript preferred
        # over session; per-key tier semantics are unchanged.
        key = ID_JOIN_KEYS[0] if ID_JOIN_KEYS[0] in match["id_tiers"] else ID_JOIN_KEYS[1]
        tier = match["id_tiers"][key]
        # Donor-cohort guard (log_evidence honesty): a log-evidenced key is
        # implied from the donor session's own client log. When that donor
        # session's evidence links MORE THAN ONE distinct section, the session
        # genuinely spans several sections and its tokens cannot be divided —
        # so refuse the allocation as ambiguous EVEN THOUGH this is the only
        # section that survived the id-conflict vetoes (the sibling sections
        # were conflict-vetoed or are separate work items). This is the mirror
        # of the multi-section ambiguity guard below, reaching the case where
        # the veto already removed the other candidates. The implied key still
        # groups the section under its session for display.
        cohort_size = _log_evidence_cohort_size(work) if tier == "log_evidenced" else 1
        if cohort_size > 1:
            candidate_work_ids = [str(work.get("work_id") or "")]
            if str(usage.get("client") or "") == "codex":
                reason = (
                    f"this session recorded {cohort_size} sections (client-log evidenced); "
                    "session token totals are not divided among sections"
                )
            else:
                reason = (
                    f"this session's log evidence links {cohort_size} sections; not allocating to a section"
                )
            return {
                "work": None,
                "strategy": f"client_log_evidenced_{key}_ambiguous_sections",
                "confidence": "medium",
                "reason": reason,
                "candidate_work_ids": candidate_work_ids,
                "vetoes": sorted(set(vetoes)),
            }
        return {
            "work": work,
            "strategy": f"{TIER_STRATEGY_PREFIX[tier]}_{key}",
            "confidence": TIER_CONFIDENCE[tier],
            "reason": _allocation_reason(key, tier),
            "candidate_work_ids": [str(work.get("work_id") or "")],
            "vetoes": sorted(set(vetoes)),
        }
    if len(id_matches) > 1:
        candidate_work_ids = [str(work.get("work_id") or "") for work, _ in id_matches]
        transcript_matched = sum(1 for _, match in id_matches if "client_transcript_id" in match["id_tiers"])
        key = "client_transcript_id" if transcript_matched >= 2 else "client_session_id"
        # Reason strings render in HTML (chip title attrs, reconciliation
        # notes) and must stay id-free: counts + key name only. The full
        # candidate work ids travel in candidate_work_ids, a JSON-only
        # surface. Copy-only codex variant (matcher unchanged): a codex
        # session whose sections are ALL client-log evidenced shows sections
        # and session totals together on the session row, so the refusal must
        # read as the by-design rule, not as breakage.
        matched_tiers = {tier for _, match in id_matches for tier in match["id_tiers"].values()}
        if str(usage.get("client") or "") == "codex" and matched_tiers == {"log_evidenced"}:
            reason = (
                f"this session recorded {len(candidate_work_ids)} sections (client-log evidenced); "
                "session token totals are not divided among sections"
            )
        else:
            reason = f"usage shares {key} with {len(candidate_work_ids)} sections; not allocating to a section"
        return {
            "work": None,
            "strategy": f"exact_{key}_ambiguous_sections",
            "confidence": "medium",
            "reason": reason,
            "candidate_work_ids": candidate_work_ids,
            "vetoes": sorted(set(vetoes)),
        }

    if any("run_id" in match["join_keys"] for _, match in matches):
        return {
            "work": None,
            "strategy": "exact_run_id_grouping_hint",
            "confidence": "low",
            "reason": RUN_ID_HINT_REASON,
            "candidate_work_ids": [],
            "vetoes": sorted(set(vetoes)),
        }

    if any("parent_client_session_id" in match["join_keys"] for _, match in matches):
        return {
            "work": None,
            "strategy": "parent_child_context_hint",
            "confidence": "low",
            "reason": PARENT_CHILD_HINT_REASON,
            "candidate_work_ids": [],
            "vetoes": sorted(set(vetoes)),
        }

    reason = _veto_reason(vetoes) if vetoes else UNJOINED_REASON
    return {
        "work": None,
        "strategy": "unjoined",
        "confidence": "unjoined",
        "reason": reason,
        "candidate_work_ids": [],
        "vetoes": sorted(set(vetoes)),
    }
