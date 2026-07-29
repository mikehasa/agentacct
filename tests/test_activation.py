"""Tests for the activation readiness projection and install-boundary store.

The domain model -- the readiness funnel, the input vocabulary, and the
returned "progress" checklist -- lives at the source in
``build_activation_snapshot``; read that docstring first. This file does not
restate it.

What's covered here
-------------------
* build_activation_snapshot -- one test per funnel stage (all six stages).
* ActivationStateStore -- the owner-only install-boundary record: marking,
  client dedup/merge, idempotency, and fail-closed handling of corrupt state.

RuntimeManager (the process supervisor) has its own test file.

How to read these tests
-----------------------
* ``_snapshot(**overrides)`` starts from a fully-ready baseline that lands on
  the final ``waiting_for_task`` stage; each stage test flips the ONE input
  that selects the stage under test.
* Tests index the returned ``progress`` checklist; the index map
  (client / usage / recording / sync / task) sits in a comment just above the
  stage tests.
"""

from __future__ import annotations

import stat
import time

import pytest
from fastapi.testclient import TestClient

from agentacct.activation import (
    ActivationStateError,
    ActivationStateStore,
    build_activation_snapshot,
)
from agentacct.api import create_local_api_app
from agentacct.ingestion_health import IngestionHealthStore
from agentacct.service import SentinelService


def _runtime(state: str = "running") -> dict[str, object]:
    """Minimal runtime-status dict accepted by ``build_activation_snapshot``."""
    return {"state": state, "dashboard_url": "http://127.0.0.1:8765/"}


def _snapshot(**overrides: object) -> dict[str, object]:
    """Call ``build_activation_snapshot`` from a fully-ready baseline.

    The defaults describe a project that has cleared every gate EXCEPT having a
    task -- so ``_snapshot()`` with no overrides lands on the final
    ``waiting_for_task`` stage. Each stage test then flips the ONE input that
    selects the stage under test, so the override is exactly the behavior
    being asserted.
    """
    defaults: dict[str, object] = {
        "source_rows": [{"client": "codex", "status": "found"}],  # a detected client
        "ingestion_health": {"watcher": {"state": "running"}},    # background sync is up
        "task_projection": {"tasks": []},                         # no tasks yet
        "runtime_status": _runtime("running"),                    # dashboard process is up
        "recording_configured": True,                             # MCP/hooks installed
        # NOTE: new_session_required defaults to False inside build_activation_snapshot.
    }
    defaults.update(overrides)
    return build_activation_snapshot(**defaults)


def _usage(service: SentinelService, *, session: str, started_at: float) -> None:
    """Record one synthetic codex usage row -- i.e. simulate one agent 'chat'.

    The row mimics what the local-usage importer writes when it reads a codex
    session, so the service sees a realistic root session of a known client.
    """
    service.record_event(
        {
            "source": "codex-local-session-import",
            "event_type": "model_usage",
            "provider": "codex",
            "model": "gpt-test",
            "estimated_input_tokens": 10,
            "estimated_output_tokens": 2,
            "usage_confidence": "client_reported",
            "cost_confidence": "unknown",
            "metadata": {
                "usage_source": "local_client_session_store",
                "client": "codex",
                "client_session_id": session,
                "client_session_kind": "root",
                "cache_creation_tokens_reported": False,
                "cache_read_tokens_reported": True,
                "started_at": started_at,
                "updated_at": started_at,
            },
        },
        trusted_usage_import=True,
    )


# The "progress" checklist returned by build_activation_snapshot is an ordered
# list of steps, one per readiness dimension. Tests index it; the order is:
#   [0] client    - "Agent detected"
#   [1] usage     - "Usage source"
#   [2] recording - "Work recording"
#   [3] sync      - "Continuous sync"
#   [4] task      - "First real Task"   (also progress[-1])


# ---------------------------------------------------------------------------
# build_activation_snapshot: each readiness stage
# ---------------------------------------------------------------------------


def test_no_client_source_suggests_connecting_one() -> None:
    """Funnel stage 1: no coding-agent source detected -> tell the user to connect one.

    Significance: this is the FIRST gate, so it must win regardless of the other
    (ready-by-default) inputs -- nothing else matters until agentacct can read
    at least one client's logs.
    """
    payload = _snapshot(source_rows=[])  # wipe the default codex source
    # The stage is the highest-priority unmet gate.
    assert payload["stage"] == "client_needed"
    # The headline is the H1 text the dashboard renders for this stage.
    assert payload["headline"] == "Connect a coding-agent source"
    # "ready" means "a real Task is captured" -- never true at the first stage.
    assert payload["ready"] is False
    # The single action button's shell command; the UI offers to run it.
    assert payload["primary_action"]["command"] == "agentacct usage discover-sources"


