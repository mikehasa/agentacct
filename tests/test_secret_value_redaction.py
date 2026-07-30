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

from agentacct.service import (
    RESERVED_VALUE_REDACTION_KEYS,
    SentinelService,
)
from agentacct.usage_truth import (
    is_local_session_observation_event,
    mark_trusted_local_session_observation_event,
)


# Built by concatenation so the fixtures are unmistakably synthetic while
# keeping the exact shape the patterns must still recognize.
FAKE_API_KEY = "sk-" + "fakeonly" + "0" * 32 + "AbCd"
FAKE_OPENROUTER_KEY = "sk-or-v1-" + "fakeonly" + "0" * 40 + "EfGh"
FAKE_BEARER_HEADER = "Bearer " + "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.c2lnbmF0dXJl"
# Credential alphabets that a fixed [A-Za-z0-9._~+/=-] charset cannot hold:
# percent-encoding and a basic-auth style colon pair.
FAKE_BEARER_OPAQUE = "Bearer " + "fake%3Auser:pass%2F" + "0" * 12
FAKE_BEARER_BASE64 = "Bearer " + "ZmFrZS1vbmx5" + "0" * 20 + "Kw=="

# The prose that the shipped discriminator destroyed: hyphenated engineering
# compounds, which are credential-shaped only if `-` counts as a credential
# character.
BEARER_PROSE = (
    "switch to Bearer token-based-authentication soon; "
    "Bearer tokens-are-rotated-nightly by the job, and "
    "we should use a Bearer token here."
)


def _session_observation(*, title: str) -> dict:
    return {
        "source": "codex-local-session-observation-import",
        "event_type": "session_observed",
        "run_id": "client_codex_redaction_marker",
        "metadata": {
            "client": "codex",
            "client_session_id": "redaction-marker-session",
            "client_session_kind": "root",
            "source_namespace_fingerprint": "sha256:" + "b" * 64,
            "project_dir": "/tmp/observed-project",
            "started_at": 100.0,
            "updated_at": 200.0,
            "source_parse_complete": True,
            "client_session_title": title,
            "client_session_title_source": "explicit_client_title_field",
            "client_session_title_sanitized": True,
            "title_redacted": False,
        },
    }


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


def test_a_bearer_credential_outside_the_token_charset_is_cut_whole(tmp_path: Path) -> None:
    """A fixed credential alphabet leaked the whole key on the first `%`.

    "summary" is not a sensitive key NAME, so the value pattern is the only
    defence here: with the charset stopping at `%`, the run after "Bearer "
    was just "abc", fell under the length floor, and nothing matched at all.
    """

    store = tmp_path / "state"
    service = SentinelService(store)
    credential = "abc%2Fdefghijklmnopqrstuvwx"

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "metadata": {"summary": f"curl failed: Authorization: Bearer {credential}"},
        }
    )

    assert recorded["metadata"]["summary"] == "curl failed: Authorization: [REDACTED_SECRET]"
    assert credential not in _stored_text(store)


def test_hyphenated_prose_after_bearer_is_left_alone(tmp_path: Path) -> None:
    """Counting `-` as credential-shaped shredded ordinary engineering prose."""

    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "branch": "feat/bearer-token-based-authentication",
            "metadata": {"summary": BEARER_PROSE},
        }
    )

    assert recorded["metadata"]["summary"] == BEARER_PROSE
    assert recorded["branch"] == "feat/bearer-token-based-authentication"
    assert "[REDACTED" not in _stored_text(store)


def test_every_credential_shaped_bearer_value_is_still_cut(tmp_path: Path) -> None:
    """The other half of the prose rule: real credential shapes still go."""

    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "metadata": {
                "jwt": FAKE_BEARER_HEADER,
                "opaque": FAKE_BEARER_OPAQUE,
                "base64": FAKE_BEARER_BASE64,
                "header_line": f"Authorization: {FAKE_BEARER_HEADER}",
            },
        }
    )

    assert recorded["metadata"]["jwt"] == "[REDACTED_SECRET]"
    assert recorded["metadata"]["opaque"] == "[REDACTED_SECRET]"
    assert recorded["metadata"]["base64"] == "[REDACTED_SECRET]"
    assert recorded["metadata"]["header_line"] == "Authorization: [REDACTED_SECRET]"
    stored = _stored_text(store)
    for secret in (FAKE_BEARER_HEADER, FAKE_BEARER_OPAQUE, FAKE_BEARER_BASE64):
        assert secret not in stored


