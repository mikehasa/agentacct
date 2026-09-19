"""The shared display vocabulary: one owner for every display string."""

from __future__ import annotations

import time

import pytest

from agentacct import display_vocabulary as vocab


@pytest.fixture
def los_angeles(monkeypatch: pytest.MonkeyPatch):
    """Pin the process to one local zone so local-date text is deterministic."""

    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset unavailable on this platform")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


# --- cost grammar -------------------------------------------------------------


def test_cost_display_prefixes_by_completeness_and_confidence():
    reported = vocab.cost_display(1554.672, True, "client_reported")
    assert reported == {"display_text": "$1,554.67", "prefix": "$", "state": "complete"}
    assert vocab.cost_display(10.77, True, "provider_billed")["display_text"] == "$10.77"
    estimate = vocab.cost_display(10.77, True, "estimated_from_tokens")
    assert estimate == {"display_text": "≈$10.77", "prefix": "≈$", "state": "complete"}
    # a missing confidence never earns the bare reported/billed $.
    assert vocab.cost_display(10.77, True, None)["prefix"] == "≈$"
    partial = vocab.cost_display(1635.574, False, "estimated_from_tokens")
    assert partial == {"display_text": "~$1,635.57", "prefix": "~$", "state": "partial"}


def test_cost_display_partial_amount_overrides_the_incomplete_total():
    shown = vocab.cost_display(None, False, None, partial_amount=2.5)
    assert shown["display_text"] == "~$2.50"
    # An explicit None subtotal means nothing is priced, even if a total exists.
    assert vocab.cost_display(3.0, False, None, partial_amount=None)["display_text"] == "unpriced"


def test_cost_display_names_its_absences():
    assert vocab.cost_display(None, False, None, has_usage=False) == {
        "display_text": "no usage recorded",
        "prefix": None,
        "state": "no_usage",
    }
    assert vocab.cost_display(None, False, None) == {"display_text": "unpriced", "prefix": None, "state": "unpriced"}
    # non-finite amounts are never printed as $nan / $inf.
    assert vocab.cost_display(float("nan"), True, "client_reported")["display_text"] == "unpriced"
    assert vocab.cost_display(True, True, "client_reported")["display_text"] == "unpriced"


def test_cost_basis_labels_have_one_spelling():
    assert vocab.cost_basis_label("pricing_table") == "pricing estimate"
    assert vocab.cost_basis_label("client_reported") == "client-reported"
    assert vocab.cost_basis_label("provider_billed") == "provider-billed"
    assert vocab.cost_basis_label("local_client_session") == "client-reported"
    assert vocab.cost_basis_label("unknown") == "cost basis not reported"
    assert vocab.cost_basis_label(None) == "cost basis not reported"
    assert vocab.cost_basis_label("some_new_basis") == "some new basis"


def test_cost_legend_and_confidence_display():
    # Each glyph is bound to the words that define it by NO-BREAK SPACE, so a
    # wrapping legend can only break at " · " and never orphans a symbol (K25).
    assert vocab.COST_LEGEND == "~$ partial subtotal · ≈$ estimate · $ reported or billed"
    assert vocab.COST_CHART_LEGEND == "~$ partial subtotal · open cap = partial"
    for legend in (vocab.COST_LEGEND, vocab.COST_CHART_LEGEND):
        assert all(" " not in pair for pair in legend.split(" · "))
    assert vocab.cost_confidence_display(True, "pricing_table") == "mixed · mostly pricing estimate"
    assert vocab.cost_confidence_display(True, None) == "mixed"
    assert vocab.cost_confidence_display(False, "client_reported") == "client-reported"


# --- dates, resets, windows, shares ------------------------------------------


def test_display_date_is_local(los_angeles):
    # 2026-09-15T03:13Z is still Sep 14 in Los Angeles — never a future UTC date.
    assert vocab.display_date(1789441994.94) == "Sep 14"
    assert vocab.display_date(None) == "date not recorded"


def test_reset_text_three_named_states(los_angeles):
    resets_at = 1789365000  # 2026-09-13 22:50 PDT
    assert vocab.reset_text(resets_at, now=resets_at - (4 * 86400 + 3 * 3600)) == "resets in 4d 3h"
    assert vocab.reset_text(resets_at, now=resets_at + 60) == "reset passed Sep 13, 10:50 PM"
    assert vocab.reset_text(None, now=resets_at) == "reset time not reported"


