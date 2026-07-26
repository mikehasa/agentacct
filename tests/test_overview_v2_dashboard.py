"""Dashboard v2 (P2) Overview surface: the metric tiles, the two server-rendered
no-JS usage charts (cumulative line + daily stacked bars with an agent / model /
agent-model breakdown selector), the per-agent tokens/cost roster line, the
top-sessions-by-cost panel, and the expandable/scrollable per-task session list.

Honesty invariants pinned here (never invent a zero, never leak a session id on
the product home, disclose held rows) match the locked owner principles. All
stores are throwaway tmp_path stores; the suite conftest guards the real ledger.
"""

import time

from tests.test_dashboard_pages import _client, _trusted_usage

HTML_ACCEPT = {"Accept": "text/html"}

# One deterministic multi-day, multi-client, multi-model workload. Each row's
# cache-inclusive total is input + output + cached_input.
_SEED = (
    ("claude-code", "claude-opus-4", 5000, 1200, 3000, 0.42),
    ("claude-code", "claude-sonnet-4", 2000, 800, 1000, 0.11),
    ("codex", "gpt-5.5", 3000, 900, 1500, 0.20),
    ("codex", "gpt-5-mini", 1200, 300, 400, 0.03),
    ("hermes", "hermes-large", 800, 200, 0, 0.02),
)


def _seed_multiday(store_root, *, days=6):
    # Anchor each seeded day at LOCAL NOON, starting YESTERDAY, so the measured-
    # day count is deterministic regardless of the wall-clock time-of-day and any
    # window/bucket day boundary. Seeding relative to a bare time.time() with a
    # today-anchored first day let the newest day fall outside the 30-day window
    # near midnight / a date rollover (local window date vs UTC bucket date can
    # disagree by a day), which non-deterministically dropped the count from 6 to
    # 5. A 1-day near buffer (start yesterday) + noon + a wide far buffer keep all
    # `days` days inside the window and on distinct days.
    from datetime import date as _date, datetime as _datetime, time as _time, timedelta as _timedelta

    first_noon = _datetime.combine(_date.today() - _timedelta(days=1), _time(12, 0))
    index = 0
    for day in range(days):
        day_noon = (first_noon - _timedelta(days=day)).timestamp()
        for client_name, model, inp, out, cached, cost in _SEED:
            index += 1
            _trusted_usage(
                store_root,
                session=f"s-{client_name}-{model}-{day}-{index}",
                client=client_name,
                model=model,
                input_tokens=inp,
                output_tokens=out,
                cached_input_tokens=cached,
                cost=cost,
                started_at=day_noon - index * 60,
            )


def test_overview_v2_metric_tiles_and_charts_render(tmp_path):
    store_root = tmp_path / "state"
    _seed_multiday(store_root)
    html = _client(store_root).get("/", headers=HTML_ACCEPT).text

    # Four honest summary tiles. Tokens = 6 days * 21,300/day = 127,800 → 127.8K;
    # cost = 6 * 0.78 = $4.68. Numbers are derived, not invented.
    assert '<div class="metric-grid ov-metric-grid">' in html
    assert '<div class="label">Tracked tasks</div>' in html
    assert "<div class=\"label\">Tokens · 30d</div><div class=\"value\">127.8K</div>" in html
    assert "<div class=\"label\">Est. cost · 30d</div><div class=\"value\">$4.68</div>" in html
    assert "<div class=\"label\">Active agents</div><div class=\"value\">3</div>" in html

    # Cumulative line chart: a real path (not an empty state) and per-day hover.
    assert 'id="ov-usage-line"' in html
    assert '<path class="ov-line-path"' in html
    assert "No saved usage rows in this window yet" not in html

    # Daily stacked bars: real chart-bar rects and the daily-usage subhead. The
    # default (agent) breakdown chart carries the ov-usage-bars-agent id; all
    # three breakdown charts render up front for the CSS-only tab selector.
    assert 'id="ov-usage-bars-agent"' in html
    assert '<rect class="chart-bar"' in html
    assert "Token usage over time" in html


