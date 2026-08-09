"""The ``/v1/sessions`` lane — a stable, versioned session list for native shells.

Contract mirrors ``/v1/glance`` (see :mod:`agentacct.glance`): bearer-gated,
additive-only within v1 (consumers pin ``schema`` and ignore unknown keys),
and cheap under polling — the enriched view is cached by events fingerprint +
TTL; a request that hits the cache pays one envelope slice, never a rebuild.

Why this lane exists next to the legacy ``/sessions`` rollup dump:

* **Curated projection** — a native shell decodes every field it is shown, so
  this lane serves a deliberate schema (identity, activity, status, usage,
  work, relations, plan share) rather than "whatever the rollup emits". The
  rollup's internal plumbing (namespace fingerprints, join-policy bits,
  grouping keys) stays off the wire.
* **Server-side roots + pagination** — the "12 root sessions" failure mode:
  slicing the recency-sorted root+child mix server-side and filtering roots
  client-side starves the list (one root's subagent children can fill any
  window). ``roots_only`` filters BEFORE the slice; ``offset``/``limit`` walk
  the filtered population; ``truncated`` + totals disclose every cut.
* **Plan share with children folded** — a roots view hides child sessions, so
  each root row carries its children's weekly-plan share too
  (:func:`agentacct.glance.fold_plan_pcts_to_roots` rationale). ``plan_pct``
  is the honest headline (own + children); the split is disclosed alongside.

Pagination is a recency-ordered offset walk: a session landing between two
polls shifts the window by one — fine for a local UI, disclosed here rather
than hidden behind a cursor the event log cannot cheaply support.
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Any, Callable

from .task_outcome import step_evidence_grade

V1_SESSIONS_SCHEMA_VERSION = "agentacct.v1-sessions.v1"
V1_SESSION_DETAIL_SCHEMA_VERSION = "agentacct.v1-session-detail.v1"

# Row keys that exist only for the view's own bookkeeping, never on the wire.
_VIEW_INTERNAL_KEYS = frozenset({"is_root", "fold_top"})

# TTL rationale: the view is mostly event-derived (the fingerprint catches
# those changes), but plan calibration windows are clock-relative and the
# ledger's secondary inputs are fingerprint-invisible. 30s matches the ledger
# cache's TTL so the two stack to a bounded ≤60s worst case for
# fingerprint-invisible inputs — a 60s view TTL over a 30s ledger TTL
# compounded to ~90s (adversarial-review finding).
V1_SESSIONS_CACHE_MAX_AGE_SECONDS = 30.0

# Session-level status reduction — same precedence as the TUI badge:
# blocked > handed_off > in_progress > completed, with ``resolved`` (the
# terminal blocker-resolution state a machine check can stamp on a work item)
# counting as completed. NOTE an honest divergence: the ledger-free glance
# recents reduce raw section events and never see machine-check blocker
# resolutions, so after a resolution the glance can still show "blocked"
# while this lane and the TUI show "completed" — the stale side errs in the
# conservative direction (unfinished-looking finished work, never the
# reverse).
_OPEN_ITEM_STATUSES = frozenset({"started", "checkpoint"})
_DONE_ITEM_STATUSES = frozenset({"completed", "resolved"})


def _reduce_work_status(items: Any) -> str | None:
    """One session badge from its work items' latest statuses.

    A later completed section must never erase a still-open or blocked one —
    a finished-looking label on unfinished work is exactly the dishonesty
    class this product exists to fix (same precedence as the TUI badge; see
    the module-level note on the glance's blocker-resolution lag).
    """

    statuses: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            status = str(item.get("latest_status") or "")
            if status:
                statuses.add(status)
    if "blocked" in statuses:
        return "blocked"
    if "handed_off" in statuses:
        return "handed_off"
    if statuses & _OPEN_ITEM_STATUSES:
        return "in_progress"
    if statuses & _DONE_ITEM_STATUSES:
        return "completed"
    return None


def _fold_parent_key(
    entry: dict[str, Any], entries_by_key: dict[tuple[str, str], dict[str, Any]]
) -> tuple[str, str] | None:
    """The entry's fold parent ON THIS SURFACE, or None (= shown as a root).

    The rollup's parent linkage is the starting authority, but two of its
    states mean "do NOT fold here" (adversarial-review findings, both
    repro-confirmed):

    * the ledger REFUSED the join (``parent_source_namespace_mismatch``) —
      folding would hand one identity's plan share to a same-id root from a
      different source home, on a row whose own ``child_session_count`` says 0;
    * the parent has no rollup entry (orphan child — "no stub entry") —
      folding would sink the share under a key no row carries, silently
      dropping a live session and its weekly-plan consumption from the
      default roots page. The ledger's own instrumentation pass treats an
      orphan as its own root; so does this surface.

    A legacy ``':stem'`` child lane with no parent metadata folds into its
    prefix root when that root exists AND lives in the same source home
    (:func:`agentacct.glance.fold_namespace_compatible` — the ledger has no
    link to refuse for these lanes, so the namespace gate is applied here) —
    the same defensive fallback the glance applies, so the two /v1 surfaces
    agree on the root's headline number instead of the sessions lane
    re-counting legacy children as roots. Like the glance, the fallback is
    not client-gated (a non-claude id containing ':' folds by shape);
    acceptable while only claude-code carries plan shares.
    """

    from .glance import fold_namespace_compatible

    client = str(entry.get("client") or "")
    related = entry.get("related")
    parent = related.get("parent") if isinstance(related, dict) else None
    if isinstance(parent, dict):
        if related.get("parent_source_namespace_mismatch"):
            return None
        parent_id = str(parent.get("client_session_id") or "")
        if parent_id:
            key = (client, parent_id)
            return key if key in entries_by_key else None
        return None
    if isinstance(related, dict) and related.get("note"):
        # The ledger REFUSED a recorded parent link (conflicting or
        # self-referencing pointers — related.note carries the refusal).
        # "Missing beats wrong" must not be overridden by an id-shape guess:
        # falling through to the ':' fallback here re-folded exactly what the
        # ledger refused (round-4 adversarial finding).
        return None
    session_id = str(entry.get("client_session_id") or "")
    if ":" in session_id:
        key = (client, session_id.split(":", 1)[0])
        prefix_entry = entries_by_key.get(key)
        if prefix_entry is not None:
            child_ns = entry.get("source_namespace_fingerprint")
            parent_ns = prefix_entry.get("source_namespace_fingerprint")
            if fold_namespace_compatible(
                str(child_ns) if child_ns else None,
                str(parent_ns) if parent_ns else None,
                None,
            ):
                return key
    return None


def _observed_models_with_usage_fallback(
    instrumented: Any, usage: Any
) -> list[str] | None:
    """The session's model list, completed from usage when instrumentation is
    thin.

    ``observed_models`` is sourced from the recording hook's section metadata,
    which is often empty even when the session demonstrably used models. The
    usage records are the authoritative token source and always carry the model
    (``identity_models`` / ``model_lanes``). Union the two — instrumentation
    order first, then any usage model not already listed — so a multi-model
    session (e.g. one that switched models mid-run) shows every model it used at
    the row level instead of a blank. Returns ``None`` when neither source has a
    model, so the wire stays null-not-empty.
    """

    models: list[str] = []
    seen: set[str] = set()

    def _add(name: Any) -> None:
        text = str(name or "").strip()
        if text and text not in seen:
            seen.add(text)
            models.append(text)

    if isinstance(instrumented, list):
        for name in instrumented:
            _add(name)
    if isinstance(usage, dict):
        identity = usage.get("identity_models")
        if isinstance(identity, list):
            for name in identity:
                _add(name)
        lanes = usage.get("model_lanes")
        if isinstance(lanes, list):
            for lane in lanes:
                if isinstance(lane, dict):
                    _add(lane.get("model"))
    return models or None


def _project_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """The curated wire row for one rollup entry (plan fields joined later).

    Blocks the app renders (``usage``/``join``/``work``/``related``) pass
    through verbatim — they are already-built read models with their own
    honesty semantics (None-not-zero costs, evidence enums); re-projecting
    them here would just be a second place for those rules to drift.
    """

    work = entry.get("work") if isinstance(entry.get("work"), dict) else {}
    # Title fallback chain, mirroring the glance: the client's own transcript
    # title when it recorded one, else the newest HUMAN-titled work item (a
    # section-only session still deserves a human label). work.items arrives
    # NEWEST-FIRST from the rollup (build_work_items sorts by updated_at
    # descending) — iterate forward; reversed() picked the oldest title
    # (adversarial-review finding). build_work_items backfills an untitled
    # item's title with its work_id/section_id — skip those placeholders so a
    # newer untitled section can't shadow an older human title (round-2
    # finding: the glance only considers real section titles).
    title = entry.get("client_session_title")
    if not title:
        items = work.get("items") if isinstance(work.get("items"), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            candidate = item.get("title")
            if candidate and candidate != item.get("work_id") and candidate != item.get("section_id"):
                title = candidate
                break
    return {
        "session_key": entry.get("session_key"),
        "client": entry.get("client"),
        "client_session_id": entry.get("client_session_id"),
        "client_session_id_short": entry.get("client_session_id_short"),
        "session_kind": entry.get("session_kind"),
        "title": title,
        "project": entry.get("project"),
        "project_source": entry.get("project_source"),
        "status": _reduce_work_status(work.get("items")),
        "first_activity_at": entry.get("first_activity_at"),
        "last_activity_at": entry.get("last_activity_at"),
        "duration_seconds": entry.get("duration_seconds"),
        "instrumentation_state": entry.get("instrumentation_state"),
        "instrumentation_state_basis": entry.get("instrumentation_state_basis"),
        "instrumentation_installed_at": entry.get("instrumentation_installed_at"),
        "observed_models": _observed_models_with_usage_fallback(
            entry.get("observed_models"), entry.get("usage")
        ),
        "usage": entry.get("usage"),
        "usage_note": entry.get("usage_note"),
        "join": entry.get("join"),
        "work": entry.get("work"),
        "related": entry.get("related"),
        "plan_pct_own": None,
        "plan_pct_children": None,
        "plan_pct": None,
    }


def build_v1_sessions_view(
    ledger: dict[str, Any], events: list[dict[str, Any]], *, now: float | None = None
) -> dict[str, Any]:
    """The cacheable enriched view: every rollup entry projected + plan-joined.

    Unfiltered and unsliced so one build serves any ``roots_only``/paging
    combination. Plan shares are the calibrated-or-nothing per-session
    percentages; every descendant's share is accumulated onto the TOPMOST
    ancestor that exists as an entry on this surface (see
    :func:`_fold_parent_key` for the three do-not-fold states), so the sum of
    the visible roots' ``plan_pct`` always equals the total attributed share —
    hiding a child never hides its weekly-plan consumption. Raw floats, never
    rounded here (shells format like the TUI: ``≈{pct:.1f}%`` with a ``<0.1%``
    band). ``generated_at`` is the BUILD time (glance contract: freshness is
    judged from the HTTP response itself, and the stamp never claims a cached
    view is fresher than it is).
    """

    import time as _time

    from .glance import plan_status_and_session_pcts, resolve_fold_top

    rollup = ledger.get("session_rollup") if isinstance(ledger, dict) else {}
    entries = rollup.get("sessions") if isinstance(rollup, dict) else []
    entries = [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []

    plan, session_pcts, weights_by_client, records_by_client = plan_status_and_session_pcts(events)

    def _key(entry: dict[str, Any]) -> tuple[str, str]:
        return (str(entry.get("client") or ""), str(entry.get("client_session_id") or ""))

    entries_by_key = {_key(entry): entry for entry in entries}
    fold_step = {key: _fold_parent_key(entry, entries_by_key) for key, entry in entries_by_key.items()}
    # Rootness and share attribution come from the SAME fixpoint: a row is a
    # root iff it is its own fold top. resolve_fold_top breaks parent cycles
    # deterministically (min member), so cycle members can never all vanish
    # from the roots page with their shares (round-2 adversarial finding).
    fold_top_by_key = {key: resolve_fold_top(fold_step, key) for key in entries_by_key}

    descendant_pct_by_top: dict[tuple[str, str], float] = {}
    for entry in entries:
        key = _key(entry)
        own = session_pcts.get(key)
        if own is None:
            continue
        top = fold_top_by_key.get(key, key)
        if top != key:
            descendant_pct_by_top[top] = descendant_pct_by_top.get(top, 0.0) + own

    root_count = 0
    rows: list[dict[str, Any]] = []
    for entry in entries:
        row = _project_entry(entry)
        key = _key(entry)
        is_root = fold_top_by_key.get(key, key) == key
        if is_root:
            root_count += 1
        own = session_pcts.get(key)
        descendants = descendant_pct_by_top.get(key)
        row["plan_pct_own"] = own
        row["plan_pct_children"] = descendants
        if own is not None or descendants is not None:
            row["plan_pct"] = (own or 0.0) + (descendants or 0.0)
        row["is_root"] = is_root
        # View-internal (stripped from the wire like is_root): the fixpoint
        # this row's share folds to — the detail endpoint lists a root's
        # descendants by matching it.
        row["fold_top"] = fold_top_by_key.get(key, key)
        rows.append(row)

    # Step-join indexes, built once per view so the detail route is pure
    # lookups instead of per-request ledger scans + a full usage-view rebuild
    # (adversarial-review finding: /v1/session cost ~10x /v1/sessions per
    # poll). Everything under plan_context/step_join is VIEW-INTERNAL — the
    # wire never carries it (slice/detail project explicit fields only).
    attributions_by_work: dict[str, list[dict[str, Any]]] = {}
    for attribution in ledger.get("attributions") or []:
        if isinstance(attribution, dict):
            work_id = str(attribution.get("work_id") or "")
            if work_id:
                attributions_by_work.setdefault(work_id, []).append(attribution)
    usage_event_models: dict[str, tuple[str | None, str | None]] = {}
    for usage_event in ledger.get("usage_events") or []:
        if isinstance(usage_event, dict):
            usage_event_id = str(usage_event.get("usage_event_id") or "")
            if usage_event_id:
                usage_event_models[usage_event_id] = (
                    usage_event.get("model"),
                    usage_event.get("provider"),
                )

    return {
        "generated_at": _time.time() if now is None else float(now),
        "rows": rows,
        "plan": plan,
        "total_sessions": len(rows),
        "total_root_sessions": root_count,
        "plan_context": {
            "weights_by_client": weights_by_client,
            "records_by_client": records_by_client,
        },
        "step_join": {
            "attributions_by_work": attributions_by_work,
            "usage_event_models": usage_event_models,
        },
    }


def slice_sessions_payload(
    view: dict[str, Any],
    *,
    roots_only: bool,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """The wire envelope for one request — a filter + slice over the cached view.

    Every cut is disclosed: ``total_sessions``/``total_root_sessions`` describe
    the full store, ``filtered_total`` the population the offset/limit walk,
    and ``truncated`` says whether rows exist beyond this page — a consumer can
    never mistake a page for the whole population. ``generated_at`` is the
    cached view's BUILD time, never the request time — stamping fresh clocks
    on cached content would be a freshness lie (adversarial-review finding).
    """

    rows = view.get("rows") or []
    pool = [row for row in rows if row.get("is_root")] if roots_only else list(rows)
    page = pool[offset : offset + limit]
    # ``is_root``/``fold_top`` are view-internal bookkeeping (the wire has
    # related.parent); strip them without mutating the cached rows.
    page = [
        {key: value for key, value in row.items() if key not in _VIEW_INTERNAL_KEYS}
        for row in page
    ]
    return {
        "schema": V1_SESSIONS_SCHEMA_VERSION,
        "generated_at": view.get("generated_at"),
        "total_sessions": view.get("total_sessions"),
        "total_root_sessions": view.get("total_root_sessions"),
        "filtered_total": len(pool),
        "roots_only": roots_only,
        "offset": offset,
        "limit": limit,
        "returned": len(page),
        "truncated": offset + len(page) < len(pool),
        "plan": view.get("plan"),
        "sessions": page,
    }


class V1SessionsCache:
    """Fingerprint + TTL cache for the enriched view (GlanceCache pattern).

    Same concurrency posture: the cached value is one atomic tuple assignment,
    racing rebuilds waste one build and the last writer wins. The builder is
    injected per call so the cache stays free of service wiring.
    """

    def __init__(self, max_age_seconds: float = V1_SESSIONS_CACHE_MAX_AGE_SECONDS) -> None:
        self._lock = Lock()
        self._cached: tuple[int, float, dict[str, Any]] | None = None
        self.max_age_seconds = float(max_age_seconds)

    def _fresh(self, cached: tuple[int, float, dict[str, Any]] | None, fingerprint: int, moment: float) -> bool:
        return (
            cached is not None
            and cached[0] == fingerprint
            and (moment - cached[1]) < self.max_age_seconds
        )

    def view(
        self,
        fingerprint: int,
        builder: Callable[[], dict[str, Any]],
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        moment = time.time() if now is None else float(now)
        cached = self._cached
        if self._fresh(cached, fingerprint, moment):
            return cached[2]  # type: ignore[index]
        with self._lock:
            cached = self._cached
            if self._fresh(cached, fingerprint, moment):
                return cached[2]  # type: ignore[index]
            view = builder()
            self._cached = (fingerprint, moment, view)
            return view


# ---------------------------------------------------------------------------
# session detail (the expandable-steps view — the depth the TUI sets the bar for)
# ---------------------------------------------------------------------------

_STEP_CHECK_FIELDS = (
    # Curated check projection, passed through from the ledger's evidence
    # events VERBATIM under their real names and types. In that projection
    # ``artifact_path``/``artifact_url`` are ALREADY the sanitized-safe
    # strings, the ``*_redacted`` twins are BOOLEAN was-withheld disclosures,
    # and no raw command string exists at all (``command_redacted`` says one
    # was recorded). Round-1 adversarial finding: an earlier draft mistook
    # the boolean flags for redacted strings and served ``command: true``
    # while dropping the safe artifact values.
    "event_id",
    "created_at",
    "evidence_type",
    "result",
    "summary",
    "exit_code",
    # Independence category (mcp_agent_reported / client_hook / ci) — not
    # sensitive, and it is what lets a surface show that a check saying "CI
    # green" in its summary is really only agent-reported.
    "source_type",
    "check_identity",
    "supersession_state",
    "superseded_by_event_id",
    "resolution_scope",
    "resolution_summary",
    "resolves_blocked_event_id",
    "files",
    "artifact_ref",
    "artifact_path",
    "artifact_url",
    "command_redacted",
    "artifact_path_redacted",
    "artifact_url_redacted",
)


def _project_check(event: dict[str, Any]) -> dict[str, Any]:
    return {name: event.get(name) for name in _STEP_CHECK_FIELDS}


def _project_step(item: dict[str, Any], models: list[dict[str, Any]]) -> dict[str, Any]:
    evidence_events = item.get("evidence_events")
    checks = [
        _project_check(event)
        for event in (evidence_events if isinstance(evidence_events, list) else [])
        if isinstance(event, dict)
    ]
    grade = step_evidence_grade(item)
    return {
        "work_id": item.get("work_id"),
        # Per-step evidence grade (M2) + a one-line reason ("why strong/weak/
        # none"). Graded by WHO attested; today every check is agent-reported,
        # so a step tops out at self_checked until a hook / CI source lands.
        "evidence_grade": grade.get("grade"),
        "evidence_grade_reason": grade.get("reason"),
        "section_id": item.get("section_id"),
        "title": item.get("title"),
        "latest_status": item.get("latest_status"),
        "kind": item.get("kind"),
        "phase": item.get("phase"),
        "started_at": item.get("started_at"),
        "updated_at": item.get("updated_at"),
        "summary": item.get("summary"),
        "files": item.get("files"),
        "blocker": item.get("blocker"),
        "next_step": item.get("next_step"),
        "usage": {
            "total_tokens": item.get("usage_total"),
            "fresh_tokens": item.get("usage_fresh_total"),
            "cache_read_tokens": item.get("usage_cache_read_total"),
            "cache_creation_tokens": item.get("usage_cache_creation_total"),
            # The ledger's internal sum defaults to 0.0; under the wire name
            # estimated_cost_usd the product-wide contract is None-never-$0
            # when nothing was priced (adversarial-review finding). A value
            # with unpriced rows alongside is a PARTIAL subtotal — the counts
            # below are the shell's completeness signal.
            "estimated_cost_usd": (
                item.get("estimated_cost_total")
                if isinstance(item.get("priced_usage_records"), int)
                and item.get("priced_usage_records", 0) > 0
                else None
            ),
            "linked_usage_records": item.get("linked_usage_records"),
            "priced_usage_records": item.get("priced_usage_records"),
            "unpriced_usage_records": item.get("unpriced_usage_records"),
        },
        "join_confidence": item.get("join_confidence"),
        "join_explanation": item.get("join_explanation"),
        "evidence_status": item.get("evidence_status"),
        "models": models,
        "checks": checks,
    }


def _step_models(
    work_id: str,
    attributions_by_work: dict[str, list[dict[str, Any]]],
    usage_event_models: dict[str, tuple[str | None, str | None]],
) -> list[dict[str, Any]]:
    """Per-step model lanes via the attribution join.

    A step has no model of its own — models ride on usage events, and the
    ledger's attributions are the ONLY honest link between the two. Tokens
    come from the attribution rows (the attributed slice), never re-guessed.
    Unattributed steps simply return [] — missing beats invented.
    """

    lanes: dict[tuple[str | None, str | None], float] = {}
    for attribution in attributions_by_work.get(work_id, []):
        usage_event_id = str(attribution.get("usage_event_id") or "")
        lane_identity = usage_event_models.get(usage_event_id)
        if lane_identity is None:
            continue  # dangling attribution — we know nothing about this event
        model, provider = lane_identity
        # A known event with NO model keeps its lane (model: null) — dropping
        # it made the lane sum silently undercount the step total (the
        # session rollup's model_lanes keep null lanes for the same reason).
        key = (model, provider)
        tokens = attribution.get("usage_tokens")
        amount = float(tokens) if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) else 0.0
        lanes[key] = lanes.get(key, 0.0) + amount
    return sorted(
        (
            {"model": model, "provider": provider, "total_tokens": tokens}
            for (model, provider), tokens in lanes.items()
        ),
        key=lambda lane: (-lane["total_tokens"], str(lane["model"])),
    )


def _descendant_row(row: dict[str, Any]) -> dict[str, Any]:
    usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
    return {
        "client": row.get("client"),
        "client_session_id": row.get("client_session_id"),
        "client_session_id_short": row.get("client_session_id_short"),
        "title": row.get("title"),
        "status": row.get("status"),
        "last_activity_at": row.get("last_activity_at"),
        "usage": {
            "total_tokens": usage.get("total_tokens"),
            "fresh_tokens": usage.get("fresh_tokens"),
            "estimated_cost_usd": usage.get("estimated_cost_usd"),
            "cost_confidence": usage.get("cost_confidence"),
        },
        "plan_pct": row.get("plan_pct_own"),
    }


def build_v1_session_detail(
    view: dict[str, Any],
    ledger: dict[str, Any],
    *,
    client: str,
    session_id: str,
) -> dict[str, Any] | None:
    """The one-session deep view: the list row + expandable steps + descendants.

    ``None`` when the session has no rollup entry (the route 404s — never an
    empty fabrication). Steps come from the ledger's full work_items (the
    rollup's embedded mini-rows lack timestamps/usage/checks), filtered by
    (client, session_id) exactly like the TUI's detail screen, newest first.
    Each step carries its attributed model lanes and its machine checks
    (curated projection). Descendants are every view row whose fold fixpoint
    is this session — the same accounting behind the list row's
    ``plan_pct_children``, so the numbers can never disagree. The per-session
    plan block adds the why-this-number disclosure (basis/scale) and a
    by-model split of the session's OWN share (calibrated-or-nothing), all
    derived from the CACHED view's own fit and indexes — a per-request
    recompute both cost a full usage-view rebuild per poll and could disagree
    with the very pct values it shipped next to (adversarial-review findings).
    """

    from .plan_cost import plan_status_entry, session_components_by_model

    key = (client, session_id)
    row = next(
        (
            candidate
            for candidate in (view.get("rows") or [])
            if (candidate.get("client"), candidate.get("client_session_id")) == key
        ),
        None,
    )
    if row is None:
        return None

    items = [
        item
        for item in (ledger.get("work_items") or [])
        if isinstance(item, dict)
        and str(item.get("client") or "") == client
        and str(item.get("client_session_id") or "") == session_id
    ]

    step_join = view.get("step_join") if isinstance(view.get("step_join"), dict) else {}
    attributions_by_work = step_join.get("attributions_by_work") or {}
    usage_event_models = step_join.get("usage_event_models") or {}

    steps = [
        _project_step(
            item,
            _step_models(str(item.get("work_id") or ""), attributions_by_work, usage_event_models),
        )
        for item in items
    ]

    descendants = [
        _descendant_row(candidate)
        for candidate in (view.get("rows") or [])
        if candidate.get("fold_top") == key
        and (candidate.get("client"), candidate.get("client_session_id")) != key
    ]
    # Enrich with the subagent's role + Task prompt read from its transcript
    # on disk (the same bounded, fail-soft reader the TUI detail uses) — an
    # untitled child otherwise renders as a bare id, which unravels nothing.
    if descendants:
        from .client_usage import _sanitized_session_title
        from .service import _redact_secret_spans
        from .subagent_roles import read_roles_for_children, scan_enabled

        if scan_enabled():
            # NB: a dedicated loop variable — reusing ``row`` here shadowed the
            # selected session row and served the LAST descendant as the
            # session (self-caught while validating against the live store).
            child_ids = [str(child.get("client_session_id") or "") for child in descendants]
            roles = read_roles_for_children(session_id, child_ids)
            for child in descendants:
                role = roles.get(str(child.get("client_session_id") or ""))
                if role is not None:
                    child["agent_type"] = role.agent_type
                    # The wire gets a LABEL, not the prompt: first line,
                    # bounded, secret-spans redacted, then the same sanitizer
                    # session titles use. Full multi-KB Task prompts blew a
                    # live 179-descendant payload past 1MB and can carry
                    # secrets — the same redaction posture that keeps raw
                    # commands off this wire applies (adversarial-review
                    # findings).
                    label: str | None = None
                    if role.task:
                        # Redact BEFORE bounding: truncating first could split a
                        # secret so the remaining prefix falls under a pattern's
                        # min-length floor and slips through un-redacted. Redact
                        # the whole first line, then bound the sanitized text.
                        # _sanitized_session_title returns None when the line
                        # sanitizes to empty (e.g. a first line of only
                        # control/format chars, which survive .strip()); guard
                        # it — None[:160] would 500 the whole detail response.
                        first_line = role.task.splitlines()[0]
                        redacted, _classes = _redact_secret_spans(first_line)
                        sanitized = _sanitized_session_title(redacted)
                        label = sanitized[:160] if sanitized else None
                    child["task"] = label

    plan_context = view.get("plan_context") if isinstance(view.get("plan_context"), dict) else {}
    weights = (plan_context.get("weights_by_client") or {}).get(client)
    records = (plan_context.get("records_by_client") or {}).get(client) or []
    plan: dict[str, Any]
    if weights is not None:
        plan = dict(plan_status_entry(weights))
        if weights.confidence == "calibrated":
            components = session_components_by_model(
                client=client, session_id=session_id, records=records
            )
            plan["by_model"] = sorted(
                (
                    {
                        # total = the REAL token count (incl. cache); the pct
                        # weighs the components (cache reads discounted).
                        "model": model,
                        "total_tokens": parts["total"],
                        "pct": weights.pct_for_components(
                            {model: parts["fresh"]}, {model: parts["cache_read"]}
                        ),
                    }
                    for model, parts in components.items()
                ),
                key=lambda entry: (-entry["pct"], entry["model"]),
            )
        else:
            plan["by_model"] = None
    else:
        # Not a plan-bearing client: an explicit no-plan block, not a guess —
        # SAME key set as plan_status_entry so the schema shape is uniform
        # across clients on this endpoint (adversarial-review finding).
        plan = {"client": client, "confidence": None, "calibration_state": None,
                "calibratable": False, "basis": None, "scale": None, "alpha": None,
                "intervals_used": None, "intervals_needed": None, "raw_scale": None,
                "trusted_band": None, "state_detail": None, "by_model": None}
    plan["pct_own"] = row.get("plan_pct_own")
    plan["pct_children"] = row.get("plan_pct_children")
    plan["pct"] = row.get("plan_pct")

    session = {name: value for name, value in row.items() if name not in _VIEW_INTERNAL_KEYS}
    return {
        "schema": V1_SESSION_DETAIL_SCHEMA_VERSION,
        "generated_at": view.get("generated_at"),
        "session": session,
        "steps": steps,
        "descendants": descendants,
        "plan": plan,
    }
