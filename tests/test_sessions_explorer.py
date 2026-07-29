"""Phase 3.5c regressions: the Sessions explorer (PRD §6).

The /sessions filters (client / project / join / kind / days — whitelisted,
composable, every combination a URL, 40-row cap AFTER filtering with the
honest "Showing N of M (filtered from T)" restatement), the redesigned
two-line session row (lane badge, goal, duration, turns — absent values
omitted, never guessed), the Overview Theme B KPI cards, the /sessions JSON
verbatim-plus-additive contract, and the redaction sweep re-run on the
filtered page.

All stores are throwaway tmp_path stores (suite conftest guards the real
dogfood ledger)."""

import re
import time

from fastapi.testclient import TestClient

from agentacct.api import _session_join_filter_bucket, create_local_api_app
from agentacct.service import SentinelService

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
    model="gpt-5.5",
    session_kind=None,
    parent_session=None,
    project_dir=None,
    started_at=None,
    updated_at=None,
    turn_count=None,
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


def _record_section(store_root, *, section_id, title, session, client="codex", status="completed", project_dir=None):
    metadata = {
        "sentinel_semantic_kind": "section",
        "section_id": section_id,
        "section_status": status,
        "section_title": title,
        "client": client,
        "client_session_id": session,
    }
    if project_dir is not None:
        metadata["project_dir"] = project_dir
    return SentinelService(store_root).record_event(
        {
            "source": client,
            "event_type": f"section_{status}",
            "metadata": metadata,
        }
    )


def _client(store_root):
    return TestClient(create_local_api_app(store_dir=store_root))


def _sessions_html(client, query=""):
    response = client.get(f"/sessions{query}", headers=HTML_ACCEPT)
    assert response.status_code == 200
    return response.text


def _session_rows(html):
    """The rendered session-row chunks of the #sessions list, in order."""

    start = html.index('id="sessions"')
    end = html.index('id="work-items"')
    return html[start:end].split('<details class="session-row">')[1:]


def _row_for(html, short_id):
    for row in _session_rows(html):
        if f"<code>{short_id}</code>" in row:
            return row
    raise AssertionError(f"no session row for short id {short_id}")


# ---------------------------------------------------------------------------
# Filters: whitelists, composition, counts, empty states, cap-after-filter
# ---------------------------------------------------------------------------


