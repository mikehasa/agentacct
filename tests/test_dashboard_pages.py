"""Dashboard regressions, including the Phase 7 product information architecture.

The primary navigation is intentionally small (`Work`, `Usage`, `Advanced`)
while the stable session, raw, and Evidence-v2 routes remain available under
their owning product area. These tests also lock the compact Work shell,
diagnostic identity strip, CSP/localhost boundary, no-live-scan contract,
single-feed composition, and honest cost/coverage disclosures.

All stores are throwaway tmp_path stores (suite conftest guards the real
dogfood ledger)."""

from fastapi.testclient import TestClient

import agent_chronicle.api as api_module
import agent_chronicle.service as service_module
from agent_chronicle.api import DASHBOARD_CSP, UsageDiscoveryConfig, create_local_api_app
from agent_chronicle.service import SentinelService
from refresh_flash import run_dashboard_refresh

HTML_ACCEPT = {"Accept": "text/html"}


def _trusted_usage(
    store_root,
    *,
    session,
    client="codex",
    input_tokens=100,
    output_tokens=25,
    cached_input_tokens=50,
    cost=0.01,
    cost_confidence="estimated_from_tokens",
    model="gpt-5.5",
    started_at=None,
    project_dir=None,
    session_kind=None,
    parent_session=None,
    cache_creation_input_tokens=None,
    cache_read_input_tokens=None,
    cache_creation_reported=None,
    cache_read_reported=None,
):
    if cache_creation_reported is None:
        cache_creation_reported = client != "codex"
    if cache_read_reported is None:
        cache_read_reported = True
    if cache_creation_reported and cache_creation_input_tokens is None:
        cache_creation_input_tokens = 0
    if cache_read_reported and cache_read_input_tokens is None:
        cache_read_input_tokens = cached_input_tokens
    metadata = {
        "usage_source": "local_client_session_store",
        "usage_update_semantics": "cumulative_snapshot",
        "client": client,
        "client_session_id": session,
        "cached_input_tokens": cached_input_tokens,
        "cache_creation_tokens_reported": cache_creation_reported,
        "cache_read_tokens_reported": cache_read_reported,
    }
    if started_at is not None:
        metadata["started_at"] = started_at
        metadata["updated_at"] = started_at
    if project_dir is not None:
        metadata["project_dir"] = project_dir
    if session_kind is not None:
        metadata["client_session_kind"] = session_kind
    if parent_session is not None:
        metadata["parent_client_session_id"] = parent_session
    if cache_creation_input_tokens is not None:
        metadata["cache_creation_input_tokens"] = cache_creation_input_tokens
    if cache_read_input_tokens is not None:
        metadata["cache_read_input_tokens"] = cache_read_input_tokens
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
            "cost_confidence": cost_confidence,
            "metadata": metadata,
        },
        trusted_usage_import=True,
    )


def _client(store_root, **kwargs):
    return TestClient(create_local_api_app(store_dir=store_root, **kwargs))


def _get_page(client, path):
    response = client.get(path, headers=HTML_ACCEPT)
    assert response.status_code == 200, path
    return response


# ---------------------------------------------------------------------------
# Route split + tab-era redirects
# ---------------------------------------------------------------------------


