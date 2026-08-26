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
# Only APPLY a fitted scale (and claim "calibrated") when it lands in this band. The
# 7-day meter is account-wide but our tokens cover only tracked clients, so a scale
# far from 1 means either heavy untracked Claude usage (desktop app / claude.ai) or a
# very different plan tier — neither of which we can identify — so we keep the shipped
# baseline rather than apply an unreliable (over- or under-stating) scale.
_TRUSTED_SCALE_BAND = (0.5, 2.5)
# An out-of-band fit is still ACCEPTED when the evidence is overwhelming and
# internally consistent: a fixed band alone turned "calibrating" into "rejected
# forever" for any account whose true tokens→meter ratio sits past the edge (a
# smaller plan tier, or baseline weights that have drifted for a new model mix)
# — the fit was being re-rejected on 130+ clean intervals whose split halves
# agreed within ~12%. A PERSISTENT, time-split-stable ratio sustained across
# weeks is a property of the account, not noise. KNOWN LIMIT, disclosed in the
# basis: the A+B>0 gate above excludes only intervals with ZERO tracked tokens;
# untracked Claude usage (desktop app / claude.ai) that consistently co-occurs
# with tracked work still inflates the fitted ratio, and if that habit is
# steady it is stable too — the accepted scale maps tracked tokens to the
# ACCOUNT-WIDE meter, which is exactly what the share numbers then mean.
# Requirements, all of them:
# * at least this many clean intervals (noise averages out far above the
#   3-interval floor),
_STABILITY_MIN_INTERVALS = 24
# * the fit window spans at least this much wall clock, split at its time
#   midpoint with at least this many intervals in EACH half — a single heavy
#   day can mint 24+ readings whose "halves" are its own morning and
#   afternoon, and must never self-certify,
_STABILITY_MIN_WINDOW_SPAN_SECONDS = 7 * 86400.0
_STABILITY_MIN_HALF_INTERVALS = 6
# * each time-half independently lands within this relative distance of the
#   full fit (a drifting or bimodal ratio never qualifies),
_STABILITY_HALF_AGREEMENT = 0.35
# * and the fit sits inside a wide hard ceiling — beyond it no amount of
#   stability makes the number credible (something structural is wrong).
_STABILITY_HARD_BAND = (0.2, 8.0)

# The two-component token model. The weekly meter is driven by FRESH work
# (input + output + cache_creation); cache READS barely move it — measured on
# the reference account (2026-08-06): a two-component fit put the cache-read
# coefficient at 0.00 in every leave-one-out fold while cutting the residual
# by a third, and the single-component fit (total-including-cache) had been
# structurally over-predicting on cache-heavy days until the trusted band
# rejected it (the "plan %% vanishes exactly on heavy days" cliff). alpha (the
# cache-read discount) is FITTED per account in [0, 1], so an account whose
# meter does charge cache reads still calibrates.
#
# The shipped BASELINE_MODEL_WEIGHTS were measured against total-including-
# cache volume at the reference account's cache mix; re-anchoring the same
# table into fresh-component units uses this factor, measured the same way the
# table itself was (two-component fit on the reference account: fresh scale
# 8.3 vs the total-anchored table).
_FRESH_COMPONENT_REF_FACTOR = 8.3
# alpha grid resolution for the fit (closed-form scale per alpha, min-SSE pick).
_ALPHA_GRID_STEPS = 100

# Plan-bearing clients whose meter can actually CALIBRATE to per-session weekly
# percentages — i.e. a clean weekly-reset cumulative meter. This is the single
# source of truth; shells (TUI plan column, glance/app payloads) derive their
# three-state display from it.
#
# codex joined 2026-08-27: its rate_limits shape changed — the meter now
# reports a single primary window (window_minutes=10080) with an observable
# resets_at, and the local rollouts show week-reset-cumulative behavior
# (monotonic climb within an epoch, drop to 0 at a new resets_at ≈ +7d),
# the same semantics the fit requires. The 2026-08-05 "rolling and opaque"
# verdict described the OLD shape and no longer holds. Residual noise the
# machinery already absorbs: integer-quantized percents (transition-recorded,
# so consecutive readings differ by ≥1 point), irregular resets (any drop is
# skipped as a reset), and fork/replay contamination (the series importer
# collapses replay bursts; the latest reader is mtime-anchored).
CALIBRATABLE_CLIENTS = ("claude-code", "codex")


