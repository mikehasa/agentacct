"""Text hygiene: the copy an agent's words meet, and the words agents should write.

Companion to ``design-plans/data-quality/TEXT.md``. These are source-level guards
rather than runtime validators, because copy drift is a source problem: the
cheapest reliable enforcement is a test that fails when a new apology string, a
new identity substitute or a new wall of explanation is added.

What the measurements behind them say (installed ledger: 816 summaries, 1,166
titles):

* summaries are 27 words / 2 sentences at the median, with **0%** bullets,
  0% line breaks and 0% markdown -- plain prose, no structure;
* at least 20 distinct phrasings exist for "this field is missing";
* ``"unknown"`` is used both as a recorded data value and as a display fallback
  in 101 places, so a reader cannot tell a recorded unknown from a gap.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from agentacct.mcp import SentinelMCPServer, _collapse_narrative_text

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PRODUCT_SURFACES = [
    "src/agentacct/mcp.py",
    "src/agentacct/receipt.py",
    "src/agentacct/task_intelligence.py",
    "src/agentacct/task_timeline.py",
    "src/agentacct/tui.py",
    "src/agentacct/install_guide.py",
]

# Copy that names a *value* the reader is looking for, by substituting a generic
# identity. Each entry is a conscious decision; the test exists so a new one
# cannot be added by accident. Prefer omitting the field over narrating it.
# Sites where display code uses the literal "unknown". Every one of these stands
# for a *recorded* value (a check result, a session grouping key, an ingestion
# state), not for "we could not read this" -- which is exactly why the inventory
# is pinned: a new site must be a conscious choice between the two meanings.
ALLOWED_UNKNOWN_FALLBACKS = [
    "src/agentacct/receipt.py",
    "src/agentacct/receipt.py",
    "src/agentacct/task_intelligence.py",
    "src/agentacct/task_timeline.py",
    "src/agentacct/tui.py",
    "src/agentacct/tui.py",
]

ALLOWED_IDENTITY_FALLBACKS = {
    'or "recorded check"': 2,      # receipt.py, tui.py -- a check with no name
    'or "Recorded check"': 2,      # task_intelligence.py, task_timeline.py -- same, other casing
    'or "Recorded work"': 1,       # task_timeline.py -- a section with no title AND no summary
}

# One-line budget for copy that renders inside a card, a table cell or a status
# row. Longer explanation belongs behind ContextHelp / a disclosure.
MAX_ONE_LINE_COPY = 140


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


# --- the structure agents are asked to write must survive normalization -------


def test_narrative_normalization_preserves_bullets_and_paragraphs() -> None:
    """The renderer cannot show structure the normalizer destroyed."""
    structured = "Ran the focused suite after the change.\n\n- 3 passed\n- 0 failed\n\nRemaining: ship it."
    assert _collapse_narrative_text(structured) == structured


def test_narrative_normalization_still_removes_control_characters() -> None:
    dirty = "Done.\x00\x07\n- kept\tthis"
    cleaned = _collapse_narrative_text(dirty)
    assert "\x00" not in cleaned and "\x07" not in cleaned
    assert cleaned == "Done.\n- kept this"


def test_narrative_normalization_collapses_empty_line_runs() -> None:
    """A run of blank lines is vertical layout, not signal: it renders exactly
    like one blank line and only makes a card taller, so one is canonical."""
    assert _collapse_narrative_text("One.\n\n\n\nTwo.") == "One.\n\nTwo."


# --- a refusal must not hand the agent a list it cannot copy -----------------


def _refusal(server: SentinelMCPServer, arguments: dict) -> str:
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "agentacct_record_section", "arguments": arguments},
        }
    )
    assert "error" in response, response
    return str(response["error"]["message"])


def test_terminal_refusal_shows_a_structured_example(tmp_path) -> None:
    """The summary the contract asks for is an outcome sentence plus short
    lines, so the example shown to the agent must have that shape -- not a
    one-line placeholder that teaches the wrong format."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _refusal(
        server,
        {"source": "codex", "section_id": "add-rate-limit", "section_status": "completed", "section_title": "Add rate limit"},
    )
    assert "summary" in message
    # The example must be in the summary field, so it is valid Python for the agent.
    example = message.split("for example:", 1)[1]
    assert "summary=" in example
    assert "\n" not in example, "a refusal example must stay on one copyable line"