def test_tab_raw_redirects_302_to_raw_with_params(tmp_path):
    client = _client(tmp_path / "state")

    response = client.get("/?tab=raw", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/raw"

    with_params = client.get(
        "/?tab=raw&timeline_view=all&tools_sort=agent", follow_redirects=False
    )
    assert with_params.status_code == 302
    location = with_params.headers["location"]
    assert location.startswith("/raw?")
    assert "timeline_view=all" in location
    assert "tools_sort=agent" in location
    assert "tab=" not in location

    followed = client.get("/?tab=raw&timeline_view=all")
    assert followed.status_code == 200
    assert "Raw Data / Debug" in followed.text


def test_tab_product_redirects_302_to_overview(tmp_path):
    client = _client(tmp_path / "state")

    response = client.get("/?tab=product", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"

    # The old _safe_choice fallback rendered the product tab for unknown
    # values; the shim sends them to the Overview home the same way.
    bogus = client.get("/?tab=bogus", follow_redirects=False)
    assert bogus.status_code == 302
    assert bogus.headers["location"] == "/"

    with_params = client.get("/?tab=product&cost_sort=input", follow_redirects=False)
    assert with_params.headers["location"] == "/?cost_sort=input"


def test_bare_root_renders_overview_without_redirect(tmp_path):
    client = _client(tmp_path / "state")

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 200
    assert 'body class="page-overview"' in response.text


def test_top_nav_present_with_correct_active_state_on_every_page(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="nav-session")
    client = _client(store_root)

    # Primary nav is the product surface only: Work + Usage. Control and the
    # Advanced forensic pages are demoted to a footer link, so they render NO
    # active primary tab (their routes stay reachable).
    active_group = {
        "/": ("/", "Work"),
        "/sessions": ("/", "Work"),
        "/tokens": ("/tokens", "Usage"),
    }
    no_active = (
        "/control",
        "/advanced",
        "/raw",
        "/work-graph",
        "/evidence-matrix",
        "/discrepancies",
        "/cost-outcome-basis",
    )
    primary = (("/", "Work"), ("/tokens", "Usage"))
    for path in [*active_group, *no_active]:
        html = _get_page(client, path).text
        nav_start = html.index('<nav class="tabs" aria-label="Dashboard pages">')
        nav = html[nav_start : html.index("</nav>", nav_start)]
        assert nav.count('<a class="tab-link') == 2, path
        for href, label in primary:
            assert f'href="{href}"' in nav, (path, href)
            assert f'>{label}</a>' in nav, (path, label)
        # Control + Advanced are no longer in the primary nav.
        assert ">Control</a>" not in nav, path
        assert ">Advanced</a>" not in nav, path
        assert ">Overview</a>" not in nav
        assert ">Sessions</a>" not in nav
        assert ">Raw data</a>" not in nav
        if path in active_group:
            active_href, active_label = active_group[path]
            assert nav.count('class="tab-link active"') == 1, path
            assert (
                f'class="tab-link active" href="{active_href}" aria-current="page">'
                f"{active_label}</a>"
            ) in nav, path
        else:
            assert nav.count('class="tab-link active"') == 0, path

    # The demoted surfaces stay reachable via the footer.
    home = _get_page(client, "/").text
    assert '<span class="footer-links">' in home
    assert 'href="/advanced"' in home
    assert 'href="/control"' in home


# ---------------------------------------------------------------------------
# Product shell + diagnostic identity strip
# ---------------------------------------------------------------------------


def test_titleless_agent_step_never_reads_not_reported_work():
    # A completed section the agent recorded with no title (title aliased to the
    # work_id) and kind "unknown" must NOT render "Not reported work" next to the
    # "Agent reported" tag — that reads as a self-contradiction.
    item = {
        "work_id": "w-123",
        "section_id": "s-1",
        "title": "w-123",
        "kind": "unknown",
        "client": "claude-code",
    }
    label = api_module._work_item_display_title(item)
    assert "Not reported" not in label
    assert label == "Untitled step · Claude Code"
    # The agent's own summary is preferred when present.
    assert (
        api_module._work_item_display_title({**item, "summary": "Restore context and read PROGRESS"})
        == "Restore context and read PROGRESS"
    )
    # A meaningful kind reads as a normal step.
    assert api_module._work_item_display_title({**item, "kind": "testing"}) == "Testing step"


def test_work_home_uses_compact_shell_while_detail_pages_keep_identity(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="strip-a")
    _trusted_usage(store_root, session="strip-b")
    client = _client(store_root)

    home = _get_page(client, "/").text
    assert "<h1>Work</h1>" in home
    assert "What agents are doing in" in home
    assert '<div class="label">Store</div>' not in home
    assert '<div class="label">Saved usage rows</div>' not in home
    assert "Context after install" not in home

    for path in ("/tokens", "/sessions", "/raw"):
        html = _get_page(client, path).text
        assert '<div class="label">Store</div>' in html, path
        assert '<div class="label">Last import</div>' in html, path
        assert '<div class="label">Saved usage rows</div>' in html, path
        # Saved-row count reflects the imported usage rows.
        assert '<div class="label">Saved usage rows</div><div class="value">2</div>' in html, path
        # The post-instrumentation context KPI (setup hint without a marker).
        assert '<div class="label">Context after install</div>' in html, path
        assert "No instrumentation marker recorded" in html, path
        # Refresh is universal.  The potentially expensive local-log preview
        # is deliberately discoverable only from the Advanced hub.
        assert 'action="/usage/import-local"' in html, path
        assert '<button class="button" type="submit">Refresh</button>' in html, path
        assert "Preview local logs" not in html, path

    advanced = _get_page(client, "/advanced").text
    assert '<div class="label">Store</div>' in advanced
    assert '<div class="label">Preview evidence</div>' in advanced
    assert '<div class="value">Latest 2</div>' in advanced
    assert "bounded HTML preview · cap 2,000 records" in advanced
    assert '<div class="label">Last import</div>' not in advanced
    assert '<div class="label">Saved usage rows</div>' not in advanced
    assert '<div class="label">Context after install</div>' not in advanced
    assert 'action="/usage/import-local"' in advanced
    assert '<button class="button" type="submit">Refresh</button>' in advanced
    assert '<a class="button secondary button-link" href="/raw">Preview local logs</a>' in advanced

    raw = _get_page(client, "/raw").text
    assert '<a class="button secondary button-link" href="/advanced">Advanced home</a>' in raw
    assert 'action="/usage/import-local"' in home
    assert '<button class="button" type="submit">Refresh</button>' in home


def test_identity_strip_scope_label_project_vs_custom(tmp_path):
    project_store = tmp_path / "my-project" / ".agent-sentinel" / "state"
    project_client = _client(project_store)
    project_home = _get_page(project_client, "/").text
    project_details = _get_page(project_client, "/tokens").text
    assert "What agents are doing in my-project" in project_home
    assert "project-scoped store" in project_details

    custom_client = _client(tmp_path / "elsewhere")
    custom_home = _get_page(custom_client, "/").text
    custom_details = _get_page(custom_client, "/tokens").text
    assert "custom-scoped store" in custom_details
    # Never an absolute path.
    assert str(tmp_path) not in custom_home
    assert str(tmp_path) not in custom_details
    assert str(tmp_path) not in project_home
    assert str(tmp_path) not in project_details


def test_identity_strip_shows_last_import_time_after_refresh(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    import sqlite3

    con = sqlite3.connect(home / ".hermes" / "state.db")
    try:
        con.execute(
            """
            create table sessions (
                id text primary key, source text, model text, started_at real,
                message_count integer, input_tokens integer, output_tokens integer,
                cache_read_tokens integer, cache_write_tokens integer, reasoning_tokens integer,
                billing_provider text, estimated_cost_usd real, actual_cost_usd real
            )
            """
        )
        con.execute(
            "insert into sessions values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("hermes-import-time", "cli", "gpt-5-mini", 1_750_000_000.0, 2, 100, 20, 5, 4, 1, "openai", 0.11, 0.42),
        )
        con.commit()
    finally:
        con.close()
    client = _client(tmp_path / "state", usage_discovery=UsageDiscoveryConfig.from_home(home))

    before = _get_page(client, "/tokens").text
    assert "no saved imports yet" in before

    response = run_dashboard_refresh(client)
    assert response.status_code == 303

    after = _get_page(client, "/tokens").text
    assert "no saved imports yet" not in after


# ---------------------------------------------------------------------------
# Performance contract (PRD §8): product pages never run the live scan
# ---------------------------------------------------------------------------


def test_product_and_advanced_pages_never_run_live_scan(tmp_path, monkeypatch):
    """Every saved-data/evidence page renders without scanning client logs."""

    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="no-scan-session")

    def _boom(*args, **kwargs):
        raise AssertionError("the live client-log scan must not run for product pages")

    monkeypatch.setattr(api_module, "_discover_local_usage", _boom)
    monkeypatch.setattr(api_module, "_discover_local_usage_sources", _boom)
    client = _client(store_root)

    assert client.get("/").status_code == 200
    assert client.get("/tokens").status_code == 200
    assert client.get("/sessions", headers=HTML_ACCEPT).status_code == 200
    assert client.get("/sessions").status_code == 200  # JSON path too
    for path in (
        "/advanced",
        "/work-graph",
        "/evidence-matrix",
        "/discrepancies",
        "/cost-outcome-basis",
    ):
        assert client.get(path, headers=HTML_ACCEPT).status_code == 200, path


def test_advanced_preview_does_not_rebuild_v1_page_data(tmp_path, monkeypatch):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="advanced-no-v1-rebuild")
    client = _client(store_root)

    def _boom(*args, **kwargs):
        raise AssertionError("the Advanced evidence preview must not rebuild the v1 work ledger")

    monkeypatch.setattr(SentinelService, "list_all_events", _boom)

    response = client.get("/advanced", headers=HTML_ACCEPT)

    assert response.status_code == 200
    assert "Agent capability coverage" in response.text
    assert "Activity sync" in response.text
    assert "Preview evidence" in response.text
    assert "Preview sources" in response.text
    assert "Latest 1" in response.text
    assert "bounded HTML preview · cap 2,000 records" in response.text
    assert "Indexed evidence" not in response.text


def test_raw_page_and_refresh_post_still_scan(tmp_path, monkeypatch):
    """/raw keeps its live sections and the Refresh POST keeps scanning —
    tested WITHOUT the raising patch (counters instead)."""

    store_root = tmp_path / "state"
    calls = {"usage": 0, "sources": 0}
    real_usage = api_module._discover_local_usage
    real_sources = api_module._discover_local_usage_sources

    def counted_usage(*args, **kwargs):
        calls["usage"] += 1
        return real_usage(*args, **kwargs)

    def counted_sources(*args, **kwargs):
        calls["sources"] += 1
        return real_sources(*args, **kwargs)

    monkeypatch.setattr(api_module, "_discover_local_usage", counted_usage)
    monkeypatch.setattr(api_module, "_discover_local_usage_sources", counted_sources)
    client = _client(store_root, usage_discovery=UsageDiscoveryConfig.from_home(tmp_path / "home"))

    assert client.get("/raw").status_code == 200
    assert calls == {"usage": 1, "sources": 1}

    # The background refresh scans once; wait for it to finish before counting.
    run_dashboard_refresh(client)
    assert calls["usage"] == 2


# ---------------------------------------------------------------------------
# CSP + localhost guard on every HTML route
# ---------------------------------------------------------------------------


def test_csp_header_unchanged_value_on_every_html_page(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="csp-session")
    client = _client(store_root)

    assert DASHBOARD_CSP == "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"
    for path in (
        "/",
        "/tokens",
        "/sessions",
        "/advanced",
        "/raw",
        "/work-graph",
        "/evidence-matrix",
        "/discrepancies",
        "/cost-outcome-basis",
    ):
        response = _get_page(client, path)
        assert response.headers.get("content-security-policy") == DASHBOARD_CSP, path


def test_localhost_guard_covers_all_html_routes(tmp_path):
    client = _client(tmp_path / "state")

    for path in (
        "/",
        "/tokens",
        "/sessions",
        "/advanced",
        "/raw",
        "/work-graph",
        "/evidence-matrix",
        "/discrepancies",
        "/cost-outcome-basis",
    ):
        blocked = client.get(path, headers={"Host": "evil.example", **HTML_ACCEPT})
        assert blocked.status_code == 403, path


def test_csp_header_on_redirect_shims_and_refresh_post(tmp_path):
    """Fix round, cluster F: the CSP rides EVERY dashboard response,
    including the tab-era 302 shims and the Refresh POST's 303."""

    client = _client(tmp_path / "state", usage_discovery=UsageDiscoveryConfig.from_home(tmp_path / "home"))

    for path in ("/?tab=raw", "/?tab=product", "/?tab=bogus", "/?tab=raw&cost_sort=total"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 302, path
        assert response.headers.get("content-security-policy") == DASHBOARD_CSP, path

    refresh = run_dashboard_refresh(client)
    assert refresh.status_code == 303
    assert refresh.headers.get("content-security-policy") == DASHBOARD_CSP


def test_work_home_is_one_product_feed_and_hides_diagnostic_surfaces(tmp_path):
    store_root = tmp_path / "state"
    opaque_session_id = "opaque-client-session-id-that-must-not-be-a-visible-label"
    SentinelService(store_root).record_event(
        {
            "source": "codex",
            "event_type": "section_started",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "phase-7-human-work",
                "section_status": "started",
                "section_title": "Productize the work dashboard",
                "client": "codex",
                "client_session_id": opaque_session_id,
            },
        }
    )
    html = _client(store_root).get("/").text

    assert "Recent Tasks" in html
    assert 'id="work-feed" aria-label="Task feed"' in html
    assert "Productize the work dashboard" in html
    assert html.count('id="work-feed"') == 1
    assert 'id="attention"' not in html
    assert 'id="activity"' not in html
    assert 'id="source-coverage"' not in html
    assert 'id="usage-snapshot"' not in html
    assert "missing client_session_id" not in html
    assert "usage row(s) have no work context" not in html
    assert "Last updated" in html
    assert "Last checked" in html

    # Stable forensic routes still exist, but they are not presented as peer
    # product labels on Work. The Task homepage uses opaque action tokens and
    # must not emit the full client session id anywhere in its HTML.
    assert "Raw Data / Debug" not in html
    assert "Work Graph" not in html
    assert "Evidence Matrix" not in html
    assert "Forensic inspectors" not in html
    assert opaque_session_id not in html


def test_work_home_visualizes_agent_model_and_task_path(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="visual-session", client="codex", model="gpt-5.5")
    SentinelService(store_root).record_event(
        {
            "source": "codex",
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "visual-work",
                "section_status": "completed",
                "section_title": "Render a visual Task card",
                "client": "codex",
                "client_session_id": "visual-session",
                "summary": "The long-form agent note belongs in the drill-down.",
            },
        }
    )

    html = _client(store_root).get("/").text

    assert '<section class="agent-board"' in html
    assert "Agents &amp; models" in html
    assert '<span class="agent-avatar lane-codex"' in html
    assert '<span class="model-chip">gpt-5.5</span>' in html
    assert '<div class="run-flow" role="img"' in html
    assert ">Observed</span>" in html
    assert ">Task</span>" in html
    assert ">Check</span>" in html
    assert ">Outcome</span>" in html
    assert "175 total tokens" in html
    assert "incl. cache · 125 input + output · 50 cache reads" in html
    card_start = html.index('<article class="work-feed-item">')
    card = html[card_start : html.index("</article>", card_start)]
    assert card.index("<summary>Task details &amp; controls</summary>") < card.index(
        "The long-form agent note belongs in the drill-down."
    )