@dataclass(frozen=True)
class PlanWeights:
    """Effective per-model weekly-plan weights for one account (two-component).

    ``weights`` are FRESH-component weights (percent of the weekly plan per 1M
    fresh tokens = input + output + cache_creation): the re-anchored baseline
    times the fitted ``scale``. ``alpha`` is the fitted cache-read discount in
    [0, 1] — a cache-read token counts as ``alpha`` fresh tokens (measured ~0
    on the reference account). ``confidence`` is ``calibrated`` when the fit
    came from enough recorded 7-day history and landed in the trusted band
    (or, outside it, passed the split-half stability acceptance), else
    ``baseline``. ``raw_scale`` preserves the pre-band fit for the
    calibration-progress disclosure. ``default_weight`` applies to a model
    absent from ``weights``.
    """

    weights: Mapping[str, float]
    default_weight: float
    scale: float
    confidence: str
    basis: str
    intervals_used: int
    client: str
    alpha: float = 0.0
    raw_scale: float | None = None

    def weight_for(self, model: Any) -> float:
        value = self.weights.get(str(model))
        return float(value) if isinstance(value, (int, float)) else self.default_weight

    def pct_for_tokens(self, tokens_by_model: Mapping[str, Any]) -> float:
        """Percent of the weekly plan for a per-model FRESH-component map."""

        total = 0.0
        for model, tokens in tokens_by_model.items():
            count = _finite(tokens)
            if count is None or count <= 0:
                continue
            total += self.weight_for(model) * (count / 1_000_000.0)
        return total

    def pct_for_components(
        self,
        fresh_by_model: Mapping[str, Any],
        cache_read_by_model: Mapping[str, Any],
    ) -> float:
        """Percent of the weekly plan for per-model (fresh, cache_read) maps."""

        total = self.pct_for_tokens(fresh_by_model)
        for model, tokens in cache_read_by_model.items():
            count = _finite(tokens)
            if count is None or count <= 0:
                continue
            total += self.weight_for(model) * self.alpha * (count / 1_000_000.0)
        return total


def record_components(record: Any) -> tuple[float, float]:
    """One usage record's (fresh, cache_read) token components.

    fresh = input + output + cache_creation (the work that drives the weekly
    meter); cache_read is the discounted component. A record carrying a
    combined cached count with no creation/read split books the whole cached
    bucket as reads — reads dominate real caches, and the conservative
    direction for the ESTIMATE is the discounted one (never overstate a
    session's share on unreported data).
    """

    creation = _finite(getattr(record, "cache_creation_input_tokens", None)) or 0.0
    read = _finite(getattr(record, "cache_read_input_tokens", None)) or 0.0
    cached = _finite(getattr(record, "cached_input_tokens", None)) or 0.0
    if creation + read == 0 and cached > 0:
        read = cached
    fresh = (
        (_finite(getattr(record, "input_tokens", None)) or 0.0)
        + (_finite(getattr(record, "output_tokens", None)) or 0.0)
        + creation
    )
    return fresh, read


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


