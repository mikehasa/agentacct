"""Sessions rollup regressions (post-HTML retirement).

The Sessions explorer HTML page is retired: GET /sessions is JSON-only, and
the filter-pill / chip / row-rendering helpers are gone with it. What this
file pins now is the KEPT surface:

- the /sessions JSON contract: JSON for every Accept header, verbatim rollup
  plus the additive fields (duration_seconds, usage.turns_total), and the
  ``ignored_html_params`` wire honesty for the legacy HTML filter params;
- the rollup's join-state semantics (attributed / ambiguous / sections_only /
  unjoined) that the retired join filter used to assert through rendered rows;
- honest-absence data rules that used to be asserted through the row markup:
  a backwards client clock refuses the duration (None, never a guessed 0),
  and a sections-only session reports zero usage rows with a None cost —
  not a zero-cost claim;
- the /tasks projection dedup contract.

All stores are throwaway tmp_path stores (suite conftest guards the real
dogfood ledger)."""

import time

from fastapi.testclient import TestClient

from agentacct.api import create_local_api_app
from agentacct.service import SentinelService


def _trusted_usage(
    store_root,
    *,
    session,
    client="codex",
    input_tokens=100,
    output_tokens=25,
    cached_input_tokens=50,
    cost=0.01,
    model="gpt-5.5",
    started_at=None,
    updated_at=None,
    turn_count=None,
):
    metadata = {
        "usage_source": "local_client_session_store",
        "client": client,
        "client_session_id": session,
        "cached_input_tokens": cached_input_tokens,
        "cache_creation_tokens_reported": client != "codex",
        "cache_read_tokens_reported": True,
    }
    if started_at is not None:
        metadata["started_at"] = started_at
        metadata["updated_at"] = updated_at if updated_at is not None else started_at
    if turn_count is not None:
        metadata["turn_count"] = turn_count
    return SentinelService(store_root).record_event(
        {
            "source": f"{client}-local-session-import",
            "event_type": "model_usage",
            "provider": client,
            "model": model,
            "estimated_input_tokens": input_tokens,
            "estimated_output_tokens": output_tokens,
            "estimated_cost_usd": cost,
            "usage_confidence": "client_reported",
            "cost_confidence": "estimated_from_tokens",
            "metadata": metadata,
        },
        trusted_usage_import=True,
    )


def _record_section(store_root, *, section_id, title, session, client="codex", status="completed"):
    return SentinelService(store_root).record_event(
        {
            "source": client,
            "event_type": f"section_{status}",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": section_id,
                "section_status": status,
                "section_title": title,
                "client": client,
                "client_session_id": session,
            },
        }
    )


def _client(store_root):
    return TestClient(create_local_api_app(store_dir=store_root))


def _sessions_by_id(client):
    payload = client.get("/sessions").json()
    return {entry["client_session_id"]: entry for entry in payload["sessions"]}


# ---------------------------------------------------------------------------
# JSON-only contract: every Accept header gets the same JSON rollup
# ---------------------------------------------------------------------------


def test_sessions_is_json_only_for_every_accept_header(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="nego-matrix-sess", started_at=time.time() - 60)
    client = _client(store_root)

    baseline = client.get("/sessions", headers={"Accept": "application/json"}).json()

    browser = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    accepts = (
        # Former HTML lane: browsers and HTML-preferring clients now get JSON.
        browser,
        "text/html",
        "text/html;q=0.9,application/json;q=0.8",
        # Always-JSON lane, unchanged.
        "*/*",
        "application/json",
        "",  # absent header
        "application/json, text/html;q=0.5",
        "application/json, text/html;q=0",
        "text/html;q=0",
    )
    for accept in accepts:
        response = client.get("/sessions", headers={"Accept": accept})
        assert response.status_code == 200, accept
        assert response.headers["content-type"].startswith("application/json"), accept
        assert response.json() == baseline, accept  # one rollup, no negotiation


def test_sessions_json_names_the_html_filter_params_it_ignored(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="ignored-params-01", started_at=time.time() - 60)
    client = _client(store_root)

    plain = client.get("/sessions").json()
    assert "ignored_html_params" not in plain  # additive: absent when unused

    filtered = client.get("/sessions?client=claude-code&days=7&join=attributed").json()
    assert filtered["ignored_html_params"] == ["client", "days", "join"]
    without_marker = {key: value for key, value in filtered.items() if key != "ignored_html_params"}
    assert without_marker == plain  # the rollup itself is untouched


# ---------------------------------------------------------------------------
# Join-state semantics (formerly asserted through the join filter's rows)
# ---------------------------------------------------------------------------


