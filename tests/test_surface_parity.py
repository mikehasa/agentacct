"""Cross-surface vocabulary parity — the guardrail behind the ONE-vocabulary rule.

agentacct has four human-facing surfaces for the same Receipt: the CLI's
terminal render, the exported Markdown, the ``agentacct tui`` panes, and the
``/v1`` payloads the macOS app renders. Display text and rules flow from ONE
Python vocabulary (``display_vocabulary`` + the reducers in ``receipt.py``);
every surface is supposed to *print* those strings, never re-derive its own.

This module is the automatic stopping rule for that: a deterministic fixture set
covering the scenarios a reviewer actually has to read — an errored check, a
genuine handoff that still carries checks, a blocked task, partial coverage, an
observed / not-check-relevant task, a fail-then-pass frontier, a failure whose
exit code disagrees with its result, and a task with many findings — asserted
across all four surfaces for the same facts:

    verdict headline · gap label + gap text · coverage wording · check tally ·
    cost display + basis · decision label · source labels · attention reason

Every expectation is taken from the reducer / ``display_vocabulary`` output at
assert time, never hard-coded here: a test that spells the words itself is just
a fifth copy of the vocabulary. A surface that does not show a given fact is
named in the test that skips it, so "the TUI has no joined headline" is a
documented design decision rather than a silent hole.

The payload lane is checked twice: once per scenario against the reducers
(``build_receipt`` / ``build_receipt_summary``), and once end-to-end against a
real store, proving ``/v1/receipt`` and ``/v1/tasks`` return exactly those
reducer outputs rather than a re-shaped copy.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

os.environ.setdefault("AGENTACCT_TUI_AUTO_IMPORT", "0")

import pytest
from fastapi.testclient import TestClient
from rich.console import Console
from rich.text import Text

import agentacct.cli as cli_module
import agentacct.tui as tui
from agentacct.api import create_local_api_app
from agentacct.display_vocabulary import (
    ASSERTED_BY_PHRASES,
    ATTENTION_REASON_LABELS,
    COST_BASIS_LABELS,
    DECISION_LABELS,
    GAP_LABEL_NOT_YET_PROVEN,
    SOURCE_LABELS,
    TIER_LABELS,
    cost_basis_label,
    decision_label,
    source_label,
)
from agentacct.receipt import (
    build_receipt,
    build_receipt_summary,
    check_tally_text,
    evidence_coverage_headline,
    evidence_coverage_ledger,
    receipt_cost_text,
)
from agentacct.receipt_markdown import (
    receipt_attention_lines,
    receipt_lead,
    render_receipt_markdown,
)
from agentacct.service import SentinelService

# --------------------------------------------------------------------------- #
# the deterministic fixture set                                               #
# --------------------------------------------------------------------------- #

# A fixed, plausible wall clock so nothing in a rendered receipt depends on the
# day the suite runs (only the "N ago" strings do, and none are asserted).
_T0 = 1_760_000_000.0

TITLE = "Add rate limit to login"
PUBLIC_ID = "task_parity"


def _check(
    result: str,
    *,
    name: str = "pytest",
    kind: str = "test",
    at: float = _T0 + 100.0,
    exit_code: int = 0,
    source_type: str = "client_hook",
    event_id: str | None = None,
    identity: str | None = None,
    supersession_state: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event_id": event_id or f"evt_{name}_{result}_{int(at)}",
        "result": result,
        "name": name,
        "evidence_type": kind,
        "created_at": at,
        "exit_code": exit_code,
        "source_type": source_type,
        "source": "claude-code",
        "check_identity": identity or f"check:{name}",
        "check_identity_stable": True,
    }
    if supersession_state:
        event["supersession_state"] = supersession_state
    return event


def _step(
    work_id: str,
    status: str,
    *,
    kind: str = "implementation",
    checks: list[dict[str, Any]] | None = None,
    at: float = _T0 + 50.0,
    **extra: Any,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "work_id": work_id,
        "section_id": work_id,
        "latest_status": status,
        "kind": kind,
        "created_at": at,
        "updated_at": at,
        "client_session_id": "s1",
        "section_title": f"Step {work_id}",
    }
    if checks is not None:
        item["current_check_events"] = checks
    item.update(extra)
    return item


# Cost shapes, one per named cost state, so the cost grammar ($ / ≈$ / ~$ and
# the two named absences) is exercised across the matrix rather than once.
_COST_ESTIMATE = {
    "rows": 2,
    "estimated_cost_usd": 0.5,
    "cost_complete": True,
    "cost_basis": "pricing_table",
    "cost_confidence": "estimated_from_tokens",
    "total_tokens": 1000,
    "fresh_tokens": 800,
}
_COST_REPORTED = {
    "rows": 2,
    "estimated_cost_usd": 1.25,
    "cost_complete": True,
    "cost_basis": "client_reported",
    "cost_confidence": "client_reported",
    "total_tokens": 1000,
    "fresh_tokens": 800,
}
_COST_PARTIAL = {
    "rows": 3,
    "estimated_cost_usd": 0.75,
    "cost_complete": False,
    "cost_basis": "pricing_table",
    "cost_confidence": "estimated_from_tokens",
    "total_tokens": 900,
    "fresh_tokens": 700,
}
_COST_UNPRICED = {
    "rows": 2,
    "estimated_cost_usd": None,
    "cost_complete": False,
    "cost_basis": None,
    "cost_confidence": None,
    "total_tokens": 400,
    "fresh_tokens": 400,
}
_COST_NO_USAGE = {
    "rows": 0,
    "estimated_cost_usd": None,
    "cost_complete": False,
    "cost_basis": None,
    "cost_confidence": None,
}


def _task(
    items: list[dict[str, Any]],
    *,
    task_checks: list[dict[str, Any]],
    usage: dict[str, Any],
) -> dict[str, Any]:
    return {
        "task_id": PUBLIC_ID,
        "public_task_id": PUBLIC_ID,
        "primary_root": {"client": "claude-code", "client_session_id": "s1"},
        "root_keys": [{"client": "claude-code", "client_session_id": "s1"}],
        "session_keys": [{"client": "claude-code", "client_session_id": "s1"}],
        "sessions": [
            {
                "client": "claude-code",
                "client_session_id": "s1",
                "project": "acme",
                "identity_scope_state": "explicit",
                "first_activity_at": _T0,
                "started_at": _T0,
                "last_activity_at": _T0 + 300.0,
                "usage": {},
            }
        ],
        "session_count": 1,
        "supporting_count": 0,
        "child_count": 0,
        "internal_count": 0,
        "last_activity_at": _T0 + 300.0,
        "work_items": items,
        "work_associations": [],
        "usage": dict(usage),
        "models": ["claude-opus"],
        "actions": {
            "tool_category_counts": {"read": 3, "edit": 1},
            "tool_category_total": 4,
            "touched_files": ["src/login.py"],
            "touched_file_count": 1,
        },
        "current_check_events": task_checks,
    }


def _errored_check_task() -> dict[str, Any]:
    """The genuine errored-check scenario: a check whose recorded result is
    ``error`` — it could not run, so it is a named gap, never a failure."""

    checks = [
        _check("error", name="pytest", exit_code=4, at=_T0 + 101.0),
        _check("error", name="mypy", exit_code=1, at=_T0 + 102.0, identity="check:mypy"),
    ]
    return _task([_step("w1", "completed", checks=checks)], task_checks=checks, usage=_COST_ESTIMATE)


def _handoff_with_checks_task() -> dict[str, Any]:
    """A genuine handoff: a deliberate stop that still carries passing checks,
    beside a completed, checked step."""

    checks = [_check("passed", name="pytest", at=_T0 + 101.0)]
    return _task(
        [
            _step("w1", "handed_off", checks=checks, next_step="hand to the release owner"),
            _step("w2", "completed", checks=checks),
        ],
        task_checks=checks,
        usage=_COST_REPORTED,
    )


def _blocked_task() -> dict[str, Any]:
    return _task(
        [_step("w1", "blocked", blocker="The staging migration needs an owner role.")],
        task_checks=[],
        usage=_COST_NO_USAGE,
    )


def _partial_coverage_task() -> dict[str, Any]:
    checks = [_check("passed", name="pytest", at=_T0 + 101.0)]
    return _task(
        [_step("w1", "completed", checks=checks), _step("w2", "completed")],
        task_checks=checks,
        usage=_COST_PARTIAL,
    )


def _observed_task() -> dict[str, Any]:
    """Nothing check-relevant was done: review + research steps only."""

    return _task(
        [_step("w1", "completed", kind="review"), _step("w2", "completed", kind="research")],
        task_checks=[],
        usage=_COST_UNPRICED,
    )


def _fail_then_pass_task() -> dict[str, Any]:
    earlier = _check(
        "failed",
        name="pytest",
        at=_T0 + 101.0,
        exit_code=1,
        event_id="evt_pytest_first",
        supersession_state="superseded",
    )
    later = _check("passed", name="pytest", at=_T0 + 200.0, event_id="evt_pytest_second")
    return _task(
        [_step("w1", "completed", checks=[earlier, later])],
        task_checks=[earlier, later],
        usage=_COST_ESTIMATE,
    )


def _failed_with_exit_zero_task() -> dict[str, Any]:
    """A recorded failure whose exit code says success — the receipt names the
    disagreement instead of silently re-grading either side."""

    checks = [_check("failed", name="pytest", at=_T0 + 101.0, exit_code=0)]
    return _task([_step("w1", "completed", checks=checks)], task_checks=checks, usage=_COST_ESTIMATE)


def _many_findings_task() -> dict[str, Any]:
    checks = [
        _check(
            "failed",
            name=f"check-{index}",
            at=_T0 + 100.0 + index,
            exit_code=1,
            identity=f"check:c{index}",
        )
        for index in range(4)
    ]
    return _task(
        [_step(f"w{index}", "completed", checks=[checks[index]]) for index in range(4)],
        task_checks=checks,
        usage=_COST_ESTIMATE,
    )


SCENARIOS: dict[str, Any] = {
    "errored_check": _errored_check_task,
    "handoff_with_checks": _handoff_with_checks_task,
    "blocked": _blocked_task,
    "partial_coverage": _partial_coverage_task,
    "observed": _observed_task,
    "fail_then_pass": _fail_then_pass_task,
    "failed_with_exit_zero": _failed_with_exit_zero_task,
    "many_findings": _many_findings_task,
}

SCENARIO_NAMES = sorted(SCENARIOS)


# --------------------------------------------------------------------------- #
# the four surfaces                                                           #
# --------------------------------------------------------------------------- #


def _receipt(name: str) -> dict[str, Any]:
    return build_receipt(SCENARIOS[name](), public_task_id=PUBLIC_ID, title=TITLE)


def _summary(name: str) -> dict[str, Any]:
    return build_receipt_summary(SCENARIOS[name](), public_task_id=PUBLIC_ID, title=TITLE)


def _normalize(text: str) -> str:
    """Collapse whitespace so a comparison survives Rich's column padding and
    Markdown's line wrapping (the WORDS are the contract, not the columns)."""

    return " ".join(str(text).split())