def baseline_weight_fresh(model: Any, cost_per_fresh_mtok: float | None = None) -> float:
    """The baseline weight in FRESH-component units (%/M fresh tokens).

    Table models: the shipped total-anchored weight times the measured
    reference factor (see ``_FRESH_COMPONENT_REF_FACTOR``). An UNKNOWN model
    with a price derives directly from its cost per FRESH token times the
    reference plan-%-per-dollar — NOT the table path times the factor, which
    double-counted the reference cache mix and inflated a cache-light unknown
    model's weight up to ~19x (adversarial-review HIGH finding: any newly
    shipped model id would have decalibrated the account or skewed its
    sessions' shares). The identity check: at the reference mix,
    price_fresh = price_total x factor, so both paths agree for a
    reference-mix model. With neither table nor price, the Opus anchor in
    fresh units — never zero for real usage.
    """

    name = str(model or "")
    if name in BASELINE_MODEL_WEIGHTS:
        return BASELINE_MODEL_WEIGHTS[name] * _FRESH_COMPONENT_REF_FACTOR
    price_fresh = _finite(cost_per_fresh_mtok)
    if price_fresh is not None and price_fresh > 0:
        return price_fresh * _REF_PCT_PER_DOLLAR
    return BASELINE_MODEL_WEIGHTS["claude-opus-4-8"] * _FRESH_COMPONENT_REF_FACTOR


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


def _cost_per_fresh_mtok(records: Sequence[Any]) -> dict[str, float]:
    """Per-model $ per 1M FRESH tokens (the price basis for fresh-unit
    weights): total observed cost over the fresh component only. Dollars are
    mix-free evidence; dividing by fresh volume yields the weight basis the
    two-component model actually applies."""

    cost: dict[str, float] = {}
    fresh_toks: dict[str, float] = {}
    for record in records:
        model = str(getattr(record, "model", "") or "")
        if not model:
            continue
        fresh, _reads = record_components(record)
        fresh_toks[model] = fresh_toks.get(model, 0.0) + fresh
        price = _finite(getattr(record, "estimated_cost_usd", None))
        if price is not None:
            cost[model] = cost.get(model, 0.0) + price
    return {
        m: cost[m] / (fresh_toks[m] / 1_000_000.0)
        for m in fresh_toks
        if fresh_toks[m] > 0 and m in cost
    }


def _component_tokens_between(
    records: Sequence[Any], record_time, lo: float, hi: float
) -> tuple[dict[str, float], dict[str, float]]:
    """Per-model (fresh, cache_read) token maps for records inside (lo, hi]."""

    fresh: dict[str, float] = {}
    reads: dict[str, float] = {}
    for record in records:
        moment = record_time(record)
        if not isinstance(moment, (int, float)) or isinstance(moment, bool):
            continue
        if not (lo < float(moment) <= hi):
            continue
        model = str(getattr(record, "model", "") or "")
        if not model:
            continue
        f, r = record_components(record)
        fresh[model] = fresh.get(model, 0.0) + f
        reads[model] = reads.get(model, 0.0) + r
    return fresh, reads


def _stable_out_of_band_fit(
    points: list[tuple[float, float, float, float]],
    *,
    alpha: float,
    raw_scale: float,
) -> bool:
    """Should an out-of-band fit be accepted anyway? See the stability
    constants above for the contract. ``points`` are the clean intervals in
    chronological order as ``(t, delta, A, B)``; the window is split at its
    TIME midpoint (never by count — a burst day must not become both halves),
    and each half's ratio-of-sums scale (at the already-chosen alpha) must
    independently agree with the full fit."""

    if len(points) < _STABILITY_MIN_INTERVALS:
        return False
    if not (_STABILITY_HARD_BAND[0] <= raw_scale <= _STABILITY_HARD_BAND[1]):
        return False
    first_t = points[0][0]
    last_t = points[-1][0]
    if last_t - first_t < _STABILITY_MIN_WINDOW_SPAN_SECONDS:
        return False
    split_t = (first_t + last_t) / 2.0
    early = [point for point in points if point[0] <= split_t]
    late = [point for point in points if point[0] > split_t]
    for chunk in (early, late):
        if len(chunk) < _STABILITY_MIN_HALF_INTERVALS:
            return False
        observed = sum(delta for _t, delta, _a, _b in chunk)
        predicted = sum(a + alpha * b for _t, _d, a, b in chunk)
        if predicted <= 0:
            return False
        chunk_scale = observed / predicted
        if abs(chunk_scale - raw_scale) > _STABILITY_HALF_AGREEMENT * raw_scale:
            return False
    return True