def test_overview_v2_breakdown_tabs_are_css_only_and_render_all_series(tmp_path):
    store_root = tmp_path / "state"
    _seed_multiday(store_root)
    html = _client(store_root).get("/", headers=HTML_ACCEPT).text

    # Selector is CSS-only: visually-hidden radios + labels, NOT query-param links.
    assert 'href="/?usage_breakdown=' not in html
    assert '<input type="radio" name="ov-usage-breakdown" id="ov-bd-agent" class="ov-utab-radio" checked>' in html
    assert '<input type="radio" name="ov-usage-breakdown" id="ov-bd-model" class="ov-utab-radio">' in html
    assert '<input type="radio" name="ov-usage-breakdown" id="ov-bd-agent-model" class="ov-utab-radio">' in html
    assert '<label class="ov-utab" for="ov-bd-agent"' in html
    assert '<label class="ov-utab" for="ov-bd-model">By model</label>' in html
    # The :checked ~ sibling rules do the switching — no JS anywhere.
    assert "<script" not in html.lower()
    assert ".ov-usage-bars #ov-bd-model:checked ~ .ov-utab-panel-model" in html

    # ALL THREE breakdown series render up front (CSS reveals one at a time):
    # agent legend, model legend, and agent-model composite labels all present.
    for label in ("Claude Code", "Codex", "Hermes"):
        assert f'</span>{label}</span>' in html
    assert "fill:var(--lane-claude-code)" in html
    assert "claude-opus-4" in html
    assert "claude-sonnet-4" in html
    assert "opacity:" in html
    assert "Claude Code · claude-opus-4" in html
    # One panel per breakdown, each with its own chart id.
    assert 'id="ov-usage-bars-agent"' in html
    assert 'id="ov-usage-bars-model"' in html
    assert 'id="ov-usage-bars-agent-model"' in html


def test_overview_v2_breakdown_deep_link_sets_the_default_checked_radio(tmp_path):
    """The old ?usage_breakdown= link coexists: it only chooses which radio starts
    checked, so an existing deep link opens on the right tab (then CSS switches)."""
    store_root = tmp_path / "state"
    _seed_multiday(store_root)
    client = _client(store_root)

    model_html = client.get("/?usage_breakdown=model", headers=HTML_ACCEPT).text
    assert '<input type="radio" name="ov-usage-breakdown" id="ov-bd-model" class="ov-utab-radio" checked>' in model_html
    assert '<input type="radio" name="ov-usage-breakdown" id="ov-bd-agent" class="ov-utab-radio">' in model_html

    # An unknown breakdown falls back to the agent radio (page-wide _safe_choice).
    bogus_html = client.get("/?usage_breakdown=bogus", headers=HTML_ACCEPT).text
    assert '<input type="radio" name="ov-usage-breakdown" id="ov-bd-agent" class="ov-utab-radio" checked>' in bogus_html


def test_overview_v2_roster_shows_per_agent_tokens_and_cost(tmp_path):
    store_root = tmp_path / "state"
    _seed_multiday(store_root)
    html = _client(store_root).get("/", headers=HTML_ACCEPT).text

    # Each roster card carries a tokens/cost line sourced from the scoped groups.
    assert html.count('<div class="agent-nums">') == 3
    # Claude Code total incl. cache = 6 * (9200 + 3800) = 78,000 → 78K.
    assert "<strong>78K</strong> tok" in html
    # Cost is shown as a real figure, never a fabricated $0.
    assert "$0.00" not in html


def test_overview_v2_top_sessions_ranked_by_cost(tmp_path):
    store_root = tmp_path / "state"
    _seed_multiday(store_root)
    html = _client(store_root).get("/", headers=HTML_ACCEPT).text

    assert "Top root chats by est. cost" in html
    rows = html.count('class="ov-topsess-row"')
    assert rows >= 1
    # The most expensive root chat (claude-opus, $0.42) leads the panel.
    topsess_start = html.index("Top root chats by est. cost")
    topsess = html[topsess_start : topsess_start + 4000]
    assert "$0.42" in topsess
    assert "$0.00" not in topsess


def test_overview_v2_expandable_sessions_use_scroll_not_session_row(tmp_path):
    store_root = tmp_path / "state"
    _seed_multiday(store_root, days=2)
    html = _client(store_root).get("/", headers=HTML_ACCEPT).text

    # Each task card can expand to a scrollable per-session list.
    assert '<details class="ov-task-sessions">' in html
    assert '<div class="task-work-overflow ov-sess-scroll">' in html
    assert '<div class="ov-sess-row">' in html
    # The /sessions-only marker must never appear on the product home.
    assert '<details class="session-row">' not in html


def test_overview_v2_section_order_matches_approved_layout(tmp_path):
    store_root = tmp_path / "state"
    _seed_multiday(store_root, days=2)
    html = _client(store_root).get("/", headers=HTML_ACCEPT).text

    order = [
        '<section class="work-overview"',
        '<div class="metric-grid ov-metric-grid">',
        '<section class="usage-pulse"',
        '<section class="section ov-usage"',
        '<section class="agent-board"',
        'id="work-feed" aria-label="Recent activity"',
    ]
    positions = [html.index(marker) for marker in order]
    assert positions == sorted(positions), positions


def test_overview_v2_empty_store_charts_show_labeled_state_not_zero(tmp_path):
    store_root = tmp_path / "state"
    # No usage rows at all: the charts must render an explicit empty state and
    # never a fabricated zero token/cost claim.
    response = _client(store_root).get("/", headers=HTML_ACCEPT)
    assert response.status_code == 200
    html = response.text
    assert "No saved usage rows in this window yet" in html
    assert '<rect class="chart-bar"' not in html
    assert "$0.00" not in html
    # The cost tile is honest about the absence.
    assert "No estimate" in html or "No priced usage" in html


