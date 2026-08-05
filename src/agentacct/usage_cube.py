"""Usage cube — the pure Theme A aggregation over saved usage rows (PRD §5.4).

Aggregates SAVED usage rows by client, by (client, provider, model), and by
local-day / ISO-week period, with a date-range filter. The caller feeds it the
same ``DashboardUsageRecord`` intake every dashboard surface shares (trusted
local import rows only — diagnostic events and shadowed legacy rows can never
enter), plus the ONE canonical row-timestamp rule (``usage_view._usage_record_time``)
as ``record_time``; nothing here re-derives identity, trust, or time semantics.

Pure functions: no store access, no live scan, and no clock access unless the
caller omits ``today``. This module is what `/tokens`, the Overview Theme A
cluster, and `GET /usage/summary` all render — one aggregation, three surfaces.

Honesty contract carried in every bucket (PRD §10):
- ``fresh_tokens`` remains the compatibility/efficiency field for input +
  output, while product views lead with ``total_tokens_including_cached``.
- Cache-creation and cache-read tokens ride in their own fields. A bucket also
  says whether each counter was reported, partially reported, not reported,
  or remains unknown for a historical row; unsupported/unknown fields are
  never presented as measured zeroes.
- ``estimated_cost_usd`` is None (never $0.00) when no row in the bucket
  carried a cost estimate; ``cost_confidence`` is the dominant priced-row
  confidence with an explicit ``cost_confidence_mixed`` flag.
- ``sessions`` counts DISTINCT (client, base session id) pairs — per-model
  lane rows of one session never double-count it.
- Rows whose timestamps fail the bad-timestamp guard are excluded from
  date-bounded ranges (a bounded range cannot honestly claim them) and
  reported via ``totals.unknown_time_rows``; with ``days=None`` they stay in
  the totals under the explicit "unknown" period.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Callable, Iterable

# frozen: historical stores/logs/files carry this schema string forever.
USAGE_SUMMARY_SCHEMA_VERSION = "agent-sentinel.usage-summary.v1"

# The measured local-usage lanes. Cursor is intentionally absent because its
# adapter emits session observations only, never a token/cost row.
KNOWN_USAGE_CLIENTS = ("claude-code", "codex", "hermes", "opencode", "openclaw")
# Shared visual/client vocabulary also covers observation-only adapters.
KNOWN_LOCAL_CLIENTS = (*KNOWN_USAGE_CLIENTS, "cursor")

USAGE_CUBE_DAYS_CHOICES = ("7", "30", "90", "all")
USAGE_CUBE_GRANULARITY_CHOICES = ("daily", "weekly")

# Period key for rows whose timestamp failed the guard (days=None only).
UNKNOWN_PERIOD = "unknown"

# Empty-period gap fill covers at most this many trailing periods so one
# absurd-but-parseable ancient timestamp cannot balloon by_period into
# thousands of zero rows; real sparse buckets outside the fill are kept.
MAX_FILLED_PERIODS = 730

_COST_CONFIDENCE_ORDER = ("provider_billed", "client_reported", "estimated_from_tokens", "unknown")


def client_lane_class(client: Any) -> str:
    """CSS lane class for a platform: the 5 known clients + 'other'.

    Shared by badges, CSS ranked bars, and the SVG chart rects so one client
    is always one color everywhere.
    """

    text = str(client or "")
    return f"lane-{text}" if text in KNOWN_LOCAL_CLIENTS else "lane-other"


def normalized_cost_confidence(value: Any) -> str:
    text = str(value or "").strip()
    if text == "exact":
        return "provider_billed"
    if text == "estimated":
        return "estimated_from_tokens"
    return text or "unknown"


def normalized_usage_confidence(value: Any) -> str:
    text = str(value or "").strip()
    if text == "exact":
        return "provider_reported"
    return text or "unknown"


def usage_bucket_date(timestamp: Any) -> date | None:
    """Local calendar day for a row timestamp, or None (bad-timestamp guard).

    Same tolerance rule as the dashboard's time formatting: client-authored
    timestamps can be absurd-but-finite (ms epoch, year 99999, 1e300) and must
    bucket as unknown — never crash a page or endpoint.
    """

    try:
        ts = float(timestamp)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(ts) or ts <= 0:
        return None
    try:
        return date.fromtimestamp(ts)
    except (OSError, OverflowError, ValueError):
        return None


def week_start(day: date) -> date:
    """Monday of ``day``'s ISO week — weekly buckets are labeled by week start."""

    return day - timedelta(days=day.isoweekday() - 1)