def calibrate_plan_weights(
    events: list[dict[str, Any]],
    *,
    client: str = "claude-code",
    records: list[Any] | None = None,
    now: float | None = None,
) -> PlanWeights:
    """Baseline per-model weights fit to this account's recorded plan history.

    Two-component fit: for each clean interval the movement is modeled as
    ``scale × (A + alpha × B)`` where A is the fresh-component prediction
    (input+output+cache_creation at the re-anchored baseline weights) and B
    the cache-read prediction. ``scale`` is the robust ratio-of-sums
    Σobserved / Σ(A + alpha×B) for a given alpha; alpha is chosen on a [0, 1]
    grid by residual (closed-form per point, no iterative optimizer). Only
    intervals whose movement our tracked-client tokens can explain
    (``A + B > 0``) contribute, and the fitted scale is applied when it lands
    inside the trusted band — or, outside it, when it passes the split-half
    stability acceptance (see the stability constants); otherwise the shipped
    baseline is kept. This guards the
    estimate's known blind spot: the 7-day meter is ACCOUNT-WIDE (Claude
    desktop app, claude.ai, other machines all move it) while our tokens cover
    only tracked clients, so movement with no local tokens is untracked usage
    that must not inflate the scale.

    ``records`` may be passed pre-built (the caller already ran the usage view)
    to avoid rebuilding it. ``now`` bounds the recency window (injectable for
    tests). ``events`` still supplies the 7-day series regardless.
    """

    from .usage_view import _usage_record_time
    import time as _time

    now_epoch = now if now is not None else _time.time()
    if records is None:
        from .usage_snapshot import usage_records as _usage_records

        records = _usage_records(events, client=client)

    cost_per_fresh = _cost_per_fresh_mtok(records)
    observed_models = {str(getattr(r, "model", "") or "") for r in records if getattr(r, "model", None)}
    model_names = observed_models | set(BASELINE_MODEL_WEIGHTS)
    base = {m: baseline_weight_fresh(m, cost_per_fresh.get(m)) for m in model_names if m}
    default_base = BASELINE_MODEL_WEIGHTS["claude-opus-4-8"] * _FRESH_COMPONENT_REF_FACTOR

    if client not in CALIBRATABLE_CLIENTS:
        # A client without a calibratable weekly-reset meter can still land
        # numerically-clean intervals by coincidence — but its weekly plan %
        # is UNDEFINED, so a fit here would confidently label a number that
        # means nothing. The gate lives HERE, not in each display surface, so
        # no surface can ever receive one.
        return PlanWeights(
            weights=base,
            default_weight=default_base,
            scale=1.0,
            confidence="baseline",
            basis="weekly plan % is undefined for this client (no calibratable weekly meter)",
            intervals_used=0,
            client=client,
        )

    series = seven_day_series(events, client=client)
    window_start = now_epoch - _CALIBRATION_WINDOW_DAYS * 86400.0
    # (t, delta, A, B) per clean interval: interval end time, observed
    # movement, fresh-component prediction, cache-read prediction. The time
    # rides along for the stability split (chronological — series is sorted).
    points: list[tuple[float, float, float, float]] = []
    for (t0, p0), (t1, p1) in zip(series, series[1:]):
        if not (0 < t1 - t0 <= _MAX_INTERVAL_SECONDS):
            continue
        if t1 < window_start:  # only recent history reflects the current tier/mix
            continue
        delta = p1 - p0
        if delta < 0 or delta > _MAX_INTERVAL_PCT:  # a drop is a weekly reset
            continue
        fresh, reads = _component_tokens_between(records, _usage_record_time, t0, t1)
        fresh_pred = sum(base.get(m, 0.0) * (tok / 1_000_000.0) for m, tok in fresh.items())
        read_pred = sum(base.get(m, 0.0) * (tok / 1_000_000.0) for m, tok in reads.items())
        # Fit ONLY from movement our tokens can explain. An interval where the
        # meter moved with no local tokens is Claude usage outside the tracked
        # clients (desktop app / web); counting it would inflate the scale and
        # over-state every session, so skip it.
        if fresh_pred + read_pred <= 0:
            continue
        points.append((t1, delta, fresh_pred, read_pred))

    intervals = len(points)
    raw_scale = 1.0
    alpha = 0.0
    if points:
        candidates: list[tuple[float, float, float]] = []  # (alpha, scale, sse)
        observed_sum = sum(delta for _t, delta, _a, _b in points)
        for step in range(_ALPHA_GRID_STEPS + 1):
            candidate_alpha = step / _ALPHA_GRID_STEPS
            predicted_sum = sum(a + candidate_alpha * b for _t, _d, a, b in points)
            if predicted_sum <= 0:
                continue
            candidate_scale = observed_sum / predicted_sum
            sse = sum(
                (delta - candidate_scale * (a + candidate_alpha * b)) ** 2
                for _t, delta, a, b in points
            )
            candidates.append((candidate_alpha, candidate_scale, sse))
        if candidates:
            # SMALLEST alpha within tolerance of the best fit wins. alpha is a
            # second degree of freedom the trusted band never inspects:
            # untracked meter movement that co-occurs with cache-read-heavy
            # days would otherwise be absorbed into alpha, silently multiplying
            # every cache-heavy session's share while reporting "calibrated"
            # (adversarial-review finding). Reads are only charged when the
            # data clearly demands it; a collinear cache mix (alpha
            # unidentifiable) deterministically lands on 0.
            best_sse = min(sse for _a, _s, sse in candidates)
            tolerance = best_sse * 1.05 + 1e-12
            for candidate_alpha, candidate_scale, sse in candidates:  # ascending alpha
                if sse <= tolerance:
                    alpha = candidate_alpha
                    raw_scale = candidate_scale
                    break
    trusted = _TRUSTED_SCALE_BAND[0] <= raw_scale <= _TRUSTED_SCALE_BAND[1]
    stability_accepted = (
        intervals >= _MIN_SCALE_INTERVALS
        and not trusted
        and _stable_out_of_band_fit(points, alpha=alpha, raw_scale=raw_scale)
    )
    if intervals >= _MIN_SCALE_INTERVALS and (trusted or stability_accepted):
        scale = raw_scale
        confidence = "calibrated"
        # A plain string, not a %-format template — no %% escaping (a literal
        # "%%" leaked onto every basis-rendering surface).
        basis = (
            f"two-component fit x{scale:.2f}, cache-read discount {alpha:.2f} "
            f"({intervals} recent weekly-% intervals)"
        )
        if stability_accepted:
            basis += (
                "; outside the usual band, accepted on split-half stability "
                "(may include usage from untracked Claude surfaces)"
            )
    else:
        # Too little clean history, or a fit outside the trusted band (heavy
        # untracked Claude usage or a very different plan tier we can't
        # identify) → keep the shipped baseline rather than apply an
        # unreliable scale. alpha stays 0 under baseline: cache reads are
        # excluded rather than guessed (the measured reference behavior).
        scale = 1.0
        alpha = 0.0
        confidence = "baseline"
        basis = "shipped baseline (record more 7-day limit history from tracked clients to calibrate)"

    weights = {m: w * scale for m, w in base.items()}
    return PlanWeights(
        weights=weights,
        default_weight=default_base * scale,
        scale=scale,
        confidence=confidence,
        basis=basis,
        intervals_used=intervals,
        client=client,
        alpha=alpha,
        raw_scale=raw_scale if intervals else None,
    )


