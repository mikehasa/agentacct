"""Tests for the weekly-plan cost estimator (`agentacct.plan_cost`)."""

from __future__ import annotations

from pathlib import Path

from agentacct.client_usage import ClientUsageEvent
from agentacct.service import SentinelService
from agentacct import plan_cost as pc


def _record_usage(service, *, client, model, session_id, tokens, updated_at, cost):
    event = ClientUsageEvent(
        client=client,
        client_session_id=session_id,
        source_path=Path(f"/tmp/{client}/{session_id}.jsonl"),
        title=None,
        cwd="/tmp/p",
        model=model,
        input_tokens=tokens,
        output_tokens=0,
        cached_input_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_tokens_reported=True,
        cache_read_tokens_reported=True,
        reasoning_output_tokens=0,
        provider_name=client,
        started_at=updated_at,
        updated_at=updated_at,
        turn_count=1,
        usage_row_lane=f"model:{model}",
        source_namespace_fingerprint=f"sha256:{client}",
        input_tokens_reported=True,
        output_tokens_reported=True,
        reasoning_output_tokens_reported=True,
        total_tokens=tokens,
        total_tokens_reported=True,
    ).to_sentinel_event()
    if cost is not None:
        event["estimated_cost_usd"] = cost
        event["cost_confidence"] = "estimated_from_tokens"
    service.record_event(event, trusted_usage_import=True)


def _record_7d(service, *, captured, pct, client="claude-code", index=0):
    service.record_event({
        "event_id": f"evt_rl_{client}_{index}",
        "created_at": captured,
        "source": client,
        "event_type": "rate_limit_observed",
        "metadata": {
            "client": client,
            "captured_at": captured,
            "windows": [{"kind": "7d", "window_minutes": 10080, "used_percent": pct}],
        },
    })


# ---------------------------------------------------------------------------
# pure pieces
# ---------------------------------------------------------------------------


def test_baseline_weight():
    # a known model → the measured table value.
    assert pc.baseline_weight("claude-opus-4-8") == pc.BASELINE_MODEL_WEIGHTS["claude-opus-4-8"]
    # an unknown model with a cost → cost-scaled at the reference plan-%/$.
    assert pc.baseline_weight("brand-new", cost_per_mtok=2.0) == 2.0 * pc._REF_PCT_PER_DOLLAR
    # unknown with no cost → the Opus anchor (never zero for real usage).
    assert pc.baseline_weight("brand-new") == pc.BASELINE_MODEL_WEIGHTS["claude-opus-4-8"]


def test_pct_for_tokens():
    w = pc.PlanWeights(
        weights={"claude-opus-4-8": 0.01, "claude-fable-5": 0.10},
        default_weight=0.01, scale=1.0, confidence="baseline", basis="", intervals_used=0,
        client="claude-code",
    )
    # 100M opus * 0.01 + 50M fable * 0.10 = 1.0 + 5.0 = 6.0%
    assert abs(w.pct_for_tokens({"claude-opus-4-8": 100_000_000, "claude-fable-5": 50_000_000}) - 6.0) < 1e-9
    # an unknown model uses default_weight; non-positive counts are ignored.
    assert abs(w.pct_for_tokens({"other": 100_000_000, "x": 0, "y": -5}) - 1.0) < 1e-9


def test_seven_day_series(tmp_path):
    service = SentinelService(tmp_path)
    _record_7d(service, captured=200.0, pct=5.0, index=1)
    _record_7d(service, captured=100.0, pct=3.0, index=2)              # out of order on purpose
    _record_7d(service, captured=150.0, pct=9.0, client="codex", index=3)  # other client filtered out
    series = pc.seven_day_series(service.list_all_events(), client="claude-code")
    assert series == [(100.0, 3.0), (200.0, 5.0)]  # ascending, claude only


# ---------------------------------------------------------------------------
# calibration
# ---------------------------------------------------------------------------


def test_calibrate_baseline_when_no_history(tmp_path):
    service = SentinelService(tmp_path)
    _record_usage(service, client="claude-code", model="claude-opus-4-8", session_id="s1",
                  tokens=1_000_000, updated_at=1000, cost=1.0)
    weights = pc.calibrate_plan_weights(service.list_all_events(), client="claude-code")
    assert weights.confidence == "baseline"      # no 7d history → shipped baseline
    assert weights.scale == 1.0
    assert weights.weight_for("claude-opus-4-8") == pc.baseline_weight_fresh("claude-opus-4-8")