def test_source_without_work_recording_suggests_finishing_setup() -> None:
    """Funnel stage 2: a source is detected, but work recording isn't set up yet.

    Significance: agentacct can read usage without recording work, but it must
    NOT claim "ready" until the MCP/hooks that capture semantic work are
    installed. So this gate sits strictly between source detection and runtime.
    """
    payload = _snapshot(recording_configured=False)  # recording hooks NOT installed
    assert payload["stage"] == "configuration_needed"
    # The action sends the user to onboard, which installs the recording hooks.
    assert payload["primary_action"]["command"] == "agentacct onboard"


def test_recording_configured_but_runtime_stopped_suggests_starting() -> None:
    """Funnel stage 3: everything is configured, but the dashboard/sync are stopped.

    Significance: the background processes (dashboard server + usage watcher)
    are what surface the guide and keep usage fresh. If they're off, the next
    action is to start them -- not to wait for a task.
    """
    payload = _snapshot(runtime_status=_runtime("stopped"))  # dashboard process down
    assert payload["stage"] == "runtime_needed"
    assert payload["primary_action"]["command"] == "agentacct start"


def test_ready_but_new_session_required_asks_for_a_new_chat() -> None:
    """Funnel stage 4: fully running, but a fresh chat is needed for hooks to attach.

    Significance: MCP servers and hooks load when an agent SESSION starts. A
    chat that began BEFORE agentacct was configured will never record work, so
    the user must open a NEW chat. The progress checklist reflects this by
    marking the "recording" step as restart_required.
    """
    payload = _snapshot(new_session_required=True)
    assert payload["stage"] == "new_session_needed"
    # No shell command here -- the user's action is inside their agent client.
    assert payload["primary_action"]["label"] == "Open a new agent chat"
    # progress[2] is the "recording" checklist step (see the index map above).
    assert payload["progress"][2]["state"] == "restart_required"


def test_usage_only_task_is_active_but_labeled_usage_only() -> None:
    """Funnel stage 5 (active): a task with usage but NO work items.

    Significance: a usage-only task is real (it has tokens) but shallow (no
    semantic work recorded over MCP). The dashboard marks it "active" so the
    user sees progress, yet honestly labels it "usage/activity only" rather
    than implying richer evidence was captured.
    """
    # A task with tokens but an empty work_items list (no MCP sections/checks).
    payload = _snapshot(task_projection={"tasks": [{"task_id": "t1", "work_items": []}]})
    assert payload["stage"] == "active"
    assert payload["ready"] is True
    # progress[-1] is the "task" step -- the last item in the checklist.
    assert payload["progress"][-1]["state"] == "captured"
    # The honest detail: captured, but only at the usage/activity level.
    assert payload["progress"][-1]["detail"] == "usage/activity only"


def test_task_with_work_items_records_semantic_context() -> None:
    """Funnel stage 5 (active): a task that DOES carry semantic work items.

    Significance: when work_items is non-empty, the task has MCP-recorded
    semantic context (sections/checks), so the detail upgrades from
    "usage/activity only" to "semantic context recorded".
    """
    payload = _snapshot(
        task_projection={"tasks": [{"task_id": "t1", "work_items": [{"status": "started"}]}]}
    )
    assert payload["ready"] is True
    assert payload["progress"][-1]["detail"] == "semantic context recorded"


def test_waiting_for_task_copy_does_not_overstate_client_support() -> None:
    """Funnel stage 6: everything ready, no task yet -- honest "waiting" copy.

    Significance: the waiting-stage text must say a *recognized* session is
    needed and must NOT promise a *supported* session, because not every
    detected client can actually record work. This guards against overstating
    which agents agentacct fully supports.
    """
    payload = _snapshot()  # ready baseline, no task yet -> waiting
    assert payload["stage"] == "waiting_for_task"
    assert "recognized session" in payload["primary_action"]["reason"]
    assert "supported session" not in payload["primary_action"]["reason"]


# ---------------------------------------------------------------------------
# ActivationStateStore: install-boundary record
# ---------------------------------------------------------------------------


def test_install_record_is_owner_only_deduped_and_idempotent(tmp_path) -> None:
    """The install record's full lifecycle: persist, dedup, secure, re-runnable.

    Significance: this record marks WHEN agentacct finished configuring a
    project, so the dashboard can tell pre-existing chats from the first
    agentacct-aware one. It must be owner-only (privacy), de-duplicate clients,
    and be safe to re-run (onboarding may be invoked more than once).
    """
    store = ActivationStateStore(tmp_path / "state")

    # Before any configuration: nothing on disk, and snapshot reads as None.
    assert store.snapshot() is None
    assert not store.root.exists()

    # Configure with duplicates and unsorted clients to exercise normalization.
    written = store.mark_configured(
        project_dir=tmp_path,
        clients=["codex", "codex", "claude-code"],  # duplicates + out of order
        configured_at=123.5,
    )
    assert written["clients"] == ["claude-code", "codex"]  # deduped + sorted
    assert store.snapshot() == written
    # Privacy: both the directory and the file are owner-only.
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700        # dir:  owner rwx
    assert stat.S_IMODE(store.state_path.stat().st_mode) == 0o600  # file: owner rw-

    # Re-running onboarding for the same project must NOT reset it: new clients
    # merge in and the original configured_at timestamp is preserved.
    repeated = store.mark_configured(
        project_dir=tmp_path,
        clients=["codex"],      # already present
        configured_at=999.0,    # later timestamp -- must be ignored
    )
    assert repeated["configured_at"] == 123.5             # original timestamp kept
    assert repeated["clients"] == ["claude-code", "codex"]  # unchanged (codex already in)


