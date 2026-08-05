from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import agentacct.client_usage as client_usage
from agentacct.cli import app
from agentacct.client_usage import (
    ClientUsageEvent,
    discover_client_usage_with_diagnostics,
    usage_less_session_observations,
)
from agentacct.api import create_local_api_app
from agentacct.ingestion_health import IngestionHealthStore, health_scan_results
from agentacct.event_log import RAW_EVENT_LOG_FILENAME, RawEventLog
from agentacct.service import SentinelService
from agentacct.source_discovery import discover_usage_sources
from agentacct.usage_truth import is_local_session_observation_event


def _cursor_home(tmp_path: Path, rows: list[tuple[str, dict]]) -> Path:
    home = tmp_path / "Cursor"
    storage = home / "User" / "globalStorage"
    storage.mkdir(parents=True)
    connection = sqlite3.connect(storage / "state.vscdb")
    try:
        # Matches the real source's permissive BLOB declaration while storing
        # JSON text, as Cursor currently does.
        connection.execute(
            "create table cursorDiskKV (key text primary key, value blob)"
        )
        connection.executemany(
            "insert into cursorDiskKV(key, value) values (?, ?)",
            [
                (f"composerData:{key}", json.dumps(value))
                for key, value in rows
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return home


def _composer(
    composer_id: str,
    *,
    created_at: int = 1_750_000_000_000,
    updated_at: int | None = None,
    model: object = "gpt-5",
    children: list[str] | None = None,
    subagents: list[str] | None = None,
) -> dict:
    value: dict = {
        "composerId": composer_id,
        "createdAt": created_at,
        "subComposerIds": children or [],
        "subagentComposerIds": subagents or [],
        # Privacy canaries must never cross the SQL allowlist.
        "name": "PRIVATE TITLE",
        "prompt": "PRIVATE PROMPT",
        "messages": [{"content": "PRIVATE MESSAGE"}],
        "tokenUsage": {"inputTokens": 999_999},
    }
    if updated_at is not None:
        value["lastUpdatedAt"] = updated_at
    if model is not None:
        value["modelConfig"] = {"modelName": model}
    return value


def _scan(home: Path, *, limit: int = 20):
    return discover_client_usage_with_diagnostics(
        client="cursor",
        cursor_home=home,
        limit_sessions=limit,
    )


def test_cursor_import_is_metadata_only_and_folds_children(tmp_path: Path) -> None:
    home = _cursor_home(
        tmp_path,
        [
            (
                "root",
                _composer(
                    "root",
                    updated_at=1_750_000_200_000,
                    children=["child"],
                ),
            ),
            (
                "child",
                _composer(
                    "child",
                    updated_at=1_750_000_300_000,
                    model="claude-4-sonnet",
                ),
            ),
        ],
    )

    result = _scan(home)

    assert result.events == []
    assert len(result.session_observations) == 2
    by_id = {
        observation.client_session_id: observation
        for observation in result.session_observations
    }
    assert by_id["root"].client_session_kind == "root"
    assert by_id["child"].client_session_kind == "child"
    assert by_id["child"].parent_client_session_id == "root"
    assert by_id["child"].source_namespace_fingerprint == by_id["root"].source_namespace_fingerprint
    assert by_id["root"].title is None
    assert by_id["root"].cwd is None
    assert by_id["child"].observed_models == ("claude-4-sonnet",)
    assert len(usage_less_session_observations(result)) == 2
    serialized = json.dumps(
        [observation.to_sentinel_event() for observation in result.session_observations]
    )
    for secret in ("PRIVATE TITLE", "PRIVATE PROMPT", "PRIVATE MESSAGE", "999999"):
        assert secret not in serialized
    for forbidden in (
        '"provider"',
        '"model"',
        '"estimated_input_tokens"',
        '"estimated_output_tokens"',
        '"estimated_cost_usd"',
        '"usage_confidence"',
        '"cost_confidence"',
    ):
        assert forbidden not in serialized
    assert result.diagnostics["cursor"] == {
        "client": "cursor",
        "discovered": 2,
        "parsed": 2,
        "skipped": 0,
        "error_count": 0,
        "error_codes": [],
        "watermark": 1_750_000_300,
        "limit_unit": "root_groups",
        "selected_root_groups": 1,
        "returned_root_groups": 1,
        "returned_rows": 0,
        "excluded_by_limit": 0,
        "ignored_non_transcript_files": 0,
        "unresolved_identity_files": 0,
        "excluded_by_source_namespace": 0,
        "unparsed_selected_rows": 0,
        "observed_sessions": 2,
        "usage_sessions": 0,
        "sessions_without_usage": 2,
        "source_present": True,
    }


def test_cursor_default_or_missing_model_stays_unknown_without_fake_model(
    tmp_path: Path,
) -> None:
    home = _cursor_home(
        tmp_path,
        [
            ("default", _composer("default", model="default")),
            ("missing", _composer("missing", model=None)),
            ("empty", _composer("empty", model="")),
        ],
    )

    result = _scan(home)

    assert len(result.session_observations) == 3
    assert all(not row.observed_models for row in result.session_observations)


def test_cursor_limit_is_root_group_based_and_keeps_children(tmp_path: Path) -> None:
    home = _cursor_home(
        tmp_path,
        [
            (
                "old-root",
                _composer("old-root", updated_at=1_750_000_100_000, children=["old-child"]),
            ),
            ("old-child", _composer("old-child", updated_at=1_750_000_200_000)),
            ("new-root", _composer("new-root", updated_at=1_750_000_900_000)),
        ],
    )

    result = _scan(home, limit=1)

    assert [row.client_session_id for row in result.session_observations] == ["new-root"]
    diagnostic = result.diagnostics["cursor"]
    assert diagnostic["discovered"] == 3
    assert diagnostic["selected_root_groups"] == 1
    assert diagnostic["excluded_by_limit"] == 1
    assert diagnostic["observed_sessions"] == 1


def test_cursor_accepts_consistent_seconds_and_milliseconds(tmp_path: Path) -> None:
    now = int(time.time()) - 60
    home = _cursor_home(
        tmp_path,
        [
            (
                "seconds",
                _composer(
                    "seconds",
                    created_at=now - 60,
                    updated_at=now - 30,
                ),
            ),
            (
                "milliseconds",
                _composer(
                    "milliseconds",
                    created_at=(now - 60) * 1_000,
                    updated_at=(now - 30) * 1_000,
                ),
            ),
        ],
    )

    result = _scan(home)

    assert result.diagnostics["cursor"]["error_codes"] == []
    observed = {
        row.client_session_id: row for row in result.session_observations
    }
    assert set(observed) == {"seconds", "milliseconds"}
    assert observed["seconds"].started_at == now - 60
    assert observed["seconds"].updated_at == now - 30
    assert observed["milliseconds"].started_at == now - 60
    assert observed["milliseconds"].updated_at == now - 30


@pytest.mark.parametrize(
    ("created_at", "updated_at"),
    [
        (0, None),
        (-1, None),
        (10**30, None),
        (int((time.time() + 2 * 24 * 60 * 60) * 1_000), None),
        (1_750_000_100_000, 1_750_000_000_000),
        (1_750_000_000, 1_750_000_001_000),
    ],
)
def test_cursor_invalid_timestamp_semantics_fail_closed_before_root_limit(
    tmp_path: Path,
    created_at: int,
    updated_at: int | None,
) -> None:
    home = _cursor_home(
        tmp_path,
        [
            (
                "bad",
                _composer(
                    "bad",
                    created_at=created_at,
                    updated_at=updated_at,
                ),
            ),
            (
                "valid",
                _composer(
                    "valid",
                    created_at=1_750_000_000_000,
                    updated_at=1_750_000_100_000,
                ),
            ),
        ],
    )

    result = _scan(home, limit=1)

    assert result.events == []
    assert result.session_observations == []
    diagnostic = result.diagnostics["cursor"]
    assert diagnostic["discovered"] == 2
    assert diagnostic["parsed"] == 0
    assert diagnostic["selected_root_groups"] == 0
    assert diagnostic["error_codes"] == ["cursor_composer_timestamp_invalid"]


def test_cursor_nonfinite_timestamp_fails_with_stable_error() -> None:
    with pytest.raises(
        RuntimeError,
        match="cursor_composer_timestamp_invalid",
    ):
        client_usage._cursor_timestamp_seconds(
            float("inf"),
            now_seconds=time.time(),
        )


@pytest.mark.parametrize(
    ("mutate", "error_code"),
    [
        (
            lambda value: {**value, "composerId": "different"},
            "cursor_composer_identity_mismatch",
        ),
        (
            lambda value: {**value, "composerId": 123},
            "cursor_composer_scalar_schema_invalid",
        ),
        (
            lambda value: {**value, "modelConfig": {"modelName": {"bad": True}}},
            "cursor_composer_scalar_schema_invalid",
        ),
        (
            lambda value: {
                key: item for key, item in value.items() if key != "createdAt"
            },
            "cursor_composer_scalar_schema_invalid",
        ),
        (
            lambda value: {**value, "subComposerIds": "child"},
            "cursor_composer_relationship_schema_invalid",
        ),
        (
            lambda value: {**value, "subComposerIds": [123]},
            "cursor_composer_relationship_schema_invalid",
        ),
        (
            lambda value: {**value, "subComposerIds": ["root"]},
            "cursor_composer_self_cycle",
        ),
    ],
)
def test_cursor_invalid_identity_scalar_and_relationships_fail_closed(
    tmp_path: Path,
    mutate,
    error_code: str,
) -> None:
    home = _cursor_home(tmp_path, [("root", mutate(_composer("root")))])

    result = _scan(home)

    assert result.events == []
    assert result.session_observations == []
    assert result.diagnostics["cursor"]["error_codes"] == [error_code]


def test_cursor_cycle_and_multiple_parents_fail_closed(tmp_path: Path) -> None:
    cycle = _cursor_home(
        tmp_path / "cycle",
        [
            ("a", _composer("a", children=["b"])),
            ("b", _composer("b", children=["a"])),
        ],
    )
    result = _scan(cycle)
    assert result.diagnostics["cursor"]["error_codes"] == ["cursor_composer_cycle"]

    multi = _cursor_home(
        tmp_path / "multi",
        [
            ("a", _composer("a", children=["child"])),
            ("b", _composer("b", subagents=["child"])),
            ("child", _composer("child")),
        ],
    )
    result = _scan(multi)
    assert result.diagnostics["cursor"]["error_codes"] == [
        "cursor_composer_multiple_parents"
    ]


def test_cursor_invalid_json_schema_corruption_and_wal_fail_closed(tmp_path: Path) -> None:
    invalid = _cursor_home(tmp_path / "invalid", [("a", _composer("a"))])
    connection = sqlite3.connect(invalid / "User" / "globalStorage" / "state.vscdb")
    try:
        connection.execute("update cursorDiskKV set value = ?", ("{broken",))
        connection.commit()
    finally:
        connection.close()
    assert _scan(invalid).diagnostics["cursor"]["error_codes"] == [
        "cursor_composer_json_invalid"
    ]

    schema = tmp_path / "schema" / "User" / "globalStorage"
    schema.mkdir(parents=True)
    connection = sqlite3.connect(schema / "state.vscdb")
    try:
        connection.execute("create table cursorDiskKV (key text)")
        connection.commit()
    finally:
        connection.close()
    assert _scan(tmp_path / "schema").diagnostics["cursor"]["error_codes"] == [
        "cursor_state_db_schema_unsupported"
    ]

    corrupt = tmp_path / "corrupt" / "User" / "globalStorage"
    corrupt.mkdir(parents=True)
    (corrupt / "state.vscdb").write_bytes(b"not sqlite")
    assert _scan(tmp_path / "corrupt").diagnostics["cursor"]["error_codes"] == [
        "cursor_state_db_corrupt"
    ]

    wal = _cursor_home(tmp_path / "wal", [("a", _composer("a"))])
    (wal / "User" / "globalStorage" / "state.vscdb-wal").write_bytes(b"active")
    assert _scan(wal).diagnostics["cursor"]["error_codes"] == [
        "cursor_active_wal_not_supported"
    ]


def test_cursor_zero_length_regular_wal_is_safe_after_checkpoint(
    tmp_path: Path,
) -> None:
    home = _cursor_home(tmp_path, [("a", _composer("a"))])
    storage = home / "User" / "globalStorage"
    (storage / "state.vscdb-wal").write_bytes(b"")
    (storage / "state.vscdb-shm").write_bytes(b"regular shared memory sidecar")

    result = _scan(home)

    assert result.diagnostics["cursor"]["error_codes"] == []
    assert [row.client_session_id for row in result.session_observations] == ["a"]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_cursor_symlinked_sqlite_sidecars_fail_closed(
    tmp_path: Path,
    suffix: str,
) -> None:
    home = _cursor_home(tmp_path, [("a", _composer("a"))])
    storage = home / "User" / "globalStorage"
    target = tmp_path / f"outside{suffix}"
    target.write_bytes(b"")
    (storage / f"state.vscdb{suffix}").symlink_to(target)

    result = _scan(home)

    assert result.session_observations == []
    assert result.diagnostics["cursor"]["error_codes"] == [
        "cursor_state_db_sidecar_unsafe"
    ]


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_cursor_non_regular_sqlite_sidecars_fail_closed(
    tmp_path: Path,
    suffix: str,
) -> None:
    home = _cursor_home(tmp_path, [("a", _composer("a"))])
    (home / "User" / "globalStorage" / f"state.vscdb{suffix}").mkdir()

    result = _scan(home)

    assert result.session_observations == []
    assert result.diagnostics["cursor"]["error_codes"] == [
        "cursor_state_db_sidecar_unsafe"
    ]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_cursor_root_global_storage_and_database_symlinks_fail_closed(
    tmp_path: Path,
) -> None:
    real = _cursor_home(tmp_path / "real", [("a", _composer("a"))])
    root_link = tmp_path / "root-link"
    root_link.symlink_to(real, target_is_directory=True)
    assert _scan(root_link).diagnostics["cursor"]["error_codes"] == [
        "cursor_root_symlink_not_allowed"
    ]

    global_root = tmp_path / "global-link"
    (global_root / "User").mkdir(parents=True)
    (global_root / "User" / "globalStorage").symlink_to(
        real / "User" / "globalStorage", target_is_directory=True
    )
    assert _scan(global_root).diagnostics["cursor"]["error_codes"] == [
        "cursor_global_storage_symlink_not_allowed"
    ]

    db_root = tmp_path / "db-link"
    (db_root / "User" / "globalStorage").mkdir(parents=True)
    (db_root / "User" / "globalStorage" / "state.vscdb").symlink_to(
        real / "User" / "globalStorage" / "state.vscdb"
    )
    assert _scan(db_root).diagnostics["cursor"]["error_codes"] == [
        "cursor_state_db_symlink_not_allowed"
    ]


def test_cursor_backup_and_ai_tracking_are_never_scanned(tmp_path: Path) -> None:
    home = tmp_path / "Cursor"
    storage = home / "User" / "globalStorage"
    storage.mkdir(parents=True)
    backup_source = _cursor_home(tmp_path / "backup-source", [("secret", _composer("secret"))])
    shutil.copyfile(
        backup_source / "User" / "globalStorage" / "state.vscdb",
        storage / "state.vscdb.backup",
    )
    ai_tracking = storage / "ai-tracking"
    ai_tracking.mkdir()
    shutil.copyfile(storage / "state.vscdb.backup", ai_tracking / "state.vscdb")

    result = _scan(home)
    source = {row.client: row for row in discover_usage_sources(cursor_home=home)}["cursor"]

    assert result.session_observations == []
    assert result.diagnostics["cursor"]["discovered"] == 0
    assert source.status == "missing"
    assert source.file_count == 0


def test_cursor_source_discovery_matches_importer_trust_boundary(tmp_path: Path) -> None:
    home = _cursor_home(tmp_path, [("a", _composer("a"))])

    result = _scan(home)
    source = {row.client: row for row in discover_usage_sources(cursor_home=home)}["cursor"]

    assert result.diagnostics["cursor"]["error_count"] == 0
    assert source.status == "found"
    assert source.session_count == 1
    assert source.usage_confidence == "unknown"
    assert source.cost_confidence == "unknown"
    assert source.importer == "agentacct usage import-local --client cursor"
    assert "observation-only" in " ".join(source.notes)

    (home / "User" / "globalStorage" / "state.vscdb-wal").write_bytes(b"active")
    failed = _scan(home)
    failed_source = {
        row.client: row for row in discover_usage_sources(cursor_home=home)
    }["cursor"]
    assert failed.diagnostics["cursor"]["error_codes"] == [
        "cursor_active_wal_not_supported"
    ]
    assert failed_source.status == "error"
    assert "cursor_active_wal_not_supported" in " ".join(failed_source.notes)


def test_cursor_in_place_change_during_scan_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _cursor_home(tmp_path, [("a", _composer("a"))])
    original = client_usage._assert_cursor_source_unchanged
    calls = 0

    def change_after_query(source):
        nonlocal calls
        calls += 1
        if calls == 2:
            stat_result = source.path.stat()
            os.utime(
                source.path,
                ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1_000_000),
            )
        original(source)

    monkeypatch.setattr(
        client_usage,
        "_assert_cursor_source_unchanged",
        change_after_query,
    )

    result = _scan(home)

    assert result.session_observations == []
    assert result.diagnostics["cursor"]["error_codes"] == [
        "cursor_state_db_changed_during_scan"
    ]


def test_cursor_cli_persists_observation_only_idempotently(tmp_path: Path) -> None:
    home = _cursor_home(tmp_path, [("a", _composer("a"))])
    store = tmp_path / "chronicle-state"
    args = [
        "usage",
        "import-local",
        "--client",
        "cursor",
        "--store-dir",
        str(store),
        "--cursor-home",
        str(home),
        "--json",
    ]

    first = CliRunner().invoke(app, args)

    assert first.exit_code == 0, first.output
    payload = json.loads(first.output)
    assert payload["scanned_sessions"] == 1
    assert payload["observed_sessions"] == 1
    assert payload["usage_sessions"] == 0
    assert payload["sessions_without_usage"] == 1
    assert payload["imported_events"] == 0
    assert payload["imported_session_observations"] == 1
    stored = SentinelService(store).list_all_events()
    assert len(stored) == 1
    assert is_local_session_observation_event(stored[0])
    assert stored[0]["metadata"]["client"] == "cursor"
    assert not {
        "provider",
        "model",
        "estimated_input_tokens",
        "estimated_output_tokens",
        "estimated_cost_usd",
        "usage_confidence",
        "cost_confidence",
    }.intersection(stored[0])
    ledger = RawEventLog(store / RAW_EVENT_LOG_FILENAME).read_lines()

    second = CliRunner().invoke(app, args)

    assert second.exit_code == 0, second.output
    assert json.loads(second.output)["imported_session_observations"] == 0
    assert RawEventLog(store / RAW_EVENT_LOG_FILENAME).read_lines() == ledger

    human = CliRunner().invoke(app, [arg for arg in args if arg != "--json"])
    assert human.exit_code == 0, human.output
    assert "usage, cache, and cost are unavailable by design" in human.output
    assert "Usage confidence: client_reported" not in human.output
    assert "Tokens: input=0" not in human.output


def test_cursor_can_never_be_constructed_as_usage_event(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot emit usage"):
        ClientUsageEvent(
            client="cursor",  # type: ignore[arg-type]
            client_session_id="cursor-session",
            source_path=tmp_path / "state.vscdb",
            title=None,
            cwd=None,
            model=None,
            input_tokens=0,
            output_tokens=0,
        )


def test_cursor_import_projects_observation_only_on_json_data_lanes(
    tmp_path: Path,
) -> None:
    home = _cursor_home(
        tmp_path,
        [
            ("root", _composer("root", children=["child"])),
            ("child", _composer("child", model="claude-4-sonnet")),
        ],
    )
    store = tmp_path / "chronicle-state"

    imported = CliRunner().invoke(
        app,
        [
            "usage",
            "import-local",
            "--client",
            "cursor",
            "--store-dir",
            str(store),
            "--cursor-home",
            str(home),
            "--json",
        ],
    )

    assert imported.exit_code == 0, imported.output
    payload = json.loads(imported.output)
    assert payload["sessions_without_usage"] == 2
    assert payload["imported_session_observations"] == 2
    api = TestClient(create_local_api_app(store_dir=store))
    sessions = api.get("/sessions", headers={"accept": "application/json"}).json()
    assert sessions["total_sessions"] == 2
    assert all(session["usage"]["rows"] == 0 for session in sessions["sessions"])
    assert all(
        session["usage"]["estimated_cost_usd"] is None
        for session in sessions["sessions"]
    )
    tasks = api.get("/tasks").json()
    assert tasks["summary"]["task_count"] == 1
    assert tasks["tasks"][0]["usage"]["usage_availability"] == "unknown"


def test_cursor_wal_health_has_actionable_single_source_recovery(
    tmp_path: Path,
) -> None:
    home = _cursor_home(tmp_path, [("root", _composer("root"))])
    (home / "User" / "globalStorage" / "state.vscdb-wal").write_bytes(b"active")
    store = tmp_path / "chronicle-state"

    imported = CliRunner().invoke(
        app,
        [
            "usage",
            "import-local",
            "--client",
            "cursor",
            "--store-dir",
            str(store),
            "--cursor-home",
            str(home),
            "--json",
        ],
    )

    assert imported.exit_code == 0, imported.output
    api = TestClient(create_local_api_app(store_dir=store))
    health = api.get("/ingestion/health").json()
    issue = next(issue for issue in health["issues"] if issue["source"] == "cursor")
    assert issue["code"] == "source_snapshot_required"
    assert "Quit Cursor completely" in issue["action"]


@pytest.mark.parametrize(
    ("error_code", "issue_code", "action_fragment"),
    [
        (
            "cursor_state_db_filesystem_read_failed",
            "source_read_permission_required",
            "grant the Dashboard process read permission",
        ),
        (
            "cursor_composer_timestamp_invalid",
            "source_adapter_incompatible",
            "Refresh alone cannot repair it",
        ),
    ],
)
def test_cursor_structural_errors_have_actionable_recovery(
    tmp_path: Path,
    error_code: str,
    issue_code: str,
    action_fragment: str,
) -> None:
    store_dir = tmp_path / error_code
    health_store = IngestionHealthStore(store_dir)
    scan_id = health_store.begin_scan(
        sources=("cursor",),
        scan_limit=20,
        importer_version="test",
        started_at=1_000.0,
    )
    health_store.complete_scan(
        scan_id,
        results=health_scan_results(
            {
                "cursor": {
                    "discovered": 1,
                    "parsed": 0,
                    "skipped": 1,
                    "error_count": 1,
                    "error_codes": [error_code],
                    "limit_unit": "root_groups",
                    "selected_root_groups": 0,
                    "returned_root_groups": 0,
                    "returned_rows": 0,
                }
            }
        ),
        completed_at=1_001.0,
    )

    snapshot = health_store.snapshot(now=1_002.0)
    issue = next(issue for issue in snapshot["issues"] if issue["source"] == "cursor")
    assert issue["code"] == issue_code
    assert action_fragment in issue["action"]

    health = TestClient(create_local_api_app(store_dir=store_dir)).get(
        "/ingestion/health"
    ).json()
    served = next(issue for issue in health["issues"] if issue["source"] == "cursor")
    assert served["code"] == issue_code
    assert action_fragment in served["action"]