def test_every_terminal_refusal_names_the_field_and_the_fix(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    for status, field in (("completed", "summary"), ("handed_off", "summary"), ("blocked", "blocker")):
        message = _refusal(
            server,
            {"source": "codex", "section_id": "s", "section_status": status, "section_title": "Some work"},
        )
        assert field in message, (status, message)
        assert "agentacct_record_section(" in message, (status, message)


# --- the source-level guards -------------------------------------------------


def test_no_new_identity_substitute_copy() -> None:
    """Display code must not invent a name for a value it does not have."""
    found: dict[str, int] = {}
    for path in PRODUCT_SURFACES:
        for match in re.finditer(r'(?:or|\?\?)\s*"(Recorded [a-z]+|recorded [a-z]+|Unnamed [a-z]+|Untitled [^"]*)"', _read(path)):
            found[match.group(0).strip()] = found.get(match.group(0).strip(), 0) + 1
    assert found == ALLOWED_IDENTITY_FALLBACKS, (
        "identity-substitute copy changed. Add the field as absent instead, or "
        f"update ALLOWED_IDENTITY_FALLBACKS with a reason. Found: {found}"
    )


def test_one_line_copy_stays_within_budget() -> None:
    """User-visible copy that must fit on one line is capped, so a card never
    shows an ellipsized explanation where data should be.

    The measured distribution: outside the agent-facing prompt text (whose long
    lines, up to 909 characters, are read once by an agent with no width limit)
    the product surfaces contain exactly four literals over 140 characters, all
    of them long-form outcome explanations in task_intelligence.py. This test
    pins that ceiling so the count cannot grow silently -- and it is the list to
    move behind ContextHelp when those surfaces are next touched.
    """
    # Four long-form outcome explanations are known and pinned below; everything
    # else must fit the budget.
    known_long_form = 4
    offenders: list[str] = []
    for path in PRODUCT_SURFACES:
        if path.endswith("install_guide.py"):
            continue  # agent-facing prompt copy, not rendered in the UI
        for match in re.finditer(r'"([^"\n]+)"', _read(path)):
            text = match.group(1)
            if len(text) <= MAX_ONE_LINE_COPY:
                continue
            # An error message or a tool description is read in a log, a tool
            # list or a refusal -- not inside a card.
            if "must be" in text or "requires" in text or "Record " in text or "needs at least" in text:
                continue
            if "Inherited client context" in text or "Join keys were inherited" in text:
                continue
            offenders.append(f"{path}: {len(text)} chars: {text[:70]}...")
    assert len(offenders) <= known_long_form, (
        f"{len(offenders)} long copy literals (budget {known_long_form}):\n" + "\n".join(offenders)
    )


def test_recorded_unknown_and_display_unknown_stay_inventoried() -> None:
    """`unknown` is a real recorded value, so display code using the same literal
    as a stand-in for "unreadable" is genuinely ambiguous. The six sites that do
    were each checked and all six pass a recorded value through; this test pins
    that inventory so a seventh is a decision, not an accident."""
    found = []
    for path in PRODUCT_SURFACES:
        for _ in re.finditer(r'(?:or|\?\?)\s*"unknown"', _read(path)):
            found.append(path)
    assert found == ALLOWED_UNKNOWN_FALLBACKS, (
        "the 'unknown' fallback inventory changed: " + repr(found)
    )


# --- unnamed Tasks must be distinguishable in the Task list -----------------


def test_unnamed_task_titles_are_distinct_per_session() -> None:
    """Measured in the installed store: four observation-only OpenClaw Tasks
    rendered the identical label "Untitled OpenClaw chat". A reader cannot act
    on four identical rows, and the label named neither the session nor when it
    happened, so it carried no information at all."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "src"))
    from agentacct.api import _task_title

    def task(session: str, started: float) -> dict:
        return {
            "primary_root": {"client": "openclaw", "client_session_id": session},
            "sessions": [
                {"client": "openclaw", "client_session_id": session, "started_at": started, "project": None}
            ],
            "last_activity_at": started,
        }

    day = 86_400.0
    base = 1_789_255_328.0
    titles = [_task_title(task(f"session-{index}", base - index * day)) for index in range(3)]
    assert len(set(titles)) == 3, titles
    assert all("Untitled" not in title for title in titles), titles

    # A named work item still wins: the fallback must never override real content.
    named = {
        "primary_root": {"client": "codex", "client_session_id": "s"},
        "sessions": [{"client": "codex", "client_session_id": "s", "started_at": base}],
        "work_items": [{"title": "Add rate-limit to login", "section_id": "add-rate-limit"}],
    }
    assert _task_title(named) == "Add rate-limit to login"


def test_unnamed_task_without_any_timestamp_still_says_something() -> None:
    import sys

    sys.path.insert(0, str(REPO_ROOT / "src"))
    from agentacct.api import _task_title

    title = _task_title({"primary_root": {"client": "openclaw"}, "sessions": [], "last_activity_at": None})
    assert title == "OpenClaw session (unnamed)"
