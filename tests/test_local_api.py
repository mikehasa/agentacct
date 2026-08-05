import json
import os
import socket
import sqlite3

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentacct.api import UsageDiscoveryConfig, create_local_api_app
from agentacct.cli import _local_usage_import_payload, app
from agentacct.cost import CostLedger, UsageEstimate
from agentacct.event_log import RAW_EVENT_LOG_FILENAME, RawEventLog
from agentacct.mcp import SentinelMCPServer
from agentacct.outcome import apply_judge_result, write_outcome
from agentacct.pricing_catalog import default_pricing_catalog_snapshot_path
from agentacct.reports import build_run_report_payload
from agentacct.runner import RunOptions, start_guarded_run
from agentacct.service import SentinelService
from agentacct.storage import RunStore


def _make_home_usage_sources(home):
    codex_home = home / ".codex"
    codex_sessions = codex_home / "sessions"
    codex_sessions.mkdir(parents=True)
    rollout_path = codex_sessions / "rollout-test.jsonl"
    rollout_path.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 300,
                            "cached_input_tokens": 100,
                            "output_tokens": 20,
                            "reasoning_output_tokens": 5,
                        }
                    },
                    "model": "gpt-5.5",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    con = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        con.execute(
            """
            create table threads (
                id text primary key,
                rollout_path text not null,
                created_at integer not null,
                updated_at integer not null,
                cwd text not null,
                title text not null,
                tokens_used integer not null,
                model text,
                cli_version text
            )
            """
        )
        con.execute(
            "insert into threads values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("codex-session", str(rollout_path), 100, 200, "/tmp/project", "Codex fixture", 320, "gpt-5.5", "0.test"),
        )
        con.commit()
    finally:
        con.close()

    hermes_home = home / ".hermes"
    hermes_home.mkdir()
    con = sqlite3.connect(hermes_home / "state.db")
    try:
        con.execute(
            """
            create table sessions (
                id text primary key,
                source text,
                model text,
                started_at real,
                message_count integer,
                input_tokens integer,
                output_tokens integer,
                cache_read_tokens integer,
                cache_write_tokens integer,
                reasoning_tokens integer,
                billing_provider text,
                estimated_cost_usd real,
                actual_cost_usd real
            )
            """
        )
        con.execute(
            "insert into sessions values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("hermes-session", "cli", "gpt-5-mini", 1_750_000_000.0, 2, 100, 20, 5, 4, 1, "openai", 0.11, 0.42),
        )
        con.commit()
    finally:
        con.close()

    openclaw_home = home / ".openclaw" / "agents" / "default" / "sessions"
    openclaw_home.mkdir(parents=True)
    (openclaw_home / "openclaw-session.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "model_change", "provider": "openai", "modelId": "gpt-5.2"}),
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "usage": {"input": 7, "output": 3, "cacheRead": 2, "cost": {"total": 0.01}},
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    return codex_home, hermes_home


def _usage_discovery_for_home(home):
    return UsageDiscoveryConfig.from_home(home)


def _import_local_usage(store_root, home, *, client="all", refresh=True, estimate_costs=True):
    """Run the kept local-usage import lane (the CLI's data function) against a
    fixture home, mirroring the retired dashboard refresh's always-price and
    replace-changed semantics. Every client home is pinned inside the fixture
    home so a developer's real env overrides can never leak into the scan."""

    return _local_usage_import_payload(
        store_dir=store_root,
        client=client,
        codex_home=home / ".codex",
        claude_home=home / ".claude",
        opencode_home=home / ".local" / "share" / "opencode",
        hermes_home=home / ".hermes",
        openclaw_home=home / ".openclaw",
        cursor_home=home / "cursor-home",
        refresh=refresh,
        estimate_costs=estimate_costs,
    )


def _add_codex_internal_review_source(home):
    codex_home = home / ".codex"
    review_rollout = codex_home / "sessions" / "rollout-review.jsonl"
    review_rollout.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 80,
                            "cached_input_tokens": 40,
                            "output_tokens": 10,
                            "reasoning_output_tokens": 2,
                        }
                    },
                    "model": "codex-auto-review",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    con = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        con.execute(
            "insert into threads values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("codex-review-session", str(review_rollout), 101, 201, "/tmp/project", "Internal Codex review", 90, "codex-auto-review", "0.test"),
        )
        con.commit()
    finally:
        con.close()


def _make_run(tmp_path):
    dummy = tmp_path / "api_dummy.py"
    dummy.write_text("print('api ok')\n", encoding="utf-8")
    store_root = tmp_path / "state"
    result = start_guarded_run(["python", str(dummy)], RunOptions(store_dir=store_root, poll_interval=0.05))
    return store_root, result


def _record_trusted_usage(store_root, *, run_id=None, session="codex-session", input_tokens=100, output_tokens=25, cached_input_tokens=50, started_at=None, updated_at=None):
    return SentinelService(store_root).record_event(
        {
            "source": "codex-local-session-import",
            "event_type": "model_usage",
            "run_id": run_id,
            "provider": "codex",
            "model": "gpt-5.5",
            "estimated_input_tokens": input_tokens,
            "estimated_output_tokens": output_tokens,
            "estimated_cost_usd": 0.01,
            "usage_confidence": "client_reported",
            "cost_confidence": "estimated_from_tokens",
            "metadata": {
                "usage_source": "local_client_session_store",
                "client": "codex",
                "client_session_id": session,
                "cached_input_tokens": cached_input_tokens,
                "started_at": started_at,
                "updated_at": updated_at,
            },
        },
        trusted_usage_import=True,
    )


def _stored_usage_rows(store_root):
    rows = []
    for event in SentinelService(store_root).list_all_events():
        if event.get("event_type") == "model_usage":
            rows.append(event)
    return rows


def test_local_api_lists_runs_and_returns_report(tmp_path):
    store_root, result = _make_run(tmp_path)
    client = TestClient(create_local_api_app(store_dir=store_root))

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True

    runs = client.get("/runs")
    assert runs.status_code == 200
    assert runs.json()["runs"][0]["run_id"] == result.run_id

    run = client.get(f"/runs/{result.run_id}")
    assert run.status_code == 200
    assert run.json()["run_id"] == result.run_id

    report = client.get(f"/runs/{result.run_id}/report")
    assert report.status_code == 200
    assert report.json()["schema_version"] == "agent-sentinel.report.v1"
    assert report.json()["run"]["run_id"] == result.run_id