def test_calibrate_fits_scale_from_history(tmp_path):
    # Construct history where the account's OWN weekly-% moves exactly 2x what the
    # baseline predicts → the fitted per-user scale should be ~2.0 (calibrated).
    service = SentinelService(tmp_path)
    t0 = 1_000_000
    opus = pc.baseline_weight_fresh("claude-opus-4-8")  # %/M fresh tokens
    # 100M Opus/hour → baseline predicts 100 * opus % per hour; make the real move 2x.
    move = 2.0 * (100.0 * opus)
    pct = 0.0
    _record_7d(service, captured=float(t0), pct=pct, index=0)
    for i in range(4):  # 4 intervals of 1 hour each (> _MIN_SCALE_INTERVALS)
        mid = t0 + i * 3600 + 1800
        _record_usage(service, client="claude-code", model="claude-opus-4-8",
                      session_id=f"s{i}", tokens=100_000_000, updated_at=mid, cost=1.0)
        pct += move
        _record_7d(service, captured=float(t0 + (i + 1) * 3600), pct=pct, index=i + 1)
    # now= just after the synthetic history so the recency window includes it.
    weights = pc.calibrate_plan_weights(service.list_all_events(), client="claude-code",
                                        now=float(t0 + 5 * 3600))
    assert weights.confidence == "calibrated"
    assert weights.intervals_used >= pc._MIN_SCALE_INTERVALS
    assert abs(weights.scale - 2.0) < 0.15                 # recovered the 2x scale (in the trusted band)
    # the effective weight is the baseline times the fitted scale.
    assert abs(weights.weight_for("claude-opus-4-8") - opus * weights.scale) < 1e-9


def test_calibrate_ignores_weekly_reset(tmp_path):
    # A drop in the 7d% (a weekly reset) must not be regressed as negative usage.
    service = SentinelService(tmp_path)
    t0 = 1_000_000
    _record_7d(service, captured=float(t0), pct=50.0, index=0)
    _record_usage(service, client="claude-code", model="claude-opus-4-8", session_id="s1",
                  tokens=10_000_000, updated_at=t0 + 1800, cost=1.0)
    _record_7d(service, captured=float(t0 + 3600), pct=2.0, index=1)  # reset (50 → 2)
    weights = pc.calibrate_plan_weights(service.list_all_events(), client="claude-code",
                                        now=float(t0 + 2 * 3600))
    # the single reset interval is skipped → no usable intervals → baseline.
    assert weights.confidence == "baseline"


def test_calibrate_skips_untracked_movement(tmp_path):
    # Regression (review): the 7-day meter is account-wide. If it moves but NO local
    # usage records fall in those windows (Claude used on the desktop app / web), those
    # intervals must NOT inflate the scale — with no locally-attributable tokens the
    # fit has nothing, so it stays baseline instead of over-stating every session.
    service = SentinelService(tmp_path)
    t0 = 1_000_000
    pct = 0.0
    _record_7d(service, captured=float(t0), pct=pct, index=0)
    for i in range(4):
        pct += 5.0  # meter climbs, but no usage tokens are recorded
        _record_7d(service, captured=float(t0 + (i + 1) * 3600), pct=pct, index=i + 1)
    weights = pc.calibrate_plan_weights(service.list_all_events(), client="claude-code",
                                        now=float(t0 + 5 * 3600))
    assert weights.confidence == "baseline"
    assert weights.intervals_used == 0


def _record_cached_usage(service, *, session_id, fresh, cache_read, updated_at, index=0):
    """A claude-code usage row with an explicit fresh/cache-read split."""

    event = ClientUsageEvent(
        client="claude-code",
        client_session_id=session_id,
        source_path=Path(f"/tmp/claude-code/{session_id}-{index}.jsonl"),
        title=None,
        cwd="/tmp/p",
        model="claude-opus-4-8",
        input_tokens=fresh,
        output_tokens=0,
        cached_input_tokens=cache_read,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=cache_read,
        cache_creation_tokens_reported=True,
        cache_read_tokens_reported=True,
        reasoning_output_tokens=0,
        provider_name="claude-code",
        started_at=updated_at,
        updated_at=updated_at,
        turn_count=1,
        usage_row_lane="model:claude-opus-4-8",
        source_namespace_fingerprint="sha256:claude-code",
        input_tokens_reported=True,
        output_tokens_reported=True,
        reasoning_output_tokens_reported=True,
        total_tokens=fresh,
        total_tokens_reported=True,
    ).to_sentinel_event()
    service.record_event(event, trusted_usage_import=True)


