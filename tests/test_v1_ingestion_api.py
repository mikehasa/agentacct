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
