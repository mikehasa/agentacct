"""A selected usage period retains the same trust/coverage as the full cube."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
import time

import pytest

from agentacct.usage_cube import usage_bucket_date
from tests.test_tokens_explorer import TODAY, _client, _cube, _cube_record, _trusted_usage, _ts


@pytest.mark.parametrize("granularity", ["daily", "weekly"])
def test_period_breakdown_separates_clients_and_providers_for_the_same_model(granularity):
    records = [
        _cube_record(client="codex", provider="openai", model="gpt-6-astra", session="shared-id",
                     day=TODAY, input_tokens=100, output_tokens=20, cache_creation=30,
                     cache_read=700, cost=0.5),
        _cube_record(client="hermes", provider="openai", model="gpt-6-astra", session="shared-id",
                     day=TODAY, input_tokens=40, output_tokens=10, cache_creation=0,
                     cache_read=100, cost=0.2),
        _cube_record(client="hermes", provider="router", model="gpt-6-astra", session="shared-id",
                     day=TODAY, input_tokens=5, output_tokens=1, cost=None),
    ]
    cube = _cube(records, days=7, granularity=granularity)
    period = next(row for row in cube["by_period"] if row["rows"])
    models = {(row["client"], row["provider"], row["model"]): row for row in period["by_model"]}
    assert set(models) == {
        ("codex", "openai", "gpt-6-astra"),
        ("hermes", "openai", "gpt-6-astra"),
        ("hermes", "router", "gpt-6-astra"),
    }
    assert period["by_model"] == cube["by_model"]
    for client in cube["by_client"]:
        assert period["by_client"][client["client"]] == {
            key: value for key, value in client.items() if key != "client"
        }
    assert period["sessions"] == 2
    assert period["by_client"]["hermes"]["sessions"] == 1
    assert models[("codex", "openai", "gpt-6-astra")]["total_tokens_including_cached"] == 850
    assert period["total_tokens_including_cached"] == 1006
    assert sum(row["total_tokens_including_cached"] for row in models.values()) == 1006
    assert period["by_client"]["hermes"]["cost_complete"] is False
    assert period["by_client"]["hermes"]["estimated_cost_usd"] == pytest.approx(0.2)
    assert models[("hermes", "router", "gpt-6-astra")]["estimated_cost_usd"] is None


def test_period_breakdown_preserves_zero_unknown_partial_cost_and_held_usage():
    reported_zero = _cube_record(day=TODAY, session="zero", cost=0.0)
    unknown = _cube_record(day=TODAY, session="unknown", cost=None,
                           cache_read_reported=None, cache_creation_reported=False)
    held = replace(_cube_record(day=TODAY, session="held", model="held-model", cost=99,
                                input_tokens=99_000, cache_read=999_000), usage_additive=False)
    period = next(row for row in _cube([reported_zero, unknown, held], days=7)["by_period"] if row["rows"])
    lane = period["by_client"]["codex"]
    models = {row["model"]: row for row in period["by_model"]}
    assert lane["usage_availability"] == "partial"
    assert lane["estimated_cost_usd"] is None
    assert lane["known_additive_cost_usd"] == 0.0
    assert lane["cost_complete"] is False
    assert lane["priced_rows"] == lane["unpriced_rows"] == lane["excluded_non_additive_rows"] == 1
    assert lane["fresh_tokens"] == 250
    assert lane["cache_read_reporting"] == "partial"
    assert lane["cache_creation_reporting"] == "partial"
    assert models["gpt-5.5"]["estimated_cost_usd"] == 0.0
    assert models["gpt-5.5"]["cost_complete"] is False
    assert models["gpt-5.5"]["cache_read_reporting"] == "partial"
    assert models["gpt-5.5"]["cache_creation_reporting"] == "partial"
    assert models["gpt-5.5"]["cache_read_reported_rows"] == 1
    assert models["gpt-5.5"]["cache_read_unknown_rows"] == 1
    assert models["held-model"]["usage_availability"] == "held"
    assert models["held-model"]["fresh_tokens"] == 0
    assert models["held-model"]["estimated_cost_usd"] is None


def test_period_confidence_and_empty_or_unknown_days_keep_their_own_scope():
    records = [
        _cube_record(day=TODAY, session="estimated", cost=1, cost_confidence="estimated_from_tokens"),
        _cube_record(day=TODAY, session="reported", cost=1, cost_confidence="client_reported"),
        _cube_record(day=TODAY - timedelta(days=2), session="old", model="other", cost=3),
        _cube_record(timestamp=float("inf"), session="unknown-time", cost=99),
    ]
    cube = _cube(records, days=None)
    periods = {row["period"]: row for row in cube["by_period"]}
    empty = periods[(TODAY - timedelta(days=1)).isoformat()]
    assert empty["by_client"] == {}
    assert empty["by_model"] == []
    lane = periods[TODAY.isoformat()]["by_client"]["codex"]
    assert lane["cost_confidence_mixed"] is True
    assert "mostly" not in lane["cost_confidence_label"]
    assert lane["estimated_cost_usd"] == 2
    assert len(periods["unknown"]["by_model"]) == 1
    assert periods["unknown"]["by_model"][0]["estimated_cost_usd"] == 99
    bounded = _cube(records, days=7, model="gpt-5.5")
    assert "unknown" not in {row["period"] for row in bounded["by_period"]}
    assert all(model["model"] == "gpt-5.5" for period in bounded["by_period"] for model in period["by_model"])


def test_multiday_cumulative_session_is_attributed_once_without_inventing_daily_usage():
    early = _ts(TODAY - timedelta(days=2))
    late = _ts(TODAY)
    records = [
        replace(_cube_record(client="codex", session="codex", day=TODAY), started_at=early, updated_at=late),
        replace(_cube_record(client="hermes", session="hermes", day=TODAY), started_at=early, updated_at=late),
    ]
    periods = {row["period"]: row for row in _cube(records, days=7)["by_period"]}
    assert set(periods[TODAY.isoformat()]["by_client"]) == {"codex"}
    assert set(periods[(TODAY - timedelta(days=2)).isoformat()]["by_client"]) == {"hermes"}
    assert periods[(TODAY - timedelta(days=1)).isoformat()]["by_model"] == []
    assert sum(row["fresh_tokens"] for row in periods.values()) == 250


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="requires process timezone control")
def test_period_model_breakdown_uses_the_same_local_day_across_dst_boundary():
    original = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        # Both are March 8 UTC, but the first is still March 7 locally.
        before = datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc).timestamp()
        after = datetime(2026, 3, 8, 10, 30, tzinfo=timezone.utc).timestamp()
        assert usage_bucket_date(before).isoformat() == "2026-03-07"
        assert usage_bucket_date(after).isoformat() == "2026-03-08"
        cube = _cube([_cube_record(timestamp=before, session="before"),
                      _cube_record(timestamp=after, session="after")], days=None)
        populated = [row for row in cube["by_period"] if row["rows"]]
        assert [row["period"] for row in populated] == ["2026-03-07", "2026-03-08"]
        assert [row["by_model"][0]["sessions"] for row in populated] == [1, 1]
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


def test_usage_summary_exposes_selected_day_client_model_cost_breakdown(tmp_path):
    store = tmp_path / "state"
    for client, cost in (("codex", 0.1), ("hermes", None)):
        _trusted_usage(store, session=f"{client}-session", client=client, model="shared-model",
                       started_at=_ts(TODAY), cost=cost)
    response = _client(store).get("/usage/summary?days=all&granularity=daily")
    assert response.status_code == 200
    payload = response.json()
    assert payload["period_attribution"]["basis"] == "saved_session_row"
    assert payload["period_attribution"]["timezone"] == "daemon_local"
    assert payload["period_attribution"]["exact_daily_usage"] is False
    period = next(row for row in payload["by_period"] if row["rows"])
    assert len(period["by_model"]) == 2
    assert period["by_client"]["codex"]["estimated_cost_usd"] == pytest.approx(0.1)
    assert period["by_client"]["hermes"]["estimated_cost_usd"] is None
    assert period["by_model"] == payload["by_model"]