def test_work_home_keeps_multi_model_session_in_one_visual_card(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="multi-model-session", client="claude-code", model="claude-opus-4")
    _trusted_usage(store_root, session="multi-model-session", client="claude-code", model="claude-haiku-4")

    html = _client(store_root).get("/").text

    assert html.count('<article class="work-feed-item">') == 1
    assert '<span class="agent-avatar lane-claude-code"' in html
    assert '<span class="model-chip">claude-opus-4</span>' in html
    assert '<span class="model-chip">claude-haiku-4</span>' in html


def test_work_home_groups_root_child_and_internal_as_one_task(tmp_path):
    store_root = tmp_path / "state"
    now = 1_750_000_000.0
    _trusted_usage(
        store_root,
        session="lineage-root-session",
        session_kind="root",
        input_tokens=100,
        output_tokens=25,
        cached_input_tokens=0,
        model="gpt-root",
        started_at=now,
    )
    _trusted_usage(
        store_root,
        session="lineage-child-session",
        session_kind="child",
        parent_session="lineage-root-session",
        input_tokens=20,
        output_tokens=5,
        cached_input_tokens=0,
        model="gpt-child",
        started_at=now + 1,
    )
    _trusted_usage(
        store_root,
        session="lineage-review-session",
        session_kind="internal",
        parent_session="lineage-child-session",
        input_tokens=8,
        output_tokens=2,
        cached_input_tokens=0,
        model="gpt-review",
        started_at=now + 2,
    )
    client = _client(store_root)

    overview = client.get("/").text

    assert overview.count('<article class="work-feed-item">') == 1
    card_start = overview.index('<article class="work-feed-item">')
    card = overview[card_start : overview.index("</article>", card_start)]
    assert "1 root chat · 2 supporting sessions collapsed" in card
    assert "125 known subtotal tokens" in card
    assert "incl. cache · 125 input + output" in card
    assert "2 usage rows held" in card
    assert "cache split partial" in card
    assert "1 root chat · 2 supporting sessions" in card
    assert (
        "Proven additive rows contribute once; 2 usage rows are held for source identity or lineage normalization"
        in card
    )
    assert card.count('class="model-chip"') == 3

    task_payload = client.get("/tasks").json()
    assert task_payload["summary"]["task_count"] == 1
    task = task_payload["tasks"][0]
    assert task["session_count"] == 3
    assert task["child_count"] == 1
    assert task["internal_count"] == 1
    assert task["usage"]["fresh_tokens"] == 125
    assert task["usage"]["excluded_non_additive_rows"] == 2
    detail_href = card.split('href="', 1)[1].split('"', 1)[0]
    detail = client.get(detail_href, headers={"Accept": "text/html"}).text
    assert "Known additive subtotal incl. cache" in detail
    assert "complete cost unavailable" in detail

    usage_pulse_start = overview.index('<section class="usage-pulse"')
    usage_pulse = overview[usage_pulse_start : overview.index("</section>", usage_pulse_start)]
    assert "1 root chat · 2 supporting sessions collapsed" in usage_pulse
    assert "main run" not in usage_pulse.lower()
    agent_board_start = overview.index('<section class="agent-board"')
    agent_board = overview[agent_board_start : overview.index("</section>", agent_board_start)]
    assert "1 root chat · 2 supporting sessions collapsed" in agent_board
    assert "main run" not in agent_board.lower()
    assert len(client.get("/sessions").json()["sessions"]) == 3