def test_local_api_records_machine_check_and_prepares_judge(tmp_path):
    store_root, result = _make_run(tmp_path)
    client = TestClient(create_local_api_app(store_dir=store_root))

    check = client.post(
        f"/runs/{result.run_id}/outcome/machine-check",
        json={
            "name": "pytest",
            "before_exit_code": 1,
            "after_exit_code": 0,
            "before_summary": "failed before",
            "after_summary": "passed after",
        },
    )
    assert check.status_code == 200
    assert check.json()["outcome"]["machine_checks"]["resolved_failures"] == 1

    package = client.post(
        f"/runs/{result.run_id}/judge/prepare",
        json={"task_goal": "Fix tests", "rubric": "Score test improvement.", "write_package": True},
    )
    assert package.status_code == 200
    assert package.json()["schema_version"] == "agent-sentinel.judge-package.v1"
    assert package.json()["task_goal"] == "Fix tests"
    assert (store_root / "runs" / result.run_id / "judge_package.json").exists()


def test_local_api_computes_value_without_paid_judge_call(tmp_path):
    store_root, result = _make_run(tmp_path)
    store = RunStore(store_root)
    report = build_run_report_payload(store, result.run_id)
    outcome = report["outcome"]
    outcome["machine_checks"] = {
        "configured": True,
        "before": "failed",
        "after": "passed",
        "resolved_failures": 1,
        "introduced_failures": 0,
    }
    outcome = apply_judge_result(
        outcome,
        {"deliverable_score": 85, "confidence": "medium", "reason": "useful", "risks": []},
        source="openrouter",
        model="openai/gpt-4o-mini",
        cost_event_id="cost_test",
    )
    write_outcome(store, result.run_id, outcome)

    client = TestClient(create_local_api_app(store_dir=store_root))
    value = client.post(f"/runs/{result.run_id}/value/compute", json={"budget_usd": 0.01})

    assert value.status_code == 200
    assert value.json()["value"]["score"] == 90
    assert value.json()["value"]["components"]["cost_efficiency_score"] == 100
    report_after = client.get(f"/runs/{result.run_id}/report").json()
    assert report_after["outcome"]["value"]["score"] == 90


def test_local_api_returns_404_for_unknown_run(tmp_path):
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))

    response = client.get("/runs/not-a-run/report")

    assert response.status_code == 404


def test_local_api_validates_limit_and_budget_inputs(tmp_path):
    store_root, result = _make_run(tmp_path)
    client = TestClient(create_local_api_app(store_dir=store_root))

    assert client.get("/runs?limit=0").status_code == 422
    assert client.get("/runs?limit=101").status_code == 422
    assert client.post(f"/runs/{result.run_id}/value/compute", json={"budget_usd": -0.01}).status_code == 422


def test_local_api_returns_422_for_invalid_run_id(tmp_path):
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))

    response = client.get("/runs/run%20with%20spaces/report")

    assert response.status_code == 422


def test_local_api_records_and_lists_redacted_events(tmp_path):
    store_root, result = _make_run(tmp_path)
    client = TestClient(create_local_api_app(store_dir=store_root))

    response = client.post(
        "/events",
        json={
            "source": "hermes",
            "event_type": "model_usage",
            "run_id": result.run_id,
            "provider": "openai",
            "model": "gpt-4o-mini",
            "estimated_input_tokens": 12,
            "estimated_output_tokens": 3,
            "estimated_cost_usd": 0.000004,
            "metadata": {
                "task": "dashboard smoke",
                "api_key": "fake-api-key-for-redaction-test",
                "Authorization": "Bearer secret",
                "nested": {
                    "token": "secret-token",
                    "aws_secret_access_key": "aws-secret-canary",
                    "clientSecret": "client-secret-canary",
                    "safe": "ok",
                },
            },
        },
    )

    assert response.status_code == 200
    event = response.json()["event"]
    assert event["source"] == "hermes"
    assert event["event_type"] == "model_usage"
    assert event["run_id"] == result.run_id
    assert event["estimated_input_tokens"] == 12
    assert event["estimated_output_tokens"] == 3
    assert event["metadata"]["task"] == "dashboard smoke"
    assert event["metadata"]["api_key"] == "[REDACTED]"
    assert event["metadata"]["Authorization"] == "[REDACTED]"
    assert event["metadata"]["nested"]["token"] == "[REDACTED]"
    assert event["metadata"]["nested"]["aws_secret_access_key"] == "[REDACTED]"
    assert event["metadata"]["nested"]["clientSecret"] == "[REDACTED]"
    assert event["metadata"]["nested"]["safe"] == "ok"

    listed = client.get("/events")
    assert listed.status_code == 200
    assert listed.json()["events"][0]["event_id"] == event["event_id"]
    ledger_text = "\n".join(RawEventLog(store_root / RAW_EVENT_LOG_FILENAME).read_lines())
    assert "fake-api-key-for-redaction-test" not in ledger_text
    assert "aws-secret-canary" not in ledger_text
    assert "client-secret-canary" not in ledger_text


def test_service_redacts_secret_keys_nested_in_tuples(tmp_path):
    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "tuple_metadata",
            "metadata": {
                "items": (
                    {"apiToken": "TUPLE_SECRET_CANARY", "safe": "kept"},
                )
            },
        }
    )

    assert recorded["metadata"]["items"][0]["apiToken"] == "[REDACTED]"
    assert recorded["metadata"]["items"][0]["safe"] == "kept"
    ledger_text = "\n".join(RawEventLog(store / RAW_EVENT_LOG_FILENAME).read_lines())
    assert "TUPLE_SECRET_CANARY" not in ledger_text