def resolve_granularity(days: str, granularity: str) -> str:
    """Locked decision (PRD §11.6): daily for 7/30-day ranges, weekly for
    90/all, with an explicit ``granularity=daily|weekly`` override param
    (any other value, incl. "auto", falls through to the range rule)."""

    if granularity in USAGE_CUBE_GRANULARITY_CHOICES:
        return granularity
    return "weekly" if days in {"90", "all"} else "daily"


def days_choice_to_int(days: str) -> int | None:
    """Whitelisted ``days`` choice -> the cube's int|None range ('all' -> None)."""

    return None if days == "all" else int(days)


def models_in_records(records: Iterable[Any]) -> list[str]:
    """Sorted distinct model names across ALL rows — the whitelist the model
    filter is validated against (an unknown model returns the empty result
    with the filter echoed, never a guess)."""

    return sorted({str(model) for record in records if (model := getattr(record, "model", None))})


def filter_usage_records(
    records: Iterable[Any],
    *,
    record_time: Callable[[Any], float],
    client: str | None = None,
    model: str | None = None,
    days: int | None = None,
    today: date | None = None,
) -> tuple[list[Any], int]:
    """(rows matching the filters, unknown-time row count) — the ONE filter
    rule shared by the cube and any per-record view (e.g. the /tokens
    by-model cost table), so a page can never show two different populations.

    ``days=N`` keeps rows whose local calendar day falls in the trailing N
    days ending ``today``. Unknown-time rows (bad-timestamp guard) are
    counted either way, but kept only when ``days`` is None — a bounded date
    range cannot honestly include a row with no usable date.
    """

    today = today or date.today()
    range_start = today - timedelta(days=days - 1) if days is not None else None
    kept: list[Any] = []
    unknown_time_rows = 0
    for record in records:
        if client is not None and str(getattr(record, "client", "") or "") != client:
            continue
        if model is not None and str(getattr(record, "model", "") or "") != model:
            continue
        day = usage_bucket_date(record_time(record))
        if day is None:
            unknown_time_rows += 1
            if days is None:
                kept.append(record)
            continue
        if range_start is not None and not (range_start <= day <= today):
            continue
        kept.append(record)
    return kept, unknown_time_rows