def test_overview_cycle_group_keeps_every_session_and_counts_only_supporting_members():
    def entry(session_id, parent_id):
        return {
            "client": "codex",
            "client_session_id": session_id,
            "session_kind": "internal",
            "last_activity_at": 1.0,
            "related": {"parent": {"client_session_id": parent_id}},
            "usage": {"rows": 0, "model_lanes": []},
        }

    groups = api_module._overview_root_session_groups(
        [entry("cycle-b", "cycle-a"), entry("cycle-a", "cycle-b")]
    )

    assert len(groups) == 1
    assert groups[0]["root"]["client_session_id"] == "cycle-a"
    assert len(groups[0]["members"]) == 2
    assert groups[0]["supporting_count"] == 1
    assert groups[0]["internal_count"] == 1
    assert groups[0]["lineage_state"] == "cycle"
    card = api_module._root_activity_feed_item_html(groups[0], esc=api_module._esc_html)
    assert "Lineage group · cycle collapsed" in card
    assert "lineage group total" not in card  # no usage rows in this fixture
    assert "Main run" not in card

    orphan = api_module._overview_root_session_groups([entry("orphan-child", "missing-parent")])[0]
    assert orphan["lineage_state"] == "orphan_parent_missing"
    orphan_card = api_module._root_activity_feed_item_html(orphan, esc=api_module._esc_html)
    assert "Top-level supporting session · parent not observed" in orphan_card
    assert "Main run" not in orphan_card
    roster = api_module._agent_roster_html([orphan], api_module._esc_html)
    assert "0 root chats · 1 unresolved lineage group" in roster
    assert "main run" not in roster.lower()


def test_work_home_usage_strip_uses_scoped_saved_rows_once(tmp_path):
    store_root = tmp_path / "alpha" / ".agent-sentinel" / "state"
    _trusted_usage(
        store_root,
        session="alpha-usage-session",
        input_tokens=10,
        output_tokens=5,
        cached_input_tokens=10,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=7,
        cache_creation_reported=True,
        project_dir=str(store_root.parent.parent),
    )
    _trusted_usage(
        store_root,
        session="beta-usage-session",
        input_tokens=1_000,
        output_tokens=500,
        cached_input_tokens=900,
        cache_creation_input_tokens=400,
        cache_read_input_tokens=500,
        cache_creation_reported=True,
        project_dir="/worktrees/beta",
    )

    html = _client(store_root).get("/").text
    pulse_start = html.index('<section class="usage-pulse"')
    pulse = html[pulse_start : html.index("</section>", pulse_start)]

    assert "Workspace usage" in pulse
    assert "25</strong><span>Total incl. cache" in pulse
    assert "10 input after reported cache" in pulse
    assert "5 output" in pulse
    assert "3 cache writes" in pulse
    assert "7 cache reads" in pulse
    assert "1</strong><span>Sessions" in pulse
    assert "1</strong><span>Usage rows" in pulse
    assert "1.5K" not in pulse


def test_agent_roster_does_not_silently_cap_platforms(tmp_path):
    store_root = tmp_path / "state"
    platforms = (
        ("codex", "gpt-5.5"),
        ("claude-code", "claude-opus-4"),
        ("hermes", "gpt-5.4-mini"),
        ("opencode", "qwen-3"),
        ("openclaw", "gemini-3"),
    )
    for index, (client_name, model_name) in enumerate(platforms):
        _trusted_usage(
            store_root,
            session=f"roster-session-{index}",
            client=client_name,
            model=model_name,
        )

    html = _client(store_root).get("/").text

    assert html.count('<article class="agent-roster-item">') == len(platforms)
    for label in ("Codex", "Claude Code", "Hermes", "OpenCode", "OpenClaw"):
        assert f"<strong>{label}</strong>" in html


def test_work_home_marks_unlinked_model_neutrally_and_escapes_model_text(tmp_path):
    store_root = tmp_path / "state"
    SentinelService(store_root).record_event(
        {
            "source": "codex",
            "event_type": "section_started",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "sections-only",
                "section_status": "started",
                "section_title": "Sections-only work",
                "client": "codex",
                "client_session_id": "sections-only-session",
            },
        }
    )
    _trusted_usage(
        store_root,
        session="escaped-model-session",
        client="hermes",
        model='<model data-x="unsafe">',
    )

    html = _client(store_root).get("/").text

    assert "Model not linked" not in html
    assert "No deterministic session/model link was recorded" in html
    assert '&lt;model data-x=&quot;unsafe&quot;&gt;' in html
    assert '<model data-x="unsafe">' not in html


def test_work_home_uses_reporting_source_for_exact_session_model_link(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="source-fallback-session", client="codex", model="gpt-linked")
    SentinelService(store_root).record_event(
        {
            "source": "codex",
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "source-fallback",
                "section_status": "completed",
                "section_title": "Link through reporting source",
                "client_session_id": "source-fallback-session",
            },
        }
    )

    html = _client(store_root).get("/").text
    card_start = html.index('<article class="work-feed-item">')
    card = html[card_start : html.index("</article>", card_start)]

    assert '<span class="model-chip">gpt-linked</span>' in card
    assert "175 total tokens" in card
    assert "No deterministic session/model link" not in card


