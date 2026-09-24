from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from typer.testing import CliRunner

from agentacct.cli import app
from agentacct.evidence_runtime import EvidenceRuntime
from agentacct.evidence_store import EvidenceStore
from agentacct.service import SentinelService


runner = CliRunner()

# The compact-spool result contract the CLI serializes for --json.
SPOOL_COMPACTION_FIELDS = {
    "dry_run",
    "spool_bytes_before",
    "spool_bytes_after",
    "rows_before",
    "rows_after",
    "dropped_rows",
    "kept_rows",
    "dropped_bytes",
    "archived_path",
    "archive_bytes",
    "swapped",
    "generation",
    "verification",
    "warnings",
}


def _seed_tool_activity_shadow(store_dir, count: int) -> None:
    # shadow_skip_event_types=() opts out of the default skip so the seeded
    # tool_activity rows actually land in the projection for the prune to remove.
    runtime = EvidenceRuntime(store_dir, enabled=True, shadow_skip_event_types=())
    for i in range(count):
        runtime.shadow_v1_event(
            {
                "event_id": f"ta-{i}",
                "created_at": 1_750_000_000.0 + i,
                "source": "codex",
                "event_type": "tool_activity_observed",
                "run_id": "run-1",
                "metadata": {"client": "codex", "client_session_id": "session-1", "tool_name": "Bash"},
            },
            transport="mcp",
        )


