"""Canonical lane inventory (migration increment I2).

Answers the question the authoritative-write flip must answer before v1 can
leave the hot path: *for every kind of event the v1 ``events.jsonl`` ledger
records, is its payload fully represented in canonical, or must v1 remain its
record?*

The subtlety this module exists to capture: an event can be TOUCHED by a
canonical lane without its payload being modeled. A ``machine_check``, a
``finding_disposition``, a ``note`` and a ``client_context_attached`` all carry
a ``client_session_id``, so the SESSION lane observes their session identity —
but their actual payload (the test/build result, the disposition note, the free
annotation, the join provenance) has NO canonical lane. Dropping their v1 row
under authoritative mode would silently lose that payload even though the
session lane "saw" them.

So retention is decided by ``must_retain_in_v1`` — a FAIL-SAFE predicate that
keeps an event in v1 unless its payload is provably, fully modeled: it lands in
the work-claim or usage lane, or it is one of a small allow-list of events whose
entire payload IS the session observation. Anything else — including a brand new
event type nobody has classified yet — is retained, never dropped.

``V1_ONLY_RETAINED`` is the human register of the KNOWN retained types with the
reason each stays v1's record. Modeling any of these payloads in canonical (an
evidence lane, a disposition lane, …) is a later sub-increment; recording the
explicit retention decision is what closes the I2 precondition. The guard test
(tests/test_canonical_lane_inventory.py) pins both the predicate and the
register against the real canonical classifiers so they cannot silently drift.
"""

from __future__ import annotations

from typing import Any, Mapping

from .legacy_import import _is_usage_event
from .v1_events import (
    client_for,
    event_metadata,
    normalized_work_claim_fields,
    observed_session_fields,
)


# The canonical lanes an event can land in. An event is "touched" by canonical
# iff at least one lane claims it; that is NOT the same as its payload being
# modeled (see the module docstring).
CANONICAL_LANES = ("work_claim", "session", "usage")

# Events whose ENTIRE payload is the session observation itself, so the session
# lane fully models them (no separate claim/usage/evidence content is lost when
# v1 is dropped). Kept deliberately small and explicit; everything not on this
# allow-list and not a work-claim/usage event is retained in v1 by default.
PAYLOAD_COMPLETE_SESSION_EVENT_TYPES = frozenset({"session_observed"})

# NOTE (classification quirk, flagged for review, not a retention decision):
# ``agent_usage_debug_reported`` is claimed by the USAGE lane because
# ``_is_usage_event`` matches any event type containing the substring "usage".
# The MCP contract says that debug snapshot is NOT billing truth, so whether it
# should occupy the usage lane at all (vs. a held/non-additive row, or its own
# lane) is a usage-classifier correctness question tracked separately — NOT
# something this inventory silently retains or drops. Recorded here so the next
# reader sees the same thing the I2 guard test surfaced.


def canonical_lanes_for_event(event: Mapping[str, Any]) -> tuple[str, ...]:
    """Which canonical lanes (if any) claim this v1 ledger event. Mirrors the
    live shadow writer's own classification exactly, so it never diverges from
    what actually gets written."""

    metadata = event_metadata(event)
    lanes: list[str] = []
    if normalized_work_claim_fields(event, metadata) is not None:
        lanes.append("work_claim")
    client = client_for(event, metadata)
    if observed_session_fields(event, metadata, client=client) is not None:
        lanes.append("session")
    if _is_usage_event(event):
        lanes.append("usage")
    return tuple(lanes)


def is_canonical_touched_event(event: Mapping[str, Any]) -> bool:
    """True iff at least one canonical lane processes this event — identical to
    the live writer's ``not_represented_in_canonical_model`` skip decision (it
    skips only events NO lane touches). NOTE: touched != payload fully modeled;
    use ``must_retain_in_v1`` to decide whether v1 may stop being the record."""

    return bool(canonical_lanes_for_event(event))


def payload_fully_modeled(event: Mapping[str, Any]) -> bool:
    """True iff this event's PAYLOAD is fully represented in canonical, so its v1
    row could be dropped without losing anything: its content lands in the
    work-claim or usage lane, or it is a session observation whose entire
    payload is that observation. A session lane touching an event only for its
    incidental ``client_session_id`` does NOT make its payload modeled."""

    lanes = canonical_lanes_for_event(event)
    if "work_claim" in lanes or "usage" in lanes:
        return True
    event_type = str(event.get("event_type") or "").lower()
    return "session" in lanes and event_type in PAYLOAD_COMPLETE_SESSION_EVENT_TYPES


def must_retain_in_v1(event: Mapping[str, Any]) -> bool:
    """The fail-safe retention gate for authoritative mode: retain an event in
    v1 unless its payload is provably, fully modeled in canonical. A new or
    unclassified event type is retained, never dropped."""

    return not payload_fully_modeled(event)


# Every KNOWN event type whose payload is NOT modeled in canonical, with the
# reason it stays v1's record. Enforced by the guard test: each must genuinely
# be retained (``must_retain_in_v1`` True) with no work-claim/usage lane.
V1_ONLY_RETAINED: dict[str, str] = {
    "machine_check": (
        "objective test/build/lint evidence (command, result, exit code). Its "
        "session identity is observed, but the evidence payload has no canonical "
        "lane. A canonical evidence lane is a later sub-increment; retained in v1."
    ),
    "machine_check_observed": (
        "the observed/envelope form of a machine check; same evidence payload, "
        "same deferral as machine_check."
    ),
    "finding_disposition": (
        "review-finding disposition notes with their own fsynced, conflict-guarded "
        "writer and idempotency contract; deliberately skipped by the live shadow "
        "writer. Retained in v1."
    ),
    "client_context_attached": (
        "client/session/turn join-provenance stamped by the server. It is applied "
        "TO modeled events, not modeled as its own canonical payload. Retained in "
        "v1."
    ),
    "note": (
        "free-form budget/outcome/annotation notes with no canonical payload lane. "
        "Retained in v1."
    ),
    "mcp_doctor_test": (
        "a diagnostic self-test marker emitted by `mcp doctor`; not a product "
        "fact. Retained in v1."
    ),
    "workflow_smoke": (
        "a smoke-test marker from the optional real-agent smoke suite; not a "
        "product fact. Retained in v1."
    ),
    "refuse_route_and_retain_quarantine": (
        "a quarantine marker recording that a route was refused and its payload "
        "retained; an operational forensic record, not a modeled fact. Retained "
        "in v1."
    ),
}


def retention_reason(event_type: str) -> str | None:
    """The recorded reason a KNOWN event type is retained in v1, or None if the
    type is not in the register (either fully modeled, or a new type not yet
    classified — which ``must_retain_in_v1`` still retains safely)."""

    return V1_ONLY_RETAINED.get(event_type)


__all__ = [
    "CANONICAL_LANES",
    "PAYLOAD_COMPLETE_SESSION_EVENT_TYPES",
    "V1_ONLY_RETAINED",
    "canonical_lanes_for_event",
    "is_canonical_touched_event",
    "must_retain_in_v1",
    "payload_fully_modeled",
    "retention_reason",
]