def test_window_labels():
    assert vocab.WINDOW_LABELS == {"5h": "5-hour limit", "7d": "7-day limit"}
    assert vocab.window_label_for("7d") == "7-day limit"
    assert vocab.window_label_for("custom", 120) == "120m limit"
    assert vocab.window_label_for("custom", float("inf")) == "limit window"


def test_percent_share_never_rounds_a_real_share_to_zero():
    assert vocab.percent_share(0) == "0%"
    assert vocab.percent_share(0.0001) == "<1%"
    assert vocab.percent_share(0.00499) == "<1%"
    assert vocab.percent_share(0.005) == "1%"
    assert vocab.percent_share(0.426) == "43%"
    assert vocab.percent_share(1.0) == "100%"


def test_humanize_seconds():
    assert vocab.humanize_seconds(0) == "<1m"
    assert vocab.humanize_seconds(2 * 86400 + 3 * 3600) == "2d 3h"


# --- sources, tiers, decisions, receipt fields, dispositions ----------------


def test_source_labels_carry_label_legend_and_tier():
    expected = {
        "mcp": "Agent-reported",
        "hook": "Hook-captured",
        "client_log": "Client log",
        "transcript_scan": "Transcript scan",
        "inferred": "Inferred",
        "none": "No source recorded",
    }
    for key, label in expected.items():
        entry = vocab.SOURCE_LABELS[key]
        assert entry["label"] == label
        assert set(entry) == {"label", "legend", "tier_key", "tier_label"}
        assert entry["legend"].endswith(".")
        assert entry["tier_label"]
    assert vocab.SOURCE_LABELS["mcp"]["tier_key"] == "self_checked"
    assert vocab.SOURCE_LABELS["hook"]["tier_key"] == "independently_checked"
    assert vocab.source_label("client_log") == "Client log"
    assert vocab.source_label("") == "No source recorded"
    assert vocab.source_label("some_source") == "some source"


def test_tier_table_names_who_ran_each_check():
    by_key = {row["key"]: row for row in vocab.TIER_TABLE}
    assert [row["label"] for row in vocab.TIER_TABLE] == [
        "externally verified",
        "independently checked",
        "self-checked",
        "unchecked",
    ]
    assert by_key["self_checked"]["definition"] == "The agent ran the check itself and reported the result."
    for row in vocab.TIER_TABLE:
        assert set(row) == {"key", "label", "definition"}


def test_decision_labels_are_complete_and_sentence_case():
    from agentacct.receipt import _DECISION_ASSERTED_BY, _DECISION_STATEMENTS

    for key in set(_DECISION_ASSERTED_BY) | set(_DECISION_STATEMENTS):
        label = vocab.DECISION_LABELS[key]
        assert label[0].isupper() and label[1:] == label[1:].lower(), label
    # Work-status keys are the agent's report, kept OUT of the decision table.
    for key in ("started", "checkpoint", "completed"):
        assert key not in vocab.DECISION_LABELS
        label = vocab.WORK_STATUS_LABELS[key]
        assert label[0].isupper() and label[1:] == label[1:].lower(), label
    assert vocab.work_status_label("checkpoint") == "In progress"
    assert vocab.work_status_label("completed") == "Completed"
    for key, label in {
        "in_progress": "In progress",
        "handed_off": "Handed off",
        "ended_open": "Ended open",
        "blocked": "Blocked",
        "finding": "Finding",
        "reported": "Reported",
        "verified": "Verified",
        "blocker_resolved_by_user": "Blocker resolved",
        "finding_resolved_by_user": "Finding resolved",
    }.items():
        assert vocab.decision_label(key) == label
    assert vocab.decision_label(None) == "No outcome recorded"
    assert vocab.decision_label("brand_new_state") == "Brand new state"


