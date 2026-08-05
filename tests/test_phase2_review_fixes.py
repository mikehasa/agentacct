"""Phase 2 adversarial-review fix-round regressions.

Covers the five confirmed majors and the fixed minors, on the KEPT data
lanes (the HTML display layer is retired; ``/sessions`` and friends are
JSON-only, and the rollup/ledger fields below are what any display must
render from):

[0] a session may claim full attribution ONLY when every usage row in the
    session is attributed; mixed sessions expose per-session coverage
    (row_states + attributed_fresh_tokens) alongside whole-session totals;
[1] a transcript-conflict VETO is canonically unjoined — the rollup state
    must never claim a context match, and /work-items agrees;
[2] one-level nesting: deep/cyclic/self-parent lineage never vanishes from
    the session rollup (property-tested invariant on _top_level_session_keys);
[3] ambiguous join reasons are id-free on every served surface (full ids
    are dedicated-field-only);
[4] proxy budget decisions travel the /timeline data lane as distinct,
    honestly-unjoined rows that never drown recorded sections;
plus minors: collision-suffixed labels for attention examples and parent
references, fresh-vs-cache-inclusive blind-spot figures, "~" for
home-directory project labels, cache-triple reconciliation, out-of-range
timestamp guard, dead DashboardUsageView fields removed.

All stores are throwaway tmp_path stores (suite conftest guards the real
dogfood ledger)."""

from pathlib import Path

from fastapi.testclient import TestClient

import agentacct.api as api_module
from agentacct.api import _fmt_time, _top_level_session_keys, create_local_api_app
from agentacct.cost import CostLedger, UsageEstimate
from agentacct.service import SentinelService
from agentacct.work_ledger import build_work_ledger


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


def _sessions_payload(store_root):
    # HTML retirement: GET /sessions is JSON-only (the rollup, served
    # verbatim from ledger["session_rollup"]).
    response = _app_client(store_root).get("/sessions")
    assert response.status_code == 200
    return response.json()


def _ledger(store_root):
    return build_work_ledger(SentinelService(store_root).list_all_events(), run_reports=[], cost_events=[])


def _rollup_entry(ledger, client, session):
    for entry in ledger["session_rollup"]["sessions"]:
        if entry["client"] == client and entry["client_session_id"] == session:
            return entry
    raise AssertionError(f"no rollup entry for {client}::{session}")


def _payload_entry(payload, client, session):
    for entry in payload["sessions"]:
        if entry["client"] == client and entry["client_session_id"] == session:
            return entry
    raise AssertionError(f"no /sessions entry for {client}::{session}")


# ---------------------------------------------------------------------------
# [0] partial attribution coverage on the session entry
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


def test_mixed_session_exposes_partial_coverage_next_to_whole_session_totals(tmp_path):
    store_root = tmp_path / "state"
    _seed_partial_attribution_store(store_root)

    ledger = _ledger(store_root)
    entry = _rollup_entry(ledger, "codex", "partial-session")
    assert entry["join"]["state"] == "attributed"
    assert entry["join"]["row_states"]["attributed"] == 1
    assert entry["join"]["row_states"]["unjoined"] == 99
    assert entry["join"]["attributed_fresh_tokens"] == 20

    # The /sessions JSON lane carries the SAME coverage data a display needs
    # to qualify the chip ("1 of 100 rows / 20 fresh tokens attributed") —
    # never a bare full-attribution claim next to whole-session totals.
    served = _payload_entry(_sessions_payload(store_root), "codex", "partial-session")
    assert served["join"]["state"] == "attributed"
    assert served["join"]["row_states"] == {
        "attributed": 1,
        "ambiguous": 0,
        "context_matched_unallocated": 0,
        "unjoined": 99,
    }
    assert served["join"]["attributed_fresh_tokens"] == 20
    # Whole-session figures stay alongside (exact-key session truth).
    assert served["usage"]["rows"] == 100
    assert served["usage"]["fresh_tokens"] == 1_485_020


