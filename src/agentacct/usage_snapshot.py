"""Shared usage & rate-limit snapshot layer.

Pure functions and typed dataclasses computed over an already-loaded event list.
``agentacct now``, ``agentacct limits`` and the Textual TUI all consume this one
module so the three surfaces derive identical numbers from a single place — the
authoritative local event log, with no credentials or API calls.

Data paths (factored out of the CLI commands verbatim, so behavior is unchanged):

* **usage/cost** — ``_build_usage_view([], events)`` (:mod:`agentacct.api`) maps
  events to usage records (``model_usage`` only; session / diagnostic / work
  events are excluded), then :func:`agentacct.usage_cube.build_usage_cube` with
  ``days=N`` scopes each calendar window. Expressing a window as ``days=N``
  routes every row through ``usage_bucket_date``, so future / absurd timestamps
  are excluded exactly as the dashboard's usage summary does (a hand-rolled
  ``>= cutoff`` filter would not). Cost completeness is read from the cube's own
  ``cost_complete`` flag — never inferred from the mere presence of an estimate
  (``estimated_cost_usd`` stays non-``None`` even when additive rows are unpriced,
  so presence would over-claim completeness).

* **limits** — the latest ``rate_limit_observed`` event per ``(client, org,
  run_id)`` stream from the same event list (see :mod:`agentacct.rate_limits`).
  The provider-reported windows carry ``used_percent`` + ``resets_at`` and drive
  the live bars and reset countdown.

The heavy imports (:mod:`agentacct.api`, :mod:`agentacct.usage_cube`,
:mod:`agentacct.rate_limits`) are deferred into the functions that need them so
importing this module stays cheap for the CLI's cold start.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Optional


# ---------------------------------------------------------------------------
# window vocabulary (shared by `now` and the TUI)
# ---------------------------------------------------------------------------

# Calendar windows shown in the overview, newest-first. ``None`` days = all time.
NOW_WINDOWS: tuple[tuple[str, Optional[int]], ...] = (
    ("today", 1),
    ("last 7 days", 7),
    ("last 30 days", 30),
    ("all time", None),
)
# User ``--window`` tokens → the calendar window label above. Mirrors the
# dashboard's usage summary, which scopes by trailing calendar days.
NOW_WINDOW_ALIASES: dict[str, str] = {
    "today": "today",
    "24h": "today",
    "7d": "last 7 days",
    "30d": "last 30 days",
    "all": "all time",
}
NOW_WINDOW_DAYS: dict[str, Optional[int]] = {label: days for label, days in NOW_WINDOWS}

# Origin → a short human label distinguishing the Claude feeds.
ORIGIN_LABELS: dict[str, str] = {
    "claude_plan_usage": "desktop app",
    "claude_statusline": "CLI",
}

# Short labels for the two canonical rate-limit windows.
WINDOW_KIND_LABELS: dict[str, str] = {"5h": "5h", "7d": "7d"}


# ---------------------------------------------------------------------------
# pure formatters (reused by the CLI renderers and the TUI)
# ---------------------------------------------------------------------------


def finite(value: Any) -> float | None:
    """The value as a finite float, or ``None`` (rejects bool, inf, nan)."""

    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
        return float(value)
    return None


def format_tokens(value: Any) -> str:
    """A thousands-separated integer, or an em-dash when not a real number."""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{int(value):,}"
    return "—"


def cost_text(bucket: Mapping[str, Any]) -> str:
    """A cost cell for a cube bucket.

    The complete estimate (``$X.XX``) is shown ONLY when the cube vouches for
    completeness via ``cost_complete`` (every additive row priced, none held) —
    the mere presence of ``estimated_cost_usd`` does NOT imply completeness, since
    it is a priced-rows-only sum that ignores unpriced additive rows. Otherwise
    the priced subtotal is shown as partial (``~$``), or an em-dash when nothing
    is priced. Non-finite values degrade to an em-dash.
    """

    if bucket.get("cost_complete"):
        complete = finite(bucket.get("estimated_cost_usd"))
        if complete is not None:
            return f"${complete:.2f}"
    known = finite(bucket.get("known_additive_cost_usd"))
    if known is not None:
        return f"~${known:.2f}"
    return "—"


def usage_bar(used_percent: float, width: int = 20) -> str:
    """A unicode fill bar for a 0..100 percentage (clamped)."""

    pct = max(0.0, min(100.0, float(used_percent)))
    filled = int(round(pct / 100.0 * width))
    return "█" * filled + "░" * (width - filled)


def ratio_bar(value: Any, max_value: Any, width: int = 20) -> str:
    """A unicode fill bar for ``value`` scaled to ``max_value`` (the row's share
    of the busiest bucket). An empty / non-positive max yields an all-empty bar so
    a zero-usage window never draws a full bar. Non-finite inputs degrade to 0."""

    numerator = finite(value) or 0.0
    denominator = finite(max_value) or 0.0
    if denominator <= 0.0 or numerator <= 0.0:
        return "░" * width
    filled = int(round(max(0.0, min(1.0, numerator / denominator)) * width))
    return "█" * filled + "░" * (width - filled)


def humanize_seconds(seconds: float) -> str:
    """A compact ``2d 3h`` / ``4h 5m`` / ``6m`` / ``<1m`` duration string."""

    total = int(max(0, seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return "<1m"


def window_label(window: Mapping[str, Any]) -> str:
    """A human label for a rate-limit window (``5-hour`` / ``7-day`` / ``Nm``)."""

    labels = {"5h": "5-hour", "7d": "7-day"}
    kind = str(window.get("kind") or "")
    if kind in labels:
        return labels[kind]
    minutes = finite(window.get("window_minutes"))
    if minutes is not None:
        return f"{int(minutes)}m"
    return "window"


# ---------------------------------------------------------------------------
# rate-limit selection (pure over events)
# ---------------------------------------------------------------------------


def latest_limit_events(
    events: list[dict[str, Any]], *, client: str | None = None
) -> list[dict[str, Any]]:
    """Latest ``rate_limit_observed`` event per ``(client, org, run_id)`` stream.

    Ordered by the provider observation time (``metadata.captured_at``, falling
    back to ``created_at``). Sorted deterministically by ``(client, org)`` for a
    stable render. ``client`` filters to one client when given.
    """

    from .rate_limits import EVENT_TYPE

    latest: dict[tuple[Any, Any, Any], tuple[float, dict[str, Any]]] = {}
    for event in events:
        if event.get("event_type") != EVENT_TYPE:
            continue
        metadata = event.get("metadata") or {}
        event_client = metadata.get("client")
        if client and event_client != client:
            continue
        key = (event_client, metadata.get("org"), event.get("run_id"))
        captured = metadata.get("captured_at")
        if not isinstance(captured, (int, float)) or isinstance(captured, bool):
            created = event.get("created_at")
            captured = float(created) if isinstance(created, (int, float)) else 0.0
        ordering = float(captured)
        current = latest.get(key)
        if current is None or ordering >= current[0]:
            latest[key] = (ordering, event)
    return [
        event
        for _key, (_ordering, event) in sorted(
            latest.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))
        )
    ]


def limit_json_entry(event: Mapping[str, Any]) -> dict[str, Any]:
    """The public JSON shape for one latest-limit event.

    Passes the raw window dicts through verbatim so ``now --json`` and
    ``limits --json`` stay byte-for-byte stable.
    """

    metadata = event.get("metadata") or {}
    return {
        "client": metadata.get("client"),
        "origin": metadata.get("origin"),
        "plan_type": metadata.get("plan_type"),
        "org": metadata.get("org"),
        "captured_at": metadata.get("captured_at"),
        "windows": metadata.get("windows") or [],
        "credits": metadata.get("credits"),
        "reached_type": metadata.get("reached_type"),
        "source_file": metadata.get("source_file"),
    }


def limit_teaser_lines(
    events: list[dict[str, Any]], *, client: str | None = None
) -> list[str]:
    """Compact one-line-per-client limit readings (``codex: 5h 12% · 7d 26%``).

    Best-effort: never raises (a teaser must not be able to break ``now``).
    """

    try:
        snapshots = latest_limit_events(events, client=client)
    except Exception:  # noqa: BLE001 - a teaser must never break its caller.
        return []
    lines: list[str] = []
    for event in snapshots:
        metadata = event.get("metadata") or {}
        parts: list[str] = []
        for window in metadata.get("windows") or []:
            if not isinstance(window, Mapping):
                continue
            used = window.get("used_percent")
            if not isinstance(used, (int, float)) or isinstance(used, bool):
                continue
            label = WINDOW_KIND_LABELS.get(str(window.get("kind") or ""), "win")
            parts.append(f"{label} {float(used):.0f}%")
        if parts:
            lines.append(f"{metadata.get('client') or '?'}: " + " · ".join(parts))
    return lines


# ---------------------------------------------------------------------------
# typed snapshots (the TUI's data model)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageWindow:
    """One calendar window's totals bucket (from the cube)."""

    label: str
    days: Optional[int]
    totals: dict[str, Any]


