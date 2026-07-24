from __future__ import annotations

from copy import deepcopy

import pytest

from agent_chronicle.refreshable_usage import (
    CANONICAL_CUMULATIVE_USAGE_SEMANTICS,
    CUMULATIVE_USAGE_SEMANTICS,
    UNRESOLVED_SOURCE_NAMESPACE,
    is_trusted_refreshable_local_usage,
    normalized_refreshable_usage_semantics,
    refreshable_usage_revision_id,
    refreshable_usage_slot_digest,
    refreshable_usage_slot_identity,
    refreshable_usage_source_order,
    refreshable_usage_truth_digest,
    refreshable_usage_truth_material,
    stable_refreshable_usage_revision_id,
)
from agent_chronicle.usage_truth import (
    CODEX_LINEAGE_DELTA_SEMANTICS,
    LOCAL_USAGE_SOURCE,
    mark_trusted_local_usage_import_event,
)


NAMESPACE_A = "sha256:" + "a" * 64
NAMESPACE_B = "sha256:" + "b" * 64


def _usage_event(
    *,
    client: str = "codex",
    session_id: str = "session-1",
    model: str = "gpt-5.6",
    semantics: str = "codex_rollout_token_count_events",
    namespace: str | None = NAMESPACE_A,
    representation: str | None = None,
) -> dict:
    metadata = {
        "usage_update_semantics": semantics,
        "client": client,
        "client_session_id": session_id,
        "source_namespace_fingerprint": namespace,
        "source_revision_at": 1_700_000_000_000_000,
        "source_updated_at": 1_700_000_000,
        "updated_at": 1_700_000_000,
        "cached_input_tokens": 30,
        "cache_creation_input_tokens": 7,
        "cache_read_input_tokens": 23,
        "reasoning_output_tokens": 11,
        "total_tokens": 171,
        "input_tokens_reported": True,
        "output_tokens_reported": True,
        "cache_creation_tokens_reported": True,
        "cache_read_tokens_reported": True,
        "reasoning_output_tokens_reported": True,
        "total_tokens_reported": True,
        "usage_additive": True,
        "usage_normalization_state": "source_cumulative_snapshot",
        "precedence_role": "authoritative",
        "client_reported_cost_usd": None,
        "client_cost_source": None,
        "evidenced_event_ids": ["evt_b", "evt_a"],
        "evidenced_event_id_total": 2,
        "evidenced_outputs_skipped": 0,
    }
    if representation is not None:
        metadata["usage_representation"] = representation
    return mark_trusted_local_usage_import_event({
        "event_id": "evt_random_one",
        "created_at": 1_700_000_100,
        "source": f"{client}-local-session-import",
        "event_type": "model_usage",
        "provider": "openai",
        "model": model,
        "estimated_input_tokens": 100,
        "estimated_output_tokens": 60,
        "estimated_cost_usd": None,
        "usage_confidence": "client_reported",
        "cost_confidence": "unknown",
        "cost_basis": "client_session",
        "metadata": metadata,
    })


@pytest.mark.parametrize(
    "semantics",
    [
        CANONICAL_CUMULATIVE_USAGE_SEMANTICS,
        "codex_rollout_token_count_events",
        "codex_sqlite_tokens_used_fallback",
        CODEX_LINEAGE_DELTA_SEMANTICS,
        "claude_assistant_message_usage_rows",
        "opencode_step_finish_events",
        "hermes_state_db_session_rows",
        "openclaw_assistant_usage_rows",
    ],
)
def test_gate_normalizes_every_supported_cumulative_semantic(semantics: str) -> None:
    event = _usage_event(semantics=semantics)

    assert semantics in CUMULATIVE_USAGE_SEMANTICS
    assert is_trusted_refreshable_local_usage(event) is True
    assert (
        normalized_refreshable_usage_semantics(event)
        == CANONICAL_CUMULATIVE_USAGE_SEMANTICS
    )
    assert refreshable_usage_slot_identity(event).update_semantics == "cumulative_snapshot"


