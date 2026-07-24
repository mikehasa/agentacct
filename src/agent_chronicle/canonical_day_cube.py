"""Canonical usage-day cube — the phase-4 read-side sibling of usage_cube.py.

Aggregates ``rm_usage_day`` rows (already day-sliced, UTC-labeled, per
(day, client, provider, model, usage_basis)) into the same
``{totals, by_client, by_model, by_period}`` surface shape the v1 cube
serves — plus an explicit ``undated`` bucket for the epoch-0 sentinel day.

Pure functions: no store access, no clock access (``today`` is required).

Honesty contract (tighter than v1 where the canonical model is tighter):
- A token field is ``None`` unless at least one contributing row reported it
  AND every reporting row's day value was computable; per-field
  ``*_reported_count`` / ``*_missing_count`` ride alongside. v1 coerces
  missing token fields to summed zeros — the canonical cube never does.
- Fields the day model cannot represent are explicit ``None``, never 0:
  per-day distinct ``sessions`` and the cost-confidence provenance trio.
  ``cost_completeness`` (the canonical cost-honesty signal) rides instead.
- Counts are named for what they count: ``measurement_days`` (one
  measurement's contribution to one day), never v1's record-count ``rows``
  under a silently different unit.
- Held (``*_held_non_additive``) rows are counted and named but NEVER summed
  (v1's non-additive exclusion): their reported flags and explicit zeros stay
  out of the metric accumulators entirely, so one held row cannot poison the
  additive sums or inflate the reported counts.
- Epoch-sentinel rows (``1970-01-01``: usage whose source timestamp was
  absent/unparseable) NEVER appear as a real date. For ``days=None`` they
  join the totals under the v1 cube's existing ``"unknown"`` period concept;
  for bounded ranges they are excluded from totals (a bounded range cannot
  honestly claim them) but always reported in the ``undated`` block.
- Empty periods inside the range are enumerated (a gap is information) with
  all-``None`` tokens and ``usage_availability="unknown"``: the canonical
  store cannot distinguish "no usage occurred" from "usage not ingested",
  so a gap-filled day claims nothing.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Mapping

from .canonical_read import UNDATED_SENTINEL_DAY
from .usage_cube import (
    UNKNOWN_PERIOD,
    _filled_period_keys,
    _token_reporting_status,
    week_start,
)

CANONICAL_USAGE_SUMMARY_SCHEMA_VERSION = "agent-sentinel.usage-summary.v2-canonical"

_HELD_BASIS_SUFFIX = "_held_non_additive"

# (rm_usage_day value column, reported-count column, missing-count column,
#  output key). Output keys reuse the v1 cube's names ONLY where the
# semantics genuinely match; the client-reported total gets its own name
# because v1's ``total_tokens_including_cached`` is a DERIVED sum, not the
# client-reported total the canonical column carries.
_METRICS: tuple[tuple[str, str, str, str], ...] = (
    ("input_tokens", "input_reported_count", "input_missing_count", "input_tokens"),
    ("output_tokens", "output_reported_count", "output_missing_count", "output_tokens"),
    ("cached_input_tokens", "cache_reported_count", "cache_missing_count", "cached_input_tokens"),
    (
        "cache_creation_input_tokens",
        "cache_creation_reported_count",
        "cache_creation_missing_count",
        "cache_creation_tokens",
    ),
    (
        "cache_read_input_tokens",
        "cache_read_reported_count",
        "cache_read_missing_count",
        "cache_read_tokens",
    ),
    (
        "reasoning_output_tokens",
        "reasoning_reported_count",
        "reasoning_missing_count",
        "reasoning_output_tokens",
    ),
    ("total_tokens", "total_reported_count", "total_missing_count", "total_tokens_reported"),
)

_REPORTING_STATUS_METRICS = ("cached_input_tokens", "cache_creation_tokens", "cache_read_tokens")


def _new_metric() -> dict[str, Any]:
    return {"value": 0, "reported": 0, "missing": 0, "incomputable": False}


def _new_bucket() -> dict[str, Any]:
    return {
        "measurement_days": 0,
        "held_measurement_days": 0,
        "metrics": {out_key: _new_metric() for _, _, _, out_key in _METRICS},
        "cost_microusd": 0,
        "priced_day_rows": 0,
        "unpriced_day_rows": 0,
        "completeness": {"complete": 0, "partial": 0, "unavailable": 0},
        "models": set(),
    }


def _row_int(row: Mapping[str, Any], key: str) -> int:
    value = row.get(key)
    return int(value) if value is not None else 0


def _accumulate(bucket: dict[str, Any], row: Mapping[str, Any]) -> None:
    bucket["measurement_days"] += _row_int(row, "measurement_count")
    bucket["held_measurement_days"] += _row_int(row, "held_measurement_count")
    model = row.get("model")
    if model:
        bucket["models"].add(str(model))
    if str(row.get("usage_basis") or "").endswith(_HELD_BASIS_SUFFIX):
        # v1 parity: non-additive (held) rows are counted and named, never
        # summed. A held row is the projection's one producer of
        # value-NULL-with-reported>0 (its raw reported flags survive while
        # only explicit zeros keep a value), so folding it into the metric
        # accumulators would poison every additive sum to None and inflate
        # the reported counts. held_measurement_days is its whole story here.
        return
    for value_key, reported_key, missing_key, out_key in _METRICS:
        metric = bucket["metrics"][out_key]
        reported = _row_int(row, reported_key)
        metric["reported"] += reported
        metric["missing"] += _row_int(row, missing_key)
        value = row.get(value_key)
        if value is not None:
            metric["value"] += int(value)
        elif reported > 0:
            # The projection could not compute this row's day value even
            # though measurements reported the field (non-monotonic deltas);
            # a partial sum would misrepresent the bucket.
            metric["incomputable"] = True
    cost = row.get("estimated_cost_microusd")
    if cost is not None:
        bucket["priced_day_rows"] += 1
        bucket["cost_microusd"] += int(cost)
    else:
        bucket["unpriced_day_rows"] += 1
    completeness = str(row.get("cost_completeness") or "unavailable")
    if completeness not in bucket["completeness"]:
        completeness = "unavailable"
    bucket["completeness"][completeness] += 1


def _metric_value(metric: Mapping[str, Any]) -> int | None:
    if metric["reported"] <= 0 or metric["incomputable"]:
        return None
    return int(metric["value"])


def _finalize(bucket: dict[str, Any]) -> dict[str, Any]:
    tokens: dict[str, int | None] = {
        out_key: _metric_value(bucket["metrics"][out_key]) for _, _, _, out_key in _METRICS
    }
    input_tokens = tokens["input_tokens"]
    output_tokens = tokens["output_tokens"]
    fresh_tokens = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else None
    )
    # v1's derived rule: input + output + cached_input, with an UNREPORTED
    # cache contributing 0 while the reporting counts say so out loud. A
    # reported-but-incomputable cache is different: the field claims cache
    # inclusion, so a value known to be missing must null the total, never
    # ride along as 0. The core tokens are never defaulted.
    cached_metric = bucket["metrics"]["cached_input_tokens"]
    if input_tokens is None or output_tokens is None or cached_metric["incomputable"]:
        total_including_cached = None
    else:
        total_including_cached = input_tokens + output_tokens + (tokens["cached_input_tokens"] or 0)
    held = int(bucket["held_measurement_days"])
    additive = max(0, int(bucket["measurement_days"]) - held)
    priced = int(bucket["priced_day_rows"])
    completeness_counts = bucket["completeness"]
    graded_rows = sum(completeness_counts.values())
    if graded_rows == 0 or completeness_counts["unavailable"] == graded_rows:
        cost_completeness = "unavailable"
    elif completeness_counts["complete"] == graded_rows:
        cost_completeness = "complete"
    else:
        cost_completeness = "partial"
    finalized: dict[str, Any] = {
        "measurement_days": int(bucket["measurement_days"]),
        "held_measurement_days": held,
        # Not representable in the day model — explicitly unknown, never 0.
        "sessions": None,
        "fresh_tokens": fresh_tokens,
        "total_tokens_including_cached": total_including_cached,
        "estimated_cost_usd": (
            bucket["cost_microusd"] / 1_000_000 if priced > 0 and held == 0 else None
        ),
        "known_additive_cost_usd": bucket["cost_microusd"] / 1_000_000 if priced > 0 else None,
        "cost_complete": bool(
            additive > 0
            and held == 0
            and graded_rows > 0
            and completeness_counts["complete"] == graded_rows
        ),
        "cost_completeness": cost_completeness,
        "priced_day_rows": priced,
        "unpriced_day_rows": int(bucket["unpriced_day_rows"]),
        # Cost-confidence provenance is not carried per day row.
        "cost_confidence": None,
        "cost_confidence_mixed": None,
        "cost_confidence_label": None,
        "usage_availability": (
            "partial"
            if additive and held
            else "held"
            if held
            else "available"
            if additive
            else "unknown"
        ),
        "models": sorted(bucket["models"]),
    }
    for _, _, _, out_key in _METRICS:
        metric = bucket["metrics"][out_key]
        finalized[out_key] = tokens[out_key]
        finalized[f"{out_key}_reported_count"] = int(metric["reported"])
        finalized[f"{out_key}_missing_count"] = int(metric["missing"])
    for out_key in _REPORTING_STATUS_METRICS:
        metric = bucket["metrics"][out_key]
        status_key = {
            "cached_input_tokens": "cached_input_reporting",
            "cache_creation_tokens": "cache_creation_reporting",
            "cache_read_tokens": "cache_read_reporting",
        }[out_key]
        finalized[status_key] = _token_reporting_status(
            int(metric["reported"]), int(metric["missing"]), 0
        )
    return finalized


def _total_rank(bucket: Mapping[str, Any]) -> int:
    value = bucket.get("total_tokens_including_cached")
    return int(value) if value is not None else 0


def build_canonical_day_cube(
    dated_rows: Iterable[Mapping[str, Any]],
    undated_rows: Iterable[Mapping[str, Any]],
    *,
    granularity: str,
    days: int | None,
    today: date,
) -> dict[str, Any]:
    """rm_usage_day rows → ``{totals, by_client, by_model, by_period, undated}``.

    ``dated_rows`` must already be range- and dimension-filtered (the
    repository query does both); ``undated_rows`` are the sentinel-day rows.
    ``today`` is the UTC today the caller derived the range from — days here
    are UTC labels, so gap fill must use the same basis.
    """

    include_undated = days is None
    totals = _new_bucket()
    by_client: dict[str, dict[str, Any]] = {}
    by_model: dict[tuple[str, str | None, str | None], dict[str, Any]] = {}
    by_period: dict[str, dict[str, Any]] = {}
    period_lanes: dict[str, dict[str, dict[str, Any]]] = {}
    dated_days: list[date] = []

    def _bucket_row(row: Mapping[str, Any], period_key: str) -> None:
        client = str(row.get("client") or "")
        provider = row.get("provider")
        model = row.get("model")
        for bucket in (
            totals,
            by_client.setdefault(client, _new_bucket()),
            by_model.setdefault(
                (client, str(provider) if provider is not None else None,
                 str(model) if model is not None else None),
                _new_bucket(),
            ),
            by_period.setdefault(period_key, _new_bucket()),
            period_lanes.setdefault(period_key, {}).setdefault(client, _new_bucket()),
        ):
            _accumulate(bucket, row)

    for row in dated_rows:
        day = date.fromisoformat(str(row["day"]))
        dated_days.append(day)
        period_key = (week_start(day) if granularity == "weekly" else day).isoformat()
        _bucket_row(row, period_key)

    undated_bucket = _new_bucket()
    undated_measurement_days = 0
    for row in undated_rows:
        undated_measurement_days += _row_int(row, "measurement_count")
        _accumulate(undated_bucket, row)
        if include_undated:
            _bucket_row(row, UNKNOWN_PERIOD)

    if dated_days or (include_undated and undated_measurement_days):
        for period_key in _filled_period_keys(
            dated_days, days=days, granularity=granularity, today=today
        ):
            by_period.setdefault(period_key, _new_bucket())

    finalized_clients = {name: _finalize(bucket) for name, bucket in by_client.items()}
    client_entries = [
        {"client": name, **finalized_clients[name]}
        for name in sorted(
            finalized_clients, key=lambda name: (-_total_rank(finalized_clients[name]), name)
        )
    ]
    finalized_models = {key: _finalize(bucket) for key, bucket in by_model.items()}
    model_entries = [
        {"client": key[0], "provider": key[1], "model": key[2], **finalized_models[key]}
        for key in sorted(
            finalized_models,
            key=lambda key: (
                -_total_rank(finalized_models[key]),
                key[0],
                key[1] or "",
                key[2] or "",
            ),
        )
    ]
    dated_keys = sorted(key for key in by_period if key != UNKNOWN_PERIOD)
    period_keys = dated_keys + ([UNKNOWN_PERIOD] if UNKNOWN_PERIOD in by_period else [])
    period_entries = [
        {
            "period": key,
            "by_client": {
                client: _finalize(lane) for client, lane in period_lanes.get(key, {}).items()
            },
            **_finalize(by_period[key]),
        }
        for key in period_keys
    ]

    return {
        "totals": {
            **_finalize(totals),
            "undated_measurement_days": undated_measurement_days,
            "undated_included": include_undated,
        },
        "by_client": client_entries,
        "by_model": model_entries,
        "by_period": period_entries,
        "undated": {
            "sentinel_day": UNDATED_SENTINEL_DAY,
            "included_in_totals": include_undated,
            "measurement_days": undated_measurement_days,
            "bucket": _finalize(undated_bucket) if undated_measurement_days else None,
        },
    }


__all__ = [
    "CANONICAL_USAGE_SUMMARY_SCHEMA_VERSION",
    "build_canonical_day_cube",
]