@dataclass(frozen=True)
class UsageSnapshot:
    """The usage/cost half of a live snapshot.

    ``windows`` holds the four calendar overviews; ``by_client`` / ``by_model``
    are the cube breakdown for the selected ``breakdown_window``.
    """

    as_of: Optional[float]
    generated_at: float
    event_count: int
    usage_record_count: int
    client_filter: Optional[str]
    windows: list[UsageWindow]
    breakdown_window: str
    by_client: list[dict[str, Any]]
    by_model: list[dict[str, Any]]


@dataclass(frozen=True)
class UsagePage:
    """The dedicated usage screen's data: a period time series + model detail.

    ``by_period`` / ``by_model`` / ``totals`` are the raw cube buckets for the
    selected trailing-``range_days`` range (``None`` = all time), at the resolved
    ``granularity`` (daily for short ranges, weekly for long ones). Kept as plain
    dicts — the same shape the HTML dashboard's Theme A cluster rendered — so the
    screen renders straight from the cube with no re-derivation.
    """

    range_days: Optional[int]
    granularity: str
    as_of: Optional[float]
    generated_at: float
    usage_record_count: int
    client_filter: Optional[str]
    totals: dict[str, Any]
    by_period: list[dict[str, Any]]
    by_model: list[dict[str, Any]]


