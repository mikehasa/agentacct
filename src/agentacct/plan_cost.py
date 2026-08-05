"""Estimate what fraction of a Claude subscription plan a session/task consumed.

The weekly (7-day) plan meter is a **per-week-reset cumulative** counter — verified
from the desktop plan-usage series, it climbs from a weekly reset then drops back to
0 (unlike the 5-hour window, which rolls). So a session's contribution to the weekly
plan is simply its own weighted usage divided by the weekly capacity — no rolling
decay or concurrency confound to untangle.

Raw token counts are the wrong unit and cost alone is not enough either: models burn
the plan at very different rates *per dollar* (measured: Fable burns several times
more of the weekly plan per dollar than Opus 4.8). So the estimate has two parts:

* a **baseline per-model weight** — percent of the weekly plan per 1M tokens, at a
  reference (Max-tier) scale. These ship with the product so every user gets a
  reasonable estimate out of the box. They are *measured*, with Opus 4.8 solid (many
  clean single-model intervals) and the rarer models approximate; a model absent from
  the table falls back to a cost-scaled weight. This is the universal part.

* a per-user **scale** — one number that maps the baseline onto *this* account's plan
  (its tier sets the capacity, and the baseline can drift), learned by comparing the
  baseline's predicted weekly-%% movement against the account's own recorded 7-day
  usage-%% history. A single scalar is robust (no per-model collinearity) and updates
  continuously as more limit history accrues — from any source that records it
  (desktop plan-usage file import, Codex rollouts, or the Claude CLI statusLine hook),
  so CLI-only users are covered too.

Estimate = scale x Σ baseline_weight(model) x Mtokens(model). Always an ESTIMATE, and
every result carries a ``confidence`` (``calibrated`` once fit from enough history,
else ``baseline``). No credentials, no API calls — it reads the local event log.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

_SEVEN_DAY_MINUTES = 10080

# Baseline per-model weekly-plan weights: percent of the weekly plan per 1M tokens,
# at a reference (Max-tier) scale. Measured from a dense plan-usage series regressed
# against per-model token volume (main + subagent turns). Opus 4.8 is well-determined
# (dozens of clean single-model intervals); the rarer models are approximate — one
# account rarely runs several models enough to separate them, so a per-user scale (and
# eventually cross-user data) refines them. Values are intentionally conservative and
# meant to be updated as more data accrues.
BASELINE_MODEL_WEIGHTS: dict[str, float] = {
    "claude-opus-4-8": 0.0127,             # solid: ~31 clean intervals
    "claude-opus-5": 0.040,                # approximate (few clean intervals)
    "claude-fable-5": 0.130,               # approximate: ~10x Opus 4.8; matches lived experience
    "claude-haiku-4-5-20251001": 0.004,    # cost-scaled (cheap model)
}
# For a model not in the table, weight it from its own cost at Opus 4.8's measured
# plan-percent-per-dollar, so a new/unknown model still gets a sensible baseline.
_REF_PCT_PER_DOLLAR = 0.016
# Below this many usable weekly-%% intervals we don't trust a scale fit — ship the
# baseline as-is (scale 1.0) rather than chase a number from noise.
_MIN_SCALE_INTERVALS = 3
# Guard rails for what counts as a clean calibration interval. The window is kept
# short so a weekly reset is unlikely to hide inside a net-positive interval.
_MAX_INTERVAL_SECONDS = 12 * 3600.0
_MAX_INTERVAL_PCT = 60.0
# Only fit from recent history, so an old plan tier / stale baseline doesn't drag the
# current scale.
_CALIBRATION_WINDOW_DAYS = 21
# Keep a raw fitted scale sane even if the sparse early data is noisy.
_SCALE_CLAMP = (0.1, 10.0)
# Only APPLY a fitted scale (and claim "calibrated") when it lands in this band. The
# 7-day meter is account-wide but our tokens cover only tracked clients, so a scale
# far from 1 means either heavy untracked Claude usage (desktop app / claude.ai) or a
# very different plan tier — neither of which we can identify — so we keep the shipped
# baseline rather than apply an unreliable (over- or under-stating) scale.
_TRUSTED_SCALE_BAND = (0.5, 2.5)

# Plan-bearing clients whose meter can actually CALIBRATE to per-session weekly
# percentages — i.e. a clean weekly-reset cumulative meter. codex's 7-day meter is
# rolling and opaque (Σdeltas ≫ the window, resets unobservable), so a weekly plan %
# is undefined for it: it must never be shown as "calibrating", because that promises
# a number that will never arrive. This is the single source of truth; shells
# (TUI plan column, glance/app payloads) derive their three-state display from it.
CALIBRATABLE_CLIENTS = ("claude-code",)


@dataclass(frozen=True)
class PlanWeights:
    """Effective per-model weekly-plan weights (percent per 1M tokens) for one account.

    ``weights`` is the baseline times the fitted ``scale``. ``confidence`` is
    ``calibrated`` when ``scale`` was fit from enough recorded 7-day history, else
    ``baseline`` (the shipped table at scale 1.0). ``default_weight`` applies to a
    model absent from ``weights``.
    """

    weights: Mapping[str, float]
    default_weight: float
    scale: float
    confidence: str
    basis: str
    intervals_used: int
    client: str

    def weight_for(self, model: Any) -> float:
        value = self.weights.get(str(model))
        return float(value) if isinstance(value, (int, float)) else self.default_weight

    def pct_for_tokens(self, tokens_by_model: Mapping[str, Any]) -> float:
        """Estimated percent of the weekly plan for a per-model token map."""

        total = 0.0
        for model, tokens in tokens_by_model.items():
            count = _finite(tokens)
            if count is None or count <= 0:
                continue
            total += self.weight_for(model) * (count / 1_000_000.0)
        return total


def _finite(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
        return float(value)
    return None


def baseline_weight(model: Any, cost_per_mtok: float | None = None) -> float:
    """The shipped baseline weekly-plan weight for a model (%/Mtoken, reference scale).

    Known models use the measured table; an unknown model is weighted from its own
    cost at the reference plan-%%-per-dollar; with neither, it falls back to the Opus
    4.8 anchor so the estimate is never zero for real usage.
    """

    name = str(model or "")
    if name in BASELINE_MODEL_WEIGHTS:
        return BASELINE_MODEL_WEIGHTS[name]
    price = _finite(cost_per_mtok)
    if price is not None and price > 0:
        return price * _REF_PCT_PER_DOLLAR
    return BASELINE_MODEL_WEIGHTS["claude-opus-4-8"]


def seven_day_series(events: Sequence[Mapping[str, Any]], *, client: str = "claude-code") -> list[tuple[float, float]]:
    """``[(captured_at, used_percent_7d)]`` for one client, ascending by time, from the
    recorded ``rate_limit_observed`` events' 7-day window."""

    from .rate_limits import EVENT_TYPE

    out: list[tuple[float, float]] = []
    for event in events:
        if event.get("event_type") != EVENT_TYPE:
            continue
        metadata = event.get("metadata") or {}
        if client and str(metadata.get("client") or "") != client:
            continue
        captured = _finite(metadata.get("captured_at"))
        if captured is None:
            captured = _finite(event.get("created_at"))
        if captured is None:
            continue
        for window in metadata.get("windows") or []:
            if not isinstance(window, Mapping):
                continue
            if window.get("window_minutes") == _SEVEN_DAY_MINUTES or str(window.get("kind")) == "7d":
                used = _finite(window.get("used_percent"))
                if used is not None:
                    out.append((captured, used))
                    break
    out.sort(key=lambda row: row[0])
    return out


