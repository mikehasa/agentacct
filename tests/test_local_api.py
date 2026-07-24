import json
import os
import sqlite3

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agent_chronicle.api import UsageDiscoveryConfig, create_local_api_app
from agent_chronicle.cli import app
from agent_chronicle.cost import CostLedger, UsageEstimate
from agent_chronicle.mcp import SentinelMCPServer
from agent_chronicle.outcome import apply_judge_result, write_outcome
from agent_chronicle.pricing_catalog import default_pricing_catalog_snapshot_path
from agent_chronicle.reports import build_run_report_payload
from agent_chronicle.runner import RunOptions, start_guarded_run
from agent_chronicle.service import SentinelService
from agent_chronicle.storage import RunStore
from refresh_flash import refresh_flash_qs, run_dashboard_refresh


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


def _make_codex_only_home(home, *, model="gpt-5.6-sol"):
    """A codex-only usage home whose single session runs an unknown-priced
    model (the live gpt-5.6-sol gap) — for the dashboard reprice/TTL tests."""

    codex_home = home / ".codex"
    codex_sessions = codex_home / "sessions"
    codex_sessions.mkdir(parents=True)
    rollout_path = codex_sessions / "rollout-unknown-model.jsonl"
    rollout_path.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 400,
                            "cached_input_tokens": 100,
                            "output_tokens": 40,
                            "reasoning_output_tokens": 4,
                        }
                    },
                    "model": model,
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
            ("codex-unknown-model-1", str(rollout_path), 100, 200, "/tmp/project", "Unknown model", 440, model, "0.test"),
        )
        con.commit()
    finally:
        con.close()
    return codex_home


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
    assert "fake-api-key-for-redaction-test" not in (store_root / "events.jsonl").read_text(encoding="utf-8")
    assert "aws-secret-canary" not in (store_root / "events.jsonl").read_text(encoding="utf-8")
    assert "client-secret-canary" not in (store_root / "events.jsonl").read_text(encoding="utf-8")


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
    assert "TUPLE_SECRET_CANARY" not in (store / "events.jsonl").read_text(encoding="utf-8")


def test_local_api_event_summary_counts_without_metadata_leakage(tmp_path):
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
    response = run_dashboard_refresh(client)

    assert sources == []
    assert preview["totals"]["sessions"] == 0
    assert preview["events"] == []
    assert "imported=0" in refresh_flash_qs(response)


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


