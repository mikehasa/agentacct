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

V1_SESSIONS_SCHEMA_VERSION = "agentacct.v1-sessions.v1"

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
    entry: dict[str, Any], existing_keys: set[tuple[str, str]]
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
    prefix root when that root exists — the same defensive fallback the
    glance applies, so the two /v1 surfaces agree on the root's headline
    number instead of the sessions lane re-counting legacy children as roots.
    """

    client = str(entry.get("client") or "")
    related = entry.get("related")
    parent = related.get("parent") if isinstance(related, dict) else None
    if isinstance(parent, dict):
        if related.get("parent_source_namespace_mismatch"):
            return None
        parent_id = str(parent.get("client_session_id") or "")
        if parent_id:
            key = (client, parent_id)
            return key if key in existing_keys else None
        return None
    session_id = str(entry.get("client_session_id") or "")
    if ":" in session_id:
        key = (client, session_id.split(":", 1)[0])
        if key in existing_keys:
            return key
    return None


def _project_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """The curated wire row for one rollup entry (plan fields joined later).

    Blocks the app renders (``usage``/``join``/``work``/``related``) pass
    through verbatim — they are already-built read models with their own
    honesty semantics (None-not-zero costs, evidence enums); re-projecting
    them here would just be a second place for those rules to drift.
    """

    work = entry.get("work") if isinstance(entry.get("work"), dict) else {}
    # Title fallback chain, mirroring the glance: the client's own transcript
    # title when it recorded one, else the newest work item's title (a
    # section-only session still deserves a human label). work.items arrives
    # NEWEST-FIRST from the rollup (build_work_items sorts by updated_at
    # descending) — iterate forward; reversed() picked the oldest title
    # (adversarial-review finding).
    title = entry.get("client_session_title")
    if not title:
        items = work.get("items") if isinstance(work.get("items"), list) else []
        for item in items:
            if isinstance(item, dict) and item.get("title"):
                title = item["title"]
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
        "observed_models": entry.get("observed_models"),
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

    from .glance import plan_status_and_session_pcts

    rollup = ledger.get("session_rollup") if isinstance(ledger, dict) else {}
    entries = rollup.get("sessions") if isinstance(rollup, dict) else []
    entries = [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []

    plan, session_pcts = plan_status_and_session_pcts(events)

    def _key(entry: dict[str, Any]) -> tuple[str, str]:
        return (str(entry.get("client") or ""), str(entry.get("client_session_id") or ""))

    existing_keys = {_key(entry) for entry in entries}
    fold_step = {_key(entry): _fold_parent_key(entry, existing_keys) for entry in entries}

    def _fold_top(key: tuple[str, str]) -> tuple[str, str]:
        # Resolve to the topmost existing ancestor (rows normally nest one
        # level; the loop guards hostile chains/cycles rather than trusting
        # that). A share must land on a row the roots page actually shows.
        seen = {key}
        while True:
            parent = fold_step.get(key)
            if parent is None or parent in seen:
                return key
            seen.add(parent)
            key = parent

    descendant_pct_by_top: dict[tuple[str, str], float] = {}
    for entry in entries:
        key = _key(entry)
        own = session_pcts.get(key)
        if own is None:
            continue
        top = _fold_top(key)
        if top != key:
            descendant_pct_by_top[top] = descendant_pct_by_top.get(top, 0.0) + own

    root_count = 0
    rows: list[dict[str, Any]] = []
    for entry in entries:
        row = _project_entry(entry)
        key = _key(entry)
        is_root = fold_step.get(key) is None
        if is_root:
            root_count += 1
        own = session_pcts.get(key)
        descendants = descendant_pct_by_top.get(key)
        row["plan_pct_own"] = own
        row["plan_pct_children"] = descendants
        if own is not None or descendants is not None:
            row["plan_pct"] = (own or 0.0) + (descendants or 0.0)
        row["is_root"] = is_root
        rows.append(row)

    return {
        "generated_at": _time.time() if now is None else float(now),
        "rows": rows,
        "plan": plan,
        "total_sessions": len(rows),
        "total_root_sessions": root_count,
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
    # ``is_root`` is view-internal bookkeeping (the wire has related.parent);
    # strip it without mutating the cached rows.
    page = [{key: value for key, value in row.items() if key != "is_root"} for row in page]
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