def _cli_text(receipt: dict[str, Any]) -> str:
    """The CLI's terminal Work Receipt, rendered wide enough not to wrap."""

    recorder = Console(width=400, record=True, force_terminal=False, no_color=True, legacy_windows=False)
    previous = cli_module.console
    cli_module.console = recorder
    try:
        cli_module._render_receipt_text(receipt)
    finally:
        cli_module.console = previous
    return _normalize(recorder.export_text())


def _markdown_text(receipt: dict[str, Any]) -> str:
    return _normalize(render_receipt_markdown(receipt))


def _tui_receipt_text(receipt: dict[str, Any]) -> str:
    """The TUI's Work-Receipt detail panes as plain text (markup stripped)."""

    parts = tui._build_receipt_parts(receipt, tui._LIGHT, width=260)
    return _normalize(Text.from_markup("\n".join(parts.values())).plain)


def _tui_card_text(summary: dict[str, Any]) -> str:
    """One TUI master-list ROW as plain text.

    The Sessions master list is a DataTable now, not a stack of expanded cards,
    so the row builder returns its own plain-text mirror (the second element)
    for exactly this: asserting row content without a terminal width.
    """

    _cells, plain = tui._work_row_cells(summary, tui._LIGHT, 260)
    return _normalize(plain)


def _text_surfaces(receipt: dict[str, Any]) -> dict[str, str]:
    """The three text surfaces that render a FULL receipt. The ``/v1`` lane is
    structured JSON, so it is asserted field-by-field rather than by substring."""

    return {
        "cli": _cli_text(receipt),
        "markdown": _markdown_text(receipt),
        "tui": _tui_receipt_text(receipt),
    }


