"""Numbered nouns for user-facing text.

Every count the app, the terminal UI, or a receipt prints carries a noun that
agrees with it: "1 session", "3 sessions". Callers pass an explicit plural for
irregular nouns.
"""

from __future__ import annotations


def count_noun(count: int, singular: str, plural: str | None = None) -> str:
    """Return ``"<count> <noun>"`` with the noun numbered to match ``count``."""
    noun = singular if count == 1 else (plural if plural is not None else singular + "s")
    return f"{count} {noun}"