def test_two_component_fit_recovers_the_cache_discount(tmp_path):
    """Intervals constructed with observed = 1.0 x (fresh + 0.5 x reads)
    prediction → the grid recovers alpha ≈ 0.5 with scale ≈ 1.0."""

    service = SentinelService(tmp_path)
    t0 = 1_000_000
    w = pc.baseline_weight_fresh("claude-opus-4-8")
    pct = 0.0
    _record_7d(service, captured=float(t0), pct=pct, index=0)
    # Vary the cache mix across intervals so alpha is identified (all-same
    # mixes are collinear and any alpha would fit).
    mixes = [(100, 0), (50, 200), (100, 100), (20, 400)]  # (fresh M, reads M)
    for i, (fresh_m, reads_m) in enumerate(mixes):
        _record_cached_usage(service, session_id=f"s{i}", fresh=fresh_m * 1_000_000,
                             cache_read=reads_m * 1_000_000, updated_at=t0 + i * 3600 + 1800,
                             index=i)
        pct += w * (fresh_m + 0.5 * reads_m)
        _record_7d(service, captured=float(t0 + (i + 1) * 3600), pct=pct, index=i + 1)
    weights = pc.calibrate_plan_weights(service.list_all_events(), client="claude-code",
                                        now=float(t0 + 5 * 3600))
    assert weights.confidence == "calibrated"
    assert abs(weights.alpha - 0.5) < 0.05
    assert abs(weights.scale - 1.0) < 0.1


def test_cache_heavy_days_no_longer_fall_out_of_the_band(tmp_path):
    """The cliff this redesign removes: huge cache-read volume with modest
    meter movement used to drag the single-component fit far below the band
    (plan %% vanished exactly on heavy days). Under the two-component model
    the same store calibrates with alpha ≈ 0."""

    service = SentinelService(tmp_path)
    t0 = 1_000_000
    w = pc.baseline_weight_fresh("claude-opus-4-8")
    pct = 0.0
    _record_7d(service, captured=float(t0), pct=pct, index=0)
    for i in range(4):
        # 10M fresh + 500M cache reads per interval; the meter moves with the
        # FRESH work only (the measured real-world behavior).
        _record_cached_usage(service, session_id=f"s{i}", fresh=10_000_000,
                             cache_read=500_000_000, updated_at=t0 + i * 3600 + 1800,
                             index=i)
        pct += w * 10.0
        _record_7d(service, captured=float(t0 + (i + 1) * 3600), pct=pct, index=i + 1)
    weights = pc.calibrate_plan_weights(service.list_all_events(), client="claude-code",
                                        now=float(t0 + 5 * 3600))
    assert weights.confidence == "calibrated"
    assert weights.alpha < 0.05
    assert abs(weights.scale - 1.0) < 0.1
    # A cache-heavy session's share reflects its fresh work, not its reads.
    records = __import__("agentacct.usage_snapshot", fromlist=["usage_records"]).usage_records(
        service.list_all_events(), client="claude-code"
    )
    pcts = pc.session_plan_pcts(records, weights, client="claude-code")
    assert abs(pcts["s0"] - w * 10.0) < 0.2


