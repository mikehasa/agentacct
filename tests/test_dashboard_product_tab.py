"""Phase 2 Batch B regressions: Product-tab session rollup, work items,
grouped attention, reconciliation, redaction, and cap notes.

All stores are throwaway tmp_path stores (suite conftest guards the real
dogfood ledger). Every assertion is at the rendered-HTML level — the whole
point of Batch B is what a first-time user actually sees."""

import re

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


def _product_html(store_root):
    """Work home `/`: compact state summary plus one deduplicated feed."""
    client = TestClient(create_local_api_app(store_dir=store_root))
    response = client.get("/")
    assert response.status_code == 200
    return response.text


def _sessions_html(store_root, query=""):
    """Sessions page (Phase 3.5a: the full rollup, work items, attention,
    reconciliation, and ledger insights live here; the bare /sessions path
    without an HTML Accept header stays the JSON rollup). Pass
    ``query="?days=all"`` when the test's synthetic sessions fall outside
    the explorer's default 30-day last-activity window (Phase 3.5c)."""
    client = TestClient(create_local_api_app(store_dir=store_root))
    response = client.get(f"/sessions{query}", headers={"Accept": "text/html"})
    assert response.status_code == 200
    return response.text


def _tokens_html(store_root):
    client = TestClient(create_local_api_app(store_dir=store_root))
    response = client.get("/tokens")
    assert response.status_code == 200
    return response.text


def _section_slice(html, section_id, next_section_id):
    start = html.index(f'id="{section_id}"')
    end = html.index(f'id="{next_section_id}"')
    assert start < end
    return html[start:end]


# ---------------------------------------------------------------------------
# 2g: summary strip + session rollup rows
# ---------------------------------------------------------------------------


def test_task_feed_usage_metadata_leads_with_total_tokens(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="strip-session", input_tokens=100, output_tokens=25, cached_input_tokens=50)

    html = _product_html(store_root)

    # A Task card leads with the full reported total. The workspace usage pulse
    # keeps the component composition visible separately.
    card_start = html.index('<article class="work-feed-item">')
    card = html[card_start : html.index("</article>", card_start)]
    assert "175 total tokens" in card
    assert "125 fresh tokens" not in card
    assert "cache writes" not in card
    assert "Workspace usage" in html
    assert "175</strong><span>Total incl. cache" in html
    assert "Usage snapshot" not in html


def test_session_rollup_renders_root_rows_and_never_merged_children(tmp_path):
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

    html = _sessions_html(store_root)
    sessions_html = _section_slice(html, "sessions", "work-items")

    # Exactly ONE top-level row: the child nests as a labeled line, never a row.
    assert sessions_html.count('<details class="session-row">') == 1
    assert f"<code>{ROOT_UUID[:8]}</code>" in sessions_html
    # Child usage is a labeled, never-merged descendants line.
    assert "1 subagent session(s) — not allocated to this session" in sessions_html
    assert "370 fresh" in sessions_html  # child's own fresh tokens in the line
    # Parent row's own tokens stay the parent's own usage only.
    assert "<strong>1,200</strong> fresh" in sessions_html
    assert "<strong>1,570</strong> fresh" not in sessions_html
    # Child short label is component-wise, agent- prefix stripped.
    assert f"{ROOT_UUID[:8]}:abcdef12" in sessions_html
    # Sessions metric: 1 root-level group of 2 total sessions.
    assert '<div class="value">1</div>' in sessions_html
    assert "2 total" in sessions_html
    # Full session ids never render.
    assert ROOT_UUID not in html
    assert CHILD_SESSION not in html


def test_orphan_child_session_stays_visible_with_parent_reference(tmp_path):
    store_root = tmp_path / "state"
    # Child row whose parent never formed a rollup entry: it must stay
    # visible as a top-level row (never vanish) with a short parent label.
    _trusted_usage(
        store_root,
        session=CHILD_SESSION,
        client="claude-code",
        session_kind="child",
        parent_session=ROOT_UUID,
    )

    html = _sessions_html(store_root)
    sessions_html = _section_slice(html, "sessions", "work-items")

    assert sessions_html.count('<details class="session-row">') == 1
    assert "child of session <code>" in sessions_html
    # The related-sessions line is real markup, not double-escaped text.
    assert "&lt;code&gt;" not in sessions_html
    # The parent is referenced by short label only.
    assert ROOT_UUID not in html


