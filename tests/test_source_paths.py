from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

import agentacct.client_usage as client_usage_module
from agentacct.api import UsageDiscoveryConfig, create_local_api_app
from agentacct.cli import app
from agentacct.client_usage import (
    ClientSessionObservation,
    ClientUsageEvent,
    build_usage_import_write,
    build_usage_import_write_batches,
    classify_usage_write_conflict_candidates,
    describe_scanned_client_homes,
    discover_client_usage_with_diagnostics,
    discover_codex_usage,
    local_usage_row_identity,
    plan_local_usage_import,
    recognized_local_usage_row_identity,
    select_usage_import_candidates,
    source_namespace_adoption_candidates,
)
from agentacct.service import SentinelService
from agentacct.source_discovery import discover_usage_sources
from agentacct.source_paths import MAX_USAGE_SOURCE_HOMES, resolve_usage_source_paths
from agentacct.task_projection import build_task_projection
from agentacct.work_ledger import build_work_ledger
from refresh_flash import refresh_flash_qs, run_dashboard_refresh


def _make_codex_home(root: Path, *, session_id: str = "codex-env-session") -> Path:
    home = root / "codex-home"
    rollout = home / "sessions" / f"rollout-{session_id}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": session_id}}),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 100,
                                    "cached_input_tokens": 20,
                                    "output_tokens": 10,
                                }
                            },
                            "model": "gpt-5",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    connection = sqlite3.connect(home / "state_5.sqlite")
    try:
        connection.execute(
            """
            create table threads (
                id text primary key,
                rollout_path text not null,
                created_at integer not null,
                updated_at integer not null,
                cwd text not null,
                tokens_used integer not null,
                model text,
                cli_version text
            )
            """
        )
        connection.execute(
            "insert into threads values (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, str(rollout), 100, 200, "/work/project", 110, "gpt-5", "test"),
        )
        connection.commit()
    finally:
        connection.close()
    return home