def test_local_dashboard_renders_work_first_and_keeps_live_usage_in_advanced(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _make_home_usage_sources(home)
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state", usage_discovery=_usage_discovery_for_home(home)))

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    tokens_html = client.get("/tokens").text
    sessions_html = client.get("/sessions", headers={"Accept": "text/html"}).text
    advanced_html = client.get("/advanced").text
    raw_html = client.get("/raw").text
    # Phase 8: Work is one compact session-first Task feed. Integration hygiene and
    # source diagnostics remain in Sessions/Advanced instead of posing as
    # user-facing tasks.
    assert "<h1>Work</h1>" in html
    assert "Recent Tasks" in html
    assert 'id="work-feed" aria-label="Task feed"' in html
    assert "Source coverage" not in html
    assert "Usage snapshot" not in html
    assert "unknown is not zero" not in html
    assert "Needs attention" not in html
    assert "Source coverage" in advanced_html
    assert "Attributed" in sessions_html
    assert 'href="#attention"' in sessions_html
    # An empty store's Ledger insights renders the batch D empty state, never
    # measured-looking zero cards ("good · 0 · $0.00").
    assert "Ledger insights" in sessions_html
    assert "Nothing to reconcile yet." in sessions_html
    # 3.5b explorer sections: the cache-inclusive By agent / By day tables
    # (and their caption excuse) are gone; the confidence tables stay.
    assert "Usage basics" in tokens_html
    assert "By platform" in tokens_html
    assert "By model" in tokens_html
    assert "By period" in tokens_html
    assert "include cache reads at full weight" not in tokens_html
    assert "Usage confidence" in tokens_html
    assert "Cost confidence" in tokens_html
    assert "Reconciliation" in sessions_html
    assert "Usage truth" in sessions_html
    assert "MCP work" in sessions_html
    assert "Attribution" in sessions_html
    assert "Action" in sessions_html
    for product_html in (html, tokens_html, sessions_html):
        assert ">461<" not in product_html
        assert "Local usage preview" not in product_html
        assert "AI tools detected" not in product_html
        assert "AI tools found on this Mac" not in product_html
        assert "Activity log" not in product_html
        assert 'action="/usage/import-local"' in product_html
        assert '<button class="button" type="submit">Refresh</button>' in product_html
        assert "Preview local logs" not in product_html
        assert "prompt" not in product_html.lower()
    assert '<a class="button secondary button-link" href="/raw">Preview local logs</a>' in advanced_html
    assert "Forensic inspectors" in advanced_html
    assert "AI tools found on this Mac" in raw_html
    assert "Usage records" in raw_html
    assert "Tokens found" in raw_html
    assert "Known usage sources not detected" in raw_html
    assert "runtime source detection, not an agent support matrix" in raw_html
    assert "Codex" in raw_html
    assert "Hermes" in raw_html
    assert "OpenClaw" in raw_html
    assert "Hermes local state database" in raw_html
    assert "Token-price estimate" in raw_html
    assert "Activity log" in raw_html
    assert "agentacct event log" in raw_html
    assert "Current run flow" in raw_html
    assert "Save usage to Activity log" in raw_html
    assert "agentacct usage import-local --client all" in raw_html
    assert "Main chats, child agents, and internal auto-review overhead" in raw_html
    assert "Input tokens / cost" in raw_html
    assert "Output tokens / cost" in raw_html
    assert "Cache create / cost" in raw_html
    assert "Cache read / cost" in raw_html
    assert "Total tokens / cost" in raw_html
    assert "2 priced / 1 unpriced records" not in html
    assert "openai / gpt-5.2" in raw_html
    assert "1 unpriced" not in html
    assert "Sort:" in raw_html
    assert "Next step" not in html


def test_local_dashboard_refresh_and_save_imports_usage_to_activity_log(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _make_home_usage_sources(home)
    store_root = tmp_path / "state"
    client = TestClient(create_local_api_app(store_dir=store_root, usage_discovery=_usage_discovery_for_home(home)))

    response = run_dashboard_refresh(client)

    assert response.status_code == 303
    assert "imported=3" in refresh_flash_qs(response)
    assert "refreshed=3" in refresh_flash_qs(response)
    assert "priced=1" in refresh_flash_qs(response)
    summary = client.get("/events/summary").json()["summary"]
    assert summary["event_count"] == 3
    assert summary["by_source"] == {
        "codex-local-session-import": 1,
        "hermes-local-session-import": 1,
        "openclaw-local-session-import": 1,
    }
    assert summary["by_cost_confidence"] == {"client_reported": 2, "estimated_from_tokens": 1}

    page = client.get(response.headers["location"])
    # The legacy raw-tab URL still lands on the raw page via the 302 shim.
    raw_page = client.get("/?tab=raw")

    assert page.status_code == 200
    assert raw_page.status_code == 200
    assert "Activity refreshed." in page.text
    assert "3 saved usage record(s) updated" in page.text
    assert "Codex" in page.text
    # Raw session cells render the ledger's short label, never the full id.
    # codex labels keep the LAST 8 chars (UUIDv7 tail rule); here the codex
    # and hermes fixture tails collide ("-session"), so the ledger's
    # deterministic ~hash4 suffix keeps the two sessions distinguishable.
    from hashlib import sha256 as _sha256

    codex_raw_label = "-session~" + _sha256(b"codex::codex-session").hexdigest()[:4]
    assert f"<code>{codex_raw_label}</code>" in raw_page.text
    assert "codex-session" not in raw_page.text
    # Work keeps usage as compact feed metadata; reconciliation and provider /
    # model analysis live on /sessions and /tokens.
    assert "total tokens" in page.text
    assert "Reconciliation" in client.get("/sessions", headers={"Accept": "text/html"}).text
    # The fixture's timestamps are historical, so the default 30-day explorer
    # window won't show them; days=all must (every filter state is a URL).
    assert "openai / gpt-5-mini" in client.get("/tokens?days=all").text
    assert "Session time" in raw_page.text

    before_duplicate = (store_root / "events.jsonl").read_bytes()
    duplicate = run_dashboard_refresh(client)

    assert duplicate.status_code == 303
    # Nothing changed since the first import, so the duplicate refresh is a
    # no-op: no rows are rewritten and no event_ids are reissued (skip-unchanged).
    assert "imported=0" in refresh_flash_qs(duplicate)
    assert "refreshed=0" in refresh_flash_qs(duplicate)
    assert client.get("/events/summary").json()["summary"]["event_count"] == 3
    assert (store_root / "events.jsonl").read_bytes() == before_duplicate


def test_refresh_uses_a_clean_url_and_a_one_time_flash_cookie(tmp_path, monkeypatch):
    """A refresh completes on a clean "/" (no 18-field query string); the
    summary rides a one-time flash cookie, so the banner shows once and a plain
    reload does not re-flash it. The refresh runs in the background via the
    /refreshing progress page, which 303s home with the flash when done."""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _make_home_usage_sources(home)
    store_root = tmp_path / "state"
    client = TestClient(
        create_local_api_app(store_dir=store_root, usage_discovery=_usage_discovery_for_home(home))
    )

    # POST starts a background refresh and hands off to the progress page.
    posted = client.post("/usage/import-local", follow_redirects=False)
    assert posted.status_code == 303 and posted.headers["location"] == "/refreshing"

    # The progress page finishes on a clean "/" — no query-string clutter.
    response = run_dashboard_refresh(client)
    assert response.headers["location"] == "/"
    assert "?" not in response.headers["location"]
    # The summary is carried by the flash cookie instead.
    assert "imported=3" in refresh_flash_qs(response)

    # First load shows the banner (cookie present) and clears the cookie.
    first = client.get("/")
    assert "Activity refreshed." in first.text
    # A plain reload does NOT re-flash — the cookie was consumed.
    second = client.get("/")
    assert "Activity refreshed." not in second.text


def test_refreshing_page_redirects_home_when_no_refresh_is_running(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _make_home_usage_sources(home)
    client = TestClient(
        create_local_api_app(store_dir=tmp_path / "state", usage_discovery=_usage_discovery_for_home(home))
    )
    idle = client.get("/refreshing", follow_redirects=False)
    assert idle.status_code == 303 and idle.headers["location"] == "/"


def test_refresh_progress_is_single_flight_and_consumes_result_once():
    from agent_chronicle.api import RefreshProgress

    progress = RefreshProgress()
    assert progress.begin() is not None
    assert progress.begin() is None  # a second click joins the running refresh
    progress.scan("codex", 500, 1000)
    progress.scan("claude-code", 100, 200)
    snap = progress.snapshot()
    assert snap["state"] == "running"
    assert snap["clients"]["codex"] == {"scanned": 500, "total": 1000}
    assert 0.0 < snap["fraction"] <= 0.60  # scanning band, monotonic
    progress.finish({"imported": 5, "priced": 3})
    assert progress.consume_result() == {"imported": 5, "priced": 3}
    assert progress.consume_result() is None  # result flashes exactly once
    assert progress.begin() is not None  # once done, a new refresh may start


def test_render_refreshing_page_running_shows_counts_error_stops_polling():
    from agent_chronicle.api import _render_refreshing_page

    running = _render_refreshing_page(
        {
            "state": "running",
            "phase": "Scanning Claude Code logs",
            "fraction": 0.35,
            "clients": {
                "codex": {"scanned": 795, "total": 795},
                "claude-code": {"scanned": 283, "total": 1833},
            },
        }
    )
    assert 'http-equiv="refresh"' in running  # self-polls while running
    assert "Scanning Claude Code logs" in running
    assert "283" in running and "1,833" in running  # live counts
    assert "width:35%" in running

    failed = _render_refreshing_page(
        {"state": "error", "phase": "Failed", "error": "BoomError: nope", "clients": {}}
    )
    assert 'http-equiv="refresh"' not in failed  # a failed refresh stops polling
    assert "BoomError: nope" in failed
    assert 'href="/"' in failed  # and offers a way back


def test_pricing_catalog_scope_overrides_the_env(monkeypatch, tmp_path):
    from agent_chronicle.cost import _builtin_pricing_entries, pricing_catalog, pricing_catalog_scope
    from agent_chronicle.pricing_catalog import PricingCatalog

    # Point the global env at a bogus catalog; the scope must ignore it. A
    # distinct catalog instance proves the override — not the env fallback — won.
    monkeypatch.setenv("AGENT_CHRONICLE_PRICING_CATALOG_PATH", str(tmp_path / "nope.json"))
    scoped = PricingCatalog(_builtin_pricing_entries())
    with pricing_catalog_scope(scoped):
        assert pricing_catalog() is scoped
    assert pricing_catalog() is not scoped  # restored, env resolution resumes


def test_dashboard_noop_refresh_reconciles_evidence_and_surfaces_failure(
    tmp_path,
    monkeypatch,
):
    from agent_chronicle.ingestion_health import (
        EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE,
    )

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _make_home_usage_sources(home)
    store_root = tmp_path / "state"
    client = TestClient(
        create_local_api_app(
            store_dir=store_root,
            usage_discovery=_usage_discovery_for_home(home),
        )
    )
    initial = run_dashboard_refresh(client)
    assert initial.status_code == 303
    before = (store_root / "events.jsonl").read_bytes()
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

    response = run_dashboard_refresh(client)
    assert response.status_code == 303
    assert calls == [(True, "internal")]
    # The refresh redirects to a clean "/"; the summary rides a one-time flash
    # cookie instead of the URL query string.
    assert response.headers["location"] == "/"
    flash = refresh_flash_qs(response)
    assert "imported=0" in flash
    assert "evidence_reconcile_enabled=True" in flash
    assert "evidence_reconcile_errors=1" in flash
    assert "evidence_reconcile_conflicts=2" in flash
    assert "evidence_reconcile_complete=False" in flash
    # The surfaced summary must never leak a private/sqlite projection path.
    assert "private" not in flash
    assert "sqlite" not in flash
    assert (store_root / "events.jsonl").read_bytes() == before

    # Loading "/" once consumes the flash cookie and shows the attention banner.
    page = client.get("/")
    assert page.status_code == 200
    assert "Evidence v2 current-usage reconciliation needs attention" in page.text
    assert "private sqlite projection detail" not in page.text
    # The tab shim still redirects, and no longer forwards a refresh summary
    # in the URL (that rode the removed query string).
    legacy_redirect = client.get("/?tab=raw", follow_redirects=False)
    assert legacy_redirect.status_code == 302
    assert "private" not in legacy_redirect.headers["location"]
    assert "evidence_reconcile" not in legacy_redirect.headers["location"]

    health = client.get("/ingestion/health").json()
    assert health["state"] == "degraded"
    assert health["issues"]
    assert {
        issue["code"] for issue in health["issues"]
    } == {EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE}
    assert all(
        "private sqlite projection detail" not in issue["action"]
        for issue in health["issues"]
    )


def test_product_dashboard_shows_ledger_insights_without_raw_diagnostics(tmp_path):
    store_root, result = _make_run(tmp_path)
    _record_trusted_usage(store_root, run_id=result.run_id, session="codex-insight-session")
    client = TestClient(create_local_api_app(store_dir=store_root))
    section = client.post(
        "/events",
        json={
            "source": "codex",
            "event_type": "section_completed",
            "run_id": result.run_id,
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "insight-work",
                "section_status": "completed",
                "section_title": "Insight work",
                "client": "codex",
                "client_session_id": "codex-insight-session",
                "summary": "Completed insight work.",
            },
        },
    )
    evidence = client.post(
        "/events",
        json={
            "source": "codex",
            "event_type": "machine_check",
            "run_id": result.run_id,
            "metadata": {"sentinel_semantic_kind": "evidence", "section_id": "insight-work", "result": "passed", "summary": "Tests passed."},
        },
    )

    assert section.status_code == 200
    assert evidence.status_code == 200

    overview_html = client.get("/").text
    sessions_html = client.get("/sessions", headers={"Accept": "text/html"}).text
    raw_html = client.get("/raw").text

    assert "Ledger insights" in sessions_html
    assert "Attributed usage" in sessions_html
    assert "Unknown / unattributed usage" in sessions_html
    assert "Top blind spots" in sessions_html
    assert "Top next actions" in sessions_html
    assert "Raw diagnostic context bridge" not in overview_html
    assert "Raw diagnostic context bridge" not in sessions_html
    assert "Raw diagnostic context bridge" in raw_html


def test_local_dashboard_import_uses_store_pricing_snapshot(tmp_path, monkeypatch):
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
    client = TestClient(create_local_api_app(store_dir=store_root, usage_discovery=_usage_discovery_for_home(home)))

    response = run_dashboard_refresh(client)

    assert response.status_code == 303
    assert "priced=2" in refresh_flash_qs(response)
    page = client.get(response.headers["location"])
    raw_page = client.get("/raw")
    assert "2 received list-price estimates" in page.text
    # Historical fixture timestamps sit outside the default 30-day window.
    assert "openai / gpt-dashboard-price" in client.get("/tokens?days=all").text
    assert "gpt-dashboard-price" in raw_page.text


def test_pricing_middleware_honors_legacy_env_pin_over_store_snapshot(tmp_path, monkeypatch):
    # A pre-rename user pins a catalog via AGENT_SENTINEL_PRICING_CATALOG_PATH
    # (the alias contract accepts the old name forever). The per-request store
    # snapshot activation must treat that legacy pin exactly like the new
    # name: never shadow it with the store snapshot.
    from agent_chronicle.cost import LEGACY_PRICING_CATALOG_PATH_ENV, PRICING_CATALOG_PATH_ENV
    from agent_chronicle.env_compat import read_env_alias

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


def test_local_dashboard_labels_codex_internal_review_as_usage_overhead(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _make_home_usage_sources(home)
    _add_codex_internal_review_source(home)
    store_root = tmp_path / "state"
    client = TestClient(create_local_api_app(store_dir=store_root, usage_discovery=_usage_discovery_for_home(home)))

    response = run_dashboard_refresh(client)

    assert response.status_code == 303
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

    page = client.get(response.headers["location"])
    raw_page = client.get("/?tab=raw")

    assert page.status_code == 200
    # Raw-tab session cells render short labels, never full ids. codex labels
    # keep the LAST 8 chars (UUIDv7 tail rule); the two codex fixtures and the
    # hermes fixture all share the "-session" tail, so each carries the
    # ledger's deterministic ~hash4 collision suffix.
    from hashlib import sha256 as _sha256

    codex_label = "-session~" + _sha256(b"codex::codex-session").hexdigest()[:4]
    review_label = "-session~" + _sha256(b"codex::codex-review-session").hexdigest()[:4]
    assert f"<code>{codex_label}</code>" in raw_page.text
    assert f"<code>{review_label}</code>" in raw_page.text
    assert "codex-session" not in raw_page.text
    assert "codex-review-session" not in raw_page.text
    assert "auto-review" in raw_page.text
    assert "codex-auto-review" not in raw_page.text


def _codex_only_dashboard_client(tmp_path, monkeypatch, *, model="gpt-5.6-sol"):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _make_codex_only_home(home, model=model)
    store_root = tmp_path / "state"
    client = TestClient(create_local_api_app(store_dir=store_root, usage_discovery=_usage_discovery_for_home(home)))
    return client, store_root


def _stored_usage_rows(store_root):
    rows = []
    for line in (store_root / "events.jsonl").read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("event_type") == "model_usage":
            rows.append(event)
    return rows


def test_dashboard_refresh_reprices_unknown_cost_rows_when_catalog_resolves(tmp_path, monkeypatch):
    """Dashboard mirror of the CLI reprice: the refresh button (which always
    prices) replaces an unknown-cost row once its model resolves in the
    store-local pricing snapshot — the unknown→priced transition only."""

    client, store_root = _codex_only_dashboard_client(tmp_path, monkeypatch)

    first = run_dashboard_refresh(client)
    assert first.status_code == 303
    assert "repriced=0" in refresh_flash_qs(first)
    unknown_row = _stored_usage_rows(store_root)[0]
    assert unknown_row["cost_confidence"] == "unknown"

    catalog_path = default_pricing_catalog_snapshot_path(store_root)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(
        json.dumps(
            {
                "gpt-5.6-sol": {
                    "litellm_provider": "openai",
                    "input_cost_per_token": 0.000005,
                    "output_cost_per_token": 0.00003,
                }
            }
        ),
        encoding="utf-8",
    )

    second = run_dashboard_refresh(client)
    assert second.status_code == 303
    assert "repriced=1" in refresh_flash_qs(second)
    repriced_row = _stored_usage_rows(store_root)[0]
    assert repriced_row["cost_confidence"] == "estimated_from_tokens"
    assert repriced_row["metadata"]["pricing_source"] == "litellm_model_cost_map"
    assert repriced_row["event_id"] != unknown_row["event_id"]

    # Reissued once: the now-priced row is stable on the next refresh.
    third = run_dashboard_refresh(client)
    assert "repriced=0" in refresh_flash_qs(third)
    assert _stored_usage_rows(store_root)[0]["event_id"] == repriced_row["event_id"]


def test_dashboard_refresh_auto_fetches_snapshot_unless_user_pinned(tmp_path, monkeypatch):
    """TTL auto-refresh on the dashboard refresh path: with no snapshot on
    disk the POST fetches the (mocked) LiteLLM table, writes the store-local
    snapshot, and prices the session in the same request. A USER env pin
    blocks the fetch — but the serve middleware's own per-request pin of the
    store snapshot must NOT."""

    import httpx

    monkeypatch.setenv("AGENT_CHRONICLE_PRICING_AUTO_REFRESH", "1")
    calls = []

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "gpt-5.6-sol": {
                    "litellm_provider": "openai",
                    "input_cost_per_token": 0.000005,
                    "output_cost_per_token": 0.00003,
                }
            }

    def _fake_get(url, **kwargs):
        calls.append(url)
        return _FakeResponse()

    monkeypatch.setattr(httpx, "get", _fake_get)
    client, store_root = _codex_only_dashboard_client(tmp_path, monkeypatch)

    response = run_dashboard_refresh(client)

    assert response.status_code == 303
    assert len(calls) == 1
    assert "priced=1" in refresh_flash_qs(response)
    assert default_pricing_catalog_snapshot_path(store_root).exists()
    row = _stored_usage_rows(store_root)[0]
    assert row["cost_confidence"] == "estimated_from_tokens"
    assert row["metadata"]["pricing_source"] == "litellm_model_cost_map"

    # Within the TTL the next refresh does not fetch again (the middleware's
    # own store-snapshot pin never blocks; the sidecar freshness does).
    run_dashboard_refresh(client)
    assert len(calls) == 1


def test_dashboard_refresh_never_fetches_over_a_user_pinned_catalog(tmp_path, monkeypatch):
    import httpx

    from agent_chronicle.cost import PRICING_CATALOG_PATH_ENV

    monkeypatch.setenv("AGENT_CHRONICLE_PRICING_AUTO_REFRESH", "1")

    def _exploding_get(url, **kwargs):
        raise AssertionError("auto-refresh must never fetch over a user pin")

    monkeypatch.setattr(httpx, "get", _exploding_get)
    custom_catalog = tmp_path / "custom-catalog.json"
    custom_catalog.write_text(
        json.dumps({"pricing": [{"provider": "codex", "model": "gpt-5.6-sol", "input_cost_per_1m": 1.0, "output_cost_per_1m": 2.0}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv(PRICING_CATALOG_PATH_ENV, str(custom_catalog))
    client, store_root = _codex_only_dashboard_client(tmp_path, monkeypatch)

    response = run_dashboard_refresh(client)

    assert response.status_code == 303
    # No store snapshot was fetched/written; the user's catalog priced the row.
    assert not default_pricing_catalog_snapshot_path(store_root).exists()
    row = _stored_usage_rows(store_root)[0]
    assert row["cost_confidence"] == "estimated_from_tokens"


def test_local_dashboard_refresh_replaces_stale_local_usage_without_deleting_notes(tmp_path, monkeypatch):
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
    client = TestClient(create_local_api_app(store_dir=store_root, usage_discovery=_usage_discovery_for_home(home)))

    response = run_dashboard_refresh(client)

    assert response.status_code == 303
    summary = client.get("/events/summary?limit=20").json()["summary"]
    assert summary["event_count"] == 4
    assert summary["note_count"] == 1
    assert summary["estimated_cost_usd"] < 999.0
    assert summary["by_source"]["manual"] == 1
    assert summary["tokens_by_provider"]["codex"]["estimated_input_tokens"] == 200


def test_local_dashboard_accepts_sort_controls(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _make_home_usage_sources(home)
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state", usage_discovery=_usage_discovery_for_home(home)))

    response = client.get("/?tools_sort=agent&cost_sort=input&activity_sort=oldest")

    assert response.status_code == 200
    # The legacy tab URL redirects to /raw with the sort params intact.
    html = client.get("/?tools_sort=agent&cost_sort=input&activity_sort=oldest&tab=raw").text
    assert "tools_sort=agent" in html
    assert "cost_sort=input" in html
    assert "activity_sort=oldest" in html
    assert 'class="sort-link active" href="/raw?tools_sort=agent' in html
    assert "Cost and provider breakdown" in html


def test_local_dashboard_handles_legacy_oversized_event_costs(tmp_path):
    store_root = tmp_path / "state"
    store_root.mkdir(parents=True)
    with (store_root / "events.jsonl").open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "event_id": "evt_oversized_cost",
                    "created_at": 1,
                    "source": "legacy",
                    "event_type": "model_usage",
                    "estimated_cost_usd": 10**400,
                    "metadata": {},
                }
            )
            + "\n"
        )

    response = TestClient(create_local_api_app(store_dir=store_root)).get("/?tab=raw")

    assert response.status_code == 200
    assert "usage diagnostic" in response.text
    assert "Activity log" in response.text


def test_local_api_rejects_server_owned_event_fields_from_callers(tmp_path):
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))

    response = client.post(
        "/events",
        json={"source": "x", "event_type": "y", "event_id": "caller", "created_at": "not-a-number"},
    )

    assert response.status_code == 422
    assert client.get("/events").status_code == 200
    assert client.get("/").status_code == 200


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
    assert event["metadata"]["summary"] == "[REDACTED]"
    assert secretish not in (tmp_path / "state" / "events.jsonl").read_text(encoding="utf-8")


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
    dashboard = client.get("/")

    assert bad.status_code == 422
    assert good.status_code == 200
    assert len(good.json()["events"]) == 1
    assert dashboard.status_code == 200


def test_local_dashboard_renders_runs_events_and_cost_without_secrets(tmp_path):
    store_root, result = _make_run(tmp_path)
    client = TestClient(create_local_api_app(store_dir=store_root))
    client.post(
        "/events",
        json={
            "source": "codex",
            "event_type": "model_usage",
            "run_id": result.run_id,
            "provider": "openai",
            "model": "gpt-4o-mini",
            "estimated_cost_usd": 0.000123,
            "estimated_input_tokens": 20,
            "estimated_output_tokens": 5,
            "metadata": {"authorization": "fake-auth-header-for-redaction-test", "cached_input_tokens": 7, "reasoning_output_tokens": 3},
        },
    )

    page = client.get("/?tab=raw")

    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    html = page.text
    assert "agentacct" in html
    assert result.run_id in html
    assert "codex" in html
    assert "openai" in html
    assert "$0.00" in html
    assert "Usage truth total" not in html
    assert "usage diagnostic" in html
    assert "20 input / 5 output" in html
    assert "Imported usage by provider" in html
    assert "Total incl. cached" in html
    assert ">0<" in html
    assert "Saved AI usage" in html
    assert "agentacct event log" in html
    assert "fake-auth-header-for-redaction-test" not in html
    assert "authorization" not in html.lower()


def test_local_dashboard_aggregates_usage_reported_through_mcp(tmp_path):
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
                    "name": "sentinel_record_event",
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
    page = client.get("/?tab=raw")

    assert summary["event_count"] == 2
    assert summary["estimated_input_tokens"] == 0
    assert summary["estimated_output_tokens"] == 0
    assert summary["estimated_cost_usd"] == 0.0
    assert summary["by_source"] == {"claude-code": 1, "codex": 1}
    assert summary["by_provider"] == {"agent-cli": 2}
    assert page.status_code == 200
    assert "Activity log" in page.text
    assert "Usage truth total" not in page.text
    assert "usage diagnostic" in page.text
    assert "30 input / 7 output" in page.text
    assert "20 input / 5 output" in page.text
    assert "claude-code" in page.text
    assert "codex" in page.text
    assert "agent-cli" in page.text


def test_local_dashboard_shows_usage_context_bridge(tmp_path):
    store_root, result = _make_run(tmp_path)
    server = SentinelMCPServer(store_dir=store_root)
    usage_event = _record_trusted_usage(store_root, run_id=result.run_id, session="codex-session", input_tokens=100, output_tokens=20, cached_input_tokens=30)
    section_response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "sentinel_record_section",
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
                "name": "sentinel_record_agent_usage_debug",
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
    page = client.get("/")
    sessions_page = client.get("/sessions", headers={"Accept": "text/html"})
    raw_page = client.get("/raw")

    assert summary["usage_context_bridge"]["linked_usage_records"] == 1
    assert summary["usage_context_bridge"]["context_matched_usage_records"] == 1
    assert summary["usage_context_bridge"]["attributed_usage_records"] == 1
    assert summary["usage_context_bridge"]["links"][0]["section_count"] == 1
    assert summary["usage_context_bridge"]["links"][0]["usage_debug_count"] == 1
    assert page.status_code == 200
    assert sessions_page.status_code == 200
    assert raw_page.status_code == 200
    assert 'body class="page-overview"' in page.text
    assert 'body class="page-raw"' in raw_page.text
    assert 'class="tab-link active" href="/" aria-current="page">Work</a>' in page.text
    # /raw is an Advanced-group forensic page; Advanced is demoted from the
    # primary nav, so no primary tab is active there.
    raw_nav = raw_page.text[raw_page.text.index('<nav class="tabs"') : raw_page.text.index("</nav>")]
    assert raw_nav.count('class="tab-link active"') == 0
    assert "Reconciliation" in sessions_page.text
    assert "Usage truth" in sessions_page.text
    assert "MCP work" in sessions_page.text
    assert "Evidence" in sessions_page.text
    assert "Attribution" in sessions_page.text
    assert "Action" in sessions_page.text
    assert "Attributed" in sessions_page.text
    assert "Raw diagnostic context bridge" not in page.text
    assert "Raw diagnostic context bridge" not in sessions_page.text
    assert "Raw usage log" not in page.text
    assert "Raw usage log" not in sessions_page.text
    assert "Raw diagnostic context bridge" in raw_page.text
    assert "Product reconciliation above uses the work ledger join inspector" in raw_page.text
    assert "Raw usage log" in raw_page.text
    assert "Join evidence" in raw_page.text
    assert "MCP semantics" in raw_page.text
    assert "No unlinked MCP context events." in raw_page.text
    assert "Context bridge" in raw_page.text
    assert "exact" in raw_page.text
    assert "unavailable" in raw_page.text


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
                    "src/agent_chronicle/api.py",
                    "src\\agent_chronicle\\cli.py",
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
    assert payload["files"] == ["src/agent_chronicle/api.py", "src/agent_chronicle/cli.py"]
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
    html = client.get("/?tab=raw").text

    assert usage_event["metadata"]["usage_provenance"] == "agent_sentinel_local_usage_import"
    assert usage_entry["time"] == 1_782_036_000
    assert "2026-06-21" in html


def test_local_dashboard_overhaul_surfaces_work_timeline_and_trust(tmp_path):
    store_root, result = _make_run(tmp_path)
    client = TestClient(create_local_api_app(store_dir=store_root))
    usage = _record_trusted_usage(store_root, run_id=result.run_id, session="codex-ui-session", input_tokens=40, output_tokens=10, cached_input_tokens=5)
    completed = client.post(
        "/events",
        json={
            "source": "codex",
            "event_type": "section_completed",
            "run_id": result.run_id,
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "ui-overhaul",
                "section_status": "completed",
                "section_title": "Dashboard overhaul",
                "summary": "Reworked the local dashboard information architecture.",
                "client": "codex",
                "client_session_id": "codex-ui-session",
                "files": ["src/agent_chronicle/api.py"],
            },
        },
    )
    blocked = client.post(
        "/events",
        json={
            "source": "codex",
            "event_type": "section_blocked",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "follow-up",
                "section_status": "blocked",
                "section_title": "Follow-up import",
                "summary": "Needs another local sample.",
                "client": "codex",
                "client_session_id": "codex-ui-session",
                "next_step": "Add a fresh local import fixture.",
            },
        },
    )
    check = client.post(
        "/events",
        json={
            "source": "codex",
            "event_type": "machine_check",
            "run_id": result.run_id,
            "metadata": {"name": "pytest", "exit_code": 0, "summary": "Targeted dashboard tests passed with Usage context bridge debug coverage."},
        },
    )
    unlinked_check = client.post(
        "/events",
        json={
            "source": "codex",
            "event_type": "machine_check",
            "run_id": "run_unlinked_check",
            "metadata": {"name": "lint", "exit_code": 1, "summary": "Lint failed before linking to a section with Raw usage log details."},
        },
    )
    assert usage["metadata"]["usage_provenance"] == "agent_sentinel_local_usage_import"
    assert completed.status_code == 200
    assert blocked.status_code == 200
    assert check.status_code == 200
    assert unlinked_check.status_code == 200

    overview_html = client.get("/").text
    tokens_html = client.get("/tokens").text
    html = client.get("/sessions", headers={"Accept": "text/html"}).text
    raw_html = client.get("/raw").text

    # 3.5a layout: the Sessions page leads with Sessions and Work items
    # sections; /tokens is the 3.5b explorer (fresh-first tables + the
    # store-wide confidence tables at the bottom).
    assert 'id="sessions"' in html
    assert 'id="work-items"' in html
    assert "Usage basics" in tokens_html
    assert "By platform" in tokens_html
    assert "By model" in tokens_html
    assert "By period" in tokens_html
    assert "include cache reads at full weight" not in tokens_html
    assert "Usage confidence" in tokens_html
    assert "Cost confidence" in tokens_html
    assert "Reconciliation" in html
    assert "Usage truth" in html
    assert "MCP work" in html
    assert "Evidence" in html
    assert "Attribution" in html
    assert "Action" in html
    assert 'body class="page-overview"' in overview_html
    assert 'body class="page-sessions"' in html
    assert 'class="tab-link active" href="/" aria-current="page">Work</a>' in html
    # Advanced is demoted from the primary nav to the footer.
    sessions_nav = html[html.index('<nav class="tabs"') : html.index("</nav>")]
    assert ">Advanced</a>" not in sessions_nav
    assert '<span class="footer-links">' in html
    for product_html in (tokens_html, html):
        assert "Raw Data / Debug" not in product_html
        assert "Work Timeline" not in product_html
        assert "Agents / Clients" not in product_html
        assert "Raw diagnostic context bridge" not in product_html
        assert "Raw usage log" not in product_html
        assert "Targeted dashboard tests passed with Usage context bridge debug coverage." not in product_html
        assert "Lint failed before linking to a section with Raw usage log details." not in product_html
    assert "Raw Data / Debug" not in overview_html
    assert "Work Timeline" not in overview_html
    assert "Agents / Clients" not in overview_html
    assert "Raw diagnostic context bridge" not in overview_html
    assert "Targeted dashboard tests passed with Usage context bridge debug coverage." not in overview_html
    # Open failed checks now remain visible even when agentacct cannot safely
    # assign them to a Task. Their agent-authored summary is product evidence,
    # while the raw tables and successful unlinked diagnostics stay Advanced-only.
    assert "Unassigned agent finding" in overview_html
    assert "Lint failed before linking to a section with Raw usage log details." in overview_html
    assert "Dashboard overhaul" in html
    assert "Dashboard overhaul" in overview_html
    assert "Follow-up import" in html
    assert "Ambiguous" in html
    assert "Usage unknown / not attributed" in html
    # Work items without usage render in #work-items (the old reconciliation
    # prelude rows moved there); honesty language survives.
    assert "Work items" in html
    assert "Multiple sections share this client session; agentacct does not allocate section-level usage." in html
    assert "Machine check passed" in html
    assert "Add a fresh local import fixture." in html
    assert "Raw Data / Debug" in raw_html
    assert "Work Timeline" in raw_html
    assert "Agents / Clients" in raw_html
    assert "Raw diagnostic context bridge" in raw_html
    assert "Raw usage log" in raw_html
    assert "Targeted dashboard tests passed with Usage context bridge debug coverage." in raw_html
    assert "Lint failed before linking to a section with Raw usage log details." in raw_html
    assert "log_import" in raw_html
    assert "mcp_agent_reported" in raw_html
    assert "provider_billed" in raw_html
    assert "client_reported" in tokens_html
    assert "estimated_from_tokens" in tokens_html
    assert "estimated_from_tokens" in html
    assert "unknown" in raw_html


