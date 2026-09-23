from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from agentacct.client_usage import ClientUsageEvent
from agentacct.receipt_snapshot_refresh import usage_refresh_preserves_snapshots
from agentacct.usage_truth import mark_trusted_local_usage_import_event


def _usage(client="codex"):
    return ClientUsageEvent(
        client=client, client_session_id="session", source_path=Path("/client/rollout.jsonl"),
        title="Work title", cwd="/work/project", model="example-model",
        input_tokens=100, output_tokens=20, cached_input_tokens=30,
        cache_read_input_tokens=30, reasoning_output_tokens=10,
        input_tokens_reported=True, output_tokens_reported=True,
        total_tokens_reported=True, total_tokens=120,
        cache_read_tokens_reported=True, cache_creation_tokens_reported=False,
        started_at=100, updated_at=200, turn_count=1,
        client_reported_cost_usd=0.1, client_cost_source="client_source",
        raw_usage_rows=1, deduplicated_usage_rows=1,
        raw_cumulative_input_tokens=130, raw_cumulative_cached_input_tokens=30,
        raw_cumulative_output_tokens=20, raw_cumulative_reasoning_output_tokens=10,
        source_namespace_fingerprint="sha256:" + "a" * 64,
        source_revision_at=200_000_000, source_revision_basis="source_timestamp",
    )


def _record(usage=None, *, event_id="evt_aaaaaaaaaaaa", created_at=300.0):
    event = mark_trusted_local_usage_import_event((usage or _usage()).to_sentinel_event())
    event.update(event_id=event_id, created_at=created_at)
    return event


@pytest.mark.parametrize("client", ["codex", "claude-code"])
def test_actual_importer_numeric_refresh_preserves_snapshot_and_inputs(client):
    usage = _usage(client)
    old = _record(usage)
    new = _record(replace(
        usage, input_tokens=200, output_tokens=50, cached_input_tokens=60,
        cache_read_input_tokens=60, reasoning_output_tokens=15, total_tokens=250,
        client_reported_cost_usd=0.25, turn_count=2, raw_usage_rows=3,
        deduplicated_usage_rows=2, updated_at=400, source_revision_at=400_000_000,
        raw_cumulative_input_tokens=260, raw_cumulative_cached_input_tokens=60,
        raw_cumulative_output_tokens=50, raw_cumulative_reasoning_output_tokens=15,
    ), event_id="evt_bbbbbbbbbbbb", created_at=500.0)
    before = deepcopy([old, new])
    assert usage_refresh_preserves_snapshots([old], [new])
    assert [old, new] == before


@pytest.mark.parametrize("field,value", [
    ("title", "Changed title"),
    ("model", "another-model"),
    ("cwd", "/another/project"),
    ("source_path", Path("/client/other.jsonl")),
    ("source_namespace_fingerprint", "sha256:" + "b" * 64),
    ("client_session_id", "another-session"),
    ("client_session_kind", "subagent"),
    ("parent_client_session_id", "new-parent"),
    ("usage_row_lane", "new-model-lane"),
    ("input_tokens_reported", False),
    ("source_revision_basis", "different_clock"),
    ("client_cost_source", "another_cost_source"),
    ("started_at", 101),
    ("replay_baseline_input_tokens", 5),
    ("evidenced_event_ids", ("evt_link",)),
    ("evidenced_event_id_total", 1),
])
def test_importer_semantic_or_provenance_changes_invalidate(field, value):
    usage = _usage()
    # Evidence IDs serialize only when total > 0.
    if field == "evidenced_event_ids":
        usage = replace(usage, evidenced_event_id_total=1, evidenced_event_ids=("evt_old",))
    assert not usage_refresh_preserves_snapshots(
        [_record(usage)], [_record(replace(usage, **{field: value}))]
    )


@pytest.mark.parametrize("field,value", [
    ("value_redaction_applied", True),
    ("value_redaction_fields", ["metadata.client_session_title"]),
    ("usage_normalization_state", "held_unknown_lineage"),
    ("usage_additive", False),
    ("pricing_source", "new_catalog"),
    ("unknown_future_field", 1),
    ("evidenced_outputs_skipped", 1),
])
def test_new_metadata_is_not_blanket_ignored(field, value):
    old = _record()
    new = deepcopy(old)
    new["metadata"][field] = value
    assert not usage_refresh_preserves_snapshots([old], [new])


