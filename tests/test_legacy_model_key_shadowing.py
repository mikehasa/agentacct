"""Un-migrated legacy ':model:' stores (Phase 1 review findings 4 + 6).

The historical double-count pairing — a stale base-keyed claude-code row
frozen next to ':model:'-suffixed sibling rows for the same session — must not
double-attribute after read-time base normalization. One shared rule in
usage_truth (split_shadowed_legacy_usage_events) excludes the stale base row
on EVERY read surface: work ledger, dashboard usage records, context bridge.
The one-shot import migration later supersedes the pairing permanently and
must yield the same totals the read-time rule already reported.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from agentacct.api import _build_usage_view
from agentacct.cli import app
from agentacct.context_bridge import build_usage_context_bridge
from agentacct.usage_truth import (
    is_shadowed_legacy_usage_import_event,
    legacy_suffixed_claude_code_bases,
    split_shadowed_legacy_usage_events,
)
from agentacct.work_ledger import build_work_ledger


def _claude_row(event_id: str, session_key: str, input_tokens: int, *, model: str = "claude-opus-4-8", lane: str | None = None) -> dict:
    row = {
        "event_id": event_id,
        "created_at": 10,
        "source": "claude-code-local-session-import",
        "event_type": "model_usage",
        "provider": "claude-code",
        "model": model,
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": 0,
        "usage_confidence": "client_reported",
        "cost_confidence": "unknown",
        "metadata": {
            "usage_source": "local_client_session_store",
            "usage_provenance": "agent_sentinel_local_usage_import",
            "client": "claude-code",
            "client_session_id": session_key,
            "cached_input_tokens": 0,
        },
    }
    if lane is not None:
        row["metadata"]["usage_row_lane"] = lane
    return row


def _claude_section(session: str = "S") -> dict:
    return {
        "event_id": "evt_section_claude",
        "created_at": 20,
        "source": "claude-code",
        "event_type": "section_completed",
        "metadata": {
            "sentinel_semantic_kind": "section",
            "client": "claude-code",
            "client_session_id": session,
            "client_context_keys_authored": ["client_session_id"],
            "section_id": "claude-work",
            "section_status": "completed",
            "section_title": "Claude work",
        },
    }


def _pairing_events() -> list[dict]:
    """Verifier repro: stale base 'S'=1000 + suffixed 3000 + 2000; truth 5000."""
    return [
        _claude_row("evt_stale_base", "S", 1000),
        _claude_row("evt_lane_opus", "S:model:claude-opus-4-8", 3000),
        _claude_row("evt_lane_sonnet", "S:model:claude-sonnet-4-9", 2000, model="claude-sonnet-4-9"),
        _claude_section(),
    ]


def test_shared_helper_classifies_only_the_stale_base_row() -> None:
    events = _pairing_events()
    bases = legacy_suffixed_claude_code_bases(events)
    assert bases == {"S"}
    flags = {event["event_id"]: is_shadowed_legacy_usage_import_event(event, bases) for event in events}
    assert flags == {
        "evt_stale_base": True,
        "evt_lane_opus": False,
        "evt_lane_sonnet": False,
        "evt_section_claude": False,
    }
    kept, shadowed = split_shadowed_legacy_usage_events(events)
    assert [event["event_id"] for event in shadowed] == ["evt_stale_base"]
    assert len(kept) == 3


def test_ledger_bridge_and_dashboard_agree_on_true_usage() -> None:
    events = _pairing_events()

    ledger = build_work_ledger(events)
    ledger_attributed = [attr for attr in ledger["attributions"] if attr.get("work_id")]
    assert sum(attr["usage_tokens"] for attr in ledger_attributed) == 5000
    assert {attr["join_confidence"] for attr in ledger_attributed} == {"exact"}
    assert ledger["insights"]["legacy_shadowed_rows"] == 1

    bridge = build_usage_context_bridge(events)
    assert bridge["usage_records"] == 2
    assert bridge["attributed_usage_records"] == 2
    assert sum(int(link["total_tokens_including_cached"]) for link in bridge["links"]) == 5000
    assert {link["client_session_id"] for link in bridge["links"]} == {"S"}

    view = _build_usage_view([], events)
    assert len(view.saved_records) == 2
    assert {record.session_id for record in view.saved_records} == {"S"}
    assert sum(record.input_tokens for record in view.saved_records) == 5000


def test_non_claude_clients_with_model_marker_are_never_shadow_excluded() -> None:
    events = [
        _claude_row("evt_codex_base", "S", 1000),
        _claude_row("evt_codex_marker", "S:model:gpt-5.5", 3000),
    ]
    for event in events:
        event["metadata"]["client"] = "codex"
        event["source"] = "codex-local-session-import"
        event["provider"] = "codex"

    assert legacy_suffixed_claude_code_bases(events) == set()
    kept, shadowed = split_shadowed_legacy_usage_events(events)
    assert shadowed == []
    assert len(kept) == 2
    view = _build_usage_view([], events)
    assert {record.session_id for record in view.saved_records} == {"S", "S:model:gpt-5.5"}


def _make_claude_home(root: Path) -> Path:
    """Claude Code transcript fixture with a mid-session model switch."""
    claude_home = root / "claude-home"
    project = claude_home / "projects" / "-tmp-project"
    project.mkdir(parents=True)
    rows = [
        {
            "type": "assistant",
            "sessionId": "claude-session",
            "cwd": "/work/project",
            "timestamp": "2026-06-20T10:00:00Z",
            "message": {
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 10, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 40, "output_tokens": 5},
            },
        },
        {
            "type": "assistant",
            "sessionId": "claude-session",
            "cwd": "/work/project",
            "timestamp": "2026-06-21T10:00:00Z",
            "message": {
                "model": "claude-haiku-4-5-20251001",
                "usage": {"input_tokens": 4, "cache_creation_input_tokens": 1, "cache_read_input_tokens": 2, "output_tokens": 3},
            },
        },
    ]
    (project / "claude-session.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return claude_home


def test_migration_yields_the_same_totals_the_read_time_rule_reported(tmp_path: Path) -> None:
    """Store the legacy pairing exactly as pre-fix imports produced it (stale
    partial base row + suffixed rows matching the transcripts), read totals
    BEFORE any migration, then run the real `usage import-local` migration and
    assert the attributed totals are unchanged and the exclusion counter drops
    to zero."""
    from agentacct.client_usage import discover_claude_code_usage

    claude_home = _make_claude_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"
    store_dir.mkdir(parents=True)

    candidates = discover_claude_code_usage(claude_home=claude_home, limit_sessions=10)
    legacy_rows = []
    for index, candidate in enumerate(candidates):
        event = candidate.to_sentinel_event()
        lane = event["metadata"].pop("usage_row_lane")
        # Legacy stored shape: suffixed key, no lane tag, trusted provenance.
        event["metadata"].pop("source_namespace_fingerprint", None)
        event["metadata"].pop("parent_source_namespace_fingerprint", None)
        event["metadata"]["client_session_id"] = f"claude-session:model:{lane.removeprefix('model:')}"
        event["metadata"]["usage_provenance"] = "agent_sentinel_local_usage_import"
        event["event_id"] = f"evt_legacy_{index}"
        event["created_at"] = 5 + index
        legacy_rows.append(event)
    assert len(legacy_rows) == 2
    stale_base = _claude_row("evt_stale_base", "claude-session", 7)  # frozen partial totals
    section = _claude_section(session="claude-session")

    events_path = store_dir / "events.jsonl"
    with events_path.open("w", encoding="utf-8") as handle:
        for event in [stale_base, *legacy_rows, section]:
            handle.write(json.dumps(event) + "\n")

    def _attributed_totals(events: list[dict]) -> int:
        ledger = build_work_ledger(events)
        return sum(attr["usage_tokens"] for attr in ledger["attributions"] if attr.get("work_id"))

    before_events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    before_ledger = build_work_ledger(before_events)
    before_total = _attributed_totals(before_events)
    assert before_ledger["insights"]["legacy_shadowed_rows"] == 1
    assert before_total > 0

    result = CliRunner().invoke(
        app,
        [
            "usage",
            "import-local",
            "--client",
            "claude-code",
            "--store-dir",
            str(store_dir),
            "--claude-home",
            str(claude_home),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["migrated_events"] == 2
    assert payload["superseded_legacy_rows"] == 3

    after_events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    after_ledger = build_work_ledger(after_events)
    assert _attributed_totals(after_events) == before_total
    assert after_ledger["insights"]["legacy_shadowed_rows"] == 0
    usage_keys = {
        event["metadata"]["client_session_id"]
        for event in after_events
        if event.get("event_type") == "model_usage"
    }
    assert usage_keys == {"claude-session"}