def test_work_home_uses_root_task_boundary_and_nests_run_id_steps(tmp_path):
    store_root = tmp_path / "state"
    now = 1_750_000_000.0
    _trusted_usage(
        store_root,
        session="long-root-session",
        session_kind="root",
        input_tokens=40,
        output_tokens=10,
        cached_input_tokens=0,
        model="gpt-root",
        started_at=now,
    )
    _trusted_usage(
        store_root,
        session="linked-review-session",
        session_kind="internal",
        parent_session="long-root-session",
        model="gpt-review",
        started_at=now + 1,
    )
    _trusted_usage(
        store_root,
        session="unlinked-sibling-session",
        session_kind="child",
        parent_session="long-root-session",
        input_tokens=24,
        output_tokens=6,
        cached_input_tokens=0,
        model="gpt-sibling",
        started_at=now + 2,
    )
    service = SentinelService(store_root)
    service.record_event(
        {
            "source": "codex",
            "run_id": "semantic-run-one",
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "linked-step",
                "section_status": "completed",
                "section_title": "Linked review step",
                "client": "codex",
                "client_session_id": "linked-review-session",
            },
        }
    )
    service.record_event(
        {
            "source": "codex",
            "run_id": "semantic-run-one",
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "idless-step",
                "section_status": "completed",
                "section_title": "Id-less sibling step",
                "client": "codex",
            },
        }
    )

    client = _client(store_root)
    projection = client.get("/tasks").json()
    assert projection["summary"]["task_count"] == 1
    assert projection["summary"]["associated_work_count"] == 2
    assert projection["summary"]["unresolved_work_count"] == 0
    task = projection["tasks"][0]
    assert task["session_count"] == 3
    assert task["usage"]["fresh_tokens"] == 50
    assert task["usage"]["excluded_non_additive_rows"] == 2
    assert {item["section_id"] for item in task["work_items"]} == {
        "linked-step",
        "idless-step",
    }
    assert task["run_subgroups"] == [
        {
            "run_id": "semantic-run-one",
            "work_ids": [item["work_id"] for item in task["work_items"]],
        }
    ]
    idless_item = next(item for item in task["work_items"] if item["section_id"] == "idless-step")
    assert idless_item["client_session_id"] is None
    assert idless_item["linked_usage_records"] == 0
    idless_association = next(
        association
        for association in task["work_associations"]
        if association["work_id"] == idless_item["work_id"]
    )
    assert idless_association["basis"] == "unique_task_from_run_id"
    assert idless_association["provenance"] == ["run_id_grouping_hint"]
    assert idless_association["exact_session_id"] is None
    assert set(task["models"]) == {"gpt-root", "gpt-review", "gpt-sibling"}

    html = client.get("/").text
    assert html.count('<article class="work-feed-item">') == 1
    card_start = html.index('<article class="work-feed-item">')
    task_card = html[card_start : html.index("</article>", card_start)]
    assert "1 root chat · 2 supporting sessions collapsed · 2 work steps" in task_card
    assert "Linked review step" in task_card
    assert "Id-less sibling step" in task_card
    assert "reported run semantic-run-one" not in task_card
    assert "Task-level association" in task_card
    assert task_card.count("50 known subtotal tokens") == 1
    assert "2 usage rows held" in task_card
    assert "cache split partial" in task_card
    assert "attributed usage" not in task_card
    assert "remaining activity" not in task_card
    assert "Model not linked" not in html
    for session_id in (
        "long-root-session",
        "linked-review-session",
        "unlinked-sibling-session",
    ):
        assert session_id not in html


def test_source_coverage_stays_in_advanced_and_off_work_home(tmp_path):
    client = _client(tmp_path / "state")
    response = client.post(
        "/work-events",
        json={
            "source": "openlit",
            "event_kind": "section",
            "status": "completed",
            "section_id": "phase-7-coverage",
            "client_session_id": "coverage-session",
        },
    )
    assert response.status_code == 200

    home = client.get("/").text
    assert 'id="source-coverage"' not in home
    assert "connection remains “Not verified.”" not in home

    advanced = client.get("/advanced").text
    coverage_start = advanced.index("<h2>Source coverage</h2>")
    coverage = advanced[coverage_start:]
    assert "OpenLIT" in coverage
    assert "1 evidence received" in coverage
    assert "connection remains “Not verified.”" in coverage
    assert '<span class="status status-missing">Not verified</span>' in coverage
    assert "does not prove that the source is installed, connected, or healthy now" in coverage


# ---------------------------------------------------------------------------
# Overview composition (locked PRD decisions)
# ---------------------------------------------------------------------------


def test_overview_caps_the_single_feed_and_keeps_full_sessions_explorer(tmp_path):
    store_root = tmp_path / "state"
    base = 1_750_000_000.0
    # Session ids whose 8-char display labels are unique (full ids never
    # render, so the probes below must target the short labels). codex labels
    # keep the LAST 8 chars (UUIDv7 tail rule), so the ids are tail-unique.
    for index in range(12):
        _trusted_usage(store_root, session=f"session-rec{index:02d}", started_at=base + index * 3600)
    client = _client(store_root)

    overview = client.get("/").text
    assert overview.count('<article class="work-feed-item">') == 10
    assert '<details class="session-row">' not in overview
    assert "Showing 10 of 12 recent Tasks." in overview
    assert '<a class="see-all" href="/sessions">View all activity →</a>' in overview

    # The explorer defaults to a 30-day last-activity window (locked PRD
    # decision); these synthetic sessions are older, so the full list needs
    # days=all.
    sessions_page = client.get("/sessions?days=all", headers=HTML_ACCEPT).text
    assert sessions_page.count('<details class="session-row">') == 12


def test_recent_completed_task_is_not_hidden_behind_old_verified_tasks(tmp_path, monkeypatch):
    store_root = tmp_path / "state"
    clock = iter(float(value) for value in range(1_000, 1_100))

    class Clock:
        @staticmethod
        def time():
            return next(clock)

    monkeypatch.setattr(service_module, "time", Clock())
    service = SentinelService(store_root)
    for index in range(10):
        section_id = f"old-verified-{index}"
        service.record_event(
            {
                "source": "codex",
                "event_type": "section_completed",
                "metadata": {
                    "sentinel_semantic_kind": "section",
                    "section_id": section_id,
                    "section_status": "completed",
                    "section_title": f"Old verified {index}",
                    "client": "codex",
                },
            }
        )
        service.record_event(
            {
                "source": "codex",
                "event_type": "machine_check",
                "metadata": {
                    "sentinel_semantic_kind": "evidence",
                    "section_id": section_id,
                    "evidence_type": "test",
                    "name": f"Old check {index}",
                    "result": "passed",
                },
            }
        )
    service.record_event(
        {
            "source": "codex",
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "new-reported",
                "section_status": "completed",
                "section_title": "Newest completed Task",
                "client": "codex",
            },
        }
    )

    overview = _client(store_root).get("/").text

    assert "Newest completed Task" in overview
    assert "Old verified 0" not in overview
    assert "Showing 10 of 11 recent Tasks." in overview


def test_work_home_keeps_cost_compact_and_tokens_page_owns_full_disclosure(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="cost-session", cost=0.25)
    client = _client(store_root)

    overview = client.get("/").text
    assert "175 total tokens" in overview
    assert "$0.25 est." in overview
    disclosure = "Costs are estimates derived from token counts × known API prices"
    assert disclosure not in overview
    tokens_html = client.get("/tokens").text
    assert disclosure in tokens_html
    assert "never provider invoices" in tokens_html


def test_work_home_only_promotes_blocked_work_not_join_hygiene(tmp_path):
    store_root = tmp_path / "state"
    for index in range(3):
        _trusted_usage(store_root, session=f"attn-{index}")
    SentinelService(store_root).record_event(
        {
            "source": "codex",
            "event_type": "section_blocked",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "blocked-work",
                "section_status": "blocked",
                "section_title": "Ship the dashboard",
                "client": "codex",
                "client_session_id": "blocked-session",
                "blocker": "A product decision is required.",
                "next_step": "Choose the default homepage scope.",
            },
        }
    )
    client = _client(store_root)

    overview = client.get("/").text
    assert "1 task needs input" in overview
    assert '<span class="status status-error">Blocked</span>' in overview
    assert "A product decision is required." in overview
    assert "Choose the default homepage scope." in overview
    assert "3 usage row(s) have no work context" not in overview
    assert "Enable MCP context attach at session start" not in overview
    assert "Needs attention" not in overview


