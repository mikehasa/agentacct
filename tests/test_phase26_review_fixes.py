"""Phase 2.6 adversarial-review fix round regressions.

Covers the 2 confirmed majors and the code-level minors:
- MAJOR 0 (+minors 3/13): store scope is EXPLICIT — `other_project` chips can
  only fire on a conventional per-project store, never on the documented
  global store or any custom --store-dir layout; ambiguous label comparisons
  ("~", unknown, case variants) always stay "this_store".
- MAJOR 1: a conflict-VETOED session (its context demonstrably lives HERE)
  never claims its context lives in another project's store.
- Minors: hook home-dir worktree label is "~"; codex parent adoption reads
  ONLY the rollout's first session_meta line; non-dict JSON rollout lines
  never crash the import; worktree chip title names the OWNER label;
  backslashes are path separators only for Windows-shaped paths; session
  project label = majority LABEL first, then majority source within it, with
  source ties keeping the worktree marker.

All stores/homes are throwaway tmp_path fixtures (suite conftest guards the
real dogfood ledger).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agentacct.api import _store_scope_and_label, create_local_api_app
from agentacct.client_usage import _read_codex_rollout_usage
from agentacct.hooks import derive_claude_code_client_context
from agentacct.service import SentinelService
from agentacct.store_resolution import claude_worktree_owner_path_text
from agentacct.work_ledger import _project_label_info, build_work_ledger

import json


def _usage_event(
    *,
    session: str,
    project_dir: str | None,
    transcript: str | None = None,
    client: str = "claude-code",
    lane: str | None = "model:claude-fable-5",
) -> dict:
    """Pre-2.6 historical row shape (same as tests/test_phase26_dogfood_fixes)."""

    metadata = {
        "usage_source": "local_client_session_store",
        "usage_update_semantics": "claude_assistant_message_usage_rows",
        "client": client,
        "client_session_id": session,
        "client_session_kind": "root",
        "parent_client_session_id": None,
        "client_transcript_id": transcript if transcript is not None else session,
        "client_spawn_status": None,
        "client_thread_source": None,
        "source_file": f"{session}.jsonl",
        "project_dir": project_dir,
        "title_redacted": True,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_5m_input_tokens": 0,
        "cache_creation_1h_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "client_reported_cost_usd": None,
        "client_cost_source": None,
        "started_at": 1_751_700_000,
        "updated_at": 1_751_700_100,
        "turn_count": 3,
        "raw_usage_rows": 3,
        "deduplicated_usage_rows": 0,
    }
    if lane is not None:
        metadata["usage_row_lane"] = lane
    return {
        "source": f"{client}-local-session-import",
        "event_type": "model_usage",
        "provider": client,
        "model": "claude-fable-5",
        "estimated_input_tokens": 100,
        "estimated_output_tokens": 25,
        "estimated_cost_usd": None,
        "usage_confidence": "client_reported",
        "cost_confidence": "unknown",
        "cost_basis": "client_session_totals",
        "metadata": metadata,
    }


def _record_usage(store_root: Path, event: dict) -> dict:
    return SentinelService(store_root).record_event(event, trusted_usage_import=True)


def _record_section(
    store_root: Path, *, section_id: str, session: str, transcript: str | None = None, client: str = "claude-code"
) -> dict:
    metadata = {
        "sentinel_semantic_kind": "section",
        "section_id": section_id,
        "section_status": "completed",
        "section_title": f"Section {section_id}",
        "client": client,
        "client_session_id": session,
    }
    if transcript is not None:
        metadata["client_transcript_id"] = transcript
    return SentinelService(store_root).record_event(
        {"source": client, "event_type": "section_completed", "metadata": metadata}
    )


def _product_html(store_root: Path) -> str:
    client = TestClient(create_local_api_app(store_dir=store_root))
    response = client.get("/")
    assert response.status_code == 200
    return response.text


def _sessions_html(store_root: Path) -> str:
    client = TestClient(create_local_api_app(store_dir=store_root))
    response = client.get("/sessions?days=all", headers={"Accept": "text/html"})
    assert response.status_code == 200
    return response.text


def _project_store(tmp_path) -> Path:
    return tmp_path / "dash-project" / ".agent-sentinel" / "state"


FOREIGN_PROJECT_DIR = "/Users/owner/legacy-project-a"


# ---------------------------------------------------------------------------
# MAJOR 0 (+ minors 3/13) — explicit store scope gates every other_project claim
# ---------------------------------------------------------------------------


def test_global_shaped_store_renders_plain_no_mcp_context(tmp_path) -> None:
    """The INSTALL.md global layout must never claim 'session ran in <project>':
    the global store IS where every project's new MCP context is recorded."""

    store_root = tmp_path / "ghome" / ".agent-sentinel-global" / "state"
    _record_usage(store_root, _usage_event(session="foreign", project_dir=FOREIGN_PROJECT_DIR))

    assert _store_scope_and_label(store_root)[0] == "custom"
    client = TestClient(create_local_api_app(store_dir=store_root))
    payload = client.get("/sessions").json()
    assert payload["sessions"][0]["join"]["context_scope"] == "this_store"

    html = client.get("/sessions?days=all", headers={"Accept": "text/html"}).text
    assert ">No MCP context</span>" in html
    assert "session ran in" not in html
    assert "Stores are per-project" not in html


