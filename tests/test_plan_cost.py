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


def test_session_tokens_by_model(tmp_path):
    service = SentinelService(tmp_path)
    _record_usage(service, client="claude-code", model="claude-opus-4-8", session_id="s1",
                  tokens=100, updated_at=1000, cost=0.1)
    _record_usage(service, client="claude-code", model="claude-fable-5", session_id="s1",
                  tokens=40, updated_at=1001, cost=0.1)
    _record_usage(service, client="claude-code", model="claude-opus-4-8", session_id="s2",
                  tokens=999, updated_at=1002, cost=0.1)
    tbm = pc.session_tokens_by_model(service.list_all_events(), client="claude-code", session_id="s1")
    assert tbm == {"claude-opus-4-8": 100.0, "claude-fable-5": 40.0}  # only s1, grouped by model


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
    assert weights.weight_for("claude-opus-4-8") == pc.BASELINE_MODEL_WEIGHTS["claude-opus-4-8"]


def test_calibrate_fits_scale_from_history(tmp_path):
    # Construct history where the account's OWN weekly-% moves exactly 2x what the
    # baseline predicts → the fitted per-user scale should be ~2.0 (calibrated).
    service = SentinelService(tmp_path)
    t0 = 1_000_000
    opus = pc.BASELINE_MODEL_WEIGHTS["claude-opus-4-8"]  # %/Mtoken
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


def test_calibrate_untrusted_scale_falls_back_to_baseline(tmp_path):
    # Regression (review): if the meter moved ~10x what local tokens predict (heavy
    # untracked usage, or a very different tier we can't identify), the fitted scale is
    # outside the trusted band → keep the baseline rather than a 10x over-estimate.
    service = SentinelService(tmp_path)
    t0 = 1_000_000
    opus = pc.BASELINE_MODEL_WEIGHTS["claude-opus-4-8"]
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
        def __init__(self, client, session_id, model, tok):
            self.client = client; self.session_id = session_id
            self.model = model; self.total_tokens_including_cached = tok

    recs = [
        _R("claude-code", "s1", "claude-opus-4-8", 100_000_000),
        _R("claude-code", "s1", "claude-fable-5", 10_000_000),
        _R("claude-code", "s2", "claude-opus-4-8", 50_000_000),
        _R("codex", "s3", "gpt-5", 99_000_000),  # non-claude → excluded
    ]
    w = pc.PlanWeights(weights={"claude-opus-4-8": 0.01, "claude-fable-5": 0.10},
                       default_weight=0.01, scale=1.0, confidence="baseline", basis="",
                       intervals_used=0, client="claude-code")
    pcts = pc.session_plan_pcts(recs, w, client="claude-code")
    assert set(pcts) == {"s1", "s2"}                      # one pass, grouped, codex out
    assert abs(pcts["s1"] - (100 * 0.01 + 10 * 0.10)) < 1e-9  # 1.0 + 1.0 = 2.0
    assert abs(pcts["s2"] - 0.5) < 1e-9                       # 50M × 0.01