def test_sections_only_session_renders_honest_usage_note(tmp_path):
    store_root = tmp_path / "state"
    _record_section(store_root, section_id="ghost-work", title="Sections only work", session="sections-only-session")

    html = _sessions_html(store_root)
    sessions_html = _section_slice(html, "sessions", "work-items")

    assert "Sections recorded; no usage imported" in sessions_html
    assert "usage unknown for this session — no imported usage rows matched; this is not a zero-cost claim" in sessions_html
    assert "Sections only work" in sessions_html
    # Sections-only entries render a dash, never a zero-cost claim.
    assert "<strong>0</strong> fresh" not in sessions_html


def test_join_state_chips_attributed_and_unjoined_with_reason_title(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="joined-session", input_tokens=500, output_tokens=100, cached_input_tokens=0)
    _record_section(store_root, section_id="joined-work", title="Joined work", session="joined-session")
    _trusted_usage(store_root, session="lonely-session", input_tokens=10, output_tokens=5, cached_input_tokens=0)

    html = _sessions_html(store_root)
    sessions_html = _section_slice(html, "sessions", "work-items")

    assert ">Attributed</span>" in sessions_html
    assert ">No MCP context</span>" in sessions_html
    # The join reason travels on the chip's title attribute.
    assert '"status status-ready" title="' in sessions_html
    # Attributed-sessions fraction in the strip.
    assert '<div class="value">1 of 2</div>' in html
    # Attributed detail names the strategy at its confidence.
    assert "Joined work" in sessions_html
    assert "confidence" in sessions_html


def test_session_rows_order_attributed_first(tmp_path):
    store_root = tmp_path / "state"
    # Newest activity but unattributed…
    _trusted_usage(store_root, session="new-unjoined", input_tokens=50, output_tokens=10)
    # …versus older attributed session: attributed still renders first.
    _trusted_usage(store_root, session="old-attributed", input_tokens=40, output_tokens=8)
    _record_section(store_root, section_id="old-work", title="Old attributed work", session="old-attributed")

    html = _sessions_html(store_root)
    sessions_html = _section_slice(html, "sessions", "work-items")

    # codex tail labels: "old-attributed" -> "tributed", "new-unjoined" -> "unjoined".
    # Anchor on the session-row <code> label markup: bare "tributed"/"unjoined"
    # also match the "Attributed sessions" metric card and the "?join=unjoined"
    # filter pill, which render BEFORE the first row and made the ordering
    # assertion vacuous. Only session rows render the <code> label.
    attributed_row_at = sessions_html.index("<code>tributed</code>")
    unjoined_row_at = sessions_html.index("<code>unjoined</code>")
    assert attributed_row_at < unjoined_row_at


def test_session_cap_note_only_when_truncated(tmp_path):
    store_root = tmp_path / "state"
    for index in range(41):
        _trusted_usage(store_root, session=f"bulk-session-{index:03d}", input_tokens=10, output_tokens=1, cached_input_tokens=0)

    html = _sessions_html(store_root)
    sessions_html = _section_slice(html, "sessions", "work-items")

    assert "Showing 40 of 41 top-level sessions." in sessions_html
    assert sessions_html.count('<details class="session-row">') == 40


# ---------------------------------------------------------------------------
# 2h: work items table
# ---------------------------------------------------------------------------


def test_work_items_attributed_first_dash_for_zero_and_encoded_href(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="attr-session", input_tokens=700, output_tokens=100, cached_input_tokens=20)
    _record_section(store_root, section_id="attributed-work", title="Attributed work item", session="attr-session")
    # Recorded AFTER (newer updated_at) but without usage: must sort second.
    _record_section(store_root, section_id="usage-less-work", title="Usage-less work item", session="other-session")

    api_client = TestClient(create_local_api_app(store_dir=store_root))
    html = api_client.get("/sessions", headers={"Accept": "text/html"}).text
    work_html = _section_slice(html, "work-items", "attention")

    assert work_html.index("Attributed work item") < work_html.index("Usage-less work item")
    # Fresh headline for the attributed item; dash (never 0) for the other.
    assert "<strong>800</strong> fresh tokens" in work_html
    assert "Usage unknown / not attributed" in work_html
    # The attributed cost carries its confidence label (PRD §10.2 — every
    # cost figure states a confidence).
    assert "(estimated_from_tokens confidence)" in work_html
    # JSON links URL-encode the namespaced work_id and resolve.
    hrefs = re.findall(r'href="(/work-items/[^"]+)"', work_html)
    assert len(hrefs) == 2
    assert any("%3A%3A" in href for href in hrefs)
    for href in hrefs:
        assert api_client.get(href).status_code == 200