def test_unknown_model_fallback_prices_in_fresh_units():
    """Round-2 HIGH regression: an unknown model's cost fallback derives from
    its $ per FRESH Mtok x the reference %/$, NOT the total-anchored path
    times the reference factor (which double-counted the reference cache mix
    and inflated cache-light unknown models ~19x). Identity: at the reference
    mix the two paths agree."""

    # Table path: unchanged anchoring.
    assert pc.baseline_weight_fresh("claude-opus-4-8") == (
        pc.BASELINE_MODEL_WEIGHTS["claude-opus-4-8"] * pc._FRESH_COMPONENT_REF_FACTOR
    )
    # Unknown model with a FRESH price: linear in the price, no 8.3 anywhere.
    assert abs(pc.baseline_weight_fresh("brand-new", cost_per_fresh_mtok=100.0)
               - 100.0 * pc._REF_PCT_PER_DOLLAR) < 1e-12
    # Reference-mix identity: price_fresh = price_total x factor -> the
    # fallback equals the old total path re-anchored.
    price_total = 15.0
    price_fresh = price_total * pc._FRESH_COMPONENT_REF_FACTOR
    assert abs(pc.baseline_weight_fresh("brand-new", cost_per_fresh_mtok=price_fresh)
               - price_total * pc._REF_PCT_PER_DOLLAR * pc._FRESH_COMPONENT_REF_FACTOR) < 1e-12
    # No price at all -> the Opus anchor in fresh units.
    assert pc.baseline_weight_fresh("brand-new") == (
        pc.BASELINE_MODEL_WEIGHTS["claude-opus-4-8"] * pc._FRESH_COMPONENT_REF_FACTOR
    )


def test_collinear_cache_mix_lands_on_alpha_zero(tmp_path):
    """When every interval has the SAME fresh:read ratio, alpha is
    unidentifiable — the smallest-alpha-within-tolerance rule must land on 0
    deterministically (never an arbitrary grid point)."""

    service = SentinelService(tmp_path)
    t0 = 1_000_000
    w = pc.baseline_weight_fresh("claude-opus-4-8")
    pct = 0.0
    _record_7d(service, captured=float(t0), pct=pct, index=0)
    for i in range(4):
        # constant 1:4 fresh:read mix; meter moves as if only fresh counts.
        _record_cached_usage(service, session_id=f"s{i}", fresh=50_000_000,
                             cache_read=200_000_000, updated_at=t0 + i * 3600 + 1800,
                             index=i)
        pct += w * 50.0
        _record_7d(service, captured=float(t0 + (i + 1) * 3600), pct=pct, index=i + 1)
    weights = pc.calibrate_plan_weights(service.list_all_events(), client="claude-code",
                                        now=float(t0 + 5 * 3600))
    assert weights.alpha == 0.0
    assert weights.confidence == "calibrated"


def test_raw_scale_discloses_the_unclamped_fit(tmp_path):
    """The calibration-progress field must carry the TRUE fitted ratio, not a
    clamped copy — two very different out-of-band accounts must not read
    identically (round-2 finding)."""

    service = SentinelService(tmp_path)
    t0 = 1_000_000
    w = pc.baseline_weight_fresh("claude-opus-4-8")
    # Small volume so a 45x ratio still fits inside a clean interval (<60%).
    move = 45.0 * (10.0 * w)
    pct = 0.0
    _record_7d(service, captured=float(t0), pct=pct, index=0)
    for i in range(4):
        _record_usage(service, client="claude-code", model="claude-opus-4-8",
                      session_id=f"s{i}", tokens=10_000_000, updated_at=t0 + i * 3600 + 1800,
                      cost=1.0)
        pct += move
        _record_7d(service, captured=float(t0 + (i + 1) * 3600), pct=pct, index=i + 1)
    weights = pc.calibrate_plan_weights(service.list_all_events(), client="claude-code",
                                        now=float(t0 + 5 * 3600))
    assert weights.confidence == "baseline"  # far outside the band
    assert weights.raw_scale is not None and weights.raw_scale > 10.0


def test_codex_never_calibrates_even_with_numerically_clean_history(tmp_path):
    """A rolling meter can land 3 clean intervals in the trusted band by
    coincidence, but codex's weekly plan % is UNDEFINED by design — the gate
    lives in calibrate_plan_weights so NO surface can ever receive a
    'calibrated' codex fit (adversarial-review finding: /v1/plan served
    confidently-labeled aggregates of an undefined quantity)."""

    service = SentinelService(tmp_path)
    t0 = 1_000_000
    anchor = pc.baseline_weight_fresh("claude-opus-4-8")  # unknown codex model -> anchor weight
    move = 100.0 * anchor  # observed == predicted -> scale ~1.0, inside the band
    pct = 10.0
    _record_7d(service, captured=float(t0), pct=pct, client="codex", index=0)
    for i in range(4):
        _record_usage(service, client="codex", model="gpt-5.2-codex", session_id=f"c{i}",
                      tokens=100_000_000, updated_at=t0 + i * 3600 + 1800, cost=None)
        pct += move
        _record_7d(service, captured=float(t0 + (i + 1) * 3600), pct=pct, client="codex", index=i + 1)
    weights = pc.calibrate_plan_weights(service.list_all_events(), client="codex",
                                        now=float(t0 + 5 * 3600))
    assert weights.confidence == "baseline"
    assert pc.calibration_state(weights) == "never"
    assert "undefined" in weights.basis


