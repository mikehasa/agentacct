"""Phase 2 adversarial-review fix-round regressions.

Covers the five confirmed majors and the fixed minors:

[0] a session row may claim a bare "Attributed" chip ONLY when every usage
    row in the session is attributed; mixed sessions render "Partially
    attributed" with a visible per-session coverage line;
[1] a transcript-conflict VETO is canonically unjoined — the rollup state and
    session chip must never claim "Context matched";
[2] one-level nesting: deep/cyclic/self-parent lineage never vanishes from
    the Sessions view (property-tested invariant);
[3] ambiguous join reasons are id-free in every rendered surface (full ids
    are JSON-only / href-only);
[4] proxy and diagnostic usage rows group per (client, day) in the default
    timeline view like import rows, so sections cannot be drowned;
plus minors: collision-suffixed labels for attention examples and parent
references, fresh-first blind-spot figures, "~" for home-directory project
labels, raw-tab short session labels, cache-triple reconciliation,
out-of-range timestamp guard, dead DashboardUsageView fields removed.

All stores are throwaway tmp_path stores (suite conftest guards the real
dogfood ledger)."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

import agent_chronicle.api as api_module
from agent_chronicle.api import _fmt_time, _top_level_session_keys, create_local_api_app
from agent_chronicle.cost import CostLedger, UsageEstimate
from agent_chronicle.service import SentinelService
from agent_chronicle.work_ledger import build_work_ledger


def _trusted_usage(
    store_root,
    *,
    session,
    client="codex",
    transcript=None,
    parent_session=None,
    session_kind=None,
    input_tokens=100,
    output_tokens=25,
    cached_input_tokens=0,
    cache_creation_tokens=None,
    cache_read_tokens=None,
    cost=0.01,
    project_dir=None,
    updated_at=None,
    model="gpt-5.5",
):
    metadata = {
        "usage_source": "local_client_session_store",
        "client": client,
        "client_session_id": session,
        "cached_input_tokens": cached_input_tokens,
    }
    if transcript is not None:
        metadata["client_transcript_id"] = transcript
    if parent_session is not None:
        metadata["parent_client_session_id"] = parent_session
    if session_kind is not None:
        metadata["client_session_kind"] = session_kind
    if project_dir is not None:
        metadata["project_dir"] = project_dir
    if updated_at is not None:
        metadata["updated_at"] = updated_at
    if cache_creation_tokens is not None:
        metadata["cache_creation_input_tokens"] = cache_creation_tokens
    if cache_read_tokens is not None:
        metadata["cache_read_input_tokens"] = cache_read_tokens
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


def _record_section(store_root, *, section_id, title, session, client="codex", transcript=None, status="completed"):
    metadata = {
        "sentinel_semantic_kind": "section",
        "section_id": section_id,
        "section_status": status,
        "section_title": title,
        "client": client,
        "client_session_id": session,
    }
    if transcript is not None:
        metadata["client_transcript_id"] = transcript
    return SentinelService(store_root).record_event(
        {"source": client, "event_type": f"section_{status}", "metadata": metadata}
    )


def _app_client(store_root):
    return TestClient(create_local_api_app(store_dir=store_root))


def _product_html(store_root):
    # Phase 3.5a route split: session rows, work items, attention,
    # reconciliation, and ledger insights live on the /sessions page
    # (Accept-negotiated HTML; the bare path stays the JSON rollup).
    response = _app_client(store_root).get("/sessions", headers={"Accept": "text/html"})
    assert response.status_code == 200
    return response.text


def _sessions_slice(html):
    return html[html.index('id="sessions"') : html.index('id="work-items"')]


def _ledger(store_root):
    return build_work_ledger(SentinelService(store_root).list_all_events(), run_reports=[], cost_events=[])


def _rollup_entry(ledger, client, session):
    for entry in ledger["session_rollup"]["sessions"]:
        if entry["client"] == client and entry["client_session_id"] == session:
            return entry
    raise AssertionError(f"no rollup entry for {client}::{session}")


# ---------------------------------------------------------------------------
# [0] partial attribution coverage on the session row
# ---------------------------------------------------------------------------


def _seed_partial_attribution_store(store_root):
    """1 attributed row + 99 conflict-vetoed rows in ONE session."""

    _record_section(
        store_root,
        section_id="the-real-goal",
        title="The one real goal",
        session="partial-session",
        transcript="tx-real",
    )
    _trusted_usage(
        store_root,
        session="partial-session",
        transcript="tx-real",
        input_tokens=15,
        output_tokens=5,
        cost=0.001,
    )
    for index in range(99):
        _trusted_usage(
            store_root,
            session="partial-session",
            transcript=f"tx-other-{index:03d}",
            input_tokens=15000,
            output_tokens=0,
            cost=1.0,
        )


def test_mixed_session_renders_partially_attributed_with_coverage_line(tmp_path):
    store_root = tmp_path / "state"
    _seed_partial_attribution_store(store_root)

    ledger = _ledger(store_root)
    entry = _rollup_entry(ledger, "codex", "partial-session")
    assert entry["join"]["state"] == "attributed"
    assert entry["join"]["row_states"]["attributed"] == 1
    assert entry["join"]["row_states"]["unjoined"] == 99
    assert entry["join"]["attributed_fresh_tokens"] == 20

    html = _product_html(store_root)
    sessions_html = _sessions_slice(html)

    # The chip is qualified and the coverage is visible text, not a tooltip.
    assert ">Partially attributed</span>" in sessions_html
    assert "1 of 100 rows · 20 fresh tokens attributed" in sessions_html
    # NEVER a bare "Attributed" chip next to whole-session totals.
    assert ">Attributed</span>" not in sessions_html
    # Whole-session figures stay (exact-key session truth).
    assert "1,485,020" in sessions_html


def test_fully_attributed_session_keeps_the_bare_attributed_chip(tmp_path):
    store_root = tmp_path / "state"
    _record_section(store_root, section_id="full-goal", title="Fully joined goal", session="full-session")
    _trusted_usage(store_root, session="full-session", input_tokens=40, output_tokens=10)

    html = _product_html(store_root)
    sessions_html = _sessions_slice(html)

    assert ">Attributed</span>" in sessions_html
    assert "Partially attributed" not in sessions_html
    assert "rows ·" not in sessions_html  # no coverage line when coverage is total


# ---------------------------------------------------------------------------
# [1] veto is unjoined, never "context matched"
# ---------------------------------------------------------------------------


def test_conflict_vetoed_session_state_is_unjoined_with_veto_reason(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="veto-session-001", transcript="transcript-USAGE")
    _record_section(
        store_root,
        section_id="veto-work",
        title="Vetoed transcript work",
        session="veto-session-001",
        transcript="transcript-WORK",
    )

    ledger = _ledger(store_root)
    attribution = ledger["attributions"][0]
    assert attribution["join_strategy"] == "unjoined"
    assert "conflicting evidence vetoes the join" in attribution["join_reason"]

    entry = _rollup_entry(ledger, "codex", "veto-session-001")
    # The rollup mirrors the canonical decision: no context_only mislabel,
    # machine-consistent row_states, and the honest veto reason.
    assert entry["join"]["state"] == "unjoined"
    assert entry["join"]["row_states"] == {
        "attributed": 0,
        "ambiguous": 0,
        "context_matched_unallocated": 0,
        "unjoined": 1,
    }
    assert "conflicting evidence vetoes the join" in entry["join"]["reason"]


def test_conflict_vetoed_session_chip_never_claims_a_match(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="veto-session-001", transcript="transcript-USAGE")
    _record_section(
        store_root,
        section_id="veto-work",
        title="Vetoed transcript work",
        session="veto-session-001",
        transcript="transcript-WORK",
    )

    html = _product_html(store_root)
    sessions_html = _sessions_slice(html)
    work_html = html[html.index('id="work-items"') : html.index('id="attention"')]

    # Session chip reads as unjoined; the veto reason travels on the title.
    assert ">Not attributed</span>" in sessions_html
    assert "Context matched" not in sessions_html
    assert "conflicting evidence vetoes the join" in sessions_html
    # The work-items table on the SAME page agrees.
    assert ">Unattributed</span>" in work_html
    assert "Context matched" not in work_html


def test_actual_context_match_still_renders_context_only(tmp_path):
    store_root = tmp_path / "state"
    # run_id grouping hint: a REAL non-vetoed context match that never
    # allocates — this is what context_only is for. (Different sessions, so
    # the context-matched session renders as its own top-level row.)
    SentinelService(store_root).record_event(
        {
            "source": "codex",
            "event_type": "section_completed",
            "run_id": "run-ctx-hint",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "hint-work",
                "section_status": "completed",
                "section_title": "Run-grouped work",
                "client": "codex",
                "client_session_id": "hint-work-session",
            },
        }
    )
    SentinelService(store_root).record_event(
        {
            "source": "codex-local-session-import",
            "event_type": "model_usage",
            "provider": "codex",
            "model": "gpt-5.5",
            "run_id": "run-ctx-hint",
            "estimated_input_tokens": 10,
            "estimated_output_tokens": 5,
            "usage_confidence": "client_reported",
            "cost_confidence": "estimated_from_tokens",
            "metadata": {
                "usage_source": "local_client_session_store",
                "client": "codex",
                "client_session_id": "hint-usage-session",
                "cached_input_tokens": 0,
            },
        },
        trusted_usage_import=True,
    )

    ledger = _ledger(store_root)
    entry = _rollup_entry(ledger, "codex", "hint-usage-session")
    assert entry["join"]["state"] == "context_only"
    assert entry["join"]["row_states"]["context_matched_unallocated"] == 1

    sessions_html = _sessions_slice(_product_html(store_root))
    assert "Context matched; not allocated" in sessions_html


# ---------------------------------------------------------------------------
# [2] one-level nesting: nothing vanishes
# ---------------------------------------------------------------------------


def test_grandchild_session_renders_top_level_with_child_of_chip(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="lineage-root", client="claude-code", session_kind="root", input_tokens=100, output_tokens=10)
    _trusted_usage(
        store_root,
        session="lineage-child",
        client="claude-code",
        session_kind="child",
        parent_session="lineage-root",
        input_tokens=200,
        output_tokens=20,
    )
    _trusted_usage(
        store_root,
        session="lineage-grandchild",
        client="claude-code",
        session_kind="child",
        parent_session="lineage-child",
        input_tokens=40000,
        output_tokens=4000,
        cost=3.0,
    )

    html = _product_html(store_root)
    sessions_html = _sessions_slice(html)

    # Root and grandchild render as rows; the child nests under the root.
    assert sessions_html.count('<details class="session-row">') == 2
    assert "<code>lineage-</code>" not in sessions_html  # labels are suffixed on collision
    # The grandchild's spend is visible in its own row, not vanished.
    assert "<strong>44,000</strong> fresh" in sessions_html
    assert "$3.00" in sessions_html
    # The grandchild names its (unrendered) parent.
    assert "child of " in sessions_html


def test_parent_cycle_members_all_render_top_level(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="cycle-aaaa", parent_session="cycle-bbbb", input_tokens=10, output_tokens=1)
    _trusted_usage(store_root, session="cycle-bbbb", parent_session="cycle-aaaa", input_tokens=20, output_tokens=2)

    html = _product_html(store_root)
    sessions_html = _sessions_slice(html)

    assert sessions_html.count('<details class="session-row">') == 2
    # codex labels keep the distinctive tail (UUIDv7 rule).
    assert "<code>cle-aaaa</code>" in sessions_html
    assert "<code>cle-bbbb</code>" in sessions_html


def test_self_parent_session_renders_and_never_duplicates_its_own_usage(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="self-parent-session", parent_session="self-parent-session", input_tokens=9, output_tokens=1)

    ledger = _ledger(store_root)
    entry = _rollup_entry(ledger, "codex", "self-parent-session")
    # Data level: the corrupt pointer is dropped with an honest note, so the
    # session's own usage can never reappear as a "descendants" subtotal.
    assert entry["related"]["parent"] is None
    assert entry["related"]["note"] == "self-referencing parent pointer ignored"
    assert entry["related"]["children_usage"] is None

    sessions_html = _sessions_slice(_product_html(store_root))
    assert sessions_html.count('<details class="session-row">') == 1
    assert "subagent session(s) — not allocated" not in sessions_html


def test_top_level_invariant_nothing_vanishes_nothing_double_nests(tmp_path):
    """Property: every rollup entry either renders as its own top-level row
    or is the direct child of exactly one rendered top-level row — across
    chains, cycles, self-parents, orphans, and forests."""

    store_root = tmp_path / "state"
    lineage = {
        # depth-4 chain
        "chain-0": None,
        "chain-1": "chain-0",
        "chain-2": "chain-1",
        "chain-3": "chain-2",
        # 3-cycle with a tail hanging off it
        "cyc-a": "cyc-c",
        "cyc-b": "cyc-a",
        "cyc-c": "cyc-b",
        "cyc-tail": "cyc-a",
        # self-parent, orphan parent, plain root with two children
        "selfie": "selfie",
        "orphan-child": "never-recorded-parent",
        "forest-root": None,
        "forest-kid-1": "forest-root",
        "forest-kid-2": "forest-root",
    }
    for session, parent in lineage.items():
        _trusted_usage(store_root, session=session, parent_session=parent, input_tokens=10, output_tokens=1)

    ledger = _ledger(store_root)
    rollup_sessions = ledger["session_rollup"]["sessions"]
    top_keys = _top_level_session_keys(rollup_sessions)
    entry_keys = {(entry["client"], entry["client_session_id"]) for entry in rollup_sessions}

    assert len(rollup_sessions) == len(lineage)
    for entry in rollup_sessions:
        key = (entry["client"], entry["client_session_id"])
        parent = entry["related"]["parent"]
        if key in top_keys:
            continue
        # Nested: must have a parent that IS a rendered top-level entry.
        assert parent is not None, key
        parent_key = (entry["client"], parent["client_session_id"])
        assert parent_key in entry_keys, key
        assert parent_key in top_keys, key
    # Identity never vanishes, while only the two parentless roots have
    # independently additive Codex usage. Parent-linked rows are held rather
    # than replayed into the aggregate.
    assert ledger["session_rollup"]["summary"]["totals"]["fresh_tokens"] == 22
    assert ledger["insights"]["usage_additivity_quarantine"]["excluded_rows"] == len(lineage) - 2

    # Rendered check: the rows on the page are exactly the top-level keys.
    sessions_html = _sessions_slice(_product_html(store_root))
    assert sessions_html.count('<details class="session-row">') == len(top_keys)


# ---------------------------------------------------------------------------
# [3] ambiguous reasons are id-free everywhere rendered
# ---------------------------------------------------------------------------

AMBIG_UUID = "0d9f31c2-7b44-4e02-9a55-1234deadbeef"


def test_ambiguous_store_renders_no_full_session_id_anywhere(tmp_path):
    store_root = tmp_path / "state"
    _record_section(store_root, section_id="ambig-a", title="Ambiguous section A", session=AMBIG_UUID)
    _record_section(store_root, section_id="ambig-b", title="Ambiguous section B", session=AMBIG_UUID)
    _trusted_usage(store_root, session=AMBIG_UUID)

    ledger = _ledger(store_root)
    attribution = ledger["attributions"][0]
    assert attribution["join_strategy"] == "exact_client_session_id_ambiguous_sections"
    # Reason: counts + key name only; candidate ids stay JSON-only.
    assert attribution["join_reason"] == (
        "usage shares client_session_id with 2 sections; not allocating to a section"
    )
    assert len(attribution["ambiguous_candidate_work_ids"]) == 2
    assert all(AMBIG_UUID in work_id for work_id in attribution["ambiguous_candidate_work_ids"])

    client = _app_client(store_root)
    for target in ("/", "/?tab=raw"):
        html = client.get(target).text
        # Strip href attributes: full ids are allowed ONLY as addresses.
        displayed = re.sub(r'href="[^"]*"', 'href=""', html)
        assert AMBIG_UUID not in displayed, target
    # The id-free reason renders in the full Sessions attribution inspector,
    # not the compact Work home.
    sessions_html = client.get("/sessions", headers={"Accept": "text/html"}).text
    assert "usage shares client_session_id with 2 sections" in sessions_html


# ---------------------------------------------------------------------------
# [4] proxy rows group in the default timeline view
# ---------------------------------------------------------------------------


def _timeline_slice(html):
    start = html.find("<h2>Work Timeline</h2>")
    end = html.find("Agent source discovery", start)
    assert start != -1 and end != -1
    return html[start:end]


def test_grouped_timeline_groups_proxy_budget_decisions(tmp_path):
    store_root = tmp_path / "state"
    for name in ("alpha", "beta", "gamma"):
        _record_section(store_root, section_id=f"proxy-flood-{name}", title=f"Proxy flood section {name}", session=f"pf-{name}")
    cost_ledger = CostLedger(store_root)
    for index in range(100):
        cost_ledger.record_usage(
            UsageEstimate(
                provider="openrouter",
                model="openai/gpt-4o-mini",
                endpoint="/openrouter/v1/chat/completions",
                estimated_input_tokens=2,
                estimated_output_tokens=1,
                estimated_cost_usd=0.000001,
            ),
            run_id=f"run_proxy_{index}",
            decision="allowed",
        )

    raw_html = _app_client(store_root).get("/?tab=raw").text
    timeline_html = _timeline_slice(raw_html)

    # Sections stay visible despite 100 NEWER budget decisions.
    for name in ("alpha", "beta", "gamma"):
        assert f"Proxy flood section {name}" in timeline_html
    # One labeled group row instead of 100 passthrough rows.
    assert "Proxy usage — 100 budget decision(s)" in timeline_html
    assert timeline_html.count("Proxy budget decision<") == 0
    # Group rows show labeled fresh tokens and honest join counts.
    assert "300 fresh" in timeline_html
    assert "0 attributed / 100 not attributed" in timeline_html
    # Nothing truncated in the grouped view, so no cap note may render.
    assert "timeline entries." not in raw_html
    # Row-level rows remain available (and honestly capped) one click away.
    all_html = _app_client(store_root).get("/?tab=raw&timeline_view=all").text
    assert "Showing 80 of 103 timeline entries." in all_html


# ---------------------------------------------------------------------------
# minors
# ---------------------------------------------------------------------------


def test_attention_example_refs_use_collision_suffixed_labels(tmp_path):
    store_root = tmp_path / "state"
    # claude-code fixtures: codex labels are tail-based now (these ids' tails
    # differ), so the first-8 collision machinery is pinned via a first-8 client.
    _trusted_usage(store_root, session="abcdef12-1111-4000-8000-000000000001", client="claude-code")
    _trusted_usage(store_root, session="abcdef12-2222-4000-8000-000000000002", client="claude-code")

    ledger = _ledger(store_root)
    rollup_labels = {
        entry["client_session_id"]: entry["client_session_id_short"]
        for entry in ledger["session_rollup"]["sessions"]
    }
    # The rollup suffixes the first-8 collision.
    assert len(set(rollup_labels.values())) == 2
    assert all(label.startswith("abcdef12~") for label in rollup_labels.values())

    groups = ledger["attention_groups"]["groups"]
    example_labels = [ref["label"] for group in groups for ref in group["example_refs"]]
    assert len(example_labels) == 2
    # Example refs carry the SAME suffixed labels — never a bare truncation
    # that makes two different sessions indistinguishable.
    assert len(set(example_labels)) == 2
    for label in example_labels:
        assert not label.endswith(" abcdef12")
        assert any(label.endswith(suffixed) for suffixed in rollup_labels.values())

    html = _product_html(store_root)
    attention_html = html[html.index('id="attention"') : html.index('id="reconciliation"')]
    for suffixed in rollup_labels.values():
        assert suffixed in attention_html


def test_parent_reference_label_routes_through_collision_assigner(tmp_path):
    store_root = tmp_path / "state"
    # An entry whose base label collides with an UNRECORDED parent's base
    # label: both must be suffixed, never rendered identically. claude-code
    # fixtures keep the generic first-8 rule under test (codex is tail-based).
    _trusted_usage(store_root, session="collide99-aaaa-4000-8000-000000000001", client="claude-code")
    _trusted_usage(store_root, session="orphan-kid", client="claude-code", parent_session="collide99-bbbb-4000-8000-000000000002")

    ledger = _ledger(store_root)
    entry = _rollup_entry(ledger, "claude-code", "collide99-aaaa-4000-8000-000000000001")
    orphan = _rollup_entry(ledger, "claude-code", "orphan-kid")
    parent_label = orphan["related"]["parent"]["label"]
    assert parent_label != entry["client_session_id_short"]
    assert parent_label.startswith("collide9~")
    assert entry["client_session_id_short"].startswith("collide9~")


def test_blind_spot_detail_leads_with_fresh_and_labels_the_total(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="blind-spot-session", input_tokens=10, output_tokens=10, cached_input_tokens=1_000_000, cost=0.5)

    html = _product_html(store_root)
    # Ledger insights is the last /sessions section since the 3.5a split
    # (Usage Basics moved to /tokens); slice to the end of the page.
    insights_html = html[html.index('id="ledger-insights"') :]

    assert "20 fresh tokens; 1,000,020 incl. caches;" in insights_html
    # The old bare cache-inflated figure must be gone.
    assert "1,000,020 tokens;" not in insights_html


def test_home_directory_project_label_renders_tilde_not_username(tmp_path):
    store_root = tmp_path / "state"
    home = Path.home()
    _trusted_usage(store_root, session="home-cwd-session", project_dir=str(home))

    ledger = _ledger(store_root)
    entry = _rollup_entry(ledger, "codex", "home-cwd-session")
    assert entry["project"] == "~"

    html = _product_html(store_root)
    assert home.name not in html
    assert ">~<" in _sessions_slice(html)


def test_raw_tab_bridge_and_activity_cells_truncate_session_ids(tmp_path):
    store_root = tmp_path / "state"
    session_uuid = "3c60be00-1f6e-4d10-9d3d-abcdefabcdef"
    _trusted_usage(store_root, session=session_uuid)
    # Unlinked context event in a session with no usage row at all: falls
    # back to the shared base truncation, still never the full id.
    SentinelService(store_root).record_event(
        {
            "source": "codex",
            "event_type": "client_context_attached",
            "metadata": {
                "sentinel_semantic_kind": "client_context",
                "client": "codex",
                "client_session_id": "9f80c3d2-5a11-4b22-8c33-001122334455",
            },
        }
    )

    raw_html = _app_client(store_root).get("/?tab=raw").text
    displayed = re.sub(r'href="[^"]*"', 'href=""', raw_html)

    assert session_uuid not in displayed
    assert "9f80c3d2-5a11-4b22-8c33-001122334455" not in displayed
    # codex labels keep the distinctive UUIDv7 tail — on the rollup-backed
    # usage row AND on the context-only fallback path alike.
    assert "<code>efabcdef</code>" in raw_html
    assert "<code>22334455</code>" in raw_html


def test_cache_triple_reconciles_when_split_and_merged_fields_disagree(tmp_path):
    store_root = tmp_path / "state"
    # Split fields present, merged field absent (the natural FUTURE importer
    # shape): the caches must still be in total_tokens.
    _trusted_usage(
        store_root,
        session="split-no-merged",
        input_tokens=10,
        output_tokens=5,
        cached_input_tokens=0,
        cache_creation_tokens=30,
        cache_read_tokens=70,
    )
    # Split fields inconsistent with a LARGER merged figure: remainder goes
    # to cache reads (conservative bucket).
    _trusted_usage(
        store_root,
        session="split-inconsistent",
        input_tokens=10,
        output_tokens=5,
        cached_input_tokens=100,
        cache_creation_tokens=5,
        cache_read_tokens=0,
    )

    ledger = _ledger(store_root)
    by_session = {row["client_session_id"]: row for row in ledger["usage_events"]}

    row = by_session["split-no-merged"]
    assert (row["fresh_tokens"], row["cache_creation_tokens"], row["cache_read_tokens"]) == (15, 30, 70)
    assert row["total_tokens"] == 115

    row = by_session["split-inconsistent"]
    assert (row["fresh_tokens"], row["cache_creation_tokens"], row["cache_read_tokens"]) == (15, 5, 95)
    assert row["total_tokens"] == 115

    # The invariant on every row: fresh + creation + read == total.
    for row in ledger["usage_events"]:
        assert row["fresh_tokens"] + row["cache_creation_tokens"] + row["cache_read_tokens"] == row["total_tokens"]


def test_out_of_range_timestamp_never_500s_the_dashboard(tmp_path):
    store_root = tmp_path / "state"
    # Millisecond-epoch drift: fromtimestamp(1.75e12) is "year 57425".
    _trusted_usage(store_root, session="ms-epoch-session", updated_at=1_750_000_000_000)

    client = _app_client(store_root)
    assert client.get("/").status_code == 200
    assert client.get("/?tab=raw").status_code == 200

    # The guard lives in _fmt_time itself, covering every render site.
    assert _fmt_time(1_750_000_000_000) == ""
    assert _fmt_time(1e300) == ""
    assert _fmt_time(253_402_300_800.0) == ""


def test_dashboard_usage_view_dead_total_fields_removed():
    fields = getattr(api_module.DashboardUsageView, "__dataclass_fields__", {})
    assert "local_totals" not in fields
    assert "saved_totals" not in fields