@pytest.mark.parametrize("semantics", ["atomic_increment", "unknown", ""])
def test_gate_rejects_atomic_and_unknown_semantics(semantics: str) -> None:
    event = _usage_event(semantics=semantics)

    assert is_trusted_refreshable_local_usage(event) is False
    assert normalized_refreshable_usage_semantics(event) is None
    assert refreshable_usage_slot_identity(event) is None
    assert refreshable_usage_truth_digest(event) is None
    assert refreshable_usage_revision_id(event) is None


def test_gate_requires_exact_event_type_and_reserved_local_source() -> None:
    wrong_type = _usage_event()
    wrong_type["event_type"] = "task_started"
    wrong_source = _usage_event()
    wrong_source["metadata"]["usage_source"] = "untrusted"
    forged_source_only = _usage_event()
    forged_source_only["metadata"].pop("usage_provenance")
    forged_source_only["metadata"]["usage_source"] = LOCAL_USAGE_SOURCE

    assert is_trusted_refreshable_local_usage(wrong_type) is False
    assert is_trusted_refreshable_local_usage(wrong_source) is False
    assert is_trusted_refreshable_local_usage(forged_source_only) is False


def test_slot_identity_separates_namespace_client_session_and_representation() -> None:
    base = _usage_event(representation="codex-rollout-v1")
    base_slot = refreshable_usage_slot_identity(base)
    other_namespace = refreshable_usage_slot_identity(
        _usage_event(namespace=NAMESPACE_B, representation="codex-rollout-v1")
    )
    other_client = refreshable_usage_slot_identity(
        _usage_event(client="hermes", representation="codex-rollout-v1")
    )
    other_session = refreshable_usage_slot_identity(
        _usage_event(session_id="session-2", representation="codex-rollout-v1")
    )
    other_representation = refreshable_usage_slot_identity(
        _usage_event(representation="codex-sqlite-fallback-v1")
    )

    assert base_slot is not None
    assert len(
        {
            refreshable_usage_slot_digest(slot)
            for slot in (
                base_slot,
                other_namespace,
                other_client,
                other_session,
                other_representation,
            )
            if slot is not None
        }
    ) == 5


def test_explicit_namespace_and_unresolved_namespace_are_distinct() -> None:
    unresolved = refreshable_usage_slot_identity(_usage_event(namespace=None))
    explicit = refreshable_usage_slot_identity(_usage_event(namespace=NAMESPACE_A))

    assert unresolved is not None
    assert explicit is not None
    assert unresolved.namespace_kind == "unresolved"
    assert explicit.namespace_kind == "explicit"
    assert unresolved != explicit
    assert refreshable_usage_slot_digest(unresolved) != refreshable_usage_slot_digest(
        explicit
    )


@pytest.mark.parametrize(
    "namespace",
    [
        UNRESOLVED_SOURCE_NAMESPACE,
        "/Users/example/.codex",
        "sha256:home-a",
        "sha256:" + "A" * 64,
        " " + NAMESPACE_A,
    ],
)
def test_nonempty_invalid_source_namespace_is_fail_visible(namespace: str) -> None:
    with pytest.raises(ValueError, match="source_namespace_fingerprint"):
        refreshable_usage_slot_identity(_usage_event(namespace=namespace))


def test_claude_model_lane_splits_but_non_claude_model_drift_does_not() -> None:
    claude_opus = _usage_event(
        client="claude-code",
        model="claude-opus-4-8",
        semantics="claude_assistant_message_usage_rows",
    )
    claude_haiku = deepcopy(claude_opus)
    claude_haiku["model"] = "claude-haiku-4-5"
    codex_first = _usage_event(model="gpt-5.5")
    codex_second = deepcopy(codex_first)
    codex_second["model"] = "gpt-5.6"

    opus_slot = refreshable_usage_slot_identity(claude_opus)
    haiku_slot = refreshable_usage_slot_identity(claude_haiku)
    assert opus_slot is not None and haiku_slot is not None
    assert opus_slot.client_session_id == haiku_slot.client_session_id
    assert opus_slot.lane == "model:claude-opus-4-8"
    assert haiku_slot.lane == "model:claude-haiku-4-5"
    assert opus_slot.model_discriminator is not None
    assert haiku_slot.model_discriminator is not None
    assert len(opus_slot.model_discriminator) == len("sha256:") + 64
    assert opus_slot.model_discriminator != haiku_slot.model_discriminator
    assert opus_slot != haiku_slot

    codex_first_slot = refreshable_usage_slot_identity(codex_first)
    codex_second_slot = refreshable_usage_slot_identity(codex_second)
    assert codex_first_slot == codex_second_slot
    assert codex_first_slot is not None
    assert codex_first_slot.model_discriminator is None
    assert refreshable_usage_truth_digest(codex_first) != refreshable_usage_truth_digest(
        codex_second
    )


