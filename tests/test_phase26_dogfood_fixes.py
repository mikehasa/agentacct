"""Phase 2.6 dogfood fixes: worktree project labels, codex internal-session
nesting via rollout session_meta parents, cross-store context-scope honesty,
and the global-install CLI capability (hook --store-dir / user settings).

All stores/homes are throwaway tmp_path fixtures (suite conftest guards the
real dogfood ledger). Rendered-HTML assertions cover what the owner actually
saw break; data-level assertions pin the ledger/importer contracts.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentacct.api import _store_scope_and_label, create_local_api_app
from agentacct.client_usage import ClientUsageEvent, discover_codex_usage, discover_hermes_usage
from agentacct.hooks import (
    derive_claude_code_client_context,
    install_claude_code_hook,
    render_claude_hook_wrapper,
    wrapper_executable_candidates,
)
from agentacct.service import SentinelService
from agentacct.store_resolution import claude_worktree_owner_path_text
from agentacct.work_ledger import build_work_ledger

WORKTREE_CWD = "/Users/owner/agent-chronicle-github-clean/.claude/worktrees/great-tesla-9cc8ad"
OWNER_LABEL = "agent-chronicle-github-clean"


def _historical_usage_event(
    *,
    session: str,
    project_dir: str | None,
    client: str = "claude-code",
    input_tokens: int = 100,
    output_tokens: int = 25,
    session_kind: str = "root",
    parent_session: str | None = None,
    lane: str | None = "model:claude-fable-5",
) -> dict:
    """Exact shape of a pre-2.6 row in the real store (verified read-only)."""

    metadata = {
        "usage_source": "local_client_session_store",
        "usage_update_semantics": "claude_assistant_message_usage_rows",
        "client": client,
        "client_session_id": session,
        "client_session_kind": session_kind,
        "parent_client_session_id": parent_session,
        "client_transcript_id": session,
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
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "estimated_cost_usd": None,
        "usage_confidence": "client_reported",
        "cost_confidence": "unknown",
        "cost_basis": "client_session_totals",
        "metadata": metadata,
    }


def _record_usage(store_root: Path, event: dict) -> dict:
    return SentinelService(store_root).record_event(event, trusted_usage_import=True)


def _record_section(store_root: Path, *, section_id: str, session: str, client: str = "claude-code") -> dict:
    return SentinelService(store_root).record_event(
        {
            "source": client,
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": section_id,
                "section_status": "completed",
                "section_title": f"Section {section_id}",
                "client": client,
                "client_session_id": session,
            },
        }
    )


def _product_html(store_root: Path) -> str:
    # Phase 3.5a route split: session rows live on the /sessions page
    # (Accept-negotiated HTML; the bare path stays the JSON rollup).
    # days=all: these fixtures pin historical timestamps, which the
    # explorer's default 30-day last-activity window (Phase 3.5c) excludes.
    client = TestClient(create_local_api_app(store_dir=store_root))
    response = client.get("/sessions?days=all", headers={"Accept": "text/html"})
    assert response.status_code == 200
    return response.text


# ---------------------------------------------------------------------------
# Fix 1 — worktree project labels (read-time remap + complements)
# ---------------------------------------------------------------------------


def test_worktree_owner_path_text_is_a_pure_string_parse() -> None:
    assert claude_worktree_owner_path_text(WORKTREE_CWD) == "/Users/owner/agent-chronicle-github-clean"
    assert claude_worktree_owner_path_text(WORKTREE_CWD + "/src/deep") == "/Users/owner/agent-chronicle-github-clean"
    assert claude_worktree_owner_path_text("/Users/owner/plain-repo") is None
    # No owner prefix -> no remap; bare worktrees dir (no name) -> no remap.
    assert claude_worktree_owner_path_text(".claude/worktrees/x") is None
    assert claude_worktree_owner_path_text("/repo/.claude/worktrees") is None


def test_historical_worktree_row_labels_as_owner_repo_in_ledger(tmp_path) -> None:
    store_root = tmp_path / "state"
    _record_usage(store_root, _historical_usage_event(session="wt-session", project_dir=WORKTREE_CWD))

    ledger = build_work_ledger(SentinelService(store_root).list_all_events())

    row = ledger["usage_events"][0]
    assert row["project_dir"] == OWNER_LABEL
    assert row["project_source"] == "claude_worktree"
    entry = ledger["session_rollup"]["sessions"][0]
    assert entry["project"] == OWNER_LABEL
    assert entry["project_source"] == "claude_worktree"
    # The meaningless worktree folder name never leaks into any derived label.
    assert "great-tesla-9cc8ad" not in json.dumps(ledger["session_rollup"])


def test_non_worktree_row_keeps_plain_label_and_no_source(tmp_path) -> None:
    store_root = tmp_path / "state"
    _record_usage(store_root, _historical_usage_event(session="plain-session", project_dir="/Users/owner/plain-repo"))

    ledger = build_work_ledger(SentinelService(store_root).list_all_events())

    row = ledger["usage_events"][0]
    assert row["project_dir"] == "plain-repo"
    assert row["project_source"] is None
    entry = ledger["session_rollup"]["sessions"][0]
    assert entry["project_source"] is None


def test_worktree_row_renders_owner_label_with_worktree_chip(tmp_path) -> None:
    store_root = tmp_path / "state"
    _record_usage(store_root, _historical_usage_event(session="wt-session", project_dir=WORKTREE_CWD))

    html = _product_html(store_root)

    assert OWNER_LABEL in html
    assert ">worktree</span>" in html
    assert "great-tesla-9cc8ad" not in html


def test_plain_row_renders_no_worktree_chip(tmp_path) -> None:
    store_root = tmp_path / "state"
    _record_usage(store_root, _historical_usage_event(session="plain-session", project_dir="/Users/owner/plain-repo"))

    html = _product_html(store_root)

    assert "plain-repo" in html
    assert ">worktree</span>" not in html


def test_sessions_json_keeps_clean_owner_name_plus_project_source(tmp_path) -> None:
    store_root = tmp_path / "state"
    _record_usage(store_root, _historical_usage_event(session="wt-session", project_dir=WORKTREE_CWD))

    client = TestClient(create_local_api_app(store_dir=store_root))
    payload = client.get("/sessions").json()

    entry = payload["sessions"][0]
    assert entry["project"] == OWNER_LABEL
    assert entry["project_source"] == "claude_worktree"


def test_hook_capture_remaps_worktree_cwd_to_owner_basename() -> None:
    context = derive_claude_code_client_context(
        {
            "session_id": "hook-session",
            "transcript_path": "/Users/owner/.claude/projects/x/hook-session.jsonl",
            "cwd": WORKTREE_CWD,
            "hook_event_name": "PreToolUse",
        }
    )
    assert context is not None
    assert context["project_label"] == OWNER_LABEL

    plain = derive_claude_code_client_context(
        {"session_id": "hook-session", "cwd": "/Users/owner/plain-repo", "hook_event_name": "PreToolUse"}
    )
    assert plain is not None
    assert plain["project_label"] == "plain-repo"


def test_import_time_complement_adds_project_owner_dir_for_worktree_cwd(tmp_path) -> None:
    worktree_event = ClientUsageEvent(
        client="claude-code",
        client_session_id="s1",
        source_path=tmp_path / "s1.jsonl",
        title=None,
        cwd=WORKTREE_CWD,
        model="claude-fable-5",
        input_tokens=10,
        output_tokens=5,
    ).to_sentinel_event()
    assert worktree_event["metadata"]["project_owner_dir"] == "/Users/owner/agent-chronicle-github-clean"
    # project_dir stays the verbatim cwd — raw truth is never rewritten.
    assert worktree_event["metadata"]["project_dir"] == WORKTREE_CWD

    plain_event = ClientUsageEvent(
        client="claude-code",
        client_session_id="s2",
        source_path=tmp_path / "s2.jsonl",
        title=None,
        cwd="/Users/owner/plain-repo",
        model="claude-fable-5",
        input_tokens=10,
        output_tokens=5,
    ).to_sentinel_event()
    assert "project_owner_dir" not in plain_event["metadata"]


def _make_hermes_home(root: Path, *, with_cwd_column: bool) -> Path:
    hermes_home = root / "hermes-home"
    hermes_home.mkdir()
    con = sqlite3.connect(hermes_home / "state.db")
    try:
        cwd_column = "cwd text," if with_cwd_column else ""
        con.execute(
            f"""
            create table sessions (
                id text primary key,
                source text not null,
                model text,
                title text,
                {cwd_column}
                started_at real not null,
                message_count integer default 0,
                input_tokens integer default 0,
                output_tokens integer default 0,
                cache_read_tokens integer default 0,
                cache_write_tokens integer default 0,
                reasoning_tokens integer default 0,
                billing_provider text,
                estimated_cost_usd real,
                actual_cost_usd real
            )
            """
        )
        columns = ["id", "source", "model", "title"]
        values: list[object] = ["hermes-session-1", "cli", "claude-sonnet-4-20250514", "secret short title"]
        if with_cwd_column:
            columns.append("cwd")
            values.append("/Users/owner/hermes-project")
        columns += ["started_at", "message_count", "input_tokens", "output_tokens", "billing_provider", "actual_cost_usd"]
        values += [1_750_000_000.25, 4, 1200, 300, "anthropic", 0.34]
        con.execute(
            f"insert into sessions ({', '.join(columns)}) values ({', '.join('?' for _ in columns)})",
            values,
        )
        con.commit()
    finally:
        con.close()
    return hermes_home


def test_hermes_importer_reads_cwd_but_never_title(tmp_path) -> None:
    hermes_home = _make_hermes_home(tmp_path, with_cwd_column=True)

    events = discover_hermes_usage(hermes_home=hermes_home, limit_sessions=10)

    assert len(events) == 1
    assert events[0].cwd == "/Users/owner/hermes-project"
    # Privacy line: transcript-derived text (incl. short titles) is never
    # imported, even when the column is sitting right there.
    assert events[0].title is None
    payload = events[0].to_sentinel_event()
    assert payload["metadata"]["project_dir"] == "/Users/owner/hermes-project"
    assert "secret short title" not in json.dumps(payload)


def test_hermes_importer_survives_schema_without_cwd_column(tmp_path) -> None:
    hermes_home = _make_hermes_home(tmp_path, with_cwd_column=False)

    events = discover_hermes_usage(hermes_home=hermes_home, limit_sessions=10)

    assert len(events) == 1
    assert events[0].cwd is None


# ---------------------------------------------------------------------------
# Fix 2 — codex internal sessions nest via rollout session_meta parents
# ---------------------------------------------------------------------------

ROOT_THREAD = "019f3187-aaaa-bbbb-cccc-000000000001"
INTERNAL_THREAD = "019f3207-aaaa-bbbb-cccc-000000000002"


def _write_codex_rollout(path: Path, *, thread_id: str, parent_thread_id: str | None, model: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    session_meta_payload: dict[str, object] = {
        "id": thread_id,
        "session_id": parent_thread_id or thread_id,
        "originator": "Codex Desktop",
        "thread_source": "subagent" if parent_thread_id else "user",
    }
    if parent_thread_id is not None:
        session_meta_payload["parent_thread_id"] = parent_thread_id
    lines = [
        json.dumps({"type": "session_meta", "payload": session_meta_payload}),
        json.dumps(
            {
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
                    "model": model,
                },
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_codex_home_with_internal_thread(
    root: Path, *, internal_parent: str | None = ROOT_THREAD, spawn_edge_parent: str | None = None
) -> Path:
    codex_home = root / "codex-home"
    sessions = codex_home / "sessions" / "2026" / "07" / "05"
    root_rollout = sessions / f"rollout-root-{ROOT_THREAD}.jsonl"
    internal_rollout = sessions / f"rollout-internal-{INTERNAL_THREAD}.jsonl"
    _write_codex_rollout(root_rollout, thread_id=ROOT_THREAD, parent_thread_id=None, model="gpt-5.5")
    _write_codex_rollout(internal_rollout, thread_id=INTERNAL_THREAD, parent_thread_id=internal_parent, model="codex-auto-review")
    con = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        con.execute(
            """
            create table threads (
                id text primary key,
                rollout_path text not null,
                created_at integer not null,
                updated_at integer not null,
                cwd text not null,
                title text not null,
                tokens_used integer not null,
                model text,
                cli_version text,
                thread_source text,
                source text
            )
            """
        )
        con.execute(
            "insert into threads values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ROOT_THREAD, str(root_rollout), 100, 300, "/work/project", "t", 980, "gpt-5.5", "0.test", "user", "{}"),
        )
        con.execute(
            "insert into threads values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                INTERNAL_THREAD,
                str(internal_rollout),
                150,
                250,
                "/work/project",
                "t",
                980,
                "codex-auto-review",
                "0.test",
                "subagent",
                '{"subagent":{"other":"guardian"}}',
            ),
        )
        # The real machine's table exists but is EMPTY unless a test sets an
        # edge — exactly the condition that left internal threads parentless.
        con.execute("create table thread_spawn_edges (parent_thread_id text, child_thread_id text, status text)")
        if spawn_edge_parent is not None:
            con.execute("insert into thread_spawn_edges values (?, ?, ?)", (spawn_edge_parent, INTERNAL_THREAD, "done"))
        con.commit()
    finally:
        con.close()
    return codex_home


def test_codex_internal_thread_gets_parent_from_rollout_session_meta(tmp_path) -> None:
    codex_home = _make_codex_home_with_internal_thread(tmp_path)

    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)

    by_id = {event.client_session_id: event for event in events}
    internal = by_id[INTERNAL_THREAD]
    assert internal.parent_client_session_id == ROOT_THREAD
    # The auto-review chip stays: kind is still "internal", never "child".
    assert internal.client_session_kind == "internal"
    assert by_id[ROOT_THREAD].parent_client_session_id is None
    assert by_id[ROOT_THREAD].client_session_kind == "root"


def test_codex_rollout_self_pointer_is_dropped(tmp_path) -> None:
    codex_home = _make_codex_home_with_internal_thread(tmp_path, internal_parent=INTERNAL_THREAD)

    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)

    internal = {event.client_session_id: event for event in events}[INTERNAL_THREAD]
    # Missing beats wrong: a self-pointer is corrupt linkage, not a parent.
    assert internal.parent_client_session_id is None


def test_codex_spawn_edge_stays_the_primary_parent_source(tmp_path) -> None:
    other_parent = "019f9999-aaaa-bbbb-cccc-000000000009"
    codex_home = _make_codex_home_with_internal_thread(tmp_path, spawn_edge_parent=other_parent)

    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)

    internal = {event.client_session_id: event for event in events}[INTERNAL_THREAD]
    assert internal.parent_client_session_id == other_parent


def test_internal_session_nests_under_root_in_rendered_rollup(tmp_path) -> None:
    store_root = tmp_path / "state"
    _record_usage(
        store_root,
        _historical_usage_event(session=ROOT_THREAD, project_dir="/work/project", client="codex", lane=None),
    )
    internal_row = _historical_usage_event(
        session=INTERNAL_THREAD,
        project_dir="/work/project",
        client="codex",
        session_kind="internal",
        parent_session=ROOT_THREAD,
        lane=None,
    )
    _record_usage(store_root, internal_row)

    html = _product_html(store_root)

    # One top-level row; the internal session folds under its true root as a
    # labeled, never-merged subagent line.
    sessions_html = html[html.index('id="sessions"') : html.index('id="work-items"')]
    assert sessions_html.count('<details class="session-row">') == 1
    assert "1 subagent session(s)" in sessions_html


def test_orphaned_internal_session_renders_auto_review_chip(tmp_path) -> None:
    store_root = tmp_path / "state"
    # No parent recorded (outside the import window): stays top-level with an
    # honest, self-explanatory chip.
    _record_usage(
        store_root,
        _historical_usage_event(
            session=INTERNAL_THREAD, project_dir="/work/project", client="codex", session_kind="internal", lane=None
        ),
    )

    html = _product_html(store_root)

    assert "internal (auto-review) session" in html


# ---------------------------------------------------------------------------
# Fix 3 — cross-store context-scope honesty
# ---------------------------------------------------------------------------


def _dashboard_store(tmp_path) -> Path:
    return tmp_path / "dash-project" / ".agent-sentinel" / "state"


def test_store_scope_and_label_derivation() -> None:
    # Scope is EXPLICIT and structural: only the conventional
    # <project>/.agent-sentinel/state layout is "project"; the documented
    # global store and any custom --store-dir are "custom" (review fix:
    # other_project chips must never fire on custom/global stores).
    assert _store_scope_and_label(Path("/Users/owner/dash-project/.agent-sentinel/state")) == (
        "project",
        "dash-project",
    )
    assert _store_scope_and_label(Path("/Users/owner/.agent-sentinel-global/state")) == ("custom", "state")
    assert _store_scope_and_label(Path("/Users/owner/repo/.claude/worktrees/zippy-x/.agent-sentinel/state")) == (
        "project",
        "repo",
    )
    assert _store_scope_and_label(Path("/Users/owner/sentinel-store")) == ("custom", "sentinel-store")


def test_context_scope_marks_other_project_zero_context_sessions(tmp_path) -> None:
    store_root = _dashboard_store(tmp_path)
    _record_usage(store_root, _historical_usage_event(session="foreign", project_dir="/Users/owner/legacy-project-a"))
    _record_usage(store_root, _historical_usage_event(session="local", project_dir=str(tmp_path / "dash-project")))

    ledger = build_work_ledger(
        SentinelService(store_root).list_all_events(), store_project_label="dash-project", store_scope="project"
    )

    joins = {
        entry["client_session_id"]: entry["join"] for entry in ledger["session_rollup"]["sessions"]
    }
    assert joins["foreign"]["context_scope"] == "other_project"
    assert joins["local"]["context_scope"] == "this_store"


def test_context_scope_stays_this_store_for_sessions_with_sections_here(tmp_path) -> None:
    store_root = _dashboard_store(tmp_path)
    _record_usage(store_root, _historical_usage_event(session="foreign-joined", project_dir="/Users/owner/legacy-project-a"))
    _record_section(store_root, section_id="sec-here", session="foreign-joined")

    ledger = build_work_ledger(
        SentinelService(store_root).list_all_events(), store_project_label="dash-project", store_scope="project"
    )

    entry = ledger["session_rollup"]["sessions"][0]
    # The session HAS context in this store (whatever its join state): the
    # display must never claim its context lives elsewhere.
    assert entry["join"]["context_scope"] == "this_store"


def test_context_scope_defaults_to_this_store_without_a_store_label(tmp_path) -> None:
    store_root = _dashboard_store(tmp_path)
    _record_usage(store_root, _historical_usage_event(session="foreign", project_dir="/Users/owner/legacy-project-a"))

    ledger = build_work_ledger(SentinelService(store_root).list_all_events())

    assert ledger["session_rollup"]["sessions"][0]["join"]["context_scope"] == "this_store"


def test_cross_store_chip_renders_project_name_and_explanation(tmp_path) -> None:
    store_root = _dashboard_store(tmp_path)
    _record_usage(store_root, _historical_usage_event(session="foreign", project_dir="/Users/owner/legacy-project-a"))
    _record_usage(store_root, _historical_usage_event(session="local", project_dir=str(tmp_path / "dash-project")))

    html = _product_html(store_root)

    assert "No MCP context in this store — session ran in legacy-project-a" in html
    assert "Stores are per-project" in html
    # The same-project zero-context row keeps the bare label.
    assert ">No MCP context</span>" in html


def test_sessions_json_carries_context_scope(tmp_path) -> None:
    store_root = _dashboard_store(tmp_path)
    _record_usage(store_root, _historical_usage_event(session="foreign", project_dir="/Users/owner/legacy-project-a"))

    client = TestClient(create_local_api_app(store_dir=store_root))
    payload = client.get("/sessions").json()

    assert payload["sessions"][0]["join"]["context_scope"] == "other_project"


# ---------------------------------------------------------------------------
# Fix 4 — global-install CLI capability (hook store binding + user settings)
# ---------------------------------------------------------------------------


def test_hook_install_embeds_explicit_store_dir_args(tmp_path) -> None:
    store_dir = tmp_path / "global" / "state"
    install = install_claude_code_hook(tmp_path / "global", store_dir=store_dir)

    wrapper_text = install.hook_path.read_text(encoding="utf-8")
    assert f"AGENT_CHRONICLE_HOOK_ARGS = ['--store-dir', '{store_dir}']" in wrapper_text
    # The wrapper still parses and still names a resolvable executable form.
    assert wrapper_executable_candidates(wrapper_text)


def test_hook_install_without_store_dir_keeps_empty_hook_args(tmp_path) -> None:
    install = install_claude_code_hook(tmp_path / "proj")

    wrapper_text = install.hook_path.read_text(encoding="utf-8")
    assert "AGENT_CHRONICLE_HOOK_ARGS = []" in wrapper_text


def test_hook_install_rejects_relative_store_dir(tmp_path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        install_claude_code_hook(tmp_path / "proj", store_dir=Path("relative/state"))


def test_user_settings_example_uses_absolute_wrapper_path(tmp_path) -> None:
    install = install_claude_code_hook(
        tmp_path / "global", store_dir=tmp_path / "global" / "state", user_settings_example=True
    )

    settings = json.loads(install.settings_path.read_text(encoding="utf-8"))
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "$CLAUDE_PROJECT_DIR" not in command
    assert str(install.hook_path) in command
    assert install.hook_path.is_absolute()


def test_default_settings_example_keeps_project_dir_reference(tmp_path) -> None:
    install = install_claude_code_hook(tmp_path / "proj")

    settings = json.loads(install.settings_path.read_text(encoding="utf-8"))
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "$CLAUDE_PROJECT_DIR" in command


def test_render_wrapper_store_dir_roundtrip() -> None:
    text = render_claude_hook_wrapper("/abs/agent-chronicle", store_dir="/abs/global/state")
    assert "AGENT_CHRONICLE_HOOK_ARGS = ['--store-dir', '/abs/global/state']" in text
    # Phase 2.10: one wrapper dispatches both hook events; the store binding
    # rides every subcommand invocation.
    assert 'subcommand, *AGENT_CHRONICLE_HOOK_ARGS' in text