def test_custom_store_dir_never_fires_other_project(tmp_path) -> None:
    store_root = tmp_path / "sentinel-store"
    _record_usage(store_root, _usage_event(session="foreign", project_dir=FOREIGN_PROJECT_DIR))

    ledger = build_work_ledger(
        SentinelService(store_root).list_all_events(), store_project_label="sentinel-store", store_scope="custom"
    )

    assert ledger["session_rollup"]["sessions"][0]["join"]["context_scope"] == "this_store"


def test_project_store_still_fires_other_project_for_truly_foreign_sessions(tmp_path) -> None:
    """Control: the cross-store chip survives on conventional project stores."""

    store_root = _project_store(tmp_path)
    _record_usage(store_root, _usage_event(session="foreign", project_dir=FOREIGN_PROJECT_DIR))

    ledger = build_work_ledger(
        SentinelService(store_root).list_all_events(), store_project_label="dash-project", store_scope="project"
    )

    assert ledger["session_rollup"]["sessions"][0]["join"]["context_scope"] == "other_project"


def test_missing_store_scope_never_fires_other_project(tmp_path) -> None:
    """A label WITHOUT an explicit scope is not enough — scope is never guessed."""

    store_root = _project_store(tmp_path)
    _record_usage(store_root, _usage_event(session="foreign", project_dir=FOREIGN_PROJECT_DIR))

    ledger = build_work_ledger(SentinelService(store_root).list_all_events(), store_project_label="dash-project")

    assert ledger["session_rollup"]["sessions"][0]["join"]["context_scope"] == "this_store"


def test_ambiguous_labels_always_stay_this_store(tmp_path) -> None:
    """'~' on either side, unknown labels, and case-variant equality are all
    ambiguity — missing beats wrong applies to the chip itself."""

    store_root = _project_store(tmp_path)
    _record_usage(store_root, _usage_event(session="home-session", project_dir=str(Path.home()), lane="model:m1"))
    _record_usage(store_root, _usage_event(session="alias-session", project_dir="/Users/owner/aliasrepo", lane="model:m2"))
    _record_usage(store_root, _usage_event(session="unlabeled-session", project_dir=None, lane="model:m3"))

    events = SentinelService(store_root).list_all_events()

    # Session labeled "~" (ran from the home directory) on a project store.
    ledger = build_work_ledger(events, store_project_label="dash-project", store_scope="project")
    joins = {entry["client_session_id"]: entry["join"] for entry in ledger["session_rollup"]["sessions"]}
    assert joins["home-session"]["context_scope"] == "this_store"
    assert joins["unlabeled-session"]["context_scope"] == "this_store"

    # Case-variant equality is an alias of THIS project, not another project.
    alias_ledger = build_work_ledger(events, store_project_label="ALIASREPO", store_scope="project")
    alias_joins = {entry["client_session_id"]: entry["join"] for entry in alias_ledger["session_rollup"]["sessions"]}
    assert alias_joins["alias-session"]["context_scope"] == "this_store"

    # A store whose OWN label is "~" (home-layout store) claims nothing.
    home_store_ledger = build_work_ledger(events, store_project_label="~", store_scope="project")
    home_joins = {entry["client_session_id"]: entry["join"] for entry in home_store_ledger["session_rollup"]["sessions"]}
    assert all(join["context_scope"] == "this_store" for join in home_joins.values())


