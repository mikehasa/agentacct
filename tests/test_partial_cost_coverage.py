"""Cost coverage regression for local JSON usage imports (issue #243).

Only synthetic usage counters are used. The open-file shim lets this parser
test run on Windows; the normal source-file safety path is tested elsewhere
on the project's supported Linux/macOS platforms.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import agentacct.client_usage as usage


def _usage_rows(client: str, costs: list[object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, cost in enumerate(costs, start=1):
        if client == "opencode":
            part: dict[str, object] = {
                "type": "step-finish",
                "tokens": {"input": 100, "output": 10},
            }
            if cost is not None:
                part["cost"] = cost
            rows.append({"sessionID": "synthetic", "part": part})
        else:
            counter: dict[str, object] = {"input": 100, "output": 10}
            if cost is not None:
                counter["cost"] = {"total": cost}
            rows.append({
                "type": "message",
                "message": {
                    "role": "assistant",
                    "timestamp": index * 1000,
                    "usage": counter,
                },
            })
    return rows


def _read_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: str,
    costs: list[object],
    *,
    add_noise: bool = False,
) -> dict[str, object]:
    path = tmp_path / f"{client}.jsonl"
    rows = _usage_rows(client, costs)
    if add_noise:
        rows.extend([
            rows[0],  # Exact replay of an accepted turn.
            {"type": "message", "message": {"role": "user", "usage": {"input": 100}}},
            {"type": "message", "message": {
                "role": "assistant", "timestamp": 9999,
                "usage": {"input": 0, "output": 0},
            }},
        ])
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    file_stat = path.stat()
    source = usage._RegularSourceFile(
        path=path,
        root=tmp_path,
        mtime=file_stat.st_mtime,
        mtime_ns=file_stat.st_mtime_ns,
        device=file_stat.st_dev,
        inode=file_stat.st_ino,
        size=file_stat.st_size,
    )
    monkeypatch.setattr(usage, "_open_regular_source_text", lambda item: item.path.open("r", encoding="utf-8"))
    reader = usage._read_opencode_json_usage if client == "opencode" else usage._read_openclaw_jsonl_usage
    result = reader(source)
    assert result is not None
    return result


@pytest.mark.parametrize("client", ["opencode", "openclaw"])
@pytest.mark.parametrize(
    "costs",
    [
        pytest.param([1.25, None], id="missing-last"),
        pytest.param([None, 1.25], id="missing-first"),
        pytest.param(["invalid", 1.25], id="invalid-text"),
        pytest.param(["NaN", 1.25], id="not-a-number"),
        pytest.param(["Infinity", 1.25], id="infinite"),
        pytest.param([False, 1.25], id="false-placeholder"),
        pytest.param([True, 1.25], id="true-placeholder"),
        pytest.param([0, None], id="zero-then-missing"),
    ],
)
def test_partial_cost_does_not_become_complete_session_cost(tmp_path, monkeypatch, client, costs):
    result = _read_rows(tmp_path, monkeypatch, client, costs)
    assert result["turn_count"] == 2
    assert result["input_tokens"] == 200
    assert result["output_tokens"] == 20
    assert result["cost_usd"] is None


@pytest.mark.parametrize("client", ["opencode", "openclaw"])
def test_complete_cost_keeps_reported_sum(tmp_path, monkeypatch, client):
    result = _read_rows(tmp_path, monkeypatch, client, [1.25, 0.5])
    assert result["turn_count"] == 2
    assert result["cost_usd"] == 1.75


@pytest.mark.parametrize("client", ["opencode", "openclaw"])
def test_no_cost_stays_unknown(tmp_path, monkeypatch, client):
    result = _read_rows(tmp_path, monkeypatch, client, [None, None])
    assert result["turn_count"] == 2
    assert result["cost_usd"] is None


def test_openclaw_positive_cost_with_zero_placeholder_stays_unknown(tmp_path, monkeypatch):
    result = _read_rows(tmp_path, monkeypatch, "openclaw", [1.25, 0])
    assert result["turn_count"] == 2
    assert result["cost_usd"] is None


def test_opencode_explicit_zero_keeps_existing_semantics(tmp_path, monkeypatch):
    result = _read_rows(tmp_path, monkeypatch, "opencode", [0, 0])
    assert result["turn_count"] == 2
    assert result["cost_usd"] == 0


def test_openclaw_ignores_replay_and_non_usage_rows_in_cost_coverage(tmp_path, monkeypatch):
    result = _read_rows(tmp_path, monkeypatch, "openclaw", [1.25, 0.5], add_noise=True)
    assert result["turn_count"] == 2
    assert result["input_tokens"] == 200
    assert result["output_tokens"] == 20
    assert result["cost_usd"] == 1.75


@pytest.mark.parametrize("client", ["opencode", "openclaw"])
def test_refresh_plan_replaces_legacy_partial_cost_without_token_growth(tmp_path, client):
    old = usage.ClientUsageEvent(
        client=client,
        client_session_id="synthetic",
        source_path=tmp_path / "synthetic.jsonl",
        title=None,
        cwd=None,
        model=None,
        input_tokens=200,
        output_tokens=20,
        turn_count=2,
        client_reported_cost_usd=1.25,
    )
    corrected = replace(old, client_reported_cost_usd=None)
    stored = old.to_sentinel_event()
    stored["event_id"] = "old-partial-cost"

    plan = usage.plan_local_usage_import([corrected], [stored])

    assert plan.refresh_candidates == [corrected]
    assert plan.unchanged_candidates == []