def test_local_dashboard_includes_cost_proxy_events_separately(tmp_path):
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

    page = client.get("/?tab=raw")

    assert page.status_code == 200
    html = page.text
    assert "Activity log" in html
    assert "API budget decisions" in html
    assert "API budget decisions" in html
    assert "openrouter" in html
    assert "openai/gpt-4o-mini" in html
    assert "$0.00" in html


def test_dashboard_labels_cost_proxy_total_as_estimated_requested_cost(tmp_path):
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
            forwarded=False,
        ),
        run_id=result.run_id,
        decision="blocked",
        reason="budget exceeded",
    )

    html = TestClient(create_local_api_app(store_dir=store_root)).get("/?tab=raw").text

    assert "API budget decisions" in html
    assert "Estimated requested cost" not in html


def test_dashboard_escapes_html_from_events_and_cost_events(tmp_path):
    store_root, result = _make_run(tmp_path)
    client = TestClient(create_local_api_app(store_dir=store_root))
    client.post(
        "/events",
        json={"source": "<script>alert(1)</script>", "event_type": "task", "metadata": {"summary": "<b>bold</b>"}},
    )
    CostLedger(store_root).record_usage(
        UsageEstimate(
            provider="<script>bad</script>",
            model="<img src=x onerror=alert(1)>",
            endpoint="/openrouter/v1/chat/completions",
            estimated_input_tokens=1,
            estimated_output_tokens=1,
            estimated_cost_usd=0.000001,
        ),
        run_id=result.run_id,
        decision="allowed",
    )

    html = client.get("/?tab=raw").text

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x onerror=alert(1)>" not in html