# ---------------------------------------------------------------------------
# MAJOR 1 — a vetoed session never claims its context lives elsewhere
# ---------------------------------------------------------------------------


def test_vetoed_session_never_fires_other_project(tmp_path) -> None:
    """Verifier repro: a section in THIS store shares the usage row's
    client_transcript_id but carries a conflicting client_session_id — the
    join is vetoed, the vetoing context demonstrably lives HERE, and the
    session must keep its honest veto reason instead of pointing elsewhere."""

    store_root = _project_store(tmp_path)
    _record_usage(
        store_root,
        _usage_event(session="veto-usage", transcript="shared-T", project_dir="/Users/owner/other-place"),
    )
    _record_section(store_root, section_id="sec-conflict", session="other-session", transcript="shared-T")

    ledger = build_work_ledger(
        SentinelService(store_root).list_all_events(), store_project_label="dash-project", store_scope="project"
    )

    entry = next(
        item for item in ledger["session_rollup"]["sessions"] if item["client_session_id"] == "veto-usage"
    )
    assert entry["join"]["state"] == "unjoined"
    assert entry["join"]["vetoed_rows"] == 1
    assert "vetoes the join" in str(entry["join"]["reason"])
    # THE fix: vetoed stays this_store, never "ran in another project".
    assert entry["join"]["context_scope"] == "this_store"


def test_vetoed_session_chip_never_renders_the_other_project_claim(tmp_path) -> None:
    store_root = _project_store(tmp_path)
    _record_usage(
        store_root,
        _usage_event(session="veto-usage", transcript="shared-T", project_dir="/Users/owner/other-place"),
    )
    _record_section(store_root, section_id="sec-conflict", session="other-session", transcript="shared-T")

    html = _sessions_html(store_root)

    # The self-contradictory chip (both claims in one title attr) is gone.
    assert "session ran in other-place" not in html
    assert "lives in that project&#x27;s own store" not in html
    assert "lives in that project's own store" not in html
    # The honest veto reason survives on the chip title.
    assert "conflicting evidence vetoes the join" in html


def test_attribution_rows_expose_join_vetoes(tmp_path) -> None:
    store_root = _project_store(tmp_path)
    _record_usage(
        store_root,
        _usage_event(session="veto-usage", transcript="shared-T", project_dir="/Users/owner/other-place"),
    )
    _record_section(store_root, section_id="sec-conflict", session="other-session", transcript="shared-T")

    ledger = build_work_ledger(SentinelService(store_root).list_all_events())

    attribution = ledger["attributions"][0]
    assert attribution["join_vetoes"] == ["client_session_id_conflict"]
    # Non-vetoed rows expose an empty list, never a missing key.
    clean_store = _project_store(tmp_path / "clean")
    _record_usage(clean_store, _usage_event(session="clean", project_dir=FOREIGN_PROJECT_DIR))
    clean_ledger = build_work_ledger(SentinelService(clean_store).list_all_events())
    assert clean_ledger["attributions"][0]["join_vetoes"] == []


# ---------------------------------------------------------------------------
# Minor 1 — hook capture labels the home directory as "~", never the username
# ---------------------------------------------------------------------------


def test_hook_label_for_home_dir_worktree_owner_is_tilde() -> None:
    home = Path.home()
    context = derive_claude_code_client_context(
        {
            "session_id": "hook-session",
            "cwd": str(home / ".claude" / "worktrees" / "zippy-visionary-123abc"),
            "hook_event_name": "PreToolUse",
        }
    )
    assert context is not None
    assert context["project_label"] == "~"
    assert home.name not in str(context["project_label"])