def _assert_in_all(surfaces: dict[str, str], expected: str, *, what: str) -> None:
    needle = _normalize(expected)
    assert needle, f"{what}: the reducer produced an empty string, so nothing can be asserted"
    for surface, rendered in surfaces.items():
        assert needle in rendered, (
            f"{surface} does not print the reducer's {what}.\n"
            f"expected: {needle!r}\nrendered: {rendered[:1600]!r}"
        )


# --------------------------------------------------------------------------- #
# 1. verdict headline                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario", SCENARIO_NAMES)
def test_verdict_headline_is_one_reducer_string_everywhere(scenario: str) -> None:
    """The CLI, the Markdown and both ``/v1`` payloads lead with the SAME
    headline string. The TUI shows a decision BADGE beside a coverage tile
    instead of the joined sentence, so it is asserted on both halves."""

    receipt = _receipt(scenario)
    summary = _summary(scenario)
    headline = receipt["verdict"]["headline"]

    # The reducer's own words, not a copy: label + the shared coverage clause.
    assert headline.startswith(f"{decision_label(receipt['verdict']['decision_key'])} — ")

    assert _normalize(headline) in _cli_text(receipt)
    assert _normalize(headline) in _markdown_text(receipt)
    # The list row and the detail can never word the verdict differently.
    assert summary["verdict"]["headline"] == headline
    assert summary["verdict"]["proof_clause"] == receipt["verdict"]["proof_clause"]

    tui_text = _tui_receipt_text(receipt)
    assert _normalize(receipt["axes"]["decision_status"]["label"]) in tui_text
    coverage_tile = receipt["axes"]["evidence_strength"]["coverage_tile"]
    assert _normalize(str(coverage_tile["value"] or coverage_tile["absent"])) in tui_text


# --------------------------------------------------------------------------- #
# 2. gap label + gap text                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario", SCENARIO_NAMES)
def test_gap_label_and_text_are_the_reducers_typed_gap(scenario: str) -> None:
    """``Not yet proven`` sits over the unproven part ONLY, and every text
    surface prints the reducer's own ``<label>: <text>`` line. When nothing is
    unproven no surface may invent the label."""

    receipt = _receipt(scenario)
    summary = _summary(scenario)
    verdict = receipt["verdict"]
    surfaces = _text_surfaces(receipt)
    gap_line = receipt_lead(receipt)["gap_line"]

    assert summary["verdict"]["gap_label"] == verdict["gap_label"]
    assert summary["verdict"]["gap_text"] == verdict["gap_text"]
    assert summary["verdict"]["ledger_text"] == verdict["ledger_text"]

    if verdict["gap_label"]:
        assert verdict["gap_label"] == GAP_LABEL_NOT_YET_PROVEN
        assert gap_line == f"{verdict['gap_label']}: {verdict['gap_text']}"
        _assert_in_all(surfaces, gap_line, what="verdict gap line")
    else:
        assert not gap_line
        for surface, rendered in surfaces.items():
            assert GAP_LABEL_NOT_YET_PROVEN not in rendered, (
                f"{surface} prints {GAP_LABEL_NOT_YET_PROVEN!r} for a task the reducer "
                "reports nothing unproven for"
            )


# --------------------------------------------------------------------------- #
# 3. coverage wording                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario", SCENARIO_NAMES)
def test_coverage_wording_comes_from_the_one_coverage_reducer(scenario: str) -> None:
    """Coverage is a RATIO with tier words from the shared tier table (or the
    named ``Not gradeable (<reason>)`` absence) — one string, shared."""

    receipt = _receipt(scenario)
    summary = _summary(scenario)
    evidence = receipt["axes"]["evidence_strength"]

    # The payload carries the reducer's own output, not a re-render.
    assert evidence["coverage_hero"] == evidence_coverage_headline(evidence)
    assert (evidence["coverage_ledger"] or "") == evidence_coverage_ledger(evidence)
    for field in ("coverage_hero", "coverage_row", "coverage_tile"):
        assert summary["evidence_strength"][field] == evidence[field]

    # The CLI and Markdown print the hero verbatim; the TUI prints the tile the
    # same reducer built (a KPI cell, not a sentence) plus its qualifier.
    hero = _normalize(evidence["coverage_hero"])
    assert hero in _cli_text(receipt)
    assert hero in _markdown_text(receipt)

    tui_text = _tui_receipt_text(receipt)
    tile = evidence["coverage_tile"]
    assert _normalize(str(tile["value"] or tile["absent"])) in tui_text
    assert _normalize(str(tile["qualifier"])) in tui_text

    if evidence["coverage_ledger"]:
        _assert_in_all(_text_surfaces(receipt), evidence["coverage_ledger"], what="coverage ledger")

    # Tier words are the shared table's, never a surface's own spelling.
    if evidence["gradeable"] and evidence["checked_total"]:
        assert any(label in evidence["coverage_hero"] for label in TIER_LABELS.values())


