"""Client-log-evidenced context linking (single source of truth).

At usage-import time agentacct can pair each agentacct-recorded MCP event with
the client session log that created it: the client's own transcript contains
the creation tool call AND the server's response carrying the new event_id.
That pairing is client-derived truth (the log is usage truth), but it is
post-hoc — computed at import time, not authored in-session — so it feeds the
unified matcher as its own provenance tier ``log_evidenced`` (high, never
``exact``; see join_rules).

Hard rules encoded here, in honesty order:

- **Allowlist first, shape second.** Event ids are extracted ONLY from the
  five creation tools' outputs, paired by call id. Read tools
  (list/summary/runs/report) echo OTHER sessions' event ids and are never
  donors; the shape check alone must also reject read-shaped payloads.
- **Derived-only markers.** Evidence links are computed fresh from trusted
  usage-import rows on every read. ``log_evidenced_*`` / ``log_evidence_*``
  keys are never read from stored event metadata, so MCP/HTTP writers cannot
  forge a link (the donor side is additionally gated by the usage-truth trust
  markers that ``strip_untrusted_usage_truth_metadata`` removes at write time).
- **Unproven Codex descendant replay is not a donor.** A forked rollout may
  replay its parent's old MCP responses. Child/internal donors require both a
  canonical event time and a trusted session start, with a five-second skew
  allowance; missing/conflicting time is excluded for descendants, and
  impossible ordering is excluded for every Codex donor instead of guessed.
- **Source namespaces fail closed.** The same evidenced event id observed by
  donor rows from different (or explicit-vs-missing) client-home namespaces
  links to neither. Canonical event-id collisions across semantic namespaces
  likewise refuse every donor while preserving raw evidence.
- **Conflicts veto both ways; ambiguity refuses.** A snapshot whose own
  claimed ids disagree with the evidenced session joins nothing; an event
  evidenced by more than one session links to none; refusals are counted,
  never silent.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from typing import Any, Callable, Hashable

from .join_rules import namespace_join_compatible
from .usage_truth import (
    is_local_usage_import_event,
    local_session_observation_event_key,
    normalized_local_usage_session_id,
    reduce_local_session_observation_events,
    split_shadowed_legacy_usage_events,
)

# The ONLY tools whose responses may donate an evidenced event id. Every one
# of them returns exactly one created event under a top-level "event" key
# (mcp.py); read tools (sentinel_list_events, sentinel_get_event_summary,
# sentinel_list_runs, sentinel_get_report, ...) echo foreign ids and must
# never appear here.
# frozen: the sentinel_* tool names survive the Agent Chronicle rename —
# historical stores/logs/files carry them forever, and three shipped
# instruction surfaces plus real user CLAUDE.md/AGENTS.md files quote them.
SENTINEL_CREATION_TOOLS = frozenset(
    {
        "sentinel_record_event",
        "sentinel_attach_client_context",
        "sentinel_record_section",
        "sentinel_record_agent_usage_debug",
        "sentinel_record_machine_check",
    }
)

# \Z (not $) so a trailing newline is rejected: 'evt_deadbeef\n' must NOT
# pass at any of the .match() call sites ($ matches before a final newline).
EVIDENCED_EVENT_ID_RE = re.compile(r"^evt_[0-9a-f]{6,64}\Z")

# Real max observed in one session: 157 distinct created ids. The cap bounds
# hostile/huge sessions; the true total is always preserved alongside so
# overflow is visible, never silent.
EVIDENCED_EVENT_IDS_CAP = 200

# Codex fork rollouts can begin by replaying the parent's transcript, including
# old MCP creation responses.  A small clock-skew allowance keeps near-boundary
# facts joinable while still rejecting the impossible case: an event recorded
# well before the alleged donor session existed.
CODEX_REPLAY_CLOCK_SKEW_SECONDS = 5.0
CODEX_REPLAY_REJECTION_REASON = "codex_event_before_session_start"
CODEX_REPLAY_TIME_UNPROVEN_REASON = "codex_replay_timestamps_unproven"
SOURCE_NAMESPACE_CONFLICT_REASON = "source_namespace_conflict"
CROSS_KIND_DONOR_IDENTITY_CONFLICT_REASON = "cross_kind_donor_identity_conflict"
CANONICAL_EVENT_IDENTITY_CONFLICT_REASON = "canonical_event_identity_conflict"

# The documented MCP registration names (every current setup path writes
# "agentacct"). Claude Code transcripts carry the registration key verbatim in
# `mcp__<server>__<tool>`; Codex normalizes the config key to underscores in the
# rollout `namespace` field (`mcp__agent_chronicle` / `mcp__agent_sentinel`; the
# hyphen-free `agentacct` normalizes to itself). A custom registration name
# loses evidence links (skipped-but-counted, never guessed) — documented in
# INSTALL.md.
#
# ALL THREE names are accepted FOREVER: `agentacct` is what new installs now
# register, and the pre-rename `agent-chronicle` / `agent-sentinel` keys stay
# accepted because historical client logs carry them forever and pre-rename
# registrations keep producing them in new sessions — dropping an old key would
# silently unlink every pre-rename session on the next import.
SENTINEL_SERVER_KEY = "agentacct"  # the name new installs register
ACCEPTED_SERVER_KEYS = frozenset({"agentacct", "agent-chronicle", "agent-sentinel"})
_CODEX_ACCEPTED_NAMESPACES = frozenset(
    {"agentacct", "agent-chronicle", "agent_chronicle", "agent-sentinel", "agent_sentinel"}
)
_CLAUDE_TOOL_PREFIX = "mcp__"


def extract_created_event_id(text: str) -> str | None:
    """Strict single-created-event shape check shared by every wire adapter.

    Accepts exactly the creation-tool payload shape: a JSON dict with a
    top-level ``"event"`` dict whose ``event_id`` is a well-formed evt id.
    Rejects (belt-and-braces — the tool-name allowlist already excludes read
    tools): list-shaped payloads carrying ``"events"``/``"runs"`` lists,
    error texts, truncated/torn JSON, outcome-only machine_check payloads,
    and any non-dict shape.
    """

    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    if any(isinstance(payload.get(key), list) for key in ("events", "runs")):
        return None
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not EVIDENCED_EVENT_ID_RE.match(event_id):
        return None
    return event_id


def codex_namespace_matches_sentinel(namespace: Any) -> bool:
    """True when a codex function_call namespace identifies the sentinel server.

    Absent namespace is accepted (older codex versions may omit it; the bare
    tool-name allowlist still gates). A present namespace must be exactly one
    of the pinned server keys (new or pre-rename) modulo codex's underscore
    normalization and the optional ``mcp__`` prefix — substring matches
    (``mcp__not_agent_chronicle_fake``) are rejected.
    """

    if namespace is None:
        return True
    if not isinstance(namespace, str):
        return False
    remainder = namespace.removeprefix(_CLAUDE_TOOL_PREFIX)
    return remainder in _CODEX_ACCEPTED_NAMESPACES


def classify_codex_function_call(name: Any, namespace: Any) -> str | None:
    """"accepted" / "rejected" / None for a codex rollout function_call.

    "rejected" means the bare tool name is a creation tool but the namespace
    does not identify the sentinel server — its output is skipped-but-counted
    so custom registration names and wire drift surface in the counters.
    """

    if not isinstance(name, str) or name not in SENTINEL_CREATION_TOOLS:
        return None
    return "accepted" if codex_namespace_matches_sentinel(namespace) else "rejected"


def classify_claude_tool_use(name: Any) -> str | None:
    """"accepted" / "rejected" / None for a Claude Code tool_use block name.

    Accepted only for ``mcp__agent-chronicle__<creation tool>`` or the
    pre-rename ``mcp__agent-sentinel__<creation tool>`` exactly; a
    creation-tool name under any other server segment is rejected
    (skipped-but-counted). Non-MCP and non-creation names return None.
    """

    if not isinstance(name, str) or not name.startswith(_CLAUDE_TOOL_PREFIX):
        return None
    server, separator, tool = name.removeprefix(_CLAUDE_TOOL_PREFIX).partition("__")
    if not separator or tool not in SENTINEL_CREATION_TOOLS:
        return None
    return "accepted" if server in ACCEPTED_SERVER_KEYS else "rejected"


def classify_codex_mcp_invocation(server: Any, tool: Any) -> str | None:
    """"accepted" / "rejected" / None for a codex mcp_tool_call_end invocation.

    Codex 0.144.0-alpha.4 dropped the duplicate ``function_call`` records for
    MCP tools; the ``mcp_tool_call_end`` event_msg's ``invocation`` block
    (``server``/``tool``) is now the only channel. Same allowlist-first rule
    as :func:`classify_codex_function_call`, with ONE deliberate difference:
    the ``server`` field is always present in this shape, so a None/absent
    server is REJECTED (skipped-but-counted — absence is drift), never
    accepted the way an absent function_call namespace is.
    """

    if not isinstance(tool, str) or tool not in SENTINEL_CREATION_TOOLS:
        return None
    if server is None:
        return "rejected"
    return "accepted" if codex_namespace_matches_sentinel(server) else "rejected"


def unwrap_codex_mcp_result(result: Any) -> str | None:
    """Codex mcp_tool_call_end ``result`` field -> the MCP text-block string.

    Observed wire shape: ``{"Ok": {"content": [{"type": "text", "text":
    "<server JSON payload>"}]}}``. ``Err`` results and any other shape return
    None (skipped-but-counted upstream — a failed tool call created nothing).
    """

    if not isinstance(result, dict):
        return None
    ok = result.get("Ok")
    if not isinstance(ok, dict):
        return None
    content = ok.get("content")
    if not isinstance(content, list):
        return None
    return _first_text_block(content)


def unwrap_codex_output_text(output: Any) -> str | None:
    """Codex function_call_output ``output`` field -> the MCP text-block string.

    Observed wire shape: a string holding a human wrapper (``Wall time: ...\n
    Output:\n``) followed by a JSON array of MCP content blocks whose single
    text block carries the server's JSON payload. Bare-JSON strings and dict
    outputs with a ``content`` block list (future codex shapes) are also
    handled. Returns None for anything else (skipped-but-counted upstream).
    """

    if isinstance(output, dict):
        content = output.get("content")
        if isinstance(content, list):
            return _first_text_block(content)
        return None
    if not isinstance(output, str):
        return None
    candidates = [output]
    marker = "Output:\n"
    if marker in output:
        candidates.append(output.split(marker, 1)[1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, list):
            text = _first_text_block(parsed)
            if text is not None:
                return text
            continue
        if isinstance(parsed, dict):
            # Tolerate a direct payload (no content-block wrapper).
            return candidate
    return None


def _first_text_block(blocks: list[Any]) -> str | None:
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            return block["text"]
    return None


class LogEvidenceAccumulator:
    """Ordered, deduped, capped event-id accumulation with honest counters."""

    def __init__(self, *, cap: int = EVIDENCED_EVENT_IDS_CAP) -> None:
        self._cap = cap
        self.evidenced_event_ids: list[str] = []
        self._seen: set[str] = set()
        self.evidenced_outputs_skipped = 0

    def add_output_text(self, text: str | None) -> None:
        event_id = extract_created_event_id(text) if isinstance(text, str) else None
        if event_id is None:
            self.evidenced_outputs_skipped += 1
            return
        if event_id in self._seen:
            return
        self._seen.add(event_id)
        if len(self.evidenced_event_ids) < self._cap:
            self.evidenced_event_ids.append(event_id)

    def record_skip(self) -> None:
        self.evidenced_outputs_skipped += 1

    @property
    def evidenced_event_id_total(self) -> int:
        return len(self._seen)

    @property
    def has_evidence(self) -> bool:
        return bool(self._seen) or self.evidenced_outputs_skipped > 0

    def as_usage_fields(self) -> dict[str, Any]:
        return {
            "evidenced_event_ids": list(self.evidenced_event_ids),
            "evidenced_event_id_total": self.evidenced_event_id_total,
            "evidenced_outputs_skipped": self.evidenced_outputs_skipped,
        }


# ---------------------------------------------------------------------------
# Read-time index + applier (derived-only; unforgeable by MCP/HTTP writers)
# ---------------------------------------------------------------------------


class LogEvidenceIndex(dict[str, list[dict[str, Any]]]):
    """Evidence donor map plus privacy-safe build diagnostics.

    This remains a normal ``dict`` for existing consumers.  Diagnostics are
    attached out-of-band so rejected replay evidence never becomes a fake
    event id in the index, while callers that expose health/status can report
    the refusal instead of making it silently disappear.
    """

    def __init__(self) -> None:
        super().__init__()
        self.diagnostics: dict[str, Any] = {
            "rejected_donor_links": 0,
            "rejected_usage_rows": 0,
            "rejection_reasons": {},
            "clock_skew_tolerance_seconds": CODEX_REPLAY_CLOCK_SKEW_SECONDS,
        }


def build_log_evidence_index(events: list[dict[str, Any]]) -> LogEvidenceIndex:
    """event_id -> ordered unique donors [{client, client_session_id, client_transcript_id, usage_event_id}].

    Donor rows are ONLY server-trust-marked local usage import rows (after the
    shared shadowed-legacy-row exclusion); ``strip_untrusted_usage_truth_metadata``
    removes the trust markers from every MCP/HTTP write, so agent-recorded
    events can never mint a donor. Ids are re-validated and defensively
    re-capped; donors are deduped by (client, base session id) so per-model
    lane rows and re-imported rows of one session count as ONE donor.
    """

    kept_events, _shadowed = split_shadowed_legacy_usage_events(events)
    index = LogEvidenceIndex()
    canonical_created_at, canonical_identity_conflicts = _canonical_event_identity(events)
    pending: dict[str, list[dict[str, Any]]] = {}
    rejected_links: set[tuple[str, str, str, str, str]] = set()
    rejected_usage_rows: set[tuple[str, str, str, str]] = set()
    rejected_observation_rows: set[tuple[str, str, str, str]] = set()
    rejection_reasons: Counter[str] = Counter()

    def reject(raw_id: str, donor: dict[str, Any], reason: str) -> None:
        key = (
            reason,
            raw_id,
            str(donor.get("client") or ""),
            str(donor.get("client_session_id") or ""),
            str(donor.get("source_namespace_fingerprint") or ""),
        )
        if key not in rejected_links:
            rejected_links.add(key)
            rejection_reasons[reason] += 1
        rejected_rows = (
            rejected_usage_rows
            if donor.get("donor_kind") == "usage"
            else rejected_observation_rows
        )
        rejected_rows.add(
            (
                str(donor.get("donor_event_id") or donor.get("usage_event_id") or ""),
                str(donor.get("client") or ""),
                str(donor.get("client_session_id") or ""),
                str(donor.get("source_namespace_fingerprint") or ""),
            )
        )

    for event in kept_events:
        if not is_local_usage_import_event(event):
            continue
        metadata = event.get("metadata")
        if not isinstance(metadata, dict):
            continue
        ids = metadata.get("evidenced_event_ids")
        if not isinstance(ids, list) or not ids:
            continue
        client = _optional_str(metadata.get("client"))
        raw_session = _optional_str(metadata.get("client_session_id"))
        if client is None or raw_session is None:
            continue
        session = normalized_local_usage_session_id(client, raw_session)
        transcript = _optional_str(metadata.get("client_transcript_id"))
        usage_event_id = _optional_str(event.get("event_id")) or ""
        session_started_at = _safe_positive_timestamp(metadata.get("started_at"))
        session_kind = str(metadata.get("client_session_kind") or "root").strip().lower()
        parent_session = _optional_str(metadata.get("parent_client_session_id"))
        source_namespace = _optional_str(metadata.get("source_namespace_fingerprint"))
        codex_descendant = client == "codex" and (
            session_kind in {"child", "internal"} or parent_session is not None
        )
        donor = {
            "client": client,
            "client_session_id": session,
            "client_transcript_id": transcript,
            "usage_event_id": usage_event_id,
            "donor_event_id": usage_event_id,
            "donor_kind": "usage",
            "source_namespace_fingerprint": source_namespace,
        }
        accepted = 0
        for raw_id in ids:
            if accepted >= EVIDENCED_EVENT_IDS_CAP:
                break
            if not isinstance(raw_id, str) or not EVIDENCED_EVENT_ID_RE.match(raw_id):
                continue
            accepted += 1
            if raw_id in canonical_identity_conflicts:
                reject(raw_id, donor, CANONICAL_EVENT_IDENTITY_CONFLICT_REASON)
                continue
            event_created_at = canonical_created_at.get(raw_id)
            if client == "codex":
                if codex_descendant and (event_created_at is None or session_started_at is None):
                    reject(raw_id, donor, CODEX_REPLAY_TIME_UNPROVEN_REASON)
                    continue
                if (
                    event_created_at is not None
                    and session_started_at is not None
                    and event_created_at + CODEX_REPLAY_CLOCK_SKEW_SECONDS < session_started_at
                ):
                    reject(raw_id, donor, CODEX_REPLAY_REJECTION_REASON)
                    continue
            pending.setdefault(raw_id, []).append(donor)

    selected_observations, _observation_diagnostics = reduce_local_session_observation_events(events)
    for event in selected_observations:
        metadata = event.get("metadata")
        observation_key = local_session_observation_event_key(event)
        if not isinstance(metadata, dict) or observation_key is None:
            continue
        ids = metadata.get("evidenced_event_ids")
        if not isinstance(ids, list) or not ids:
            continue
        client, raw_session = observation_key
        session = normalized_local_usage_session_id(client, raw_session)
        transcript = _optional_str(metadata.get("client_transcript_id"))
        observation_event_id = _optional_str(event.get("event_id")) or ""
        session_started_at = _safe_positive_timestamp(metadata.get("started_at"))
        session_kind = str(metadata.get("client_session_kind") or "root").strip().lower()
        parent_session = _optional_str(metadata.get("parent_client_session_id"))
        source_namespace = _optional_str(metadata.get("source_namespace_fingerprint"))
        codex_descendant = client == "codex" and (
            session_kind in {"child", "internal"} or parent_session is not None
        )
        donor = {
            "client": client,
            "client_session_id": session,
            "client_transcript_id": transcript,
            "donor_event_id": observation_event_id,
            "donor_kind": "session_observation",
            "source_namespace_fingerprint": source_namespace,
        }
        accepted = 0
        for raw_id in ids:
            if accepted >= EVIDENCED_EVENT_IDS_CAP:
                break
            if not isinstance(raw_id, str) or not EVIDENCED_EVENT_ID_RE.match(raw_id):
                continue
            accepted += 1
            if raw_id in canonical_identity_conflicts:
                reject(raw_id, donor, CANONICAL_EVENT_IDENTITY_CONFLICT_REASON)
                continue
            event_created_at = canonical_created_at.get(raw_id)
            if client == "codex":
                if codex_descendant and (event_created_at is None or session_started_at is None):
                    reject(raw_id, donor, CODEX_REPLAY_TIME_UNPROVEN_REASON)
                    continue
                if (
                    event_created_at is not None
                    and session_started_at is not None
                    and event_created_at + CODEX_REPLAY_CLOCK_SKEW_SECONDS < session_started_at
                ):
                    reject(raw_id, donor, CODEX_REPLAY_REJECTION_REASON)
                    continue
            pending.setdefault(raw_id, []).append(donor)

    for raw_id, all_candidates in pending.items():
        usage_candidates = [
            donor for donor in all_candidates if donor.get("donor_kind") == "usage"
        ]
        observation_candidates = [
            donor
            for donor in all_candidates
            if donor.get("donor_kind") == "session_observation"
        ]
        # Usage is the stronger donor only when the observation lane tells the
        # same identity story. Checking usage and observation cohorts
        # separately lets a usage row from home B silently overrule an
        # observation from home A (or a different session in the same home).
        # Validate the full source cohort first, then validate complete donor
        # identity across kinds before applying the priority rule.
        if not _source_namespace_cohort_compatible(all_candidates):
            for donor in all_candidates:
                reject(raw_id, donor, SOURCE_NAMESPACE_CONFLICT_REASON)
            continue
        if usage_candidates and observation_candidates and not all(
            _cross_kind_donor_identity_compatible(usage, observation)
            for usage in usage_candidates
            for observation in observation_candidates
        ):
            for donor in all_candidates:
                reject(
                    raw_id,
                    donor,
                    CROSS_KIND_DONOR_IDENTITY_CONFLICT_REASON,
                )
            continue
        candidates = usage_candidates or observation_candidates
        if not candidates:
            continue
        donors = index.setdefault(raw_id, [])
        for donor in candidates:
            if any(
                existing["client"] == donor["client"]
                and existing["client_session_id"] == donor["client_session_id"]
                and existing.get("source_namespace_fingerprint")
                == donor.get("source_namespace_fingerprint")
                for existing in donors
            ):
                continue
            donors.append(donor)

    rejected_count = len(rejected_links)
    index.diagnostics.update(
        {
            "rejected_donor_links": rejected_count,
            "rejected_usage_rows": len(rejected_usage_rows),
            "rejection_reasons": dict(sorted(rejection_reasons.items())),
        }
    )
    if rejected_observation_rows:
        index.diagnostics["rejected_observation_rows"] = len(rejected_observation_rows)
    return index


def apply_log_evidence_to_snapshots(
    snapshots: list[dict[str, Any]],
    index: dict[str, list[dict[str, Any]]],
    *,
    group_key: Callable[[dict[str, Any]], Hashable | None] | None = None,
) -> dict[str, int]:
    """Enrich DERIVED snapshots with evidenced session keys; returns honesty counters.

    Decision table per evidenced snapshot (single donor unless noted):

    - >= 2 donors: ambiguous refusal — no implied key, ``log_evidence_ambiguous``.
    - claims conflict with the donor (session id differs, transcript differs
      from the donor transcript, or client differs): **veto both** — the live
      ``client_session_id`` is replaced by the donor session (evidenced
      grouping wins for display), the original claim is preserved as
      ``claimed_client_session_id`` (conflicting transcript/client claims
      likewise), and ``log_evidence_conflict`` makes join_rules.pair_match
      veto every id-key equality against this snapshot.
    - claim equals the donor: ``log_evidence_corroborated`` (keys and tiers
      untouched; explicit stays exact).
    - no session claim: implied key — ``client_session_id`` <- donor session,
      ``log_evidenced_join_keys`` = ["client_session_id"] (tier
      ``log_evidenced``, high, never exact), donor row recorded for audit.
      ``client_transcript_id`` is deliberately NOT implied.

    ``group_key`` (the work-events call site passes work_id) guards merged
    items: when one group's evidenced snapshots resolve to more than one
    distinct donor session, NO snapshot of that group receives an implied key
    (would-be-implied snapshots get ``log_evidence_ambiguous`` instead;
    conflict vetoes still stand — they are the stronger refusal), counted as
    ``item_conflicts``.

    ``group_key`` also drives the donor-cohort honesty rule (the mirror of the
    item guard): when one donor session's evidence links MORE THAN ONE distinct
    section (group), that session genuinely spans several sections and its
    tokens cannot be divided among them. Every implied-key snapshot of such a
    donor is stamped ``log_evidence_cohort_size`` (the distinct-section count)
    so ``join_rules.decide_attribution`` refuses the log-evidenced allocation as
    ambiguous EVEN WHEN only one section survived the id-conflict vetoes — the
    count that must drive the decision is the sections the donor's evidence
    links, not the sections surviving the veto. The implied key is still
    stamped (the section keeps grouping under its evidenced session for
    display); only the allocation refuses.

    The markers are computed fresh here on every build and never copied from
    stored event metadata — a hostile event that writes these keys into its
    metadata changes nothing.
    """

    counters = {
        "evidenced_snapshots": 0,
        "implied_session_keys": 0,
        "corroborated": 0,
        "conflicts": 0,
        "ambiguous_multi_session": 0,
        "item_conflicts": 0,
    }
    if not index:
        return counters

    resolutions: list[tuple[dict[str, Any], list[dict[str, Any]], Hashable | None]] = []
    group_donor_sessions: dict[Hashable, set[tuple[str, str, str | None]]] = {}
    # Distinct sections (groups) each single-donor session's evidence links.
    # This counts EVERY evidenced snapshot of that donor session — implied,
    # corroborated, or conflict-vetoed — because each is a section belonging to
    # the donor; a conflict-vetoed section still means the session spans it.
    donor_session_groups: dict[tuple[str, str, str | None], set[Hashable]] = {}
    for snapshot in snapshots:
        event_id = str(snapshot.get("event_id") or "")
        donors = index.get(event_id)
        if not donors:
            continue
        group = group_key(snapshot) if group_key is not None else None
        resolutions.append((snapshot, donors, group))
        if group is not None:
            sessions = group_donor_sessions.setdefault(group, set())
            for donor in donors:
                sessions.add(
                    (
                        donor["client"],
                        donor["client_session_id"],
                        donor.get("source_namespace_fingerprint"),
                    )
                )
            if len(donors) == 1:
                donor = donors[0]
                donor_session_groups.setdefault(
                    (
                        donor["client"],
                        donor["client_session_id"],
                        donor.get("source_namespace_fingerprint"),
                    ),
                    set(),
                ).add(group)
    refused_groups = {group for group, sessions in group_donor_sessions.items() if len(sessions) > 1}
    counters["item_conflicts"] = len(refused_groups)

    for snapshot, donors, group in resolutions:
        counters["evidenced_snapshots"] += 1
        if len(donors) > 1:
            snapshot["log_evidence_ambiguous"] = {"session_count": len(donors)}
            # Keep the donor identities as non-allocating display evidence.
            # Product projections may use them only to answer the coarser
            # question "do all observations belong to the same root Task?";
            # the exact section-to-session join remains deliberately unset.
            snapshot["log_evidence_candidate_sessions"] = [
                {
                    "client": client,
                    "client_session_id": session_id,
                    **(
                        {"source_namespace_fingerprint": source_namespace}
                        if source_namespace
                        else {}
                    ),
                }
                for client, session_id, source_namespace in sorted(
                    {
                        (
                            donor["client"],
                            donor["client_session_id"],
                            donor.get("source_namespace_fingerprint"),
                        )
                        for donor in donors
                    },
                    key=lambda value: (value[0], value[1], value[2] or ""),
                )
            ]
            counters["ambiguous_multi_session"] += 1
            continue
        donor = donors[0]
        snapshot["log_evidenced_source_namespace_fingerprint"] = _optional_str(
            donor.get("source_namespace_fingerprint")
        )
        claimed_client = _optional_str(snapshot.get("client"))
        claimed_session = _optional_str(snapshot.get("client_session_id"))
        claimed_transcript = _optional_str(snapshot.get("client_transcript_id"))
        session_conflict = claimed_session is not None and claimed_session != donor["client_session_id"]
        transcript_conflict = (
            claimed_transcript is not None
            and donor.get("client_transcript_id") is not None
            and claimed_transcript != donor["client_transcript_id"]
        )
        client_conflict = claimed_client is not None and claimed_client != donor["client"]
        if session_conflict or transcript_conflict or client_conflict:
            # Label the conflict by the key(s) that actually differ. Only the
            # conflicting claim is preserved + replaced by the donor value; a
            # key that agreed (or was absent) is left untouched so we never
            # record an equal claimed==evidenced pair or blame the wrong key.
            conflict: dict[str, Any] = {"conflicting_keys": []}
            if session_conflict:
                snapshot["claimed_client_session_id"] = claimed_session
                snapshot["client_session_id"] = donor["client_session_id"]
                conflict["conflicting_keys"].append("client_session_id")
                conflict["claimed_client_session_id"] = claimed_session
                conflict["evidenced_client_session_id"] = donor["client_session_id"]
            else:
                # Session agrees (or is absent): keep the live session under the
                # donor for display grouping, but DO NOT record a session-id
                # conflict pair — the disagreement is elsewhere.
                snapshot["client_session_id"] = donor["client_session_id"]
            if transcript_conflict:
                snapshot["claimed_client_transcript_id"] = claimed_transcript
                snapshot["client_transcript_id"] = None
                conflict["conflicting_keys"].append("client_transcript_id")
                conflict["claimed_client_transcript_id"] = claimed_transcript
                conflict["evidenced_client_transcript_id"] = donor.get("client_transcript_id")
            if client_conflict:
                snapshot["claimed_client"] = claimed_client
                snapshot["client"] = donor["client"]
                conflict["conflicting_keys"].append("client")
                conflict["claimed_client"] = claimed_client
                conflict["evidenced_client"] = donor["client"]
            snapshot["log_evidence_conflict"] = conflict
            counters["conflicts"] += 1
            continue
        if claimed_session is not None:
            snapshot["log_evidence_corroborated"] = True
            counters["corroborated"] += 1
            continue
        if group in refused_groups:
            snapshot["log_evidence_ambiguous"] = {"session_count": len(group_donor_sessions[group])}
            snapshot["log_evidence_candidate_sessions"] = [
                {
                    "client": client,
                    "client_session_id": session_id,
                    **(
                        {"source_namespace_fingerprint": source_namespace}
                        if source_namespace
                        else {}
                    ),
                }
                for client, session_id, source_namespace in sorted(
                    group_donor_sessions[group],
                    key=lambda value: (value[0], value[1], value[2] or ""),
                )
            ]
            counters["ambiguous_multi_session"] += 1
            continue
        snapshot["client_session_id"] = donor["client_session_id"]
        snapshot["log_evidenced_join_keys"] = ["client_session_id"]
        snapshot["log_evidenced_by_event_id"] = donor.get("donor_event_id")
        snapshot["log_evidence_donor_kind"] = donor.get("donor_kind")
        # Frozen compatibility field: existing usage donors keep precisely
        # the old key/value. Observation-only donors do not pretend to be
        # usage rows and leave it absent.
        if donor.get("usage_event_id"):
            snapshot["log_evidenced_by_usage_event_id"] = donor["usage_event_id"]
        if claimed_client is None:
            snapshot["client"] = donor["client"]
        # Donor-cohort size: the number of distinct sections this donor
        # session's evidence links (all snapshots, veto-survivors or not). >1
        # means the session spans multiple sections, so join_rules refuses the
        # log-evidenced allocation as ambiguous even when this is the only
        # section that survived the id-conflict vetoes. The implied key is
        # still stamped above so the section keeps grouping under the session.
        cohort = donor_session_groups.get(
            (
                donor["client"],
                donor["client_session_id"],
                donor.get("source_namespace_fingerprint"),
            )
        )
        if cohort is not None and len(cohort) > 1:
            snapshot["log_evidence_cohort_size"] = len(cohort)
        counters["implied_session_keys"] += 1
    return counters


def build_log_evidence_session_blocks(
    events: list[dict[str, Any]], index: dict[str, list[dict[str, Any]]]
) -> dict[tuple[str, str, str | None], dict[str, Any]]:
    """(client, session, source home) -> evidence counts and conflicts.

    Counts every evidenced event PRESENT in this store (including plain
    record_event/task events that never become work items) against its single
    donor session; multi-donor evidence counts toward no session (honest
    refusal). ``conflicts`` counts store events whose own metadata claims a
    session/transcript/client that disagrees with the evidenced donor —
    mirroring the applier's conflict rule for surfaces that have no snapshot.
    """

    blocks: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    if not index:
        return blocks
    for event in events:
        event_id = _optional_str(event.get("event_id"))
        if event_id is None:
            continue
        donors = index.get(event_id)
        if not donors or len(donors) != 1:
            continue
        donor = donors[0]
        key = (
            donor["client"],
            donor["client_session_id"],
            _optional_str(donor.get("source_namespace_fingerprint")),
        )
        block = blocks.setdefault(key, {"evidenced_event_count": 0, "by_event_type": {}, "conflicts": 0})
        block["evidenced_event_count"] += 1
        event_type = str(event.get("event_type") or "unknown")
        block["by_event_type"][event_type] = block["by_event_type"].get(event_type, 0) + 1
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        claimed_session = _optional_str(metadata.get("client_session_id"))
        claimed_transcript = _optional_str(metadata.get("client_transcript_id"))
        claimed_client = _optional_str(metadata.get("client"))
        if (
            (claimed_session is not None and claimed_session != donor["client_session_id"])
            or (
                claimed_transcript is not None
                and donor.get("client_transcript_id") is not None
                and claimed_transcript != donor["client_transcript_id"]
            )
            or (claimed_client is not None and claimed_client != donor["client"])
        ):
            block["conflicts"] += 1
    return blocks


def summarize_log_evidence_donor_rows(events: list[dict[str, Any]]) -> dict[str, int]:
    """Store-wide donor counters: rows carrying ids, id totals, skipped outputs."""

    kept_events, _shadowed = split_shadowed_legacy_usage_events(events)
    donor_usage_rows = 0
    donor_observation_rows = 0
    evidenced_event_ids_total = 0
    outputs_skipped = 0
    observation_evidenced_event_ids_total = 0
    observation_outputs_skipped = 0
    for event in kept_events:
        if not is_local_usage_import_event(event):
            continue
        metadata = event.get("metadata")
        if not isinstance(metadata, dict):
            continue
        ids = metadata.get("evidenced_event_ids")
        valid_ids = [item for item in ids if isinstance(item, str) and EVIDENCED_EVENT_ID_RE.match(item)] if isinstance(ids, list) else []
        row_total = _safe_nonnegative_int(metadata.get("evidenced_event_id_total")) or len(valid_ids)
        if valid_ids:
            donor_usage_rows += 1
            evidenced_event_ids_total += max(row_total, len(valid_ids))
        outputs_skipped += _safe_nonnegative_int(metadata.get("evidenced_outputs_skipped"))
    selected_observations, _diagnostics = reduce_local_session_observation_events(events)
    for event in selected_observations:
        metadata = event.get("metadata")
        if not isinstance(metadata, dict):
            continue
        ids = metadata.get("evidenced_event_ids")
        valid_ids = [
            item
            for item in ids
            if isinstance(item, str) and EVIDENCED_EVENT_ID_RE.match(item)
        ] if isinstance(ids, list) else []
        row_total = _safe_nonnegative_int(metadata.get("evidenced_event_id_total")) or len(valid_ids)
        if valid_ids:
            donor_observation_rows += 1
            observation_evidenced_event_ids_total += max(row_total, len(valid_ids))
        observation_outputs_skipped += _safe_nonnegative_int(metadata.get("evidenced_outputs_skipped"))
    return {
        "donor_usage_rows": donor_usage_rows,
        "donor_observation_rows": donor_observation_rows,
        "evidenced_event_ids_total": evidenced_event_ids_total,
        "outputs_skipped": outputs_skipped,
        "observation_evidenced_event_ids_total": observation_evidenced_event_ids_total,
        "observation_outputs_skipped": observation_outputs_skipped,
    }


def _canonical_event_identity(events: list[dict[str, Any]]) -> tuple[dict[str, float], set[str]]:
    """Return (unambiguous timestamps, conflicting canonical event ids).

    Local usage rows are donor claims, not canonical creation facts. Duplicate
    rows are harmless only when their timestamp and semantic namespace remain
    compatible. Conflicting timestamps, cross-namespace identity, or an
    explicit namespace missing its fingerprint make the event id unusable for
    every donor; raw rows remain untouched.
    """

    candidates: dict[str, list[tuple[float | None, dict[str, Any]]]] = {}
    for event in events:
        if is_local_usage_import_event(event):
            continue
        event_id = _optional_str(event.get("event_id"))
        if event_id is None:
            continue
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        candidates.setdefault(event_id, []).append(
            (
                _safe_positive_timestamp(event.get("created_at")),
                {
                    "namespace_fingerprint": metadata.get("session_namespace_fingerprint")
                    or metadata.get("namespace_fingerprint"),
                    "identity_scope_state": metadata.get("identity_scope_state"),
                },
            )
        )

    timestamps: dict[str, float] = {}
    conflicts: set[str] = set()
    for event_id, rows in candidates.items():
        valid_times = {timestamp for timestamp, _namespace in rows if timestamp is not None}
        missing_time = any(timestamp is None for timestamp, _namespace in rows)
        namespace_rows = [namespace for _timestamp, namespace in rows]
        if len(valid_times) > 1 or (len(rows) > 1 and missing_time) or not _namespace_cohort_compatible(namespace_rows):
            conflicts.add(event_id)
            continue
        if len(valid_times) == 1:
            timestamps[event_id] = next(iter(valid_times))
    return timestamps, conflicts


def _source_namespace_cohort_compatible(donors: list[dict[str, Any]]) -> bool:
    return _namespace_cohort_compatible(
        [
            {
                "namespace_fingerprint": donor.get("source_namespace_fingerprint"),
                "identity_scope_state": (
                    "explicit" if donor.get("source_namespace_fingerprint") else "unscoped"
                ),
            }
            for donor in donors
        ]
    )


def _cross_kind_donor_identity_compatible(
    usage: dict[str, Any], observation: dict[str, Any]
) -> bool:
    """Whether usage may safely supersede one observation donor.

    Source namespace compatibility is checked for the complete cohort by the
    caller. Client and normalized session must match exactly. Transcript is
    optional corroboration: two explicit, different transcript ids conflict;
    one missing transcript does not invent a disagreement.
    """

    if (
        _optional_str(usage.get("client"))
        != _optional_str(observation.get("client"))
        or _optional_str(usage.get("client_session_id"))
        != _optional_str(observation.get("client_session_id"))
    ):
        return False
    usage_transcript = _optional_str(usage.get("client_transcript_id"))
    observation_transcript = _optional_str(
        observation.get("client_transcript_id")
    )
    return not (
        usage_transcript is not None
        and observation_transcript is not None
        and usage_transcript != observation_transcript
    )


def _namespace_cohort_compatible(facts: list[dict[str, Any]]) -> bool:
    for index, left in enumerate(facts):
        if not namespace_join_compatible(left, left):
            return False
        for right in facts[index + 1 :]:
            if not namespace_join_compatible(left, right):
                return False
    return True


def _safe_positive_timestamp(value: Any) -> float | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(timestamp) or timestamp <= 0:
        return None
    return timestamp


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _safe_nonnegative_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if number > 0 else 0