def test_hook_label_for_home_dir_cwd_is_tilde() -> None:
    context = derive_claude_code_client_context(
        {"session_id": "hook-session", "cwd": str(Path.home()), "hook_event_name": "PreToolUse"}
    )
    assert context is not None
    assert context["project_label"] == "~"


def test_hook_label_for_plain_repo_unchanged() -> None:
    context = derive_claude_code_client_context(
        {"session_id": "hook-session", "cwd": "/Users/owner/plain-repo", "hook_event_name": "PreToolUse"}
    )
    assert context is not None
    assert context["project_label"] == "plain-repo"


# ---------------------------------------------------------------------------
# Minors 2 + 12 — codex rollout parsing: first session_meta only, non-dict-safe
# ---------------------------------------------------------------------------

_USAGE_LINE = {
    "type": "event_msg",
    "payload": {
        "info": {
            "total_token_usage": {
                "input_tokens": 900,
                "cached_input_tokens": 100,
                "output_tokens": 80,
                "reasoning_output_tokens": 0,
                "total_tokens": 980,
            }
        },
        "model": "gpt-5.5",
    },
}


def test_codex_parent_adoption_reads_only_the_first_session_meta(tmp_path) -> None:
    """8 of 26 real rollouts carry multiple session_meta lines (resume
    re-emits); a LATER meta describing another thread must never donate a
    parent to this rollout's own (parentless) first meta."""

    rollout = tmp_path / "rollout-multi-meta.jsonl"
    own_meta = {"type": "session_meta", "payload": {"id": "thread-own", "thread_source": "user"}}
    foreign_meta = {
        "type": "session_meta",
        "payload": {"id": "thread-other", "parent_thread_id": "thread-foreign-root"},
    }
    rollout.write_text(
        "\n".join(json.dumps(line) for line in (own_meta, foreign_meta, _USAGE_LINE)) + "\n",
        encoding="utf-8",
    )

    usage = _read_codex_rollout_usage(rollout)

    assert usage is not None
    assert "session_meta_parent_thread_id" not in usage


def test_codex_parent_from_first_session_meta_still_adopted(tmp_path) -> None:
    rollout = tmp_path / "rollout-first-meta-parent.jsonl"
    own_meta = {
        "type": "session_meta",
        "payload": {"id": "thread-own", "parent_thread_id": "thread-root", "thread_source": "subagent"},
    }
    later_meta = {"type": "session_meta", "payload": {"id": "thread-own", "parent_thread_id": "thread-WRONG"}}
    rollout.write_text(
        "\n".join(json.dumps(line) for line in (own_meta, later_meta, _USAGE_LINE)) + "\n",
        encoding="utf-8",
    )

    usage = _read_codex_rollout_usage(rollout)

    assert usage is not None
    assert usage["session_meta_parent_thread_id"] == "thread-root"


def test_codex_rollout_survives_valid_non_dict_json_lines(tmp_path) -> None:
    """Pre-existing crash: a rollout line holding valid non-dict JSON (torn
    partial write) raised AttributeError and took down the whole import."""

    rollout = tmp_path / "rollout-torn-lines.jsonl"
    scalar_lines = ["123", "[1, 2]", '"str"', "null", "true"]
    rollout.write_text(
        "\n".join(scalar_lines + [json.dumps(_USAGE_LINE)]) + "\n",
        encoding="utf-8",
    )

    usage = _read_codex_rollout_usage(rollout)

    assert usage is not None
    assert usage["total_tokens"] == 980


# ---------------------------------------------------------------------------
# Minor 4 — worktree chip title names the OWNER label, not "this repository"
# ---------------------------------------------------------------------------


