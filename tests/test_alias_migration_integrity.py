from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agent_chronicle.api import UsageDiscoveryConfig, create_local_api_app
from agent_chronicle.cli import app
from agent_chronicle.client_usage import (
    ClientUsageEvent,
    build_usage_import_write_batches,
    plan_local_usage_import,
    select_usage_import_candidates,
)
from agent_chronicle.service import SentinelService
from agent_chronicle.usage_truth import split_shadowed_legacy_usage_events
from refresh_flash import refresh_flash_qs, run_dashboard_refresh


NS_A = "sha256:" + "a" * 64
NS_B = "sha256:" + "b" * 64
SESSION_ID = "alias-integrity-session"


def _candidate(
    tmp_path: Path,
    model: str | None,
    *,
    session_id: str = SESSION_ID,
    tokens: int = 1,
    namespace: str | None = NS_A,
    parent_session_id: str | None = None,
    lane: str | None = None,
) -> ClientUsageEvent:
    lane_name = lane if lane is not None else f"model:{model or 'unknown'}"
    return ClientUsageEvent(
        client="claude-code",
        client_session_id=session_id,
        source_path=tmp_path / f"{session_id}.jsonl",
        title=None,
        cwd="/work/project",
        model=model,
        input_tokens=tokens,
        output_tokens=1,
        usage_row_lane=lane_name,
        source_namespace_fingerprint=namespace,
        parent_client_session_id=parent_session_id,
    )


def _record_legacy_alias(
    service: SentinelService,
    tmp_path: Path,
    model: str,
    *,
    tokens: int = 100,
    namespace: str | None = None,
) -> dict:
    event = _candidate(
        tmp_path,
        model,
        tokens=tokens,
        namespace=namespace,
    ).to_sentinel_event()
    event["metadata"]["client_session_id"] = f"{SESSION_ID}:model:{model}"
    event["metadata"].pop("usage_row_lane", None)
    event["metadata"].pop("source_namespace_fingerprint", None)
    if namespace is not None:
        event["metadata"]["source_namespace_fingerprint"] = namespace
    return service.record_event(event, trusted_usage_import=True)


