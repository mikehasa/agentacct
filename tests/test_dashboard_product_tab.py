"""Phase 2 Batch B regressions, retargeted after the HTML display-layer
retirement: session rollup, work items, grouped attention, reconciliation,
redaction, and truncation honesty — asserted on the kept JSON surfaces
(/sessions, /work-items, /attention, /timeline, /overview). The claims are
the same product truths the old /sessions HTML explorer rendered; only the
assertion surface moved (the JSON payloads carry the exact same rollup).

All stores are throwaway tmp_path stores (suite conftest guards the real
dogfood ledger).

Deleted with the HTML layer (display-only claims with no JSON equivalent):
attributed-first row ordering (an HTML sort; the JSON rollup is served
verbatim and ``sort`` is an ignored wire param), the work-items
"Showing 50 of 51" cap note (the /work-items JSON has no total counter),
friendly empty-state copy, and HTML escaping of agent-authored titles.
"""

from fastapi.testclient import TestClient

from agentacct.api import create_local_api_app
from agentacct.service import SentinelService

ROOT_UUID = "f224d28b-1234-4abc-8def-0123456789ab"
CHILD_SESSION = f"{ROOT_UUID}:agent-abcdef1234"
OTHER_UUID = "9c8845a4-5678-4def-9abc-fedcba987654"


def _trusted_usage(
    store_root,
    *,
    session,
    client="codex",
    input_tokens=100,
    output_tokens=25,
    cached_input_tokens=50,
    cost=0.01,
    session_kind=None,
    parent_session=None,
    project_dir=None,
    model="gpt-5.5",
    started_at=None,
    cache_creation_reported=None,
    cache_read_reported=None,
):
    if cache_creation_reported is None:
        cache_creation_reported = client != "codex"
    if cache_read_reported is None:
        cache_read_reported = True
    metadata = {
        "usage_source": "local_client_session_store",
        "client": client,
        "client_session_id": session,
        "cached_input_tokens": cached_input_tokens,
        "cache_creation_tokens_reported": cache_creation_reported,
        "cache_read_tokens_reported": cache_read_reported,
    }
    if session_kind is not None:
        metadata["client_session_kind"] = session_kind
    if parent_session is not None:
        metadata["parent_client_session_id"] = parent_session
    if project_dir is not None:
        metadata["project_dir"] = project_dir
    if started_at is not None:
        metadata["started_at"] = started_at
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


def _api(store_root):
    return TestClient(create_local_api_app(store_dir=store_root))


def _sessions_payload(store_root, query=""):
    """The /sessions JSON rollup (the one sessions surface since the HTML
    retirement; the payload is ledger["session_rollup"] served verbatim)."""
    response = _api(store_root).get(f"/sessions{query}")
    assert response.status_code == 200
    return response.json()


def _entry(payload, session_id):
    matches = [entry for entry in payload["sessions"] if entry["client_session_id"] == session_id]
    assert len(matches) == 1, f"expected exactly one rollup entry for {session_id}"
    return matches[0]


# ---------------------------------------------------------------------------
# 2g: summary + session rollup entries
# ---------------------------------------------------------------------------