def test_receipt_field_labels():
    assert vocab.RECEIPT_FIELD_LABELS == {
        "task": "Task",
        "decision": "Decision",
        "outcome": "Decision",
        "coverage": "Coverage",
        "checks": "Checks",
        "evidence": "Checks",
        "cost": "Cost",
        "agents": "Agents",
        "actors": "Agents",
        "actions": "Tool calls",
        "weekly_plan": "Weekly plan",
        # The four questions a reviewer arrives with, as section headings. They
        # are section names, not dimension names, and they live here so the
        # macOS app stops spelling them as Swift literals.
        "goal": "Goal",
        "outcome_section": "Outcome",
        "evidence_section": "Evidence",
        "next_section": "Next",
    }
    # A LIST of tasks names three more columns; a single receipt has none of
    # them, so they ship with the task list, from the same vocabulary.
    assert vocab.TASK_LIST_FIELD_LABELS == {
        **vocab.RECEIPT_FIELD_LABELS,
        "client": "Client",
        "updated": "Updated",
        "attention": "Attention",
    }
    # Every receipt dimension key has a label; no key is ever shown raw.
    for key in ("task", "actors", "actions", "cost", "evidence", "outcome"):
        assert vocab.receipt_field_label(key) != key
    assert vocab.receipt_field_label("brand_new") == "Brand new"


def test_disposition_effects_state_what_each_action_does():
    # Every action states its queue consequence (one queue noun) and what the
    # badge becomes; reopen has its own effect.
    assert vocab.DISPOSITION_EFFECTS[("finding", "reviewed")] == (
        "Leaves Attention; the badge stays Finding until resolved."
    )
    assert vocab.DISPOSITION_EFFECTS[("finding", "resolved")] == (
        "Leaves Attention and records your resolution; the badge becomes Finding resolved. "
        "The failing check stays in history."
    )
    assert vocab.DISPOSITION_EFFECTS[("blocked", "reviewed")] == (
        "Leaves Attention; the badge stays Blocked until resolved."
    )
    assert vocab.DISPOSITION_EFFECTS[("blocked", "resolved")] == (
        "Leaves Attention and records your resolution; the badge becomes Blocker resolved."
    )
    for kind in ("finding", "blocked", "check_not_run"):
        effects = vocab.disposition_effects(kind)
        assert set(effects) == {"reviewed", "resolved", "reopen"} and all(effects.values())
        assert effects["reopen"] == "Returns to Attention with its original badge."
        assert all("Needs attention" not in text for text in effects.values())
    assert vocab.disposition_effects("unknown_kind") == {"reviewed": None, "resolved": None, "reopen": None}
    assert vocab.attention_count_text(4) == "4 in Attention"
    assert vocab.ATTENTION_OPEN_ACTION == "Open Attention"


def test_decision_legend_covers_every_badge_word_and_group():
    legend = vocab.decision_legend()
    keys = [row["key"] for row in legend["decisions"]]
    assert len(keys) == len(set(keys)) == 15
    for row in legend["decisions"]:
        assert row["label"] == vocab.decision_label(row["key"])
        assert row["definition"] == vocab.DECISION_DEFINITIONS[row["key"]]
        assert row["group_key"] in vocab.GROUP_DEFINITIONS
    assert [row["key"] for row in legend["groups"]] == [
        "attention", "verified", "reported", "in_progress", "observed", "stopped", "other"
    ]
    assert {row["label"] for row in legend["groups"]} >= {"Attention", "Stopped", "Other"}
    # The group rule reads the reducer's attention predicate.
    assert vocab.decision_group("reported", True) == "attention"
    assert vocab.decision_group("finding", False) == "other"
    assert vocab.decision_group("blocked", None) == "attention"
    assert vocab.decision_group("inactive", False) == "stopped"
    assert vocab.decision_group("brand_new", None) == "other"