@dataclass(frozen=True)
class LimitWindow:
    """One provider rate-limit window, normalized for display."""

    kind: str
    label: str
    used_percent: Optional[float]
    window_minutes: Optional[int]
    resets_at: Optional[int]


@dataclass(frozen=True)
class ClientLimit:
    """The latest provider-limit reading for one client stream."""

    client: str
    origin: Optional[str]
    origin_label: Optional[str]
    plan_type: Optional[str]
    org: Optional[str]
    captured_at: Optional[float]
    windows: list[LimitWindow]
    credits: Optional[Mapping[str, Any]]
    reached_type: Optional[str]
    source_file: Optional[str]
    raw_event: Mapping[str, Any] = field(repr=False, default_factory=dict)

    def to_json_entry(self) -> dict[str, Any]:
        """The byte-stable public JSON shape (raw windows passed through)."""

        return limit_json_entry(self.raw_event)


@dataclass(frozen=True)
class LiveSnapshot:
    """Everything the TUI renders in one refresh tick."""

    usage: UsageSnapshot
    limits: list[ClientLimit]


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def usage_records(events: list[dict[str, Any]], *, client: str | None = None) -> list[Any]:
    """Map events to dashboard usage records (optionally filtered to one client).

    Includes both additive and held (excluded) rows — exactly the set the cube
    needs so held usage is still counted toward availability.
    """

    from .api import _build_usage_view

    view = _build_usage_view([], events)
    records = [*view.saved_records, *view.excluded_saved_records]
    if client is not None:
        records = [r for r in records if getattr(r, "client", None) == client]
    return records


