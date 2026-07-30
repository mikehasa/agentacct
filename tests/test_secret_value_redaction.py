"""Value-pattern secret redaction: cut the secret, keep the record.

The original implementation replaced the WHOLE string on any pattern hit and
used unanchored patterns, so ordinary ledger data was destroyed: "task-",
"disk-" and "risk-" all contain "sk-", and the phrase "Bearer token" matched
the Authorization pattern. Worse than the loss, every destroyed section_id
collapsed onto the same literal placeholder, so unrelated jobs merged into one
phantom section. These tests pin both halves: real secrets still go, and the
false positives that caused the incident never touch a value again.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentacct.service import SentinelService


# Built by concatenation so the fixtures are unmistakably synthetic while
# keeping the exact shape the patterns must still recognize.
FAKE_API_KEY = "sk-" + "fakeonly" + "0" * 32 + "AbCd"
FAKE_OPENROUTER_KEY = "sk-or-v1-" + "fakeonly" + "0" * 40 + "EfGh"
FAKE_BEARER_HEADER = "Bearer " + "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.c2lnbmF0dXJl"


def _stored_text(store: Path) -> str:
    return (store / "events.jsonl").read_text(encoding="utf-8")


def _stored_events(store: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in _stored_text(store).splitlines()
        if line.strip()
    ]


def test_real_shaped_secrets_are_still_removed_from_the_stored_event(tmp_path: Path) -> None:
    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "metadata": {
                "openai": FAKE_API_KEY,
                "openrouter": FAKE_OPENROUTER_KEY,
                "header": FAKE_BEARER_HEADER,
            },
        }
    )

    assert recorded["metadata"]["openai"] == "[REDACTED_SECRET]"
    assert recorded["metadata"]["openrouter"] == "[REDACTED_SECRET]"
    assert recorded["metadata"]["header"] == "[REDACTED_SECRET]"
    stored = _stored_text(store)
    for secret in (FAKE_API_KEY, FAKE_OPENROUTER_KEY, FAKE_BEARER_HEADER):
        assert secret not in stored
    # The openrouter shape is a prefix-superset of the plain key shape; it must
    # report as the specific class, not the generic one.
    classes = {row["pattern_class"] for row in recorded["metadata"]["value_redaction_fields"]}
    assert classes == {"api_key", "openrouter_api_key", "bearer_token"}


def test_surrounding_prose_survives_a_redacted_secret(tmp_path: Path) -> None:
    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "metadata": {
                "summary": (
                    f"Rotated the provider key to {FAKE_API_KEY} after the outage. "
                    "The dashboard came back clean."
                )
            },
        }
    )

    assert recorded["metadata"]["summary"] == (
        "Rotated the provider key to [REDACTED_SECRET] after the outage. "
        "The dashboard came back clean."
    )
    assert FAKE_API_KEY not in _stored_text(store)


def test_incident_false_positives_leave_every_value_untouched(tmp_path: Path) -> None:
    store = tmp_path / "state"
    service = SentinelService(store)
    project_dir = "/Users/x/.claude/worktrees/canonical-migration-4-work-task-sessions-4801fa"

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "section_id": "work-task-sessions-4801fa",
            "project_dir": project_dir,
            "branch": "feat/task-sessions-4801fa",
            "metadata": {
                "summary": (
                    "disk-usage climbed while risk-assessment-2026 was open; "
                    "we should use a Bearer token here."
                ),
                "idempotency_key": "task-sessions-4801fa-attempt-1",
            },
        }
    )

    assert recorded["section_id"] == "work-task-sessions-4801fa"
    assert recorded["project_dir"] == project_dir
    assert recorded["branch"] == "feat/task-sessions-4801fa"
    assert recorded["metadata"]["summary"] == (
        "disk-usage climbed while risk-assessment-2026 was open; "
        "we should use a Bearer token here."
    )
    assert recorded["metadata"]["idempotency_key"] == "task-sessions-4801fa-attempt-1"
    assert "[REDACTED" not in _stored_text(store)


def test_two_task_shaped_ids_stay_distinct_in_the_ledger(tmp_path: Path) -> None:
    """The merge bug: both ids used to collapse onto the literal placeholder."""

    store = tmp_path / "state"
    service = SentinelService(store)

    first = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "section_id": "canonical-migration-4-work-task-sessions-4801fa",
            "metadata": {"idempotency_key": "work-task-sessions-4801fa-checkpoint"},
        }
    )
    second = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "section_id": "dashboard-rewrite-2-work-task-findings-9c31bb",
            "metadata": {"idempotency_key": "work-task-findings-9c31bb-checkpoint"},
        }
    )

    assert first["section_id"] != second["section_id"]
    assert first["metadata"]["idempotency_key"] != second["metadata"]["idempotency_key"]
    assert first["event_id"] != second["event_id"]
    stored = _stored_events(store)
    assert len(stored) == 2
    assert {row["section_id"] for row in stored} == {
        "canonical-migration-4-work-task-sessions-4801fa",
        "dashboard-rewrite-2-work-task-findings-9c31bb",
    }


def test_marker_names_the_redacted_fields_and_pattern_classes(tmp_path: Path) -> None:
    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "branch": f"feat/{FAKE_API_KEY}",
            "metadata": {
                "summary": f"header was {FAKE_BEARER_HEADER}",
                "items": [{"note": FAKE_OPENROUTER_KEY}],
            },
        }
    )

    assert recorded["metadata"]["value_redaction_applied"] is True
    assert recorded["metadata"]["value_redaction_fields"] == [
        {"field": "branch", "pattern_class": "api_key"},
        {"field": "metadata.items.0.note", "pattern_class": "openrouter_api_key"},
        {"field": "metadata.summary", "pattern_class": "bearer_token"},
    ]


def test_marker_is_absent_when_nothing_was_redacted(tmp_path: Path) -> None:
    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "project_dir": "/repos/brisk-task-runner",
            "metadata": {"summary": "disk-usage report for risk-assessment-2026"},
        }
    )

    assert "value_redaction_applied" not in recorded["metadata"]
    assert "value_redaction_fields" not in recorded["metadata"]


def test_a_forged_redaction_marker_is_stripped_from_caller_metadata(tmp_path: Path) -> None:
    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "metadata": {
                "summary": "nothing sensitive here",
                "value_redaction_applied": True,
                "value_redaction_fields": [{"field": "metadata.summary", "pattern_class": "api_key"}],
            },
        }
    )

    assert "value_redaction_applied" not in recorded["metadata"]
    assert "value_redaction_fields" not in recorded["metadata"]
    assert recorded["metadata"]["reserved_value_redaction_provenance_stripped"] is True


def test_sensitive_key_names_still_lose_the_whole_value(tmp_path: Path) -> None:
    """Key-name decisions are unchanged: only the value-pattern path narrowed."""

    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "metadata": {
                "api_key": "some prose and " + FAKE_API_KEY + " and more prose",
                "safe": "kept",
            },
        }
    )

    assert recorded["metadata"]["api_key"] == "[REDACTED]"
    assert recorded["metadata"]["safe"] == "kept"
    # Whole-value key redaction is self-evident from the key name, so it does
    # not claim a value-pattern redaction.
    assert "value_redaction_applied" not in recorded["metadata"]
    assert FAKE_API_KEY not in _stored_text(store)