def test_actions_synopsis_names_each_absence_and_integrity_state():
    synopsis = vocab.actions_synopsis
    assert synopsis({}, 0, capture_known=False)["tile"] == {
        "value": None, "absent": "not instrumented", "qualifier": None
    }
    assert synopsis({}, 0, capture_known=True)["tile"]["absent"] == "no tool calls recorded"
    unknown = synopsis(None, None, capture_known=False)
    assert unknown["state"] == "capture_unknown" and unknown["tile"]["absent"] == "capture coverage unknown"
    exact = synopsis({"read": 5, "edit": 3}, 8, capture_known=True, source_text="Hook-captured")
    assert exact["state"] == "exact" and exact["can_show_distribution"] is True
    assert exact["headline"] == "8 tool calls captured"
    assert exact["tile"] == {"value": "8", "absent": None, "qualifier": "Hook-captured"}
    assert [m["label"] for m in exact["metrics"]] == ["Read", "Edit"]
    total_only = synopsis(None, 80, capture_known=True)
    assert total_only["state"] == "total_only" and total_only["headline"] == "80 tool calls in stored total"
    mismatch = synopsis({"read": 8, "edit": 4}, 10, capture_known=True)
    assert mismatch["state"] == "mismatch" and mismatch["tile"]["absent"] == "tool-call totals conflict"
    assert mismatch["integrity_detail"] == "category counts sum to 12 · stored total is 10"
    invalid = synopsis({"read": 8, "edit": -2}, 8, capture_known=True)
    assert invalid["state"] == "invalid" and invalid["tile"]["absent"] == "tool-call data incomplete"
    assert invalid["integrity_detail"] == (
        "1 invalid category was omitted · 8 valid tool calls remain categorized · stored total is 8"
    )
    missing_total = synopsis({"read": 3, "execute": 2}, None, capture_known=True)
    assert missing_total["state"] == "total_unavailable"
    assert missing_total["tile"] == {"value": "5", "absent": None, "qualifier": "categorized calls"}
    future = synopsis({"future_type": 2}, 2, capture_known=True, source_text="Hook-captured")
    assert future["state"] == "unrecognized_categories"
    assert future["tile"]["qualifier"] == "Hook-captured · types changed"
    assert future["metrics"][-1]["label"] == "Unrecognized types"
    assert vocab.related_paths_text(0) == "no related paths recorded"
    assert vocab.related_paths_text(None) == ""


def test_no_display_string_is_a_dash():
    strings: list[str] = [
        *vocab.DECISION_LABELS.values(),
        *vocab.RECEIPT_FIELD_LABELS.values(),
        *vocab.ATTENTION_REASON_LABELS.values(),
        *vocab.COST_BASIS_LABELS.values(),
        *(str(entry["label"]) for entry in vocab.SOURCE_LABELS.values()),
        vocab.cost_display(None, False, None)["display_text"],
        vocab.reset_text(None, 0),
    ]
    assert all(text.strip() and text.strip() not in {"—", "-", "–"} for text in strings)


def test_the_vocabulary_is_a_leaf_module():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(vocab))
    relative = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level]
    assert relative == []


def test_surfaces_import_their_labels_from_the_vocabulary():
    from agentacct import cli, receipt, receipt_markdown, task_timeline, tui, usage_snapshot

    assert tui.decision_label is vocab.decision_label
    assert tui.ATTENTION_REASON_LABELS is vocab.ATTENTION_REASON_LABELS
    assert usage_snapshot.ORIGIN_LABELS is vocab.ORIGIN_LABELS
    assert usage_snapshot.humanize_seconds is vocab.humanize_seconds
    assert cli.RECEIPT_FIELD_LABELS is vocab.RECEIPT_FIELD_LABELS
    assert receipt_markdown.RECEIPT_FIELD_LABELS is vocab.RECEIPT_FIELD_LABELS
    assert receipt.PROVENANCE_LEGEND["mcp"] == vocab.SOURCE_LABELS["mcp"]["legend"]
    # the per-surface label tables are gone
    for module, name in (
        (tui, "_DECISION_LABEL"),
        (tui, "_PROVENANCE_LABEL"),
        (tui, "_ATTENTION_REASON_LABEL"),
        (task_timeline, "_source_label"),
    ):
        assert not hasattr(module, name), f"{module.__name__}.{name}"


def test_receipt_cost_text_uses_the_shared_grammar():
    from agentacct.receipt import receipt_cost_text

    assert receipt_cost_text(
        {"estimated_cost_usd": 1554.672, "cost_complete": True, "cost_basis": "pricing_table",
         "cost_confidence": "estimated_from_tokens"}
    ) == "≈$1,554.67 · pricing estimate"
    assert receipt_cost_text(
        {"estimated_cost_usd": 1.2987, "cost_complete": True, "cost_basis": "local_client_session"}
    ) == "$1.30 · client-reported"
    assert receipt_cost_text(
        {"estimated_cost_usd": 4.0, "cost_complete": False, "cost_basis": "pricing_table"}
    ) == "~$4.00 · pricing estimate"
    assert receipt_cost_text({"estimated_cost_usd": None, "provenance": ["none"]}) == "no usage recorded"
    assert receipt_cost_text({"estimated_cost_usd": None, "provenance": ["client_log"]}) == "unpriced"


