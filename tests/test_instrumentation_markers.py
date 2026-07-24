"""Instrumentation markers (pre/post honesty).

Sessions that ran before Chronicle instrumentation was installed can never
have MCP work context; CLI-authored marker events let the ledger say so
instead of rendering pre-install history as product failure. These tests pin:
the CLI writers (mark-instrumented, setup instructions, hooks install), the
service trust gate (forged provenance strips, only CLI markers classify),
per-client boundary classification with root inheritance, and the
post-install context-rate KPI (absent without a marker, never a fake 100%).

All stores are throwaway tmp_path stores (suite conftest guards the real
dogfood ledger).
"""

from __future__ import annotations

import html as html_module
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agent_chronicle.api import _session_join_chip_html, create_local_api_app
from agent_chronicle.cli import app
from agent_chronicle.service import SentinelService
from agent_chronicle.usage_truth import (
    CLI_INSTRUMENTATION_PROVENANCE,
    INSTRUMENTATION_MARKER_EVENT_TYPE,
    is_instrumentation_marker_event,
)
from agent_chronicle.work_ledger import build_work_ledger

runner = CliRunner()


def _store_events(store_dir: Path) -> list[dict]:
    events_path = store_dir / "events.jsonl"
    if not events_path.is_file():
        return []
    return [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _store_markers(store_dir: Path) -> list[dict]:
    return [event for event in _store_events(store_dir) if event.get("event_type") == INSTRUMENTATION_MARKER_EVENT_TYPE]


# --- pure-ledger fixtures (as-stored event shapes, same pattern as
# test_work_ledger_session_rollup) ------------------------------------------


def _marker(
    *,
    client: str = "codex",
    installed_at: float = 1000.0,
    event_id: str | None = None,
    provenance: str | None = CLI_INSTRUMENTATION_PROVENANCE,
) -> dict:
    metadata = {
        "client": client,
        "installed_at": installed_at,
        "installed_at_source": "backfill",
        "surface": "instructions_user",
    }
    if provenance is not None:
        metadata["instrumentation_provenance"] = provenance
    return {
        "event_id": event_id or f"evt_marker_{client}_{installed_at}",
        "created_at": installed_at,
        "source": "agent-sentinel-setup",
        "event_type": INSTRUMENTATION_MARKER_EVENT_TYPE,
        "run_id": None,
        "metadata": metadata,
    }


def _usage(
    *,
    session: str,
    client: str = "codex",
    started_at: float | None = None,
    parent: str | None = None,
    kind: str | None = None,
    event_id: str | None = None,
    transcript: str | None = None,
) -> dict:
    metadata = {
        "usage_source": "local_client_session_store",
        "usage_provenance": "agent_sentinel_local_usage_import",
        "client": client,
        "client_session_id": session,
        "parent_client_session_id": parent,
        "client_session_kind": kind,
        "cached_input_tokens": 0,
        "started_at": started_at,
        "project_dir": "/tmp/project",
    }
    if transcript is not None:
        metadata["client_transcript_id"] = transcript
    return {
        "event_id": event_id or f"evt_usage_{client}_{session}",
        "created_at": started_at or 100.0,
        "source": f"{client}-local-session-import",
        "event_type": "model_usage",
        "run_id": None,
        "provider": client,
        "model": "gpt-5.5",
        "estimated_input_tokens": 100,
        "estimated_output_tokens": 25,
        "estimated_cost_usd": 0.01,
        "usage_confidence": "client_reported",
        "cost_confidence": "estimated_from_tokens",
        "metadata": metadata,
    }


def _section(
    *,
    session: str,
    client: str = "codex",
    section_id: str = "sec-1",
    created_at: float = 100.0,
    transcript: str | None = None,
) -> dict:
    metadata = {
        "sentinel_semantic_kind": "section",
        "client": client,
        "client_session_id": session,
        "client_context_keys_authored": ["client_session_id"],
        "project_dir": "/tmp/project",
        "section_id": section_id,
        "section_status": "completed",
        "section_title": f"Section {section_id}",
    }
    if transcript is not None:
        metadata["client_transcript_id"] = transcript
    return {
        "event_id": f"evt_section_{client}_{session}_{section_id}",
        "created_at": created_at,
        "source": client,
        "event_type": "section_completed",
        "run_id": None,
        "metadata": metadata,
    }


def _entry(ledger: dict, client: str, session: str) -> dict:
    for entry in ledger["session_rollup"]["sessions"]:
        if entry["client"] == client and entry["client_session_id"] == session:
            return entry
    raise AssertionError(f"no rollup entry for {client}::{session}")


def _instrumentation_summary(ledger: dict) -> dict:
    return ledger["session_rollup"]["summary"]["instrumentation"]


# --- 1-2: `setup mark-instrumented` ----------------------------------------


def test_mark_instrumented_records_marker_event_and_is_idempotent(tmp_path: Path) -> None:
    store = tmp_path / "state"
    before = time.time()
    result = runner.invoke(app, ["setup", "mark-instrumented", "--client", "claude-code", "--store-dir", str(store), "--json"])
    assert result.exit_code == 0, result.output
    event = json.loads(result.output)["event"]
    assert event["event_type"] == INSTRUMENTATION_MARKER_EVENT_TYPE
    assert event["source"] == "agent-sentinel-setup"
    metadata = event["metadata"]
    assert metadata["client"] == "claude-code"
    assert metadata["surface"] == "instructions_user"
    assert metadata["installed_at_source"] == "command_time"
    # Keyless by design (fix round E): idempotency is a pre-scan for a genuine
    # marker, because a forged pre-seeded key would hijack keyed lookups.
    assert "idempotency_key" not in metadata
    # The provenance is stamped by the trust gate, never caller-supplied.
    assert metadata["instrumentation_provenance"] == CLI_INSTRUMENTATION_PROVENANCE
    assert is_instrumentation_marker_event(event)
    assert before - 1.0 <= float(metadata["installed_at"]) <= time.time() + 1.0

    # Re-run returns the ORIGINAL event: earliest honest install time wins.
    again = runner.invoke(app, ["setup", "mark-instrumented", "--client", "claude-code", "--store-dir", str(store), "--json"])
    assert again.exit_code == 0, again.output
    assert json.loads(again.output)["event"]["event_id"] == event["event_id"]
    assert len(_store_markers(store)) == 1


def test_mark_instrumented_backfill_stores_exact_epoch_and_rejects_bad_timestamps(tmp_path: Path) -> None:
    store = tmp_path / "state"
    iso = "2026-07-07T19:57:00+08:00"
    result = runner.invoke(
        app,
        ["setup", "mark-instrumented", "--client", "codex", "--installed-at", iso, "--store-dir", str(store), "--json"],
    )
    assert result.exit_code == 0, result.output
    metadata = json.loads(result.output)["event"]["metadata"]
    assert metadata["installed_at"] == datetime.fromisoformat(iso).timestamp()
    assert metadata["installed_at_source"] == "backfill"
    # Backfill is a deliberate rewrite: NO idempotency key.
    assert "idempotency_key" not in metadata

    naive = runner.invoke(
        app,
        ["setup", "mark-instrumented", "--client", "codex", "--installed-at", "2026-07-07T19:57:00", "--store-dir", str(store)],
    )
    assert naive.exit_code == 2

    future_iso = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    future = runner.invoke(
        app,
        ["setup", "mark-instrumented", "--client", "codex", "--installed-at", future_iso, "--store-dir", str(store)],
    )
    assert future.exit_code == 2

    assert len(_store_markers(store)) == 1


# --- 3-4: auto-record from the install commands -----------------------------


def test_setup_instructions_records_one_marker_across_write_and_rerun(tmp_path: Path) -> None:
    store = tmp_path / "state"
    target = tmp_path / "CLAUDE.md"
    first = runner.invoke(
        app, ["setup", "instructions", "--agent", "claude-code", "--path", str(target), "--store-dir", str(store)]
    )
    assert first.exit_code == 0, first.output
    # Idempotent re-run (no file change) records nothing more: the fresh
    # install's marker with the original time stays the ONE event.
    again = runner.invoke(
        app, ["setup", "instructions", "--agent", "claude-code", "--path", str(target), "--store-dir", str(store)]
    )
    assert again.exit_code == 0, again.output
    assert "No change" in again.output
    markers = _store_markers(store)
    assert len(markers) == 1
    metadata = markers[0]["metadata"]
    assert metadata["client"] == "claude-code"
    assert metadata["surface"] == "instructions_project"
    assert metadata["target_path"] == str(target)
    assert metadata["instrumentation_provenance"] == CLI_INSTRUMENTATION_PROVENANCE


def test_setup_instructions_dry_run_and_remove_record_nothing(tmp_path: Path) -> None:
    store = tmp_path / "state"
    target = tmp_path / "CLAUDE.md"
    dry = runner.invoke(
        app,
        ["setup", "instructions", "--agent", "claude-code", "--path", str(target), "--store-dir", str(store), "--dry-run"],
    )
    assert dry.exit_code == 0, dry.output
    assert _store_markers(store) == []

    # Install for real (one marker), then --remove must not add another.
    runner.invoke(app, ["setup", "instructions", "--agent", "claude-code", "--path", str(target), "--store-dir", str(store)])
    assert len(_store_markers(store)) == 1
    removed = runner.invoke(
        app,
        ["setup", "instructions", "--agent", "claude-code", "--path", str(target), "--store-dir", str(store), "--remove"],
    )
    assert removed.exit_code == 0, removed.output
    assert len(_store_markers(store)) == 1


def test_hooks_claude_code_install_records_marker_but_dry_run_does_not(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    store = tmp_path / "state"
    dry = runner.invoke(
        app,
        ["hooks", "claude-code", "install", "--project-dir", str(project), "--store-dir", str(store), "--dry-run"],
    )
    assert dry.exit_code == 0, dry.output
    assert _store_markers(store) == []

    result = runner.invoke(
        app, ["hooks", "claude-code", "install", "--project-dir", str(project), "--store-dir", str(store)]
    )
    assert result.exit_code == 0, result.output
    markers = _store_markers(store)
    assert len(markers) == 1
    metadata = markers[0]["metadata"]
    assert metadata["client"] == "claude-code"
    assert metadata["surface"] == "claude_code_hook"
    assert metadata["target_path"].endswith("claude_pre_tool_use.py")
    assert metadata["instrumentation_provenance"] == CLI_INSTRUMENTATION_PROVENANCE


# --- 5: trust gate ----------------------------------------------------------


def test_forged_idempotency_key_cannot_suppress_the_genuine_marker(tmp_path: Path) -> None:
    store = tmp_path / "state"
    forged = _marker(client="claude-code", installed_at=100.0)
    forged["metadata"]["idempotency_key"] = f"{INSTRUMENTATION_MARKER_EVENT_TYPE}:claude-code:instructions_user"
    # Pre-seed our idempotency key through the default (untrusted) path.
    SentinelService(store).record_event(forged)

    result = runner.invoke(app, ["setup", "mark-instrumented", "--client", "claude-code", "--store-dir", str(store), "--json"])
    assert result.exit_code == 0, result.output
    event = json.loads(result.output)["event"]
    # The pre-scan ignores the forged (stripped) event entirely; the CLI
    # writer lands a genuine trusted marker.
    assert is_instrumentation_marker_event(event)
    genuine = [e for e in _store_markers(store) if is_instrumentation_marker_event(e)]
    assert len(genuine) == 1


def test_forged_marker_is_stripped_and_classifies_nothing(tmp_path: Path) -> None:
    service = SentinelService(tmp_path / "state")
    # Default recording path (what MCP record_event / HTTP POST /events use):
    # a perfectly-shaped forged marker loses its provenance.
    recorded = service.record_event(_marker(client="codex", installed_at=100.0))
    assert "instrumentation_provenance" not in recorded["metadata"]
    assert recorded["metadata"]["reserved_instrumentation_provenance_stripped"] is True
    assert not is_instrumentation_marker_event(recorded)

    ledger = build_work_ledger([recorded, _usage(session="sess-a", client="codex", started_at=200.0)])
    assert _entry(ledger, "codex", "sess-a")["instrumentation_state"] == "unknown"
    summary = _instrumentation_summary(ledger)
    assert summary["markers_by_client"] == {}
    assert summary["post_context_kpi"]["context_rate"] is None


# --- 6-9: ledger classification --------------------------------------------


def test_no_marker_means_unknown_everywhere_and_kpi_absent() -> None:
    ledger = build_work_ledger(
        [
            _usage(session="sess-a", started_at=500.0),
            _usage(session="sess-b", started_at=600.0),
        ]
    )
    for session in ("sess-a", "sess-b"):
        entry = _entry(ledger, "codex", session)
        assert entry["instrumentation_state"] == "unknown"
        assert entry["instrumentation_state_basis"] is None
        assert entry["instrumentation_installed_at"] is None
    summary = _instrumentation_summary(ledger)
    assert summary["markers_by_client"] == {}
    assert summary["pre_instrumentation_sessions"] == 0
    assert summary["post_instrumentation_sessions"] == 0
    assert summary["unknown_sessions"] == 2
    assert summary["post_context_kpi"] == {
        "clients": [],
        "post_sessions": 0,
        "post_with_context": 0,
        "context_rate": None,
    }


def test_boundary_triplet_pre_post_post() -> None:
    ledger = build_work_ledger(
        [
            _marker(client="codex", installed_at=1000.0),
            _usage(session="before", started_at=999.0),
            _usage(session="boundary", started_at=1000.0),
            _usage(session="after", started_at=1001.0),
        ]
    )
    assert _entry(ledger, "codex", "before")["instrumentation_state"] == "pre_instrumentation"
    # Boundary equality goes to post — the non-flattering direction.
    assert _entry(ledger, "codex", "boundary")["instrumentation_state"] == "post_instrumentation"
    assert _entry(ledger, "codex", "after")["instrumentation_state"] == "post_instrumentation"
    for session in ("before", "boundary", "after"):
        entry = _entry(ledger, "codex", session)
        assert entry["instrumentation_state_basis"] == "session_start_vs_marker"
        assert entry["instrumentation_installed_at"] == 1000.0


def test_per_client_isolation_marker_never_classifies_other_clients() -> None:
    ledger = build_work_ledger(
        [
            _marker(client="claude-code", installed_at=1000.0),
            _usage(session="codex-sess", client="codex", started_at=1500.0),
            _usage(session="claude-sess", client="claude-code", started_at=1500.0),
        ]
    )
    codex_entry = _entry(ledger, "codex", "codex-sess")
    assert codex_entry["instrumentation_state"] == "unknown"
    assert codex_entry["instrumentation_state_basis"] is None
    assert _entry(ledger, "claude-code", "claude-sess")["instrumentation_state"] == "post_instrumentation"


def test_children_inherit_root_state_and_orphans_use_own_time() -> None:
    ledger = build_work_ledger(
        [
            _marker(client="codex", installed_at=1000.0),
            _usage(session="root-sess", kind="root", started_at=500.0),
            # Spawned after the marker, but the ROOT ran pre-install: inherit.
            _usage(session="root-sess:agent-child1", kind="child", parent="root-sess", started_at=2000.0),
            # Parent never formed an entry: classify by the child's own time.
            _usage(session="ghost:agent-child2", kind="child", parent="ghost", started_at=2000.0),
        ]
    )
    child = _entry(ledger, "codex", "root-sess:agent-child1")
    assert child["instrumentation_state"] == "pre_instrumentation"
    assert child["instrumentation_state_basis"] == "inherited_from_root"
    orphan = _entry(ledger, "codex", "ghost:agent-child2")
    assert orphan["instrumentation_state"] == "post_instrumentation"
    assert orphan["instrumentation_state_basis"] == "session_start_vs_marker"


# --- 10: chip precedence (unit level; rendered chips are covered in
# test_dashboard_product_tab.py) ---------------------------------------------


def test_client_log_evidence_chip_still_wins_over_pre_instrumentation() -> None:
    def esc(value: object) -> str:
        return html_module.escape(str(value if value is not None else ""))

    entry = {
        "client": "claude-code",
        "join": {
            "state": "unjoined",
            "reason": None,
            "row_states": {"unjoined": 1},
            "client_log_evidence": {"evidenced_event_count": 2, "conflicts": 0},
        },
        "work": {"counts": {"total": 0}},
        "instrumentation_state": "pre_instrumentation",
        "instrumentation_installed_at": 1000.0,
    }
    chip = _session_join_chip_html(entry, esc)
    assert "client-log evidence" in chip
    assert "Pre-instrumentation" not in chip


# --- 11: KPI math -----------------------------------------------------------


def test_kpi_counts_top_level_post_sessions_only() -> None:
    ledger = build_work_ledger(
        [
            _marker(client="codex", installed_at=1000.0),
            _usage(session="pre-sess", started_at=500.0),
            _usage(session="post-context", started_at=1500.0),
            _section(session="post-context", created_at=1500.0),
            _usage(session="post-bare", started_at=1600.0),
            # Child of a post session: excluded from the KPI denominator.
            _usage(session="post-context:agent-kid", kind="child", parent="post-context", started_at=1700.0),
        ]
    )
    summary = _instrumentation_summary(ledger)
    assert summary["pre_instrumentation_sessions"] == 1
    # Entry states count every session (the child inherits post from its root)…
    assert summary["post_instrumentation_sessions"] == 3
    kpi = summary["post_context_kpi"]
    # …but the KPI denominator is top-level sessions only.
    assert kpi["post_sessions"] == 2
    assert kpi["post_with_context"] == 1
    assert kpi["context_rate"] == 0.5
    assert kpi["clients"] == [
        {
            "client": "codex",
            "installed_at": 1000.0,
            "post_sessions": 2,
            "post_with_context": 1,
            "context_rate": 0.5,
        }
    ]


# --- 12: /sessions JSON stays additive; earliest marker wins ----------------


def test_sessions_json_gains_additive_fields_and_earliest_marker_wins(tmp_path: Path) -> None:
    store = tmp_path / "state"
    service = SentinelService(store)
    # Duplicate markers (both trusted, e.g. a backfill after an auto-record):
    # the EARLIEST installed_at is the boundary.
    late = "2026-01-02T00:00:00+00:00"
    early = "2026-01-01T00:00:00+00:00"
    for iso in (late, early):
        result = runner.invoke(
            app,
            ["setup", "mark-instrumented", "--client", "codex", "--installed-at", iso, "--store-dir", str(store)],
        )
        assert result.exit_code == 0, result.output
    earliest_epoch = datetime.fromisoformat(early).timestamp()
    service.record_event(
        _usage(session="post-sess", started_at=datetime.fromisoformat(late).timestamp() + 60.0),
        trusted_usage_import=True,
    )

    client = TestClient(create_local_api_app(store_dir=store))
    payload = client.get("/sessions").json()
    # Existing envelope keys are untouched (additive-only contract).
    assert payload["schema_version"] == "agent-sentinel.work-ledger.v2"
    assert payload["session_rollup_schema_version"] == "agent-sentinel.session-rollup.v1"
    assert payload["total_sessions"] == 1
    entry = payload["sessions"][0]
    assert entry["instrumentation_state"] == "post_instrumentation"
    assert entry["instrumentation_state_basis"] == "session_start_vs_marker"
    assert entry["instrumentation_installed_at"] == earliest_epoch
    instrumentation = payload["summary"]["instrumentation"]
    assert instrumentation["markers_by_client"]["codex"]["installed_at"] == earliest_epoch
    assert instrumentation["markers_by_client"]["codex"]["marker_count"] == 2
    assert instrumentation["post_context_kpi"]["clients"][0]["installed_at"] == earliest_epoch


# --- fix round A: the pre-instrumentation chip may fire ONLY for truly
# context-free sessions (no sections, no vetoes, no evidence, no conflicts) ---


def _esc(value: object) -> str:
    return html_module.escape(str(value if value is not None else ""))


def test_vetoed_session_with_later_marker_keeps_the_veto_chip() -> None:
    """A conflict-vetoed session HAS context here; the veto is the honest
    display, never 'no MCP work context could have been recorded'."""
    ledger = build_work_ledger(
        [
            _marker(client="codex", installed_at=1000.0),
            _usage(session="veto-sess", started_at=500.0, transcript="tx-USAGE"),
            _section(session="veto-sess", created_at=500.0, transcript="tx-WORK"),
        ]
    )
    entry = _entry(ledger, "codex", "veto-sess")
    assert entry["instrumentation_state"] == "pre_instrumentation"
    assert entry["join"]["state"] == "unjoined"
    assert int(entry["join"]["vetoed_rows"]) == 1
    chip = _session_join_chip_html(entry, _esc)
    assert "Not attributed" in chip
    assert "Pre-instrumentation" not in chip
    assert "no MCP work context could have been recorded" not in chip
    assert "conflicting evidence vetoes the join" in chip


def test_sections_bearing_unjoined_entry_never_gets_the_pre_chip() -> None:
    entry = {
        "client": "claude-code",
        "join": {
            "state": "unjoined",
            "reason": "no section shared this session's ids",
            "row_states": {"unjoined": 1},
            "vetoed_rows": 0,
            "client_log_evidence": None,
        },
        "work": {"counts": {"total": 2}},
        "instrumentation_state": "pre_instrumentation",
        "instrumentation_state_basis": "session_start_vs_marker",
        "instrumentation_installed_at": 1000.0,
    }
    chip = _session_join_chip_html(entry, _esc)
    assert "Not attributed" in chip
    assert "Pre-instrumentation" not in chip


def test_conflicted_evidence_entry_never_gets_the_pre_chip() -> None:
    entry = {
        "client": "claude-code",
        "join": {
            "state": "unjoined",
            "reason": None,
            "row_states": {"unjoined": 1},
            "vetoed_rows": 0,
            "client_log_evidence": {"evidenced_event_count": 0, "conflicts": 1},
        },
        "work": {"counts": {"total": 0}},
        "instrumentation_state": "pre_instrumentation",
        "instrumentation_state_basis": "session_start_vs_marker",
        "instrumentation_installed_at": 1000.0,
    }
    chip = _session_join_chip_html(entry, _esc)
    assert "Pre-instrumentation" not in chip


def test_truly_bare_pre_instrumentation_session_still_gets_the_chip() -> None:
    entry = {
        "client": "claude-code",
        "join": {
            "state": "unjoined",
            "reason": None,
            "row_states": {"unjoined": 1},
            "vetoed_rows": 0,
            "client_log_evidence": None,
        },
        "work": {"counts": {"total": 0}},
        "instrumentation_state": "pre_instrumentation",
        "instrumentation_state_basis": "session_start_vs_marker",
        "instrumentation_installed_at": 1000.0,
    }
    chip = _session_join_chip_html(entry, _esc)
    assert "Pre-instrumentation — recording was not installed when this session ran" in chip
    assert "no MCP work context could have been recorded" in chip


def test_inherited_child_pre_chip_speaks_about_the_root_session() -> None:
    """A child can START after install yet inherit pre from its root: the
    title must not claim THIS session started before install."""
    entry = {
        "client": "claude-code",
        "join": {
            "state": "unjoined",
            "reason": None,
            "row_states": {"unjoined": 1},
            "vetoed_rows": 0,
            "client_log_evidence": None,
        },
        "work": {"counts": {"total": 0}},
        "instrumentation_state": "pre_instrumentation",
        "instrumentation_state_basis": "inherited_from_root",
        "instrumentation_installed_at": 1000.0,
    }
    chip = _session_join_chip_html(entry, _esc)
    assert "Pre-instrumentation" in chip
    assert (
        "The root session of this subagent session started before recording instructions for Claude Code were installed"
        in chip
    )
    assert "this session started before that" not in chip


# --- fix round B: command-time auto-markers record on FRESH installs only ---


def test_setup_instructions_no_change_path_records_nothing_and_hints(tmp_path: Path) -> None:
    """The surface being already installed does NOT prove it was installed NOW:
    stamping installed_at=now would flip genuinely-instrumented history to
    pre_instrumentation (wrong beats missing, the forbidden direction)."""
    from agent_chronicle import install_guide

    store = tmp_path / "state"
    target = tmp_path / "CLAUDE.md"
    target.write_text(install_guide.render_instruction_file("", remove=False), encoding="utf-8")

    result = runner.invoke(
        app, ["setup", "instructions", "--agent", "claude-code", "--path", str(target), "--store-dir", str(store)]
    )
    assert result.exit_code == 0, result.output
    assert "No change" in result.output
    assert _store_markers(store) == []
    combined = result.output
    assert "mark-instrumented" in combined
    assert "--installed-at" in combined


def test_setup_instructions_block_replacement_records_nothing_and_hints(tmp_path: Path) -> None:
    from agent_chronicle import install_guide

    store = tmp_path / "state"
    target = tmp_path / "CLAUDE.md"
    target.write_text(
        f"{install_guide.INSTRUCTIONS_BEGIN_MARKER}\nstale old block body\n{install_guide.INSTRUCTIONS_END_MARKER}\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app, ["setup", "instructions", "--agent", "claude-code", "--path", str(target), "--store-dir", str(store)]
    )
    assert result.exit_code == 0, result.output
    assert _store_markers(store) == []
    assert "mark-instrumented" in result.output
    assert "--installed-at" in result.output


def test_setup_instructions_rerun_with_existing_marker_skips_silently(tmp_path: Path) -> None:
    store = tmp_path / "state"
    target = tmp_path / "CLAUDE.md"
    first = runner.invoke(
        app, ["setup", "instructions", "--agent", "claude-code", "--path", str(target), "--store-dir", str(store)]
    )
    assert first.exit_code == 0, first.output
    assert len(_store_markers(store)) == 1

    again = runner.invoke(
        app, ["setup", "instructions", "--agent", "claude-code", "--path", str(target), "--store-dir", str(store)]
    )
    assert again.exit_code == 0, again.output
    assert len(_store_markers(store)) == 1
    # A genuine marker already covers this client: no backfill hint noise.
    assert "mark-instrumented" not in again.output


def test_hooks_force_reinstall_records_nothing_and_hints(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    store = tmp_path / "state"
    first = runner.invoke(app, ["hooks", "claude-code", "install", "--project-dir", str(project), "--store-dir", str(store)])
    assert first.exit_code == 0, first.output
    assert len(_store_markers(store)) == 1

    # Re-install over the existing wrapper: same store already carries the
    # genuine marker, so nothing is recorded and no hint is needed.
    again = runner.invoke(
        app, ["hooks", "claude-code", "install", "--project-dir", str(project), "--store-dir", str(store), "--force"]
    )
    assert again.exit_code == 0, again.output
    assert len(_store_markers(store)) == 1
    assert "mark-instrumented" not in again.output

    # Re-install bound to a DIFFERENT (marker-less) store: still nothing —
    # install time is unknown — plus the honest backfill hint.
    other_store = tmp_path / "other-state"
    rebound = runner.invoke(
        app,
        ["hooks", "claude-code", "install", "--project-dir", str(project), "--store-dir", str(other_store), "--force"],
    )
    assert rebound.exit_code == 0, rebound.output
    assert _store_markers(other_store) == []
    assert "mark-instrumented" in rebound.output
    assert "--installed-at" in rebound.output


# --- fix round C: the marker store derives from the INSTALL TARGET, never cwd ---


def test_hooks_install_marker_lands_in_target_projects_store_not_cwd(tmp_path: Path, monkeypatch) -> None:
    cwd_project = tmp_path / "cwd-proj"
    (cwd_project / ".agent-sentinel" / "state").mkdir(parents=True)
    target_project = tmp_path / "target-proj"
    (target_project / ".agent-sentinel" / "state").mkdir(parents=True)
    monkeypatch.chdir(cwd_project)
    monkeypatch.delenv("AGENT_CHRONICLE_STORE_DIR", raising=False)

    result = runner.invoke(app, ["hooks", "claude-code", "install", "--project-dir", str(target_project)])
    assert result.exit_code == 0, result.output
    assert len(_store_markers(target_project / ".agent-sentinel" / "state")) == 1
    assert _store_markers(cwd_project / ".agent-sentinel" / "state") == []


def test_hooks_install_without_target_store_skips_and_hints(tmp_path: Path, monkeypatch) -> None:
    cwd_project = tmp_path / "cwd-proj"
    (cwd_project / ".agent-sentinel" / "state").mkdir(parents=True)
    target_project = tmp_path / "target-proj"
    target_project.mkdir()
    monkeypatch.chdir(cwd_project)
    monkeypatch.delenv("AGENT_CHRONICLE_STORE_DIR", raising=False)

    result = runner.invoke(app, ["hooks", "claude-code", "install", "--project-dir", str(target_project)])
    assert result.exit_code == 0, result.output
    # Never the cwd store, never a freshly created store at the target.
    assert _store_markers(cwd_project / ".agent-sentinel" / "state") == []
    assert not (target_project / ".agent-sentinel" / "state").exists()
    assert "mark-instrumented" in result.output


def test_user_level_instructions_without_store_dir_skip_and_hint(tmp_path: Path, monkeypatch) -> None:
    """A user-level surface spans projects: only an explicit --store-dir names
    an honest home for the marker — the cwd store must never inherit it."""
    home = tmp_path / "home"
    home.mkdir()
    cwd_project = tmp_path / "cwd-proj"
    (cwd_project / ".agent-sentinel" / "state").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(cwd_project)
    monkeypatch.delenv("AGENT_CHRONICLE_STORE_DIR", raising=False)

    result = runner.invoke(app, ["setup", "instructions", "--agent", "claude-code", "--user"])
    assert result.exit_code == 0, result.output
    assert (home / ".claude" / "CLAUDE.md").is_file()
    assert _store_markers(cwd_project / ".agent-sentinel" / "state") == []
    assert "mark-instrumented" in result.output
    assert "--store-dir" in result.output


def test_path_instructions_without_store_dir_skip_and_hint(tmp_path: Path, monkeypatch) -> None:
    cwd_project = tmp_path / "cwd-proj"
    (cwd_project / ".agent-sentinel" / "state").mkdir(parents=True)
    monkeypatch.chdir(cwd_project)
    monkeypatch.delenv("AGENT_CHRONICLE_STORE_DIR", raising=False)
    target = tmp_path / "elsewhere" / "CLAUDE.md"
    target.parent.mkdir()

    result = runner.invoke(app, ["setup", "instructions", "--agent", "claude-code", "--path", str(target)])
    assert result.exit_code == 0, result.output
    assert _store_markers(cwd_project / ".agent-sentinel" / "state") == []
    assert "mark-instrumented" in result.output


def test_project_level_instructions_marker_uses_the_target_projects_store(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "proj"
    (project / ".agent-sentinel" / "state").mkdir(parents=True)
    monkeypatch.chdir(project)
    monkeypatch.delenv("AGENT_CHRONICLE_STORE_DIR", raising=False)

    result = runner.invoke(app, ["setup", "instructions", "--agent", "claude-code"])
    assert result.exit_code == 0, result.output
    markers = _store_markers(project / ".agent-sentinel" / "state")
    assert len(markers) == 1
    assert markers[0]["metadata"]["surface"] == "instructions_project"

    # Same command in a store-less project: skip + hint, never create a store.
    bare = tmp_path / "bare-proj"
    bare.mkdir()
    monkeypatch.chdir(bare)
    skipped = runner.invoke(app, ["setup", "instructions", "--agent", "claude-code"])
    assert skipped.exit_code == 0, skipped.output
    assert not (bare / ".agent-sentinel").exists()
    assert "mark-instrumented" in skipped.output


# --- fix round E: read-time marker plausibility + key-less idempotency ------


def test_implausible_markers_classify_nothing_and_are_counted() -> None:
    """A merge-imported/forged marker that claims an install AFTER its own
    recording (or carries junk client/installed_at) must not classify — and
    the rejection is COUNTED, never silent."""
    future = _marker(client="codex", installed_at=99_999.0, event_id="evt_forged_future")
    future["created_at"] = 1000.0  # claims install ~98s999 after it was recorded
    boolean = _marker(client="codex", installed_at=True, event_id="evt_forged_bool")  # type: ignore[arg-type]
    boolean["created_at"] = 1000.0
    long_client = _marker(client="x" * 81, installed_at=900.0, event_id="evt_forged_client")
    long_client["created_at"] = 1000.0

    ledger = build_work_ledger([future, boolean, long_client, _usage(session="sess-a", started_at=100_000.0)])
    entry = _entry(ledger, "codex", "sess-a")
    assert entry["instrumentation_state"] == "unknown"
    assert entry["instrumentation_state_basis"] is None
    summary = _instrumentation_summary(ledger)
    assert summary["markers_by_client"] == {}
    assert summary["invalid_marker_count"] == 3


def test_backfill_marker_within_write_slack_still_classifies() -> None:
    marker = _marker(client="codex", installed_at=1000.0)
    marker["created_at"] = 1100.0  # recorded after the install fact: fine
    ledger = build_work_ledger([marker, _usage(session="sess-a", started_at=1500.0)])
    assert _entry(ledger, "codex", "sess-a")["instrumentation_state"] == "post_instrumentation"
    assert _instrumentation_summary(ledger)["invalid_marker_count"] == 0


def test_command_time_markers_are_keyless_and_idempotent_by_prescan(tmp_path: Path) -> None:
    """Service-level idempotency is forgeable (a pre-seeded key permanently
    hijacks the lookup); marker idempotency is a pre-scan for a GENUINE
    marker instead, and the recorded marker carries no key at all."""
    store = tmp_path / "state"
    result = runner.invoke(app, ["setup", "mark-instrumented", "--client", "claude-code", "--store-dir", str(store), "--json"])
    assert result.exit_code == 0, result.output
    metadata = json.loads(result.output)["event"]["metadata"]
    assert "idempotency_key" not in metadata


def test_forged_keyed_event_never_defeats_marker_idempotency(tmp_path: Path) -> None:
    store = tmp_path / "state"
    forged = _marker(client="claude-code", installed_at=100.0)
    forged["metadata"]["idempotency_key"] = f"{INSTRUMENTATION_MARKER_EVENT_TYPE}:claude-code:instructions_user"
    SentinelService(store).record_event(forged)

    event_ids = set()
    for _ in range(3):
        result = runner.invoke(
            app, ["setup", "mark-instrumented", "--client", "claude-code", "--store-dir", str(store), "--json"]
        )
        assert result.exit_code == 0, result.output
        event = json.loads(result.output)["event"]
        assert is_instrumentation_marker_event(event)
        event_ids.add(event["event_id"])
    # All three runs converge on the SAME genuine marker; the forged event
    # neither suppresses it nor forces an append per run.
    assert len(event_ids) == 1
    genuine = [e for e in _store_markers(store) if is_instrumentation_marker_event(e)]
    assert len(genuine) == 1


# --- fix round I: "unknown" never carries an inherited basis ------------------


def test_marker_less_children_serve_unknown_with_no_basis() -> None:
    ledger = build_work_ledger(
        [
            _usage(session="root-sess", kind="root", started_at=500.0),
            _usage(session="root-sess:agent-kid", kind="child", parent="root-sess", started_at=600.0),
        ]
    )
    for session in ("root-sess", "root-sess:agent-kid"):
        entry = _entry(ledger, "codex", session)
        assert entry["instrumentation_state"] == "unknown"
        assert entry["instrumentation_state_basis"] is None