def test_work_items_cap_note_at_fifty(tmp_path):
    store_root = tmp_path / "state"
    for index in range(51):
        _record_section(
            store_root,
            section_id=f"bulk-work-{index:03d}",
            title=f"Bulk work {index:03d}",
            session=f"bulk-work-session-{index:03d}",
        )

    html = _sessions_html(store_root)
    work_html = _section_slice(html, "work-items", "attention")

    assert "Showing 50 of 51 work items." in work_html


# ---------------------------------------------------------------------------
# 2i: grouped attention
# ---------------------------------------------------------------------------


def test_attention_headline_is_group_count_with_anchor_and_bounded_examples(tmp_path):
    store_root = tmp_path / "state"
    for index in range(30):
        _trusted_usage(store_root, session=f"noise-session-{index:02d}", input_tokens=10, output_tokens=2, cached_input_tokens=0)

    html = _sessions_html(store_root)

    # Strip metric: headline number = GROUP count, raw item count in the note.
    assert '<a href="#attention">1</a>' in html
    assert "30 item(s) grouped by cause" in html
    attention_html = _section_slice(html, "attention", "reconciliation")
    assert "30 usage row(s) have no work context" in attention_html
    # Bounded, pre-redacted example refs (never one line per flooded row).
    assert attention_html.count("e.g. ") == 3
    assert "1 cause group(s) · 30 item(s)" in html


def test_attention_empty_state(tmp_path):
    store_root = tmp_path / "state"
    SentinelService(store_root)

    html = _sessions_html(store_root)

    assert "No reconciliation attention items." in html
    assert '<a href="#attention">0</a>' in html


# ---------------------------------------------------------------------------
# 2j: reconciliation — THE regression class from the readiness review
# ---------------------------------------------------------------------------


def test_reconciliation_shows_attributed_row_among_121_unattributed(tmp_path):
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

    html = _sessions_html(store_root)
    recon_html = _section_slice(html, "reconciliation", "ledger-insights")

    # The attributed row is visible in the rendered table…
    assert "Attributed marker section" in recon_html
    assert ">Attributed</span>" in recon_html
    # …ahead of the unattributed flood…
    assert recon_html.index("Attributed marker section") < recon_html.index("Usage without MCP context")
    # …and the cap is announced, never silent.
    assert "Showing 120 of 122 usage rows." in recon_html


def test_reconciliation_no_cap_note_below_cap_and_fresh_tokens_cell(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="recon-session", input_tokens=100, output_tokens=25, cached_input_tokens=50)

    html = _sessions_html(store_root)
    recon_html = _section_slice(html, "reconciliation", "ledger-insights")

    assert "Showing" not in recon_html
    assert "125 fresh tokens" in recon_html
    assert "175 total incl. 50 cache reads" in recon_html
    # Session id renders as the ledger's short label only (codex tail rule).
    assert "<code>-session</code>" in recon_html
    assert "recon-session" not in recon_html


# ---------------------------------------------------------------------------
# Redaction, empty store, escaping
# ---------------------------------------------------------------------------


def test_product_tab_redacts_paths_full_ids_and_doctor_noise(tmp_path):
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

    # The sweep covers every product page the sections moved to (3.5a split).
    api_client = TestClient(create_local_api_app(store_dir=store_root))
    pages = {
        "/": _product_html(store_root),
        "/sessions": _sessions_html(store_root),
        "/tokens": api_client.get("/tokens").text,
    }
    for page, html in pages.items():
        # No absolute paths anywhere; the pre-redacted last segment is allowed.
        assert "/Users/" not in html, page
        # Full session ids appear nowhere outside href addresses (locked
        # decision: the namespaced work_id in /work-items hrefs is an
        # address, not display).
        displayed = re.sub(r'href="[^"]*"', 'href=""', html)
        assert ROOT_UUID not in displayed, page
        assert OTHER_UUID not in displayed, page
        # Chronicle's own diagnostic events never reach a product page.
        assert "mcp_doctor_test" not in html, page
        assert "Doctor test" not in html, page
        assert "doctor self-test event" not in html, page
    for page in ("/", "/sessions"):
        assert "secret-project" in pages[page], page
        displayed = re.sub(r'href="[^"]*"', 'href=""', pages[page])
        if page == "/sessions":
            assert ROOT_UUID[:8] in displayed
        else:
            assert ROOT_UUID[:8] not in displayed