def test_redacted_text_removal_and_provenance_removal_invalidate():
    old = _record()
    old["metadata"].update(value_redaction_applied=True, value_redaction_fields=["metadata.title"])
    for field in ("client_session_title", "value_redaction_applied", "usage_provenance"):
        new = deepcopy(old)
        del new["metadata"][field]
        assert not usage_refresh_preserves_snapshots([old], [new])


def test_changed_existing_replay_baseline_and_prefix_invalidate():
    old = _record(replace(_usage(), replay_baseline_input_tokens=10, replay_prefix_token_events=2))
    for field in ("replay_baseline_input_tokens", "replay_prefix_token_events"):
        new = deepcopy(old)
        new["metadata"][field] += 1
        assert not usage_refresh_preserves_snapshots([old], [new])


def test_deletion_dedup_and_refolding_cannot_hide_in_same_length_lists():
    old = _record()
    other = _record(replace(_usage(), client_session_id="other"))
    assert not usage_refresh_preserves_snapshots([old], [])
    assert not usage_refresh_preserves_snapshots([old, deepcopy(old)], [old, other])
    folded = _record(replace(_usage(), input_tokens=200, output_tokens=40))
    assert not usage_refresh_preserves_snapshots([old, other], [folded, deepcopy(folded)])


def test_arbitrary_event_is_exact_and_order_and_appends_are_allowed():
    old = _record()
    arbitrary = {"event_type": "machine_check", "created_at": 100, "metadata": {"title": "Check"}}
    new = _record(replace(_usage(), input_tokens=200))
    assert usage_refresh_preserves_snapshots([arbitrary, old], [new, arbitrary, {"event_type": "new"}])
    assert usage_refresh_preserves_snapshots([], [arbitrary, new])
    assert usage_refresh_preserves_snapshots([], [])
    for changed in ({**arbitrary, "created_at": 200}, {**arbitrary, "metadata": {"title": "Changed"}}):
        assert not usage_refresh_preserves_snapshots([arbitrary, old], [changed, new])


@pytest.mark.parametrize("mutation", ["untrusted", "nonusage", "fallback", "nonadditive", "wrongsource"])
def test_numeric_exception_does_not_extend_to_other_lanes(mutation):
    old = _record()
    if mutation == "untrusted":
        del old["metadata"]["usage_provenance"]
    elif mutation == "nonusage":
        old["event_type"] = "machine_check"
    elif mutation == "fallback":
        old["metadata"]["precedence_role"] = "fallback"
    elif mutation == "nonadditive":
        old["metadata"]["usage_additive"] = False
    else:
        old["source"] = "arbitrary_source"
    new = deepcopy(old)
    new["estimated_input_tokens"] += 1
    assert usage_refresh_preserves_snapshots([old], [deepcopy(old)])
    assert not usage_refresh_preserves_snapshots([old], [new])


@pytest.mark.parametrize("field,value", [
    ("estimated_input_tokens", True), ("estimated_input_tokens", "hidden text"),
    ("estimated_input_tokens", -1), ("estimated_input_tokens", 1.5),
    ("estimated_cost_usd", float("nan")), ("estimated_cost_usd", float("inf")),
    ("created_at", {"text": "cannot disappear"}),
])
def test_invalid_counter_values_fail_closed(field, value):
    old = _record()
    new = deepcopy(old)
    new[field] = value
    assert not usage_refresh_preserves_snapshots([old], [new])


def test_counter_presence_nullability_and_unknown_event_ids_are_preserved():
    old = _record()
    for value in (None, "not-generated", "generated-local-usage-event"):
        new = deepcopy(old)
        new["event_id"] = value
        assert not usage_refresh_preserves_snapshots([old], [new])
    for field in ("total_tokens", "updated_at"):
        new = deepcopy(old)
        del new["metadata"][field]
        assert not usage_refresh_preserves_snapshots([old], [new])
        new["metadata"][field] = None
        assert not usage_refresh_preserves_snapshots([old], [new])


def test_price_provenance_change_is_not_just_numeric_refresh():
    old = _record()
    for field in ("cost_confidence", "cost_basis", "usage_confidence"):
        new = deepcopy(old)
        new[field] = "different"
        assert not usage_refresh_preserves_snapshots([old], [new])
