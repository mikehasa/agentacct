"""The cost-coverage regression must remain visible after a trusted import."""

from __future__ import annotations

import json

import pytest

from agentacct.client_usage import discover_openclaw_usage, discover_opencode_usage
from agentacct.service import SentinelService
from agentacct.usage_cube import build_usage_cube
from agentacct.usage_view import _record_from_sentinel_event, _usage_record_time


@pytest.mark.parametrize("client", ["opencode", "openclaw"])
@pytest.mark.parametrize(
    "costs,complete",
    [
        pytest.param([1.25, None], False, id="missing-last"),
        pytest.param([None, 1.25], False, id="missing-first"),
        pytest.param([1.25, 0.5], True, id="complete"),
    ],
)
def test_partial_cost_is_not_a_complete_stored_session(tmp_path, client, costs, complete):
    home = tmp_path / client
    if client == "opencode":
        path = home / "synthetic.jsonl"
        rows = []
        for cost in costs:
            part = {"type": "step-finish", "tokens": {"input": 100, "output": 10}}
            if cost is not None:
                part["cost"] = cost
            rows.append({"type": "step_finish", "sessionID": "synthetic", "part": part})
    else:
        path = home / "agents" / "main" / "sessions" / "synthetic.jsonl"
        rows = []
        for index, cost in enumerate(costs, start=1):
            usage = {"input": 100, "output": 10, "cacheRead": 0, "cacheWrite": 0}
            if cost is not None:
                usage["cost"] = {"total": cost}
            rows.append({"type": "message", "message": {
                "role": "assistant", "timestamp": index * 2000,
                "provider": "synthetic-provider", "model": "synthetic-model", "usage": usage,
            }})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    events = (
        discover_opencode_usage(opencode_home=home)
        if client == "opencode"
        else discover_openclaw_usage(openclaw_home=home)
    )
    event, = events
    assert event.turn_count == 2
    assert event.input_tokens == 200
    assert event.output_tokens == 20
    assert event.client_reported_cost_usd == (1.75 if complete else None)
    payload = event.to_sentinel_event()
    assert payload["cost_confidence"] == ("client_reported" if complete else "unknown")

    saved = SentinelService(tmp_path / f"{client}-store").record_event(
        payload, trusted_usage_import=True,
    )
    record = _record_from_sentinel_event(saved)
    assert record is not None
    totals = build_usage_cube([record], record_time=_usage_record_time)["totals"]
    assert totals["total_tokens_including_cached"] == 220
    assert totals["cost_complete"] is complete
    assert totals["priced_rows"] == (1 if complete else 0)
    assert totals["unpriced_rows"] == (0 if complete else 1)