@pytest.mark.parametrize("explicit_lane", [False, True])
@pytest.mark.parametrize(
    ("first_model", "second_model", "expected_lane"),
    [
        ("a.b", "a_b", "model:a_b"),
        ("x" * 80 + "-tail-a", "x" * 80 + "-tail-b", "model:" + "x" * 80),
    ],
)
def test_claude_exact_model_discriminator_prevents_sanitized_or_truncated_collision(
    explicit_lane: bool,
    first_model: str,
    second_model: str,
    expected_lane: str,
) -> None:
    first = _usage_event(
        client="claude-code",
        model=first_model,
        semantics="claude_assistant_message_usage_rows",
    )
    second = deepcopy(first)
    second["model"] = second_model
    if explicit_lane:
        first["metadata"]["usage_row_lane"] = expected_lane
        second["metadata"]["usage_row_lane"] = expected_lane

    first_slot = refreshable_usage_slot_identity(first)
    second_slot = refreshable_usage_slot_identity(second)

    assert first_slot is not None and second_slot is not None
    assert first_slot.lane == second_slot.lane == expected_lane
    assert first_slot.model_discriminator != second_slot.model_discriminator
    assert refreshable_usage_slot_digest(first_slot) != refreshable_usage_slot_digest(
        second_slot
    )


def test_claude_explicit_lane_must_match_exact_model_and_unknown_is_stable() -> None:
    mismatched = _usage_event(
        client="claude-code",
        model="a.b",
        semantics="claude_assistant_message_usage_rows",
    )
    mismatched["metadata"]["usage_row_lane"] = "model:other"
    with pytest.raises(ValueError, match="usage_row_lane"):
        refreshable_usage_slot_identity(mismatched)

    missing = _usage_event(
        client="claude-code",
        model="",
        semantics="claude_assistant_message_usage_rows",
    )
    null_model = deepcopy(missing)
    null_model["model"] = None
    assert refreshable_usage_slot_identity(missing) == refreshable_usage_slot_identity(
        null_model
    )


def test_legacy_claude_session_suffix_normalizes_to_base_and_lane() -> None:
    event = _usage_event(
        client="claude-code",
        session_id="session-1:model:claude-opus-4-8",
        model="ignored",
        semantics="claude_assistant_message_usage_rows",
    )

    slot = refreshable_usage_slot_identity(event)

    assert slot is not None
    assert slot.client_session_id == "session-1"
    assert slot.lane == "model:claude-opus-4-8"


def test_source_order_uses_only_trusted_metadata_watermarks() -> None:
    event = _usage_event()
    assert refreshable_usage_source_order(event) == 1_700_000_000_000_000

    event["metadata"]["source_revision_at"] = None
    event["metadata"]["source_updated_at"] = 1_700_000_001
    assert refreshable_usage_source_order(event) == 1_700_000_001_000_000

    event["metadata"]["source_updated_at"] = None
    event["metadata"]["updated_at"] = 1_700_000_002.5
    assert refreshable_usage_source_order(event) == 1_700_000_002_500_000

    event["metadata"]["updated_at"] = None
    event["created_at"] = 9_999_999_999
    assert refreshable_usage_source_order(event) is None


