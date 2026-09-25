"""The agent's own account on the Receipt: goal, progress and next step.

It is reduced from the Task's steps with one policy per field. The first goal
wins, so a late subagent errand cannot become the Task's purpose. Progress is
the newest account from the root session(s). Before agents wrote progress
notes, the newest closed step's summary or blocker stands in and says so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agentacct.api import create_local_api_app
from agentacct.receipt import AGENT_REPORT_STALE_AFTER_SECONDS, _agent_report
from agentacct.service import SentinelService

TOKEN = "test-agent-report-token"
NS = "sha256:agent-report-ns"


def _item(section_id: str, *, session: str = "root", start: float, end: float | None = None, **fields: Any) -> dict[str, Any]:
    return {
        "section_id": section_id,
        "title": fields.pop("title", f"Step {section_id}"),
        "client_session_id": session,
        "started_at": start,
        "updated_at": end if end is not None else start,
        **fields,
    }


def _task(*items: dict[str, Any], last_activity_at: float | None = None) -> dict[str, Any]:
    return {
        "primary_root": {"client": "claude-code", "client_session_id": "root"},
        "root_keys": [{"client": "claude-code", "client_session_id": "root"}],
        "work_items": list(items),
        "last_activity_at": last_activity_at if last_activity_at is not None else max(i["updated_at"] for i in items),
    }


def test_no_steps_means_no_account() -> None:
    assert _agent_report({"work_items": []}) is None


def test_the_first_goal_wins_and_progress_is_the_newest_note() -> None:
    report = _agent_report(
        _task(
            _item("a", start=10, goal="Stop duplicate checkout charges", latest_status="completed",
                  progress="Found the retry that mints a new id. Next: reuse it."),
            _item("b", start=20, goal="A later goal", latest_status="completed",
                  progress="Retries reuse the first charge id. Done."),
        )
    )
    assert report is not None
    assert report["goal"] == {"text": "Stop duplicate checkout charges", "source": "goal", "section_id": "a"}
    assert report["progress"]["text"] == "Retries reuse the first charge id. Done."
    assert report["progress"]["source"] == "progress"
    assert report["progress"]["section_id"] == "b"


def test_a_subagent_note_never_becomes_the_task_account() -> None:
    report = _agent_report(
        _task(
            _item("root-step", start=10, latest_status="completed",
                  progress="Planned the checkout fix. Next: hand the search to a helper."),
            _item("errand", session="subagent", start=30, goal="Grep for charge ids",
                  latest_status="completed", progress="Listed every charge-id call site. Done."),
        )
    )
    assert report is not None
    assert report["progress"]["section_id"] == "root-step"
    # The subagent's goal is not the Task's either; the root's first title is.
    assert report["goal"] == {"text": "Step root-step", "source": "step_title", "section_id": "root-step"}


def test_only_subagent_steps_still_produce_an_account() -> None:
    report = _agent_report(
        _task(_item("errand", session="subagent", start=5, latest_status="completed", summary="Listed call sites."))
    )
    assert report is not None and report["progress"]["section_id"] == "errand"


def test_before_progress_notes_a_closed_step_summary_stands_in_with_its_lead() -> None:
    report = _agent_report(
        _task(
            _item("open", start=5, latest_status="started"),
            _item("done", start=10, end=40, latest_status="completed", title="Fix the rounding",
                  summary="Rounded each line before summing.\n- Changed: total.py\n- Verified: 14 passed",
                  next_step="Cover refunds."),
        )
    )
    assert report is not None
    progress = report["progress"]
    assert progress["source"] == "step_summary"
    assert progress["lead"] == "Rounded each line before summing."
    assert progress["text"].startswith("Rounded each line") and "14 passed" in progress["text"]
    assert progress["step_title"] == "Fix the rounding"
    assert progress["written_at"] == 40
    assert report["next_step"] == "Cover refunds."
    # The goal falls back to how the work began: the first step's title.
    assert report["goal"]["source"] == "step_title" and report["goal"]["text"] == "Step open"


def test_a_blocked_step_reports_its_blocker() -> None:
    report = _agent_report(
        _task(_item("b", start=10, latest_status="blocked", blocker="The staging key has expired.", summary=None))
    )
    assert report is not None
    assert report["progress"] == {
        "text": "The staging key has expired.",
        "source": "blocker",
        "lead": "The staging key has expired.",
        "section_id": "b",
        "step_title": "Step b",
        "step_status": "blocked",
        "written_at": 10,
    }


def test_open_steps_without_notes_offer_only_a_goal() -> None:
    report = _agent_report(_task(_item("open", start=5, latest_status="started", summary="half-written")))
    assert report is not None
    assert report["progress"] is None
    assert report["goal"]["text"] == "Step open"


def test_work_after_the_account_is_flagged_without_hiding_it() -> None:
    written = 1_000.0
    fresh = _agent_report(
        _task(_item("a", start=written, latest_status="completed", progress="Shipped the fix. Done."),
              last_activity_at=written + AGENT_REPORT_STALE_AFTER_SECONDS)
    )
    stale = _agent_report(
        _task(_item("a", start=written, latest_status="completed", progress="Shipped the fix. Done."),
              last_activity_at=written + AGENT_REPORT_STALE_AFTER_SECONDS + 1)
    )
    assert fresh is not None and fresh["activity_after_report"] is False
    assert stale is not None and stale["activity_after_report"] is True
    assert stale["progress"]["text"] == "Shipped the fix. Done."


# --- end to end through /v1/receipt ------------------------------------------


def _record(service: SentinelService, event_id: str, event_type: str, at: float, metadata: dict[str, Any], **extra: Any) -> None:
    service.record_event(
        {"event_id": event_id, "created_at": at, "source": "claude-code", "event_type": event_type, "run_id": None,
         "metadata": metadata, **extra},
        trusted_usage_import=event_type == "model_usage",
    )


def test_receipt_carries_the_agent_account_beside_the_counted_outcome(tmp_path: Path) -> None:
    service = SentinelService(tmp_path)
    scope = {
        "client": "claude-code", "client_session_id": "s1", "project_dir": "/tmp/project",
        "session_namespace_fingerprint": NS, "identity_scope_state": "explicit",
    }
    _record(service, "evt_usage", "model_usage", 100.0,
            {**scope, "usage_source": "local_client_session_store",
             "usage_provenance": "agent_sentinel_local_usage_import", "started_at": 100.0, "updated_at": 100.0,
             "source_namespace_fingerprint": NS},
            provider="claude-code", model="claude-opus-4-8", estimated_input_tokens=100,
            estimated_output_tokens=25, estimated_cost_usd=0.5, usage_confidence="client_reported",
            cost_confidence="estimated_from_tokens", cost_basis="pricing_table")
    section = {**scope, "sentinel_semantic_kind": "section", "client_context_keys_authored": ["client_session_id"],
               "section_id": "fix", "section_title": "Fix duplicate charges", "kind": "implementation"}
    _record(service, "evt_start", "section_started", 101.0,
            {**section, "section_status": "started", "goal": "Stop duplicate checkout charges"})
    _record(service, "evt_done", "section_completed", 102.0,
            {**section, "section_status": "completed",
             "summary": "Retries reuse the first charge id; two tests cover it.",
             "progress": "Retries now reuse the first charge. Stopped before refunds; next: cover them."})

    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    headers = {"Authorization": f"Bearer {TOKEN}"}
    task_id = client.get("/v1/tasks", headers=headers).json()["tasks"][0]["task_id"]
    outcome = client.get(f"/v1/receipt?task={task_id}", headers=headers).json()["dimensions"]["outcome"]
    report = outcome["agent_report"]
    assert report["goal"]["text"] == "Stop duplicate checkout charges"
    assert report["progress"]["source"] == "progress"
    assert report["progress"]["text"].endswith("next: cover them.")
    # The account never moves the counted decision: it stays agent-reported.
    assert outcome["asserted_by"] != "machine"