# --------------------------------------------------------------------------- #
# 4. check tally                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario", SCENARIO_NAMES)
def test_check_tally_is_one_string_with_named_remainders(scenario: str) -> None:
    """One tally, with every remainder named (``could not run`` is never folded
    into ``failed``). No surface may drop a part another surface names."""

    receipt = _receipt(scenario)
    summary = _summary(scenario)
    evidence = receipt["axes"]["evidence_strength"]
    tally = evidence["check_tally_text"]

    assert tally == check_tally_text(evidence)
    assert receipt["dimensions"]["evidence"]["check_tally_text"] == tally
    assert summary["evidence_strength"]["check_tally_text"] == tally
    assert summary["evidence_strength"]["checks_tile"] == evidence["checks_tile"]

    assert _normalize(tally) in _cli_text(receipt)
    assert _normalize(tally) in _markdown_text(receipt)

    # The TUI's checks KPI is the same tally split at its ratio.
    tui_text = _tui_receipt_text(receipt)
    tile = evidence["checks_tile"]
    assert _normalize(str(tile["value"] or tile["absent"])) in tui_text
    if tile["qualifier"]:
        assert _normalize(str(tile["qualifier"])) in tui_text

    # A check that could not run is its own named remainder everywhere.
    if evidence["checks_not_run"]:
        assert evidence["checks_not_run_text"]
        _assert_in_all(
            _text_surfaces(receipt), evidence["checks_not_run_text"], what="checks-could-not-run gap"
        )


# --------------------------------------------------------------------------- #
# 5. cost display + basis                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario", SCENARIO_NAMES)
def test_cost_display_and_basis_are_one_grammar(scenario: str) -> None:
    """Every cost carries its basis, in one grammar: ``$`` reported/billed,
    ``≈$`` estimate, ``~$`` partial subtotal, or a NAMED absence."""

    receipt = _receipt(scenario)
    summary = _summary(scenario)
    cost = receipt["dimensions"]["cost"]
    cost_line = receipt_cost_text(cost)

    # The CLI and Markdown print the amount AND its spelled-out basis together.
    assert _normalize(cost_line) in _cli_text(receipt)
    assert _normalize(cost_line) in _markdown_text(receipt)

    # The TUI's cost KPI is the payload's display text (basis on its sub-line
    # once there is a figure to qualify).
    tui_text = _tui_receipt_text(receipt)
    assert _normalize(str(cost["display_text"])) in tui_text
    if cost["state"] not in {"no_usage", "unpriced"}:
        assert _normalize(str(cost["basis_label"])) in tui_text
        assert cost["basis_label"] == cost_basis_label(cost.get("cost_basis") or cost.get("cost_confidence"))
        assert cost["basis_label"] in COST_BASIS_LABELS.values()

    # The list row's cost is the same reducer's fields and the same rendered text.
    row_cost = summary["cost"]
    assert row_cost["display_text"] == cost["display_text"]
    assert row_cost["basis_label"] == cost["basis_label"]
    assert row_cost["state"] == cost["state"]
    assert _normalize(str(row_cost["display_text"])) in _tui_card_text(summary)


# --------------------------------------------------------------------------- #
# 6. decision label                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario", SCENARIO_NAMES)
def test_decision_label_is_the_vocabulary_label_on_every_surface(scenario: str) -> None:
    """The decision word is the shared table's label — never a re-cased raw key
    — and the phrase after ``asserted by`` is the grammatical one, not the chip."""

    receipt = _receipt(scenario)
    summary = _summary(scenario)
    decision = receipt["axes"]["decision_status"]
    label = decision["label"]

    assert label == decision_label(decision["key"])
    assert label in DECISION_LABELS.values()
    assert decision["asserted_by_phrase"] == ASSERTED_BY_PHRASES[decision["asserted_by"]]

    surfaces = _text_surfaces(receipt)
    _assert_in_all(surfaces, label, what="decision label")
    for name in ("cli", "markdown"):
        assert f"asserted by {decision['asserted_by_phrase']}" in surfaces[name]
        # The chip label never lands in prose after "asserted by".
        assert f"asserted by {decision['asserted_by_label']}" not in surfaces[name]

    assert summary["decision_status"]["label"] == label
    assert summary["decision_status"]["asserted_by_phrase"] == decision["asserted_by_phrase"]
    assert _normalize(label) in _tui_card_text(summary)


# --------------------------------------------------------------------------- #
# 7. source labels                                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario", SCENARIO_NAMES)
def test_source_labels_are_rendered_never_raw_keys(scenario: str) -> None:
    """Provenance keys stay in the data; only ``source_label`` spellings show."""

    receipt = _receipt(scenario)
    surfaces = _text_surfaces(receipt)
    dims = receipt["dimensions"]

    keys = {
        str(key)
        for name in ("task", "actors", "actions", "cost", "evidence", "outcome")
        for key in (dims[name].get("provenance") or [])
    }
    assert keys, "the fixture should exercise at least one provenance source"

    for key in keys:
        label = source_label(key)
        assert label == SOURCE_LABELS[key]["label"]
        for surface in ("cli", "markdown"):
            assert label in surfaces[surface], f"{surface} does not render source label {label!r}"

    # The raw snake_case keys must never reach a reader on any text surface.
    # Only multi-word keys are checked: a single-word key like ``hook`` is also
    # an ordinary English word inside the shared provenance prose ("captured by
    # an agentacct client hook"), so its bare appearance proves nothing.
    for key in keys:
        if "_" not in key:
            continue
        for surface, rendered in surfaces.items():
            assert not re.search(rf"\b{re.escape(key)}\b", rendered), (
                f"{surface} leaked the raw provenance key {key!r} instead of {source_label(key)!r}"
            )

    # Each check row carries the shared label too, so no surface maps keys itself.
    for check in receipt["dimensions"]["evidence"].get("checks") or []:
        assert check["source_label"] == source_label(check["source"])


# --------------------------------------------------------------------------- #
# 8. attention reason                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario", SCENARIO_NAMES)
def test_attention_reason_is_the_reducers_words(scenario: str) -> None:
    """One attention block rides the receipt, each summary row and ``/v1``; the
    reason words are the shared table's, and every text surface prints them."""

    receipt = _receipt(scenario)
    summary = _summary(scenario)
    attention = receipt["attention"]

    assert summary["attention"] == attention
    assert summary["attention_open"] == receipt["attention_open"]

    if attention is None:
        return

    lines = receipt_attention_lines(receipt)
    reason = lines[0]
    assert attention["reason_label"] in ATTENTION_REASON_LABELS.values()
    _assert_in_all(_text_surfaces(receipt), reason, what="attention reason")

    # The disposition sentence is shared prose, printed by all three surfaces.
    disposition = next(line for line in lines if line.startswith("Disposition:"))
    _assert_in_all(_text_surfaces(receipt), disposition, what="attention disposition sentence")


