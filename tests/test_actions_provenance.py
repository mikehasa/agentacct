"""Honest Actions provenance: distinguish a transcript scan from a client hook.

The Receipt Actions dimension used to declare ``hook`` provenance for any tool
categories/names/commands. But Codex and OpenCode Actions are derived from the
client's own store on disk at import time (their hooks do not fire), not observed
by a live hook — so they now carry a distinct ``transcript_scan`` provenance.
"""

from __future__ import annotations

from agentacct.receipt import (
    SOURCE_HOOK,
    SOURCE_MCP,
    SOURCE_TRANSCRIPT_SCAN,
    _actions_dimension,
)
from agentacct.tool_activity import (
    DISCOVERY_TOOL_ACTIVITY_CAPTURE_BASIS,
    TOOL_ACTIVITY_CAPTURE_BASIS,
    TOOL_ACTIVITY_EVENT_TYPE,
    build_tool_activity_capture_basis_by_session,
)


def _activity_event(client: str, session: str, basis: str) -> dict:
    return {
        "event_type": TOOL_ACTIVITY_EVENT_TYPE,
        "metadata": {
            "client": client,
            "client_session_id": session,
            "capture_basis": basis,
            "tool_category_counts": {"execute": 1},
        },
    }


# --- the capture-basis builder ----------------------------------------------


def test_capture_basis_builder_maps_hook_and_scan():
    bases = build_tool_activity_capture_basis_by_session(
        [
            _activity_event("opencode", "s1", DISCOVERY_TOOL_ACTIVITY_CAPTURE_BASIS),
            _activity_event("claude-code", "s2", TOOL_ACTIVITY_CAPTURE_BASIS),
            _activity_event("codex", "s3", DISCOVERY_TOOL_ACTIVITY_CAPTURE_BASIS),
        ]
    )
    assert bases[("opencode", "s1")] == ["transcript_scan"]
    assert bases[("claude-code", "s2")] == ["hook"]
    assert bases[("codex", "s3")] == ["transcript_scan"]


def test_capture_basis_builder_reports_both_when_mixed_and_dedupes():
    bases = build_tool_activity_capture_basis_by_session(
        [
            _activity_event("x", "s", TOOL_ACTIVITY_CAPTURE_BASIS),
            _activity_event("x", "s", DISCOVERY_TOOL_ACTIVITY_CAPTURE_BASIS),
            _activity_event("x", "s", DISCOVERY_TOOL_ACTIVITY_CAPTURE_BASIS),
        ]
    )
    assert bases[("x", "s")] == ["hook", "transcript_scan"]


def test_capture_basis_builder_ignores_unknown_basis_and_blank_identity():
    bases = build_tool_activity_capture_basis_by_session(
        [
            _activity_event("x", "s", "some_future_basis"),  # unknown -> nothing
            _activity_event("", "s", TOOL_ACTIVITY_CAPTURE_BASIS),  # blank client
            _activity_event("x", "", TOOL_ACTIVITY_CAPTURE_BASIS),  # blank session
            {"event_type": "other", "metadata": {}},  # not a tool-activity event
        ]
    )
    assert bases == {}


# --- the Actions dimension provenance ---------------------------------------


def test_actions_scan_declares_transcript_scan_not_hook():
    d = _actions_dimension(
        {
            "actions": {
                "tool_category_counts": {"execute": 3},
                "commands": ["npm test"],
                "capture_bases": [SOURCE_TRANSCRIPT_SCAN],
            }
        }
    )
    assert d["provenance"] == [SOURCE_TRANSCRIPT_SCAN]
    assert SOURCE_HOOK not in d["provenance"]


def test_actions_hook_declares_hook():
    d = _actions_dimension(
        {"actions": {"tool_category_counts": {"read": 3}, "capture_bases": [SOURCE_HOOK]}}
    )
    assert d["provenance"] == [SOURCE_HOOK]


def test_actions_mixed_declares_both():
    d = _actions_dimension(
        {
            "actions": {
                "tool_name_counts": {"bash": 2},
                "capture_bases": [SOURCE_HOOK, SOURCE_TRANSCRIPT_SCAN],
            }
        }
    )
    assert SOURCE_HOOK in d["provenance"]
    assert SOURCE_TRANSCRIPT_SCAN in d["provenance"]


