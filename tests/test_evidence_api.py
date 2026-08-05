from __future__ import annotations

from fastapi.testclient import TestClient

from agentacct.api import create_local_api_app
from agentacct.service import SentinelService


def test_work_event_http_transport_writes_v1_and_v2(tmp_path) -> None:
    store = tmp_path / "state"
    client = TestClient(create_local_api_app(store_dir=store))

    request = {
        "source": "codex",
        "event_kind": "section",
        "status": "completed",
        "occurred_at": 1_783_900_123.5,
        "source_event_id": "codex-section-implementation-1",
        "run_id": "run-1",
        "section_id": "implementation",
        "client": "codex",
        "client_session_id": "session-1",
        "summary": "Implemented Evidence v2.",
        "files": ["src/agentacct/evidence.py", "../private.txt"],
    }
    response = client.post("/work-events", json=request)
    retry = client.post("/work-events", json=request)

    assert response.status_code == 200, response.text
    assert retry.status_code == 200, retry.text
    payload = response.json()
    assert payload["work_event"]["transport"] == "http"
    assert payload["work_event"]["occurred_at"] == 1_783_900_123.5
    assert payload["work_event"]["source_event_id"] == "codex-section-implementation-1"
    assert payload["work_event"]["files"] == ["src/agentacct/evidence.py"]
    assert payload["v1_event"]["event_type"] == "section_completed"
    assert payload["v1_event"]["metadata"]["client_event_timestamp"] == 1_783_900_123.5
    assert payload["v1_event"]["metadata"]["source_event_id"] == "codex-section-implementation-1"
    assert retry.json()["v1_event"]["event_id"] == payload["v1_event"]["event_id"]
    v1_events = SentinelService(store).list_all_events()
    assert len(v1_events) == 1
    assert (store / "evidence-v2" / "spool.jsonl").is_file()

    evidence = client.get("/evidence/events").json()["evidence"]
    assert len(evidence) == 1
    assert evidence[0]["receipt_count"] == 2
    assert evidence[0]["duplicate_receipt_count"] == 1
    assert evidence[0]["envelope"]["source_instance"] == "v1-http"
    assert evidence[0]["envelope"]["assertion"] == "claimed"


def test_evidence_json_surfaces_have_projection_parity(tmp_path) -> None:
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))
    client.post(
        "/work-events",
        json={
            "source": "claude-code",
            "event_kind": "task",
            "status": "started",
            "run_id": "run-1",
            "work_id": "work-1",
            "client_session_id": "session-1",
        },
    )

    product = client.get("/evidence/product").json()
    assert product["summary"]["evidence_count"] == 1
    assert client.get("/evidence/work-graph").json()["work_graph"] == product["work_graph"]
    assert client.get("/evidence/matrix").json()["evidence_matrix"] == product["evidence_matrix"]
    assert client.get("/evidence/discrepancies").json()["discrepancies"] == product["discrepancies"]
    assert client.get("/evidence/cost-outcome-basis").json()["cost_outcome_basis"] == product["cost_outcome_basis"]
    assert client.get("/evidence/status").json()["stats"]["evidence_versions"] == 1

    evidence_id = client.get("/evidence/events").json()["evidence"][0]["envelope"]["evidence_id"]
    assert client.get(f"/evidence/events/{evidence_id}").status_code == 200
    assert client.get("/evidence/events/evd_missing").status_code == 404


def test_evidence_claimed_links_route_contract(tmp_path) -> None:
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))
    response = client.post(
        "/work-events",
        json={
            "source": "codex",
            "event_kind": "section",
            "status": "completed",
            "section_id": "phase-5",
            "client_session_id": "session-1",
        },
    )
    assert response.status_code == 200, response.text

    links = client.get("/evidence/claimed-links")
    assert links.status_code == 200, links.text
    assert links.json() == {"claimed_links": []}

    for state in ("pending", "valid", "invalid"):
        filtered = client.get("/evidence/claimed-links", params={"validation_state": state})
        assert filtered.status_code == 200, filtered.text
        assert filtered.json()["claimed_links"] == []

    assert client.get("/evidence/claimed-links?validation_state=bogus").status_code == 422
    assert client.get("/evidence/claimed-links?limit=0").status_code == 422
    assert client.get("/evidence/claimed-links?limit=1001").status_code == 422