def test_work_home_shows_explicit_blocker_resolution_without_faking_verified_completion(tmp_path):
    store_root = tmp_path / "state"
    project_dir = str(tmp_path / "project")
    _trusted_usage(
        store_root,
        session="resolved-session",
        project_dir=project_dir,
    )
    service = SentinelService(store_root)
    blocked = service.record_event(
        {
            "source": "codex",
            "event_type": "section_blocked",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "publish-private-pr",
                "section_status": "blocked",
                "section_title": "Back up product work in a private PR",
                "client": "codex",
                "client_session_id": "resolved-session",
                "project_dir": project_dir,
                "blocker": "GitHub authentication is unavailable.",
                "next_step": "Re-authenticate GitHub.",
            },
        }
    )
    service.record_event(
        {
            "source": "codex",
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "section_id": "publish-private-pr",
                "client": "codex",
                "client_session_id": "resolved-session",
                "project_dir": project_dir,
                "evidence_type": "artifact",
                "result": "passed",
                "exit_code": 0,
                "summary": "The private PR is available.",
                "resolves_blocked_event_id": blocked["event_id"],
                "resolution_scope": "full",
                "resolution_summary": "Authentication recovered and the private backup was published.",
                "resolution_objective_basis": "exit_code",
            },
        },
        trusted_blocker_resolution=True,
    )

    overview = _client(store_root).get("/").text

    assert "Back up product work in a private PR" in overview
    assert '<span class="status status-missing">Resolved</span>' in overview
    assert "Resolution reported" in overview
    assert "Authentication recovered and the private backup was published." in overview
    assert "agentacct does not relabel this as a verified completion." in overview
    assert "GitHub authentication is unavailable." not in overview
    assert '<span class="status status-found">Verified</span>' not in overview
    assert '<span class="status status-error">Blocked</span>' not in overview
    assert "0</strong><span>Needs input" in overview
    assert "1 root chat" in overview


def test_work_home_distinguishes_verified_completed_and_failed_tasks(tmp_path):
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    for section_id, title in (
        ("verified-work", "Verified result"),
        ("reported-work", "Reported result"),
        ("failed-work", "Failed result"),
    ):
        service.record_event(
            {
                "source": "codex",
                "event_type": "section_completed",
                "metadata": {
                    "sentinel_semantic_kind": "section",
                    "section_id": section_id,
                    "section_status": "completed",
                    "section_title": title,
                    "client": "codex",
                    "client_session_id": f"{section_id}-session",
                },
            }
        )
    for section_id, result in (("verified-work", "passed"), ("failed-work", "failed")):
        service.record_event(
            {
                "source": "codex",
                "event_type": "machine_check",
                "metadata": {
                    "sentinel_semantic_kind": "evidence",
                    "section_id": section_id,
                    "evidence_type": "test",
                    "result": result,
                    "summary": f"Check {result}.",
                },
            }
        )

    overview = _client(store_root).get("/").text
    assert "1 task has an open finding" in overview
    assert '<span class="status status-found">Verified</span>' in overview
    assert '<span class="status status-missing">Completed</span>' in overview
    assert '<span class="status status-finding">Open finding</span>' in overview
    assert "Agent finding" in overview
    assert "Check failed." in overview
    assert "This finding is about the work being reviewed, not agentacct health." in overview
    assert "What failed" not in overview
    assert "Resolve the reported failure, then rerun the same check." not in overview
    assert "completed work item(s) without strong evidence" not in overview


def test_generic_newer_completion_does_not_hide_historical_failure(tmp_path):
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    session_id = "historical-failure-session"
    service.record_event(
        {
            "source": "codex",
            "event_type": "section_started",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "still-failed",
                "section_status": "started",
                "section_title": "Do not hide failed evidence",
                "client": "codex",
                "client_session_id": session_id,
            },
        }
    )
    service.record_event(
        {
            "source": "codex",
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "section_id": "still-failed",
                "evidence_type": "test",
                "name": "Regression suite",
                "result": "failed",
            },
        }
    )
    service.record_event(
        {
            "source": "codex",
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "still-failed",
                "section_status": "completed",
                "section_title": "Do not hide failed evidence",
                "client": "codex",
                "client_session_id": session_id,
            },
        }
    )

    overview = _client(store_root).get("/").text

    assert '<span class="status status-finding">Open finding</span>' in overview
    assert "1 task has an open finding" in overview
    assert '<strong>1</strong><span>Open findings</span>' in overview


def test_independent_passing_check_does_not_erase_named_failure(tmp_path):
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    service.record_event(
        {
            "source": "codex",
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "independent-checks",
                "section_status": "completed",
                "section_title": "Keep independent checks separate",
                "client": "codex",
            },
        }
    )
    for name, result in (("Security tests", "failed"), ("Unit tests", "passed")):
        service.record_event(
            {
                "source": "codex",
                "event_type": "machine_check",
                "metadata": {
                    "sentinel_semantic_kind": "evidence",
                    "section_id": "independent-checks",
                    "evidence_type": "test",
                    "name": name,
                    "result": result,
                },
            }
        )

    overview = _client(store_root).get("/").text

    assert '<span class="status status-finding">Open finding</span>' in overview
    assert "Agent finding" in overview
    assert '<span class="status status-found">Verified</span>' not in overview


def test_work_cards_do_not_repeat_unattributed_session_usage_per_section(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="shared-visual-session", cost=0.25)
    service = SentinelService(store_root)
    for section_id, title in (("shared-one", "Shared task one"), ("shared-two", "Shared task two")):
        service.record_event(
            {
                "source": "codex",
                "event_type": "section_completed",
                "metadata": {
                    "sentinel_semantic_kind": "section",
                    "section_id": section_id,
                    "section_status": "completed",
                    "section_title": title,
                    "client": "codex",
                    "client_session_id": "shared-visual-session",
                },
            }
        )

    overview = _client(store_root).get("/").text

    assert overview.count('<article class="work-feed-item">') == 1
    card_start = overview.index('<article class="work-feed-item">')
    card = overview[card_start : overview.index("</article>", card_start)]
    assert '<span class="model-chip">gpt-5.5</span>' in card
    assert "Shared task one" in card
    assert "Shared task two" in card
    assert card.count("175 total tokens") == 1
    assert card.count("$0.25 est.") == 1
    assert "cache split partial" in card
    assert "2 work steps" in card


def test_work_card_does_not_render_unknown_cost_as_zero(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="unpriced-work-session", cost=None, cost_confidence="unknown")
    SentinelService(store_root).record_event(
        {
            "source": "codex",
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "unpriced-work",
                "section_status": "completed",
                "section_title": "Keep unknown cost unknown",
                "client": "codex",
                "client_session_id": "unpriced-work-session",
            },
        }
    )

    overview = _client(store_root).get("/").text

    assert "175 total tokens" in overview
    assert "$0.00 est." not in overview