def test_overview_v2_cost_tile_labels_partial_and_known_subtotal(tmp_path):
    # Priced + unpriced additive rows (no held): a real but incomplete estimate
    # must be labeled "Partial est. cost", never presented as a complete total.
    partial_store = tmp_path / "partial"
    now = time.time()
    _trusted_usage(partial_store, session="priced", client="claude-code", model="m1",
                   input_tokens=1000, output_tokens=200, cached_input_tokens=100, cost=0.5, started_at=now - 3600)
    _trusted_usage(partial_store, session="unpriced", client="claude-code", model="m2",
                   input_tokens=1000, output_tokens=200, cached_input_tokens=100, cost=None, started_at=now - 7200)
    partial_html = _client(partial_store).get("/", headers=HTML_ACCEPT).text
    assert '<div class="label">Est. cost · 30d</div><div class="value">$0.50</div><div class="note">Partial est. cost</div>' in partial_html

    # Priced additive row + a held (non-additive) row: the complete estimate is
    # unavailable, so the tile shows the priced portion labeled "Known subtotal".
    known_store = tmp_path / "known"
    _trusted_usage(known_store, session="priced-add", client="claude-code", model="m1",
                   input_tokens=2000, output_tokens=400, cached_input_tokens=200, cost=1.25, started_at=now - 3600)
    _trusted_usage(known_store, session="held", client="codex", session_kind="internal",
                   parent_session="missing-parent", input_tokens=9_000_000_000, output_tokens=1_000_000_000,
                   cached_input_tokens=0, cost=999.0, started_at=now - 7200)
    known_html = _client(known_store).get("/", headers=HTML_ACCEPT).text
    assert '<div class="label">Est. cost · 30d</div><div class="value">$1.25</div><div class="note">Known subtotal</div>' in known_html


def test_overview_v2_held_only_window_tokens_tile_is_not_a_fabricated_zero(tmp_path):
    store_root = tmp_path / "state"
    # Every in-window row is held (non-additive): the token total is UNKNOWN,
    # not zero. The tile must not render "0" with a completeness-implying note.
    _trusted_usage(store_root, session="held-only", client="codex", session_kind="internal",
                   parent_session="missing-parent", input_tokens=5_000_000_000, output_tokens=1_000_000_000,
                   cached_input_tokens=0, cost=500.0, started_at=time.time() - 3600)
    html = _client(store_root).get("/", headers=HTML_ACCEPT).text
    assert '<div class="label">Tokens · 30d</div><div class="value">—</div>' in html
    tile_start = html.index('<div class="label">Tokens · 30d</div>')
    tile = html[tile_start : tile_start + 260]
    assert "held" in tile
    assert '<div class="value">0</div>' not in tile
    assert "$5" not in html  # the held codex row's cost never leaks into a total


def test_overview_v2_line_title_measured_days_matches_visible_count(tmp_path):
    store_root = tmp_path / "state"
    _seed_multiday(store_root, days=6)
    html = _client(store_root).get("/", headers=HTML_ACCEPT).text
    line_start = html.index('id="ov-usage-line"')
    line = html[line_start : html.index("</figure>", line_start)]
    # 6 real days of usage in a gap-filled 30-day window: the accessible title
    # must say the measured-day count, not the ~30 gap-filled calendar days.
    assert "6 measured days" in line
    assert "30 measured days" not in line
    # The visible subhead and the chart title agree on the measured-day count.
    assert "6 measured days in this window" in html


def test_overview_v2_held_rows_are_disclosed_not_charted_as_zero(tmp_path):
    store_root = tmp_path / "state"
    # A priced additive workload plus one held (non-additive) codex descendant.
    _trusted_usage(
        store_root,
        session="additive-a",
        client="claude-code",
        model="claude-opus-4",
        input_tokens=4000,
        output_tokens=1000,
        cached_input_tokens=2000,
        cost=0.30,
        started_at=time.time() - 3600,
    )
    _trusted_usage(
        store_root,
        session="held-child",
        client="codex",
        session_kind="internal",
        parent_session="missing-parent",
        input_tokens=9_000_000_000,
        output_tokens=2_000_000_000,
        cached_input_tokens=0,
        cost=999.0,
        started_at=time.time() - 7200,
    )
    html = _client(store_root).get("/", headers=HTML_ACCEPT).text

    # The held row is disclosed in words on the charts, never summed or drawn.
    assert "usage row(s) held" in html
    # The held codex row's astronomical count never becomes a bar or a total.
    assert "$999" not in html