def _cost_per_mtok(records: Sequence[Any]) -> dict[str, float]:
    cost: dict[str, float] = {}
    toks: dict[str, float] = {}
    for record in records:
        model = str(getattr(record, "model", "") or "")
        if not model:
            continue
        toks[model] = toks.get(model, 0.0) + (_finite(getattr(record, "total_tokens_including_cached", None)) or 0.0)
        price = _finite(getattr(record, "estimated_cost_usd", None))
        if price is not None:
            cost[model] = cost.get(model, 0.0) + price
    return {m: cost[m] / (toks[m] / 1_000_000.0) for m in toks if toks[m] > 0 and m in cost}


def _model_tokens_between(records: Sequence[Any], record_time, lo: float, hi: float) -> dict[str, float]:
    tokens: dict[str, float] = {}
    for record in records:
        moment = record_time(record)
        if not isinstance(moment, (int, float)) or isinstance(moment, bool):
            continue
        if not (lo < float(moment) <= hi):
            continue
        model = str(getattr(record, "model", "") or "")
        if not model:
            continue
        total = _finite(getattr(record, "total_tokens_including_cached", None))
        tokens[model] = tokens.get(model, 0.0) + (total or 0.0)
    return tokens


def calibrate_plan_weights(
    events: list[dict[str, Any]],
    *,
    client: str = "claude-code",
    records: list[Any] | None = None,
    now: float | None = None,
) -> PlanWeights:
    """Baseline per-model weights scaled to this account's recorded plan history.

    Fits ONE per-user scale = (observed weekly-%% moved) / (baseline-predicted %%)
    over recent, clean, non-reset intervals — robust and collinearity-free. Only
    intervals whose movement our tracked-client tokens can actually explain
    (``predicted > 0``) contribute, and the fitted scale is applied only inside a
    trusted band; otherwise the shipped baseline is kept. This guards the estimate's
    known blind spot: the 7-day meter is ACCOUNT-WIDE (Claude desktop app, claude.ai,
    other machines all move it) while our tokens cover only tracked clients, so
    movement with no local tokens is untracked usage that must not inflate the scale.

    ``records`` may be passed pre-built (the caller already ran the usage view) to
    avoid rebuilding it. ``now`` bounds the recency window (injectable for tests).
    ``events`` still supplies the 7-day series regardless.
    """

    from .usage_view import _usage_record_time
    import time as _time

    now_epoch = now if now is not None else _time.time()
    if records is None:
        from .usage_snapshot import usage_records as _usage_records

        records = _usage_records(events, client=client)

    cost_per_mtok = _cost_per_mtok(records)
    observed_models = {str(getattr(r, "model", "") or "") for r in records if getattr(r, "model", None)}
    model_names = observed_models | set(BASELINE_MODEL_WEIGHTS)
    base = {m: baseline_weight(m, cost_per_mtok.get(m)) for m in model_names if m}

    if client not in CALIBRATABLE_CLIENTS:
        # A rolling/opaque meter (codex) can land 3 numerically-clean intervals
        # inside the trusted band by coincidence — but its weekly plan %% is
        # UNDEFINED by design, so a fit here would confidently label a number
        # that means nothing (adversarial-review finding: /v1/plan served
        # "calibrated" codex aggregates). The gate lives HERE, not in each
        # display surface, so no surface can ever receive one.
        return PlanWeights(
            weights=base,
            default_weight=BASELINE_MODEL_WEIGHTS["claude-opus-4-8"],
            scale=1.0,
            confidence="baseline",
            basis="weekly plan %% is undefined for this client's rolling meter (it never calibrates)",
            intervals_used=0,
            client=client,
        )

    series = seven_day_series(events, client=client)
    window_start = now_epoch - _CALIBRATION_WINDOW_DAYS * 86400.0
    observed = 0.0
    predicted = 0.0
    intervals = 0
    for (t0, p0), (t1, p1) in zip(series, series[1:]):
        if not (0 < t1 - t0 <= _MAX_INTERVAL_SECONDS):
            continue
        if t1 < window_start:  # only recent history reflects the current tier/mix
            continue
        delta = p1 - p0
        if delta < 0 or delta > _MAX_INTERVAL_PCT:  # a drop is a weekly reset
            continue
        interval_tokens = _model_tokens_between(records, _usage_record_time, t0, t1)
        pred = sum(base.get(m, 0.0) * (tok / 1_000_000.0) for m, tok in interval_tokens.items())
        # Fit ONLY from movement our tokens can explain. An interval where the meter
        # moved with no local tokens (pred == 0) is Claude usage outside the tracked
        # clients (desktop app / web); counting it would inflate the scale and
        # over-state every session, so skip it.
        if pred <= 0:
            continue
        observed += delta
        predicted += pred
        intervals += 1

    raw_scale = (observed / predicted) if predicted > 0 else 1.0
    raw_scale = max(_SCALE_CLAMP[0], min(_SCALE_CLAMP[1], raw_scale))
    trusted = _TRUSTED_SCALE_BAND[0] <= raw_scale <= _TRUSTED_SCALE_BAND[1]
    if intervals >= _MIN_SCALE_INTERVALS and predicted > 0 and trusted:
        scale = raw_scale
        confidence = "calibrated"
        basis = f"baseline scaled x{scale:.2f} to this account ({intervals} recent weekly-%% intervals)"
    else:
        # Too little clean history, or a fit outside the trusted band (heavy untracked
        # Claude usage or a very different plan tier we can't identify) → keep the
        # shipped baseline rather than apply an unreliable scale.
        scale = 1.0
        confidence = "baseline"
        basis = "shipped baseline (record more 7-day limit history from tracked clients to calibrate)"

    weights = {m: w * scale for m, w in base.items()}
    default = BASELINE_MODEL_WEIGHTS["claude-opus-4-8"] * scale
    return PlanWeights(
        weights=weights,
        default_weight=default,
        scale=scale,
        confidence=confidence,
        basis=basis,
        intervals_used=intervals,
        client=client,
    )