def test_work_card_hides_partial_session_cost_while_usage_strip_labels_it(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="partial-cost-session", cost=0.25)
    _trusted_usage(
        store_root,
        session="partial-cost-session",
        cost=None,
        cost_confidence="unknown",
        model="gpt-unpriced",
    )
    SentinelService(store_root).record_event(
        {
            "source": "codex",
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "partial-cost-work",
                "section_status": "completed",
                "section_title": "Keep partial run cost honest",
                "client": "codex",
                "client_session_id": "partial-cost-session",
            },
        }
    )

    html = _client(store_root).get("/").text
    card_start = html.index('<article class="work-feed-item">')
    card = html[card_start : html.index("</article>", card_start)]

    assert "350 total tokens" in card
    assert "$0.25 est." not in card
    assert "$0.25</strong><span>Partial est. cost" in html


def test_attributed_group_cost_requires_every_linked_row_to_be_priced():
    item = {
        "linked_usage_records": 2,
        "usage_fresh_total": 250,
        "usage_total": 350,
        "estimated_cost_total": 0.25,
        "cost_confidence_breakdown": {"estimated_from_tokens": 1, "unknown": 1},
    }

    metrics = api_module._work_group_metrics([item], [], show_session_totals=False)

    assert ("350 total tokens", "attributed usage") in metrics
    assert not any(label == "attributed cost" for _value, label in metrics)


def test_supporting_review_rollup_requires_observed_supporting_lineage():
    base = {
        "items": [{"kind": "review", "latest_status": "completed"}],
        "sessions": [{"session_kind": "internal"}],
        "state": {"key": "verified", "action_required": False},
    }

    assert api_module._is_collapsible_supporting_review(base) is True
    assert api_module._is_collapsible_supporting_review(
        {**base, "sessions": [{"session_kind": "root"}]}
    ) is False
    assert api_module._is_collapsible_supporting_review(
        {**base, "state": {"key": "check_failed", "action_required": True}}
    ) is False


def test_reported_run_never_turns_unpriced_attributed_usage_into_zero_cost(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(
        store_root,
        session="stale-confidence-session",
        cost=None,
        # Deliberately stale/conflicting confidence: cost presence, not this
        # label, decides whether the row is actually priced.
        cost_confidence="estimated_from_tokens",
    )
    SentinelService(store_root).record_event(
        {
            "source": "codex",
            "run_id": "unpriced-reported-run",
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "unpriced-reported-work",
                "section_status": "completed",
                "section_title": "Keep attributed unknown cost honest",
                "client": "codex",
                "client_session_id": "stale-confidence-session",
            },
        }
    )

    html = _client(store_root).get("/").text
    card_start = html.index('<article class="work-feed-item">')
    card = html[card_start : html.index("</article>", card_start)]

    assert "175 total tokens" in card
    assert "$0.00 est." not in card
    assert "attributed cost" not in card


def test_completed_task_keeps_neutral_unverified_flow_signal(tmp_path):
    store_root = tmp_path / "state"
    SentinelService(store_root).record_event(
        {
            "source": "codex",
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "reported-neutral",
                "section_status": "completed",
                "section_title": "Reported without a check",
                "client": "codex",
                "client_session_id": "reported-neutral-session",
            },
        }
    )

    overview = _client(store_root).get("/").text

    assert '<span class="status status-missing">Completed</span>' in overview
    assert '<span class="run-step is-reported">Outcome</span>' in overview


def test_newer_passing_check_resolves_historical_failure_on_work_home(tmp_path):
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    service.record_event(
        {
            "source": "codex",
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "resolved-work",
                "section_status": "completed",
                "section_title": "Resolved check",
                "client": "codex",
                "client_session_id": "resolved-session",
            },
        }
    )
    for result in ("failed", "passed"):
        service.record_event(
            {
                "source": "codex",
                "event_type": "machine_check",
                "metadata": {
                    "sentinel_semantic_kind": "evidence",
                        "section_id": "resolved-work",
                        "evidence_type": "test",
                        "name": "Resolved work regression suite",
                        "result": result,
                },
            }
        )

    overview = _client(store_root).get("/").text
    assert "Resolved check" in overview
    assert '<span class="status status-found">Verified</span>' in overview
    assert '<strong>0</strong><span>Open findings</span>' in overview
    assert "Check failed" not in overview


def test_project_work_home_excludes_explicit_other_project_activity(tmp_path):
    project_root = tmp_path / "current-project"
    store_root = project_root / ".agent-sentinel" / "state"
    _trusted_usage(
        store_root,
        session="current-session",
        client="codex",
        project_dir=str(project_root),
    )
    _trusted_usage(
        store_root,
        session="trading-session",
        client="claude-code",
        project_dir=str(tmp_path / "Trading"),
    )
    client = _client(store_root)

    home = client.get("/").text
    assert "Codex in current-project" in home
    assert "current-project" in home
    assert "Claude Code task" not in home
    assert "Trading" not in home
    assert str(project_root) not in home
    assert str(tmp_path / "Trading") not in home

    sessions = client.get("/sessions?days=all", headers=HTML_ACCEPT).text
    assert "Claude Code" in sessions


def test_import_notice_renders_on_overview(tmp_path):
    client = _client(tmp_path / "state")

    html = client.get("/?refreshed=2&scanned=3&priced=1").text

    assert "Activity refreshed." in html
    assert "2 saved usage record(s) updated" in html
    assert "1 received list-price estimates" in html
    assert "3 scanned session(s)" not in html


# ---------------------------------------------------------------------------
# /tokens interim page + /sessions content negotiation
# ---------------------------------------------------------------------------


def test_tokens_page_cost_sort_param_moved_with_its_table(tmp_path):
    # Fix round, cluster A: the By-model table ranks by the STORED cost sum
    # (the pricing-catalog re-estimate is gone from /tokens), so the sort is
    # seeded with stored estimated_cost_usd values, not a catalog snapshot.
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    for client_name, model, cost in (("codex", "gpt-cheap", 0.001), ("hermes", "gpt-pricey", 0.50)):
        service.record_event(
            {
                "source": f"{client_name}-local-session-import",
                "event_type": "model_usage",
                "provider": client_name,
                "model": model,
                "estimated_input_tokens": 1000,
                "estimated_output_tokens": 100,
                "estimated_cost_usd": cost,
                "usage_confidence": "client_reported",
                "cost_confidence": "estimated_from_tokens",
                "metadata": {
                    "usage_source": "local_client_session_store",
                    "client": client_name,
                    "client_session_id": f"sort-{client_name}",
                    "cached_input_tokens": 0,
                },
            },
            trusted_usage_import=True,
        )
    client = _client(store_root)

    default_html = client.get("/tokens").text
    table = default_html[default_html.index("By model") :]
    # Default sort (stored cost, descending) puts the pricey lane first;
    # agent sort flips to alphabetical.
    assert table.index("gpt-pricey") < table.index("gpt-cheap")

    agent_html = client.get("/tokens?cost_sort=agent").text
    agent_table = agent_html[agent_html.index("By model") :]
    assert agent_table.index("gpt-cheap") < agent_table.index("gpt-pricey")

    # Whitelist fallback: bogus values render the default, never a 500.
    assert client.get("/tokens?cost_sort=bogus").status_code == 200