def _write_claude_home(tmp_path: Path, extra_line: str | dict) -> Path:
    claude_home = tmp_path / "claude-home"
    project = claude_home / "projects" / "-work-project"
    project.mkdir(parents=True)
    rows: list[str] = [
        json.dumps(
            {
                "type": "assistant",
                "sessionId": SESSION_ID,
                "cwd": "/work/project",
                "message": {
                    "role": "assistant",
                    "model": "opus",
                    "usage": {"input_tokens": 50, "output_tokens": 1},
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "sessionId": SESSION_ID,
                "cwd": "/work/project",
                "message": {
                    "role": "assistant",
                    "model": "haiku",
                    "usage": {"input_tokens": 100, "output_tokens": 1},
                },
            }
        ),
    ]
    rows.append(extra_line if isinstance(extra_line, str) else json.dumps(extra_line))
    (project / f"{SESSION_ID}.jsonl").write_text(
        "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    return claude_home


def _seed_alias_store(store: Path, tmp_path: Path) -> dict[str, tuple[str, float]]:
    service = SentinelService(store)
    seeded = [
        _record_legacy_alias(service, tmp_path, "opus"),
        _record_legacy_alias(service, tmp_path, "haiku"),
    ]
    return {
        event["model"]: (event["event_id"], event["created_at"])
        for event in seeded
    }


def _usage_rows(store: Path) -> list[dict]:
    return [
        event
        for event in SentinelService(store).list_all_events()
        if event.get("event_type") == "model_usage"
    ]


def _assert_legacy_alias_rows_preserved(
    store: Path,
    revisions: dict[str, tuple[str, float]],
) -> None:
    rows = _usage_rows(store)
    assert len(rows) == 2
    assert {
        row["metadata"]["client_session_id"] for row in rows
    } == {
        f"{SESSION_ID}:model:opus",
        f"{SESSION_ID}:model:haiku",
    }
    assert {row["model"]: row["estimated_input_tokens"] for row in rows} == {
        "opus": 100,
        "haiku": 100,
    }
    assert {
        row["model"]: (row["event_id"], row["created_at"])
        for row in rows
    } == revisions


def _api_config(tmp_path: Path, claude_home: Path) -> UsageDiscoveryConfig:
    return UsageDiscoveryConfig(
        enabled=True,
        codex_home=tmp_path / "missing-codex",
        claude_home=claude_home,
        opencode_home=tmp_path / "missing-opencode",
        hermes_home=tmp_path / "missing-hermes",
        openclaw_home=tmp_path / "missing-openclaw",
    )


def test_dashboard_preserves_alias_cohort_when_same_lane_has_malformed_json(
    tmp_path: Path,
) -> None:
    claude_home = _write_claude_home(
        tmp_path,
        (
            '{"type":"assistant","sessionId":"alias-integrity-session",'
            '"message":{"role":"assistant","model":"opus",'
            '"usage":{"input_tokens":50'
        ),
    )
    store = tmp_path / "state"
    revisions = _seed_alias_store(store, tmp_path)
    client = TestClient(
        create_local_api_app(
            store_dir=store,
            usage_discovery=_api_config(tmp_path, claude_home),
        )
    )

    response = run_dashboard_refresh(client)

    assert response.status_code == 303
    assert "imported=0" in refresh_flash_qs(response)
    assert "migrated=0" in refresh_flash_qs(response)
    assert "incomplete_alias_migrations=1" in refresh_flash_qs(response)
    _assert_legacy_alias_rows_preserved(store, revisions)

    page = client.get(response.headers["location"])
    assert "agentacct preserved 1 legacy session(s)" in page.text
    health = client.get("/ingestion/health").json()
    claude_health = next(
        row for row in health["sources"] if row["source"] == "claude-code"
    )
    assert health["incomplete_alias_migrations"] == 1
    assert claude_health["incomplete_alias_migrations"] == 1
    assert health["issues"][0]["code"] == "alias_migration_incomplete"


def test_cli_preserves_alias_cohort_on_semantic_usage_schema_drift(
    tmp_path: Path,
) -> None:
    claude_home = _write_claude_home(
        tmp_path,
        {
            "type": "assistant",
            "sessionId": SESSION_ID,
            "message": {
                "role": "assistant",
                "model": "opus",
                "usage": {"input": 50, "output": 1},
            },
        },
    )
    store = tmp_path / "state"
    revisions = _seed_alias_store(store, tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "usage",
            "import-local",
            "--client",
            "claude-code",
            "--store-dir",
            str(store),
            "--claude-home",
            str(claude_home),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["imported_events"] == 0
    assert payload["migrated_events"] == 0
    assert payload["incomplete_alias_migrations"] == 1
    assert "claude_transcript_usage_schema_drift" in payload["source_diagnostics"][
        "claude-code"
    ]["error_codes"]
    _assert_legacy_alias_rows_preserved(store, revisions)


def test_namespace_rejected_lane_cannot_complete_alias_migration(
    tmp_path: Path,
) -> None:
    service = SentinelService(tmp_path / "state")
    _record_legacy_alias(service, tmp_path, "opus", namespace=NS_A)
    _record_legacy_alias(service, tmp_path, "haiku", namespace=NS_A)
    parent = _candidate(
        tmp_path,
        "parent",
        session_id="foreign-parent",
        namespace=NS_B,
    )
    service.record_event(parent.to_sentinel_event(), trusted_usage_import=True)
    opus = _candidate(
        tmp_path,
        "opus",
        namespace=NS_A,
        parent_session_id="foreign-parent",
    )
    haiku = _candidate(tmp_path, "haiku", namespace=NS_A)
    before = service.list_all_events()

    plan = plan_local_usage_import([opus, haiku], before)

    assert plan.namespace_conflict_candidates == [opus]
    assert plan.migration_candidates == []
    assert plan.incomplete_migration_bases == frozenset(
        {("claude-code", SESSION_ID)}
    )
    selected = select_usage_import_candidates(plan, include_refresh=True)
    assert selected == []
    assert build_usage_import_write_batches(selected, plan) == []
    assert service.list_all_events() == before


def test_explicit_stable_unknown_lane_is_never_disposable_alias_shadow(
    tmp_path: Path,
) -> None:
    service = SentinelService(tmp_path / "state")
    stable_unknown = service.record_event(
        _candidate(
            tmp_path,
            None,
            tokens=100,
            namespace=NS_A,
            lane="model:unknown",
        ).to_sentinel_event(),
        trusted_usage_import=True,
    )
    legacy_opus = _record_legacy_alias(
        service,
        tmp_path,
        "opus",
        tokens=50,
        namespace=NS_A,
    )
    before = service.list_all_events()

    kept, shadowed = split_shadowed_legacy_usage_events(before)
    assert stable_unknown in kept
    assert legacy_opus in kept
    assert shadowed == []

    plan = plan_local_usage_import(
        [_candidate(tmp_path, "opus", tokens=50, namespace=NS_A)],
        before,
    )

    assert plan.migration_candidates == []
    assert plan.incomplete_migration_bases == frozenset(
        {("claude-code", SESSION_ID)}
    )
    selected = select_usage_import_candidates(plan, include_refresh=True)
    assert selected == []
    assert build_usage_import_write_batches(selected, plan) == []
    assert service.list_all_events() == before


def test_plain_unknown_shadow_cannot_hide_real_suffixed_unknown_lane(
    tmp_path: Path,
) -> None:
    service = SentinelService(tmp_path / "state")
    _record_legacy_alias(service, tmp_path, "unknown", tokens=100, namespace=NS_A)
    _record_legacy_alias(service, tmp_path, "opus", tokens=50, namespace=NS_A)
    plain_shadow = _candidate(
        tmp_path,
        None,
        tokens=1,
        namespace=NS_A,
        lane="model:unknown",
    ).to_sentinel_event()
    plain_shadow["metadata"].pop("usage_row_lane", None)
    service.record_event(plain_shadow, trusted_usage_import=True)
    before = service.list_all_events()

    plan = plan_local_usage_import(
        [_candidate(tmp_path, "opus", tokens=50, namespace=NS_A)],
        before,
    )

    assert plan.migration_candidates == []
    assert plan.incomplete_migration_bases == frozenset(
        {("claude-code", SESSION_ID)}
    )
    assert select_usage_import_candidates(plan, include_refresh=True) == []
    assert service.list_all_events() == before
