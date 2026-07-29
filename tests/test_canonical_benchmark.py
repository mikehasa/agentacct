from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from agentacct.canonical.benchmark import BenchmarkHookError, benchmark_sqlite_truth
from agentacct.canonical.sqlite import CanonicalRepository


_HARNESS_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "sqlite_truth_benchmark.py"
_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "canonical" / "v1" / "spike.json"
_SPEC = importlib.util.spec_from_file_location("canonical_benchmark_fixture_module", _HARNESS_PATH)
assert _SPEC is not None and _SPEC.loader is not None
harness = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = harness
_SPEC.loader.exec_module(harness)


def _request(scratch: Path, *, mode: str, source: dict[str, object]) -> dict[str, object]:
    scratch.chmod(0o700)
    return {
        "schema_version": "agent-chronicle.sqlite-truth-benchmark-request.v1",
        "mode": mode,
        "scratch_dir": str(scratch.resolve()),
        "candidate_db": str((scratch / "candidate.sqlite3").resolve()),
        "source": source,
    }


def _snapshot_source(root: Path, *relatives: str) -> dict[str, object]:
    files: list[dict[str, object]] = []
    for relative in relatives:
        path = root / relative
        raw = path.read_bytes()
        files.append(
            {
                "path": relative,
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    manifest = root.parent / f"{root.name}-manifest-{len(list(root.parent.glob('*-manifest-*.json')))}.json"
    encoded = json.dumps(
        {"version": 1, "kind": "codex", "files": files},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest.write_bytes(encoded)
    return {
        "kind": "verified_snapshot",
        "manifest_kind": "codex",
        "manifest_integrity_verified": True,
        "known_live_path": False,
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
        "snapshot_root": str(root.resolve()),
        "snapshot_manifest": str(manifest.resolve()),
        "files": [
            {
                "relative_path": item["path"],
                "path": str((root / str(item["path"])).resolve()),
                "size_bytes": item["size_bytes"],
                "sha256": item["sha256"],
            }
            for item in files
        ],
    }


@pytest.mark.parametrize(
    "variable",
    ("AGENT_CHRONICLE_STORE_DIR", "AGENT_SENTINEL_STORE_DIR"),
)
def test_direct_hook_rejects_scratch_overlapping_configured_live_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    source = harness.write_normal_corpus(scratch / "normal.jsonl", 1)
    monkeypatch.setenv(variable, str(scratch / "nested-live-store"))

    with pytest.raises(BenchmarkHookError, match="disjoint.*configured live state root"):
        benchmark_sqlite_truth(_request(scratch, mode="normal", source=source))

    assert not (scratch / "candidate.sqlite3").exists()


def test_default_normal_hook_reports_explicit_churn_and_query_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    source = harness.write_normal_corpus(scratch / "normal.jsonl", 12)
    observed_usage_queries = []
    real_usage_days = CanonicalRepository.usage_days

    def capture_usage_query(self, query):
        observed_usage_queries.append(query)
        return real_usage_days(self, query)

    monkeypatch.setattr(CanonicalRepository, "usage_days", capture_usage_query)

    result = benchmark_sqlite_truth(_request(scratch, mode="normal", source=source))

    assert result["status"] == "passed"
    assert result["acceptance_passed"] is True
    assert all(result["gates"].values())
    assert result["churn"]["equivalent_attempt_count"] == 1_100_000
    assert result["churn"]["canonical_writes"] == 0
    assert result["churn"]["canonical_sequence_delta"] == 0
    assert result["churn"]["database_growth_bytes"] < 10_000_000
    assert result["churn"]["gate_passed"] is True
    assert result["import"]["unchanged_reconciliation"] == {
        "session_attempts": 2,
        "usage_attempts": 2,
        "physical_writes": 0,
        "canonical_sequence_delta": 0,
        "gate_passed": True,
    }
    assert result["gates"]["exact_replay_zero_write"] is True
    assert result["query"]["plan_gate_passed"] is True
    assert result["query"]["unbounded_core_scans"] == []
    assert observed_usage_queries
    assert {query.client for query in observed_usage_queries} == {"codex"}


def test_144_record_normal_corpus_matches_the_declared_fixture_contract(
    tmp_path: Path,
) -> None:
    fixture = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))["normal_corpus"]
    expected_generation = fixture["generation"]
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    source = harness.write_normal_corpus(
        scratch / "normal.jsonl",
        expected_generation["record_count"],
    )

    result = benchmark_sqlite_truth(_request(scratch, mode="normal", source=source))

    assert result["status"] == "passed"
    assert result["import"]["records"] == expected_generation["record_count"]
    assert result["import"]["sessions"] == expected_generation["session_count"]
    assert result["import"]["tasks"] == expected_generation["task_anchor_count"]
    assert result["import"]["dispositions"] == {
        "fact_conflict": expected_generation["source_conflict_count"],
        "fact_inserted": expected_generation["semantic_fact_count"],
        # Corrections now inherit the predecessor's valid Task/session scope
        # atomically, so the benchmark's explicit re-link is a physical no-op.
        "link_noop": expected_generation["task_anchor_count"],
        "session_inserted": expected_generation["session_count"],
        "session_noop": fixture["expected"]["unchanged_reconcile_session_count"],
        "usage_inserted": expected_generation["canonical_usage_measurement_count"],
        "usage_noop": expected_generation["task_anchor_count"],
        "usage_updated": expected_generation["task_anchor_count"],
    }
    unchanged = result["import"]["unchanged_reconciliation"]
    assert unchanged["session_attempts"] == fixture["expected"][
        "unchanged_reconcile_session_count"
    ]
    assert unchanged["usage_attempts"] == fixture["expected"][
        "unchanged_reconcile_usage_count"
    ]
    assert unchanged["physical_writes"] == fixture["expected"][
        "unchanged_reconcile_canonical_writes"
    ]
    assert unchanged["canonical_sequence_delta"] == 0
    assert unchanged["gate_passed"] is True
    assert {
        key: result["table_counts"][key]
        for key in fixture["expected"]["post_churn_probe_table_counts"]
    } == fixture["expected"]["post_churn_probe_table_counts"]


def test_default_snapshot_hook_requires_nonzero_clean_reader_coverage(tmp_path: Path) -> None:
    root = tmp_path / "offline-codex"
    rollout = root / "sessions" / "2026" / "07" / "rollout-session-a.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": "session-a"}}),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 100,
                                    "cached_input_tokens": 20,
                                    "output_tokens": 10,
                                    "reasoning_output_tokens": 1,
                                    "total_tokens": 110,
                                }
                            },
                            "model": "gpt-synthetic",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    result = benchmark_sqlite_truth(
        _request(
            scratch,
            mode="snapshot",
            source=_snapshot_source(root, rollout.relative_to(root).as_posix()),
        )
    )

    assert result["status"] == "passed"
    assert result["import"]["coverage_passed"] is True
    assert result["import"]["observed_sessions"] == 1
    assert result["import"]["usage_events"] == 1
    assert result["import"]["verified_rollout_files"] == 1
    assert result["import"]["result_source_files"] == 1
    assert result["import"]["missing_rollout_files"] == 0

    import sqlite3

    connection = sqlite3.connect(scratch / "candidate.sqlite3")
    try:
        usage = connection.execute(
            "SELECT input_tokens, input_tokens_reported, output_tokens, output_tokens_reported, "
            "reasoning_output_tokens, reasoning_output_tokens_reported FROM usage_measurements"
        ).fetchone()
    finally:
        connection.close()
    assert usage == (80, 1, 10, 1, 1, 1)


