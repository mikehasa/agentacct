"""Homepage contract for complete, no-JavaScript Task step disclosure."""

from pathlib import Path

from fastapi.testclient import TestClient

from agent_chronicle.api import create_local_api_app
from agent_chronicle.service import SentinelService


def _task_home(tmp_path: Path, *, step_count: int) -> str:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    session_id = "expandable-root"
    service.record_event(
        {
            "source": "codex-local-session-import",
            "event_type": "model_usage",
            "provider": "codex",
            "model": "gpt-test",
            "estimated_input_tokens": 20,
            "estimated_output_tokens": 5,
            "usage_confidence": "client_reported",
            "cost_confidence": "unknown",
            "metadata": {
                "usage_source": "local_client_session_store",
                "client": "codex",
                "client_session_id": session_id,
                "client_session_kind": "root",
                "client_session_title": "Build the product",
                "client_session_title_source": "explicit_client_title_field",
                "client_session_title_sanitized": True,
                "title_redacted": False,
                "cache_creation_tokens_reported": False,
                "cache_read_tokens_reported": True,
                "started_at": 1_750_000_000.0,
                "updated_at": 1_750_000_000.0,
            },
        },
        trusted_usage_import=True,
    )
    for index in range(1, step_count + 1):
        service.record_event(
            {
                "source": "codex",
                "event_type": "section_completed",
                "metadata": {
                    "sentinel_semantic_kind": "section",
                    "section_id": f"step-{index}",
                    "section_status": "completed",
                    "section_title": f"Recorded step {index:02d}",
                    "client": "codex",
                    "client_session_id": session_id,
                },
            }
        )
    return TestClient(create_local_api_app(store_dir=store_root)).get("/").text


def test_task_card_expands_every_step_without_javascript(tmp_path: Path) -> None:
    html = _task_home(tmp_path, step_count=6)

    assert '<div class="task-work-preview-head"><strong>Work steps</strong><span>6 recorded</span></div>' in html
    assert html.count('<details class="task-work-expander">') == 1
    assert "Show 2 more steps" in html
    assert "Hide additional steps" in html
    assert 'aria-label="Remaining 2 recorded work steps"' in html
    assert 'start="5"' in html
    for index in range(1, 7):
        assert html.count(f"Recorded step {index:02d}") == 1
    assert "+2 more steps" not in html
    assert "Work inside this Task" not in html
    assert "onclick=" not in html


def test_task_card_with_four_steps_needs_no_expander(tmp_path: Path) -> None:
    html = _task_home(tmp_path, step_count=4)

    assert '<div class="task-work-preview-head"><strong>Work steps</strong><span>4 recorded</span></div>' in html
    assert 'class="task-work-expander"' not in html
    for index in range(1, 5):
        assert html.count(f"Recorded step {index:02d}") == 1


def test_task_card_uses_singular_copy_for_one_remaining_step(tmp_path: Path) -> None:
    html = _task_home(tmp_path, step_count=5)

    assert "Show 1 more step" in html
    assert "Show 1 more steps" not in html
    assert 'aria-label="Remaining 1 recorded work step"' in html


def test_task_card_never_silently_drops_the_thirteenth_step(tmp_path: Path) -> None:
    html = _task_home(tmp_path, step_count=13)

    assert "Show 9 more steps" in html
    assert "Recorded step 13" in html
    assert html.count("Recorded step ") == 13
    assert "max-height: 22rem" in html
    assert "overflow-y: auto" in html
    assert "min-height: 44px" in html
    assert ".task-work-expander summary:focus-visible" in html
    assert "white-space: normal" in html