def test_top_level_serve_refuses_non_local_bind(tmp_path):
    result = CliRunner().invoke(app, ["serve", "--host", "0.0.0.0", "--store-dir", str(tmp_path / "state")])

    assert result.exit_code == 1
    assert "Refusing non-local bind" in result.output


def test_top_level_serve_prints_local_usage_scan_notice(tmp_path, monkeypatch):
    calls = []

    def fake_run(api_app, *, host, port, log_level):
        calls.append({"api_app": api_app, "host": host, "port": port, "log_level": log_level})

    monkeypatch.setattr("uvicorn.run", fake_run)

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


def test_dashboard_import_replaces_legacy_alias_rows_once(tmp_path, monkeypatch):
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
    client = TestClient(create_local_api_app(store_dir=store_root, usage_discovery=_usage_discovery_for_home(home)))

    response = run_dashboard_refresh(client)

    assert response.status_code == 303
    assert "migrated=2" in refresh_flash_qs(response)
    page = client.get(response.headers["location"])
    assert page.status_code == 200
    assert "replaced legacy per-model session keys" in page.text
    stored = [json.loads(line) for line in (store_root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
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


def test_dashboard_import_refreshes_matching_lane_and_appends_new_lane(tmp_path, monkeypatch):
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
    client = TestClient(create_local_api_app(store_dir=store_root, usage_discovery=_usage_discovery_for_home(home)))

    response = run_dashboard_refresh(client)

    assert response.status_code == 303
    assert "migrated=0" in refresh_flash_qs(response)
    stored = [json.loads(line) for line in (store_root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
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
    surfaces and the dashboard still renders with them present."""

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

    # Dashboard renders with the new ledger keys present (no template change
    # in Batch A; rendering the new data is Batch B/C).
    assert client.get("/").status_code == 200
    assert client.get("/?tab=raw").status_code == 200