def test_worktree_chip_title_names_the_owner_label(tmp_path) -> None:
    store_root = _project_store(tmp_path)
    _record_usage(
        store_root,
        _usage_event(session="wt-session", project_dir="/Users/owner/otherrepo/.claude/worktrees/musing-x"),
    )

    html = _sessions_html(store_root)

    assert "temporary Claude worktree of otherrepo" in html
    assert "worktree of this repository" not in html


# ---------------------------------------------------------------------------
# Minor 5 — backslashes are separators only for Windows-shaped paths
# ---------------------------------------------------------------------------


def test_posix_names_with_literal_backslashes_never_remap() -> None:
    evil = "/Users/owner/dir\\.claude\\worktrees\\evil"
    assert claude_worktree_owner_path_text(evil) is None
    label, source = _project_label_info(evil)
    # No false worktree marker, no label for a directory that does not exist.
    assert source is None
    assert label != "dir"


def test_windows_shaped_paths_still_remap() -> None:
    assert (
        claude_worktree_owner_path_text("C:\\Users\\x\\repo\\.claude\\worktrees\\wt") == "C:/Users/x/repo"
    )
    assert (
        claude_worktree_owner_path_text("\\\\host\\share\\repo\\.claude\\worktrees\\wt") == "//host/share/repo"
    )
    # Plain POSIX worktree paths are untouched by the gate.
    assert (
        claude_worktree_owner_path_text("/Users/owner/repo/.claude/worktrees/wt") == "/Users/owner/repo"
    )


# ---------------------------------------------------------------------------
# Minors 6 + 11 — majority LABEL first, then majority source; ties keep marker
# ---------------------------------------------------------------------------


def test_majority_label_wins_even_when_its_sources_split(tmp_path) -> None:
    """repo-b has 3 votes (2 worktree + 1 plain) vs repo-a's 2 plain votes:
    the old (label, source) pair counting elected repo-a."""

    store_root = _project_store(tmp_path)
    rows = [
        ("/Users/owner/repo-a", "model:m1"),
        ("/Users/owner/repo-a", "model:m2"),
        ("/Users/owner/repo-b/.claude/worktrees/wt-1", "model:m3"),
        ("/Users/owner/repo-b/.claude/worktrees/wt-2", "model:m4"),
        ("/Users/owner/repo-b", "model:m5"),
    ]
    for project_dir, lane in rows:
        _record_usage(store_root, _usage_event(session="mixed-session", project_dir=project_dir, lane=lane))

    ledger = build_work_ledger(SentinelService(store_root).list_all_events())

    entry = ledger["session_rollup"]["sessions"][0]
    assert entry["project"] == "repo-b"
    # Within repo-b: 2 worktree votes vs 1 plain — the marker stays.
    assert entry["project_source"] == "claude_worktree"


def test_source_tie_keeps_the_worktree_marker(tmp_path) -> None:
    store_root = _project_store(tmp_path)
    _record_usage(store_root, _usage_event(session="tie-session", project_dir="/Users/owner/tierepo", lane="model:m1"))
    _record_usage(
        store_root,
        _usage_event(
            session="tie-session", project_dir="/Users/owner/tierepo/.claude/worktrees/wt", lane="model:m2"
        ),
    )

    ledger = build_work_ledger(SentinelService(store_root).list_all_events())

    entry = ledger["session_rollup"]["sessions"][0]
    assert entry["project"] == "tierepo"
    assert entry["project_source"] == "claude_worktree"


def test_plain_majority_within_label_drops_the_marker(tmp_path) -> None:
    store_root = _project_store(tmp_path)
    _record_usage(store_root, _usage_event(session="plain-session", project_dir="/Users/owner/tierepo", lane="model:m1"))
    _record_usage(store_root, _usage_event(session="plain-session", project_dir="/Users/owner/tierepo", lane="model:m2"))
    _record_usage(
        store_root,
        _usage_event(
            session="plain-session", project_dir="/Users/owner/tierepo/.claude/worktrees/wt", lane="model:m3"
        ),
    )

    ledger = build_work_ledger(SentinelService(store_root).list_all_events())

    entry = ledger["session_rollup"]["sessions"][0]
    assert entry["project"] == "tierepo"
    assert entry["project_source"] is None