def test_local_api_event_summary_counts_without_metadata_leakage(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "0")
    store_root, result = _make_run(tmp_path)
    client = TestClient(create_local_api_app(store_dir=store_root))
    secretish = "bearer-redaction-fixture-local-summary-placeholder-1234567890"
    first = client.post(
        "/events",
        json={
            "source": "codex",
            "event_type": "note",
            "run_id": result.run_id,
            "metadata": {"summary": "called " + secretish},
        },
    )
    second = client.post(
        "/events",
        json={
            "source": "hermes",
            "event_type": "model_usage",
            "run_id": result.run_id,
            "provider": "openai",
            "estimated_input_tokens": 100,
            "estimated_output_tokens": 25,
            "estimated_cost_usd": 0.0005,
            "metadata": {"raw_provider_body": secretish, "cached_input_tokens": 40, "reasoning_output_tokens": 5},
        },
    )
    other = client.post("/events", json={"source": "manual", "event_type": "note", "run_id": "run_other"})
    assert first.status_code == 200
    assert second.status_code == 200
    assert other.status_code == 200
    with (store_root / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "event_id": "evt_huge_a",
                    "created_at": 1,
                    "source": "legacy",
                    "event_type": "model_usage",
                    "run_id": result.run_id,
                    "estimated_cost_usd": 1e308,
                    "estimated_input_tokens": 10**400,
                    "estimated_output_tokens": 10**400,
                    "metadata": {"summary": "legacy malformed event"},
                }
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "event_id": "evt_huge_b",
                    "created_at": 2,
                    "source": "legacy",
                    "event_type": "model_usage",
                    "run_id": result.run_id,
                    "estimated_cost_usd": 1e308,
                    "metadata": {},
                }
            )
            + "\n"
        )

    response = client.get(f"/events/summary?run_id={result.run_id}")

    assert response.status_code == 200
    assert "Infinity" not in response.text
    assert secretish not in response.text
    summary = response.json()["summary"]
    assert summary["event_count"] == 4
    assert summary["note_count"] == 1
    assert summary["estimated_cost_usd"] == 0.0
    assert summary["estimated_input_tokens"] == 0
    assert summary["estimated_output_tokens"] == 0
    assert summary["estimated_total_tokens"] == 0
    assert summary["cached_input_tokens"] == 0
    assert summary["cache_creation_input_tokens"] == 0
    assert summary["cache_read_input_tokens"] == 0
    assert summary["reasoning_output_tokens"] == 0
    assert summary["total_tokens_including_cached"] == 0
    assert summary["by_source"] == {"codex": 1, "hermes": 1, "legacy": 2}
    assert summary["by_type"] == {"model_usage": 3, "note": 1}
    assert summary["by_provider"] == {"openai": 1}
    assert summary["tokens_by_provider"] == {}

    all_runs = client.get("/events/summary").json()["summary"]
    assert all_runs["event_count"] == 5
    assert all_runs["note_count"] == 2
    assert client.get("/events/summary?run_id=../bad").status_code == 422
    assert client.get("/events/summary?limit=0").status_code == 422
    assert client.get("/events/summary?limit=201").status_code == 422


def test_local_api_usage_sources_endpoint_discovers_local_stores(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _make_home_usage_sources(home)
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state", usage_discovery=_usage_discovery_for_home(home)))

    response = client.get("/usage/sources")

    assert response.status_code == 200
    payload = response.json()
    by_client = {source["client"]: source for source in payload["sources"]}
    assert by_client["codex"]["status"] == "found"
    assert by_client["codex"]["session_count"] == 1
    assert by_client["codex"]["importer"] == "agentacct usage import-local --client codex"
    assert by_client["hermes"]["status"] == "found"
    assert by_client["hermes"]["usage_confidence"] == "client_reported"
    assert by_client["hermes"]["cost_confidence"] == "client_reported"
    assert by_client["openclaw"]["status"] == "found"
    assert by_client["openclaw"]["importer"] == "agentacct usage import-local --client openclaw"
    assert by_client["claude-code"]["status"] == "missing"
    assert "prompt" not in json.dumps(payload).lower()


def test_local_api_default_usage_discovery_is_disabled_even_when_env_points_to_logs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.setenv("OPENCLAW_DIR", str(home / ".openclaw"))
    _make_home_usage_sources(home)
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))

    sources = client.get("/usage/sources").json()["sources"]
    preview = client.get("/usage/preview").json()

    assert sources == []
    assert preview["totals"]["sessions"] == 0
    assert preview["events"] == []