def test_calibrate_untrusted_scale_falls_back_to_baseline(tmp_path):
    # Regression (review): if the meter moved ~10x what local tokens predict (heavy
    # untracked usage, or a very different tier we can't identify), the fitted scale is
    # outside the trusted band → keep the baseline rather than a 10x over-estimate.
    service = SentinelService(tmp_path)
    t0 = 1_000_000
    opus = pc.baseline_weight_fresh("claude-opus-4-8")
    move = 10.0 * (100.0 * opus)  # 10x the baseline prediction
    pct = 0.0
    _record_7d(service, captured=float(t0), pct=pct, index=0)
    for i in range(4):
        mid = t0 + i * 3600 + 1800
        _record_usage(service, client="claude-code", model="claude-opus-4-8",
                      session_id=f"s{i}", tokens=100_000_000, updated_at=mid, cost=1.0)
        pct += move
        _record_7d(service, captured=float(t0 + (i + 1) * 3600), pct=pct, index=i + 1)
    weights = pc.calibrate_plan_weights(service.list_all_events(), client="claude-code",
                                        now=float(t0 + 5 * 3600))
    assert weights.confidence == "baseline"   # untrusted fit → baseline
    assert weights.scale == 1.0
    assert weights.weight_for("claude-opus-4-8") == opus


def test_session_plan_pcts():
    class _R:
        def __init__(self, client, session_id, model, fresh, cache_read=0):
            self.client = client; self.session_id = session_id
            self.model = model
            self.input_tokens = fresh; self.output_tokens = 0
            self.cached_input_tokens = cache_read
            self.cache_creation_input_tokens = 0
            self.cache_read_input_tokens = cache_read
            self.total_tokens_including_cached = fresh + cache_read

    recs = [
        _R("claude-code", "s1", "claude-opus-4-8", 100_000_000),
        _R("claude-code", "s1", "claude-fable-5", 10_000_000),
        _R("claude-code", "s2", "claude-opus-4-8", 50_000_000),
        # cache reads count at alpha (0.5 here): 100M reads ≈ 50M fresh.
        _R("claude-code", "s4", "claude-opus-4-8", 0, cache_read=100_000_000),
        _R("codex", "s3", "gpt-5", 99_000_000),  # non-claude → excluded
    ]
    w = pc.PlanWeights(weights={"claude-opus-4-8": 0.01, "claude-fable-5": 0.10},
                       default_weight=0.01, scale=1.0, confidence="baseline", basis="",
                       intervals_used=0, client="claude-code", alpha=0.5)
    pcts = pc.session_plan_pcts(recs, w, client="claude-code")
    assert set(pcts) == {"s1", "s2", "s4"}                # one pass, grouped, codex out
    assert abs(pcts["s1"] - (100 * 0.01 + 10 * 0.10)) < 1e-9  # 1.0 + 1.0 = 2.0
    assert abs(pcts["s2"] - 0.5) < 1e-9                       # 50M fresh × 0.01
    assert abs(pcts["s4"] - 0.5) < 1e-9                       # 100M reads × 0.01 × α=0.5


# ---------------------------------------------------------------------------
# stability acceptance for out-of-band fits
# ---------------------------------------------------------------------------


def _consistent_history(service, *, ratio, intervals, tokens=50_000_000, t0=1_000_000,
                        spacing=8 * 3600):
    """A weekly-%% history whose movement is a constant ``ratio`` x baseline.

    Default spacing (8h) makes 24+ intervals span more than a week, so the
    stability lane's wall-clock requirement is satisfied; a burst test passes
    a tighter spacing explicitly."""
    opus = pc.baseline_weight_fresh("claude-opus-4-8")
    move = ratio * (tokens / 1_000_000.0 * opus)
    pct = 0.0
    _record_7d(service, captured=float(t0), pct=pct, index=0)
    for i in range(intervals):
        _record_usage(service, client="claude-code", model="claude-opus-4-8",
                      session_id=f"s{i}", tokens=tokens, updated_at=t0 + i * spacing + spacing // 2,
                      cost=1.0)
        pct += move
        _record_7d(service, captured=float(t0 + (i + 1) * spacing), pct=pct, index=i + 1)
    return float(t0 + (intervals + 1) * spacing)