def session_tokens_by_model(
    events: list[dict[str, Any]] | None = None,
    *,
    client: str,
    session_id: str,
    records: list[Any] | None = None,
) -> dict[str, float]:
    """Per-model total tokens (incl. cache) for one session, from its usage records.

    ``records`` may be passed pre-built to avoid rebuilding the usage view."""

    if records is None:
        from .usage_snapshot import usage_records as _usage_records

        records = _usage_records(events or [], client=client)

    tokens: dict[str, float] = {}
    for record in records:
        if str(getattr(record, "client", "") or "") != client:
            continue
        if str(getattr(record, "session_id", "") or "") != str(session_id):
            continue
        model = str(getattr(record, "model", "") or "")
        if not model:
            continue
        total = _finite(getattr(record, "total_tokens_including_cached", None)) or 0.0
        tokens[model] = tokens.get(model, 0.0) + total
    return tokens


def session_plan_pcts(
    records: Sequence[Any], weights: PlanWeights, *, client: str = "claude-code"
) -> dict[str, float]:
    """``{session_id: estimated % of the weekly plan}`` for one client, in ONE pass.

    Groups the pre-built usage records by (session, model) once, then applies
    ``weights`` — so estimating many sessions (a list view) is cheap, not O(records)
    per session."""

    by_session: dict[str, dict[str, float]] = {}
    for record in records:
        if str(getattr(record, "client", "") or "") != client:
            continue
        session_id = str(getattr(record, "session_id", "") or "")
        model = str(getattr(record, "model", "") or "")
        if not session_id or not model:
            continue
        total = _finite(getattr(record, "total_tokens_including_cached", None)) or 0.0
        bucket = by_session.setdefault(session_id, {})
        bucket[model] = bucket.get(model, 0.0) + total
    return {sid: weights.pct_for_tokens(tokens) for sid, tokens in by_session.items()}