def test_local_api_usage_preview_returns_token_totals_without_transcripts(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _make_home_usage_sources(home)
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state", usage_discovery=_usage_discovery_for_home(home)))

    response = client.get("/usage/preview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["totals"]["sessions"] == 3
    assert payload["totals"]["input_tokens"] == 307
    assert payload["totals"]["output_tokens"] == 43
    assert payload["totals"]["cached_input_tokens"] == 111
    assert payload["totals"]["total_tokens_including_cached"] == 461
    assert round(payload["totals"]["client_reported_cost_usd"], 2) == 0.43
    assert payload["totals"]["estimated_equivalent_cost_sessions"] == 2
    assert payload["totals"]["estimated_equivalent_cost_usd"] > 0
    assert "prompt" not in json.dumps(payload).lower()
    assert "content" not in json.dumps(payload).lower()


def test_local_import_saves_usage_to_activity_log_and_skips_unchanged(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _make_home_usage_sources(home)
    store_root = tmp_path / "state"
    client = TestClient(create_local_api_app(store_dir=store_root))

    payload = _import_local_usage(store_root, home)

    assert payload["imported_events"] == 3
    assert payload["priced_events"] == 1
    summary = client.get("/events/summary").json()["summary"]
    assert summary["event_count"] == 3
    assert summary["by_source"] == {
        "codex-local-session-import": 1,
        "hermes-local-session-import": 1,
        "openclaw-local-session-import": 1,
    }
    assert summary["by_cost_confidence"] == {"client_reported": 2, "estimated_from_tokens": 1}

    before_duplicate = RawEventLog(store_root / RAW_EVENT_LOG_FILENAME).read_lines()
    duplicate = _import_local_usage(store_root, home)

    # Nothing changed since the first import, so the duplicate scan is a
    # no-op: no rows are rewritten and no event_ids are reissued (skip-unchanged).
    assert duplicate["imported_events"] == 0
    assert duplicate["refreshed_events"] == 0
    assert client.get("/events/summary").json()["summary"]["event_count"] == 3
    assert RawEventLog(store_root / RAW_EVENT_LOG_FILENAME).read_lines() == before_duplicate


def test_pricing_catalog_scope_overrides_the_env(monkeypatch, tmp_path):
    from agentacct.cost import _builtin_pricing_entries, pricing_catalog, pricing_catalog_scope
    from agentacct.pricing_catalog import PricingCatalog

    # Point the global env at a bogus catalog; the scope must ignore it. A
    # distinct catalog instance proves the override — not the env fallback — won.
    monkeypatch.setenv("AGENT_CHRONICLE_PRICING_CATALOG_PATH", str(tmp_path / "nope.json"))
    scoped = PricingCatalog(_builtin_pricing_entries())
    with pricing_catalog_scope(scoped):
        assert pricing_catalog() is scoped
    assert pricing_catalog() is not scoped  # restored, env resolution resumes


def test_noop_import_reconciles_evidence_and_surfaces_failure(
    tmp_path,
    monkeypatch,
):
    from agentacct.ingestion_health import (
        EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE,
    )

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _make_home_usage_sources(home)
    store_root = tmp_path / "state"
    client = TestClient(create_local_api_app(store_dir=store_root))
    initial = _import_local_usage(store_root, home)
    assert initial["imported_events"] == 3
    before = RawEventLog(store_root / RAW_EVENT_LOG_FILENAME).read_lines()
    calls: list[tuple[bool, str | None]] = []

    def failed_reconcile(_service, *, complete=True, transport="internal"):
        calls.append((complete, transport))
        return {
            "enabled": True,
            "complete_requested": True,
            "complete_applied": False,
            "errors": ["private sqlite projection detail"],
            "conflicts": 0,
            "existing_conflicts": 2,
        }

    monkeypatch.setattr(
        SentinelService,
        "reconcile_evidence_refreshable_usage_snapshot",
        failed_reconcile,
    )

    payload = _import_local_usage(store_root, home)
    assert calls == [(True, "internal")]
    # A no-op scan still reconciles the complete current ledger (the fail-open
    # compensation path) and reports the failure honestly...
    assert payload["imported_events"] == 0
    reconcile = payload["evidence_refreshable_usage"]
    assert reconcile["enabled"] is True
    assert reconcile["complete_applied"] is False
    assert reconcile["existing_conflicts"] == 2
    assert len(reconcile["errors"]) == 1
    # ...without rewriting any ledger rows.
    assert RawEventLog(store_root / RAW_EVENT_LOG_FILENAME).read_lines() == before

    health = client.get("/ingestion/health").json()
    assert health["state"] == "degraded"
    assert health["issues"]
    assert {
        issue["code"] for issue in health["issues"]
    } == {EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE}
    # The surfaced health actions must never leak a private/sqlite projection path.
    assert all(
        "private sqlite projection detail" not in issue["action"]
        for issue in health["issues"]
    )


def test_local_import_uses_store_pricing_snapshot(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _make_home_usage_sources(home)
    con = sqlite3.connect(home / ".hermes" / "state.db")
    try:
        con.execute(
            "update sessions set model = ?, billing_provider = ?, estimated_cost_usd = 0, actual_cost_usd = 0",
            ("gpt-dashboard-price", "openai"),
        )
        con.commit()
    finally:
        con.close()
    store_root = tmp_path / "state"
    catalog_path = default_pricing_catalog_snapshot_path(store_root)
    assert catalog_path is not None
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_text(
        json.dumps(
            {
                "gpt-dashboard-price": {
                    "litellm_provider": "openai",
                    "input_cost_per_token": 0.000001,
                    "output_cost_per_token": 0.000002,
                }
            }
        ),
        encoding="utf-8",
    )

    payload = _import_local_usage(store_root, home)

    # The store-local snapshot — not the builtin table — is the only place
    # gpt-dashboard-price resolves, so priced=2 proves the snapshot applied.
    assert payload["priced_events"] == 2
    hermes_row = next(
        row for row in _stored_usage_rows(store_root) if row.get("model") == "gpt-dashboard-price"
    )
    assert hermes_row["cost_confidence"] == "estimated_from_tokens"
    assert hermes_row["estimated_cost_usd"] > 0


def test_pricing_middleware_honors_legacy_env_pin_over_store_snapshot(tmp_path, monkeypatch):
    # A pre-rename user pins a catalog via AGENT_SENTINEL_PRICING_CATALOG_PATH
    # (the alias contract accepts the old name forever). The per-request store
    # snapshot activation must treat that legacy pin exactly like the new
    # name: never shadow it with the store snapshot.
    from agentacct.cost import LEGACY_PRICING_CATALOG_PATH_ENV, PRICING_CATALOG_PATH_ENV
    from agentacct.env_compat import read_env_alias

    store_root = tmp_path / "state"
    snapshot_path = default_pricing_catalog_snapshot_path(store_root)
    assert snapshot_path is not None
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text(json.dumps({"snapshot-model": {"input_cost_per_token": 0.000001}}), encoding="utf-8")
    pinned = tmp_path / "pinned-catalog.json"
    pinned.write_text(json.dumps({"pinned-model": {"input_cost_per_token": 0.000002}}), encoding="utf-8")

    api_app = create_local_api_app(store_dir=store_root)

    @api_app.get("/__test/effective-pricing-catalog")
    def _effective_catalog() -> dict:
        return {"effective": read_env_alias(PRICING_CATALOG_PATH_ENV)}

    client = TestClient(api_app)

    # Legacy-name pin only: the pin must win over the store snapshot...
    monkeypatch.delenv(PRICING_CATALOG_PATH_ENV, raising=False)
    monkeypatch.setenv(LEGACY_PRICING_CATALOG_PATH_ENV, str(pinned))
    assert client.get("/__test/effective-pricing-catalog").json()["effective"] == str(pinned)
    # ...and the middleware must not leave the new name set afterwards.
    assert PRICING_CATALOG_PATH_ENV not in os.environ

    # New-name pin keeps winning too.
    monkeypatch.setenv(PRICING_CATALOG_PATH_ENV, str(tmp_path / "new-pin.json"))
    assert client.get("/__test/effective-pricing-catalog").json()["effective"] == str(tmp_path / "new-pin.json")

    # With no pin at all, the store snapshot applies during the request and is
    # cleaned up after it.
    monkeypatch.delenv(PRICING_CATALOG_PATH_ENV, raising=False)
    monkeypatch.delenv(LEGACY_PRICING_CATALOG_PATH_ENV, raising=False)
    assert client.get("/__test/effective-pricing-catalog").json()["effective"] == str(snapshot_path)
    assert PRICING_CATALOG_PATH_ENV not in os.environ


def test_local_import_labels_codex_internal_review_as_usage_overhead(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _make_home_usage_sources(home)
    _add_codex_internal_review_source(home)
    store_root = tmp_path / "state"
    client = TestClient(create_local_api_app(store_dir=store_root))

    payload = _import_local_usage(store_root, home)

    assert payload["imported_events"] == 4
    summary = client.get("/events/summary?limit=20").json()["summary"]
    assert summary["event_count"] == 4
    # The internal-review rollout remains stored and visible, but its copied
    # cumulative Codex counters are held from additive provider totals until
    # lineage normalization proves the exclusive delta.
    assert summary["excluded_non_additive_usage_events"] == 1
    assert summary["tokens_by_provider"]["codex"]["event_count"] == 1
    assert summary["tokens_by_provider"]["codex"]["estimated_input_tokens"] == 200
    assert summary["tokens_by_provider"]["codex"]["cache_read_input_tokens"] == 100
    assert summary["tokens_by_provider"]["codex"]["estimated_output_tokens"] == 20
    assert summary["tokens_by_provider"]["codex"]["total_tokens_including_cached"] == 320

    sessions = client.get(
        "/sessions", headers={"accept": "application/json"}
    ).json()["sessions"]
    review = next(
        row
        for row in sessions
        if row["client_session_id"] == "codex-review-session"
    )
    assert review["session_kind"] == "internal"
    assert review["usage"]["rows"] == 1
    assert review["usage"]["additive_rows"] == 0
    assert review["usage"]["excluded_non_additive_rows"] == 1
    assert review["usage"]["total_tokens"] == 0
    assert review["usage"]["estimated_cost_usd"] is None
    assert "not a zero-usage or zero-cost claim" in review["usage_note"]


def test_local_import_replaces_stale_local_usage_without_deleting_notes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _make_home_usage_sources(home)
    store_root = tmp_path / "state"
    store_root.mkdir(parents=True)
    stale_usage = {
        "event_id": "evt_stale_usage",
        "created_at": 1,
        "source": "codex-local-session-import",
        "event_type": "model_usage",
        "provider": "codex",
        "model": "gpt-5.5",
        "estimated_input_tokens": 1,
        "estimated_output_tokens": 1,
        "estimated_cost_usd": 999.0,
        "usage_confidence": "client_reported",
        "cost_confidence": "estimated_from_tokens",
        "metadata": {
            "usage_source": "local_client_session_store",
            "client": "codex",
            "client_session_id": "codex-session",
            "cached_input_tokens": 1,
        },
    }
    note = {"event_id": "evt_note", "created_at": 2, "source": "manual", "event_type": "note", "metadata": {"summary": "keep me"}}
    (store_root / "events.jsonl").write_text(json.dumps(stale_usage) + "\n" + json.dumps(note) + "\n", encoding="utf-8")
    client = TestClient(create_local_api_app(store_dir=store_root))

    _import_local_usage(store_root, home)

    summary = client.get("/events/summary?limit=20").json()["summary"]
    assert summary["event_count"] == 4
    assert summary["note_count"] == 1
    assert summary["estimated_cost_usd"] < 999.0
    assert summary["by_source"]["manual"] == 1
    assert summary["tokens_by_provider"]["codex"]["estimated_input_tokens"] == 200


def test_local_api_rejects_server_owned_event_fields_from_callers(tmp_path):
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))

    response = client.post(
        "/events",
        json={"source": "x", "event_type": "y", "event_id": "caller", "created_at": "not-a-number"},
    )

    assert response.status_code == 422
    assert client.get("/events").status_code == 200


def test_local_api_strips_reserved_usage_truth_marker_from_public_events(tmp_path):
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))

    response = client.post(
        "/events",
        json={
            "source": "codex-local-session-import",
            "event_type": "model_usage",
            "provider": "codex",
            "model": "gpt-5.5",
            "estimated_input_tokens": 100,
            "estimated_output_tokens": 25,
            "estimated_cost_usd": 9.99,
            "usage_confidence": "client_reported",
            "cost_confidence": "client_reported",
            "metadata": {
                "usage_source": "local_client_session_store",
                "usage_provenance": "agent_sentinel_local_usage_import",
                "client": "codex",
                "client_session_id": "forged-session",
            },
        },
    )

    assert response.status_code == 200
    event = response.json()["event"]
    assert "usage_source" not in event["metadata"]
    assert "usage_provenance" not in event["metadata"]
    assert event["metadata"]["reserved_usage_source_stripped"] is True
    assert event["metadata"]["reserved_usage_provenance_stripped"] is True
    summary = client.get("/events/summary").json()["summary"]
    overview = client.get("/overview").json()["overview"]
    timeline = client.get("/timeline").json()["timeline"]
    assert summary["estimated_input_tokens"] == 0
    assert overview["usage_truth_event_count"] == 0
    assert timeline[0]["event_kind"] == "usage_diagnostic"


def test_local_api_redacts_secret_shaped_string_values(tmp_path):
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))
    secretish = "Bearer " + "local-redaction-placeholder-1234567890"

    response = client.post(
        "/events",
        json={"source": "x", "event_type": "y", "metadata": {"summary": f"called with {secretish}"}},
    )

    assert response.status_code == 200
    event = response.json()["event"]
    # Only the secret span goes; the prose around it is ledger data and stays.
    assert event["metadata"]["summary"] == "called with [REDACTED_SECRET]"
    assert event["metadata"]["value_redaction_applied"] is True
    ledger_text = "\n".join(RawEventLog(tmp_path / "state" / RAW_EVENT_LOG_FILENAME).read_lines())
    assert secretish not in ledger_text


