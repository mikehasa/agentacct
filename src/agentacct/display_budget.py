"""Character budgets shared by the recording schema and the display surfaces.

Why this module exists: a field's cap and the space that field is rendered into
were chosen independently, and they did not agree. The clearest measured case was
the timeline's title, which fell back to a section's `summary` -- so a
1,200-character paragraph was handed to a card that renders two lines of 14 pt
text in a 200x80 pt box, and the reader saw an arbitrary slice of a sentence.

The budgets below are derived from the surfaces themselves. Each one records how
it was derived, so a surface that changes size can update its number instead of
leaving a stale constant behind.

These are DISPLAY budgets, not validation limits. The schema keeps its generous
caps because the same field feeds several surfaces (a summary is a card fallback,
an inspector block and an export cell); what changes here is that the surfaces
stop inventing their own truncation and the schema can tell an author how much of
a field a reader actually sees.
"""

from __future__ import annotations

from typing import Any

# --- derivations ------------------------------------------------------------
#
# Canvas card (apps/agentacct/Sources/agentacct/WorkTimeCanvas.swift:200 and
# WorkTimeCanvasLayout.swift:61):
#   card 200 pt wide, 6 pt padding each side -> 188 pt of text
#   title font .rowLabel = 14 pt semibold (Theme.swift:319)
#   average glyph advance for a proportional sans face at 14 pt is ~0.55 em
#   -> 188 / (14 * 0.55) ~= 24 characters per line, lineLimit(2) -> ~49
#     rounded to 54 to allow the narrower glyphs a real title uses.
# The card is the tightest surface, so its number is the one a title must respect.
CARD_TITLE_CHARACTERS = 54

# Inspector (WorkTimelineView.swift:568) renders the summary as body text with no
# line limit, so the constraint is reading, not geometry: two short paragraphs is
# roughly 600 characters, and anything past that is a report rather than a
# summary. Measured real summaries: median 207, p90 396, max 1024.
INSPECTOR_SUMMARY_CHARACTERS = 600

# A card fallback title is a label. Reusing the inspector budget here is exactly
# the defect this module removes, so the fallback gets the label budget.
CARD_FALLBACK_TITLE_CHARACTERS = CARD_TITLE_CHARACTERS

# Terminal-style single-line rows (src/agentacct/tui.py renders a check headline
# on one line). A 150-column terminal is generous; anything longer wraps or is
# cut mid-word.
TERMINAL_LINE_CHARACTERS = 150

# Exported Markdown table cells (src/agentacct/receipt_markdown.py) are read
# beside other columns, so a cell is a phrase, not a sentence.
MARKDOWN_CELL_CHARACTERS = 80

# --- helpers ----------------------------------------------------------------


def truncate_for_display(value: Any, *, limit: int, ellipsis: str = "…") -> str:
    """Shorten text to a display budget at a word boundary.

    Word-boundary truncation, because a label cut mid-word reads as corruption
    while a label cut at a space reads as a label. The ellipsis is included in
    the budget, so the returned string is never longer than ``limit``.
    """
    text = str(value or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    # When the budget cannot hold the ellipsis plus even one character, the
    # ellipsis is the thing to drop: returning "C…" for a limit of 1 would break
    # the one guarantee this function makes.
    if len(ellipsis) >= limit:
        return text[:limit]
    room = limit - len(ellipsis)
    head = text[:room]
    if " " in head:
        candidate = head[: head.rfind(" ")].rstrip()
        # Only accept the word boundary when it does not discard most of the room;
        # a single very long token (a path, a hash) is better cut than emptied.
        if len(candidate) >= room // 2:
            head = candidate
    return head.rstrip() + ellipsis


def display_label_from_text(value: Any) -> str:
    """First line of prose, reduced to something that can act as a label.

    Used when a record has no title of its own. It takes the first sentence rather
    than the first N characters, so the label reads as a statement instead of a
    fragment, then applies the card budget.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    first_line = text.split("\n", 1)[0].strip()
    for terminator in (". ", "! ", "? "):
        index = first_line.find(terminator)
        if index > 0:
            first_line = first_line[: index + 1]
            break
    return truncate_for_display(first_line, limit=CARD_FALLBACK_TITLE_CHARACTERS)