def test_session_rollup_groups_child_under_root_and_never_merges_usage(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(
        store_root,
        session=ROOT_UUID,
        client="claude-code",
        input_tokens=1000,
        output_tokens=200,
        cached_input_tokens=0,
        session_kind="root",
    )
    _trusted_usage(
        store_root,
        session=CHILD_SESSION,
        client="claude-code",
        input_tokens=300,
        output_tokens=70,
        cached_input_tokens=0,
        session_kind="child",
        parent_session=ROOT_UUID,
    )

    payload = _sessions_payload(store_root)

    # One root-level group of 2 total sessions.
    assert payload["total_sessions"] == 2
    assert payload["summary"]["root_sessions"] == 1
    assert payload["summary"]["child_sessions"] == 1

    root = _entry(payload, ROOT_UUID)
    child = _entry(payload, CHILD_SESSION)

    # Parent's own tokens stay the parent's own usage only — the child is
    # NEVER merged in.
    assert root["usage"]["fresh_tokens"] == 1200
    assert root["usage"]["fresh_tokens"] != 1570
    assert child["usage"]["fresh_tokens"] == 370

    # Child usage travels as a labeled, never-allocated descendants block.
    children_usage = root["related"]["children_usage"]
    assert children_usage["sessions"] == 1
    assert children_usage["fresh_tokens"] == 370
    assert children_usage["total_tokens"] == 370
    assert root["related"]["child_session_count"] == 1

    # Short labels are component-wise with the agent- prefix stripped, and
    # the child groups under the root's rollup key.
    assert root["client_session_id_short"] == ROOT_UUID[:8]
    assert child["client_session_id_short"] == f"{ROOT_UUID[:8]}:abcdef12"
    assert root["related"]["child_session_labels"] == [f"{ROOT_UUID[:8]}:abcdef12"]
    assert child["rollup_group_key"] == root["rollup_group_key"] == f"claude-code::{ROOT_UUID}"
    assert child["session_kind"] == "child"
    assert root["session_kind"] == "root"


def test_orphan_child_session_stays_visible_with_parent_reference(tmp_path):
    store_root = tmp_path / "state"
    # Child row whose parent never formed a rollup entry: it must stay
    # visible as its own rollup entry (never vanish) with a parent reference
    # carrying the short display label.
    _trusted_usage(
        store_root,
        session=CHILD_SESSION,
        client="claude-code",
        session_kind="child",
        parent_session=ROOT_UUID,
    )

    payload = _sessions_payload(store_root)

    assert payload["total_sessions"] == 1
    child = _entry(payload, CHILD_SESSION)
    assert child["session_kind"] == "child"
    parent_ref = child["related"]["parent"]
    assert parent_ref["client_session_id"] == ROOT_UUID
    # The display label is the short form (HTML consumers must render it).
    assert parent_ref["label"] == ROOT_UUID[:8]
    assert child["related"]["relationship_source"] == "importer_recorded_parent_id"


def test_sections_only_session_carries_honest_usage_note(tmp_path):
    store_root = tmp_path / "state"
    _record_section(store_root, section_id="ghost-work", title="Sections only work", session="sections-only-session")

    payload = _sessions_payload(store_root)

    entry = _entry(payload, "sections-only-session")
    assert entry["join"]["state"] == "sections_only"
    # The exact honest note: usage unknown, never a zero-cost claim.
    assert entry["usage_note"] == (
        "usage unknown for this session — no imported usage rows matched; this is not a zero-cost claim"
    )
    # No imported usage rows backing any token figure.
    assert entry["usage"]["rows"] == 0
    assert [item["title"] for item in entry["work"]["items"]] == ["Sections only work"]
    assert payload["summary"]["sessions_with_sections_only"] == 1


def test_join_states_attributed_and_unjoined_with_reason(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="joined-session", input_tokens=500, output_tokens=100, cached_input_tokens=0)
    _record_section(store_root, section_id="joined-work", title="Joined work", session="joined-session")
    _trusted_usage(store_root, session="lonely-session", input_tokens=10, output_tokens=5, cached_input_tokens=0)

    payload = _sessions_payload(store_root)

    joined = _entry(payload, "joined-session")
    lonely = _entry(payload, "lonely-session")

    assert joined["join"]["state"] == "attributed"
    assert lonely["join"]["state"] == "unjoined"
    # The join reason travels with the state (the HTML chip's title text).
    assert lonely["join"]["reason"]
    # Attributed-sessions fraction.
    assert payload["summary"]["attributed_sessions"] == 1
    assert payload["summary"]["total_sessions"] == 2
    # Attributed detail names the work item at a stated confidence.
    attributed_work = joined["join"]["attributed_work"]
    assert len(attributed_work) == 1
    assert attributed_work[0]["title"] == "Joined work"
    assert attributed_work[0]["join_confidence"] == "high"
    assert attributed_work[0]["join_strategy"]


def test_sessions_limit_slices_but_total_stays_honest(tmp_path):
    store_root = tmp_path / "state"
    for index in range(41):
        _trusted_usage(store_root, session=f"bulk-session-{index:03d}", input_tokens=10, output_tokens=1, cached_input_tokens=0)

    # limit slices sessions[] only; total_sessions always describes the full
    # store, so truncation can never be silent (the JSON form of the old
    # "Showing 40 of 41" cap note).
    truncated = _sessions_payload(store_root, query="?limit=40")
    assert len(truncated["sessions"]) == 40
    assert truncated["total_sessions"] == 41
    # ``limit`` is a real JSON param, never flagged as an ignored HTML param.
    assert "ignored_html_params" not in truncated

    full = _sessions_payload(store_root)
    assert len(full["sessions"]) == 41
    assert full["total_sessions"] == 41


# ---------------------------------------------------------------------------
# 2h: work items
# ---------------------------------------------------------------------------


def test_work_items_usage_attribution_and_namespaced_id_resolution(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="attr-session", input_tokens=700, output_tokens=100, cached_input_tokens=20)
    _record_section(store_root, section_id="attributed-work", title="Attributed work item", session="attr-session")
    _record_section(store_root, section_id="usage-less-work", title="Usage-less work item", session="other-session")

    api_client = _api(store_root)
    items = api_client.get("/work-items").json()["work_items"]
    by_title = {item["title"]: item for item in items}
    assert set(by_title) == {"Attributed work item", "Usage-less work item"}

    attributed = by_title["Attributed work item"]
    # Fresh headline for the attributed item (cache reads stay separate).
    assert attributed["usage_fresh_total"] == 800
    assert attributed["usage_cache_read_total"] == 20
    assert attributed["linked_usage_records"] == 1
    # Every cost figure states a confidence (PRD §10.2).
    assert attributed["estimated_cost_total"] == 0.01
    assert attributed["cost_confidence_breakdown"] == {"estimated_from_tokens": 1}

    # The usage-less item never carries a zero-cost claim: no linked records.
    usage_less = by_title["Usage-less work item"]
    assert usage_less["linked_usage_records"] == 0
    assert usage_less["join_explanation"]["attributed_usage_count"] == 0

    # work_id is namespaced client::session::section_id and resolves.
    for item in items:
        assert "::" in item["work_id"]
        response = api_client.get(f"/work-items/{item['work_id']}")
        assert response.status_code == 200
        assert response.json()["work_item"]["work_id"] == item["work_id"]


# ---------------------------------------------------------------------------
# 2i: grouped attention
# ---------------------------------------------------------------------------


def test_attention_groups_by_cause_with_bounded_redacted_examples(tmp_path):
    store_root = tmp_path / "state"
    for index in range(30):
        _trusted_usage(store_root, session=f"noise-session-{index:02d}", input_tokens=10, output_tokens=2, cached_input_tokens=0)

    api_client = _api(store_root)
    response = api_client.get("/attention")
    assert response.status_code == 200
    payload = response.json()

    # ONE cause group for the flood; the raw item count travels alongside.
    assert payload["total_items"] == 30
    groups = payload["attention_groups"]
    assert len(groups) == 1
    group = groups[0]
    assert group["cause"] == "usage_truth_without_mcp_context"
    assert group["count"] == 30
    assert group["title"] == "30 usage row(s) have no work context"
    # Bounded, pre-redacted example refs (never one entry per flooded row).
    assert len(group["example_refs"]) == 3
    # Labels are pre-redacted short session tails — full ids never appear.
    assert "noise-session-" not in response.text

    overview = api_client.get("/overview").json()["overview"]
    assert overview["attention_group_count"] == 1
    assert overview["attention_item_count"] == 30
    assert overview["attention_counts"] == {"usage_truth_without_mcp_context": 30}


def test_attention_empty_state(tmp_path):
    store_root = tmp_path / "state"
    SentinelService(store_root)

    api_client = _api(store_root)
    payload = api_client.get("/attention").json()
    assert payload["total_items"] == 0
    assert payload["attention_groups"] == []

    overview = api_client.get("/overview").json()["overview"]
    assert overview["attention_group_count"] == 0
    assert overview["attention_item_count"] == 0


# ---------------------------------------------------------------------------
# 2j: reconciliation — THE regression class from the readiness review
# ---------------------------------------------------------------------------


def test_reconciliation_keeps_attributed_row_visible_among_121_unattributed(tmp_path):
    store_root = tmp_path / "state"
    for index in range(121):
        _trusted_usage(
            store_root,
            session=f"unattributed-{index:03d}",
            input_tokens=1000 + index,
            output_tokens=100,
            cached_input_tokens=0,
            cost=0.5,
        )
    _trusted_usage(store_root, session="honest-session", input_tokens=42, output_tokens=8, cached_input_tokens=0, cost=0.001)
    _record_section(store_root, section_id="honest-work", title="Attributed marker section", session="honest-session")

    api_client = _api(store_root)
    timeline = api_client.get("/timeline?limit=500").json()["timeline"]

    usage_rows = [entry for entry in timeline if entry["event_kind"] == "usage"]
    assert len(usage_rows) == 122
    # The single attributed row never drowns: it stays joined to its work item.
    attributed_rows = [row for row in usage_rows if row["work_id"] == "codex::honest-session::honest-work"]
    assert len(attributed_rows) == 1
    assert attributed_rows[0]["join_confidence"] == "high"
    assert attributed_rows[0]["tokens_fresh"] == 50
    unjoined_rows = [row for row in usage_rows if row["join_strategy"] == "unjoined"]
    assert len(unjoined_rows) == 121
    # The work event itself is present under its recorded title.
    assert any(entry["event_kind"] == "work" and entry["title"] == "Attributed marker section" for entry in timeline)

    overview = api_client.get("/overview").json()["overview"]
    assert overview["usage_without_mcp_context_count"] == 121


def test_reconciliation_row_reports_fresh_and_cache_read_tokens_separately(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="recon-session", input_tokens=100, output_tokens=25, cached_input_tokens=50)

    timeline = _api(store_root).get("/timeline?limit=500").json()["timeline"]
    usage_rows = [entry for entry in timeline if entry["event_kind"] == "usage"]
    assert len(usage_rows) == 1
    row = usage_rows[0]

    # Fresh vs cache-read never blur: 125 fresh, 175 total incl. 50 cache reads.
    assert row["tokens_fresh"] == 125
    assert row["tokens_cache_read"] == 50
    assert row["tokens_cache_creation"] == 0
    assert row["tokens"] == 175
    assert row["join_strategy"] == "unjoined"


# ---------------------------------------------------------------------------
# Redaction and diagnostic-noise exclusion
# ---------------------------------------------------------------------------


def test_product_payloads_redact_paths_and_exclude_doctor_noise(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(
        store_root,
        session=ROOT_UUID,
        client="claude-code",
        session_kind="root",
        project_dir="/Users/testuser/secret-project",
    )
    _record_section(store_root, section_id="redacted-work", title="Redaction work", session=OTHER_UUID, client="claude-code")
    SentinelService(store_root).record_event(
        {
            "source": "agent-sentinel-mcp",
            "event_type": "mcp_doctor_test",
            "metadata": {"summary": "doctor self-test event"},
        }
    )

    api_client = _api(store_root)
    sessions_response = api_client.get("/sessions")
    timeline_response = api_client.get("/timeline?limit=500")
    work_items_response = api_client.get("/work-items")

    for response in (sessions_response, timeline_response, work_items_response):
        assert response.status_code == 200
        # No absolute paths anywhere; only the pre-redacted last segment may
        # survive as the project label.
        assert "/Users/" not in response.text
        # Chronicle's own diagnostic events never reach a product surface.
        assert "mcp_doctor_test" not in response.text
        assert "doctor self-test event" not in response.text

    # The project label is pre-redacted at the data level to the last segment.
    payload = sessions_response.json()
    root = _entry(payload, ROOT_UUID)
    assert root["project"] == "secret-project"
    # The short display label rides next to the full machine-local id
    # (full ids on the JSON wire are addresses by locked decision; HTML/TUI
    # consumers must render client_session_id_short).
    assert root["client_session_id_short"] == ROOT_UUID[:8]


# ---------------------------------------------------------------------------
# Instrumentation markers: pre/post state + "Context after install" KPI
# ---------------------------------------------------------------------------


def _instrumentation_marker(store_root, *, client="claude-code", installed_at, surface="instructions_user"):
    """CLI-shaped marker written through the trust gate (as the CLI writers do)."""
    return SentinelService(store_root).record_event(
        {
            "source": "agent-sentinel-setup",
            "event_type": "instrumentation_installed",
            "metadata": {
                "client": client,
                "installed_at": installed_at,
                "installed_at_source": "backfill",
                "surface": surface,
            },
        },
        trusted_instrumentation_marker=True,
    )


def test_pre_instrumentation_state_neutral_and_post_keeps_bare_unjoined(tmp_path):
    store_root = tmp_path / "state"
    marker_at = 1_700_000_000.0
    _instrumentation_marker(store_root, client="claude-code", installed_at=marker_at)
    _trusted_usage(store_root, session="pre-session", client="claude-code", started_at=marker_at - 3600.0)
    _trusted_usage(store_root, session="post-session", client="claude-code", started_at=marker_at + 3600.0)

    payload = _sessions_payload(store_root)

    # Pre-install session: absent context is the EXPECTED state, labeled as
    # pre_instrumentation with the marker time and its derivation basis.
    pre = _entry(payload, "pre-session")
    assert pre["instrumentation_state"] == "pre_instrumentation"
    assert pre["instrumentation_state_basis"] == "session_start_vs_marker"
    assert pre["instrumentation_installed_at"] == marker_at
    # Post-install session with no context keeps today's bare unjoined state.
    post = _entry(payload, "post-session")
    assert post["instrumentation_state"] == "post_instrumentation"
    assert post["instrumentation_state_basis"] == "session_start_vs_marker"
    assert post["join"]["state"] == "unjoined"

    instrumentation = payload["summary"]["instrumentation"]
    assert instrumentation["pre_instrumentation_sessions"] == 1
    assert instrumentation["post_instrumentation_sessions"] == 1
    assert instrumentation["markers_by_client"]["claude-code"]["installed_at"] == marker_at


def test_context_after_install_kpi_fraction_and_no_marker_absence(tmp_path):
    # Without any marker the KPI is ABSENT: no rate is ever invented.
    bare_store = tmp_path / "bare-state"
    _trusted_usage(bare_store, session="uninstrumented-session", client="claude-code")
    bare = _sessions_payload(bare_store)
    bare_instrumentation = bare["summary"]["instrumentation"]
    assert bare_instrumentation["markers_by_client"] == {}
    assert bare_instrumentation["post_context_kpi"]["post_sessions"] == 0
    assert bare_instrumentation["post_context_kpi"]["context_rate"] is None
    assert _entry(bare, "uninstrumented-session")["instrumentation_state"] == "unknown"
    assert bare_instrumentation["unknown_sessions"] == 1

    # With a marker: 1 pre + 2 post (one with a section) -> 1 of 2.
    store_root = tmp_path / "state"
    marker_at = 1_700_000_000.0
    _instrumentation_marker(store_root, client="claude-code", installed_at=marker_at)
    _trusted_usage(store_root, session="pre-session", client="claude-code", started_at=marker_at - 3600.0)
    _trusted_usage(store_root, session="post-context", client="claude-code", started_at=marker_at + 3600.0)
    _record_section(store_root, section_id="post-work", title="Post-install work", session="post-context", client="claude-code")
    _trusted_usage(store_root, session="post-bare", client="claude-code", started_at=marker_at + 7200.0)

    payload = _sessions_payload(store_root)
    kpi = payload["summary"]["instrumentation"]["post_context_kpi"]
    assert kpi["post_sessions"] == 2
    assert kpi["post_with_context"] == 1
    assert kpi["context_rate"] == 0.5
    assert kpi["clients"] == [
        {
            "client": "claude-code",
            "installed_at": marker_at,
            "post_sessions": 2,
            "post_with_context": 1,
            "context_rate": 0.5,
        }
    ]


def test_context_after_install_rate_is_exact_never_flattered(tmp_path):
    """199/200 must surface as the exact 0.995 fraction, never a rounded 1.0
    (the retired HTML page floored the percent; the data keeps exactness)."""
    store_root = tmp_path / "state"
    marker_at = 1_700_000_000.0
    _instrumentation_marker(store_root, client="claude-code", installed_at=marker_at)
    service = SentinelService(store_root)
    for index in range(200):
        session = f"post-{index:03d}"
        service.record_event(
            {
                "source": "claude-code-local-session-import",
                "event_type": "model_usage",
                "provider": "claude-code",
                "model": "claude-4.5",
                "estimated_input_tokens": 10,
                "estimated_output_tokens": 5,
                "estimated_cost_usd": 0.001,
                "usage_confidence": "client_reported",
                "cost_confidence": "estimated_from_tokens",
                "metadata": {
                    "usage_source": "local_client_session_store",
                    "client": "claude-code",
                    "client_session_id": session,
                    "cached_input_tokens": 0,
                    "started_at": marker_at + 60.0 + index,
                },
            },
            trusted_usage_import=True,
        )
        if index == 0:
            continue  # exactly one post-install session stays context-free
        service.record_event(
            {
                "source": "claude-code",
                "event_type": "section_completed",
                "metadata": {
                    "sentinel_semantic_kind": "section",
                    "section_id": f"work-{index:03d}",
                    "section_status": "completed",
                    "section_title": f"Work {index:03d}",
                    "client": "claude-code",
                    "client_session_id": session,
                },
            }
        )

    payload = _sessions_payload(store_root, query="?limit=1000")
    kpi = payload["summary"]["instrumentation"]["post_context_kpi"]
    assert kpi["post_sessions"] == 200
    assert kpi["post_with_context"] == 199
    assert kpi["context_rate"] == 199 / 200
    assert kpi["context_rate"] != 1.0
    assert payload["summary"]["instrumentation"]["markers_by_client"] != {}