def test_fully_attributed_session_reports_total_coverage(tmp_path):
    store_root = tmp_path / "state"
    _record_section(store_root, section_id="full-goal", title="Fully joined goal", session="full-session")
    _trusted_usage(store_root, session="full-session", input_tokens=40, output_tokens=10)

    served = _payload_entry(_sessions_payload(store_root), "codex", "full-session")
    # Total coverage: the data that licenses a bare "Attributed" claim.
    assert served["join"]["state"] == "attributed"
    assert served["join"]["row_states"] == {
        "attributed": 1,
        "ambiguous": 0,
        "context_matched_unallocated": 0,
        "unjoined": 0,
    }
    assert served["join"]["attributed_fresh_tokens"] == 50
    assert served["usage"]["fresh_tokens"] == 50


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


def test_conflict_vetoed_session_never_claims_a_match_on_served_surfaces(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="veto-session-001", transcript="transcript-USAGE")
    _record_section(
        store_root,
        section_id="veto-work",
        title="Vetoed transcript work",
        session="veto-session-001",
        transcript="transcript-WORK",
    )

    client = _app_client(store_root)

    # /sessions serves the unjoined state with the veto reason attached —
    # never a context-match claim.
    sessions_payload = client.get("/sessions").json()
    served = _payload_entry(sessions_payload, "codex", "veto-session-001")
    assert served["join"]["state"] == "unjoined"
    assert served["join"]["row_states"]["context_matched_unallocated"] == 0
    assert "conflicting evidence vetoes the join" in served["join"]["reason"]

    # The /work-items lane on the SAME store agrees: nothing attributed.
    items = client.get("/work-items").json()["work_items"]
    veto_items = [item for item in items if item.get("section_id") == "veto-work"]
    assert len(veto_items) == 1
    assert veto_items[0]["linked_usage_records"] == 0
    assert veto_items[0]["usage_total"] == 0


def test_actual_context_match_still_reports_context_only(tmp_path):
    store_root = tmp_path / "state"
    # run_id grouping hint: a REAL non-vetoed context match that never
    # allocates — this is what context_only is for. (Different sessions, so
    # the context-matched session is its own top-level rollup entry.)
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

    served = _payload_entry(_sessions_payload(store_root), "codex", "hint-usage-session")
    assert served["join"]["state"] == "context_only"
    assert served["join"]["row_states"]["context_matched_unallocated"] == 1


# ---------------------------------------------------------------------------
# [2] one-level nesting: nothing vanishes
# ---------------------------------------------------------------------------


def test_grandchild_session_is_top_level_and_names_its_unrendered_parent(tmp_path):
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

    ledger = _ledger(store_root)
    rollup_sessions = ledger["session_rollup"]["sessions"]

    # Root and grandchild are top-level entries; the child nests under the
    # root (one-level nesting, nothing vanishes).
    top_keys = _top_level_session_keys(rollup_sessions)
    assert top_keys == {("claude-code", "lineage-root"), ("claude-code", "lineage-grandchild")}

    # Labels are suffixed on the first-8 collision — never three identical
    # "lineage-" truncations.
    labels = {entry["client_session_id"]: entry["client_session_id_short"] for entry in rollup_sessions}
    assert len(set(labels.values())) == 3
    assert "lineage-" not in labels.values()

    # The grandchild's spend is visible on its own entry, not vanished.
    grandchild = _rollup_entry(ledger, "claude-code", "lineage-grandchild")
    assert grandchild["usage"]["fresh_tokens"] == 44_000
    assert grandchild["usage"]["estimated_cost_usd"] == 3.0
    # The grandchild names its (non-top-level) parent.
    assert grandchild["related"]["parent"] is not None
    assert grandchild["related"]["parent"]["client_session_id"] == "lineage-child"