def test_client_filter_pills_compose_and_every_combination_is_a_url(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    _trusted_usage(store_root, session="codex-session-aa", client="codex", started_at=now - 600)
    _trusted_usage(store_root, session="claude-session-bb", client="claude-code", model="fable-5", started_at=now - 700)
    client = _client(store_root)

    html = _sessions_html(client)
    # Default page: both rows, pills re-encode ALL params (shareable URLs).
    assert len(_session_rows(html)) == 2
    assert 'href="/sessions?client=codex&amp;project=all&amp;join=all&amp;kind=grouped&amp;days=30"' in html
    assert 'href="/sessions?client=all&amp;project=all&amp;join=all&amp;kind=grouped&amp;days=7"' in html

    filtered = _sessions_html(client, "?client=codex")
    filtered_rows = _session_rows(filtered)
    assert len(filtered_rows) == 1
    # codex labels keep the distinctive tail (UUIDv7 rule).
    assert "<code>ssion-aa</code>" in filtered_rows[0]
    # Scoped to the session list: the store-wide sections further down the
    # page (reconciliation etc.) legitimately keep every session's label.
    assert "<code>claude-s</code>" not in "".join(filtered_rows)
    # Filtered counts restate their basis: shown / matching / population,
    # with every active filter named (the default 30-day window included).
    assert (
        "Showing 1 of 1 matching session(s), filtered from 2 top-level sessions "
        "(platform Codex; last activity in the last 30 days)." in filtered
    )

    # Unknown whitelist values fall back to defaults — never a 500.
    fallback = _sessions_html(client, "?client=bogus&join=bogus&kind=bogus&days=bogus")
    assert len(_session_rows(fallback)) == 2


def test_project_filter_uses_rollup_labels_and_unknown_project_is_empty_never_a_guess(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    _trusted_usage(store_root, session="alpha-session-01", project_dir="/Users/owner/alpha-repo", started_at=now - 60)
    _trusted_usage(store_root, session="beta-session-002", project_dir="/Users/owner/beta-repo", started_at=now - 90)
    client = _client(store_root)

    html = _sessions_html(client)
    # Project pills come from the safe labels actually present in the rollup.
    assert ">alpha-repo</a>" in html
    assert ">beta-repo</a>" in html

    filtered = _sessions_html(client, "?project=alpha-repo")
    assert len(_session_rows(filtered)) == 1
    assert "<code>ssion-01</code>" in filtered  # codex tail label
    assert "project beta-repo" not in filtered

    unknown = _sessions_html(client, "?project=never-heard-of")
    assert len(_session_rows(unknown)) == 0
    assert "Project <code>never-heard-of</code> has no sessions in this store" in unknown
    assert "never a guess" in unknown
    assert "No sessions match the current filters" in unknown


def test_join_filter_buckets_partition_the_list(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    # attributed: one section + usage sharing the exact session key.
    _trusted_usage(store_root, session="attributed-sess-1", started_at=now - 60)
    _record_section(store_root, section_id="a-work", title="Attributed work", session="attributed-sess-1")
    # ambiguous: two candidate sections share the session's context.
    _trusted_usage(store_root, session="ambiguous-sess-01", started_at=now - 70)
    _record_section(store_root, section_id="b-work-1", title="Candidate one", session="ambiguous-sess-01")
    _record_section(store_root, section_id="b-work-2", title="Candidate two", session="ambiguous-sess-01")
    # context: sections recorded, no usage imported (sections_only).
    _record_section(store_root, section_id="c-work", title="Sections only work", session="context-sess-001")
    # unjoined: usage only, no MCP context anywhere. (Tail-unique id: codex
    # labels keep the LAST 8 chars, which must not collide with the context
    # session's "sess-001" tail.)
    _trusted_usage(store_root, session="unjoined-sess-991", started_at=now - 80)
    client = _client(store_root)

    everything = _sessions_html(client)
    assert len(_session_rows(everything)) == 4

    by_filter = {
        "attributed": "<code>d-sess-1</code>",
        "ambiguous": "<code>-sess-01</code>",
        "context": "<code>sess-001</code>",
        "unjoined": "<code>sess-991</code>",
    }
    for join_value, short_prefix in by_filter.items():
        html = _sessions_html(client, f"?join={join_value}")
        rows = _session_rows(html)
        assert len(rows) == 1, join_value
        assert short_prefix in rows[0], join_value


def test_join_filter_bucket_unit_covers_log_evidenced_and_context_only_states():
    # The log-evidenced unjoined override renders a context chip, so it
    # belongs to the "context" pill — exactly the chip families, no new states.
    evidenced = {"join": {"state": "unjoined", "client_log_evidence": {"evidenced_event_count": 2}}}
    assert _session_join_filter_bucket(evidenced) == "context"
    assert _session_join_filter_bucket({"join": {"state": "context_only"}}) == "context"
    assert _session_join_filter_bucket({"join": {"state": "sections_only"}}) == "context"
    assert _session_join_filter_bucket({"join": {"state": "unjoined"}}) == "unjoined"
    assert _session_join_filter_bucket({"join": {"state": "attributed"}}) == "attributed"
    assert _session_join_filter_bucket({"join": {"state": "ambiguous"}}) == "ambiguous"


def test_days_filter_defaults_to_30_on_last_activity_and_all_shows_everything(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="recent-session-01", started_at=time.time() - 3600)
    _trusted_usage(store_root, session="ancient-session-1", started_at=1_700_000_000.0)
    client = _client(store_root)

    default = _sessions_html(client)
    assert len(_session_rows(default)) == 1
    assert "<code>ssion-01</code>" in default  # codex tail label
    assert (
        "Showing 1 of 1 matching session(s), filtered from 2 top-level sessions "
        "(last activity in the last 30 days)." in default
    )

    everything = _sessions_html(client, "?days=all")
    assert len(_session_rows(everything)) == 2
    assert "matching session(s), filtered from" not in everything


def test_kind_filter_grouped_roots_and_all_flat(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    _trusted_usage(store_root, session="root-session-0001", session_kind="root", started_at=now - 60)
    _trusted_usage(
        store_root,
        session="root-session-0001:child",
        session_kind="child",
        parent_session="root-session-0001",
        started_at=now - 50,
    )
    _trusted_usage(
        store_root,
        session="orphan-child-sess1",
        session_kind="child",
        parent_session="ghost-parent-00001",
        started_at=now - 40,
    )
    client = _client(store_root)

    # grouped (default): the nested child is a labeled line, the orphan child
    # stays visible as its own top-level row.
    grouped = _sessions_html(client)
    assert len(_session_rows(grouped)) == 2
    assert "subagent session(s) — not allocated to this session" in grouped

    # roots: root-kind sessions only — the orphan child row drops out.
    roots = _sessions_html(client, "?kind=roots")
    rows = _session_rows(roots)
    assert len(rows) == 1
    assert "<code>ion-0001</code>" in rows[0]  # codex tail label
    # all-flat: every rollup entry is its own row; lineage stays visible.
    flat = _sessions_html(client, "?kind=all-flat")
    assert len(_session_rows(flat)) == 3
    assert "child of" in flat


def test_empty_filter_result_names_the_active_filters(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="codex-only-sess01", started_at=time.time() - 60)
    client = _client(store_root)

    html = _sessions_html(client, "?client=hermes&join=attributed")
    assert len(_session_rows(html)) == 0
    assert (
        "No sessions match the current filters (platform Hermes · join state attributed · "
        "last activity in the last 30 days). Clear a filter pill above or extend the date range." in html
    )
    # A store with no sessions at all keeps the what-to-do-next empty state.
    empty = _client(tmp_path / "empty-state")
    assert "No sessions yet." in _sessions_html(empty)


def test_forty_row_cap_applies_after_filtering_with_honest_note(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    for index in range(45):
        _trusted_usage(store_root, session=f"codex-bulk-{index:04d}", started_at=now - 60 - index)
    for index in range(2):
        _trusted_usage(
            store_root, session=f"claude-bulk-{index:04d}", client="claude-code", model="fable-5", started_at=now - 30
        )
    client = _client(store_root)

    html = _sessions_html(client, "?client=codex&days=all")
    assert len(_session_rows(html)) == 40
    assert (
        "Showing 40 of 45 matching session(s), filtered from 47 top-level sessions (platform Codex)." in html
    )


# ---------------------------------------------------------------------------
# Phase 3.5 fix round, cluster B (major): the Work items section is
# filter-aware per PRD §6.2 — client/project/days apply through the items'
# OWN fields; the join filter is a session-state concept and never applies.
# ---------------------------------------------------------------------------


def _work_items_slice(html):
    return html[html.index('id="work-items"') : html.index('id="attention"')]


def test_work_items_section_is_filter_aware_per_client_and_project(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    _trusted_usage(store_root, session="codex-wi-sess-01", client="codex", started_at=now - 60, project_dir="/proj/alpha")
    _record_section(
        store_root, section_id="codex-work", title="Codex-only work item",
        session="codex-wi-sess-01", client="codex", project_dir="/proj/alpha",
    )
    _trusted_usage(
        store_root, session="claude-wi-sess-1", client="claude-code", model="fable-5",
        started_at=now - 90, project_dir="/proj/beta",
    )
    _record_section(
        store_root, section_id="claude-work", title="Claude-only work item",
        session="claude-wi-sess-1", client="claude-code", project_dir="/proj/beta",
    )
    client = _client(store_root)

    everything = _work_items_slice(_sessions_html(client, "?days=all"))
    assert "Codex-only work item" in everything
    assert "Claude-only work item" in everything

    codex_only = _work_items_slice(_sessions_html(client, "?client=codex&days=all"))
    assert "Codex-only work item" in codex_only
    assert "Claude-only work item" not in codex_only

    claude_only = _work_items_slice(_sessions_html(client, "?client=claude-code&days=all"))
    assert "Claude-only work item" in claude_only
    assert "Codex-only work item" not in claude_only

    alpha_only = _work_items_slice(_sessions_html(client, "?project=alpha&days=all"))
    assert "Codex-only work item" in alpha_only
    assert "Claude-only work item" not in alpha_only

    # The join filter never applies to work items (session-state concept) —
    # both items stay visible, and the section note says so.
    joined = _sessions_html(client, "?join=attributed&days=all")
    joined_items = _work_items_slice(joined)
    assert "Codex-only work item" in joined_items
    assert "Claude-only work item" in joined_items
    assert "join filter" in joined_items
    # The page-top disclosure now names work items as filter-aware.
    assert "Filters apply to the session list and the Work items section" in joined


def test_work_item_filter_predicate_missing_fields_and_days(tmp_path):
    from datetime import date, datetime, timedelta

    from agentacct.api import _work_item_matches_filters

    today = date(2026, 7, 10)
    range_start = today - timedelta(days=29)
    recent = datetime(2026, 7, 9, 12, 0).timestamp()
    ancient = datetime(2026, 1, 1, 12, 0).timestamp()
    item = {"client": "codex", "project_dir": "alpha", "updated_at": recent}

    assert _work_item_matches_filters(item, client="all", project="all", range_start=None, today=today)
    assert _work_item_matches_filters(item, client="codex", project="alpha", range_start=range_start, today=today)
    assert not _work_item_matches_filters(item, client="claude-code", project="all", range_start=None, today=today)
    assert not _work_item_matches_filters(item, client="all", project="beta", range_start=None, today=today)
    # Standard filter semantics: an item missing the filtered key is excluded
    # by that specific filter, but always present under "all".
    missing = {"client": None, "project_dir": None, "updated_at": None}
    assert _work_item_matches_filters(missing, client="all", project="all", range_start=None, today=today)
    assert not _work_item_matches_filters(missing, client="codex", project="all", range_start=None, today=today)
    assert not _work_item_matches_filters(missing, client="all", project="alpha", range_start=None, today=today)
    assert not _work_item_matches_filters(missing, client="all", project="all", range_start=range_start, today=today)
    # A bounded range excludes items outside it; days=all keeps them.
    old = {"client": "codex", "project_dir": "alpha", "updated_at": ancient}
    assert not _work_item_matches_filters(old, client="all", project="all", range_start=range_start, today=today)
    assert _work_item_matches_filters(old, client="all", project="all", range_start=None, today=today)


# ---------------------------------------------------------------------------
# Phase 3.5 fix round, cluster D: project pills are capped with an honest
# overflow note, and unknown project values are never echoed into URLs.
# ---------------------------------------------------------------------------


def test_project_filter_pills_capped_at_12_with_overflow_note(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    for index in range(15):
        _trusted_usage(
            store_root,
            session=f"proj-sess-{index:04d}",
            started_at=now - 60 - index,
            project_dir=f"/repos/proj-{index:02d}",
        )
    client = _client(store_root)

    html = _sessions_html(client, "?days=all")
    project_row = html[html.index('<span class="filter-label">Project</span>') : html.index('<span class="filter-label">Join state</span>')]

    assert ">proj-11</a>" in project_row
    assert ">proj-12</a>" not in project_row
    assert "and 3 more (use ?project=)" in project_row


def test_unknown_project_value_never_echoes_into_urls_and_stays_escaped(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="proj-echo-sess-1", started_at=time.time() - 60)
    client = _client(store_root)

    evil = "<script>alert(1)</script>"
    response = client.get("/sessions", params={"project": evil}, headers=HTML_ACCEPT)

    assert response.status_code == 200
    html = response.text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html  # named in the unknown-filter note, escaped
    assert "has no sessions in this store" in html
    for href in re.findall(r'href="([^"]*)"', html):
        assert "script" not in href and "alert" not in href, href


# ---------------------------------------------------------------------------
# Phase 3.5 fix round, cluster G: /sessions Accept negotiation parses
# q-values, and JSON responses name the HTML filter params they ignored.
# ---------------------------------------------------------------------------


def test_sessions_accept_negotiation_realistic_client_matrix(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="nego-matrix-sess", started_at=time.time() - 60)
    client = _client(store_root)

    browser = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    html_accepts = (
        browser,
        "text/html",
        "text/html;q=0.9,application/json;q=0.8",  # HTML preferred over JSON
    )
    json_accepts = (
        "*/*",  # curl / python-requests / fetch defaults: never HTML for */* alone
        "application/json",
        "",  # absent header
        "application/json, text/html;q=0.5",  # JSON preferred, HTML tolerated
        "application/json, text/html;q=0",  # HTML explicitly refused (RFC 9110)
        "text/html;q=0",
    )
    for accept in html_accepts:
        response = client.get("/sessions", headers={"Accept": accept})
        assert response.headers["content-type"].startswith("text/html"), accept
    for accept in json_accepts:
        response = client.get("/sessions", headers={"Accept": accept})
        assert response.headers["content-type"].startswith("application/json"), accept
        response.json()  # a JSON client can always parse what it negotiated


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
# Session row redesign (PRD §6.2)
# ---------------------------------------------------------------------------


def test_session_row_two_lines_lane_badge_goal_and_join_chip(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="designed-sess-001", started_at=time.time() - 60)
    _record_section(store_root, section_id="goal-work", title="Add rate-limit to login", session="designed-sess-001")
    client = _client(store_root)

    row = _row_for(_sessions_html(client), "sess-001")
    summary = row[: row.index("</summary>")]

    # Line 1: lane-colored client badge · 8-char id · goal · join chip.
    line1 = summary[: summary.index('<div class="session-line session-meta">')]
    assert '<span class="badge-client lane-codex">Codex</span>' in line1
    assert "<code>sess-001</code>" in line1  # codex tail label
    assert '<span class="session-goal">Add rate-limit to login</span>' in line1
    assert ">Attributed</span>" in line1
    # Line 2 (muted): project dash note · started · tokens · cost+confidence.
    line2 = summary[summary.index('<div class="session-line session-meta">') :]
    assert "started 20" in line2
    assert "<strong>125</strong> fresh" in line2
    assert "cache writes" in line2
    assert "$0.01" in line2
    assert "(estimated_from_tokens cost confidence)" in line2


def test_session_row_duration_and_turns_render_when_known(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    _trusted_usage(
        store_root,
        session="spanned-sess-0001",
        started_at=now - 7320,
        updated_at=now,
        turn_count=7,
    )
    client = _client(store_root)

    row = _row_for(_sessions_html(client), "ess-0001")
    meta = row[row.index('<div class="session-line session-meta">') : row.index("</summary>")]
    assert "2h 2m" in meta
    assert "7 turn(s)" in meta


def test_session_row_duration_and_turns_omitted_when_absent_never_zero(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    # Backwards client clock (started after updated): the ledger refuses the
    # span (None) and the row must OMIT duration, never render a guessed 0.
    # No turn_count either -> the turns segment is omitted too.
    _trusted_usage(
        store_root,
        session="backwards-sess-01",
        started_at=now,
        updated_at=now - 3600,
    )
    client = _client(store_root)

    row = _row_for(_sessions_html(client), "-sess-01")
    meta = row[row.index('<div class="session-line session-meta">') : row.index("</summary>")]
    assert "turn(s)" not in meta
    assert "&lt;1m" not in meta
    assert not re.search(r"\d+h \d+m", meta)
    # The rest of line 2 still renders.
    assert "started 20" in meta
    assert "<strong>125</strong> fresh" in meta


def test_goal_less_session_keeps_single_honest_chip_and_usage_note_dash(tmp_path):
    store_root = tmp_path / "state"
    # Usage-only session: no goal -> the join chip IS the honest label, once.
    _trusted_usage(store_root, session="no-goal-sess-0001", started_at=time.time() - 60)
    # Sections-only session keeps the not-a-zero-cost usage note dash.
    _record_section(store_root, section_id="ghost", title="Sections only work", session="sections-sess-001")
    client = _client(store_root)

    html = _sessions_html(client)
    no_goal_row = _row_for(html, "ess-0001")
    summary = no_goal_row[: no_goal_row.index("</summary>")]
    assert 'class="session-goal"' not in summary
    assert summary.count("No MCP context") == 1

    sections_row = _row_for(html, "sess-001")
    assert "Sections recorded; no usage imported" in sections_row
    assert (
        "— <span class=\"note\">usage unknown for this session — no imported usage rows matched; "
        "this is not a zero-cost claim</span>" in sections_row
    )
    assert '<span class="session-goal">Sections only work</span>' in sections_row


def test_overview_feed_deduplicates_a_session_with_named_work(tmp_path):
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

    overview = client.get("/").text
    assert overview.count('<article class="work-feed-item">') == 1
    card_start = overview.index('<article class="work-feed-item">')
    task_card = overview[card_start : overview.index("</article>", card_start)]
    assert re.search(
        r'<h3 class="work-feed-title"><a class="task-title-link" href="/tasks/task_[0-9a-f]{32}">Preview goal</a></h3>',
        task_card,
    )
    assert "1 root chat · 1 work step" in task_card
    assert "Codex · updated" in task_card
    assert task_card.count("175 total tokens") == 1
    assert "cache split partial" in task_card
    assert ">Codex task</a>" not in overview
    assert '<details class="session-row">' not in overview


# ---------------------------------------------------------------------------
# Phase 7 Work-home summary
# ---------------------------------------------------------------------------


def test_work_home_summarizes_work_before_usage(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    # Attributed root (ONE section — a second section in the same session
    # would make it ambiguous) + its subagent child + a usage-only session +
    # a sections-only session with an active section.
    _trusted_usage(store_root, session="kpi-root-sess-001", session_kind="root", started_at=now - 60)
    _record_section(store_root, section_id="kpi-done", title="Completed work", session="kpi-root-sess-001")
    _trusted_usage(
        store_root,
        session="kpi-root-sess-001:child",
        session_kind="child",
        parent_session="kpi-root-sess-001",
        started_at=now - 50,
    )
    _trusted_usage(store_root, session="kpi-plain-sess-01", started_at=now - 40)
    _record_section(
        store_root, section_id="kpi-active", title="Active work", session="kpi-sections-sess1", status="started"
    )
    client = _client(store_root)

    overview = client.get("/").text
    intro = overview[overview.index('<section class="work-overview"') : overview.index('id="work-feed"')]

    assert "1 task in progress" in intro
    assert '<strong>1</strong><span>In progress</span>' in intro
    assert '<strong>0</strong><span>Open findings</span>' in intro
    assert '<strong>0</strong><span>Needs input</span>' in intro
    assert "Completed work" in overview
    assert "Active work" in overview
    assert overview.index('class="work-overview"') < overview.index('id="work-feed"')
    assert "Named work" not in overview
    assert "Recent agent activity" not in overview
    assert '<a class="see-all" href="/sessions">View all activity →</a>' in overview
    assert overview.count("Context after install") == 0


# ---------------------------------------------------------------------------
# JSON contract + redaction
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


def test_sessions_page_redaction_sweep_with_filters(tmp_path):
    store_root = tmp_path / "state"
    session_uuid = "ab12cd34-5678-4abc-9def-0123456789ab"
    _trusted_usage(
        store_root,
        session=session_uuid,
        client="claude-code",
        model="fable-5",
        project_dir="/Users/testuser/secret-project",
        started_at=time.time() - 3600,
        turn_count=2,
    )
    _record_section(
        store_root, section_id="sweep-work", title="Sweep goal", session=session_uuid, client="claude-code"
    )
    client = _client(store_root)

    for query in ("", "?days=all&kind=all-flat", "?project=secret-project&join=attributed"):
        html = _sessions_html(client, query)
        # No absolute paths, no usernames, no full session ids outside href
        # addresses (locked decision: the namespaced work_id in /work-items
        # hrefs is an address, not display) — the 8-char short label is the
        # only id DISPLAYED, on every filter combination.
        assert "/Users/" not in html, query
        assert "testuser" not in html, query
        displayed = re.sub(r'href="[^"]*"', 'href=""', html)
        assert session_uuid not in displayed, query
    listed = _sessions_html(client)
    assert f"<code>{session_uuid[:8]}</code>" in listed
    assert "secret-project" in listed  # the safe leaf label is the display label