def test_default_snapshot_hook_keeps_missing_output_distinct_from_explicit_zero(
    tmp_path: Path,
) -> None:
    import sqlite3

    root = tmp_path / "offline-codex"
    missing_rollout = root / "sessions" / "rollout-missing.jsonl"
    zero_rollout = root / "sessions" / "rollout-zero.jsonl"
    missing_rollout.parent.mkdir(parents=True)

    def rollout(session_id: str, *, explicit_zero: bool) -> str:
        counters: dict[str, int] = {"input_tokens": 100, "total_tokens": 100}
        if explicit_zero:
            counters.update(
                {
                    "cached_input_tokens": 0,
                    "cache_write_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                }
            )
        return (
            json.dumps({"type": "session_meta", "payload": {"id": session_id}})
            + "\n"
            + json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "info": {"total_token_usage": counters},
                        "model": "gpt-synthetic",
                    },
                }
            )
            + "\n"
        )

    missing_rollout.write_text(rollout("missing", explicit_zero=False), encoding="utf-8")
    zero_rollout.write_text(rollout("zero", explicit_zero=True), encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    result = benchmark_sqlite_truth(
        _request(
            scratch,
            mode="snapshot",
            source=_snapshot_source(
                root,
                missing_rollout.relative_to(root).as_posix(),
                zero_rollout.relative_to(root).as_posix(),
            ),
        )
    )

    assert result["status"] == "passed"
    connection = sqlite3.connect(scratch / "candidate.sqlite3")
    try:
        rows = connection.execute(
            "SELECT session.client_session_id, usage.input_tokens, "
            "usage.input_tokens_reported, usage.output_tokens, "
            "usage.output_tokens_reported, usage.reasoning_output_tokens, "
            "usage.reasoning_output_tokens_reported, usage.cached_input_tokens, "
            "usage.cached_input_tokens_reported, "
            "usage.cache_creation_input_tokens, "
            "usage.cache_creation_input_tokens_reported, "
            "usage.cache_read_input_tokens, "
            "usage.cache_read_input_tokens_reported "
            "FROM usage_measurements usage "
            "JOIN sessions session ON session.session_id = usage.session_id "
            "ORDER BY session.client_session_id"
        ).fetchall()
    finally:
        connection.close()
    assert rows == [
        ("missing", None, 0, None, 0, None, 0, None, 0, None, 0, None, 0),
        ("zero", 100, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1),
    ]
    assert result["usage_field_presence"]["measurement_rows"] == 2
    assert result["usage_field_presence"]["fields"]["input_tokens"] == {
        "reported_rows": 1,
        "missing_rows": 1,
        "explicit_zero_rows": 0,
    }
    assert result["usage_field_presence"]["fields"]["output_tokens"] == {
        "reported_rows": 1,
        "missing_rows": 1,
        "explicit_zero_rows": 1,
    }
    assert result["usage_field_presence"]["fields"][
        "reasoning_output_tokens"
    ] == {
        "reported_rows": 1,
        "missing_rows": 1,
        "explicit_zero_rows": 1,
    }
    for field_name in (
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        assert result["usage_field_presence"]["fields"][field_name] == {
            "reported_rows": 1,
            "missing_rows": 1,
            "explicit_zero_rows": 1,
        }


def test_snapshot_hook_surfaces_sqlite_int64_overflow_as_failed_coverage(
    tmp_path: Path,
) -> None:
    import sqlite3

    root = tmp_path / "offline-codex"
    rollout = root / "sessions" / "rollout-overflow.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"id": "overflow-session"}}
        )
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 2**63,
                            "output_tokens": 0,
                            "total_tokens": 2**63,
                        }
                    },
                    "model": "gpt-synthetic",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    result = benchmark_sqlite_truth(
        _request(
            scratch,
            mode="snapshot",
            source=_snapshot_source(
                root,
                rollout.relative_to(root).as_posix(),
            ),
        )
    )

    assert result["status"] == "failed"
    assert "invalid_sqlite_integer" in result["import"]["coverage_failures"]
    # This counter covers values that reached a reported canonical binding.
    # Missing cache-read presence makes input unreported/NULL, so only the
    # independently reported total overflow is an invalid binding candidate.
    assert result["import"]["invalid_sqlite_integer_values"] == 1
    connection = sqlite3.connect(scratch / "candidate.sqlite3")
    try:
        row = connection.execute(
            "SELECT input_tokens, input_tokens_reported, output_tokens, "
            "output_tokens_reported, total_tokens, total_tokens_reported "
            "FROM usage_measurements"
        ).fetchone()
    finally:
        connection.close()
    assert row == (None, 0, 0, 1, None, 0)