def test_source_order_normalizes_seconds_milliseconds_and_microseconds() -> None:
    seconds = _usage_event()
    seconds["metadata"]["source_revision_at"] = 100
    fractional_seconds = _usage_event()
    fractional_seconds["metadata"]["source_revision_at"] = 99.9
    milliseconds = _usage_event()
    milliseconds["metadata"]["source_revision_at"] = 1_700_000_000_000
    microseconds = _usage_event()
    microseconds["metadata"]["source_revision_at"] = 1_700_000_000_000_000

    assert refreshable_usage_source_order(seconds) == 100_000_000
    assert refreshable_usage_source_order(fractional_seconds) == 99_900_000
    fractional = refreshable_usage_source_order(fractional_seconds)
    whole = refreshable_usage_source_order(seconds)
    assert fractional is not None and whole is not None and fractional < whole
    assert refreshable_usage_source_order(milliseconds) == 1_700_000_000_000_000
    assert refreshable_usage_source_order(microseconds) == 1_700_000_000_000_000


def test_duplicate_refresh_changes_order_but_not_truth_or_revision() -> None:
    original = _usage_event()
    refreshed = deepcopy(original)
    refreshed["event_id"] = "evt_random_two"
    refreshed["created_at"] = 1_800_000_000
    refreshed["estimated_cost_usd"] = 42.5
    refreshed["cost_confidence"] = "estimated_from_tokens"
    refreshed["cost_basis"] = "pricing_table"
    refreshed["metadata"].update(
        {
            "source_revision_at": 1_800_000_000_000_000,
            "source_updated_at": 1_800_000_000,
            "updated_at": 1_800_000_000,
            "pricing_source": "catalog-b",
            "pricing_source_model": "billing-model-b",
            "pricing_source_provider": "provider-b",
            "pricing_warning": "derived price changed",
            "poll_started_at": 1_800_000_001,
        }
    )

    assert refreshable_usage_source_order(original) != refreshable_usage_source_order(
        refreshed
    )
    assert refreshable_usage_truth_digest(original) == refreshable_usage_truth_digest(
        refreshed
    )
    assert refreshable_usage_revision_id(original) == refreshable_usage_revision_id(
        refreshed
    )


def test_real_usage_and_token_presence_changes_create_new_revisions() -> None:
    original = _usage_event()
    grown = deepcopy(original)
    grown["estimated_input_tokens"] = 101
    unreported = deepcopy(original)
    unreported["metadata"]["input_tokens_reported"] = False

    original_digest = refreshable_usage_truth_digest(original)
    assert original_digest != refreshable_usage_truth_digest(grown)
    assert original_digest != refreshable_usage_truth_digest(unreported)
    assert refreshable_usage_revision_id(original) != refreshable_usage_revision_id(
        grown
    )


def test_derived_price_is_excluded_but_client_reported_cost_is_truth() -> None:
    original = _usage_event()
    derived = deepcopy(original)
    derived["estimated_cost_usd"] = 1.25
    derived["cost_confidence"] = "estimated_from_tokens"
    derived["cost_basis"] = "pricing_table"
    derived["metadata"].update(
        {
            "pricing_source": "catalog-v2",
            "pricing_source_model": "gpt-billing-alias",
            "pricing_source_provider": "openai",
            "pricing_warning": "not an invoice",
        }
    )
    client_cost = deepcopy(original)
    client_cost["metadata"]["client_reported_cost_usd"] = 1.25
    client_cost["metadata"]["client_cost_source"] = "client-log"
    client_cost["estimated_cost_usd"] = 1.25
    client_cost["cost_confidence"] = "client_reported"

    assert refreshable_usage_truth_digest(original) == refreshable_usage_truth_digest(
        derived
    )
    assert refreshable_usage_truth_digest(original) != refreshable_usage_truth_digest(
        client_cost
    )
    material = refreshable_usage_truth_material(client_cost)
    assert material is not None
    assert material["client_reported_cost"] == {
        "usd": "1.25",
        "confidence": "client_reported",
        "source": "client-log",
    }


@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_reported_token_requires_a_nonnegative_non_bool_integer(value: object) -> None:
    event = _usage_event()
    event["estimated_input_tokens"] = value
    event["metadata"]["input_tokens_reported"] = True

    with pytest.raises(ValueError, match="reported input_tokens"):
        refreshable_usage_truth_material(event)


def test_unreported_token_value_is_not_promoted_to_truth() -> None:
    event = _usage_event()
    event["estimated_input_tokens"] = -1
    event["metadata"]["input_tokens_reported"] = False

    material = refreshable_usage_truth_material(event)

    assert material is not None
    assert material["tokens"]["input_tokens"] == {
        "reported": False,
        "value": None,
    }