def test_redacting_an_already_redacted_value_changes_nothing(tmp_path: Path) -> None:
    """The widened run must not be able to eat its own placeholder."""

    store = tmp_path / "state"
    service = SentinelService(store)
    once = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "metadata": {"summary": f"header was {FAKE_BEARER_OPAQUE}"},
        }
    )

    twice = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "metadata": {"summary": once["metadata"]["summary"]},
        }
    )

    assert twice["metadata"]["summary"] == once["metadata"]["summary"]
    assert "value_redaction_applied" not in twice["metadata"]


def test_the_marker_never_names_a_field_the_server_reassigns(tmp_path: Path) -> None:
    """event_id is overwritten, so a marker row naming it points at nothing."""

    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "event_id": "caller-" + FAKE_API_KEY,
            "metadata": {"summary": f"rotated {FAKE_API_KEY} today"},
        }
    )

    assert recorded["event_id"].startswith("evt_")
    assert "[REDACTED_SECRET]" not in recorded["event_id"]
    assert recorded["metadata"]["value_redaction_fields"] == [
        {"field": "metadata.summary", "pattern_class": "api_key"}
    ]
    assert FAKE_API_KEY not in _stored_text(store)


def test_a_reassigned_field_alone_leaves_no_marker_at_all(tmp_path: Path) -> None:
    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "event_id": "caller-" + FAKE_API_KEY,
            "metadata": {"summary": "nothing sensitive here"},
        }
    )

    assert recorded["event_id"].startswith("evt_")
    assert "value_redaction_applied" not in recorded["metadata"]
    assert "value_redaction_fields" not in recorded["metadata"]


def test_a_forged_top_level_redaction_marker_is_stripped(tmp_path: Path) -> None:
    """The stamp lands top-level for off-contract metadata, so a caller can
    plant it there too; that home is cleared like the metadata one."""

    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "value_redaction_applied": True,
            "value_redaction_fields": [
                {"field": "metadata.summary", "pattern_class": "api_key"}
            ],
            "metadata": {"summary": "nothing sensitive here"},
        }
    )

    assert "value_redaction_applied" not in recorded
    assert "value_redaction_fields" not in recorded
    assert recorded["metadata"]["reserved_value_redaction_provenance_stripped"] is True


def test_a_session_observation_says_that_its_title_was_cut(tmp_path: Path) -> None:
    """A trusted observation used to lose the marker to the allowlist rebuild.

    The title was stored correctly redacted and nothing recorded that data had
    been removed, which is the silent-cut failure the marker exists to prevent.
    """

    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        _session_observation(title=f"rotated {FAKE_API_KEY} today"),
        trusted_session_observation_import=True,
    )

    assert recorded["metadata"]["client_session_title"] == "rotated [REDACTED_SECRET] today"
    assert recorded["metadata"]["value_redaction_applied"] is True
    assert recorded["metadata"]["value_redaction_fields"] == [
        {"field": "metadata.client_session_title", "pattern_class": "api_key"}
    ]
    # The marked row must still read back as a trusted observation: the lane
    # rejects any metadata key outside its allowlist.
    assert is_local_session_observation_event(recorded)
    assert FAKE_API_KEY not in _stored_text(store)


def test_a_repeated_observation_import_is_still_one_revision(tmp_path: Path) -> None:
    """The marker is part of the row, so it must be part of a stable digest."""

    store = tmp_path / "state"
    service = SentinelService(store)
    observation = _session_observation(title=f"rotated {FAKE_API_KEY} today")

    first = service.record_event(dict(observation), trusted_session_observation_import=True)
    second = service.record_event(dict(observation), trusted_session_observation_import=True)

    assert first["metadata"]["observation_revision"] == second["metadata"]["observation_revision"]
    assert len(_stored_events(store)) == 1


def test_the_observation_allowlist_keeps_server_authored_marker_keys() -> None:
    """Unit guard for the two lists that have to agree across modules."""

    marked = mark_trusted_local_session_observation_event(
        {
            **_session_observation(title="rotated [REDACTED_SECRET] today"),
            "metadata": {
                **_session_observation(title="rotated [REDACTED_SECRET] today")["metadata"],
                "value_redaction_applied": True,
                "value_redaction_fields": [
                    {"field": "metadata.client_session_title", "pattern_class": "api_key"}
                ],
            },
        }
    )

    for key in RESERVED_VALUE_REDACTION_KEYS:
        assert key in marked["metadata"]
    assert is_local_session_observation_event(marked)