def test_actions_synopsis_taxonomy_and_integrity_edges():
    """Ported from the app's former Swift derivation: the reducer now owns it."""

    synopsis = vocab.actions_synopsis
    metrics = synopsis(
        {"search": 11, "delegate_task": 3, "read": 38, "execute": 24, "archive": 2, "edit": 7,
         "network": 5, "agent": 4, "plan": 3, "mcp": 2, "other": 1},
        105, capture_known=True,
    )["metrics"]
    assert [(m["key"], m["count"]) for m in metrics] == [
        ("read", 38), ("edit", 7), ("execute", 24), ("search", 11), ("network", 5), ("agent", 4),
        ("plan", 3), ("mcp", 2), ("other", 1), ("__unknown_types__", 5),
    ]
    assert metrics[-1]["detail"] == "2 unrecognized categories"
    assert metrics[7]["label"] == "Connected tools"

    zero_and_negative = synopsis({"read": 4, "search": 0, "edit": -1}, 4, capture_known=True)
    assert [m["key"] for m in zero_and_negative["metrics"]] == ["read"]

    exact = synopsis({"read": 8}, 8, capture_known=True)
    assert exact["can_show_distribution"] is True
    assert exact["capture_boundary"] == (
        "No ordered action ledger; captured tool-call counts cannot be linked to results or timing."
    )
    for counts, total in ((None, None), ({}, 0), (None, 8), ({"read": 8}, None),
                          ({"read": 8, "edit": 4}, 10), ({"read": -1}, 1), ({"read": 7}, 10)):
        assert synopsis(counts, total, capture_known=True)["can_show_distribution"] is False

    partial = synopsis({"read": 7}, 10, capture_known=True)
    assert partial["state"] == "mismatch"
    assert partial["integrity_detail"] == "category counts sum to 7 · stored total is 10"

    one = synopsis({"read": 1}, None, capture_known=True)
    assert one["headline"] == "1 categorized tool call"
    assert one["integrity_detail"] == "Stored tool-call total unavailable"
    assert one["capture_boundary"] is not None

    invalid_total = synopsis({"read": 2}, -1, capture_known=True)
    assert invalid_total["integrity_detail"] == "the stored total is invalid · 2 valid tool calls remain categorized"
    blank = synopsis({"   ": 4}, 4, capture_known=True)
    assert blank["state"] == "invalid" and blank["metrics"] == []
    assert blank["integrity_detail"] == "1 invalid category was omitted · stored total is 4"

    big = 2**63 - 1
    overflow = synopsis({"read": big, "edit": 1}, big, capture_known=True)
    assert overflow["state"] == "invalid" and overflow["categorized_total"] is None
    assert overflow["integrity_detail"] == f"the categorized tool-call sum overflowed · stored total is {big}"
    unknown_overflow = synopsis({"future_a": big, "future_b": 1, "": -1}, big, capture_known=True)
    assert unknown_overflow["integrity_detail"] == (
        f"1 invalid category was omitted · the categorized tool-call sum overflowed · stored total is {big}"
    )
    assert unknown_overflow["metrics"] == []

    many = {"read": 2, **{f"future_{i}": 1 for i in range(5000)}}
    bounded = synopsis(many, 5002, capture_known=True, source_text="Hook-captured")
    assert bounded["state"] == "unrecognized_categories" and len(bounded["metrics"]) == 2
    assert bounded["metrics"][-1]["count"] == 5000
    assert bounded["headline"] == "5002 tool calls captured"
    assert bounded["integrity_detail"] == "5000 unrecognized tool-call types were aggregated as Unrecognized types"
    assert bounded["can_show_distribution"] is False
    assert bounded["tile"]["qualifier"] == "Hook-captured · types changed"
    future_missing_total = synopsis({"future_type": 2}, None, capture_known=True, source_text="Hook-captured")
    assert future_missing_total["tile"]["qualifier"] == "categorized calls · Hook-captured · types changed"
    assert vocab.related_paths_text(18) == "18 related paths" and vocab.related_paths_text(1) == "1 related path"


def test_plan_share_states_one_table_with_chip_and_sentence():
    assert set(vocab.PLAN_SHARE_STATES) == {"calibrated", "calibrating", "out_of_band", "never", "row_no_share"}
    assert vocab.plan_share_state_text("out_of_band")["chip_text"] == "plan share unavailable"
    row = vocab.plan_share_fields(None, "calibrated")
    assert row["chip_text"] == "no share for this session"
    assert row["chip_text"] != vocab.plan_share_state_text("out_of_band")["chip_text"]
    assert vocab.plan_share_fields(0.05, "calibrated")["headline"] == "≈<0.1% of weekly plan"
    assert vocab.plan_share_state_text("bogus")["chip_text"] == "plan share not reported"