def test_empty_store_renders_friendly_empty_states(tmp_path):
    store_root = tmp_path / "state"
    SentinelService(store_root)

    overview_html = _product_html(store_root)
    sessions_html = _sessions_html(store_root)

    assert "No work or local activity has been recorded for this workspace yet." in overview_html
    assert "No sessions yet." not in overview_html
    assert "No sessions yet." in sessions_html
    assert "No MCP section work items yet." in sessions_html
    assert "No reconciliation attention items." in sessions_html
    assert "No imported usage recorded yet." in sessions_html
    assert '<div class="value">Usage unavailable</div>' in sessions_html
    # No table is truncated, so no cap note may render on either page.
    assert "Showing" not in overview_html
    assert "Showing" not in sessions_html


def test_new_sections_escape_agent_authored_titles(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="xss-session", input_tokens=10, output_tokens=2, cached_input_tokens=0)
    _record_section(
        store_root,
        section_id="xss-work",
        title="<script>alert(1)</script>Evil title",
        session="xss-session",
    )

    for html in (_product_html(store_root), _sessions_html(store_root)):
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;Evil title" in html


# ---------------------------------------------------------------------------
# Instrumentation markers: pre-instrumentation chip + "Context after install"
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


def test_pre_instrumentation_chip_neutral_and_post_keeps_bare_no_context(tmp_path):
    from datetime import datetime

    store_root = tmp_path / "state"
    marker_at = 1_700_000_000.0
    _instrumentation_marker(store_root, client="claude-code", installed_at=marker_at)
    _trusted_usage(store_root, session="pre-session", client="claude-code", started_at=marker_at - 3600.0)
    _trusted_usage(store_root, session="post-session", client="claude-code", started_at=marker_at + 3600.0)

    html = _sessions_html(store_root, query="?days=all")
    sessions_html = _section_slice(html, "sessions", "work-items")

    # Pre-install session: neutral chip (status-found, absent context is the
    # EXPECTED state, not a gap) with the install-date explanation on title.
    assert "Pre-instrumentation — recording was not installed when this session ran" in sessions_html
    installed_date = datetime.fromtimestamp(marker_at).strftime("%Y-%m-%d")
    assert (
        f"Recording instructions for Claude Code were installed {installed_date}; "
        "this session started before that, so no MCP work context could have been recorded."
    ) in sessions_html
    pre_chip_start = sessions_html.index("Pre-instrumentation —")
    chip_open = sessions_html.rindex('<span class="status ', 0, pre_chip_start)
    assert 'class="status status-found"' in sessions_html[chip_open:pre_chip_start]
    # Post-install session with no context keeps today's bare chip unchanged.
    assert ">No MCP context</span>" in sessions_html


def test_context_after_install_metric_kpi_fraction_and_no_marker_note(tmp_path):
    # Without any marker the KPI is ABSENT: a muted setup note, never a rate.
    bare_store = tmp_path / "bare-state"
    _trusted_usage(bare_store, session="uninstrumented-session", client="claude-code")
    bare_html = _tokens_html(bare_store)
    assert "No instrumentation marker recorded" in bare_html
    assert "`agentacct setup instructions`" in bare_html
    assert "setup mark-instrumented" in bare_html
    assert "post-install top-level sessions have MCP context" not in bare_html

    # With a marker: 1 pre + 2 post (one with a section) -> "1 of 2".
    store_root = tmp_path / "state"
    marker_at = 1_700_000_000.0
    _instrumentation_marker(store_root, client="claude-code", installed_at=marker_at)
    _trusted_usage(store_root, session="pre-session", client="claude-code", started_at=marker_at - 3600.0)
    _trusted_usage(store_root, session="post-context", client="claude-code", started_at=marker_at + 3600.0)
    _record_section(store_root, section_id="post-work", title="Post-install work", session="post-context", client="claude-code")
    _trusted_usage(store_root, session="post-bare", client="claude-code", started_at=marker_at + 7200.0)

    html = _tokens_html(store_root)

    assert '<div class="label">Context after install</div><div class="value">1 of 2</div>' in html
    assert "50% of post-install top-level sessions have MCP context" in html


def test_context_after_install_percent_floors_instead_of_rounding_up(tmp_path):
    """199/200 must render 99%, never a flattering rounded 100%."""
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

    html = _tokens_html(store_root)

    assert '<div class="label">Context after install</div><div class="value">199 of 200</div>' in html
    assert "99% of post-install top-level sessions have MCP context" in html
    assert "100% of post-install top-level sessions have MCP context" not in html
    assert "No instrumentation marker recorded" not in html