def _captured_at(event: Mapping[str, Any]) -> float | None:
    metadata = event.get("metadata") or {}
    captured = metadata.get("captured_at")
    if isinstance(captured, (int, float)) and not isinstance(captured, bool):
        return float(captured)
    created = event.get("created_at")
    if isinstance(created, (int, float)) and not isinstance(created, bool):
        return float(created)
    return None


def build_usage_snapshot(
    events: list[dict[str, Any]],
    *,
    client: str | None = None,
    breakdown_window: str = "7d",
    now: float | None = None,
    today: date | None = None,
) -> UsageSnapshot:
    """Build the usage/cost snapshot: four calendar windows plus the selected
    window's by-client / by-model breakdown.

    ``breakdown_window`` is a ``--window`` token (``today`` / ``24h`` / ``7d`` /
    ``30d`` / ``all``). Raises ``ValueError`` for an unknown token so callers can
    surface a clear error. ``now`` / ``today`` are injectable for tests.
    """

    from .api import _usage_record_time
    from .usage_cube import build_usage_cube

    if breakdown_window not in NOW_WINDOW_ALIASES:
        raise ValueError("breakdown_window must be one of: today, 24h, 7d, 30d, all")

    records = usage_records(events, client=client)
    now_epoch = now if now is not None else time.time()
    day = today or date.today()

    def _cube_for_days(days: int | None) -> dict[str, Any]:
        return build_usage_cube(records, record_time=_usage_record_time, days=days, today=day)

    windows = [
        UsageWindow(label=label, days=days, totals=_cube_for_days(days).get("totals") or {})
        for label, days in NOW_WINDOWS
    ]

    primary_label = NOW_WINDOW_ALIASES[breakdown_window]
    primary_cube = _cube_for_days(NOW_WINDOW_DAYS[primary_label])

    # Freshness: newest record with a real (finite, not-future) timestamp, so an
    # unknown (0.0) or absurd future time can't fake a "<1m ago".
    real_times = [
        t
        for r in records
        if (t := _usage_record_time(r)) is not None
        and isinstance(t, (int, float))
        and math.isfinite(float(t))
        and 0 < t <= now_epoch
    ]
    as_of = max(real_times) if real_times else None

    return UsageSnapshot(
        as_of=as_of,
        generated_at=now_epoch,
        event_count=len(events),
        usage_record_count=len(records),
        client_filter=client,
        windows=windows,
        breakdown_window=primary_label,
        by_client=primary_cube.get("by_client") or [],
        by_model=primary_cube.get("by_model") or [],
    )


def build_usage_page(
    events: list[dict[str, Any]],
    *,
    client: str | None = None,
    days: int | None = 30,
    granularity: str | None = None,
    now: float | None = None,
    today: date | None = None,
) -> UsagePage:
    """Build the dedicated usage screen's data for a trailing ``days`` range.

    ``days=None`` = all time. ``granularity`` defaults to the cube's own rule
    (daily for 7/30-day ranges, weekly for 90/all) via ``resolve_granularity`` so
    a long range collapses to readable weekly buckets instead of hundreds of days.
    Reuses the exact ``now`` data path (``usage_records`` → ``build_usage_cube``)
    so the page can never disagree with the overview. ``now`` / ``today`` are
    injectable for tests.
    """

    from .api import _usage_record_time
    from .usage_cube import build_usage_cube, resolve_granularity

    records = usage_records(events, client=client)
    now_epoch = now if now is not None else time.time()
    day = today or date.today()
    days_choice = "all" if days is None else str(days)
    gran = granularity or resolve_granularity(days_choice, "auto")

    cube = build_usage_cube(
        records, record_time=_usage_record_time, days=days, granularity=gran, today=day
    )

    # Freshness: newest record with a real (finite, not-future) timestamp — the
    # same honest "as of" rule the overview uses (an unknown 0.0 or an absurd
    # future time can't fake a "<1m ago").
    real_times = [
        t
        for r in records
        if (t := _usage_record_time(r)) is not None
        and isinstance(t, (int, float))
        and math.isfinite(float(t))
        and 0 < t <= now_epoch
    ]
    as_of = max(real_times) if real_times else None

    return UsagePage(
        range_days=days,
        granularity=gran,
        as_of=as_of,
        generated_at=now_epoch,
        usage_record_count=len(records),
        client_filter=client,
        totals=cube.get("totals") or {},
        by_period=cube.get("by_period") or [],
        by_model=cube.get("by_model") or [],
    )