def plan_pct_aggregates(
    records: Sequence[Any],
    weights: PlanWeights,
    *,
    client: str,
    days: int = 30,
    today: Any = None,
) -> dict[str, Any]:
    """Windowed/daily/by-model plan-share aggregates for one client, one pass.

    Mirrors the usage cube's calendar semantics exactly
    (:func:`agentacct.usage_cube.usage_bucket_date`: local calendar days,
    trailing ranges ending ``today``, a session-lane's tokens landing on its
    latest-update day) so a plan-share daily series lines up column-for-column
    with the /usage/summary cost chart. Tokens whose timestamp fails the
    bad-timestamp guard cannot honestly join a bounded range; their share is
    DISCLOSED as ``unknown_time_pct`` rather than silently dropped.

    Returns ``{window_pcts: {today,7d,30d}, daily: [{date, pct}] (ascending,
    empty days included), by_model: [{model, total_tokens, pct}] over the
    trailing ``days`` range, unknown_time_pct}``. Raw floats, never rounded.
    The calibrated-or-nothing rule is the CALLER's job (attach these only
    when ``weights.confidence == "calibrated"``), same as session shares.
    """

    from datetime import date as _date, timedelta as _timedelta

    from .usage_cube import usage_bucket_date
    from .usage_view import _usage_record_time

    resolved_today: _date = today or _date.today()
    start = resolved_today - _timedelta(days=days - 1)
    window_starts = {
        "today": resolved_today,
        "7d": resolved_today - _timedelta(days=6),
        "30d": resolved_today - _timedelta(days=29),
    }

    daily_tokens: dict[_date, dict[str, float]] = {}
    window_tokens: dict[str, dict[str, float]] = {label: {} for label in window_starts}
    model_tokens: dict[str, float] = {}
    unknown_tokens: dict[str, float] = {}
    for record in records:
        if str(getattr(record, "client", "") or "") != client:
            continue
        model = str(getattr(record, "model", "") or "")
        if not model:
            continue
        total = _finite(getattr(record, "total_tokens_including_cached", None)) or 0.0
        if total <= 0:
            continue
        day = usage_bucket_date(_usage_record_time(record))
        if day is None:
            unknown_tokens[model] = unknown_tokens.get(model, 0.0) + total
            continue
        if day > resolved_today:
            continue
        if start <= day:
            bucket = daily_tokens.setdefault(day, {})
            bucket[model] = bucket.get(model, 0.0) + total
            model_tokens[model] = model_tokens.get(model, 0.0) + total
        for label, window_start in window_starts.items():
            if window_start <= day:
                window_bucket = window_tokens[label]
                window_bucket[model] = window_bucket.get(model, 0.0) + total

    daily: list[dict[str, Any]] = []
    cursor = start
    while cursor <= resolved_today:
        daily.append(
            {
                "date": cursor.isoformat(),
                "pct": weights.pct_for_tokens(daily_tokens.get(cursor) or {}),
            }
        )
        cursor += _timedelta(days=1)

    by_model = sorted(
        (
            {
                "model": model,
                "total_tokens": tokens,
                "pct": weights.pct_for_tokens({model: tokens}),
            }
            for model, tokens in model_tokens.items()
        ),
        key=lambda entry: (-entry["pct"], entry["model"]),
    )

    return {
        "window_pcts": {
            label: weights.pct_for_tokens(tokens) for label, tokens in window_tokens.items()
        },
        "daily": daily,
        "by_model": by_model,
        "unknown_time_pct": (
            weights.pct_for_tokens(unknown_tokens) if unknown_tokens else None
        ),
    }


