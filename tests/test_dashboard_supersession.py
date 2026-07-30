"""End-to-end supersession behavior on the served dashboard and MCP writers."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from agentacct.api import create_local_api_app
from agentacct.mcp import SentinelMCPServer
from agentacct.service import SentinelService
from agentacct.work_ledger import build_work_ledger


_FAIL_CMD = "./build.sh && plutil -lint Info.plist && codesign --verify --deep --strict YCCountdown.app"
_PASS_CMD = (
    "./build.sh && plutil -lint YCCountdown.app/Contents/Info.plist "
    "&& codesign --verify --deep --strict --verbose=2 YCCountdown.app"
)


def _section(service: SentinelService, *, section_id: str, status: str, session: str, title: str, **extra) -> None:
    service.record_event(
        {
            "source": "codex",
            "event_type": f"section_{status}",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": section_id,
                "section_status": status,
                "section_title": title,
                "client": "codex",
                "client_session_id": session,
                **extra,
            },
        }
    )


def _check(service: SentinelService, *, section_id: str, session: str, result: str, name: str, command: str, exit_code: int, project: str) -> dict:
    return service.record_event(
        {
            "source": "codex",
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "section_id": section_id,
                "evidence_type": "build",
                "result": result,
                "name": name,
                "command": command,
                "exit_code": exit_code,
                "client": "codex",
                "client_session_id": session,
                "project_dir": project,
                "summary": f"{name}: {result}",
            },
        }
    )


def test_superseded_failure_leaves_needs_attention_but_stays_visible(tmp_path: Path) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    session = "yc-session"
    project = str(tmp_path / "yc")
    _section(service, section_id="yc-build", status="started", session=session, title="Ship YC countdown", project_dir=project)
    _check(service, section_id="yc-build", session=session, result="failed", name="首次应用包构建与严格签名验证", command=_FAIL_CMD, exit_code=1, project=project)
    _check(service, section_id="yc-build", session=session, result="passed", name="应用包构建与签名验证", command=_PASS_CMD, exit_code=0, project=project)
    _section(service, section_id="yc-build", status="completed", session=session, title="Ship YC countdown", project_dir=project)

    html = TestClient(create_local_api_app(store_dir=store_root)).get("/").text

    # Dropped out of "Needs attention": not an open finding, not verified.
    assert '<span class="status status-finding">Open finding</span>' not in html
    assert '<span class="status status-found">Verified</span>' not in html
    # Its own explicit state, counted in its own tile, not folded into Open findings.
    assert "Finding · resolved in a later check" in html
    assert "<strong>0</strong><span>Open findings</span>" in html
    assert "<strong>1</strong><span>Resolved in later check</span>" in html
    # The failed check and its later passing check are rendered adjacently, and
    # the "No follow-up action was recorded" copy is dropped.
    assert "Resolved in a later check" in html
    assert "Later check passed" in html
    assert "No follow-up action was recorded." not in html


def test_unconfirmed_failure_stays_open_with_inline_disclosure(tmp_path: Path) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    session = "market-session"
    project = str(tmp_path / "market")
    _section(service, section_id="market", status="started", session=session, title="Prediction market", project_dir=project)
    _check(service, section_id="market", session=session, result="failed", name="share boundary probe", command="./probe.sh --shares", exit_code=1, project=project)
    _check(service, section_id="market", session=session, result="passed", name="unrelated lint", command="ruff check src", exit_code=0, project=project)
    _section(service, section_id="market", status="completed", session=session, title="Prediction market", project_dir=project)

    html = TestClient(create_local_api_app(store_dir=store_root)).get("/").text

    # A later same-scope pass exists but is not provably the same check: the
    # finding stays open, with the ambiguity disclosed inline.
    assert '<span class="status status-finding">Open finding</span>' in html
    assert "agentacct cannot prove it is the same check" in html
    assert "<strong>1</strong><span>Open findings</span>" in html
    assert "Resolved in later check" not in html


def test_supersedes_check_event_id_requires_a_passing_result(tmp_path: Path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    def call(arguments: dict) -> dict:
        return server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "agentacct_record_machine_check", "arguments": arguments},
            }
        )

    base = {
        "source": "codex",
        "section_id": "s",
        "project_dir": "/tmp/p",
        "evidence_type": "build",
        "supersedes_check_event_id": "evt_earlier_failure",
    }
    rejected = call({**base, "result": "failed", "exit_code": 1})
    assert rejected["error"]["code"] == -32602

    accepted = call({**base, "result": "passed", "exit_code": 0})
    assert "error" not in accepted
    text = accepted["result"]["content"][0]["text"]
    assert "evt_earlier_failure" in text


def test_resolution_pointing_at_a_failed_check_surfaces_its_own_diagnostic(tmp_path: Path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    project = "/tmp/proj"

    def call(msg_id: int, arguments: dict) -> dict:
        return server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": "tools/call",
                "params": {"name": "agentacct_record_machine_check", "arguments": arguments},
            }
        )

    failed = call(
        1,
        {
            "source": "codex",
            "section_id": "review",
            "project_dir": project,
            "evidence_type": "test",
            "result": "failed",
            "name": "probe",
            "command": "./probe.sh",
            "exit_code": 1,
        },
    )
    failed_id = json.loads(failed["result"]["content"][0]["text"])["event"]["event_id"]

    # A passing check that (wrongly) tries to clear the FAILED CHECK as if it
    # were a blocker episode. The server stamps the resolution contract; at read
    # time it must reject with a distinct diagnostic, not silently vanish.
    resolution = call(
        2,
        {
            "source": "codex",
            "section_id": "review",
            "project_dir": project,
            "evidence_type": "artifact",
            "result": "passed",
            "name": "fix",
            "exit_code": 0,
            "summary": "fixed",
            "resolves_blocked_event_id": failed_id,
            "resolution_scope": "full",
            "resolution_summary": "The probe passes now.",
        },
    )
    assert "error" not in resolution

    ledger = build_work_ledger(server.service.list_all_events())
    reasons = ledger["insights"]["blocker_resolution"]["rejected_by_reason"]
    assert reasons.get("target_is_failed_check", 0) >= 1