def build_client_limits(
    events: list[dict[str, Any]], *, client: str | None = None
) -> list[ClientLimit]:
    """Parse the latest provider-limit events into typed :class:`ClientLimit`
    objects for display (the TUI). JSON parity is served separately via
    :func:`limit_json_entry` / :meth:`ClientLimit.to_json_entry`."""

    results: list[ClientLimit] = []
    for event in latest_limit_events(events, client=client):
        metadata = event.get("metadata") or {}
        windows: list[LimitWindow] = []
        for window in metadata.get("windows") or []:
            if not isinstance(window, Mapping):
                continue
            # Guard window_minutes / resets_at through finite() before int() — a
            # bare int() on a non-finite float raises (int(inf) -> OverflowError,
            # int(nan) -> ValueError) and would abort the whole build. The
            # recording path already drops non-finite values, but events are read
            # via json.loads, which accepts bare Infinity/NaN, so a corrupted or
            # hand-injected event must not be able to crash the display layer.
            minutes = finite(window.get("window_minutes"))
            resets = finite(window.get("resets_at"))
            windows.append(
                LimitWindow(
                    kind=str(window.get("kind") or ""),
                    label=window_label(window),
                    used_percent=finite(window.get("used_percent")),
                    window_minutes=int(minutes) if minutes is not None else None,
                    resets_at=int(resets) if resets is not None else None,
                )
            )
        origin = metadata.get("origin")
        credits = metadata.get("credits")
        results.append(
            ClientLimit(
                client=str(metadata.get("client") or "?"),
                origin=origin,
                origin_label=ORIGIN_LABELS.get(str(origin or "")),
                plan_type=metadata.get("plan_type"),
                org=metadata.get("org"),
                captured_at=_captured_at(event),
                windows=windows,
                credits=credits if isinstance(credits, Mapping) else None,
                reached_type=metadata.get("reached_type"),
                source_file=metadata.get("source_file"),
                raw_event=event,
            )
        )
    return results


def build_live_snapshot(
    events: list[dict[str, Any]],
    *,
    client: str | None = None,
    breakdown_window: str = "7d",
    now: float | None = None,
    today: date | None = None,
) -> LiveSnapshot:
    """The full snapshot the TUI renders each tick: usage + limits from one
    already-loaded event list (one scan, both halves)."""

    return LiveSnapshot(
        usage=build_usage_snapshot(
            events, client=client, breakdown_window=breakdown_window, now=now, today=today
        ),
        limits=build_client_limits(events, client=client),
    )


__all__ = [
    "NOW_WINDOWS",
    "NOW_WINDOW_ALIASES",
    "NOW_WINDOW_DAYS",
    "ORIGIN_LABELS",
    "WINDOW_KIND_LABELS",
    "finite",
    "format_tokens",
    "cost_text",
    "usage_bar",
    "ratio_bar",
    "humanize_seconds",
    "window_label",
    "latest_limit_events",
    "limit_json_entry",
    "limit_teaser_lines",
    "UsageWindow",
    "UsageSnapshot",
    "UsagePage",
    "LimitWindow",
    "ClientLimit",
    "LiveSnapshot",
    "usage_records",
    "build_usage_snapshot",
    "build_usage_page",
    "build_client_limits",
    "build_live_snapshot",
]
