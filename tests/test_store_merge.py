"""Phase 2.8: dedup-safe, additive cross-store event merge.

`usage merge-store` folds a source store's events into a target store without
rewriting the target. The tests pin the honesty-critical invariants: event ids
and provenance/trust markers survive verbatim (so the Phase 2.7 client-log
evidence join can re-link merged sections), re-merges add nothing, target rows
are never touched, dry-run writes nothing, and the `--kind mcp` filter skips
usage rows.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from agentacct.cli import app
from agentacct.service import SentinelService
from agentacct.store_merge import plan_store_merge
from agentacct.usage_truth import (
    LOCAL_SESSION_OBSERVATION_PROVENANCE,
    LOCAL_SESSION_OBSERVATION_SOURCE,
    LOCAL_USAGE_PROVENANCE,
    LOCAL_USAGE_SOURCE,
    is_local_session_observation_event,
    is_local_usage_import_event,
    mark_trusted_local_session_observation_event,
    reduce_local_session_observation_events,
)
from agentacct.work_ledger import build_work_ledger

runner = CliRunner()


def _read(store: Path) -> list[dict]:
    events_path = store / "events.jsonl"
    if not events_path.is_file():
        return []
    out = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _write_store(store: Path, events: list[dict]) -> None:
    (store / "runs").mkdir(parents=True, exist_ok=True)
    with (store / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, sort_keys=True) + "\n")


def _section(event_id: str, **extra) -> dict:
    event = {
        "event_id": event_id,
        "created_at": 1_700_000_000.0,
        "event_type": "section_started",
        "source": "codex",
        "run_id": None,
        "metadata": {"section_id": event_id, "title": "do a thing"},
    }
    event.update(extra)
    return event


def _usage_row(
    event_id: str,
    session: str,
    *,
    namespace: str | None = None,
) -> dict:
    """A trusted local usage-import row (usage-truth shape)."""
    row = {
        "event_id": event_id,
        "created_at": 1_700_000_100.0,
        "event_type": "model_usage",
        "source": "codex-local-session-import",
        "run_id": None,
        "estimated_input_tokens": 100,
        "estimated_output_tokens": 50,
        "metadata": {
            "usage_source": LOCAL_USAGE_SOURCE,
            "usage_provenance": LOCAL_USAGE_PROVENANCE,
            "client": "codex",
            "client_session_id": session,
            "trusted_local_usage_import": True,
        },
    }
    if namespace is not None:
        row["metadata"]["source_namespace_fingerprint"] = namespace
    assert is_local_usage_import_event(row), "fixture must be a real usage-truth row"
    return row


def _observation(
    event_id: str,
    session: str,
    *,
    watermark: float = 1_700_000_100.0,
    namespace: str = "codex-home-a",
    title: str = "Observed session",
) -> dict:
    """A server-trusted, usage-free local session observation."""
    row = mark_trusted_local_session_observation_event(
        {
            "event_id": event_id,
            "created_at": watermark + 1,
            "event_type": "session_observed",
            "source": "codex-local-session-observation-import",
            "run_id": None,
            "metadata": {
                "client": "codex",
                "client_session_id": session,
                "source_namespace_fingerprint": namespace,
                "source_updated_at": watermark,
                "source_parse_complete": True,
                "client_session_title": title,
                "client_session_title_source": "explicit_client_title_field",
                "client_session_title_sanitized": True,
            },
        }
    )
    assert is_local_session_observation_event(row), "fixture must be a trusted observation row"
    return row


# --- pure plan tests ---------------------------------------------------------


def test_plan_adds_new_skips_existing() -> None:
    source = [_section("evt_aaaaaaaaaaaa"), _section("evt_bbbbbbbbbbbb")]
    plan = plan_store_merge(source, {"evt_bbbbbbbbbbbb"}, kind="all")
    assert plan["add_count"] == 1
    assert plan["skipped_existing_count"] == 1
    assert [e["event_id"] for e in plan["events_to_add"]] == ["evt_aaaaaaaaaaaa"]


def test_plan_dedups_within_source() -> None:
    source = [_section("evt_dup000000000"), _section("evt_dup000000000")]
    plan = plan_store_merge(source, set(), kind="all")
    assert plan["add_count"] == 1
    assert plan["skipped_existing_count"] == 1


def test_plan_kind_mcp_filters_usage_rows() -> None:
    source = [
        _section("evt_sec000000000"),
        _usage_row("evt_usage0000000", "sess-1"),
        _observation("evt_observed000001", "sess-observed"),
    ]
    mcp_plan = plan_store_merge(source, set(), kind="mcp")
    assert mcp_plan["add_count"] == 1
    assert mcp_plan["filtered_out_count"] == 2
    assert [e["event_id"] for e in mcp_plan["events_to_add"]] == ["evt_sec000000000"]
    all_plan = plan_store_merge(source, set(), kind="all")
    assert all_plan["add_count"] == 3
    assert all_plan["filtered_out_count"] == 0


def test_plan_all_dedups_same_observation_revision_within_source() -> None:
    first = _observation("evt_observed000002", "same-session")
    replay = _observation("evt_observed000003", "same-session")
    plan = plan_store_merge([first, replay], set(), kind="all")
    assert [event["event_id"] for event in plan["events_to_add"]] == [
        "evt_observed000002"
    ]
    assert plan["skipped_duplicate_observation_count"] == 1
    assert plan["skipped_duplicate_observation_by_type"] == {"session_observed": 1}
    assert plan["skipped_observation_by_reason"] == {"idempotent_revision": 1}
    assert plan["observation_non_conflict_skip_count"] == 1
    assert plan["observation_conflict_count"] == 0
    assert plan["observation_reducer_diagnostics"]["selected_sessions"] == 1


def test_plan_all_quarantines_cross_namespace_observation_identity() -> None:
    home_a = _observation(
        "evt_observed000012",
        "colliding-raw-id",
        namespace="codex-home-a",
    )
    home_b = _observation(
        "evt_observed000013",
        "colliding-raw-id",
        namespace="codex-home-b",
    )
    plan = plan_store_merge([home_a, home_b], set(), kind="all")
    assert [row["event_id"] for row in plan["events_to_add"]] == [
        "evt_observed000012",
        "evt_observed000013",
    ]
    assert plan["skipped_duplicate_observation_count"] == 0
    assert plan["skipped_observation_by_reason"] == {}
    assert plan["observation_non_conflict_skip_count"] == 0
    assert plan["observation_conflict_count"] == 2
    assert plan["preserved_observation_conflict_count"] == 2
    assert plan["observation_reducer_diagnostics"][
        "namespace_conflict_sessions"
    ] == 1


def test_plan_skips_events_without_ids() -> None:
    source = [{"event_type": "note", "metadata": {}}, _section("evt_realid000000")]
    plan = plan_store_merge(source, set(), kind="all")
    assert plan["add_count"] == 1
    assert plan["no_event_id_count"] == 1


# --- service append tests ----------------------------------------------------


def test_append_preserves_event_id_and_created_at(tmp_path: Path) -> None:
    store = tmp_path / "target"
    _write_store(store, [])
    service = SentinelService(store)
    section = _section("evt_preserve0000", created_at=42.5)
    appended = service.append_events_preserving_identity([section])
    assert len(appended) == 1
    persisted = _read(store)
    assert persisted[0]["event_id"] == "evt_preserve0000", "merge must NOT mint a fresh id"
    assert persisted[0]["created_at"] == 42.5, "merge must preserve created_at"


def test_append_preserves_trusted_usage_markers(tmp_path: Path) -> None:
    store = tmp_path / "target"
    _write_store(store, [])
    service = SentinelService(store)
    row = _usage_row("evt_trusted00000", "sess-x")
    service.append_events_preserving_identity([row])
    persisted = _read(store)[0]
    md = persisted["metadata"]
    # record_event(trusted=False) would strip these; the merge must NOT.
    assert md["usage_source"] == LOCAL_USAGE_SOURCE
    assert md["usage_provenance"] == LOCAL_USAGE_PROVENANCE
    assert md.get("trusted_local_usage_import") is True
    assert is_local_usage_import_event(persisted), "merged usage row stays a trusted usage-truth row"


def test_append_is_dedup_safe(tmp_path: Path) -> None:
    store = tmp_path / "target"
    _write_store(store, [_section("evt_already000000")])
    service = SentinelService(store)
    appended = service.append_events_preserving_identity(
        [_section("evt_already000000"), _section("evt_new000000000")]
    )
    assert [e["event_id"] for e in appended] == ["evt_new000000000"]
    ids = [e["event_id"] for e in _read(store)]
    assert ids.count("evt_already000000") == 1


def test_atomic_merge_rechecks_logical_usage_after_stale_plan(tmp_path: Path) -> None:
    store = tmp_path / "target"
    _write_store(store, [])
    service = SentinelService(store)
    source_row = _usage_row("evt_sourceusage001", "same-session")
    stale_plan = plan_store_merge([source_row], set(), kind="all")
    assert stale_plan["add_count"] == 1

    concurrent_row = _usage_row("evt_concurrent001", "same-session")
    service.append_events_preserving_identity([concurrent_row])
    plan, appended = service.merge_events_preserving_identity(
        [source_row],
        kind="all",
    )

    assert appended == []
    assert plan["skipped_duplicate_usage_count"] == 1
    trusted = [event for event in _read(store) if is_local_usage_import_event(event)]
    assert [event["event_id"] for event in trusted] == ["evt_concurrent001"]
    assert sum(int(event.get("estimated_input_tokens") or 0) for event in trusted) == 100


# --- CLI end-to-end ----------------------------------------------------------


def test_cli_merge_is_additive_dedup_safe_and_leaves_target_rows(tmp_path: Path) -> None:
    source = tmp_path / "from" / "state"
    target = tmp_path / "into" / "state"
    target_row = _usage_row("evt_targetrow000", "target-sess")
    _write_store(source, [_section("evt_src000000001"), _section("evt_src000000002")])
    _write_store(target, [target_row])

    result = runner.invoke(
        app, ["usage", "merge-store", "--from", str(source), "--into", str(target)]
    )
    assert result.exit_code == 0, result.output
    assert "Added 2 new event(s)" in result.output

    persisted = _read(target)
    ids = {e["event_id"] for e in persisted}
    assert ids == {"evt_targetrow000", "evt_src000000001", "evt_src000000002"}
    # The pre-existing target row is byte-identical (never rewritten).
    kept = next(e for e in persisted if e["event_id"] == "evt_targetrow000")
    assert kept == target_row

    # Re-merge adds nothing.
    again = runner.invoke(app, ["usage", "merge-store", "--from", str(source), "--into", str(target)])
    assert again.exit_code == 0
    assert "Added 0 new event(s)" in again.output
    assert len(_read(target)) == 3


def test_cli_dry_run_writes_nothing(tmp_path: Path) -> None:
    source = tmp_path / "from" / "state"
    target = tmp_path / "into" / "state"
    _write_store(source, [_section("evt_src000000001")])
    _write_store(target, [_usage_row("evt_targetrow000", "s")])
    before = (target / "events.jsonl").read_bytes()

    result = runner.invoke(
        app, ["usage", "merge-store", "--from", str(source), "--into", str(target), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "Would add 1 new event(s)" in result.output
    assert (target / "events.jsonl").read_bytes() == before, "dry-run must not modify the target"


def test_cli_kind_mcp_skips_usage_rows(tmp_path: Path) -> None:
    source = tmp_path / "from" / "state"
    target = tmp_path / "into" / "state"
    _write_store(source, [_section("evt_sec000000001"), _usage_row("evt_usage0000001", "s")])
    _write_store(target, [])

    result = runner.invoke(
        app, ["usage", "merge-store", "--from", str(source), "--into", str(target), "--kind", "mcp", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["added"] == 1
    assert payload["filtered_out"] == 1
    ids = {e["event_id"] for e in _read(target)}
    assert ids == {"evt_sec000000001"}


def test_cli_kind_mcp_skips_trusted_local_session_observations(tmp_path: Path) -> None:
    source = tmp_path / "from" / "state"
    target = tmp_path / "into" / "state"
    _write_store(
        source,
        [
            _section("evt_sec000000003"),
            _observation("evt_observed000004", "observation-only-session"),
        ],
    )
    _write_store(target, [])

    result = runner.invoke(
        app,
        [
            "usage",
            "merge-store",
            "--from",
            str(source),
            "--into",
            str(target),
            "--kind",
            "mcp",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["added"] == 1
    assert payload["filtered_out"] == 1
    assert {event["event_id"] for event in _read(target)} == {"evt_sec000000003"}


def test_cli_kind_all_dedups_observation_against_target_revision(tmp_path: Path) -> None:
    source = tmp_path / "from" / "state"
    target = tmp_path / "into" / "state"
    source_row = _observation("evt_observed000005", "same-session")
    target_row = _observation("evt_observed000006", "same-session")
    _write_store(source, [source_row])
    _write_store(target, [target_row])

    result = runner.invoke(
        app,
        [
            "usage",
            "merge-store",
            "--from",
            str(source),
            "--into",
            str(target),
            "--kind",
            "all",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["added"] == 0
    assert _read(target) == [target_row]


def test_kind_all_observation_revision_order_is_additive_and_authoritative(
    tmp_path: Path,
) -> None:
    source = tmp_path / "from" / "state"
    target = tmp_path / "into" / "state"
    older = _observation(
        "evt_observed000007",
        "evolving-session",
        watermark=1_700_000_100.0,
        title="Earlier title",
    )
    newer = _observation(
        "evt_observed000008",
        "evolving-session",
        watermark=1_700_000_200.0,
        title="Later title",
    )
    _write_store(source, [newer])
    _write_store(target, [older])

    result = runner.invoke(
        app,
        [
            "usage",
            "merge-store",
            "--from",
            str(source),
            "--into",
            str(target),
            "--kind",
            "all",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["added"] == 1
    persisted = _read(target)
    assert persisted[0] == older, "merge remains additive; target history is untouched"
    selected, diagnostics = reduce_local_session_observation_events(persisted)
    assert diagnostics["selected_sessions"] == 1
    assert selected == [newer], "the newer source revision becomes read authority"
    metadata = selected[0]["metadata"]
    assert metadata["observation_source"] == LOCAL_SESSION_OBSERVATION_SOURCE
    assert metadata["observation_provenance"] == LOCAL_SESSION_OBSERVATION_PROVENANCE


def test_kind_all_skips_older_but_preserves_conflicting_observation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "from" / "state"
    target = tmp_path / "into" / "state"
    current = _observation(
        "evt_observed000009",
        "guarded-session",
        watermark=1_700_000_200.0,
        title="Current",
    )
    older = _observation(
        "evt_observed000010",
        "guarded-session",
        watermark=1_700_000_100.0,
        title="Old",
    )
    _write_store(source, [older])
    _write_store(target, [current])
    result = runner.invoke(
        app,
        [
            "usage",
            "merge-store",
            "--from",
            str(source),
            "--into",
            str(target),
            "--kind",
            "all",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    older_payload = json.loads(result.output)
    assert older_payload["added"] == 0
    assert older_payload["skipped_observation_by_reason"] == {
        "historical_revision": 1
    }
    assert older_payload["observation_non_conflict_skips"] == 1
    assert older_payload["observation_conflict_rows"] == 0
    assert _read(target) == [current]

    # Equal source time but different canonical content is a conflict, not a
    # tie that may be resolved by arrival order.
    conflicting = _observation(
        "evt_observed000011",
        "guarded-session",
        watermark=1_700_000_200.0,
        title="Conflicting",
    )
    _write_store(source, [conflicting])
    conflict = runner.invoke(
        app,
        [
            "usage",
            "merge-store",
            "--from",
            str(source),
            "--into",
            str(target),
            "--kind",
            "all",
            "--json",
        ],
    )
    assert conflict.exit_code == 0, conflict.output
    conflict_payload = json.loads(conflict.output)
    assert conflict_payload["added"] == 1
    assert conflict_payload["skipped_observation_by_reason"] == {}
    assert conflict_payload["observation_non_conflict_skips"] == 0
    assert conflict_payload["observation_conflict_rows"] == 1
    assert conflict_payload["preserved_observation_conflict_rows"] == 1
    assert conflict_payload["observation_reducer_diagnostics"][
        "watermark_conflict_sessions"
    ] == 1
    persisted = _read(target)
    assert persisted == [current, conflicting]
    selected, diagnostics = reduce_local_session_observation_events(persisted)
    assert selected == []
    assert diagnostics["watermark_conflict_sessions"] == 1


def test_kind_all_preserves_unorderable_observation_conflict(
    tmp_path: Path,
) -> None:
    source = tmp_path / "from" / "state"
    target = tmp_path / "into" / "state"
    target_row = _observation(
        "evt_observed000014",
        "unorderable-session",
        title="Earlier content",
    )
    source_row = _observation(
        "evt_observed000015",
        "unorderable-session",
        title="Different content",
    )
    target_row["metadata"].pop("source_updated_at", None)
    target_row["metadata"].pop("source_revision_at", None)
    target_row["metadata"].pop("updated_at", None)
    source_row["metadata"].pop("source_updated_at", None)
    source_row["metadata"].pop("source_revision_at", None)
    source_row["metadata"].pop("updated_at", None)
    # Re-stamp after removing the watermark so the canonical revisions remain
    # trusted but cannot be ordered against one another.
    target_row = mark_trusted_local_session_observation_event(target_row)
    source_row = mark_trusted_local_session_observation_event(source_row)
    _write_store(source, [source_row])
    _write_store(target, [target_row])

    result = runner.invoke(
        app,
        [
            "usage",
            "merge-store",
            "--from",
            str(source),
            "--into",
            str(target),
            "--kind",
            "all",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["added"] == 1
    assert payload["skipped_observation_by_reason"] == {}
    assert payload["observation_conflict_rows"] == 1
    assert payload["preserved_observation_conflict_rows"] == 1
    assert payload["observation_non_conflict_skips"] == 0
    persisted = _read(target)
    assert persisted == [target_row, source_row]
    selected, diagnostics = reduce_local_session_observation_events(persisted)
    assert selected == []
    assert diagnostics["unorderable_revision_sessions"] == 1


def test_cli_source_leftover_untouched(tmp_path: Path) -> None:
    source = tmp_path / "from" / "state"
    target = tmp_path / "into" / "state"
    _write_store(source, [_section("evt_src000000001")])
    _write_store(target, [])
    before = (source / "events.jsonl").read_bytes()
    runner.invoke(app, ["usage", "merge-store", "--from", str(source), "--into", str(target)])
    assert (source / "events.jsonl").read_bytes() == before, "the source store is read-only"


def test_cli_rejects_missing_source_and_self_merge(tmp_path: Path) -> None:
    target = tmp_path / "into" / "state"
    _write_store(target, [])
    missing = runner.invoke(app, ["usage", "merge-store", "--from", str(tmp_path / "nope"), "--into", str(target)])
    assert missing.exit_code == 2
    same = runner.invoke(app, ["usage", "merge-store", "--from", str(target), "--into", str(target)])
    assert same.exit_code == 2


def _usage_totals(events: list[dict]) -> tuple[int, int]:
    """(input, output) token sums over trusted usage-import rows only."""
    inp = out = 0
    for event in events:
        if is_local_usage_import_event(event):
            inp += int(event.get("estimated_input_tokens") or 0)
            out += int(event.get("estimated_output_tokens") or 0)
    return inp, out


# --- FIX 1 (MAJOR — usage double-count) --------------------------------------


def test_kind_all_logical_dedup_of_same_session_no_double_count(tmp_path: Path) -> None:
    """The reviewer's exact case: ONE logical client session imported into BOTH
    stores lands with a DIFFERENT event_id in each (usage event ids are minted
    fresh per import). `--kind all` must skip the source copy as a duplicate
    usage row by its LOGICAL identity (client, base session, lane), NOT its
    event_id — so target token/cost totals are unchanged. Its work events still
    merge in.
    """
    source = tmp_path / "from" / "state"
    target = tmp_path / "into" / "state"

    # Same logical session (codex / codex-shared-1), two distinct event ids.
    src_usage = _usage_row("evt_srcusage00001", "codex-shared-1")
    tgt_usage = _usage_row("evt_tgtusage00001", "codex-shared-1")
    src_section = _section("evt_srcsection001")

    _write_store(source, [src_usage, src_section])
    _write_store(target, [tgt_usage])

    before_in, before_out = _usage_totals(_read(target))

    result = runner.invoke(
        app, ["usage", "merge-store", "--from", str(source), "--into", str(target), "--kind", "all", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # The work event is added; the duplicate-identity usage row is skipped.
    assert payload["added"] == 1
    assert payload["skipped_duplicate_usage"] == 1

    persisted = _read(target)
    ids = {e["event_id"] for e in persisted}
    assert "evt_srcsection001" in ids, "the work event merges in"
    assert "evt_srcusage00001" not in ids, "the duplicate-identity usage row is NOT copied"

    after_in, after_out = _usage_totals(persisted)
    assert (after_in, after_out) == (before_in, before_out), "usage totals must be unchanged (no double-count)"


def test_kind_all_distinct_session_usage_still_merges(tmp_path: Path) -> None:
    """Logical dedup must not over-reject: a usage row for a DIFFERENT session
    (not already in the target) is still copied under --kind all."""
    source = tmp_path / "from" / "state"
    target = tmp_path / "into" / "state"
    _write_store(source, [_usage_row("evt_srcusage00002", "codex-unique-2")])
    _write_store(target, [_usage_row("evt_tgtusage00002", "codex-other-99")])

    before_in, before_out = _usage_totals(_read(target))
    result = runner.invoke(
        app, ["usage", "merge-store", "--from", str(source), "--into", str(target), "--kind", "all", "--json"]
    )
    payload = json.loads(result.output)
    assert payload["added"] == 1
    assert payload["skipped_duplicate_usage"] == 0
    after_in, after_out = _usage_totals(_read(target))
    assert after_in == before_in + 100 and after_out == before_out + 50


def test_default_kind_is_mcp_and_never_touches_usage_rows(tmp_path: Path) -> None:
    """FIX 1(a): the DEFAULT is now --kind mcp. With no --kind flag, usage rows
    are filtered out entirely (target usage totals unchanged), only work events
    merge."""
    source = tmp_path / "from" / "state"
    target = tmp_path / "into" / "state"
    _write_store(source, [_section("evt_mcpsec000001"), _usage_row("evt_srcusage00003", "codex-shared-1")])
    _write_store(target, [_usage_row("evt_tgtusage00003", "codex-shared-1")])

    before_in, before_out = _usage_totals(_read(target))
    result = runner.invoke(
        app, ["usage", "merge-store", "--from", str(source), "--into", str(target), "--json"]  # no --kind
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "mcp", "default kind must be mcp"
    assert payload["added"] == 1
    assert payload["filtered_out"] == 1
    ids = {e["event_id"] for e in _read(target)}
    assert ids == {"evt_tgtusage00003", "evt_mcpsec000001"}
    after_in, after_out = _usage_totals(_read(target))
    assert (after_in, after_out) == (before_in, before_out), "default (mcp) never adds usage rows"


def test_plan_all_logical_dedup_against_target_identities() -> None:
    """Pure-plan pin of the FIX-1 logical dedup contract."""
    from agentacct.store_merge import usage_row_identities

    target_usage = _usage_row("evt_tgt000000001", "codex-shared-1")
    identities = usage_row_identities([target_usage])
    source = [_usage_row("evt_src000000001", "codex-shared-1"), _section("evt_worksec00001")]
    plan = plan_store_merge(source, {"evt_tgt000000001"}, kind="all", target_usage_identities=identities)
    assert plan["skipped_duplicate_usage_count"] == 1
    assert plan["add_count"] == 1
    assert [e["event_id"] for e in plan["events_to_add"]] == ["evt_worksec00001"]


def test_kind_all_preserves_same_usage_identity_from_distinct_explicit_homes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "from" / "state"
    target = tmp_path / "into" / "state"
    source_row = _usage_row(
        "evt_srcexplicit01",
        "shared-raw-session",
        namespace="codex-home-b",
    )
    target_row = _usage_row(
        "evt_tgtexplicit01",
        "shared-raw-session",
        namespace="codex-home-a",
    )
    _write_store(source, [source_row])
    _write_store(target, [target_row])

    result = runner.invoke(
        app,
        [
            "usage",
            "merge-store",
            "--from",
            str(source),
            "--into",
            str(target),
            "--kind",
            "all",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["added"] == 1
    assert payload["skipped_duplicate_usage"] == 0
    assert payload["preserved_cross_namespace_usage"] == 1
    assert payload["skipped_usage_namespace_ambiguous"] == 0
    assert {row["event_id"] for row in _read(target)} == {
        "evt_srcexplicit01",
        "evt_tgtexplicit01",
    }


def test_kind_all_preserves_missing_vs_explicit_target_usage_namespace(
    tmp_path: Path,
) -> None:
    source = tmp_path / "from" / "state"
    target = tmp_path / "into" / "state"
    source_row = _usage_row(
        "evt_srcexplicit02",
        "ambiguous-raw-session",
        namespace="codex-home-a",
    )
    target_row = _usage_row(
        "evt_tgtlegacy0002",
        "ambiguous-raw-session",
    )
    _write_store(source, [source_row])
    _write_store(target, [target_row])

    result = runner.invoke(
        app,
        [
            "usage",
            "merge-store",
            "--from",
            str(source),
            "--into",
            str(target),
            "--kind",
            "all",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["added"] == 1
    assert payload["skipped_duplicate_usage"] == 0
    assert payload["skipped_usage_namespace_ambiguous"] == 0
    assert payload["preserved_ambiguous_usage_namespace"] == 1
    assert payload["preserved_cross_namespace_usage"] == 0
    persisted = _read(target)
    assert persisted == [target_row, source_row]
    ledger = build_work_ledger(persisted)
    assert ledger["usage_events"] == []
    assert ledger["attributions"] == []
    assert ledger["usage_reconciliation"] == []


def test_plan_kind_all_preserves_distinct_explicit_homes_within_source_batch() -> None:
    home_a = _usage_row(
        "evt_batchhomea001",
        "batch-shared-session",
        namespace="codex-home-a",
    )
    home_b = _usage_row(
        "evt_batchhomeb001",
        "batch-shared-session",
        namespace="codex-home-b",
    )

    plan = plan_store_merge([home_a, home_b], set(), kind="all")

    assert [row["event_id"] for row in plan["events_to_add"]] == [
        "evt_batchhomea001",
        "evt_batchhomeb001",
    ]
    assert plan["skipped_duplicate_usage_count"] == 0
    assert plan["preserved_cross_namespace_usage_count"] == 1
    assert plan["skipped_usage_namespace_ambiguous_count"] == 0


def test_plan_kind_all_preserves_missing_vs_explicit_within_source_batch() -> None:
    legacy = _usage_row("evt_batchlegacy01", "batch-ambiguous-session")
    explicit = _usage_row(
        "evt_batchexplicit1",
        "batch-ambiguous-session",
        namespace="codex-home-a",
    )

    plan = plan_store_merge([legacy, explicit], set(), kind="all")

    assert [row["event_id"] for row in plan["events_to_add"]] == [
        "evt_batchlegacy01",
        "evt_batchexplicit1",
    ]
    assert plan["skipped_duplicate_usage_count"] == 0
    assert plan["skipped_usage_namespace_ambiguous_count"] == 0
    assert plan["skipped_usage_namespace_ambiguous_by_type"] == {}
    assert plan["preserved_ambiguous_usage_namespace_count"] == 1
    assert plan["preserved_cross_namespace_usage_count"] == 0


def test_merge_remints_same_event_id_observation_conflict_and_quarantines_after_restart(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    shared_event_id = "evt_observedcollision"
    home_a = _observation(
        shared_event_id,
        "same-id-observation",
        namespace="codex-home-a",
        title="Home A",
    )
    home_b = _observation(
        shared_event_id,
        "same-id-observation",
        namespace="codex-home-b",
        title="Home B",
    )
    _write_store(target, [home_a])

    service = SentinelService(target)
    plan, appended = service.merge_events_preserving_identity([home_b], kind="all")

    assert plan["add_count"] == 1
    assert plan["reminted_truth_event_id_conflicts"] == 1
    assert plan["preserved_observation_conflict_count"] == 1
    assert len(appended) == 1
    assert appended[0]["event_id"] != shared_event_id

    restarted = SentinelService(target, create=False)
    persisted = restarted.list_all_events()
    trusted = [event for event in persisted if is_local_session_observation_event(event)]
    assert len(trusted) == 2
    assert len({event["event_id"] for event in trusted}) == 2
    selected, diagnostics = reduce_local_session_observation_events(trusted)
    assert selected == []
    assert diagnostics["namespace_conflict_sessions"] == 1
    assert build_work_ledger(persisted)["session_rollup"]["sessions"] == []

    second_plan, second_appended = restarted.merge_events_preserving_identity(
        [home_b],
        kind="all",
    )
    assert second_plan["add_count"] == 0
    assert second_plan["reminted_truth_event_id_conflicts"] == 0
    assert second_appended == []
    assert restarted.list_all_events() == persisted


def test_merge_remints_missing_vs_explicit_usage_and_excludes_all_totals(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    shared_event_id = "evt_usagecollision01"
    legacy = _usage_row(shared_event_id, "same-id-usage")
    explicit = _usage_row(
        shared_event_id,
        "same-id-usage",
        namespace="codex-home-a",
    )
    _write_store(target, [legacy])

    service = SentinelService(target)
    plan, appended = service.merge_events_preserving_identity([explicit], kind="all")

    assert plan["add_count"] == 1
    assert plan["reminted_truth_event_id_conflicts"] == 1
    assert plan["preserved_ambiguous_usage_namespace_count"] == 1
    assert len(appended) == 1
    assert appended[0]["event_id"] != shared_event_id

    persisted = SentinelService(target, create=False).list_all_events()
    trusted_usage = [event for event in persisted if is_local_usage_import_event(event)]
    assert len(trusted_usage) == 2
    assert len({event["event_id"] for event in trusted_usage}) == 2
    ledger = build_work_ledger(persisted)
    assert ledger["usage_events"] == []
    assert ledger["attributions"] == []
    assert ledger["usage_reconciliation"] == []
    assert ledger["session_rollup"]["sessions"] == []


def test_merge_remints_explicit_cross_home_usage_once_without_duplicate_attribution(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    shared_event_id = "evt_usagecollision02"
    home_a = _usage_row(
        shared_event_id,
        "same-id-cross-home",
        namespace="codex-home-a",
    )
    home_b = _usage_row(
        shared_event_id,
        "same-id-cross-home",
        namespace="codex-home-b",
    )
    home_b["estimated_input_tokens"] = 900
    home_b["estimated_output_tokens"] = 99
    _write_store(target, [home_a])

    service = SentinelService(target)
    first_plan, first_appended = service.merge_events_preserving_identity(
        [home_b],
        kind="all",
    )
    assert first_plan["add_count"] == 1
    assert first_plan["reminted_truth_event_id_conflicts"] == 1
    assert first_plan["preserved_cross_namespace_usage_count"] == 1
    assert len(first_appended) == 1

    before_remerge = service.list_all_events()
    ledger = build_work_ledger(before_remerge)
    assert sorted(event["total_tokens"] for event in ledger["usage_events"]) == [
        150,
        999,
    ]
    attribution_ids = [row["usage_event_id"] for row in ledger["attributions"]]
    reconciliation_ids = [
        row["usage_event_id"] for row in ledger["usage_reconciliation"]
    ]
    assert len(attribution_ids) == len(set(attribution_ids)) == 2
    assert len(reconciliation_ids) == len(set(reconciliation_ids)) == 2

    second_plan, second_appended = service.merge_events_preserving_identity(
        [home_b],
        kind="all",
    )
    assert second_plan["add_count"] == 0
    assert second_plan["reminted_truth_event_id_conflicts"] == 0
    assert second_appended == []
    after_events = SentinelService(target, create=False).list_all_events()
    assert after_events == before_remerge
    after = build_work_ledger(after_events)
    assert [row["usage_event_id"] for row in after["attributions"]] == attribution_ids
    assert [
        row["usage_event_id"] for row in after["usage_reconciliation"]
    ] == reconciliation_ids


def test_generic_event_id_cannot_preoccupy_truth_and_remerge_is_idempotent(
    tmp_path: Path,
) -> None:
    shared_event_id = "evt_genericcollision"
    for truth_kind in ("observation", "usage"):
        target = tmp_path / truth_kind
        generic = {
            "event_id": shared_event_id,
            "created_at": 1_700_000_000.0,
            "event_type": "note",
            "source": "generic",
            "run_id": None,
            "metadata": {"note": "preoccupy the truth event id"},
        }
        truth = (
            _observation(
                shared_event_id,
                f"cross-kind-{truth_kind}",
                namespace="codex-home-a",
            )
            if truth_kind == "observation"
            else _usage_row(
                shared_event_id,
                f"cross-kind-{truth_kind}",
                namespace="codex-home-a",
            )
        )
        _write_store(target, [generic])
        service = SentinelService(target)

        first_plan, first_appended = service.merge_events_preserving_identity(
            [truth],
            kind="all",
        )
        assert first_plan["add_count"] == 1
        assert first_plan["reminted_truth_event_id_conflicts"] == 1
        assert len(first_appended) == 1
        assert first_appended[0]["event_id"] != shared_event_id

        before_remerge = service.list_all_events()
        ledger = build_work_ledger(before_remerge)
        if truth_kind == "observation":
            assert [
                row["client_session_id"]
                for row in ledger["session_rollup"]["sessions"]
            ] == ["cross-kind-observation"]
            assert ledger["usage_events"] == []
        else:
            assert len(ledger["usage_events"]) == 1
            assert ledger["usage_events"][0]["total_tokens"] == 150
            assert len(ledger["attributions"]) == 1

        second_plan, second_appended = service.merge_events_preserving_identity(
            [truth],
            kind="all",
        )
        assert second_plan["add_count"] == 0
        assert second_plan["reminted_truth_event_id_conflicts"] == 0
        assert second_appended == []
        assert SentinelService(target, create=False).list_all_events() == before_remerge


# --- FIX 3 (minor — torn JSONL on append) ------------------------------------


def test_append_guards_missing_trailing_newline(tmp_path: Path) -> None:
    """A target whose last line has NO trailing newline must not tear on append:
    every pre-existing row AND every appended row parses (no `}{`, no dropped
    row)."""
    store = tmp_path / "target"
    (store / "runs").mkdir(parents=True, exist_ok=True)
    pre_existing = _usage_row("evt_notrailing00", "codex-pre-1")
    # Deliberately write WITHOUT a trailing newline (last byte is '}').
    (store / "events.jsonl").write_text(json.dumps(pre_existing, sort_keys=True), encoding="utf-8")
    assert (store / "events.jsonl").read_bytes()[-1:] != b"\n"

    service = SentinelService(store)
    section = _section("evt_appended00001")
    service.append_events_preserving_identity([section])

    # Both rows must parse cleanly (no torn `}{` line).
    persisted = _read(store)
    ids = {e["event_id"] for e in persisted}
    assert ids == {"evt_notrailing00", "evt_appended00001"}, "pre-existing row survives and new row parses"


def test_merged_sections_link_via_client_log_evidence(tmp_path: Path) -> None:
    """The whole point: a section merged into a store whose usage rows already
    reference its event_id (client-log evidence) becomes session-linked, not
    orphaned. This mirrors the legacy-project-a -> global join."""
    from agentacct.work_ledger import build_work_ledger

    section_id = "evt_abcdef012345"  # must be valid hex for the evidence id regex
    # Target store: a trusted usage row whose client log evidenced the section id.
    usage = _usage_row("evt_0123456789ab", "codex-session-1")
    usage["metadata"]["evidenced_event_ids"] = [section_id]
    usage["metadata"]["evidenced_event_id_total"] = 1
    source = tmp_path / "from" / "state"
    target = tmp_path / "into" / "state"
    _write_store(source, [_section(section_id)])
    _write_store(target, [usage])

    # Before merge: section absent, nothing evidenced-in-store.
    before = build_work_ledger(_read(target), store_scope="custom")
    assert before["insights"]["client_log_evidence"]["evidenced_events_in_store"] == 0

    runner.invoke(app, ["usage", "merge-store", "--from", str(source), "--into", str(target), "--kind", "mcp"])

    after = build_work_ledger(_read(target), store_scope="custom")
    cle = after["insights"]["client_log_evidence"]
    assert cle["evidenced_events_in_store"] == 1, "the merged section is now evidenced in-store"
    assert cle["implied_session_keys"] >= 1, "the section gained an implied session key from the log evidence"
    # The section became a work item grouped under its real session (not orphaned).
    items = after["work_items"]
    grouped = [i for i in items if i.get("client_session_id") == "codex-session-1"]
    assert grouped, "merged section links to the usage session, not an orphan bucket"