def session_components_by_model(
    events: list[dict[str, Any]] | None = None,
    *,
    client: str,
    session_id: str,
    records: list[Any] | None = None,
) -> dict[str, dict[str, float]]:
    """Per-model ``{total, fresh, cache_read}`` tokens for one session.

    ``total`` is the REAL total (incl. cache — what a user recognizes);
    ``fresh``/``cache_read`` are the plan components the estimate weighs."""

    if records is None:
        from .usage_snapshot import usage_records as _usage_records

        records = _usage_records(events or [], client=client)

    out: dict[str, dict[str, float]] = {}
    for record in records:
        if str(getattr(record, "client", "") or "") != client:
            continue
        if str(getattr(record, "session_id", "") or "") != str(session_id):
            continue
        model = str(getattr(record, "model", "") or "")
        if not model:
            continue
        fresh, reads = record_components(record)
        total = _finite(getattr(record, "total_tokens_including_cached", None)) or 0.0
        bucket = out.setdefault(model, {"total": 0.0, "fresh": 0.0, "cache_read": 0.0})
        bucket["total"] += total
        bucket["fresh"] += fresh
        bucket["cache_read"] += reads
    return out


def session_plan_pcts(
    records: Sequence[Any], weights: PlanWeights, *, client: str = "claude-code"
) -> dict[str, float]:
    """``{session_id: estimated % of the weekly plan}`` for one client, in ONE pass.

    Groups the pre-built usage records by (session, model) once — fresh and
    cache-read components separately — then applies ``weights`` (two-component:
    cache reads at the fitted discount), so estimating many sessions (a list
    view) is cheap, not O(records) per session."""

    fresh_by_session: dict[str, dict[str, float]] = {}
    reads_by_session: dict[str, dict[str, float]] = {}
    for record in records:
        if str(getattr(record, "client", "") or "") != client:
            continue
        session_id = str(getattr(record, "session_id", "") or "")
        model = str(getattr(record, "model", "") or "")
        if not session_id or not model:
            continue
        fresh, reads = record_components(record)
        fresh_bucket = fresh_by_session.setdefault(session_id, {})
        fresh_bucket[model] = fresh_bucket.get(model, 0.0) + fresh
        read_bucket = reads_by_session.setdefault(session_id, {})
        read_bucket[model] = read_bucket.get(model, 0.0) + reads
    return {
        sid: weights.pct_for_components(fresh_tokens, reads_by_session.get(sid) or {})
        for sid, fresh_tokens in fresh_by_session.items()
    }


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

    # Accumulator shape everywhere: {model: [fresh, cache_read, total]} — the
    # components drive the estimate, the real total stays for display.
    def _add(bucket: dict[str, list[float]], model: str, fresh: float, reads: float, total: float) -> None:
        entry = bucket.setdefault(model, [0.0, 0.0, 0.0])
        entry[0] += fresh
        entry[1] += reads
        entry[2] += total

    def _pct(bucket: dict[str, list[float]]) -> float:
        return weights.pct_for_components(
            {m: parts[0] for m, parts in bucket.items()},
            {m: parts[1] for m, parts in bucket.items()},
        )

    daily_tokens: dict[_date, dict[str, list[float]]] = {}
    window_tokens: dict[str, dict[str, list[float]]] = {label: {} for label in window_starts}
    model_tokens: dict[str, list[float]] = {}
    unknown_tokens: dict[str, list[float]] = {}
    for record in records:
        if str(getattr(record, "client", "") or "") != client:
            continue
        model = str(getattr(record, "model", "") or "")
        if not model:
            continue
        total = _finite(getattr(record, "total_tokens_including_cached", None)) or 0.0
        if total <= 0:
            continue
        fresh, reads = record_components(record)
        day = usage_bucket_date(_usage_record_time(record))
        if day is None:
            _add(unknown_tokens, model, fresh, reads, total)
            continue
        if day > resolved_today:
            continue
        if start <= day:
            _add(daily_tokens.setdefault(day, {}), model, fresh, reads, total)
            _add(model_tokens, model, fresh, reads, total)
        for label, window_start in window_starts.items():
            if window_start <= day:
                _add(window_tokens[label], model, fresh, reads, total)

    daily: list[dict[str, Any]] = []
    cursor = start
    while cursor <= resolved_today:
        daily.append(
            {
                "date": cursor.isoformat(),
                "pct": _pct(daily_tokens.get(cursor) or {}),
            }
        )
        cursor += _timedelta(days=1)

    by_model = sorted(
        (
            {
                "model": model,
                "total_tokens": parts[2],
                "pct": weights.pct_for_components({model: parts[0]}, {model: parts[1]}),
            }
            for model, parts in model_tokens.items()
        ),
        key=lambda entry: (-entry["pct"], entry["model"]),
    )

    return {
        "window_pcts": {label: _pct(tokens) for label, tokens in window_tokens.items()},
        "daily": daily,
        "by_model": by_model,
        "unknown_time_pct": (_pct(unknown_tokens) if unknown_tokens else None),
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
    clients can calibrate), ``calibratable``, the why-this-number disclosure
    fields (``basis``/``scale``/``alpha``/``intervals_used``), and the
    calibration-PROGRESS fields (``raw_scale``/``trusted_band``/
    ``intervals_needed``/``state_detail``) so a shell can show WHY a client is
    still calibrating instead of a bare spinner. ``scale``/``alpha`` are only
    meaningful when calibrated (1.0/0.0 under baseline) with ``confidence`` as
    their guard.
    """

    state = calibration_state(weights)
    if state == "calibrated":
        detail = weights.basis
    elif state == "never":
        detail = weights.basis
    elif weights.intervals_used < _MIN_SCALE_INTERVALS:
        detail = (
            f"{weights.intervals_used} of {_MIN_SCALE_INTERVALS} clean weekly-% intervals "
            "recorded — keep working with tracked clients to calibrate"
        )
    else:
        raw = weights.raw_scale
        detail = (
            f"{weights.intervals_used} intervals recorded; the fit"
            + (f" (x{raw:.2f})" if raw is not None else "")
            + f" is outside the trusted band [{_TRUSTED_SCALE_BAND[0]}, {_TRUSTED_SCALE_BAND[1]}]"
        )
        beyond_ceiling = raw is not None and not (
            _STABILITY_HARD_BAND[0] <= raw <= _STABILITY_HARD_BAND[1]
        )
        if beyond_ceiling:
            # Honest terminal state, not a spinner: past the stability ceiling
            # no amount of history calibrates this ratio.
            detail += (
                f" and beyond the stability ceiling [{_STABILITY_HARD_BAND[0]}, {_STABILITY_HARD_BAND[1]}]"
                " — it will not calibrate at this ratio (heavy untracked usage or a plan change can cause this)"
            )
        else:
            detail += (
                " — it calibrates once it holds split-half stable across "
                f"{_STABILITY_MIN_INTERVALS}+ intervals spanning a week or more"
            )
    return {
        "client": weights.client,
        "confidence": weights.confidence,
        "calibration_state": state,
        "calibratable": weights.client in CALIBRATABLE_CLIENTS,
        "basis": weights.basis,
        "scale": weights.scale,
        "alpha": weights.alpha,
        "intervals_used": weights.intervals_used,
        "intervals_needed": _MIN_SCALE_INTERVALS,
        "raw_scale": weights.raw_scale,
        "trusted_band": list(_TRUSTED_SCALE_BAND),
        # The stability-acceptance ceiling for out-of-band fits, so a shell can
        # explain the full acceptance region, not just the inner band.
        "stability_band": list(_STABILITY_HARD_BAND),
        "state_detail": detail,
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
    "record_components",
    "session_components_by_model",
    "session_plan_pcts",
]
