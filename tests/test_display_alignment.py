"""Every display budget must agree with the surface it claims to describe.

The defect this guards against: a field's cap and the space that field is
rendered into were chosen independently. The measured case was the timeline
title, which fell back to a section's `summary` -- handing a 1,200-character
paragraph to a card that renders two lines of 14 pt text in a 200x80 pt box, so
the reader saw an arbitrary slice of a sentence.

These tests check the alignment in three directions:

1. the budgets are internally consistent (no surface claims more room than the
   tightest surface it inherits from);
2. the schema's caps are never SMALLER than the budget a surface needs, because
   that would store a record the UI cannot show in full;
3. real stored values, run through the display path, come out inside budget --
   measured against the installed ledger, not a fixture.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from agentacct.display_budget import (
    CARD_FALLBACK_TITLE_CHARACTERS,
    CARD_TITLE_CHARACTERS,
    INSPECTOR_SUMMARY_CHARACTERS,
    MARKDOWN_CELL_CHARACTERS,
    TERMINAL_LINE_CHARACTERS,
    display_label_from_text,
    truncate_for_display,
)

LEDGER = "~/.local/state/agentacct/state/events.sqlite3"


def over_budget(value: object, *, limit: int) -> bool:
    """Local to the assertions below: `len` plus a comparison, nothing more."""
    return len(str(value or "")) > limit


# --- 1. internal consistency ------------------------------------------------


def test_a_card_fallback_title_uses_the_card_budget() -> None:
    """The fallback IS a card title, so it must not claim the inspector's room."""
    assert CARD_FALLBACK_TITLE_CHARACTERS == CARD_TITLE_CHARACTERS


def test_no_display_budget_exceeds_a_single_terminal_line() -> None:
    """Anything a terminal row shows must fit that row: the TUI renders a check
    headline on one line, so a budget above the line width would simply wrap."""
    for budget in (CARD_TITLE_CHARACTERS, MARKDOWN_CELL_CHARACTERS):
        assert budget <= TERMINAL_LINE_CHARACTERS


def test_the_inspector_is_the_only_multi_paragraph_surface() -> None:
    assert INSPECTOR_SUMMARY_CHARACTERS > max(CARD_TITLE_CHARACTERS, MARKDOWN_CELL_CHARACTERS)


# --- 2. helpers behave -------------------------------------------------------


@pytest.mark.parametrize("limit", [1, 8, 20, 54, 100])
def test_truncation_never_exceeds_the_budget(limit: int) -> None:
    text = "Coordinate one hundred native macOS design reviews across ten rounds in parallel"
    out = truncate_for_display(text, limit=limit)
    assert len(out) <= limit, (limit, out)


def test_truncation_prefers_a_word_boundary() -> None:
    out = truncate_for_display("Coordinate one hundred native macOS reviews", limit=30)
    assert out.endswith("…")
    assert not out[:-1].endswith(" ")
    # A word boundary, not a mid-word cut.
    assert " " in out[:-1]


def test_truncation_does_not_empty_a_single_long_token() -> None:
    """A path or hash has no word boundary; cutting is better than returning nothing."""
    out = truncate_for_display("a" * 200, limit=20)
    assert len(out) == 20
    assert out.startswith("a")


def test_short_text_passes_through_untouched() -> None:
    assert truncate_for_display("Add rate-limit to login", limit=54) == "Add rate-limit to login"


def test_a_fallback_label_is_the_first_sentence_not_a_fragment() -> None:
    prose = "Inspected live Dashboard, Work receipt and Usage. Then reviewed the native app surfaces."
    label = display_label_from_text(prose)
    assert label == "Inspected live Dashboard, Work receipt and Usage."
    assert not label.endswith("…"), "the first sentence fits the budget, so nothing should be cut"


def test_a_fallback_label_honours_the_card_budget() -> None:
    prose = "This is a deliberately long opening sentence that keeps going well past the card budget"
    label = display_label_from_text(prose)
    assert len(label) <= CARD_FALLBACK_TITLE_CHARACTERS