# --------------------------------------------------------------------------- #
# the /v1 lane is the reducer output, end to end                                #
# --------------------------------------------------------------------------- #

_V1_TOKEN = "surface-parity-token"
_NS = "sha256:surface-parity-ns"


def _seed_store(store: Path) -> None:
    """One real Task through the real ingest path, so the endpoint lane is
    exercised against a store rather than a hand-built projection."""

    service = SentinelService(store)
    service.record_event(
        {
            "event_id": "evt_usage_s1",
            "created_at": _T0,
            "source": "claude-code-local-session-import",
            "event_type": "model_usage",
            "run_id": None,
            "provider": "claude-code",
            "model": "claude-opus-4-8",
            "estimated_input_tokens": 100,
            "estimated_output_tokens": 25,
            "estimated_cost_usd": 0.5,
            "usage_confidence": "client_reported",
            "cost_confidence": "estimated_from_tokens",
            "cost_basis": "pricing_table",
            "metadata": {
                "usage_source": "local_client_session_store",
                "usage_provenance": "agent_sentinel_local_usage_import",
                "client": "claude-code",
                "client_session_id": "s1",
                "cached_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "project_dir": "/tmp/project",
                "started_at": _T0,
                "updated_at": _T0,
                "session_namespace_fingerprint": _NS,
                "identity_scope_state": "explicit",
                "source_namespace_fingerprint": _NS,
            },
        },
        trusted_usage_import=True,
    )
    service.record_event(
        {
            "event_id": "evt_section_s1",
            "created_at": _T0 + 1.0,
            "source": "claude-code",
            "event_type": "section_completed",
            "run_id": None,
            "metadata": {
                "sentinel_semantic_kind": "section",
                "client": "claude-code",
                "client_session_id": "s1",
                "client_context_keys_authored": ["client_session_id"],
                "project_dir": "/tmp/project",
                "session_namespace_fingerprint": _NS,
                "identity_scope_state": "explicit",
                "section_id": "sec-1",
                "section_status": "completed",
                "section_title": TITLE,
                "kind": "implementation",
                "files": ["src/login.py"],
                "summary": "Recorded outcome for this fixture section.",
            },
        }
    )
    service.record_event(
        {
            "event_id": "evt_check_s1",
            "created_at": _T0 + 2.0,
            "source": "claude-code",
            "event_type": "machine_check",
            "run_id": None,
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "result": "error",
                "evidence_type": "test",
                "summary": "pytest could not run",
                "name": "pytest",
                "exit_code": 4,
                "section_id": "sec-1",
                "client": "claude-code",
                "client_session_id": "s1",
                "session_namespace_fingerprint": _NS,
                "identity_scope_state": "explicit",
                "project_dir": "/tmp/project",
            },
        }
    )


def test_v1_endpoints_return_exactly_the_receipt_reducers_output(tmp_path: Path) -> None:
    """``/v1/receipt`` and ``/v1/tasks`` serve the reducer payload itself — not a
    re-shaped copy — so every parity assertion above holds for the app lane too.

    This is the link the per-scenario tests assume: they compare the reducers
    against the text surfaces; this compares the HTTP lane against the reducers.
    """

    _seed_store(tmp_path)
    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=_V1_TOKEN))
    auth = {"Authorization": f"Bearer {_V1_TOKEN}"}

    listing = client.get("/v1/tasks", headers=auth)
    assert listing.status_code == 200, listing.text
    rows = listing.json()["tasks"]
    assert rows, "the seeded store should expose one Task"
    row = rows[0]

    receipt = client.get(f"/v1/receipt?task={row['task_id']}", headers=auth)
    assert receipt.status_code == 200, receipt.text
    payload = receipt.json()

    # The words the list row and the detail lead with are one string.
    assert row["verdict"]["headline"] == payload["verdict"]["headline"]
    assert row["verdict"]["gap_label"] == payload["verdict"]["gap_label"]
    assert row["verdict"]["gap_text"] == payload["verdict"]["gap_text"]
    assert row["decision_status"]["label"] == payload["axes"]["decision_status"]["label"]
    assert (
        row["evidence_strength"]["check_tally_text"]
        == payload["axes"]["evidence_strength"]["check_tally_text"]
    )
    assert row["evidence_strength"]["coverage_hero"] == payload["axes"]["evidence_strength"]["coverage_hero"]
    assert row["cost"]["display_text"] == payload["dimensions"]["cost"]["display_text"]
    assert row["cost"]["basis_label"] == payload["dimensions"]["cost"]["basis_label"]
    assert row["attention"] == payload["attention"]

    # And the same payload renders through the text surfaces unchanged.
    surfaces = _text_surfaces(payload)
    _assert_in_all(surfaces, payload["axes"]["decision_status"]["label"], what="decision label")
    for name in ("cli", "markdown"):
        assert _normalize(payload["verdict"]["headline"]) in surfaces[name]
        assert _normalize(payload["axes"]["evidence_strength"]["check_tally_text"]) in surfaces[name]
        assert _normalize(receipt_cost_text(payload["dimensions"]["cost"])) in surfaces[name]


# --------------------------------------------------------------------------- #
# the two composed lines have exactly ONE composer                              #
# --------------------------------------------------------------------------- #

# `not_captured_line()` and `check_meta_line()` compose text rather than label a
# value, which is the shape a surface is most tempted to re-implement: joining
# four fields with a separator looks like formatting, not vocabulary. These pin
# the composition to display_vocabulary BEFORE any surface prints it, so the
# second copy is caught at the moment someone writes it.
#
# The reducer NOW emits both (`dimensions.gaps.not_captured.line` and each
# check's `meta_line`), so the emission itself is asserted below against every
# scenario and against the HTTP lane. What is still NOT asserted, and named here
# rather than left as a silent hole: the CLI, TUI and Markdown export continue to
# print the per-dimension gap LIST rather than the collapsed line. That is a
# deliberate difference of medium — a terminal render has no vertical budget to
# defend — and when a surface does adopt the collapsed line it must print
# `dimensions.gaps.not_captured.line` verbatim, never rebuild it, which is what
# the two tests below already enforce.
_VOCABULARY_MODULE = "display_vocabulary.py"
_SURFACE_MODULES = ("cli.py", "tui.py", "receipt_markdown.py", "api.py", "receipt.py")