def test_sessions_path_html_for_browsers_json_for_everyone_else(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="nego-session")
    client = _client(store_root)

    html_response = client.get("/sessions", headers=HTML_ACCEPT)
    assert html_response.status_code == 200
    assert html_response.headers["content-type"].startswith("text/html")
    assert 'body class="page-sessions"' in html_response.text

    json_response = client.get("/sessions")
    assert json_response.headers["content-type"].startswith("application/json")
    payload = json_response.json()
    # JSON shape unchanged in this batch (endpoints untouched).
    assert set(payload) == {
        "schema_version",
        "session_rollup_schema_version",
        "total_sessions",
        "summary",
        "sessions",
    }
    assert payload["total_sessions"] == 1


# ---------------------------------------------------------------------------
# Design tokens (PRD §7) — Dashboard v2: token-level light + dark theming
# ---------------------------------------------------------------------------


def test_design_tokens_and_platform_lane_palette_present_with_dark_mode(tmp_path):
    client = _client(tmp_path / "state")

    html = client.get("/").text
    # CSS custom properties replaced the hardcoded style values.
    assert ":root {" in html
    assert "var(--color-text)" in html
    assert "--status-ok-bg:" in html
    # The 6-color platform lane palette (5 clients + other) as ready classes.
    for lane in ("claude-code", "codex", "hermes", "opencode", "openclaw", "other"):
        assert f"--lane-{lane}:" in html
        assert f".lane-{lane} {{ background: var(--lane-{lane}); }}" in html
    # Dashboard v2 (2026-07, owner-approved): the earlier "dark mode deferred"
    # decision is reversed. Dark mode ships via prefers-color-scheme, done at the
    # token level — the dark media query redefines the same custom properties on
    # :root so every token-driven component adapts (no per-component dark rules
    # beyond the few hard-coded-background fixes).
    assert "@media (prefers-color-scheme: dark)" in html
    assert "--color-bg: #090c12;" in html
    # numerics render in the monospace/tabular console face
    assert "--font-mono:" in html


# ---------------------------------------------------------------------------
# Batch D: empty states on every page (PRD §10.6 — empty states say why and
# what to do next; a store where nothing was imported never renders a
# measured-looking zero)
# ---------------------------------------------------------------------------


def _record_section(store_root, *, section_id, title, session, client="claude-code", status="completed"):
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


def test_empty_store_product_and_raw_pages_render_helpful_empty_states(tmp_path):
    # /raw runs the live scan: point discovery at an empty home so this test
    # sees the empty-machine rendering, not the developer's real client logs.
    client = _client(tmp_path / "state", usage_discovery=UsageDiscoveryConfig.from_home(tmp_path / "home"))

    overview = _get_page(client, "/").text
    assert "You&#x27;re caught up" in overview
    assert "No work or local activity has been recorded for this workspace yet." in overview
    assert "Fresh tokens" not in overview
    assert "No source evidence has been indexed yet." not in overview

    tokens = _get_page(client, "/tokens").text
    # Every /tokens table carries the next step, and an empty filter reports
    # unknown usage rather than manufacturing zero token or cost totals.
    assert (
        "No saved usage rows match this filter. Import usage (Refresh &amp; save usage) "
        "or widen the date range." in tokens
    )
    assert "token and cost totals are unknown, not zero" in tokens
    assert "No saved usage confidence rows yet." in tokens
    assert "No saved cost confidence rows yet." in tokens

    sessions = _get_page(client, "/sessions").text
    assert "No sessions yet." in sessions
    assert "No MCP section work items yet." in sessions
    assert "No reconciliation attention items." in sessions
    assert "No imported usage recorded yet." in sessions
    # Ledger insights explains itself instead of grading an empty store
    # ("good" chip over zero-value cards).
    assert "Nothing to reconcile yet." in sessions
    assert "Evidence-backed completed" not in sessions

    raw = _get_page(client, "/raw").text
    assert "No saved AI sessions yet." in raw
    assert "No saved cost confidence rows yet." in raw
    assert "No local AI tool usage found yet." in raw

    # No page renders a measured-looking dollar figure on an empty store —
    # the four fixed cost-confidence buckets used to print four $0.00 rows.
    for page in (overview, tokens, sessions, raw):
        assert "$0.00" not in page


def test_sections_only_store_keeps_work_visible_without_usage_warnings(tmp_path):
    store_root = tmp_path / "state"
    _record_section(store_root, section_id="fix-auth", title="Fix auth bug", session="sec-only-1")
    _record_section(store_root, section_id="add-tests", title="Add tests", session="sec-only-2", status="started")
    client = _client(store_root)

    overview = client.get("/").text
    # Named work remains useful without turning absent usage attribution into
    # a user-facing warning.
    assert "Fix auth bug" in overview
    assert "Add tests" in overview
    assert "Completed" in overview
    assert "In progress" in overview
    assert "Not attributed" not in overview
    assert "unknown is not zero" not in overview
    assert 'href="/sessions"' in overview

    tokens = client.get("/tokens").text
    assert "No saved usage confidence rows yet." in tokens
    assert "No saved cost confidence rows yet." in tokens
    assert "$0.00" not in tokens

    sessions = client.get("/sessions", headers=HTML_ACCEPT).text
    # Sessions + work items exist, so Ledger insights DOES grade the store
    # (the empty state is only for a truly empty ledger)...
    assert "Nothing to reconcile yet." not in sessions
    assert "Evidence-backed completed" in sessions
    # ...and each session row carries the honest no-usage note.
    assert "Sections recorded; no usage imported" in sessions


def test_usage_only_store_evidence_rate_unmeasured_not_zero_percent(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="usage-only-session")
    client = _client(store_root)

    sessions = client.get("/sessions", headers=HTML_ACCEPT).text

    # No completed work items → nothing to grade: an em dash, never the old
    # measured-looking "0%" of zero items.
    assert '<div class="label">Evidence-backed completed</div><div class="value">—</div>' in sessions
    assert "0 / 0 completed work items" in sessions


def test_health_endpoint_returns_agentacct_branded_service(tmp_path):
    """#3: /health returns the agentacct-branded service string (was the
    pre-rename `agent-sentinel-local-api`, a rename miss)."""
    response = _client(tmp_path / "state").get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["service"] == "agentacct-local-api"


def test_health_service_recognizers_accept_both_brands():
    """#3: renaming the /health service string is compat-safe — both the
    readiness check and the read canary still accept the pre-rename value, so a
    cross-version upgrade (old dashboard checked by newer code, or vice versa)
    never falsely reports the dashboard unhealthy / not-ours."""
    from agent_chronicle.activation import RuntimeManager
    from agent_chronicle.canonical.read_canary import _EXPECTED_SERVICES

    both = {"agentacct-local-api", "agent-sentinel-local-api"}
    assert set(RuntimeManager.DASHBOARD_HEALTH_SERVICES) == both
    assert set(_EXPECTED_SERVICES) == both
