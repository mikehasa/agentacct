"""Tests for GET /v1/ingestion — the bearer-gated twin of /ingestion/health.

The native shell's Sources surface needs source/watcher health on the /v1
lane it already authenticates against. Contract under test: the same
fail-closed auth as every /v1 route (no token config -> 503, bad token ->
401), a schema envelope the shell can pin, and a payload that is exactly the
ingestion-health snapshot the legacy route serves (one honesty source, two
doors — the lanes can never disagree).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agentacct.api import create_local_api_app
from agentacct.ingestion_health import V1_INGESTION_SCHEMA_VERSION

TOKEN = "test-v1-token"


def test_v1_ingestion_fails_closed_without_token_config(tmp_path):
    client = TestClient(create_local_api_app(store_dir=tmp_path))
    response = client.get("/v1/ingestion")
    assert response.status_code == 503


def test_v1_ingestion_rejects_bad_bearer(tmp_path):
    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    assert client.get("/v1/ingestion").status_code == 401
    assert (
        client.get("/v1/ingestion", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )


def test_v1_ingestion_matches_legacy_snapshot(tmp_path):
    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    response = client.get("/v1/ingestion", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == V1_INGESTION_SCHEMA_VERSION

    legacy = client.get("/ingestion/health").json()
    assert payload["ingestion"] == legacy


def test_v1_version_advertises_ingestion_schema(tmp_path):
    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    response = client.get("/v1/version", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.json()["ingestion_schema"] == V1_INGESTION_SCHEMA_VERSION


def test_v1_ingestion_with_usage_but_no_import_receipt_names_the_missing_history(tmp_path):
    # Honesty: a store that already holds usage rows but has no import receipt
    # must not claim agentacct never imported anything.
    import time

    from agentacct.service import SentinelService

    headers = {"Authorization": f"Bearer {TOKEN}"}
    fresh = TestClient(create_local_api_app(store_dir=tmp_path / "empty", v1_auth_token=TOKEN))
    assert fresh.get("/v1/ingestion", headers=headers).json()["ingestion"]["state_title"] == "No import recorded yet"

    store = tmp_path / "with-usage"
    now = time.time()
    SentinelService(store).record_event({
        "source": "codex-local-session-import",
        "event_type": "model_usage",
        "provider": "codex",
        "model": "gpt-5.5",
        "estimated_input_tokens": 100,
        "estimated_output_tokens": 25,
        "metadata": {
            "usage_source": "local_client_session_store",
            "client": "codex",
            "client_session_id": "s1",
            "client_session_kind": "root",
            "started_at": now - 60,
            "updated_at": now - 60,
        },
    }, trusted_usage_import=True)
    client = TestClient(create_local_api_app(store_dir=store, v1_auth_token=TOKEN))
    ingestion = client.get("/v1/ingestion", headers=headers).json()["ingestion"]
    assert ingestion["state"] == "unknown"
    assert ingestion["state_title"] == "Import history not recorded"
    assert "1 session" in ingestion["state_detail"]
    assert "has not completed an import" not in ingestion["state_detail"]
    # The legacy door serves the same honest copy.
    assert client.get("/ingestion/health").json() == ingestion