def test_parent_cycle_members_are_all_top_level(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="cycle-aaaa", parent_session="cycle-bbbb", input_tokens=10, output_tokens=1)
    _trusted_usage(store_root, session="cycle-bbbb", parent_session="cycle-aaaa", input_tokens=20, output_tokens=2)

    ledger = _ledger(store_root)
    rollup_sessions = ledger["session_rollup"]["sessions"]
    top_keys = _top_level_session_keys(rollup_sessions)
    assert top_keys == {("codex", "cycle-aaaa"), ("codex", "cycle-bbbb")}

    # codex labels keep the distinctive tail (UUIDv7 rule).
    labels = {entry["client_session_id"]: entry["client_session_id_short"] for entry in rollup_sessions}
    assert labels["cycle-aaaa"] == "cle-aaaa"
    assert labels["cycle-bbbb"] == "cle-bbbb"


def test_self_parent_session_stays_top_level_and_never_duplicates_its_own_usage(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="self-parent-session", parent_session="self-parent-session", input_tokens=9, output_tokens=1)

    ledger = _ledger(store_root)
    entry = _rollup_entry(ledger, "codex", "self-parent-session")
    # Data level: the corrupt pointer is dropped with an honest note, so the
    # session's own usage can never reappear as a "descendants" subtotal.
    assert entry["related"]["parent"] is None
    assert entry["related"]["note"] == "self-referencing parent pointer ignored"
    assert entry["related"]["children_usage"] is None

    rollup_sessions = ledger["session_rollup"]["sessions"]
    assert len(rollup_sessions) == 1
    assert _top_level_session_keys(rollup_sessions) == {("codex", "self-parent-session")}


def test_top_level_invariant_nothing_vanishes_nothing_double_nests(tmp_path):
    """Property: every rollup entry either is its own top-level entry or is
    the direct child of exactly one top-level entry — across chains, cycles,
    self-parents, orphans, and forests."""

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
        # Nested: must have a parent that IS a top-level entry.
        assert parent is not None, key
        parent_key = (entry["client"], parent["client_session_id"])
        assert parent_key in entry_keys, key
        assert parent_key in top_keys, key
    # Identity never vanishes, while only the two parentless roots have
    # independently additive Codex usage. Parent-linked rows are held rather
    # than replayed into the aggregate.
    assert ledger["session_rollup"]["summary"]["totals"]["fresh_tokens"] == 22
    assert ledger["insights"]["usage_additivity_quarantine"]["excluded_rows"] == len(lineage) - 2

    # Served check: the /sessions JSON lane carries every entry (nothing
    # vanishes on the wire either).
    payload = _sessions_payload(store_root)
    assert payload["total_sessions"] == len(lineage)
    assert len(payload["sessions"]) == len(lineage)


# ---------------------------------------------------------------------------
# [3] ambiguous reasons are id-free everywhere served
# ---------------------------------------------------------------------------

AMBIG_UUID = "0d9f31c2-7b44-4e02-9a55-1234deadbeef"


def test_ambiguous_join_reason_is_id_free_on_every_served_surface(tmp_path):
    store_root = tmp_path / "state"
    _record_section(store_root, section_id="ambig-a", title="Ambiguous section A", session=AMBIG_UUID)
    _record_section(store_root, section_id="ambig-b", title="Ambiguous section B", session=AMBIG_UUID)
    _trusted_usage(store_root, session=AMBIG_UUID)

    ledger = _ledger(store_root)
    attribution = ledger["attributions"][0]
    assert attribution["join_strategy"] == "exact_client_session_id_ambiguous_sections"
    # Reason: counts + key name only; candidate ids stay in dedicated fields.
    assert attribution["join_reason"] == (
        "usage shares client_session_id with 2 sections; not allocating to a section"
    )
    assert len(attribution["ambiguous_candidate_work_ids"]) == 2
    assert all(AMBIG_UUID in work_id for work_id in attribution["ambiguous_candidate_work_ids"])

    # The served rollup carries the same id-free reason: the full id is
    # allowed ONLY in dedicated id fields, never inside the reason text.
    served = _payload_entry(_sessions_payload(store_root), "codex", AMBIG_UUID)
    assert served["join"]["reason"] == (
        "usage shares client_session_id with 2 sections; not allocating to a section"
    )
    assert AMBIG_UUID not in served["join"]["reason"]
    assert AMBIG_UUID not in served["client_session_id_short"]


# ---------------------------------------------------------------------------
# [4] proxy rows ride the /timeline data lane without drowning sections
# ---------------------------------------------------------------------------