def test_limit_value_and_freshness_words():
    assert vocab.limit_used_text(99.4) == "99% used"
    assert vocab.limit_used_text(0.3) == "<1% used"
    assert vocab.limit_used_text(100) == "100% used · limit reached"
    assert vocab.limit_used_text(127) == "127% used · limit exceeded"
    assert vocab.limit_used_text(-5) == "invalid provider percentage"
    assert vocab.limit_used_text(None) == "used percent not reported"
    assert vocab.limit_value_text(3.0, reset_passed=True) == "last reported 3%"
    assert vocab.relative_age_text(4.9) == "just now"
    assert vocab.relative_age_text(5) == "<1m ago"
    assert vocab.data_age_text(1000.0, 1000.0 + 86400 + 15 * 3600) == "as of 1d 15h ago"


def test_cost_total_label_never_calls_a_partial_figure_total():
    assert vocab.cost_total_label("complete", rows=4, unpriced_rows=0) == "total"
    assert vocab.cost_total_label("partial", rows=50, unpriced_rows=3) == "Partial subtotal · 3 of 50 usage records unpriced"
    assert vocab.cost_total_label("unpriced", rows=3, unpriced_rows=3) == "no priced usage · 3 of 3 usage records unpriced"
    assert vocab.cost_total_label("none_recorded", rows=0) == "no usage recorded"
    assert "total" not in vocab.cost_total_label("partial", rows=2, unpriced_rows=1).lower().split(" · ")[0].split()


def test_swift_freshness_threshold_matches_the_vocabulary():
    from pathlib import Path

    swift = (Path(__file__).resolve().parents[1] / "apps/agentacct/Sources/agentacct/MainWindow.swift").read_text()
    assert f"static let justNowSeconds = {vocab.FRESHNESS_JUST_NOW_SECONDS}" in swift
    assert f'static let justNowText = "{vocab.FRESHNESS_JUST_NOW_TEXT}"' in swift


def test_swift_mirrors_the_recorded_usage_name():
    """The app's menu section, Usage section and capacity column all take the
    one recorded-usage name from this module (K51)."""

    from pathlib import Path

    swift = (Path(__file__).resolve().parents[1] / "apps/agentacct/Sources/agentacct/UsagePane.swift").read_text()
    assert f'static let title = "{vocab.RECORDED_USAGE_TITLE}"' in swift


def test_period_label_names_the_bucket_and_its_grain():
    assert vocab.period_label("2026-09-12") == "Sep 12"
    assert vocab.period_label("2026-09-12", "daily") == "Sep 12"
    assert vocab.period_label("2026-07-20", "weekly") == "week of Jul 20"
    # The cube's undated bucket and a malformed key are named, never sliced.
    assert vocab.period_label("unknown") == vocab.PERIOD_LABEL_UNDATED
    assert vocab.period_label(None) == vocab.PERIOD_LABEL_UNDATED
    assert vocab.period_label("2026-13-40") == vocab.PERIOD_LABEL_UNDATED


def test_actions_synopsis_earns_exact_only_when_capture_covers_the_task(los_angeles):
    """'exact' is a coverage claim, not an arithmetic one.

    The categories can add up perfectly while the ledger holds records the
    capture never saw — the capture then described a sample of the Task, and
    saying 'exact' about it is a false statement.
    """

    counts, total = {"read": 26, "execute": 25, "agent": 3, "mcp": 5, "other": 1}, 60
    clean = vocab.actions_synopsis(counts, total, capture_known=True, source_text="Hook-captured")
    assert clean["state"] == "exact"

    # Sep 12 17:21-17:38 local, against a Sep 12 -> Sep 14 task.
    start = time.mktime((2026, 9, 12, 17, 21, 0, 0, 0, -1))
    partial = vocab.actions_synopsis(
        counts, total, capture_known=True, source_text="Hook-captured",
        coverage={
            "captured_first_at": start,
            "captured_last_at": start + 17 * 60,
            "activity_first_at": start - 44,
            "activity_last_at": start + 2 * 24 * 3600,
            "record_shortfalls": [
                {"record_label": "recorded section", "call_label": "record_section",
                 "captured": 4, "recorded": 5},
                {"record_label": "recorded check", "call_label": "record_machine_check",
                 "captured": 0, "recorded": 4},
            ],
        },
    )
    assert partial["state"] == "partial"
    assert partial["headline"] == "60 tool calls captured"
    assert partial["integrity_detail"] == (
        "captured 60 calls covering Sep 12 17:21–17:38 of a Sep 12–Sep 14 task"
        " · the ledger holds 5 recorded sections but capture saw 4 record_section calls"
        " · the ledger holds 4 recorded checks but capture saw no record_machine_check call"
    )
    assert partial["tile"]["qualifier"] == "Hook-captured · partial coverage"
    # The count itself is never withheld: it is real, it is just a sample.
    assert partial["tile"]["value"] == "60"