def _string_literals(name: str) -> list[str]:
    """Every string CONSTANT in one module — comments and identifiers excluded,
    so a comment mentioning a phrase is never read as a surface spelling it."""

    import ast

    source = (Path(__file__).resolve().parent.parent / "src" / "agentacct" / name).read_text(
        encoding="utf-8"
    )
    return [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_the_absence_budget_has_one_composer_and_no_surface_spells_it() -> None:
    """One record's absence line and another's must be the same sentence for the
    same set. A surface that spelled the prefix or a noun itself would be a
    second budget with its own wording and its own order."""

    from agentacct.display_vocabulary import (
        NOT_CAPTURED_NOUNS,
        NOT_CAPTURED_PREFIX,
        not_captured_line,
    )

    assert NOT_CAPTURED_PREFIX in _string_literals(_VOCABULARY_MODULE)
    # The nouns ARE the vocabulary. Only the multi-word ones are checked: a
    # bare "cost" or "model" is a payload key everywhere in this codebase.
    owned = {NOT_CAPTURED_PREFIX} | {noun for noun in NOT_CAPTURED_NOUNS.values() if " " in noun}
    for module in _SURFACE_MODULES:
        literals = set(_string_literals(module))
        spelled = sorted(owned & literals)
        assert not spelled, (
            f"{module} spells {spelled} itself; print "
            "display_vocabulary.not_captured_line() instead"
        )
    # The line is stable under caller order: two records with the same budget
    # word it identically, which is the whole point of a declaration order.
    keys = list(NOT_CAPTURED_NOUNS)[:3]
    assert not_captured_line(keys) == not_captured_line(list(reversed(keys)))


def test_the_check_meta_line_has_one_composer_and_one_separator() -> None:
    """`Passed. Exit 0. test. Agent-reported` was four fragments punctuated as
    four sentences. The replacement is ONE line, and the separator a reader (and
    a wrapper) may break on is the vocabulary's, not a surface's."""

    from agentacct.display_vocabulary import META_SEPARATOR, check_meta_line, checks_heading_line

    assert META_SEPARATOR == " · "
    line = check_meta_line("Passed", 0, "test", "Agent-reported")
    assert line.count(META_SEPARATOR) == 3 and "." not in line
    # Both composers use the same separator, so a hoisted field reads the same
    # on the heading as it did on the row it left.
    assert checks_heading_line("2/2 passed", "self-checked").count(META_SEPARATOR) == 1
    # No surface may build the exit-code fragment itself.
    for module in _SURFACE_MODULES:
        spelled = [text for text in _string_literals(module) if text.strip() in {"Exit", "Exit {}"}]
        assert not spelled, f"{module} composes an exit-code fragment itself: {spelled}"


@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_the_reducer_emits_both_composed_lines_for_every_scenario(name: str) -> None:
    """Every check row ships its own ``meta_line`` and every record ships its
    absence budget, so a surface never has four words and a separator to join.

    The expected text is taken from the composers at assert time, so this cannot
    become a fifth copy of the vocabulary.
    """

    from agentacct.display_vocabulary import (
        check_meta_line,
        collapse_not_captured_keys,
        not_captured_line,
    )

    payload = _receipt(name)
    evidence = payload["dimensions"]["evidence"]
    hoisted_type = evidence["hoisted_evidence_type"]
    hoisted_source = evidence["hoisted_source_label"]
    for row in evidence["checks"]:
        assert row["meta_line"] == check_meta_line(
            row["result_label"],
            row["exit_code"],
            None if hoisted_type else row["evidence_type"],
            None if hoisted_source else row["source_label"],
        )
        # A composed line, never a run of fragments punctuated as sentences.
        assert ". " not in row["meta_line"]

    budget = payload["dimensions"]["gaps"]["not_captured"]
    assert budget["line"] == not_captured_line(budget["keys"])
    assert budget["keys"] == collapse_not_captured_keys(
        [row["key"] for row in budget["detail"]]
    )
    # Absence is never deleted to fit the line.
    assert budget["detail_count"] == len(budget["detail"]) >= len(budget["keys"])


@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_the_revision_grouping_is_the_reducers_decision_not_a_surface_heuristic(
    name: str,
) -> None:
    """The fail -> pass fallback has to be decided once. Two surfaces grouping the
    same record two ways would be two grouping vocabularies, so the mode ships on
    the payload and the groups cover every row exactly once."""

    from agentacct.receipt import CHECK_GROUPING_BY_REVISION, CHECK_GROUPING_TIME_ORDER

    evidence = _receipt(name)["dimensions"]["evidence"]
    assert evidence["revision_grouping_mode"] in {
        CHECK_GROUPING_BY_REVISION,
        CHECK_GROUPING_TIME_ORDER,
    }
    walked = [id_ for group in evidence["revision_groups"] for id_ in group["event_ids"]]
    assert sorted(walked) == sorted(row["event_id"] for row in evidence["checks"])
    for group in evidence["revision_groups"]:
        # The header wording is the row's own label, never a second spelling of
        # the same stamp.
        assert group["label"] in {
            row["revision_label"]
            for row in evidence["checks"]
            if row["event_id"] in group["event_ids"]
        }
        # A banner and a row never both print the sentence.
        if group["contradiction_text"]:
            assert all(
                row["revision_contradiction_text"] is None
                for row in evidence["checks"]
                if row["event_id"] in group["event_ids"]
            )


def test_no_surface_spells_a_grouping_mode_or_a_summary_ellipsis() -> None:
    """The grouping mode is a reducer decision and the preview carries no
    ellipsis of its own; a surface spelling either would be re-deciding it."""

    from agentacct.receipt import CHECK_GROUPING_BY_REVISION, CHECK_GROUPING_TIME_ORDER

    owned = {CHECK_GROUPING_BY_REVISION, CHECK_GROUPING_TIME_ORDER}
    for module in ("cli.py", "tui.py", "receipt_markdown.py", "api.py"):
        spelled = sorted(owned & set(_string_literals(module)))
        assert not spelled, f"{module} decides the check grouping itself: {spelled}"
    # The sentence-boundary preview terminates on a full stop; an ellipsis would
    # be a second punctuation vocabulary for the same elision.
    assert "…" not in _string_literals(_VOCABULARY_MODULE)


# --------------------------------------------------------------------------- #
# the Swift lint's list is pinned to this vocabulary, so it cannot go stale     #
# --------------------------------------------------------------------------- #

_APP_ROOT = Path(__file__).resolve().parent.parent / "apps" / "agentacct"
_VOCABULARY_LINT = _APP_ROOT / "Tests" / "agentacctTests" / "VocabularyLintTests.swift"
_THEME = _APP_ROOT / "Sources" / "agentacct" / "Theme.swift"
_APP_FIXTURE = _APP_ROOT / "Tests" / "agentacctTests" / "Fixtures" / "dashboard.json"

# Phrases the Swift lint bans that this vocabulary does not itself spell: a
# wording the app must never invent ("claimed, unproven"), and the de-snaked
# spelling of a basis key whose label is shorter ("user subscription").
_LINT_EXTRA_BANS = {"claimed, unproven", "user subscription"}


def _swift_string_set(marker: str) -> set[str]:
    """The quoted strings of one `static let <marker>: Set<String> = [...]`."""

    source = _VOCABULARY_LINT.read_text(encoding="utf-8")
    start = source.index("[", source.index(f"static let {marker}"))
    depth = 0
    end = start
    for index in range(start, len(source)):
        if source[index] == "[":
            depth += 1
        elif source[index] == "]":
            depth -= 1
            if depth == 0:
                end = index
                break
    assert end > start, f"could not read the {marker} literal"
    # Drop `//` comment tails so a comment's words are not read as entries.
    lines = [line.split("//", 1)[0] for line in source[start : end + 1].splitlines()]
    return set(re.findall(r'"([^"]*)"', "\n".join(lines)))


def _python_display_vocabulary() -> set[str]:
    from agentacct import display_vocabulary as vocabulary

    words = set()
    for table in (
        vocabulary.DECISION_LABELS,
        vocabulary.TIER_LABELS,
        vocabulary.COST_BASIS_LABELS,
        vocabulary.ATTENTION_REASON_LABELS,
        vocabulary.ASSERTED_BY_LABELS,
        vocabulary.CHECK_RESULT_LABELS,
    ):
        words |= set(table.values())
    words |= {str(entry["label"]) for entry in vocabulary.SOURCE_LABELS.values()}
    words |= {
        vocabulary.GAP_LABEL_NOT_YET_PROVEN,
        vocabulary.NOT_GRADEABLE_TEXT,
        vocabulary.COST_ABSENT_NO_USAGE,
        vocabulary.COST_ABSENT_UNPRICED,
        vocabulary.EVIDENCE_GRADE_NOT_GRADED,
        vocabulary.EVIDENCE_GRADE_NOT_CHECK_RELEVANT,
        # The absence budget's one prefix, pinned here so Swift cannot spell a
        # second budget with its own noun order.
        vocabulary.NOT_CAPTURED_PREFIX,
        # The record page's first exempt absence, now that the reducer emits it
        # as `dimensions.task.goal_absent_text`: the app renders that field and
        # nothing else, so the words may not appear in Swift at all.
        #
        # NEXT_STEP_ABSENT is still deliberately NOT pinned: no payload field
        # carries it, so `NextStepRow.absence` remains the only source of those
        # words. It joins this set in the change that emits the SS4 next-step
        # absence — see the matching note in VocabularyLintTests.
        vocabulary.TASK_GOAL_ABSENT,
    }
    return {word for word in words if word}


@pytest.mark.skipif(not _VOCABULARY_LINT.exists(), reason="the macOS app is not in this checkout")
def test_swift_vocabulary_lint_covers_every_word_this_module_owns() -> None:
    """The Swift lint bans exactly this vocabulary (minus the key-shaped words a
    scanner cannot classify). Adding a decision word or a cost basis in Python
    without teaching the lint would leave the app free to hard-code it, so the
    two lists are pinned to each other here rather than kept in sync by hand.
    """

    banned = _swift_string_set("bannedVocabulary")
    exempt = _swift_string_set("keyShapedExemptions")
    expected = _python_display_vocabulary()

    missing = sorted(expected - banned - exempt)
    assert not missing, (
        "VocabularyLintTests.bannedVocabulary does not cover these display words: "
        f"{missing} — add them there (or, if the label equals its payload key, to "
        "keyShapedExemptions with a comment)"
    )
    stale = sorted(banned - expected - _LINT_EXTRA_BANS)
    assert not stale, (
        f"VocabularyLintTests.bannedVocabulary lists words this vocabulary no longer has: {stale}"
    )
    # The exemption is a narrow escape hatch, not a second vocabulary.
    assert exempt <= expected, sorted(exempt - expected)


@pytest.mark.skipif(not _THEME.exists(), reason="the macOS app is not in this checkout")
def test_swift_tier_fallback_labels_are_this_modules_tier_words() -> None:
    """``EvidenceTierStyle.forGrade`` carries a last-resort tier label for a
    payload that omitted one. It is allowlisted in the Swift lint, so it is
    pinned HERE instead: every label must be this vocabulary's word for that
    grade, character for character (a hyphenated "externally-verified" is a
    second vocabulary the reviewer would read beside the CLI's spelling).
    """

    from agentacct.display_vocabulary import EVIDENCE_GRADE_LABELS

    source = _THEME.read_text(encoding="utf-8")
    start = source.index("static func forGrade(")
    body = source[start : source.index("\n    }", start)]
    pairs = re.findall(r'case "([a-z_]+)":.*?label: "([^"]*)"', body, flags=re.S)
    assert pairs, "could not read EvidenceTierStyle.forGrade — has it been restructured?"

    for grade, label in pairs:
        expected = EVIDENCE_GRADE_LABELS.get(grade)
        assert expected is not None, f"Theme.swift grades {grade!r}, which this vocabulary does not know"
        assert label == expected, (
            f"Theme.swift labels the {grade!r} grade {label!r}; this vocabulary says {expected!r}"
        )
    # Every grade the vocabulary names is covered by the fallback — including
    # ``none``, whose word is "not graded": a default branch that echoed the
    # raw key would have spelled it "none" on the app and "not graded" in the
    # CLI for the same Task.
    covered = {grade for grade, _ in pairs}
    assert covered >= set(TIER_LABELS)
    assert covered >= set(EVIDENCE_GRADE_LABELS), sorted(set(EVIDENCE_GRADE_LABELS) - covered)
    # The default branch must not re-spell a grade either: it de-snakes an
    # unknown key and falls back to the "none" word, exactly as Python does.
    default = body[body.index("default:"):]
    assert 'grade ?? "none"' not in default, (
        "EvidenceTierStyle.forGrade's default echoes the raw grade key; Python says "
        f"{EVIDENCE_GRADE_LABELS['none']!r}"
    )


_TIME_CANVAS = _APP_ROOT / "Sources" / "agentacct" / "WorkTimeCanvas.swift"


@pytest.mark.skipif(not _TIME_CANVAS.exists(), reason="the macOS app is not in this checkout")
def test_swift_not_narrowable_state_is_this_vocabularys_sentence() -> None:
    """The time canvas's overview strip stops being a control when the whole
    recorded span is already shorter than the window floor, and prints a NAMED
    state in its place. Only the measured span is the app's (a number no reducer
    can know); every word is this module's, so both literals are pinned here
    character for character — the same arrangement the freshness phrase uses.
    """

    from agentacct.display_vocabulary import (
        TIMELINE_WINDOW_NOT_NARROWABLE,
        TIMELINE_WINDOW_NOT_NARROWABLE_DETAIL,
    )

    source = _TIME_CANVAS.read_text(encoding="utf-8")
    for constant, expected in (
        ("notNarrowableTemplate", TIMELINE_WINDOW_NOT_NARROWABLE),
        ("notNarrowableDetail", TIMELINE_WINDOW_NOT_NARROWABLE_DETAIL),
    ):
        match = re.search(rf'static let {constant} = "([^"]*)"', source)
        assert match, f"WorkTimeCanvas.swift no longer declares {constant} — has it been restructured?"
        assert match.group(1) == expected, (
            f"WorkTimeCanvas.swift spells {constant} {match.group(1)!r}; this vocabulary says {expected!r}"
        )
    # The app fills in the span itself; the placeholder is what makes that a
    # substitution rather than a second sentence.
    assert "{span}" in TIMELINE_WINDOW_NOT_NARROWABLE
    assert 'replacingOccurrences(of: "{span}"' in source


@pytest.mark.skipif(not _APP_FIXTURE.exists(), reason="the macOS app is not in this checkout")
def test_app_fixture_status_legend_is_this_vocabularys_words() -> None:
    """The app's offline fixture SYNTHESIZES a daemon payload, so its status
    legend is what every fixture render and legend test reads. A definition
    left behind by a wording change is a stale second vocabulary sitting in the
    tree: the fixture said the handoff definition one way for a whole release
    while the CLI, the TUI and the live app said it another.

    Only words this vocabulary owns are pinned — a fixture may still carry an
    older payload SHAPE (that is what it is for); it may not carry older WORDS.
    """

    import json

    from agentacct.display_vocabulary import decision_legend

    legend = json.loads(_APP_FIXTURE.read_text(encoding="utf-8"))["tasks"].get("decision_legend")
    assert legend, "the app fixture no longer ships a decision legend — has it been restructured?"

    current = {row["key"]: row for row in decision_legend()["decisions"]}
    drift: list[str] = []
    for row in legend.get("decisions", []):
        expected = current.get(row["key"])
        if expected is None:
            drift.append(f"decision {row['key']!r} is not a word this vocabulary has")
            continue
        for field in ("label", "definition", "group_key"):
            if row.get(field) != expected[field]:
                drift.append(
                    f"decision {row['key']!r} {field}:\n  fixture: {row.get(field)!r}\n"
                    f"  vocabulary: {expected[field]!r}"
                )
    groups = {row["key"]: row for row in decision_legend()["groups"]}
    for row in legend.get("groups", []):
        expected = groups.get(row["key"])
        if expected is None:
            drift.append(f"group {row['key']!r} is not a group this vocabulary has")
            continue
        for field in ("label", "definition"):
            if row.get(field) != expected[field]:
                drift.append(
                    f"group {row['key']!r} {field}:\n  fixture: {row.get(field)!r}\n"
                    f"  vocabulary: {expected[field]!r}"
                )

    assert not drift, (
        "apps/agentacct/Tests/agentacctTests/Fixtures/dashboard.json carries wording this "
        "vocabulary no longer uses — regenerate or hand-update those strings:\n"
        + "\n".join(drift)
    )


def test_every_scenario_is_distinct_so_the_matrix_cannot_go_stale(tmp_path: Path) -> None:
    """A guard on the fixture set itself: eight scenarios that all reduce to the
    same verdict would make the whole matrix vacuous."""

    headlines = {name: _receipt(name)["verdict"]["headline"] for name in SCENARIO_NAMES}
    assert len(set(headlines.values())) >= 5, headlines

    states = {_receipt(name)["dimensions"]["cost"]["state"] for name in SCENARIO_NAMES}
    assert {"complete", "partial", "unpriced", "no_usage"} <= states, states

    # The scenarios the brief names are each actually realised by the reducers.
    assert _receipt("errored_check")["axes"]["evidence_strength"]["checks_not_run"] == 2
    assert _receipt("handoff_with_checks")["axes"]["handoff"]["handed_off"] is True
    assert _receipt("blocked")["axes"]["decision_status"]["key"] == "blocked"
    assert _receipt("partial_coverage")["axes"]["evidence_strength"]["by_tier"]["unchecked"] == 1
    assert _receipt("observed")["axes"]["evidence_strength"]["gradeable"] is False
    assert _receipt("fail_then_pass")["axes"]["evidence_strength"]["checks_earlier_failed"] == 1
    assert _receipt("failed_with_exit_zero")["dimensions"]["evidence"]["checks"][0]["note_text"]
    assert _receipt("many_findings")["axes"]["evidence_strength"]["checks_failed"] == 4
