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

# Same TTL rationale as the glance cache: the view is mostly event-derived, but
# plan calibration windows are clock-relative (a 21-day fit ages), so an
# unchanged event list must still refresh eventually.
V1_SESSIONS_CACHE_MAX_AGE_SECONDS = 60.0

# Session-level status reduction — identical precedence to the glance recents
# and the TUI badge: blocked > handed_off > in_progress > completed. ``resolved``
# is a terminal blocker-resolution state and counts as completed.
_OPEN_ITEM_STATUSES = frozenset({"started", "checkpoint"})
_DONE_ITEM_STATUSES = frozenset({"completed", "resolved"})


def _reduce_work_status(items: Any) -> str | None:
    """One session badge from its work items' latest statuses.

    A later completed section must never erase a still-open or blocked one —
    a finished-looking label on unfinished work is exactly the dishonesty
    class this product exists to fix (same rule as glance._recent_sessions).
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


def _parent_key(entry: dict[str, Any]) -> tuple[str, str] | None:
    """``(client, parent_session_id)`` for a child entry, else None."""

    related = entry.get("related")
    parent = related.get("parent") if isinstance(related, dict) else None
    if not isinstance(parent, dict):
        return None
    parent_id = str(parent.get("client_session_id") or "")
    if not parent_id:
        return None
    return (str(entry.get("client") or ""), parent_id)


def _project_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """The curated wire row for one rollup entry (plan fields joined later).

    Blocks the app renders (``usage``/``join``/``work``/``related``) pass
    through verbatim — they are already-built read models with their own
    honesty semantics (None-not-zero costs, evidence enums); re-projecting
    them here would just be a second place for those rules to drift.
    """

    work = entry.get("work") if isinstance(entry.get("work"), dict) else {}
    # Title fallback chain, mirroring the glance: the client's own transcript
    # title when it recorded one, else the most recently started work item's
    # title (a section-only session still deserves a human label).
    title = entry.get("client_session_title")
    if not title:
        items = work.get("items") if isinstance(work.get("items"), list) else []
        for item in reversed(items):
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


def build_v1_sessions_view(ledger: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """The cacheable enriched view: every rollup entry projected + plan-joined.

    Unfiltered and unsliced so one build serves any ``roots_only``/paging
    combination. Plan shares are the calibrated-or-nothing per-session
    percentages; a root's ``plan_pct`` sums its own share and its children's
    (looked up through the rollup's own parent linkage — the ledger is the
    authority on who is whose child on this surface), so hiding the children
    never hides their weekly-plan consumption. Raw floats, never rounded here
    (shells format like the TUI: ``≈{pct:.1f}%`` with a ``<0.1%`` band).
    """

    from .glance import plan_status_and_session_pcts

    rollup = ledger.get("session_rollup") if isinstance(ledger, dict) else {}
    entries = rollup.get("sessions") if isinstance(rollup, dict) else []
    entries = [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []

    plan, session_pcts = plan_status_and_session_pcts(events)

    rows: list[dict[str, Any]] = []
    own_pct_by_key: dict[tuple[str, str], float] = {}
    children_pct_by_parent: dict[tuple[str, str], float] = {}
    for entry in entries:
        key = (str(entry.get("client") or ""), str(entry.get("client_session_id") or ""))
        own = session_pcts.get(key)
        if own is not None:
            own_pct_by_key[key] = own
            parent = _parent_key(entry)
            if parent is not None:
                children_pct_by_parent[parent] = children_pct_by_parent.get(parent, 0.0) + own

    root_count = 0
    for entry in entries:
        row = _project_entry(entry)
        key = (str(entry.get("client") or ""), str(entry.get("client_session_id") or ""))
        is_root = _parent_key(entry) is None
        if is_root:
            root_count += 1
        own = own_pct_by_key.get(key)
        children = children_pct_by_parent.get(key)
        row["plan_pct_own"] = own
        row["plan_pct_children"] = children
        if own is not None or children is not None:
            row["plan_pct"] = (own or 0.0) + (children or 0.0)
        row["is_root"] = is_root
        rows.append(row)

    return {
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
    generated_at: float,
) -> dict[str, Any]:
    """The wire envelope for one request — a filter + slice over the cached view.

    Every cut is disclosed: ``total_sessions``/``total_root_sessions`` describe
    the full store, ``filtered_total`` the population the offset/limit walk,
    and ``truncated`` says whether rows exist beyond this page — a consumer can
    never mistake a page for the whole population.
    """

    rows = view.get("rows") or []
    pool = [row for row in rows if row.get("is_root")] if roots_only else list(rows)
    page = pool[offset : offset + limit]
    # ``is_root`` is view-internal bookkeeping (the wire has related.parent);
    # strip it without mutating the cached rows.
    page = [{key: value for key, value in row.items() if key != "is_root"} for row in page]
    return {
        "schema": V1_SESSIONS_SCHEMA_VERSION,
        "generated_at": generated_at,
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