def _int_field(record: Any, name: str) -> int:
    try:
        value = int(getattr(record, name, 0) or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _new_accumulator() -> dict[str, Any]:
    return {
        "rows": 0,
        "additive_rows": 0,
        "excluded_non_additive_rows": 0,
        "session_keys": set(),
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens_including_cached": 0,
        "cache_creation_reported_rows": 0,
        "cache_creation_unreported_rows": 0,
        "cache_creation_unknown_rows": 0,
        "cache_read_reported_rows": 0,
        "cache_read_unreported_rows": 0,
        "cache_read_unknown_rows": 0,
        "estimated_cost_usd": 0.0,
        "priced_rows": 0,
        "unpriced_rows": 0,
        "confidence_rows": {},
        "confidence_costs": {},
        "models": set(),
    }


def _accumulate(accumulator: dict[str, Any], record: Any) -> None:
    accumulator["rows"] += 1
    accumulator["session_keys"].add(
        (str(getattr(record, "client", "") or ""), str(getattr(record, "session_id", "") or ""))
    )
    model = getattr(record, "model", None)
    if model:
        accumulator["models"].add(str(model))
    if getattr(record, "usage_additive", True) is not True:
        accumulator["excluded_non_additive_rows"] += 1
        return
    accumulator["additive_rows"] += 1
    accumulator["input_tokens"] += _int_field(record, "input_tokens")
    accumulator["output_tokens"] += _int_field(record, "output_tokens")
    accumulator["cache_creation_tokens"] += _int_field(record, "cache_creation_input_tokens")
    accumulator["cache_read_tokens"] += _int_field(record, "cache_read_input_tokens")
    accumulator["total_tokens_including_cached"] += _int_field(record, "total_tokens_including_cached")
    cache_creation_reported = getattr(record, "cache_creation_tokens_reported", None)
    capability_key = (
        "cache_creation_reported_rows"
        if cache_creation_reported is True
        else "cache_creation_unreported_rows"
        if cache_creation_reported is False
        else "cache_creation_unknown_rows"
    )
    accumulator[capability_key] += 1
    cache_read_reported = getattr(record, "cache_read_tokens_reported", None)
    cache_read_capability_key = (
        "cache_read_reported_rows"
        if cache_read_reported is True
        else "cache_read_unreported_rows"
        if cache_read_reported is False
        else "cache_read_unknown_rows"
    )
    accumulator[cache_read_capability_key] += 1
    cost = getattr(record, "estimated_cost_usd", None)
    if cost is None:
        accumulator["unpriced_rows"] += 1
        return
    accumulator["priced_rows"] += 1
    accumulator["estimated_cost_usd"] += float(cost)
    confidence = normalized_cost_confidence(getattr(record, "cost_confidence", None))
    accumulator["confidence_rows"][confidence] = accumulator["confidence_rows"].get(confidence, 0) + 1
    accumulator["confidence_costs"][confidence] = accumulator["confidence_costs"].get(confidence, 0.0) + float(cost)


def _authority_rank(key: str) -> int:
    return _COST_CONFIDENCE_ORDER.index(key) if key in _COST_CONFIDENCE_ORDER else len(_COST_CONFIDENCE_ORDER)


def dominant_cost_confidence(
    confidence_costs: dict[str, float], confidence_rows: dict[str, int]
) -> tuple[str | None, bool, str | None]:
    """THE shared dominant-confidence rule (PRD §10.2, one implementation for
    the cube, the Overview cost card, and every /tokens surface).

    Returns ``(dominant, mixed, display_label)``. Weights are summed cost per
    confidence; when nothing carries a positive cost, row counts are the
    fallback weight. "mostly X" is claimed ONLY when X is strictly dominant
    (> 50% of the weight); exact ties and no-majority splits render the mixed
    label naming every weighted label — and a tie NEVER resolves toward the
    higher-authority label (that would overstate cost certainty)."""

    weights = {key: cost for key, cost in confidence_costs.items() if cost > 0.0}
    if not weights:
        weights = {key: float(count) for key, count in confidence_rows.items() if count > 0}
    if not weights:
        return None, False, None
    # Ordered highest weight first; equal weights order lower-authority
    # first, so neither the dominant pick nor the naming order can lend a
    # tied label more authority than the data supports.
    ordered = sorted(weights, key=lambda key: (-weights[key], -_authority_rank(key), key))
    dominant = ordered[0]
    if len(ordered) == 1:
        return dominant, False, f"{dominant} confidence"
    if weights[dominant] * 2 > sum(weights.values()):
        return dominant, True, f"mixed confidence (mostly {dominant})"
    return dominant, True, f"mixed confidence ({' + '.join(ordered)})"


def _finalize(accumulator: dict[str, Any]) -> dict[str, Any]:
    confidence, mixed, confidence_label = dominant_cost_confidence(
        accumulator["confidence_costs"], accumulator["confidence_rows"]
    )
    reported_rows = int(accumulator["cache_creation_reported_rows"])
    unreported_rows = int(accumulator["cache_creation_unreported_rows"])
    unknown_rows = int(accumulator["cache_creation_unknown_rows"])
    cache_creation_reporting = _token_reporting_status(reported_rows, unreported_rows, unknown_rows)
    cache_read_reported_rows = int(accumulator["cache_read_reported_rows"])
    cache_read_unreported_rows = int(accumulator["cache_read_unreported_rows"])
    cache_read_unknown_rows = int(accumulator["cache_read_unknown_rows"])
    cache_read_reporting = _token_reporting_status(
        cache_read_reported_rows, cache_read_unreported_rows, cache_read_unknown_rows
    )
    excluded_rows = int(accumulator["excluded_non_additive_rows"])
    additive_rows = int(accumulator["additive_rows"])
    cost_complete = bool(additive_rows and not excluded_rows and not accumulator["unpriced_rows"])
    return {
        "rows": accumulator["rows"],
        "additive_rows": additive_rows,
        "excluded_non_additive_rows": excluded_rows,
        "sessions": len(accumulator["session_keys"]),
        "input_tokens": accumulator["input_tokens"],
        "output_tokens": accumulator["output_tokens"],
        "fresh_tokens": accumulator["input_tokens"] + accumulator["output_tokens"],
        "cache_creation_tokens": accumulator["cache_creation_tokens"],
        "cache_read_tokens": accumulator["cache_read_tokens"],
        "total_tokens_including_cached": accumulator["total_tokens_including_cached"],
        "cache_creation_reporting": cache_creation_reporting,
        "cache_creation_reported_rows": reported_rows,
        "cache_creation_unreported_rows": unreported_rows,
        "cache_creation_unknown_rows": unknown_rows,
        "cache_read_reporting": cache_read_reporting,
        "cache_read_reported_rows": cache_read_reported_rows,
        "cache_read_unreported_rows": cache_read_unreported_rows,
        "cache_read_unknown_rows": cache_read_unknown_rows,
        "estimated_cost_usd": accumulator["estimated_cost_usd"] if accumulator["priced_rows"] and not excluded_rows else None,
        "known_additive_cost_usd": accumulator["estimated_cost_usd"] if accumulator["priced_rows"] else None,
        "cost_complete": cost_complete,
        "usage_availability": (
            "partial"
            if additive_rows and excluded_rows
            else "held"
            if excluded_rows
            else "available"
            if additive_rows
            else "unknown"
        ),
        "cost_confidence": confidence,
        "cost_confidence_mixed": mixed,
        # Additive: the exact display string every HTML surface renders, so
        # /tokens, the Overview, and JSON consumers can never disagree on
        # how a mixed bucket is labeled.
        "cost_confidence_label": confidence_label,
        "priced_rows": accumulator["priced_rows"],
        "unpriced_rows": accumulator["unpriced_rows"],
        "models": sorted(accumulator["models"]),
    }


def _token_reporting_status(
    reported_rows: int, unreported_rows: int, unknown_rows: int = 0
) -> str | None:
    if reported_rows and (unreported_rows or unknown_rows):
        return "partial"
    if unknown_rows:
        return "unknown"
    if reported_rows:
        return "reported"
    if unreported_rows:
        return "not_reported"
    return None


def _filled_period_keys(dated_days: list[date], *, days: int | None, granularity: str, today: date) -> list[str]:
    """Every period key in range — a gap is information, so empty periods are
    enumerated (bounded ranges: the whole range; 'all': earliest dated row
    through today, capped at MAX_FILLED_PERIODS trailing periods)."""

    if days is not None:
        start, end = today - timedelta(days=days - 1), today
    elif dated_days:
        start, end = min(dated_days), today
    else:
        return []
    step = timedelta(days=7 if granularity == "weekly" else 1)
    if granularity == "weekly":
        start, end = week_start(start), week_start(end)
    if end < start:
        start = end
    span_periods = (end - start).days // step.days + 1
    if span_periods > MAX_FILLED_PERIODS:
        start = end - step * (MAX_FILLED_PERIODS - 1)
    keys: list[str] = []
    cursor = start
    while cursor <= end:
        keys.append(cursor.isoformat())
        cursor += step
    return keys


def build_usage_cube(
    records: Iterable[Any],
    *,
    record_time: Callable[[Any], float],
    client: str | None = None,
    model: str | None = None,
    days: int | None = None,
    granularity: str = "daily",
    today: date | None = None,
) -> dict[str, Any]:
    """The Theme A usage cube: {totals, by_client[], by_model[], by_period[]}.

    - ``by_client``: one bucket per platform with rows, sorted by total token
      volume including caches (client name breaks ties).
    - ``by_model``: one bucket per (client, provider, model), same order.
    - ``by_period``: one bucket per local day (daily) or ISO week labeled by
      its Monday (weekly), ascending, INCLUDING empty periods in range; each
      carries a ``by_client`` fresh/cache-creation/cache-read mini-split (the
      chart's stacking input, so JSON parity holds). The "unknown" period
      (days=None only) sorts last.
    - ``totals``: the same bucket shape over everything in filter, plus
      ``unknown_time_rows``.

    Every bucket: rows, sessions (distinct base session ids), input/output/
    fresh tokens, cache_creation/cache_read tokens,
    total_tokens_including_cached, estimated_cost_usd (None when nothing
    priced), dominant cost_confidence + cost_confidence_mixed,
    priced/unpriced_rows, models.
    """

    if granularity not in USAGE_CUBE_GRANULARITY_CHOICES:
        raise ValueError(f"unknown granularity: {granularity!r}")
    today = today or date.today()
    kept, unknown_time_rows = filter_usage_records(
        records, record_time=record_time, client=client, model=model, days=days, today=today
    )

    totals = _new_accumulator()
    by_client: dict[str, dict[str, Any]] = {}
    by_model: dict[tuple[str, str, str], dict[str, Any]] = {}
    by_period: dict[str, dict[str, Any]] = {}
    period_lanes: dict[str, dict[str, dict[str, Any]]] = {}
    dated_days: list[date] = []

    for record in kept:
        record_client = str(getattr(record, "client", "") or "")
        day = usage_bucket_date(record_time(record))
        if day is None:
            period_key = UNKNOWN_PERIOD
        else:
            dated_days.append(day)
            period_key = (week_start(day) if granularity == "weekly" else day).isoformat()
        model_key = (
            record_client,
            str(getattr(record, "provider", "") or ""),
            str(getattr(record, "model", "") or ""),
        )
        for accumulator in (
            totals,
            by_client.setdefault(record_client, _new_accumulator()),
            by_model.setdefault(model_key, _new_accumulator()),
            by_period.setdefault(period_key, _new_accumulator()),
        ):
            _accumulate(accumulator, record)
        lane = period_lanes.setdefault(period_key, {}).setdefault(
            record_client,
            {
                "rows": 0,
                "additive_rows": 0,
                "excluded_non_additive_rows": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "fresh_tokens": 0,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
                "total_tokens_including_cached": 0,
                "cache_creation_reported_rows": 0,
                "cache_creation_unreported_rows": 0,
                "cache_creation_unknown_rows": 0,
                "cache_read_reported_rows": 0,
                "cache_read_unreported_rows": 0,
                "cache_read_unknown_rows": 0,
            },
        )
        lane["rows"] += 1
        if getattr(record, "usage_additive", True) is not True:
            lane["excluded_non_additive_rows"] += 1
            continue
        lane["additive_rows"] += 1
        lane["input_tokens"] += _int_field(record, "input_tokens")
        lane["output_tokens"] += _int_field(record, "output_tokens")
        lane["fresh_tokens"] += _int_field(record, "input_tokens") + _int_field(record, "output_tokens")
        lane["cache_creation_tokens"] += _int_field(record, "cache_creation_input_tokens")
        lane["cache_read_tokens"] += _int_field(record, "cache_read_input_tokens")
        lane["total_tokens_including_cached"] += _int_field(record, "total_tokens_including_cached")
        lane[
            "cache_creation_reported_rows"
            if getattr(record, "cache_creation_tokens_reported", None) is True
            else "cache_creation_unreported_rows"
            if getattr(record, "cache_creation_tokens_reported", None) is False
            else "cache_creation_unknown_rows"
        ] += 1
        lane[
            "cache_read_reported_rows"
            if getattr(record, "cache_read_tokens_reported", None) is True
            else "cache_read_unreported_rows"
            if getattr(record, "cache_read_tokens_reported", None) is False
            else "cache_read_unknown_rows"
        ] += 1

    for lanes in period_lanes.values():
        for lane in lanes.values():
            lane["cache_creation_reporting"] = _token_reporting_status(
                int(lane["cache_creation_reported_rows"]),
                int(lane["cache_creation_unreported_rows"]),
                int(lane["cache_creation_unknown_rows"]),
            )
            lane["cache_read_reporting"] = _token_reporting_status(
                int(lane["cache_read_reported_rows"]),
                int(lane["cache_read_unreported_rows"]),
                int(lane["cache_read_unknown_rows"]),
            )

    # Gap fill only when the filter matched at least one row: a fully empty
    # result stays truly empty (the pages render an explicit empty state
    # instead of a wall of zero rows), while any real data makes every empty
    # period in range visible — a gap is information.
    if kept:
        for period_key in _filled_period_keys(dated_days, days=days, granularity=granularity, today=today):
            by_period.setdefault(period_key, _new_accumulator())

    def total_rank(bucket: dict[str, Any]) -> int:
        return int(bucket["total_tokens_including_cached"])

    client_entries = [
        {"client": name, **_finalize(accumulator)}
        for name, accumulator in sorted(by_client.items(), key=lambda item: (-total_rank(item[1]), item[0]))
    ]
    model_entries = [
        {"client": key[0], "provider": key[1], "model": key[2] or None, **_finalize(accumulator)}
        for key, accumulator in sorted(by_model.items(), key=lambda item: (-total_rank(item[1]), item[0]))
    ]
    dated_keys = sorted(key for key in by_period if key != UNKNOWN_PERIOD)
    period_keys = dated_keys + ([UNKNOWN_PERIOD] if UNKNOWN_PERIOD in by_period else [])
    period_entries = [
        {"period": key, "by_client": period_lanes.get(key, {}), **_finalize(by_period[key])}
        for key in period_keys
    ]

    return {
        "totals": {**_finalize(totals), "unknown_time_rows": unknown_time_rows},
        "by_client": client_entries,
        "by_model": model_entries,
        "by_period": period_entries,
    }
