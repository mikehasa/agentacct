from __future__ import annotations

from fastapi.testclient import TestClient

from agentacct.api import create_local_api_app


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
    assert (store / "events.jsonl").is_file()
    assert len((store / "events.jsonl").read_text(encoding="utf-8").splitlines()) == 1
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


def test_advanced_hub_owns_stable_evidence_and_raw_routes(tmp_path) -> None:
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))
    client.post(
        "/work-events",
        json={
            "source": "codex",
            "event_kind": "section",
            "status": "completed",
            "section_id": "phase-5",
            "client_session_id": "session-1",
        },
    )

    advanced = client.get("/advanced", headers={"accept": "text/html"})
    assert advanced.status_code == 200
    assert "Inspect only when you need to" in advanced.text
    assert "Preview evidence" in advanced.text
    assert "Preview sources" in advanced.text
    assert "Latest 1" in advanced.text
    assert "bounded HTML preview · cap 2,000 records" in advanced.text
    assert "Indexed evidence" not in advanced.text
    assert "Evidence projections" in advanced.text
    assert "Forensic inspectors" in advanced.text
    for href in (
        "/raw",
        "/work-graph",
        "/evidence-matrix",
        "/discrepancies",
        "/cost-outcome-basis",
        "/evidence/events",
        "/evidence/claimed-links",
        "/evidence/status",
        "/evidence/product",
    ):
        assert f'href="{href}"' in advanced.text, href
    assert '<a class="button secondary button-link" href="/raw">Preview local logs</a>' in advanced.text

    expected = {
        "/work-graph": "Work Graph",
        "/evidence-matrix": "Evidence Matrix",
        "/discrepancies": "Discrepancies",
        "/cost-outcome-basis": "Cost &amp; Outcome Basis",
    }
    for path, title in expected.items():
        response = client.get(path, headers={"accept": "text/html"})
        assert response.status_code == 200
        assert title in response.text
        assert "Evidence v2" in response.text
        assert "Indexed evidence" in response.text
        assert "Preview evidence" not in response.text
        assert "Preview sources" not in response.text
        assert "Content-Security-Policy" in response.headers
        # Advanced is demoted out of the primary nav (Work + Usage only); its
        # routes stay reachable via the "Advanced home" button + footer link.
        assert response.text.count('<a class="tab-link') == 2
        assert 'class="tab-link" href="/">Work</a>' in response.text
        assert 'class="tab-link" href="/tokens">Usage</a>' in response.text
        assert ">Control</a>" not in response.text[
            response.text.index('<nav class="tabs"') : response.text.index("</nav>")
        ]
        assert response.text.count('class="tab-link active"') == 0
        assert '<a class="button secondary button-link" href="/advanced">Advanced home</a>' in response.text
        assert "only explicitly provider-billed evidence is invoice-backed" in response.text
        assert "Costs shown anywhere are estimates" not in response.text


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
    assert (store / "events.jsonl").is_file()
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