def test_local_api_validates_event_run_id_and_metadata_size(tmp_path):
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))

    bad_run = client.post("/events", json={"source": "x", "event_type": "y", "run_id": "../bad"})
    huge_metadata = client.post(
        "/events",
        json={"source": "x", "event_type": "y", "metadata": {"blob": "x" * 9000}},
    )

    assert bad_run.status_code == 422
    assert huge_metadata.status_code == 422


def test_local_api_rejects_unbounded_top_level_event_extensions(tmp_path):
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))

    response = client.post(
        "/events",
        json={"source": "x", "event_type": "y", "extra_blob": "x" * 9000},
    )

    assert response.status_code == 422


def test_local_api_validates_events_query_run_id_and_skips_bad_jsonl(tmp_path):
    state = tmp_path / "state"
    client = TestClient(create_local_api_app(store_dir=state))
    assert client.post("/events", json={"source": "x", "event_type": "y", "run_id": "run_good"}).status_code == 200
    with (state / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")

    bad = client.get("/events?run_id=../bad")
    good = client.get("/events?run_id=run_good")

    assert bad.status_code == 422
    assert good.status_code == 200
    assert len(good.json()["events"]) == 1


def test_local_api_aggregates_usage_reported_through_mcp(tmp_path):
    store_root, result = _make_run(tmp_path)
    server = SentinelMCPServer(store_dir=store_root)
    for source, model, input_tokens, output_tokens, cost in [
        ("claude-code", "claude-code-live-mcp-smoke", 30, 7, 0.0002),
        ("codex", "codex-cli-live-mcp-smoke", 20, 5, 0.0001),
    ]:
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": input_tokens,
                "method": "tools/call",
                "params": {
                    "name": "agentacct_record_event",
                    "arguments": {
                        "source": source,
                        "event_type": "model_usage",
                        "run_id": result.run_id,
                        "provider": "agent-cli",
                        "model": model,
                        "estimated_input_tokens": input_tokens,
                        "estimated_output_tokens": output_tokens,
                        "estimated_cost_usd": cost,
                        "usage_confidence": "estimated",
                        "cost_confidence": "estimated",
                        "metadata": {"summary": f"{source} reported usage through agentacct MCP"},
                    },
                },
            }
        )
        assert response is not None
        assert "error" not in response

    client = TestClient(create_local_api_app(store_dir=store_root))
    summary = client.get(f"/events/summary?run_id={result.run_id}").json()["summary"]

    # MCP-reported usage is a diagnostic lane, never usage truth: it counts as
    # events by source/provider, but adds nothing to trusted usage totals.
    assert summary["event_count"] == 2
    assert summary["estimated_input_tokens"] == 0
    assert summary["estimated_output_tokens"] == 0
    assert summary["estimated_cost_usd"] == 0.0
    assert summary["by_source"] == {"claude-code": 1, "codex": 1}
    assert summary["by_provider"] == {"agent-cli": 2}