def test_timeline_lane_keeps_proxy_budget_decisions_distinct_and_sections_visible(tmp_path):
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

    payload = _app_client(store_root).get("/timeline", params={"limit": 500}).json()
    entries = payload["timeline"]

    # Sections stay on the lane despite 100 NEWER budget decisions.
    titles = {entry.get("title") for entry in entries}
    for name in ("alpha", "beta", "gamma"):
        assert f"Proxy flood section {name}" in titles

    # Proxy rows are a DISTINCT event kind with a distinct label — a display
    # layer can group them without parsing titles apart (client travels as
    # data on every proxy entry, per the v2 wire contract).
    proxy_entries = [entry for entry in entries if entry.get("event_kind") == "proxy_usage"]
    assert len(proxy_entries) == 100
    for entry in proxy_entries:
        assert entry["title"] == "Proxy budget decision"
        assert "client" in entry
        # Honest join accounting: proxy rows never claim attribution.
        assert entry["work_id"] is None
        assert entry["join_strategy"] == "unjoined"
    # Labeled fresh tokens survive per-row (the group figure's data source).
    assert sum(int(entry.get("tokens_fresh") or 0) for entry in proxy_entries) == 300


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

    # The served /attention lane carries the same suffixed example labels.
    served_groups = _app_client(store_root).get("/attention").json()["attention_groups"]
    served_labels = [ref["label"] for group in served_groups for ref in group["example_refs"]]
    assert sorted(served_labels) == sorted(example_labels)


def test_parent_reference_label_routes_through_collision_assigner(tmp_path):
    store_root = tmp_path / "state"
    # An entry whose base label collides with an UNRECORDED parent's base
    # label: both must be suffixed, never labeled identically. claude-code
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


def test_blind_spot_figures_expose_fresh_separately_from_cache_inclusive_total(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="blind-spot-session", input_tokens=10, output_tokens=10, cached_input_tokens=1_000_000, cost=0.5)

    ledger = _ledger(store_root)
    blind_spots = {spot["type"]: spot for spot in ledger["insights"]["blind_spots"]}
    spot = blind_spots["usage_without_mcp_context"]

    # Fresh figure leads its own field; the cache-inclusive total is a
    # SEPARATE labeled field — never one bare cache-inflated number.
    assert spot["fresh_tokens"] == 20
    assert spot["tokens"] == 1_000_020
    assert spot["cache_creation_tokens"] + spot["cache_read_tokens"] == 1_000_000
    assert spot["fresh_tokens"] + spot["cache_creation_tokens"] + spot["cache_read_tokens"] == spot["tokens"]


def test_home_directory_project_label_is_tilde_not_username(tmp_path):
    store_root = tmp_path / "state"
    home = Path.home()
    _trusted_usage(store_root, session="home-cwd-session", project_dir=str(home))

    ledger = _ledger(store_root)
    entry = _rollup_entry(ledger, "codex", "home-cwd-session")
    assert entry["project"] == "~"

    # The served label agrees: displays render "~", never the username.
    served = _payload_entry(_sessions_payload(store_root), "codex", "home-cwd-session")
    assert served["project"] == "~"


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


def test_out_of_range_timestamp_never_500s_kept_surfaces(tmp_path):
    store_root = tmp_path / "state"
    # Millisecond-epoch drift: fromtimestamp(1.75e12) is "year 57425".
    _trusted_usage(store_root, session="ms-epoch-session", updated_at=1_750_000_000_000)

    client = _app_client(store_root)
    assert client.get("/timeline").status_code == 200
    assert client.get("/sessions").status_code == 200

    # The guard lives in _fmt_time itself, covering every render site.
    assert _fmt_time(1_750_000_000_000) == ""
    assert _fmt_time(1e300) == ""
    assert _fmt_time(253_402_300_800.0) == ""


def test_dashboard_usage_view_dead_total_fields_removed():
    fields = getattr(api_module.DashboardUsageView, "__dataclass_fields__", {})
    assert "local_totals" not in fields
    assert "saved_totals" not in fields