def test_mark_configured_requires_at_least_one_client(tmp_path) -> None:
    """An empty client list is rejected -- the boundary must name a real client.

    Significance: a configured-but-clientless record is meaningless and would
    mislead the dashboard into thinking onboarding completed. Reject it loudly
    instead of persisting a vacuous record.
    """
    store = ActivationStateStore(tmp_path / "state")
    with pytest.raises(ActivationStateError, match="at least one configured client"):
        store.mark_configured(project_dir=tmp_path, clients=[])


def test_corrupt_install_record_fails_closed_with_an_issue(tmp_path) -> None:
    """A corrupt record is reported as an issue, never returned as trusted data.

    Significance: if the on-disk record is unreadable (disk corruption, a
    partial write, a manual edit), the store must NOT guess or return stale
    data. It surfaces a stable "issue" code so callers can fail closed and
    prompt repair.
    """
    store = ActivationStateStore(tmp_path / "state")
    # Simulate corruption: hand-write an invalid-JSON record behind the store's back.
    store.root.mkdir(parents=True)
    store.state_path.write_text("{not valid json", encoding="utf-8")

    snapshot = store.snapshot()
    assert snapshot is not None                  # a record IS returned...
    assert "issue" in snapshot                   # ...but as an issue marker, not data.
    assert snapshot["issue"].startswith("activation_state_corrupt:")


# ---------------------------------------------------------------------------
# Dashboard integration: the readiness funnel end to end
# ---------------------------------------------------------------------------


def test_empty_store_dashboard_prompts_connecting_a_client(tmp_path) -> None:
    """End-to-end: a brand-new store's dashboard shows exactly one action.

    Significance: this exercises the real API + HTML render (not just the
    projection function), confirming the stage from stage-1 actually reaches
    the UI -- one action card, the privacy-promise copy, and no provider-key ask.
    """
    # Build the real dashboard app against a throwaway empty store.
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))

    payload = client.get("/api/activation").json()  # machine-readable stage
    page = client.get("/").text                     # rendered HTML overview

    assert payload["stage"] == "client_needed"
    assert payload["primary_action"]["command"] == "agentacct usage discover-sources"
    # The overview renders the activation card with exactly one action button...
    assert 'class="activation-card"' in page
    assert "Get to your first real Task" in page
    assert page.count('class="activation-action"') == 1
    # ...and restates the no-provider-keys privacy promise.
    assert "It does not request provider API keys." in page


def test_only_a_post_configuration_chat_completes_activation(tmp_path) -> None:
    """End-to-end: a chat started BEFORE config never counts; one AFTER does.

    Significance: this is the core activation contract -- agentacct only becomes
    "active" when a real agent chat begins AFTER onboarding, because hooks
    attach at session start. A pre-existing chat must NOT flip the dashboard to
    active on its own.
    """
    store_root = tmp_path / "state"
    service = SentinelService(store_root)

    # (1) A chat that existed BEFORE configuration -- it must not satisfy activation.
    _usage(service, session="existing-chat", started_at=100.0)
    # (2) Now configure the project (timestamp 200, after the pre-existing chat).
    ActivationStateStore(store_root).mark_configured(
        project_dir=tmp_path,
        clients=["codex"],
        configured_at=200.0,
    )
    # (3) Stand up the watcher so "continuous sync" is considered running.
    health = IngestionHealthStore(store_root)
    health.acquire_watcher(
        lease_id="activation-watch",
        pid=4242,
        importer_version="integration-test",
        interval_seconds=60,
        scan_limit=20,
        sources=("codex",),
        now=time.time(),
    )
    client = TestClient(create_local_api_app(store_dir=store_root))

    # Before any new chat: not yet active -- waiting for a post-configuration session.
    before = client.get("/api/activation").json()
    assert before["stage"] == "new_session_needed"
    assert before["task_count"] == 0  # the pre-existing chat did NOT count

    # (4) A NEW chat, started AFTER configuration (timestamp 300).
    _usage(service, session="new-chat", started_at=300.0)
    after = client.get("/api/activation").json()
    page = client.get("/").text

    # Now activation completes: the new chat is recognized as a real task...
    assert after["stage"] == "active"
    assert after["ready"] is True
    assert after["task_count"] == 1  # ...exactly one task (the new chat)
    # ...and the first-run card disappears from the overview.
    assert 'class="activation-card"' not in page