def test_local_api_summary_reports_usage_context_bridge(tmp_path):
    store_root, result = _make_run(tmp_path)
    server = SentinelMCPServer(store_dir=store_root)
    usage_event = _record_trusted_usage(store_root, run_id=result.run_id, session="codex-session", input_tokens=100, output_tokens=20, cached_input_tokens=30)
    section_response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "agentacct_record_section",
                "arguments": {
                    "source": "codex",
                    "section_id": "context-bridge",
                    "section_status": "checkpoint",
                    "section_title": "Context bridge",
                    "client": "codex",
                    "client_session_id": "codex-session",
                    "summary": "Joined MCP sections to local usage.",
                },
            },
        }
    )
    debug_response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "agentacct_record_agent_usage_debug",
                "arguments": {
                    "source": "codex",
                    "client": "codex",
                    "client_session_id": "codex-session",
                    "reporting_basis": "unavailable",
                    "summary": "Usage was not visible to the agent.",
                },
            },
        }
    )
    assert usage_event["metadata"]["usage_provenance"] == "agent_sentinel_local_usage_import"
    assert "error" not in section_response
    assert "error" not in debug_response

    client = TestClient(create_local_api_app(store_dir=store_root))
    summary = client.get("/events/summary").json()["summary"]

    assert summary["usage_context_bridge"]["linked_usage_records"] == 1
    assert summary["usage_context_bridge"]["context_matched_usage_records"] == 1
    assert summary["usage_context_bridge"]["attributed_usage_records"] == 1
    assert summary["usage_context_bridge"]["links"][0]["section_count"] == 1
    assert summary["usage_context_bridge"]["links"][0]["usage_debug_count"] == 1


def test_local_api_derived_work_ledger_endpoints_do_not_count_usage_debug_cost(tmp_path):
    store_root, result = _make_run(tmp_path)
    client = TestClient(create_local_api_app(store_dir=store_root))
    _record_trusted_usage(store_root, run_id=result.run_id, session="codex-session", input_tokens=100, output_tokens=25, cached_input_tokens=50)
    client.post(
        "/events",
        json={
            "source": "codex",
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "mcp-v1",
                "section_status": "completed",
                "section_title": "MCP v1 convergence",
                "summary": "Converged MCP fields into the work ledger.",
                "client": "codex",
                "client_session_id": "codex-session",
            },
        },
    )
    client.post(
        "/events",
        json={
            "source": "codex",
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "section_id": "mcp-v1",
                "evidence_type": "test",
                "result": "passed",
                "summary": "Tests passed.",
            },
        },
    )
    client.post(
        "/events",
        json={
            "source": "codex",
            "event_type": "agent_usage_debug_reported",
            "estimated_input_tokens": None,
            "estimated_output_tokens": None,
            "estimated_cost_usd": None,
            "usage_confidence": "unknown",
            "cost_confidence": "unknown",
            "metadata": {
                "sentinel_semantic_kind": "agent_usage_debug",
                "client": "codex",
                "client_session_id": "codex-session",
                "reporting_basis": "visible_client_usage",
                "agent_reported_total_tokens": 999_999,
                "agent_reported_cost_usd": 99.99,
                "summary": "Diagnostics-only usage visibility.",
            },
        },
    )

    overview = client.get("/overview").json()["overview"]
    work_items = client.get("/work-items").json()["work_items"]
    timeline = client.get("/timeline").json()["timeline"]
    work_item = client.get("/work-items/mcp-v1").json()["work_item"]

    assert overview["total_tokens"] == 175
    assert overview["estimated_cost_total"] == 0.01
    assert overview["attributed_usage_count"] == 1
    assert overview["unattributed_usage_count"] == 0
    assert overview["usage_debug_event_count"] == 1
    assert work_items[0]["work_id"] == "codex::codex-session::mcp-v1"
    assert work_items[0]["section_id"] == "mcp-v1"
    assert work_items[0]["usage_total"] == 175
    assert work_items[0]["estimated_cost_total"] == 0.01
    assert work_items[0]["evidence_status"] == "strong"
    # Sections posted through the generic HTTP /events path carry ids with
    # unverifiable provenance: they keep joining, but never at exact.
    assert work_item["join_confidence"] == "high"
    assert {entry["event_kind"] for entry in timeline} >= {"usage", "work", "evidence", "usage_debug"}


def test_local_api_public_work_items_redact_paths_and_commands(tmp_path):
    store_root, _ = _make_run(tmp_path)
    client = TestClient(create_local_api_app(store_dir=store_root))
    client.post(
        "/events",
        json={
            "source": "codex",
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "privacy",
                "section_status": "completed",
                "section_title": "Privacy cleanup",
                "client": "codex",
                "client_session_id": "codex-session",
                "project_dir": "C:\\Users\\alice\\private-repo",
                "files": [
                    "src/agentacct/api.py",
                    "src\\agentacct\\cli.py",
                    "/Users/alice/private-repo/secret.py",
                    "..\\secret.py",
                    "C:\\Users\\alice\\private-repo\\secret.py",
                ],
            },
        },
    )
    client.post(
        "/events",
        json={
            "source": "codex",
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "section_id": "privacy",
                "evidence_type": "test",
                "result": "passed",
                "summary": "Tests passed.",
                "command": "pytest --token secret",
                "artifact_path": "/tmp/private/report.json",
                "artifact_url": "https://example.test/report?token=secret",
            },
        },
    )

    payload = client.get("/work-items/privacy").json()["work_item"]
    encoded = json.dumps(payload)

    assert payload["project_dir"] == "private-repo"
    assert payload["files"] == ["src/agentacct/api.py", "src/agentacct/cli.py"]
    assert payload["evidence_events"][0]["command"] is None
    assert payload["evidence_events"][0]["artifact_path"] is None
    assert payload["evidence_events"][0]["artifact_url"] is None
    assert "/Users/alice" not in encoded
    assert "C:\\Users" not in encoded
    assert "..\\secret" not in encoded
    assert "--token secret" not in encoded