def test_work_event_validation_cannot_mint_unknown_transport_or_kind(tmp_path) -> None:
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))

    response = client.post(
        "/work-events",
        json={
            "source": "custom",
            "event_kind": "provider_invoice",
            "status": "completed",
            "transport": "provider_billed",
        },
    )

    assert response.status_code == 422
    assert not (tmp_path / "state" / "events.jsonl").exists()


def test_evidence_kill_switch_leaves_v1_endpoint_working(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_CHRONICLE_EVIDENCE_V2", "0")
    store = tmp_path / "state"
    client = TestClient(create_local_api_app(store_dir=store))

    response = client.post(
        "/events",
        json={"source": "custom", "event_type": "task_started", "metadata": {}},
    )

    assert response.status_code == 200
    assert SentinelService(store).list_all_events()
    assert not (store / "evidence-v2").exists()
    assert client.get("/evidence/status").json()["enabled"] is False


def test_evidence_events_uses_stable_arrival_cursor_pagination(tmp_path) -> None:
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))
    for index in range(5):
        response = client.post(
            "/work-events",
            json={
                "source": "codex",
                "event_kind": "section",
                "status": "completed",
                "source_event_id": f"phase-7-section-{index}",
                "section_id": f"section-{index}",
                "client_session_id": "session-pagination",
            },
        )
        assert response.status_code == 200, response.text

    first = client.get("/evidence/events?limit=2")
    assert first.status_code == 200, first.text
    first_payload = first.json()
    assert first_payload["count"] == 2
    assert first_payload["page"] == {
        "limit": 2,
        "returned": 2,
        "has_more": True,
        "next_cursor": first_payload["evidence"][-1]["first_receipt_sequence"],
    }

    second = client.get(
        "/evidence/events",
        params={"limit": 2, "cursor": first_payload["page"]["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    second_payload = second.json()
    assert second_payload["page"]["has_more"] is True

    third = client.get(
        "/evidence/events",
        params={"limit": 2, "cursor": second_payload["page"]["next_cursor"]},
    )
    assert third.status_code == 200, third.text
    third_payload = third.json()
    assert third_payload["count"] == 1
    assert third_payload["page"] == {
        "limit": 2,
        "returned": 1,
        "has_more": False,
        "next_cursor": None,
    }

    pages = [first_payload, second_payload, third_payload]
    sequences = [
        row["first_receipt_sequence"]
        for payload in pages
        for row in payload["evidence"]
    ]
    assert sequences == sorted(sequences, reverse=True)
    assert len(sequences) == len(set(sequences)) == 5


def test_evidence_events_cursor_keeps_filters_and_supports_source_system(tmp_path) -> None:
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))
    for index, source in enumerate(("codex", "claude-code", "codex", "claude-code", "codex")):
        response = client.post(
            "/work-events",
            json={
                "source": source,
                "event_kind": "section",
                "status": "completed",
                "source_event_id": f"filter-section-{index}",
                "section_id": f"filter-{index}",
            },
        )
        assert response.status_code == 200, response.text

    first_payload = client.get(
        "/evidence/events",
        params={
            "limit": 2,
            "source_type": "mcp_agent_reported",
            "source_system": "codex",
            "dimension": "task_semantics",
            "assertion": "claimed",
        },
    ).json()
    assert first_payload["count"] == 2
    assert first_payload["page"]["has_more"] is True

    second_response = client.get(
        "/evidence/events",
        params={
            "limit": 2,
            "cursor": first_payload["page"]["next_cursor"],
            "source_type": "mcp_agent_reported",
            "source_system": "codex",
            "dimension": "task_semantics",
            "assertion": "claimed",
        },
    )
    assert second_response.status_code == 200, second_response.text
    rows = first_payload["evidence"] + second_response.json()["evidence"]
    assert len(rows) == 3
    assert {row["envelope"]["source_system"] for row in rows} == {"codex"}
    assert all("task_semantics" in row["envelope"]["dimensions"] for row in rows)


def test_evidence_events_rejects_out_of_contract_page_inputs(tmp_path) -> None:
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))

    assert client.get("/evidence/events?limit=101").status_code == 422
    assert client.get("/evidence/events?cursor=0").status_code == 422
    assert client.get("/evidence/events?cursor=not-a-number").status_code == 422

    default_page = client.get("/evidence/events").json()["page"]
    assert default_page == {
        "limit": 50,
        "returned": 0,
        "has_more": False,
        "next_cursor": None,
    }