def test_capture_shortfall_never_fires_on_the_window_alone():
    """A batch's drain time bounds when it ENDED, not when its calls began.

    A single-batch capture therefore looks like an instant even when it covered
    the whole Task, so the window is context for a proven shortfall and never a
    trigger of its own — otherwise one false statement replaces another.
    """

    window_only = {
        "captured_first_at": 1_700_000_000.0,
        "captured_last_at": 1_700_000_000.0,
        "activity_first_at": 1_699_999_000.0,
        "activity_last_at": 1_700_000_900.0,
        "record_shortfalls": [],
    }
    assert vocab.actions_capture_shortfall(window_only, 22) is None
    assert vocab.actions_synopsis({"read": 22}, 22, capture_known=True, coverage=window_only)["state"] == "exact"
    # A record shortfall with no usable window still states the shortfall.
    assert vocab.actions_capture_shortfall(
        {"record_shortfalls": [{"record_label": "recorded check", "call_label": "record_machine_check",
                                "captured": 0, "recorded": 2}]},
        5,
    ) == "the ledger holds 2 recorded checks but capture saw no record_machine_check call"
    # Garbage in the coverage block can never manufacture a claim.
    assert vocab.actions_capture_shortfall("not a mapping", 5) is None
    assert vocab.actions_capture_shortfall({"record_shortfalls": [{"captured": 4, "recorded": 2}]}, 5) is None


def test_revision_label_states_its_basis_instead_of_asserting_provenance():
    """``at <sha>`` reads as "this is the revision that ran".

    For the server-at-record basis that is false: HEAD is read when the MCP call
    ARRIVES, so an agent that records a check before committing stamps the
    commit BEFORE its own work.
    """

    revision = {"commit": "8a4e0240d24ea5", "branch": "main", "dirty": True}
    assert vocab.revision_label({**revision, "basis": "server_captured_at_record"}) == (
        "HEAD when recorded: 8a4e024 · main · uncommitted changes"
    )
    # The hook reads HEAD in the same process that ran the check, so "ran at" is true there.
    assert vocab.revision_label({**revision, "basis": "host_hook"}) == (
        "ran at 8a4e024 · main · uncommitted changes"
    )
    assert vocab.revision_label({**revision, "basis": "unavailable"}) == (
        "revision basis unknown: 8a4e024 · main · uncommitted changes"
    )
    assert vocab.revision_label({"commit": "8a4e0240d24ea5", "basis": "host_hook"}) == "ran at 8a4e024"
    assert vocab.revision_label({"basis": "host_hook"}) == "revision not captured"
    assert vocab.revision_label(None) == "revision not captured"


def test_revision_contradiction_names_the_paths_the_stamp_cannot_contain():
    assert vocab.revision_contradiction_text("8a4e0240d24ea5", ["tests/test_subtract.py"]) == (
        "The stamped revision 8a4e024 does not contain tests/test_subtract.py, "
        "so it is not the revision this check ran against."
    )
    assert vocab.revision_contradiction_text("8a4e0240d24ea5", []) is None
    assert vocab.revision_contradiction_text(None, ["a.py"]) is None


def test_command_states_are_two_different_facts_with_two_different_sentences():
    """An agent-supplied command IS stored; a hook check holds only a digest.

    Both used to print the digest-only sentence, which was false for the first.
    """

    assert vocab.command_state_text("agent_recorded") == (
        "The agent recorded this check's command; the receipt shows the name it recorded, "
        "not the command text."
    )
    assert vocab.command_state_text("digest_only") == vocab.COMMAND_NOT_SHOWN_TEXT
    assert vocab.command_state_text(None) is None
    assert vocab.command_state_text("something else") is None