def test_local_api_usage_timeline_uses_log_occurrence_time_not_import_time(tmp_path):
    store_root, result = _make_run(tmp_path)
    client = TestClient(create_local_api_app(store_dir=store_root))
    usage_event = _record_trusted_usage(
        store_root,
        run_id=result.run_id,
        session="codex-old-session",
        input_tokens=40,
        output_tokens=10,
        cached_input_tokens=0,
        started_at=1_781_949_600,
        updated_at=1_782_036_000,
    )

    timeline = client.get("/timeline").json()["timeline"]
    usage_entry = next(entry for entry in timeline if entry["event_kind"] == "usage")

    assert usage_event["metadata"]["usage_provenance"] == "agent_sentinel_local_usage_import"
    assert usage_entry["time"] == 1_782_036_000


def test_run_report_includes_cost_proxy_events(tmp_path):
    store_root, result = _make_run(tmp_path)
    ledger = CostLedger(store_root)
    ledger.record_usage(
        UsageEstimate(
            provider="openrouter",
            model="openai/gpt-4o-mini",
            endpoint="/openrouter/v1/chat/completions",
            estimated_input_tokens=20,
            estimated_output_tokens=5,
            estimated_cost_usd=0.000321,
            forwarded=True,
            usage_confidence="exact",
            cost_confidence="estimated",
        ),
        run_id=result.run_id,
        decision="allowed",
        reason="within budget",
    )
    client = TestClient(create_local_api_app(store_dir=store_root))

    report = client.get(f"/runs/{result.run_id}/report").json()

    cost = report["cost"]
    assert cost["event_count"] == 1
    assert cost["estimated_total_cost_usd"] == pytest.approx(0.000321)
    assert cost["by_provider_estimated_usd"]["openrouter"] == pytest.approx(0.000321)
    assert cost["by_model_estimated_usd"]["openai/gpt-4o-mini"] == pytest.approx(0.000321)


def test_top_level_serve_refuses_non_local_bind(tmp_path):
    result = CliRunner().invoke(app, ["serve", "--host", "0.0.0.0", "--store-dir", str(tmp_path / "state")])

    assert result.exit_code == 1
    assert "Refusing non-local bind" in result.output


def test_top_level_serve_prints_local_usage_scan_notice(tmp_path, monkeypatch):
    calls = []

    def fake_run(api_app, *, host, port, log_level):
        calls.append({"api_app": api_app, "host": host, "port": port, "log_level": log_level})

    monkeypatch.setattr("uvicorn.run", fake_run)
    # Keep the default-port path deterministic regardless of what is bound on the
    # machine running the suite (the live dashboard may hold 8765).
    monkeypatch.setattr("agentacct.cli._probe_port_free", lambda host, port: True)

    result = CliRunner().invoke(app, ["serve", "--store-dir", str(tmp_path / "state")])

    assert result.exit_code == 0, result.output
    assert "Local usage scan: enabled" in result.output
    assert "agentacct api serve" in result.output
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 8765


def test_top_level_serve_help_describes_local_dashboard():
    result = CliRunner().invoke(app, ["serve", "--help"])

    assert result.exit_code == 0
    assert "dashboard" in result.output.lower()
    assert "127.0.0.1" in result.output


def test_top_level_serve_falls_back_when_default_port_busy(tmp_path, monkeypatch):
    """A busy DEFAULT port advances to the next free one and reports it."""
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append(kwargs))
    # Simulate the default 8765 being occupied and 8766 free.
    monkeypatch.setattr("agentacct.cli._probe_port_free", lambda host, port: port != 8765)

    result = CliRunner().invoke(app, ["serve", "--store-dir", str(tmp_path / "state")])

    assert result.exit_code == 0, result.output
    assert calls[0]["port"] == 8766
    assert "Port 8765 was busy" in result.output
    assert "http://127.0.0.1:8766" in result.output


def test_top_level_serve_explicit_busy_port_errors(tmp_path, monkeypatch):
    """An explicit --port that is occupied fails loudly instead of moving."""
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append(kwargs))
    # Every port is busy; with an explicit --port there must be no silent move.
    monkeypatch.setattr("agentacct.cli._probe_port_free", lambda host, port: False)

    result = CliRunner().invoke(app, ["serve", "--port", "9000", "--store-dir", str(tmp_path / "state")])

    assert result.exit_code == 1, result.output
    assert "Port 9000 is already in use" in result.output
    assert calls == []  # uvicorn.run was never reached


def test_top_level_serve_explicit_free_port_binds_exactly(tmp_path, monkeypatch):
    """An explicit --port that is free binds that exact port (no probing drift)."""
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr("agentacct.cli._probe_port_free", lambda host, port: True)

    result = CliRunner().invoke(app, ["serve", "--port", "9123", "--store-dir", str(tmp_path / "state")])

    assert result.exit_code == 0, result.output
    assert calls[0]["port"] == 9123
    assert "was busy" not in result.output


def test_select_serve_port_advances_past_real_busy_socket():
    """End-to-end probe: a real listening socket forces a real fallback."""
    from agentacct.cli import _select_serve_port

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    busy_port = sock.getsockname()[1]
    try:
        chosen = _select_serve_port("127.0.0.1", busy_port, allow_fallback=True)
        assert chosen != busy_port
        assert busy_port < chosen <= busy_port + 20
    finally:
        sock.close()


def test_select_serve_port_explicit_busy_raises_on_real_socket():
    """With fallback disabled, a real busy port raises instead of moving."""
    from agentacct.cli import _select_serve_port

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    busy_port = sock.getsockname()[1]
    try:
        with pytest.raises(OSError):
            _select_serve_port("127.0.0.1", busy_port, allow_fallback=False)
    finally:
        sock.close()