def test_default_snapshot_hook_fails_closed_on_zero_coverage_and_unread_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "offline-codex"
    ignored = root / "sessions" / "rollout-empty.jsonl"
    ignored.parent.mkdir(parents=True)
    ignored.write_text("{}\n", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    result = benchmark_sqlite_truth(
        _request(
            scratch,
            mode="snapshot",
            source=_snapshot_source(root, ignored.relative_to(root).as_posix()),
        )
    )

    assert result["status"] == "failed"
    assert result["acceptance_passed"] is False
    assert result["import"]["coverage_failures"] == [
        "zero_observed_sessions",
        "zero_usage_events",
        "reader_errors",
        "incomplete_rollout_coverage",
    ]

    second_scratch = tmp_path / "second-scratch"
    second_scratch.mkdir()
    unsafe_root = tmp_path / "unsafe-offline-codex"
    unsafe_root.mkdir()
    state = unsafe_root / "state_5.sqlite"
    state.write_bytes(b"not-consumed")
    with pytest.raises(BenchmarkHookError, match="non-source"):
        benchmark_sqlite_truth(
            _request(
                second_scratch,
                mode="snapshot",
                source=_snapshot_source(unsafe_root, "state_5.sqlite"),
            )
        )


def test_default_snapshot_hook_rejects_transient_unmanifested_rollout_before_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    import agentacct.client_usage as client_usage

    root = tmp_path / "offline-codex"
    declared = root / "sessions" / "rollout-declared.jsonl"
    transient = root / "sessions" / "rollout-transient.jsonl"
    declared.parent.mkdir(parents=True)

    def rollout(session_id: str) -> str:
        return (
            json.dumps({"type": "session_meta", "payload": {"id": session_id}})
            + "\n"
            + json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "info": {"total_token_usage": {"input_tokens": 1}},
                        "model": "gpt-synthetic",
                    },
                }
            )
            + "\n"
        )

    declared.write_text(rollout("declared"), encoding="utf-8")
    source = _snapshot_source(root, declared.relative_to(root).as_posix())
    real_discover = client_usage._discover_codex_usage_from_home

    def discover_with_transient(**kwargs: object) -> list[object]:
        transient.write_text(rollout("transient"), encoding="utf-8")
        try:
            return real_discover(**kwargs)  # type: ignore[arg-type, return-value]
        finally:
            transient.unlink()

    monkeypatch.setattr(
        client_usage,
        "_discover_codex_usage_from_home",
        discover_with_transient,
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    with pytest.raises(BenchmarkHookError, match="outside the verified snapshot manifest"):
        benchmark_sqlite_truth(_request(scratch, mode="snapshot", source=source))

    assert not transient.exists()
    connection = sqlite3.connect(scratch / "candidate.sqlite3")
    try:
        assert connection.execute("SELECT count(*) FROM sessions").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM usage_measurements").fetchone() == (0,)
    finally:
        connection.close()


def test_default_snapshot_hook_rejects_forged_live_root_and_incomplete_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_root = tmp_path / "custom-live-codex"
    live_rollout = live_root / "sessions" / "rollout-live.jsonl"
    live_rollout.parent.mkdir(parents=True)
    live_rollout.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(live_root))
    scratch = tmp_path / "live-scratch"
    scratch.mkdir()
    with pytest.raises(BenchmarkHookError, match="configured live Codex"):
        benchmark_sqlite_truth(
            _request(
                scratch,
                mode="snapshot",
                source=_snapshot_source(live_root, "sessions/rollout-live.jsonl"),
            )
        )
    assert not (scratch / "candidate.sqlite3").exists()

    monkeypatch.delenv("CODEX_HOME")
    root = tmp_path / "mixed-offline"
    valid = root / "sessions" / "rollout-valid.jsonl"
    ignored = root / "sessions" / "rollout-ignored.jsonl"
    valid.parent.mkdir(parents=True)
    valid.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "valid"}})
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "info": {"total_token_usage": {"input_tokens": 1}},
                    "model": "gpt-synthetic",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ignored.write_text("{}\n", encoding="utf-8")
    mixed_scratch = tmp_path / "mixed-scratch"
    mixed_scratch.mkdir()
    result = benchmark_sqlite_truth(
        _request(
            mixed_scratch,
            mode="snapshot",
            source=_snapshot_source(
                root,
                "sessions/rollout-valid.jsonl",
                "sessions/rollout-ignored.jsonl",
            ),
        )
    )
    assert result["status"] == "failed"
    assert "incomplete_rollout_coverage" in result["import"]["coverage_failures"]
    assert result["import"]["missing_rollout_files"] == 1