def test_stability_accepts_persistent_out_of_band_fit(tmp_path):
    """The trusted band alone turned an out-of-band account into 'calibrating
    forever' (the live failure: raw fit x2.74 vs band cap 2.5, re-rejected on
    133 clean intervals). A fit outside the band is now accepted when the
    interval count is high and both chronological halves independently agree."""

    service = SentinelService(tmp_path)
    now = _consistent_history(service, ratio=4.0, intervals=pc._STABILITY_MIN_INTERVALS + 2)
    weights = pc.calibrate_plan_weights(service.list_all_events(), client="claude-code", now=now)
    assert weights.confidence == "calibrated"
    assert abs(weights.scale - 4.0) < 0.4
    assert "split-half stability" in weights.basis


def test_stability_rejects_a_drifting_out_of_band_fit(tmp_path):
    """Halves that disagree (the ratio drifted 2x -> 7x mid-window) must NOT be
    stability-accepted: a moving ratio is noise or a regime change, not an
    account property."""

    service = SentinelService(tmp_path)
    t0 = 1_000_000
    opus = pc.baseline_weight_fresh("claude-opus-4-8")
    tokens = 50_000_000
    n = pc._STABILITY_MIN_INTERVALS + 2
    spacing = 8 * 3600
    pct = 0.0
    _record_7d(service, captured=float(t0), pct=pct, index=0)
    for i in range(n):
        ratio = 2.0 if i < n // 2 else 7.0
        _record_usage(service, client="claude-code", model="claude-opus-4-8",
                      session_id=f"s{i}", tokens=tokens, updated_at=t0 + i * spacing + spacing // 2,
                      cost=1.0)
        pct += ratio * (tokens / 1_000_000.0 * opus)
        _record_7d(service, captured=float(t0 + (i + 1) * spacing), pct=pct, index=i + 1)
    weights = pc.calibrate_plan_weights(service.list_all_events(), client="claude-code",
                                        now=float(t0 + (n + 1) * spacing))
    assert weights.confidence == "baseline"
    assert weights.scale == 1.0


def test_stability_rejects_beyond_the_hard_ceiling(tmp_path):
    """No amount of stability makes a fit beyond the hard ceiling credible."""

    service = SentinelService(tmp_path)
    ceiling_ratio = pc._STABILITY_HARD_BAND[1] * 1.5
    now = _consistent_history(service, ratio=ceiling_ratio,
                              intervals=pc._STABILITY_MIN_INTERVALS + 2,
                              tokens=20_000_000)
    weights = pc.calibrate_plan_weights(service.list_all_events(), client="claude-code", now=now)
    assert weights.confidence == "baseline"


def test_stability_needs_more_than_the_minimum_interval_floor(tmp_path):
    """A stable-looking out-of-band fit on only a handful of intervals stays
    baseline — the stability lane requires an order more evidence than the
    in-band floor of 3."""

    service = SentinelService(tmp_path)
    now = _consistent_history(service, ratio=4.0, intervals=6)
    weights = pc.calibrate_plan_weights(service.list_all_events(), client="claude-code", now=now)
    assert weights.confidence == "baseline"
    assert weights.scale == 1.0


def test_stability_rejects_a_single_burst_day(tmp_path):
    """24+ hourly readings from ONE heavy day are numerically stable but span
    far less than the required wall clock — a burst must never self-certify
    (its "halves" are just the morning and afternoon of the same anomaly)."""

    service = SentinelService(tmp_path)
    now = _consistent_history(service, ratio=4.0,
                              intervals=pc._STABILITY_MIN_INTERVALS + 2,
                              spacing=3600)
    weights = pc.calibrate_plan_weights(service.list_all_events(), client="claude-code", now=now)
    assert weights.confidence == "baseline"
    assert weights.scale == 1.0