def _make_home_claude_mixed_model_source(home):
    project = home / ".claude" / "projects" / "-tmp-project"
    project.mkdir(parents=True)
    rows = [
        {
            "type": "assistant",
            "sessionId": "claude-session",
            "cwd": "/tmp/project",
            "message": {
                "model": "claude-opus-4-8",
                "usage": {
                    "input_tokens": 30,
                    "cache_creation_input_tokens": 33,
                    "cache_read_input_tokens": 44,
                    "output_tokens": 12,
                },
            },
        },
        {
            "type": "assistant",
            "sessionId": "claude-session",
            "cwd": "/tmp/project",
            "message": {
                "model": "claude-haiku-4-5-20251001",
                "usage": {
                    "input_tokens": 4,
                    "cache_creation_input_tokens": 1,
                    "cache_read_input_tokens": 2,
                    "output_tokens": 3,
                },
            },
        },
    ]
    (project / "claude-session.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _stored_claude_usage_row(event_id: str, client_session_id: str, *, model: str | None = None, lane: str | None = None) -> dict:
    row = {
        "event_id": event_id,
        "created_at": 1,
        "source": "claude-code-local-session-import",
        "event_type": "model_usage",
        "provider": "claude-code",
        "model": model,
        "estimated_input_tokens": 1,
        "estimated_output_tokens": 1,
        "usage_confidence": "client_reported",
        "metadata": {
            "usage_source": "local_client_session_store",
            "usage_provenance": "agent_sentinel_local_usage_import",
            "client": "claude-code",
            "client_session_id": client_session_id,
            "client_transcript_id": "claude-session",
            "cached_input_tokens": 0,
        },
    }
    if lane is not None:
        row["metadata"]["usage_row_lane"] = lane
    return row


def test_local_import_replaces_legacy_alias_rows_once(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _make_home_claude_mixed_model_source(home)
    store_root = tmp_path / "state"
    store_root.mkdir(parents=True)
    legacy_keys = ["claude-session", "claude-session:model:claude-opus-4-8"]
    with (store_root / "events.jsonl").open("w", encoding="utf-8") as handle:
        for index, key in enumerate(legacy_keys):
            handle.write(json.dumps(_stored_claude_usage_row(f"evt_legacy_{index}", key)) + "\n")
    client = TestClient(create_local_api_app(store_dir=store_root))

    payload = _import_local_usage(store_root, home)

    assert payload["migrated_events"] == 2
    stored = SentinelService(store_root).list_all_events()
    usage_rows = [event for event in stored if event["event_type"] == "model_usage"]
    assert len(usage_rows) == 2
    assert not any(str(event["event_id"]).startswith("evt_legacy_") for event in stored)
    assert {row["metadata"]["client_session_id"] for row in usage_rows} == {"claude-session"}
    assert all(":model:" not in row["metadata"]["client_session_id"] for row in usage_rows)
    assert {row["metadata"]["usage_row_lane"] for row in usage_rows} == {
        "model:claude-opus-4-8",
        "model:claude-haiku-4-5-20251001",
    }
    for row in usage_rows:
        assert row["metadata"]["migrated_from_client_session_ids"] == legacy_keys
        assert row["metadata"]["usage_key_migration_reason"] == "claude_code_session_key_unification"
    summary = client.get("/events/summary?limit=20").json()["summary"]
    assert summary["event_count"] == 2
    assert summary["estimated_input_tokens"] == 34
    assert summary["estimated_output_tokens"] == 15
    assert summary["cache_creation_input_tokens"] == 34
    assert summary["cache_read_input_tokens"] == 46


def test_local_import_refreshes_matching_lane_and_appends_new_lane(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _make_home_claude_mixed_model_source(home)
    store_root = tmp_path / "state"
    store_root.mkdir(parents=True)
    stale_lane_row = _stored_claude_usage_row(
        "evt_stale_opus", "claude-session", model="claude-opus-4-8", lane="model:claude-opus-4-8"
    )
    (store_root / "events.jsonl").write_text(json.dumps(stale_lane_row) + "\n", encoding="utf-8")

    payload = _import_local_usage(store_root, home)

    assert payload["migrated_events"] == 0
    stored = SentinelService(store_root).list_all_events()
    usage_rows = [event for event in stored if event["event_type"] == "model_usage"]
    assert len(usage_rows) == 2
    assert not any(str(event["event_id"]) == "evt_stale_opus" for event in stored)
    by_lane = {row["metadata"]["usage_row_lane"]: row for row in usage_rows}
    assert set(by_lane) == {"model:claude-opus-4-8", "model:claude-haiku-4-5-20251001"}
    assert by_lane["model:claude-opus-4-8"]["estimated_input_tokens"] == 30
    assert by_lane["model:claude-haiku-4-5-20251001"]["estimated_input_tokens"] == 4
    assert all("migrated_from_client_session_ids" not in row["metadata"] for row in usage_rows)
    assert {row["metadata"]["client_session_id"] for row in usage_rows} == {"claude-session"}


def test_local_api_payloads_carry_additive_cache_triple_keys(tmp_path):
    """Phase 2 Batch A smoke: new ledger keys are additive on the JSON
    surfaces."""

    store_root = tmp_path / "state"
    _record_trusted_usage(store_root, session="codex-batcha-session")
    client = TestClient(create_local_api_app(store_dir=store_root))
    section = client.post(
        "/events",
        json={
            "source": "codex",
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "batcha-work",
                "section_status": "completed",
                "section_title": "Batch A work",
                "client": "codex",
                "client_session_id": "codex-batcha-session",
            },
        },
    )
    assert section.status_code == 200

    overview = client.get("/overview").json()["overview"]
    assert overview["total_tokens"] == 175  # existing key keeps its meaning
    assert overview["total_fresh_tokens"] == 125
    assert overview["total_cache_read_tokens"] == 50  # merged-only fallback
    assert overview["total_cache_creation_tokens"] == 0
    assert "attention_group_count" in overview

    timeline = client.get("/timeline").json()["timeline"]
    usage_entry = next(entry for entry in timeline if entry["event_kind"] == "usage")
    assert usage_entry["tokens"] == 175
    assert usage_entry["tokens_fresh"] == 125
    assert usage_entry["tokens_cache_read"] == 50

    work_items = client.get("/work-items").json()["work_items"]
    item = next(item for item in work_items if item["section_id"] == "batcha-work")
    assert item["usage_total"] == 175
    assert item["usage_fresh_total"] == 125
    assert item["usage_cache_read_total"] == 50
    assert item["usage_cache_creation_total"] == 0