def test_actions_without_capture_bases_falls_back_to_hook():
    # A row recorded before capture-basis tracking shipped has no capture_bases and
    # honestly falls back to hook (its historical source) — never invents a scan.
    d = _actions_dimension({"actions": {"tool_category_counts": {"read": 3}}})
    assert d["provenance"] == [SOURCE_HOOK]


def test_actions_malformed_capture_bases_never_crashes_or_mislabels():
    # A corrupted non-list capture_bases (a truthy scalar, or a bare string that is
    # iterable char-by-char) must not crash the receipt or be silently mislabeled —
    # it coerces to the honest historical hook fallback.
    for bad in (True, 5, 1.5, "transcript_scan", {"transcript_scan": 1}):
        d = _actions_dimension(
            {"actions": {"tool_category_counts": {"read": 3}, "capture_bases": bad}}
        )
        assert d["provenance"] == [SOURCE_HOOK]
    # A list with junk entries keeps only the valid tokens.
    d = _actions_dimension(
        {
            "actions": {
                "tool_category_counts": {"read": 3},
                "capture_bases": [SOURCE_TRANSCRIPT_SCAN, "bogus", 7, None],
            }
        }
    )
    assert d["provenance"] == [SOURCE_TRANSCRIPT_SCAN]


def test_actions_scan_with_section_touched_files_reports_both_sources():
    # touched files recorded via MCP sections stay MCP; the scan-captured categories
    # add transcript_scan — both are honestly present.
    d = _actions_dimension(
        {
            "work_items": [{"files": ["src/a.py"]}],
            "actions": {
                "tool_category_counts": {"edit": 1},
                "touched_files": ["src/a.py"],
                "capture_bases": [SOURCE_TRANSCRIPT_SCAN],
            },
        }
    )
    assert SOURCE_MCP in d["provenance"]
    assert SOURCE_TRANSCRIPT_SCAN in d["provenance"]


# --- end to end: real store -> receipt --------------------------------------


def test_opencode_receipt_actions_declare_transcript_scan(tmp_path):
    from typer.testing import CliRunner

    from agentacct.api import _task_title, build_store_task_projection
    from agentacct.cli import app
    from agentacct.receipt import build_receipt, latest_store_activity
    from tests.test_opencode_actions import (
        _add_opencode_tool_parts,
        _make_opencode_db_home,
        _session_row,
        _tool_part,
    )

    home = _make_opencode_db_home(tmp_path, rows=[_session_row("ses_a", "/w/proj")])
    _add_opencode_tool_parts(
        home,
        [
            _tool_part("ses_a", "bash", command="npm test", exit_code=0, time_end=5),
            _tool_part("ses_a", "read", file_path="/w/proj/README.md"),
        ],
    )
    store = tmp_path / "state"
    result = CliRunner().invoke(
        app,
        [
            "usage", "import-local", "--client", "opencode",
            "--store-dir", str(store), "--opencode-home", str(home), "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    projection = build_store_task_projection(store)
    tasks = [t for t in projection.get("tasks", []) if isinstance(t, dict)]
    task = next(t for t in tasks if "ses_a" in str(t.get("session_keys")))
    # The projection carried the transcript-scan capture basis onto the Task.
    assert task["actions"]["capture_bases"] == [SOURCE_TRANSCRIPT_SCAN]

    receipt = build_receipt(
        task,
        public_task_id=str(task.get("task_id")),
        title=_task_title(task),
        latest_store_activity_at=latest_store_activity(tasks),
    )
    actions_dim = next(
        d for d in receipt["dimensions"] if isinstance(d, dict) and d.get("key") == "actions"
    ) if isinstance(receipt.get("dimensions"), list) else receipt["dimensions"]["actions"]
    assert SOURCE_TRANSCRIPT_SCAN in actions_dim["provenance"]
    assert SOURCE_HOOK not in actions_dim["provenance"]
    # The provenance roll-up legend explains the new source.
    legend = receipt["dimensions"]["provenance"]["legend"]
    assert SOURCE_TRANSCRIPT_SCAN in legend