def test_overview_v2_charts_have_instant_css_hover_tooltips_no_js(tmp_path):
    store_root = tmp_path / "state"
    _seed_multiday(store_root, days=4)
    html = _client(store_root).get("/", headers=HTML_ACCEPT).text
    # The dashboard CSP forbids all JavaScript, so hover is pure CSS: a
    # full-height hit zone + crosshair + tooltip per day, revealed by a
    # :hover rule. There must be no <script> anywhere on the product home.
    assert "<script" not in html.lower()
    assert 'class="ovh-hit"' in html
    assert 'class="ovh-tip"' in html
    assert ".ovh:hover .ovh-cross, .ovh:hover .ovh-dot, .ovh:hover .ovh-tip" in html
    # Bar-chart tooltip lists each series that day plus a Total row (inspect the
    # default agent-breakdown chart; all three render for the CSS-only tabs).
    bars = html[html.index('id="ov-usage-bars-agent"') : html.index("</figure>", html.index('id="ov-usage-bars-agent"'))]
    assert ">Total<" in bars
    assert ">Claude Code<" in bars
    # Line-chart tooltip carries the daily + cumulative figures and a hover dot.
    line = html[html.index('id="ov-usage-line"') : html.index("</figure>", html.index('id="ov-usage-line"'))]
    assert ">This day<" in line
    assert ">Cumulative<" in line
    assert 'class="ovh-dot"' in line


def test_overview_v2_bar_tooltip_folds_long_series_and_keeps_total(tmp_path):
    import re

    store_root = tmp_path / "state"
    now = time.time()
    # Many models active on the same day would overflow the tooltip; it must
    # fold the tail into "+K more" and NEVER drop the Total row.
    for i in range(10):
        _trusted_usage(store_root, session=f"m{i}", client="codex", model=f"model-{i:02d}",
                       input_tokens=1000 * (i + 1), output_tokens=100, cached_input_tokens=50,
                       cost=0.01 * (i + 1), started_at=now - 3600 - i * 30)
    html = _client(store_root).get("/?usage_breakdown=model", headers=HTML_ACCEPT).text
    # The many models live in the by-model chart; inspect that panel directly.
    bars = html[html.index('id="ov-usage-bars-model"') : html.index("</figure>", html.index('id="ov-usage-bars-model"'))]
    assert re.search(r">\+\d+ more<", bars)
    assert ">Total<" in bars


def test_overview_v2_flag_off_body_never_marks_canonical(tmp_path):
    store_root = tmp_path / "state"
    _seed_multiday(store_root, days=2)
    html = _client(store_root).get("/", headers=HTML_ACCEPT).text
    # The read flag is off (suite default); the v1 body must stay unlabeled.
    assert "data-canonical-read" not in html


def test_overview_v2_model_breakdown_caps_series_with_honest_other_bucket(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    # More models than the series cap: the tail must fold into one honestly
    # labeled "Other models (N)" lane, never be silently dropped.
    for i in range(11):
        _trusted_usage(
            store_root,
            session=f"m{i}",
            client="codex",
            model=f"model-{i:02d}",
            input_tokens=1000 * (i + 1),
            output_tokens=100,
            cached_input_tokens=50,
            cost=0.01 * (i + 1),
            started_at=now - i * 3600,
        )
    html = _client(store_root).get("/?usage_breakdown=model", headers=HTML_ACCEPT).text
    assert "Other models (3)" in html


def test_overview_v2_bar_chart_axis_labels_are_compact(tmp_path):
    store_root = tmp_path / "state"
    # A multi-billion-token day forces a 10B y-axis ceiling; the label must be
    # the compact "10B", never the 11-digit integer that would overflow the
    # chart's small left gutter.
    _trusted_usage(
        store_root,
        session="huge-day",
        client="claude-code",
        model="claude-opus-4",
        input_tokens=6_000_000_000,
        output_tokens=1_000_000_000,
        cached_input_tokens=1_000_000_000,
        cost=120.0,
        started_at=time.time() - 3600,
    )
    html = _client(store_root).get("/", headers=HTML_ACCEPT).text
    bars_start = html.index('id="ov-usage-bars-agent"')
    bars = html[bars_start : html.index("</figure>", bars_start)]
    assert 'class="chart-axis-label"' in bars
    assert ">10B</text>" in bars
    assert "10,000,000,000" not in bars


def test_overview_v2_dark_mode_side_panels_win_over_light_base(tmp_path):
    store_root = tmp_path / "state"
    html = _client(store_root).get("/", headers=HTML_ACCEPT).text
    # The dark-mode override for the usage-pulse / agent-board copy panels must
    # use descendant selectors so it beats the later light-gradient base rules
    # regardless of stylesheet source order (otherwise the panels render as an
    # unreadable light block in dark mode).
    assert ".agent-board .agent-board-copy" in html
    assert ".usage-pulse .usage-pulse-copy" in html