def test_display_label_handles_multiline_prose() -> None:
    """Only the first line is a label; the rest stays in the summary field."""
    label = display_label_from_text("Fixed the redirect.\n\n- covered by two tests\n- shipped")
    assert label == "Fixed the redirect."


# --- 3. the schema must not be tighter than its surfaces --------------------


def test_schema_caps_are_at_least_the_display_budgets() -> None:
    """The schema may be more generous than a surface (the inspector shows more
    than a card), but it must never be TIGHTER than the surface that needs the
    room -- that would refuse a record the UI is built to display."""
    from agentacct.mcp import TOOLS

    section = next(tool for tool in TOOLS if tool["name"] == "agentacct_record_section")
    props = section["inputSchema"]["properties"]
    assert props["section_title"]["maxLength"] >= CARD_TITLE_CHARACTERS
    assert props["summary"]["maxLength"] >= INSPECTOR_SUMMARY_CHARACTERS


def test_schema_descriptions_disclose_the_display_budget() -> None:
    """An author cannot respect a budget they are not told about."""
    from agentacct.mcp import TOOLS

    section = next(tool for tool in TOOLS if tool["name"] == "agentacct_record_section")
    props = section["inputSchema"]["properties"]
    assert str(CARD_TITLE_CHARACTERS) in props["section_title"]["description"]
    assert str(INSPECTOR_SUMMARY_CHARACTERS) in props["summary"]["description"]


# --- 4. the real store, measured through the display path ------------------


def _real_titles_and_summaries() -> tuple[list[str], list[str]]:
    path = os.path.expanduser(LEDGER)
    if not os.path.exists(path):
        pytest.skip("installed ledger not present")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = [row[0] for row in connection.execute("select line from event_lines order by seq")]
    finally:
        connection.close()
    titles: list[str] = []
    summaries: list[str] = []
    for line in rows:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not str(event.get("event_type") or "").startswith("section_"):
            continue
        metadata = event.get("metadata") or {}
        if metadata.get("section_title"):
            titles.append(str(metadata["section_title"]))
        if metadata.get("summary"):
            summaries.append(str(metadata["summary"]))
    return titles, summaries


def test_real_titles_are_worth_showing_in_full() -> None:
    """Measure how much of a real title a card hides.

    This is the alignment question in one number. Measured 2026-09-12: median 40
    characters, p90 52, max 77. A card renders about 54, so most titles show
    whole -- which is why the card budget is the right label budget, and why a
    title longer than it is worth flagging rather than silently clipping.
    """
    titles, _ = _real_titles_and_summaries()
    if not titles:
        pytest.skip("no recorded titles")
    over = [title for title in titles if over_budget(title, limit=CARD_TITLE_CHARACTERS)]
    share = len(over) / len(titles)
    # Not zero: the cap is 160 and a few real titles exceed the card. The assertion
    # pins the STATUS QUO so a regression (a new fallback that stuffs prose into a
    # title) fails here instead of reaching the canvas.
    assert share < 0.15, f"{share:.1%} of real titles exceed the card budget: {over[:3]}"


def test_no_real_summary_is_used_as_a_card_label_whole() -> None:
    """The defect this guards against, and its honest status.

    Measured 2026-09-12: ZERO of the 538 work items in the installed store
    actually took the summary-as-title path, so the fix removed a latent defect
    rather than a live one. It is kept because the path is one missing field away
    from firing, and because the failure is silent: a card would show an
    arbitrary middle slice of a paragraph with nothing saying so.
    """
    _, summaries = _real_titles_and_summaries()
    if not summaries:
        pytest.skip("no recorded summaries")
    long_summaries = [s for s in summaries if over_budget(s, limit=CARD_TITLE_CHARACTERS)]
    assert long_summaries, "expected some long summaries in the real store"
    for summary in long_summaries[:50]:
        label = display_label_from_text(summary)
        assert len(label) <= CARD_FALLBACK_TITLE_CHARACTERS
        assert label != summary, "a long summary must be reduced before it becomes a label"