def test_evidence_prune_cli_dry_run_default(tmp_path) -> None:
    store = tmp_path / "state"
    _seed_tool_activity_shadow(store, 2)

    result = runner.invoke(app, ["evidence", "prune", "--store-dir", str(store), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["matched_versions"] == 2
    assert payload["deleted_versions"] == 0
    assert payload["spool_left_intact"] is True


def test_evidence_prune_cli_real(tmp_path) -> None:
    store = tmp_path / "state"
    _seed_tool_activity_shadow(store, 2)

    deleted = runner.invoke(
        app,
        ["evidence", "prune", "--store-dir", str(store), "--no-dry-run", "--yes", "--no-vacuum", "--json"],
    )
    assert deleted.exit_code == 0, deleted.output
    assert json.loads(deleted.output)["deleted_versions"] == 2

    rerun = runner.invoke(
        app,
        ["evidence", "prune", "--store-dir", str(store), "--no-dry-run", "--yes", "--no-vacuum", "--json"],
    )
    assert rerun.exit_code == 0, rerun.output
    assert json.loads(rerun.output)["matched_versions"] == 0


def test_evidence_prune_cli_refuses_honesty_critical_source(tmp_path) -> None:
    store = tmp_path / "state"
    _seed_tool_activity_shadow(store, 1)

    result = runner.invoke(
        app,
        ["evidence", "prune", "--store-dir", str(store), "--source-type", "client_hook", "--no-dry-run", "--yes"],
    )

    assert result.exit_code != 0


def test_evidence_work_event_preserves_v1_and_adds_v2(tmp_path) -> None:
    store = tmp_path / "state"

    args = [
        "evidence",
        "work-event",
        "--store-dir",
        str(store),
        "--source",
        "codex",
        "--kind",
        "section",
        "--status",
        "completed",
        "--occurred-at",
        "1783900123.5",
        "--source-event-id",
        "codex-section-implementation-1",
        "--run-id",
        "run-1",
        "--section-id",
        "implementation",
        "--client",
        "codex",
        "--client-session-id",
        "session-1",
        "--title",
        "Implement the Evidence v2 shadow layer",
        "--summary",
        "Implemented the Evidence v2 shadow layer and verified both write paths.",
        "--json",
    ]
    result = runner.invoke(app, args)
    retry = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert retry.exit_code == 0, retry.output
    payload = json.loads(result.output)
    assert payload["work_event"]["transport"] == "cli"
    assert payload["work_event"]["event_kind"] == "section"
    assert payload["work_event"]["occurred_at"] == 1_783_900_123.5
    assert payload["work_event"]["source_event_id"] == "codex-section-implementation-1"
    assert payload["v1_event"]["event_type"] == "section_completed"
    assert payload["v1_event"]["metadata"]["client_event_timestamp"] == 1_783_900_123.5
    assert json.loads(retry.output)["v1_event"]["event_id"] == payload["v1_event"]["event_id"]
    v1_events = SentinelService(store).list_all_events()
    assert len(v1_events) == 1
    assert (store / "evidence-v2" / "spool.jsonl").is_file()

    listing = runner.invoke(app, ["evidence", "list", "--store-dir", str(store), "--json"])
    assert listing.exit_code == 0, listing.output
    envelopes = json.loads(listing.output)["evidence"]
    assert len(envelopes) == 1
    assert envelopes[0]["receipt_count"] == 2
    assert envelopes[0]["duplicate_receipt_count"] == 1
    assert envelopes[0]["envelope"]["source_instance"] == "v1-cli"


def test_evidence_status_product_and_idempotent_v1_replay(tmp_path) -> None:
    store = tmp_path / "state"
    service = SentinelService(store)
    service.record_event(
        {
            "source": "codex",
            "event_type": "task_started",
            "run_id": "run-1",
            "metadata": {"summary": "Start", "client_session_id": "session-1"},
        },
        transport="mcp",
    )

    status = runner.invoke(app, ["evidence", "status", "--store-dir", str(store), "--json"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["stats"]["evidence_versions"] == 1

    replay = runner.invoke(app, ["evidence", "replay-v1", "--store-dir", str(store), "--json"])
    assert replay.exit_code == 0, replay.output
    replay_payload = json.loads(replay.output)
    # The explicit replay transport is a distinct source instance, so the
    # first migration adds one compatibility version; repeating is idempotent.
    assert replay_payload["inserted_count"] == 1
    replay_again = runner.invoke(app, ["evidence", "replay-v1", "--store-dir", str(store), "--json"])
    assert json.loads(replay_again.output)["duplicate_count"] == 1

    product = runner.invoke(app, ["evidence", "product", "--store-dir", str(store), "--json"])
    assert product.exit_code == 0, product.output
    product_payload = json.loads(product.output)
    assert product_payload["summary"]["evidence_count"] == 2
    assert set(product_payload) >= {
        "work_graph",
        "evidence_matrix",
        "discrepancies",
        "cost_outcome_basis",
    }


def test_evidence_commands_honor_kill_switch(tmp_path) -> None:
    store = tmp_path / "state"
    store.mkdir()

    status = runner.invoke(
        app,
        ["evidence", "status", "--store-dir", str(store), "--json"],
        env={"AGENT_CHRONICLE_EVIDENCE_V2": "0"},
    )

    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["enabled"] is False
    assert not (store / "evidence-v2").exists()


def _seed_compaction_store(store_dir, *, unreachable: int, live: int = 1) -> None:
    """Seed a tmp store with rows the spool compaction may drop and rows it must keep.

    ``live`` rows stay in the projection. The ``unreachable`` tool_activity
    shadow rows are then pruned out of it: the forward-only replay cursor never
    rewinds, so those spool bytes can no longer answer any query — exactly the
    historical bloat `compact-spool` reclaims.
    """

    service = SentinelService(store_dir)
    for i in range(live):
        service.record_event(
            {
                "source": "codex",
                "event_type": "task_started",
                "run_id": f"run-{i}",
                "metadata": {"summary": "Start", "client_session_id": f"session-{i}"},
            },
            transport="mcp",
        )
    _seed_tool_activity_shadow(store_dir, unreachable)
    runtime = EvidenceRuntime(store_dir, enabled=True, shadow_skip_event_types=())
    pruned = runtime.store.prune_versions(dry_run=False, vacuum=False, batch_size=100)
    assert pruned.deleted_versions == unreachable


def _projection_counts(store_dir) -> dict[str, int]:
    stats = EvidenceStore(store_dir).stats().to_dict()
    return {key: value for key, value in stats.items() if key != "spool_bytes"}


def test_evidence_compact_spool_never_invents_a_store(tmp_path) -> None:
    absent = tmp_path / "no-such-store"

    result = runner.invoke(app, ["evidence", "compact-spool", "--store-dir", str(absent)])

    assert result.exit_code != 0
    assert "No agentacct store at" in result.output
    assert not absent.exists()


def test_evidence_compact_spool_is_a_dry_run_by_default(tmp_path) -> None:
    store = tmp_path / "state"
    _seed_compaction_store(store, unreachable=2)
    evidence_root = store / "evidence-v2"
    spool = evidence_root / "spool.jsonl"
    before = (
        spool.stat().st_size,
        spool.stat().st_mtime_ns,
        sorted(path.name for path in evidence_root.iterdir()),
    )

    result = runner.invoke(app, ["evidence", "compact-spool", "--store-dir", str(store), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["swapped"] is False
    assert payload["rows_before"] == 3
    assert payload["dropped_rows"] == 2
    assert payload["kept_rows"] == 1
    assert payload["dropped_bytes"] > 0
    # A dry run may name the archive it would write, but must never create one.
    assert not list(evidence_root.rglob("*archive*"))
    assert (
        spool.stat().st_size,
        spool.stat().st_mtime_ns,
        sorted(path.name for path in evidence_root.iterdir()),
    ) == before

    human = runner.invoke(app, ["evidence", "compact-spool", "--store-dir", str(store)])
    assert human.exit_code == 0, human.output
    assert human.output.startswith("DRY RUN")
    assert "Nothing was removed. Re-run with --write --yes to compact the spool." in human.output


def test_evidence_compact_spool_write_requires_yes(tmp_path) -> None:
    store = tmp_path / "state"
    _seed_compaction_store(store, unreachable=2)
    spool = store / "evidence-v2" / "spool.jsonl"
    before = spool.read_bytes()

    result = runner.invoke(app, ["evidence", "compact-spool", "--store-dir", str(store), "--write"])

    assert result.exit_code != 0
    assert "--yes" in result.output
    assert spool.read_bytes() == before


def test_evidence_compact_spool_write_compacts_and_keeps_the_projection(tmp_path) -> None:
    store = tmp_path / "state"
    _seed_compaction_store(store, unreachable=2)
    spool = store / "evidence-v2" / "spool.jsonl"
    size_before = spool.stat().st_size
    counts_before = _projection_counts(store)
    listing_before = runner.invoke(app, ["evidence", "list", "--store-dir", str(store), "--json"])
    assert listing_before.exit_code == 0, listing_before.output

    result = runner.invoke(
        app,
        ["evidence", "compact-spool", "--store-dir", str(store), "--write", "--yes", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is False
    assert payload["swapped"] is True
    assert payload["rows_before"] == 3
    assert payload["rows_after"] == 1
    assert payload["dropped_rows"] == 2
    assert payload["kept_rows"] == 1
    assert payload["spool_bytes_before"] == size_before
    assert spool.stat().st_size == payload["spool_bytes_after"] < size_before
    # The removed rows are recoverable from the archive, at whatever size or
    # compression the store chose for it.
    archive = Path(payload["archived_path"])
    assert archive.is_file()
    assert payload["archive_bytes"] == archive.stat().st_size > 0

    # Queries, receipts, and the reopened store see exactly what they saw before.
    listing_after = runner.invoke(app, ["evidence", "list", "--store-dir", str(store), "--json"])
    assert listing_after.exit_code == 0, listing_after.output
    assert json.loads(listing_after.output) == json.loads(listing_before.output)
    assert _projection_counts(store) == counts_before

    human = runner.invoke(
        app,
        ["evidence", "compact-spool", "--store-dir", str(store), "--write", "--yes"],
    )
    assert human.exit_code == 0, human.output
    assert human.output.startswith("Nothing to compact —")
    assert "Drop: nothing" in human.output
    assert "This is local storage maintenance:" in human.output


def test_evidence_compact_spool_no_archive_leaves_no_archive_file(tmp_path) -> None:
    store = tmp_path / "state"
    _seed_compaction_store(store, unreachable=2)
    spool = store / "evidence-v2" / "spool.jsonl"
    size_before = spool.stat().st_size

    result = runner.invoke(
        app,
        [
            "evidence",
            "compact-spool",
            "--store-dir",
            str(store),
            "--write",
            "--yes",
            "--no-archive",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["swapped"] is True
    assert payload["dropped_rows"] == 2
    assert payload["archived_path"] is None
    assert payload["archive_bytes"] == 0
    # The archive lives in the store's own archive/ directory, so check the
    # whole tmp tree for one instead of only the evidence root.
    assert not list(tmp_path.rglob("*archive*"))
    assert not list(tmp_path.rglob("*.zst"))
    assert spool.stat().st_size == payload["spool_bytes_after"] < size_before


def test_evidence_compact_spool_json_matches_the_result_dataclass(tmp_path) -> None:
    store = tmp_path / "state"
    _seed_compaction_store(store, unreachable=2)

    result = runner.invoke(app, ["evidence", "compact-spool", "--store-dir", str(store), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    direct = EvidenceStore(store).compact_spool()
    assert direct.dry_run is True
    assert {field.name for field in dataclasses.fields(direct)} == SPOOL_COMPACTION_FIELDS
    assert set(payload) == SPOOL_COMPACTION_FIELDS

    # The CLI serializes the same fields, so every value matches the dataclass
    # once both sides are put through the same JSON normalization.
    expected = json.loads(json.dumps({field: getattr(direct, field) for field in SPOOL_COMPACTION_FIELDS}, default=str))
    assert payload == expected
    assert isinstance(payload["verification"], dict)
    assert isinstance(payload["warnings"], list)
    assert payload["swapped"] is False


def test_evidence_compact_spool_survives_a_full_projection_rebuild(tmp_path) -> None:
    store = tmp_path / "state"
    _seed_compaction_store(store, unreachable=2)
    counts_before = _projection_counts(store)
    listing_before = runner.invoke(app, ["evidence", "list", "--store-dir", str(store), "--json"])
    assert listing_before.exit_code == 0, listing_before.output

    compacted = runner.invoke(
        app,
        ["evidence", "compact-spool", "--store-dir", str(store), "--write", "--yes", "--json"],
    )
    assert compacted.exit_code == 0, compacted.output
    assert json.loads(compacted.output)["dropped_rows"] == 2

    # Compaction has to remove rows, not merely hide them from the live cursor:
    # a projection rebuilt from scratch out of the compacted spool must still
    # answer exactly what the store answered before.
    projection = store / "evidence-v2" / "projection.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        sidecar = Path(f"{projection}{suffix}")
        if sidecar.exists():
            sidecar.unlink()
    assert _projection_counts(store) == counts_before

    listing_after = runner.invoke(app, ["evidence", "list", "--store-dir", str(store), "--json"])
    assert listing_after.exit_code == 0, listing_after.output
    assert json.loads(listing_after.output) == json.loads(listing_before.output)