def test_compat_cached_zero_is_unreported_when_only_split_flag_is_false() -> None:
    event = _usage_event()
    metadata = event["metadata"]
    metadata["cached_input_tokens"] = 0
    metadata.pop("cache_creation_tokens_reported")
    metadata["cache_read_tokens_reported"] = False

    material = refreshable_usage_truth_material(event)

    assert material is not None
    assert material["tokens"]["cached_input_tokens"] == {
        "reported": False,
        "value": None,
    }


@pytest.mark.parametrize("value", [-0.01, float("inf"), float("nan"), True, "invalid"])
def test_client_reported_cost_requires_a_finite_nonnegative_number(value: object) -> None:
    event = _usage_event()
    event["metadata"]["client_reported_cost_usd"] = value
    event["cost_confidence"] = "client_reported"

    with pytest.raises(ValueError, match="client_reported_cost_usd"):
        refreshable_usage_truth_material(event)


@pytest.mark.parametrize(
    ("location", "field", "value", "message"),
    [
        ("metadata", "input_tokens_reported", "true", "must be a bool"),
        ("event", "usage_confidence", "estimated", "usage_confidence"),
        ("metadata", "precedence_role", "guessed", "precedence_role"),
        ("metadata", "precedence_role", 1, "precedence_role"),
    ],
)
def test_invalid_truth_control_fields_are_fail_visible(
    location: str,
    field: str,
    value: object,
    message: str,
) -> None:
    event = _usage_event()
    container = event if location == "event" else event["metadata"]
    container[field] = value

    with pytest.raises(ValueError, match=message):
        refreshable_usage_truth_material(event)


def test_client_reported_cost_requires_client_reported_confidence() -> None:
    event = _usage_event()
    event["metadata"]["client_reported_cost_usd"] = 0.25
    event["cost_confidence"] = "estimated_from_tokens"

    with pytest.raises(ValueError, match="cost_confidence=client_reported"):
        refreshable_usage_truth_material(event)


def test_evidence_links_are_order_insensitive_but_not_discarded() -> None:
    original = _usage_event()
    reordered = deepcopy(original)
    reordered["metadata"]["evidenced_event_ids"] = ["evt_a", "evt_b", "evt_a"]
    extended = deepcopy(original)
    extended["metadata"]["evidenced_event_ids"].append("evt_c")
    extended["metadata"]["evidenced_event_id_total"] = 3
    skipped = deepcopy(original)
    skipped["metadata"]["evidenced_outputs_skipped"] = 1

    assert refreshable_usage_truth_digest(original) == refreshable_usage_truth_digest(
        reordered
    )
    assert refreshable_usage_truth_digest(original) != refreshable_usage_truth_digest(
        extended
    )
    assert refreshable_usage_truth_digest(original) != refreshable_usage_truth_digest(
        skipped
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("usage_additive", False),
        ("usage_normalization_state", "held_for_review"),
        ("usage_held_reason", "source_parse_incomplete"),
        ("precedence_role", "fallback"),
    ],
)
def test_additivity_held_and_precedence_are_truth(field: str, value: object) -> None:
    original = _usage_event()
    changed = deepcopy(original)
    changed["metadata"][field] = value

    assert refreshable_usage_truth_digest(original) != refreshable_usage_truth_digest(
        changed
    )


def test_revision_helper_is_stable_and_binds_slot_plus_truth() -> None:
    event = _usage_event()
    slot = refreshable_usage_slot_identity(event)
    truth_digest = refreshable_usage_truth_digest(event)

    assert slot is not None
    assert truth_digest is not None
    revision = stable_refreshable_usage_revision_id(slot, truth_digest)
    assert revision.startswith("rurev_")
    assert len(revision) == len("rurev_") + 64
    assert revision == refreshable_usage_revision_id(event)

    with pytest.raises(ValueError, match="sha256"):
        stable_refreshable_usage_revision_id(slot, "not-a-digest")
    with pytest.raises(ValueError, match="sha256"):
        stable_refreshable_usage_revision_id(slot, "sha256:" + "z" * 64)