def _make_claude_home(root: Path, *, session_id: str = "claude-env-session") -> Path:
    home = root / "claude-home"
    transcript = home / "projects" / "project" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": session_id,
                "cwd": "/work/project",
                "timestamp": "2026-07-01T00:00:00Z",
                "message": {
                    "model": "claude-sonnet-4",
                    "usage": {"input_tokens": 50, "output_tokens": 5},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return home


def _usage_candidate(
    tmp_path: Path,
    *,
    session_id: str,
    namespace: str | None,
    input_tokens: int = 10,
    parent_session_id: str | None = None,
) -> ClientUsageEvent:
    return ClientUsageEvent(
        client="codex",
        client_session_id=session_id,
        source_path=tmp_path / f"rollout-{session_id}.jsonl",
        title=None,
        cwd="/work/project",
        model="gpt-5",
        input_tokens=input_tokens,
        output_tokens=2,
        parent_client_session_id=parent_session_id,
        client_session_kind="child" if parent_session_id else "root",
        source_namespace_fingerprint=namespace,
    )


def test_source_path_plan_precedence_and_environment_deduplication(tmp_path: Path) -> None:
    user_home = tmp_path / "user"
    env_a = tmp_path / "env-a"
    env_b = tmp_path / "env-b"
    environment = {"CODEX_HOME": f" {env_a}, {env_a / '..' / 'env-a'}, {env_b} "}

    environment_plan = resolve_usage_source_paths(
        "codex",
        environ=environment,
        user_home=user_home,
    )
    assert environment_plan.precedence == "environment"
    assert environment_plan.homes == (env_a, env_b)

    explicit_plan = resolve_usage_source_paths(
        "codex",
        explicit_home=Path("~/explicit-codex"),
        environ=environment,
        user_home=user_home,
    )
    assert explicit_plan.precedence == "explicit"
    assert explicit_plan.homes == (user_home / "explicit-codex",)


def test_discovery_freezes_symlink_target_before_reading_and_hashing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target_a = tmp_path / "target-a"
    target_b = tmp_path / "target-b"
    target_a.mkdir()
    target_b.mkdir()
    source_link = tmp_path / "codex-link"
    source_link.symlink_to(target_a, target_is_directory=True)
    scanned_homes: list[Path] = []

    def fake_discovery(
        *,
        codex_home: Path,
        limit_sessions: int,
        _discovery_stats: dict,
        _session_observations: list[ClientSessionObservation],
    ) -> list[ClientUsageEvent]:
        del limit_sessions
        scanned_homes.append(codex_home)
        _discovery_stats.update({"discovered": 1, "parsed": 1, "returned_rows": 1})
        if len(scanned_homes) == 1:
            source_link.unlink()
            source_link.symlink_to(target_b, target_is_directory=True)
        candidate = _usage_candidate(
            tmp_path,
            session_id="symlink-session",
            namespace=None,
        )
        _session_observations.append(
            ClientSessionObservation(
                client="codex",
                client_session_id="symlink-session",
                source_path=candidate.source_path,
                updated_at=candidate.updated_at,
            )
        )
        return [candidate]

    monkeypatch.setattr(
        "agentacct.client_usage._discover_codex_usage_from_home",
        fake_discovery,
    )

    first = discover_codex_usage(codex_home=source_link, limit_sessions=10)
    second = discover_codex_usage(codex_home=source_link, limit_sessions=10)

    assert scanned_homes == [target_a.resolve(), target_b.resolve()]
    assert first[0].source_namespace_fingerprint != second[0].source_namespace_fingerprint


def test_claude_default_plan_keeps_xdg_then_legacy_home(tmp_path: Path) -> None:
    user_home = tmp_path / "user"
    xdg_home = tmp_path / "xdg"

    plan = resolve_usage_source_paths(
        "claude-code",
        environ={"XDG_CONFIG_HOME": str(xdg_home)},
        user_home=user_home,
    )

    assert plan.precedence == "default"
    assert plan.homes == (xdg_home / "claude", user_home / ".claude")


def test_environment_plan_has_a_visible_safety_bound(tmp_path: Path) -> None:
    candidates = [tmp_path / f"home-{index}" for index in range(MAX_USAGE_SOURCE_HOMES + 3)]

    plan = resolve_usage_source_paths(
        "codex",
        environ={"CODEX_HOME": ",".join(str(path) for path in candidates)},
        user_home=tmp_path / "user",
    )

    assert plan.homes == tuple(candidates[:MAX_USAGE_SOURCE_HOMES])
    assert plan.omitted_home_count == 3


def test_source_plan_keeps_healthy_home_when_secondary_path_is_symlink_loop(
    tmp_path: Path,
    monkeypatch,
) -> None:
    healthy = _make_codex_home(tmp_path / "healthy", session_id="healthy-loop-peer")
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    monkeypatch.setenv("CODEX_HOME", f"{healthy},{loop}")

    result = discover_client_usage_with_diagnostics(client="codex", limit_sessions=10)

    assert [event.client_session_id for event in result.events] == ["healthy-loop-peer"]


def test_source_plan_does_not_abort_on_unexpandable_named_user(
    tmp_path: Path,
) -> None:
    healthy = tmp_path / "healthy"
    invalid = "~chronicle_user_that_does_not_exist_8f2/root"

    plan = resolve_usage_source_paths(
        "codex",
        environ={"CODEX_HOME": f"{healthy},{invalid}"},
        user_home=tmp_path / "user",
    )

    assert plan.homes[0] == healthy
    assert plan.homes[1].is_absolute()
    assert plan.homes[1].as_posix().endswith(invalid)


def test_codex_environment_path_is_shared_by_status_import_and_watch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    codex_home = _make_codex_home(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    status = {
        source.client: source
        for source in discover_usage_sources(
            claude_home=tmp_path / "missing-claude",
            opencode_home=tmp_path / "missing-opencode",
            hermes_home=tmp_path / "missing-hermes",
            openclaw_home=tmp_path / "missing-openclaw",
        )
    }["codex"]
    import_scan = discover_client_usage_with_diagnostics(client="codex", limit_sessions=10)
    labels = describe_scanned_client_homes(client="codex")

    assert status.status == "found"
    assert str(codex_home / "state_5.sqlite") in status.paths
    assert [event.client_session_id for event in import_scan.events] == ["codex-env-session"]
    assert labels == [f"codex: {codex_home}"]

    result = CliRunner().invoke(
        app,
        [
            "usage",
            "watch",
            "--once",
            "--client",
            "codex",
            "--store-dir",
            str(tmp_path / "state"),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scanned_sessions"] == 1
    assert payload["imported_events"] == 1
    assert payload["scanned_homes"] == [f"codex: {codex_home}"]


def test_codex_rollout_without_state_database_is_not_claimed_as_importable(tmp_path: Path) -> None:
    codex_home = tmp_path / "rollout-only"
    rollout = codex_home / "sessions" / "rollout-orphan.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text('{"type":"event_msg"}\n', encoding="utf-8")

    status = {
        source.client: source
        for source in discover_usage_sources(
            codex_home=codex_home,
            claude_home=tmp_path / "missing-claude",
            opencode_home=tmp_path / "missing-opencode",
            hermes_home=tmp_path / "missing-hermes",
            openclaw_home=tmp_path / "missing-openclaw",
        )
    }["codex"]
    import_scan = discover_client_usage_with_diagnostics(
        client="codex",
        codex_home=codex_home,
        limit_sessions=10,
    )

    assert status.status == "missing"
    assert status.file_count == 1
    assert status.importer is None
    assert import_scan.events == []


def test_claude_environment_path_is_shared_by_status_and_import(
    tmp_path: Path,
    monkeypatch,
) -> None:
    claude_home = _make_claude_home(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))

    status = {
        source.client: source
        for source in discover_usage_sources(
            codex_home=tmp_path / "missing-codex",
            opencode_home=tmp_path / "missing-opencode",
            hermes_home=tmp_path / "missing-hermes",
            openclaw_home=tmp_path / "missing-openclaw",
        )
    }["claude-code"]
    import_scan = discover_client_usage_with_diagnostics(client="claude-code", limit_sessions=10)

    assert status.status == "found"
    assert status.paths == [str(claude_home / "projects")]
    assert [event.client_session_id for event in import_scan.events] == ["claude-env-session"]
    assert describe_scanned_client_homes(client="claude-code") == [
        f"claude-code: {claude_home}"
    ]

    result = CliRunner().invoke(
        app,
        [
            "usage",
            "import-local",
            "--client",
            "claude-code",
            "--store-dir",
            str(tmp_path / "state"),
            "--dry-run",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scanned_sessions"] == 1
    assert payload["scanned_homes"] == [f"claude-code: {claude_home}"]


def test_explicit_home_wins_over_client_environment_for_status_and_import(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment_home = _make_codex_home(tmp_path / "environment", session_id="environment-session")
    explicit_home = _make_codex_home(tmp_path / "explicit", session_id="explicit-session")
    monkeypatch.setenv("CODEX_HOME", str(environment_home))

    status = {
        source.client: source
        for source in discover_usage_sources(
            codex_home=explicit_home,
            claude_home=tmp_path / "missing-claude",
            opencode_home=tmp_path / "missing-opencode",
            hermes_home=tmp_path / "missing-hermes",
            openclaw_home=tmp_path / "missing-openclaw",
        )
    }["codex"]
    import_scan = discover_client_usage_with_diagnostics(
        client="codex",
        codex_home=explicit_home,
        limit_sessions=10,
    )

    assert str(explicit_home / "state_5.sqlite") in status.paths
    assert all(str(environment_home) not in path for path in status.paths)
    assert [event.client_session_id for event in import_scan.events] == ["explicit-session"]


def test_multi_home_session_id_collision_fails_closed_without_cross_namespace_merge(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first = _make_codex_home(tmp_path / "first", session_id="same-session")
    second = _make_codex_home(tmp_path / "second", session_id="same-session")
    monkeypatch.setenv("CODEX_HOME", f"{first},{second}")

    result = discover_client_usage_with_diagnostics(client="codex", limit_sessions=10)

    assert result.events == []
    diagnostic = result.diagnostics["codex"]
    assert diagnostic["error_count"] == 1
    assert diagnostic["error_codes"] == ["source_namespace_session_collision"]
    assert diagnostic["excluded_by_limit"] == 0
    assert diagnostic["excluded_by_source_namespace"] == 2


def test_multi_home_parent_reference_never_links_to_other_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    parent_home = _make_claude_home(tmp_path / "parent", session_id="shared-parent")
    child_home = tmp_path / "child" / "claude-home"
    child = child_home / "projects" / "project" / "child-transcript.jsonl"
    child.parent.mkdir(parents=True)
    child.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "shared-parent",
                "timestamp": "2026-07-02T00:00:00Z",
                "message": {
                    "model": "claude-sonnet-4",
                    "usage": {"input_tokens": 20, "output_tokens": 2},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", f"{parent_home},{child_home}")

    result = discover_client_usage_with_diagnostics(client="claude-code", limit_sessions=10)

    assert [event.client_session_id for event in result.events] == ["shared-parent"]
    diagnostic = result.diagnostics["claude-code"]
    assert diagnostic["error_count"] == 1
    assert diagnostic["error_codes"] == ["source_namespace_parent_mismatch"]


def test_later_scan_cannot_refresh_same_session_id_from_a_different_home(tmp_path: Path) -> None:
    first = _make_codex_home(tmp_path / "first", session_id="same-session")
    second = _make_codex_home(tmp_path / "second", session_id="same-session")
    store = tmp_path / "state"
    runner = CliRunner()

    initial = runner.invoke(
        app,
        [
            "usage",
            "import-local",
            "--client",
            "codex",
            "--codex-home",
            str(first),
            "--store-dir",
            str(store),
            "--json",
        ],
    )
    assert initial.exit_code == 0, initial.output
    assert json.loads(initial.output)["imported_events"] == 1

    conflicting_refresh = runner.invoke(
        app,
        [
            "usage",
            "import-local",
            "--client",
            "codex",
            "--codex-home",
            str(second),
            "--store-dir",
            str(store),
            "--refresh",
            "--json",
        ],
    )
    assert conflicting_refresh.exit_code == 0, conflicting_refresh.output
    payload = json.loads(conflicting_refresh.output)
    assert payload["source_namespace_conflicts"] == 1
    assert payload["imported_events"] == 0
    assert payload["refreshed_events"] == 0
    assert payload["ingestion_health"]["state"] == "degraded"
    assert payload["ingestion_health"]["source_namespace_conflicts"] == 1


def test_corrupt_secondary_codex_home_does_not_discard_healthy_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    healthy = _make_codex_home(tmp_path / "healthy", session_id="healthy-session")
    corrupt = tmp_path / "corrupt" / "codex-home"
    corrupt.mkdir(parents=True)
    (corrupt / "state_5.sqlite").write_text("not sqlite", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", f"{healthy},{corrupt}")

    result = discover_client_usage_with_diagnostics(client="codex", limit_sessions=10)

    assert [event.client_session_id for event in result.events] == ["healthy-session"]
    diagnostic = result.diagnostics["codex"]
    assert diagnostic["error_count"] == 1
    assert diagnostic["error_codes"] == ["sqlite_thread_scan_failed"]
    assert diagnostic["returned_rows"] == 1
    assert diagnostic["excluded_by_limit"] == 0
    assert diagnostic["excluded_by_source_namespace"] == 0


def test_raw_dashboard_surfaces_corrupt_codex_database_as_read_error(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "corrupt-codex"
    codex_home.mkdir()
    (codex_home / "state_5.sqlite").write_text(
        "not a sqlite database",
        encoding="utf-8",
    )
    client = TestClient(
        create_local_api_app(
            store_dir=tmp_path / "state",
            usage_discovery=UsageDiscoveryConfig(
                enabled=True,
                codex_home=codex_home,
                claude_home=tmp_path / "missing-claude",
                opencode_home=tmp_path / "missing-opencode",
                hermes_home=tmp_path / "missing-hermes",
                openclaw_home=tmp_path / "missing-openclaw",
            ),
        )
    )

    response = client.get("/raw")

    assert response.status_code == 200
    assert "Read error" in response.text
    assert "Codex state database(s) could not be read" in response.text


def test_corrupt_secondary_claude_home_does_not_discard_healthy_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    healthy = _make_claude_home(tmp_path / "healthy", session_id="healthy-session")
    corrupt = tmp_path / "corrupt" / "claude-home"
    (corrupt / "projects").mkdir(parents=True)
    original_discovery = client_usage_module._discover_claude_transcript_paths

    def guarded_discovery(projects_root: Path, **kwargs):
        if projects_root == corrupt / "projects":
            raise OSError("simulated corrupt secondary home")
        return original_discovery(projects_root, **kwargs)

    monkeypatch.setattr(
        client_usage_module,
        "_discover_claude_transcript_paths",
        guarded_discovery,
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", f"{healthy},{corrupt}")

    result = discover_client_usage_with_diagnostics(client="claude-code", limit_sessions=10)

    assert [event.client_session_id for event in result.events] == ["healthy-session"]
    diagnostic = result.diagnostics["claude-code"]
    assert diagnostic["error_count"] == 1
    assert diagnostic["error_codes"] == ["claude_transcript_discovery_failed"]
    assert diagnostic["returned_rows"] == 1


def test_planner_refuses_child_whose_stored_parent_belongs_to_another_home(tmp_path: Path) -> None:
    namespace_a = "sha256:" + "a" * 64
    namespace_b = "sha256:" + "b" * 64
    service = SentinelService(tmp_path / "state")
    parent = _usage_candidate(tmp_path, session_id="parent", namespace=namespace_a)
    service.record_event(parent.to_sentinel_event(), trusted_usage_import=True)
    child = _usage_candidate(
        tmp_path,
        session_id="child",
        namespace=namespace_b,
        parent_session_id="parent",
    )

    plan = plan_local_usage_import([child], service.list_all_events())

    assert plan.namespace_conflict_candidates == [child]
    assert plan.new_candidates == []


def test_legacy_namespace_is_adopted_only_with_a_real_refresh(tmp_path: Path) -> None:
    namespace = "sha256:" + "a" * 64
    service = SentinelService(tmp_path / "state")
    legacy = _usage_candidate(tmp_path, session_id="legacy", namespace=None)
    service.record_event(legacy.to_sentinel_event(), trusted_usage_import=True)
    scoped_unchanged = replace(legacy, source_namespace_fingerprint=namespace)

    unchanged_plan = plan_local_usage_import([scoped_unchanged], service.list_all_events())
    assert unchanged_plan.unchanged_candidates == [scoped_unchanged]
    assert unchanged_plan.refresh_candidates == []

    changed = replace(scoped_unchanged, input_tokens=11)
    refresh_plan = plan_local_usage_import([changed], service.list_all_events())
    assert refresh_plan.refresh_candidates == [changed]
    assert source_namespace_adoption_candidates([changed], refresh_plan) == [changed]
    events, predicate, guard = build_usage_import_write([changed], refresh_plan)
    assert events[0]["metadata"]["source_namespace_adoption"] == "legacy_unscoped_on_refresh"
    recorded = service.replace_events(
        predicate,
        events,
        trusted_usage_import=True,
        dedup_key=lambda event: (
            event.get("metadata", {}).get("client"),
            event.get("metadata", {}).get("client_session_id"),
            event.get("metadata", {}).get("usage_row_lane") or "",
        ),
        replace_guard=guard,
    )
    assert len(recorded) == 1
    assert recorded[0]["metadata"]["source_namespace_fingerprint"] == namespace


def test_stale_cross_namespace_refresh_plan_cannot_overwrite_first_writer(tmp_path: Path) -> None:
    namespace_a = "sha256:" + "a" * 64
    namespace_b = "sha256:" + "b" * 64
    service = SentinelService(tmp_path / "state")
    initial = _usage_candidate(tmp_path, session_id="race", namespace=None)
    service.record_event(initial.to_sentinel_event(), trusted_usage_import=True)
    snapshot = service.list_all_events()
    candidate_a = replace(initial, input_tokens=20, source_namespace_fingerprint=namespace_a)
    candidate_b = replace(initial, input_tokens=30, source_namespace_fingerprint=namespace_b)
    plan_a = plan_local_usage_import([candidate_a], snapshot)
    plan_b = plan_local_usage_import([candidate_b], snapshot)
    events_a, predicate_a, guard_a = build_usage_import_write([candidate_a], plan_a)
    events_b, predicate_b, guard_b = build_usage_import_write([candidate_b], plan_b)

    first = service.replace_events(
        predicate_a,
        events_a,
        trusted_usage_import=True,
        dedup_key=lambda event: event.get("metadata", {}).get("client_session_id"),
        replace_guard=guard_a,
    )
    second = service.replace_events(
        predicate_b,
        events_b,
        trusted_usage_import=True,
        dedup_key=lambda event: event.get("metadata", {}).get("client_session_id"),
        replace_guard=guard_b,
    )

    assert len(first) == 1
    assert second == []
    namespace_conflicts, concurrent_conflicts = classify_usage_write_conflict_candidates(
        [candidate_b],
        plan_b,
        second,
        service.list_all_events(),
    )
    assert namespace_conflicts == [candidate_b]
    assert concurrent_conflicts == []
    stored = service.list_all_events()[0]
    assert stored["estimated_input_tokens"] == 20
    assert stored["metadata"]["source_namespace_fingerprint"] == namespace_a


def test_stale_same_namespace_refresh_plan_cannot_revert_newer_event_id(tmp_path: Path) -> None:
    namespace = "sha256:" + "a" * 64
    service = SentinelService(tmp_path / "state")
    initial = _usage_candidate(tmp_path, session_id="same-source-race", namespace=namespace)
    service.record_event(initial.to_sentinel_event(), trusted_usage_import=True)
    snapshot = service.list_all_events()
    candidate_first = replace(initial, input_tokens=20)
    candidate_stale = replace(initial, input_tokens=30)
    first_plan = plan_local_usage_import([candidate_first], snapshot)
    stale_plan = plan_local_usage_import([candidate_stale], snapshot)
    first_events, first_predicate, first_guard = build_usage_import_write(
        [candidate_first], first_plan
    )
    stale_events, stale_predicate, stale_guard = build_usage_import_write(
        [candidate_stale], stale_plan
    )

    service.replace_events(
        first_predicate,
        first_events,
        trusted_usage_import=True,
        dedup_key=lambda event: event.get("metadata", {}).get("client_session_id"),
        replace_guard=first_guard,
    )
    stale_recorded = service.replace_events(
        stale_predicate,
        stale_events,
        trusted_usage_import=True,
        dedup_key=lambda event: event.get("metadata", {}).get("client_session_id"),
        replace_guard=stale_guard,
    )

    assert stale_recorded == []
    namespace_conflicts, concurrent_conflicts = classify_usage_write_conflict_candidates(
        [candidate_stale],
        stale_plan,
        stale_recorded,
        service.list_all_events(),
    )
    assert namespace_conflicts == []
    assert concurrent_conflicts == [candidate_stale]
    assert service.list_all_events()[0]["estimated_input_tokens"] == 20


def test_concurrent_new_import_from_other_home_is_classified_as_namespace_conflict(
    tmp_path: Path,
) -> None:
    namespace_a = "sha256:" + "a" * 64
    namespace_b = "sha256:" + "b" * 64
    service = SentinelService(tmp_path / "state")
    candidate_a = _usage_candidate(tmp_path, session_id="new-race", namespace=namespace_a)
    candidate_b = _usage_candidate(tmp_path, session_id="new-race", namespace=namespace_b)
    plan_a = plan_local_usage_import([candidate_a], [])
    plan_b = plan_local_usage_import([candidate_b], [])
    events_a, predicate_a, guard_a = build_usage_import_write([candidate_a], plan_a)
    events_b, predicate_b, guard_b = build_usage_import_write([candidate_b], plan_b)

    service.replace_events(
        predicate_a,
        events_a,
        trusted_usage_import=True,
        dedup_key=lambda event: event.get("metadata", {}).get("client_session_id"),
        replace_guard=guard_a,
    )
    second = service.replace_events(
        predicate_b,
        events_b,
        trusted_usage_import=True,
        dedup_key=lambda event: event.get("metadata", {}).get("client_session_id"),
        replace_guard=guard_b,
    )
    namespace_conflicts, concurrent_conflicts = classify_usage_write_conflict_candidates(
        [candidate_b],
        plan_b,
        second,
        service.list_all_events(),
    )

    assert second == []
    assert namespace_conflicts == [candidate_b]
    assert concurrent_conflicts == []


def test_raced_session_does_not_abort_unrelated_import_cohort(tmp_path: Path) -> None:
    namespace_a = "sha256:" + "a" * 64
    namespace_b = "sha256:" + "b" * 64
    service = SentinelService(tmp_path / "state")
    raced = _usage_candidate(tmp_path, session_id="raced", namespace=namespace_a)
    healthy = _usage_candidate(tmp_path, session_id="healthy", namespace=namespace_a)
    plan = plan_local_usage_import([raced, healthy], [])
    batches = build_usage_import_write_batches([raced, healthy], plan)
    assert len(batches) == 1
    assert batches[0][2] is None

    service.record_event(
        replace(raced, source_namespace_fingerprint=namespace_b).to_sentinel_event(),
        trusted_usage_import=True,
    )
    recorded: list[dict] = []
    for events, predicate, guard, dedup_key in batches:
        recorded.extend(
            service.replace_events(
                predicate,
                events,
                trusted_usage_import=True,
                dedup_key=dedup_key,
                replace_guard=guard,
            )
        )

    assert [event["metadata"]["client_session_id"] for event in recorded] == ["healthy"]
    assert {
        event["metadata"]["client_session_id"] for event in service.list_all_events()
    } == {"raced", "healthy"}


def test_many_pure_new_sessions_use_one_ledger_rewrite_batch(tmp_path: Path) -> None:
    namespace = "sha256:" + "a" * 64
    candidates = [
        _usage_candidate(tmp_path, session_id=f"session-{index}", namespace=namespace)
        for index in range(500)
    ]
    plan = plan_local_usage_import(candidates, [])

    batches = build_usage_import_write_batches(candidates, plan)

    assert len(batches) == 1
    events, _predicate, guard, _dedup_key = batches[0]
    assert len(events) == 500
    assert guard is None


def test_all_new_multi_lane_session_keeps_a_bulk_expected_empty_base_check(tmp_path: Path) -> None:
    namespace_a = "sha256:" + "a" * 64
    namespace_b = "sha256:" + "b" * 64
    service = SentinelService(tmp_path / "state")

    def lane(model: str, namespace: str) -> ClientUsageEvent:
        return ClientUsageEvent(
            client="claude-code",
            client_session_id="new-multi-lane",
            source_path=tmp_path / "new-multi-lane.jsonl",
            title=None,
            cwd="/work/project",
            model=model,
            input_tokens=10,
            output_tokens=2,
            usage_row_lane=f"model:{model}",
            source_namespace_fingerprint=namespace,
        )

    candidates = [lane("opus", namespace_a), lane("haiku", namespace_a)]
    plan = plan_local_usage_import(candidates, [])
    batches = build_usage_import_write_batches(candidates, plan)
    assert len(batches) == 1
    events, predicate, guard, dedup_key = batches[0]
    assert guard is None

    # A foreign lane that was not in the stale candidate set must still abort
    # the whole base; checking only the two planned identities is insufficient.
    service.record_event(
        lane("sonnet", namespace_b).to_sentinel_event(),
        trusted_usage_import=True,
    )
    recorded = service.replace_events(
        predicate,
        events,
        trusted_usage_import=True,
        dedup_key=dedup_key,
        replace_guard=guard,
    )

    assert recorded == []
    assert [event["model"] for event in service.list_all_events()] == ["sonnet"]


def test_new_lane_joining_existing_session_keeps_a_whole_base_guard(
    tmp_path: Path,
) -> None:
    namespace_a = "sha256:" + "a" * 64
    namespace_b = "sha256:" + "b" * 64
    service = SentinelService(tmp_path / "state")

    def lane(model: str, namespace: str) -> ClientUsageEvent:
        return ClientUsageEvent(
            client="claude-code",
            client_session_id="existing-multi-lane",
            source_path=tmp_path / "existing-multi-lane.jsonl",
            title=None,
            cwd="/work/project",
            model=model,
            input_tokens=10,
            output_tokens=2,
            usage_row_lane=f"model:{model}",
            source_namespace_fingerprint=namespace,
        )

    service.record_event(
        lane("opus", namespace_a).to_sentinel_event(),
        trusted_usage_import=True,
    )
    candidate = lane("sonnet", namespace_a)
    plan = plan_local_usage_import([candidate], service.list_all_events())
    assert plan.new_candidates == [candidate]
    batches = build_usage_import_write_batches([candidate], plan)
    assert len(batches) == 1
    events, predicate, guard, dedup_key = batches[0]
    assert guard is not None

    service.record_event(
        lane("haiku", namespace_b).to_sentinel_event(),
        trusted_usage_import=True,
    )
    recorded = service.replace_events(
        predicate,
        events,
        trusted_usage_import=True,
        dedup_key=dedup_key,
        replace_guard=guard,
    )

    assert recorded == []
    assert {event["model"] for event in service.list_all_events()} == {"opus", "haiku"}


def test_common_refresh_batch_skips_only_raced_row(tmp_path: Path) -> None:
    namespace = "sha256:" + "a" * 64
    service = SentinelService(tmp_path / "state")
    raced_initial = _usage_candidate(
        tmp_path,
        session_id="refresh-raced",
        namespace=namespace,
    )
    healthy_initial = _usage_candidate(
        tmp_path,
        session_id="refresh-healthy",
        namespace=namespace,
    )
    for candidate in (raced_initial, healthy_initial):
        service.record_event(candidate.to_sentinel_event(), trusted_usage_import=True)
    snapshot = service.list_all_events()
    raced_candidate = replace(raced_initial, input_tokens=20)
    healthy_candidate = replace(healthy_initial, input_tokens=20)
    plan = plan_local_usage_import([raced_candidate, healthy_candidate], snapshot)
    batches = build_usage_import_write_batches(
        [raced_candidate, healthy_candidate],
        plan,
    )
    assert len(batches) == 1
    events, predicate, guard, dedup_key = batches[0]
    assert guard is None

    concurrent_candidate = replace(raced_initial, input_tokens=99)
    concurrent_plan = plan_local_usage_import([concurrent_candidate], snapshot)
    concurrent_events, concurrent_predicate, concurrent_guard = build_usage_import_write(
        [concurrent_candidate],
        concurrent_plan,
    )
    service.replace_events(
        concurrent_predicate,
        concurrent_events,
        trusted_usage_import=True,
        dedup_key=recognized_local_usage_row_identity,
        replace_guard=concurrent_guard,
    )
    recorded = service.replace_events(
        predicate,
        events,
        trusted_usage_import=True,
        dedup_key=dedup_key,
        replace_guard=guard,
    )

    assert [event["metadata"]["client_session_id"] for event in recorded] == [
        "refresh-healthy"
    ]
    current = {
        event["metadata"]["client_session_id"]: event
        for event in service.list_all_events()
    }
    assert current["refresh-raced"]["estimated_input_tokens"] == 99
    assert current["refresh-healthy"]["estimated_input_tokens"] == 20


def test_concurrent_single_new_claude_lanes_cannot_mix_source_homes(
    tmp_path: Path,
) -> None:
    namespace_a = "sha256:" + "a" * 64
    namespace_b = "sha256:" + "b" * 64
    service = SentinelService(tmp_path / "state")

    def lane(model: str, namespace: str) -> ClientUsageEvent:
        return ClientUsageEvent(
            client="claude-code",
            client_session_id="single-lane-race",
            source_path=tmp_path / "single-lane-race.jsonl",
            title=None,
            cwd="/work/project",
            model=model,
            input_tokens=10,
            output_tokens=2,
            usage_row_lane=f"model:{model}",
            source_namespace_fingerprint=namespace,
        )

    candidate_a = lane("opus", namespace_a)
    candidate_b = lane("haiku", namespace_b)
    plan_a = plan_local_usage_import([candidate_a], [])
    plan_b = plan_local_usage_import([candidate_b], [])
    batch_a = build_usage_import_write_batches([candidate_a], plan_a)[0]
    batch_b = build_usage_import_write_batches([candidate_b], plan_b)[0]
    assert batch_a[2] is None
    assert batch_b[2] is None

    events_b, predicate_b, guard_b, dedup_b = batch_b
    service.replace_events(
        predicate_b,
        events_b,
        trusted_usage_import=True,
        dedup_key=dedup_b,
        replace_guard=guard_b,
    )
    events_a, predicate_a, guard_a, dedup_a = batch_a
    recorded_a = service.replace_events(
        predicate_a,
        events_a,
        trusted_usage_import=True,
        dedup_key=dedup_a,
        replace_guard=guard_a,
    )
    namespace_conflicts, concurrent_conflicts = classify_usage_write_conflict_candidates(
        [candidate_a],
        plan_a,
        recorded_a,
        service.list_all_events(),
    )

    assert recorded_a == []
    assert namespace_conflicts == [candidate_a]
    assert concurrent_conflicts == []
    current = service.list_all_events()
    assert len(current) == 1
    assert current[0]["model"] == "haiku"
    ledger = build_work_ledger(current, store_scope="custom")
    assert len(ledger["session_rollup"]["sessions"]) == 1
    assert ledger["session_rollup"]["summary"]["usage_namespace_collision_rows"] == 0


def test_work_event_with_usage_shaped_metadata_does_not_block_new_usage_row(
    tmp_path: Path,
) -> None:
    namespace = "sha256:" + "a" * 64
    service = SentinelService(tmp_path / "state")
    work_event = service.record_event(
        {
            "event_type": "task_started",
            "source": "mcp",
            "metadata": {
                "client": "codex",
                "client_session_id": "shared-session",
            },
        }
    )
    assert local_usage_row_identity(work_event) == ("codex", "shared-session", "")
    assert recognized_local_usage_row_identity(work_event) is None

    candidate = _usage_candidate(
        tmp_path,
        session_id="shared-session",
        namespace=namespace,
    )
    plan = plan_local_usage_import([candidate], service.list_all_events())
    assert plan.new_candidates == [candidate]
    events, predicate, guard = build_usage_import_write([candidate], plan)
    recorded = service.replace_events(
        predicate,
        events,
        trusted_usage_import=True,
        dedup_key=recognized_local_usage_row_identity,
        replace_guard=guard,
    )
    namespace_conflicts, concurrent_conflicts = classify_usage_write_conflict_candidates(
        [candidate],
        plan,
        recorded,
        service.list_all_events(),
    )

    assert len(recorded) == 1
    assert namespace_conflicts == []
    assert concurrent_conflicts == []
    assert {event["event_type"] for event in service.list_all_events()} == {
        "task_started",
        "model_usage",
    }


def test_stale_legacy_alias_migration_aborts_entire_cohort_when_one_lane_changes(
    tmp_path: Path,
) -> None:
    namespace = "sha256:" + "a" * 64
    service = SentinelService(tmp_path / "state")

    def lane(model: str, tokens: int, *, scoped: bool) -> ClientUsageEvent:
        return ClientUsageEvent(
            client="claude-code",
            client_session_id="multi-session",
            source_path=tmp_path / "multi-session.jsonl",
            title=None,
            cwd="/work/project",
            model=model,
            input_tokens=tokens,
            output_tokens=2,
            usage_row_lane=f"model:{model}",
            source_namespace_fingerprint=namespace if scoped else None,
        )

    for model in ("opus", "haiku"):
        legacy = lane(model, 10, scoped=False).to_sentinel_event()
        legacy["metadata"]["client_session_id"] = f"multi-session:model:{model}"
        legacy["metadata"].pop("usage_row_lane", None)
        service.record_event(legacy, trusted_usage_import=True)

    snapshot = service.list_all_events()
    candidates = [lane("opus", 11, scoped=True), lane("haiku", 10, scoped=True)]
    stale_plan = plan_local_usage_import(candidates, snapshot)
    assert len(stale_plan.migration_candidates) == 2
    stale_events, stale_predicate, stale_guard = build_usage_import_write(
        stale_plan.migration_candidates,
        stale_plan,
    )

    opus_identity = ("claude-code", "multi-session", "model:opus")
    old_opus = next(event for event in snapshot if local_usage_row_identity(event) == opus_identity)
    service.replace_events(
        lambda event: local_usage_row_identity(event) == opus_identity,
        [old_opus],
        trusted_usage_import=True,
        dedup_key=local_usage_row_identity,
    )
    stale_recorded = service.replace_events(
        stale_predicate,
        stale_events,
        trusted_usage_import=True,
        dedup_key=local_usage_row_identity,
        replace_guard=stale_guard,
    )

    assert stale_recorded == []
    current = service.list_all_events()
    assert len(current) == 2
    assert {
        event["metadata"]["client_session_id"] for event in current
    } == {"multi-session:model:opus", "multi-session:model:haiku"}


def test_real_lane_write_adopts_legacy_source_namespace_for_whole_session(
    tmp_path: Path,
) -> None:
    namespace = "sha256:" + "a" * 64
    service = SentinelService(tmp_path / "state")

    def lane(model: str, tokens: int, *, scoped: bool) -> ClientUsageEvent:
        return ClientUsageEvent(
            client="claude-code",
            client_session_id="coherent-session",
            source_path=tmp_path / "coherent-session.jsonl",
            title=None,
            cwd="/work/project",
            model=model,
            input_tokens=tokens,
            output_tokens=2,
            usage_row_lane=f"model:{model}",
            source_namespace_fingerprint=namespace if scoped else None,
        )

    for model in ("opus", "haiku"):
        service.record_event(
            lane(model, 10, scoped=False).to_sentinel_event(),
            trusted_usage_import=True,
        )
    candidates = [
        lane("opus", 11, scoped=True),
        lane("haiku", 10, scoped=True),
        lane("sonnet", 3, scoped=True),
    ]
    plan = plan_local_usage_import(candidates, service.list_all_events())

    assert {candidate.model for candidate in plan.refresh_candidates} == {"opus", "haiku"}
    assert {candidate.model for candidate in plan.new_candidates} == {"sonnet"}
    import_candidates = select_usage_import_candidates(plan, include_refresh=False)
    assert {candidate.model for candidate in import_candidates} == {"opus", "haiku", "sonnet"}
    batches = build_usage_import_write_batches(import_candidates, plan)
    assert len(batches) == 1
    events, predicate, guard, dedup_key = batches[0]
    assert guard is not None
    recorded = service.replace_events(
        predicate,
        events,
        trusted_usage_import=True,
        dedup_key=dedup_key,
        replace_guard=guard,
    )

    assert len(recorded) == 3
    current = service.list_all_events()
    assert {
        event["metadata"].get("source_namespace_fingerprint") for event in current
    } == {namespace}
    adopted_models = {
        event["model"]
        for event in current
        if event["metadata"].get("source_namespace_adoption")
    }
    assert adopted_models == {"opus", "haiku"}
    ledger = build_work_ledger(current, store_scope="custom")
    assert len(ledger["session_rollup"]["sessions"]) == 1
    assert ledger["session_rollup"]["sessions"][0]["usage"]["rows"] == 3


def test_concurrent_foreign_new_lane_aborts_whole_legacy_adoption(
    tmp_path: Path,
) -> None:
    namespace_a = "sha256:" + "a" * 64
    namespace_b = "sha256:" + "b" * 64
    service = SentinelService(tmp_path / "state")

    def lane(model: str, tokens: int, namespace: str | None) -> ClientUsageEvent:
        return ClientUsageEvent(
            client="claude-code",
            client_session_id="adoption-race",
            source_path=tmp_path / "adoption-race.jsonl",
            title=None,
            cwd="/work/project",
            model=model,
            input_tokens=tokens,
            output_tokens=2,
            usage_row_lane=f"model:{model}",
            source_namespace_fingerprint=namespace,
        )

    for model in ("opus", "haiku"):
        service.record_event(
            lane(model, 10, None).to_sentinel_event(),
            trusted_usage_import=True,
        )
    candidates = [
        lane("opus", 11, namespace_a),
        lane("haiku", 10, namespace_a),
        lane("sonnet", 3, namespace_a),
    ]
    stale_plan = plan_local_usage_import(candidates, service.list_all_events())
    import_candidates = [*stale_plan.new_candidates, *stale_plan.refresh_candidates]
    batches = build_usage_import_write_batches(import_candidates, stale_plan)
    assert len(batches) == 1
    events, predicate, guard, dedup_key = batches[0]
    assert guard is not None

    foreign_lane = lane("sonnet", 4, namespace_b)
    service.record_event(
        foreign_lane.to_sentinel_event(),
        trusted_usage_import=True,
    )
    recorded = service.replace_events(
        predicate,
        events,
        trusted_usage_import=True,
        dedup_key=dedup_key,
        replace_guard=guard,
    )
    namespace_conflicts, concurrent_conflicts = classify_usage_write_conflict_candidates(
        import_candidates,
        stale_plan,
        recorded,
        service.list_all_events(),
    )

    assert recorded == []
    assert next(candidate for candidate in candidates if candidate.model == "sonnet") in namespace_conflicts
    assert concurrent_conflicts == []
    current = service.list_all_events()
    current_by_model = {event["model"]: event for event in current}
    assert current_by_model["opus"]["estimated_input_tokens"] == 10
    assert current_by_model["haiku"]["estimated_input_tokens"] == 10
    assert "source_namespace_fingerprint" not in current_by_model["opus"]["metadata"]
    assert "source_namespace_fingerprint" not in current_by_model["haiku"]["metadata"]
    assert current_by_model["sonnet"]["metadata"]["source_namespace_fingerprint"] == namespace_b


def test_dashboard_protects_cross_home_refresh_and_surfaces_health_recovery_action(
    tmp_path: Path,
) -> None:
    first_home = _make_codex_home(tmp_path / "first", session_id="dashboard-session")
    second_home = _make_codex_home(tmp_path / "second", session_id="dashboard-session")
    store = tmp_path / "state"

    def config(home: Path) -> UsageDiscoveryConfig:
        return UsageDiscoveryConfig(
            enabled=True,
            codex_home=home,
            claude_home=tmp_path / "missing-claude",
            opencode_home=tmp_path / "missing-opencode",
            hermes_home=tmp_path / "missing-hermes",
            openclaw_home=tmp_path / "missing-openclaw",
        )

    first_client = TestClient(
        create_local_api_app(store_dir=store, usage_discovery=config(first_home))
    )
    initial = run_dashboard_refresh(first_client)
    assert initial.status_code == 303
    before = SentinelService(store).list_all_events()[0]

    second_client = TestClient(
        create_local_api_app(store_dir=store, usage_discovery=config(second_home))
    )
    protected = run_dashboard_refresh(second_client)

    assert protected.status_code == 303
    assert "source_namespace_conflicts=1" in refresh_flash_qs(protected)
    after = SentinelService(store).list_all_events()[0]
    assert after["event_id"] == before["event_id"]
    assert after["metadata"]["source_namespace_fingerprint"] == before["metadata"][
        "source_namespace_fingerprint"
    ]
    page = second_client.get(protected.headers["location"])
    assert "agentacct protected 1 usage row(s)" in page.text
    assert "confirm that agent's configured data home" in page.text
    health = second_client.get("/ingestion/health").json()
    codex = next(row for row in health["sources"] if row["source"] == "codex")
    assert health["state"] == "degraded"
    assert health["source_namespace_conflicts"] == 1
    assert codex["source_namespace_conflicts"] == 1
    assert health["issues"][0]["code"] == "source_namespace_conflict"


def test_persisted_parent_source_namespace_controls_task_lineage_end_to_end(tmp_path: Path) -> None:
    namespace_a = "sha256:" + "a" * 64
    namespace_b = "sha256:" + "b" * 64

    def projection_for(child_namespace: str) -> dict:
        service = SentinelService(tmp_path / child_namespace[-1])
        parent = _usage_candidate(tmp_path, session_id="root", namespace=namespace_a)
        child = _usage_candidate(
            tmp_path,
            session_id="child",
            namespace=child_namespace,
            parent_session_id="root",
        )
        service.record_event(parent.to_sentinel_event(), trusted_usage_import=True)
        service.record_event(child.to_sentinel_event(), trusted_usage_import=True)
        ledger = build_work_ledger(service.list_all_events(), store_scope="custom")
        return build_task_projection(
            ledger["session_rollup"]["sessions"],
            ledger["work_items"],
        )

    mismatched = projection_for(namespace_b)
    matched = projection_for(namespace_a)

    assert mismatched["summary"]["task_count"] == 2
    child_task = next(
        task for task in mismatched["tasks"] if task["task_id"] == "session:codex:child"
    )
    assert child_task["root_groups"][0]["lineage_state"] == (
        "orphan_parent_source_namespace_mismatch"
    )
    assert matched["summary"]["task_count"] == 1
    assert matched["tasks"][0]["session_count"] == 2


def test_dashboard_reports_legacy_source_namespace_adoption_on_real_refresh(tmp_path: Path) -> None:
    codex_home = _make_codex_home(tmp_path, session_id="adoption-session")
    store = tmp_path / "state"
    legacy = _usage_candidate(
        tmp_path,
        session_id="adoption-session",
        namespace=None,
        input_tokens=1,
    )
    SentinelService(store).record_event(legacy.to_sentinel_event(), trusted_usage_import=True)
    config = UsageDiscoveryConfig(
        enabled=True,
        codex_home=codex_home,
        claude_home=tmp_path / "missing-claude",
        opencode_home=tmp_path / "missing-opencode",
        hermes_home=tmp_path / "missing-hermes",
        openclaw_home=tmp_path / "missing-openclaw",
        cursor_home=tmp_path / "missing-cursor",
    )
    client = TestClient(create_local_api_app(store_dir=store, usage_discovery=config))

    response = run_dashboard_refresh(client)

    assert response.status_code == 303
    assert "source_namespace_adoptions=1" in refresh_flash_qs(response)
    stored = SentinelService(store).list_all_events()[0]
    assert stored["metadata"]["source_namespace_binding"] == "tofu_explicit_scan_v1"
    assert stored["metadata"]["source_namespace_fingerprint"].startswith("sha256:")


def test_dashboard_binds_unchanged_legacy_row_once_without_reissuing_identity(
    tmp_path: Path,
) -> None:
    codex_home = _make_codex_home(tmp_path, session_id="unchanged-legacy-session")
    discovery = discover_client_usage_with_diagnostics(
        client="codex",
        codex_home=codex_home,
        limit_sessions=10,
    )
    assert len(discovery.events) == 1
    legacy_event = discovery.events[0].to_sentinel_event()
    legacy_event["metadata"].pop("source_namespace_fingerprint", None)
    legacy_event["metadata"].pop("parent_source_namespace_fingerprint", None)
    # Keep the dashboard's intentional unknown→priced refresh out of this
    # contract: only additive namespace metadata is under test here.
    legacy_event["estimated_cost_usd"] = 0.001
    legacy_event["cost_confidence"] = "estimated_from_tokens"

    store = tmp_path / "state"
    saved = SentinelService(store).record_event(
        legacy_event,
        trusted_usage_import=True,
    )
    before_bytes = SentinelService(store).event_log.read_lines()
    config = UsageDiscoveryConfig(
        enabled=True,
        codex_home=codex_home,
        claude_home=tmp_path / "missing-claude",
        opencode_home=tmp_path / "missing-opencode",
        hermes_home=tmp_path / "missing-hermes",
        openclaw_home=tmp_path / "missing-openclaw",
    )
    client = TestClient(create_local_api_app(store_dir=store, usage_discovery=config))

    response = run_dashboard_refresh(client)

    assert response.status_code == 303
    assert "refreshed=0" in refresh_flash_qs(response)
    assert "source_namespace_adoptions=1" in refresh_flash_qs(response)
    after_bytes = SentinelService(store).event_log.read_lines()
    assert after_bytes != before_bytes
    after = SentinelService(store).list_all_events()[0]
    assert after["event_id"] == saved["event_id"]
    assert after["created_at"] == saved["created_at"]
    assert after["metadata"]["source_namespace_fingerprint"].startswith("sha256:")
    assert after["metadata"]["source_namespace_binding"] == "tofu_explicit_scan_v1"

    second = run_dashboard_refresh(client)

    assert second.status_code == 303
    assert "source_namespace_adoptions=0" in refresh_flash_qs(second)
    assert SentinelService(store).event_log.read_lines() == after_bytes