V1_PLAN_SCHEMA_VERSION = "agentacct.v1-plan.v1"


def build_v1_plan_payload(
    events: list[dict[str, Any]],
    *,
    days: int = 30,
    now: float | None = None,
    today: Any = None,
) -> dict[str, Any]:
    """The ``GET /v1/plan`` body: per-client plan status + attributed aggregates.

    One entry per plan-bearing client (the glance's PLAN_CLIENTS): the
    three-state calibration status with its why-this-number disclosure, and —
    calibrated-or-nothing — the attributed weekly-plan aggregates from
    :func:`plan_pct_aggregates` (window_pcts / daily series / by_model /
    unknown_time_pct). An uncalibrated client carries explicit ``None``
    aggregates next to its state, never fabricated numbers. These are
    ATTRIBUTED estimates over tracked sessions; the account-wide provider
    truth (7d used %) lives in the glance ``limits[]`` and is deliberately
    not duplicated here — the two are different quantities and shells must
    label them apart.
    """

    import time as _time

    from .glance import PLAN_CLIENTS
    from .usage_snapshot import usage_records

    clients: list[dict[str, Any]] = []
    for client in PLAN_CLIENTS:
        records = usage_records(events, client=client)
        weights = calibrate_plan_weights(events, client=client, records=records)
        entry: dict[str, Any] = plan_status_entry(weights)
        if weights.confidence == "calibrated":
            entry.update(plan_pct_aggregates(records, weights, client=client, days=days, today=today))
        else:
            entry.update(
                {"window_pcts": None, "daily": None, "by_model": None, "unknown_time_pct": None}
            )
        clients.append(entry)
    return {
        "schema": V1_PLAN_SCHEMA_VERSION,
        "generated_at": _time.time() if now is None else float(now),
        "days": days,
        "clients": clients,
    }


def calibration_state(weights: PlanWeights) -> str:
    """Three-state display semantic for one client's plan estimate.

    ``calibrated`` — per-session percentages are grounded in this account's own
    recorded limit history and may be shown. ``calibrating`` — the client CAN
    calibrate but hasn't yet (not enough clean intervals, or the fit fell outside
    the trusted band); an honest "warming up", not a missing feature.  ``never``
    — the client's meter cannot yield a weekly plan %% (see
    :data:`CALIBRATABLE_CLIENTS`); shells must not show "calibrating" for it.
    """

    if weights.confidence == "calibrated":
        return "calibrated"
    return "calibrating" if weights.client in CALIBRATABLE_CLIENTS else "never"


def plan_status_entry(weights: PlanWeights) -> dict[str, Any]:
    """The per-client plan payload entry shared by glance and the /v1 lane.

    Additive superset of the original ``{client, confidence}`` shape: the
    three-state ``calibration_state`` (so no shell has to hard-code which
    clients can calibrate), ``calibratable``, and the why-this-number
    disclosure fields (``basis``/``scale``/``intervals_used``) a detail view
    renders verbatim. ``scale`` is only meaningful when calibrated; it is
    reported as-is (1.0 under baseline) with ``confidence`` as its guard.
    """

    return {
        "client": weights.client,
        "confidence": weights.confidence,
        "calibration_state": calibration_state(weights),
        "calibratable": weights.client in CALIBRATABLE_CLIENTS,
        "basis": weights.basis,
        "scale": weights.scale,
        "intervals_used": weights.intervals_used,
    }


__all__ = [
    "BASELINE_MODEL_WEIGHTS",
    "CALIBRATABLE_CLIENTS",
    "V1_PLAN_SCHEMA_VERSION",
    "PlanWeights",
    "baseline_weight",
    "build_v1_plan_payload",
    "seven_day_series",
    "calibrate_plan_weights",
    "calibration_state",
    "plan_pct_aggregates",
    "plan_status_entry",
    "session_tokens_by_model",
    "session_plan_pcts",
]
