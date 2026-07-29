from __future__ import annotations

import json

from fastapi.testclient import TestClient

from agentacct.api import create_local_api_app


def test_capture_api_exposes_capabilities_and_render_only_manifest(tmp_path) -> None:
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))

    capabilities = client.get("/capture/capabilities").json()
    manifest = client.get("/capture/manifests/cursor").json()

    assert set(capabilities["capabilities"]) == {"claude-code", "codex", "cursor"}
    assert capabilities["privacy"]["captures_usage"] is False
    assert manifest["relative_path"] == ".cursor/hooks.json"
    assert manifest["written"] is False
    assert manifest["activation"] == "opt_in"


def test_capture_api_is_metadata_only_and_idempotent(tmp_path) -> None:
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))
    payload = {
        "hook_event_name": "Stop",
        "timestamp": "2026-07-13T02:03:04Z",
        "session_id": "codex-session-1",
        "turn_id": "turn-1",
        "prompt": "PROMPT_CANARY",
        "last_assistant_message": "RESPONSE_CANARY",
    }

    first = client.post("/capture/codex/Stop", json=payload)
    second = client.post("/capture/codex/Stop", json=payload)

    assert first.status_code == 200, first.text
    assert first.json()["stored_count"] == 1
    assert second.json()["stored_count"] == 1
    assert client.get("/evidence/status").json()["stats"]["duplicate_receipts"] == 1
    encoded = first.text + client.get("/evidence/events").text
    assert "PROMPT_CANARY" not in encoded
    assert "RESPONSE_CANARY" not in encoded


def test_capture_api_malformed_body_is_fail_open_and_body_is_bounded(tmp_path) -> None:
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))

    malformed = client.post("/capture/cursor/sessionStart", content=b"not-json SECRET_CANARY")
    oversized = client.post(
        "/capture/cursor/sessionStart",
        content=b"x" * (1_048_576 + 1),
    )

    assert malformed.status_code == 200
    assert malformed.json()["ignored_reason"] == "malformed_json"
    assert malformed.json()["fail_open"] is True
    assert "SECRET_CANARY" not in malformed.text
    assert oversized.status_code == 413