def test_join_states_partition_the_rollup(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    # attributed: one section + usage sharing the exact session key.
    _trusted_usage(store_root, session="attributed-sess-1", started_at=now - 60)
    _record_section(store_root, section_id="a-work", title="Attributed work", session="attributed-sess-1")
    # ambiguous: two candidate sections share the session's context.
    _trusted_usage(store_root, session="ambiguous-sess-01", started_at=now - 70)
    _record_section(store_root, section_id="b-work-1", title="Candidate one", session="ambiguous-sess-01")
    _record_section(store_root, section_id="b-work-2", title="Candidate two", session="ambiguous-sess-01")
    # sections_only: sections recorded, no usage imported.
    _record_section(store_root, section_id="c-work", title="Sections only work", session="context-sess-001")
    # unjoined: usage only, no MCP context anywhere.
    _trusted_usage(store_root, session="unjoined-sess-991", started_at=now - 80)
    client = _client(store_root)

    payload = client.get("/sessions").json()
    assert payload["total_sessions"] == 4
    by_id = {entry["client_session_id"]: entry for entry in payload["sessions"]}
    assert by_id["attributed-sess-1"]["join"]["state"] == "attributed"
    assert by_id["ambiguous-sess-01"]["join"]["state"] == "ambiguous"
    assert by_id["context-sess-001"]["join"]["state"] == "sections_only"
    assert by_id["unjoined-sess-991"]["join"]["state"] == "unjoined"

    # The attributed entry carries the exact attributed rows and work link.
    attributed = by_id["attributed-sess-1"]["join"]
    assert attributed["row_states"] == {
        "attributed": 1,
        "ambiguous": 0,
        "context_matched_unallocated": 0,
        "unjoined": 0,
    }
    assert attributed["attributed_fresh_tokens"] == 125
    assert [work["section_id"] for work in attributed["attributed_work"]] == ["a-work"]


# ---------------------------------------------------------------------------
# Honest-absence data rules (formerly asserted through the row markup)
# ---------------------------------------------------------------------------


def test_backwards_clock_refuses_duration_and_missing_turns_stay_none(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    # Backwards client clock (started after updated): the ledger refuses the
    # span — duration_seconds must be None, never a guessed 0. No turn_count
    # recorded -> usage.turns_total is None too, never 0.
    _trusted_usage(
        store_root,
        session="backwards-sess-01",
        started_at=now,
        updated_at=now - 3600,
    )
    client = _client(store_root)

    entry = _sessions_by_id(client)["backwards-sess-01"]
    assert entry["duration_seconds"] is None
    assert entry["usage"]["turns_total"] is None
    # The rest of the entry's usage stays real data.
    assert entry["usage"]["fresh_tokens"] == 125
    assert entry["usage"]["estimated_cost_usd"] == 0.01


def test_sections_only_session_reports_no_usage_and_never_a_zero_cost_claim(tmp_path):
    store_root = tmp_path / "state"
    # Usage-only session: no MCP context -> unjoined, no work items to name.
    _trusted_usage(store_root, session="no-goal-sess-0001", started_at=time.time() - 60)
    # Sections-only session: work recorded, no usage rows imported — the
    # rollup must say "unknown", not claim zero cost.
    _record_section(store_root, section_id="ghost", title="Sections only work", session="sections-sess-001")
    client = _client(store_root)

    by_id = _sessions_by_id(client)

    usage_only = by_id["no-goal-sess-0001"]
    assert usage_only["join"]["state"] == "unjoined"
    assert usage_only["work"]["items"] == []

    sections_only = by_id["sections-sess-001"]
    assert sections_only["join"]["state"] == "sections_only"
    assert sections_only["usage"]["rows"] == 0
    assert sections_only["usage"]["fresh_tokens"] == 0
    # No imported rows matched: cost is UNKNOWN (None), not a $0.00 claim.
    assert sections_only["usage"]["estimated_cost_usd"] is None
    assert sections_only["usage"]["cost_confidence"] is None
    assert [item["title"] for item in sections_only["work"]["items"]] == ["Sections only work"]
    assert [item["section_id"] for item in sections_only["work"]["items"]] == ["ghost"]


def test_tasks_projection_deduplicates_a_session_with_named_work(tmp_path):
    # The HTML overview feed is retired; the deduplication contract lives in
    # the kept /tasks JSON projection — a session with named work is ONE task
    # (never a task card plus a separate session row's worth of double count).
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="preview-sess-0001", started_at=time.time() - 60, turn_count=3)
    _record_section(store_root, section_id="pv-work", title="Preview goal", session="preview-sess-0001")
    client = _client(store_root)

    task_payload = client.get("/tasks").json()
    assert task_payload["summary"]["task_count"] == 1
    assert task_payload["summary"]["session_count"] == 1
    assert task_payload["summary"]["associated_work_count"] == 1
    task = task_payload["tasks"][0]
    assert task["session_count"] == 1
    assert task["usage"]["fresh_tokens"] == 125
    assert [item["section_id"] for item in task["work_items"]] == ["pv-work"]


# ---------------------------------------------------------------------------
# JSON contract: verbatim rollup plus additive fields
# ---------------------------------------------------------------------------


def test_sessions_json_unchanged_plus_additive_and_ignores_html_filters(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    _trusted_usage(
        store_root, session="json-sess-000001", started_at=now - 7320, updated_at=now - 120, turn_count=4
    )
    _trusted_usage(store_root, session="json-sess-000002", client="claude-code", model="fable-5", started_at=now - 60)
    client = _client(store_root)

    payload = client.get("/sessions").json()
    assert payload["total_sessions"] == 2
    by_id = {entry["client_session_id"]: entry for entry in payload["sessions"]}
    timed = by_id["json-sess-000001"]
    # Additive fields ride on the verbatim rollup.
    assert timed["duration_seconds"] == 7200.0
    assert timed["usage"]["turns_total"] == 4
    untimed = by_id["json-sess-000002"]
    assert untimed["duration_seconds"] == 0.0
    assert untimed["usage"]["turns_total"] is None
    # Existing entry shape intact (spot pins on long-standing keys).
    for key in ("session_key", "client", "usage", "join", "work", "related", "instrumentation_state"):
        assert key in timed, key

    # The HTML filter params never filter the JSON surface: same rollup no
    # matter what filters ride on the URL — plus an additive wire-level
    # marker naming the ignored params (fix round, cluster G).
    filtered = client.get("/sessions?client=hermes&project=x&join=attributed&kind=roots&days=7").json()
    assert filtered["ignored_html_params"] == ["client", "days", "join", "kind", "project"]
    assert {key: value for key, value in filtered.items() if key != "ignored_html_params"} == payload
