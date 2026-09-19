"""User-facing counts carry a noun numbered to match them."""

from agentacct.plural import count_noun


def test_singular_and_plural() -> None:
    assert count_noun(1, "session") == "1 session"
    assert count_noun(0, "session") == "0 sessions"
    assert count_noun(3, "session") == "3 sessions"


def test_explicit_plural_form() -> None:
    assert count_noun(1, "entry", "entries") == "1 entry"
    assert count_noun(2, "entry", "entries") == "2 entries"


def test_compound_noun() -> None:
    assert count_noun(1, "active session") == "1 active session"
    assert count_noun(2, "completed step") == "2 completed steps"