def test_reviewer_gap_sentences_name_what_they_prevent():
    assert vocab.gap_subagents_recorded_no_work(3, 2_396_651) == (
        "3 supporting sessions spent 2,396,651 tokens and recorded no work, "
        "so what they did is unreviewable."
    )
    # No tokens spent, or no silent sessions, is not a gap worth a sentence.
    assert vocab.gap_subagents_recorded_no_work(3, 0) is None
    assert vocab.gap_subagents_recorded_no_work(0, 500) is None
    assert vocab.gap_declared_paths_unobserved(2) == (
        "Checks declared 2 file paths while the capture observed no file edit, "
        "so the declared paths are unconfirmed."
    )
    assert vocab.gap_declared_paths_unobserved(0) is None
    assert vocab.gap_kind_label("blocks_review") == "Blocks review"
    assert vocab.gap_kind_label("provenance") == "Provenance bookkeeping"
    assert vocab.gap_kind_label("nonsense") == "Provenance bookkeeping"


def test_display_time_span_names_its_absence_and_collapses_one_instant(los_angeles):
    start = time.mktime((2026, 9, 12, 17, 21, 0, 0, 0, -1))
    assert vocab.display_time_span(start, start + 17 * 60) == "Sep 12 17:21–17:38"
    assert vocab.display_time_span(start, start + 2 * 24 * 3600) == "Sep 12–Sep 14"
    # One batch is one instant, not a zero-length range.
    assert vocab.display_time_span(start, start) == "Sep 12 17:21"
    # Reversed edges are still one window, and absence is named.
    assert vocab.display_time_span(start + 17 * 60, start) == "Sep 12 17:21–17:38"
    assert vocab.display_time_span(None, start) == "window not recorded"
    assert vocab.display_time_span(start, 0) == "window not recorded"


def test_timeline_span_text_names_sub_second_spans_and_absence():
    """A span shorter than the canvas's window floor is what the not-narrowable
    state reports, so the words have to survive hundredths of a second: whole
    seconds would print ``0s`` for the measured 0.30-second task, and
    ``humanize_seconds`` can only say ``<1m``.

    This table is mirrored, value for value, by
    ``WorkTimeCanvasInputTests.testSpanTextMirrorsThePythonSpanWords`` — the app
    measures the span at render time, so the rule exists in both places and the
    two tables must stay identical.
    """

    table = {
        0.3: "0.30s",
        0.75: "0.75s",
        0.996: "1.00s",
        1: "1.0s",
        4.62: "4.6s",
        9.9: "9.9s",
        10: "10s",
        42.4: "42s",
        59.4: "59s",
        60: "1m",
        138.61: "2m",
        3600: "1h 0m",
        24954.83: "6h 55m",
        183600: "2d 3h",
    }
    for seconds, expected in table.items():
        assert vocab.timeline_span_text(seconds) == expected, seconds
    # An unmeasurable extent is a NAMED absence, never a fabricated zero.
    for broken in (0, -1, None, float("nan"), float("inf"), "0.3"):
        assert vocab.timeline_span_text(broken) == vocab.TIME_SPAN_NOT_RECORDED


def test_not_narrowable_state_states_the_span_and_the_reason():
    """The canvas's overview strip is a control only while a narrower window
    exists. When none does, it prints this named state instead — the recorded
    span, and that there is nothing to narrow. The detail sentence explains the
    axis limit rather than blaming the Task."""

    assert (
        vocab.timeline_window_not_narrowable_text(0.3)
        == "Whole recorded span: 0.30s — nothing to narrow"
    )
    assert vocab.timeline_window_not_narrowable_text(None) == (
        f"Whole recorded span: {vocab.TIME_SPAN_NOT_RECORDED} — nothing to narrow"
    )
    # The state never reads as an error or as a missing measurement.
    detail = vocab.TIMELINE_WINDOW_NOT_NARROWABLE_DETAIL
    assert "shorter than the smallest window the time axis can label" in detail
    assert "{" not in detail and "}" not in detail
    assert "{span}" in vocab.TIMELINE_WINDOW_NOT_NARROWABLE
