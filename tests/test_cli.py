import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentacct.cli import _exit_if_unsupported_platform, app
from agentacct.event_log import RAW_EVENT_LOG_FILENAME, RawEventLog


def _ledger_text(store_dir):
    return "\n".join(RawEventLog(Path(store_dir) / RAW_EVENT_LOG_FILENAME).read_lines())


def test_win32_platform_fails_fast_with_one_actionable_sentence(capsys):
    # Native Windows would otherwise die on `import fcntl` (service.py) with a
    # traceback; the guard must exit 2 with a WSL pointer instead.
    with pytest.raises(SystemExit) as excinfo:
        _exit_if_unsupported_platform("win32")

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "macOS or Linux" in err
    assert "WSL" in err
    assert "Traceback" not in err


def test_posix_platforms_pass_the_platform_guard():
    assert _exit_if_unsupported_platform("darwin") is None
    assert _exit_if_unsupported_platform("linux") is None


def test_wrapper_entry_points_fail_fast_on_win32(monkeypatch, capsys):
    # agentacct-claude / agentacct-codex never import cli, so they carry their own
    # copy of the same guard: on native Windows they must exit 2 with the WSL
    # pointer, not reach runner.py's POSIX process-group calls as a traceback.
    import agentacct.wrappers as wrappers

    monkeypatch.setattr(wrappers.sys, "platform", "win32")

    for entry in (wrappers.sentinel_claude_main, wrappers.sentinel_codex_main):
        with pytest.raises(SystemExit) as excinfo:
            entry()
        assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "macOS or Linux" in err
    assert "WSL" in err
    assert "Traceback" not in err


def test_report_accepts_latest_argument_when_store_has_run(tmp_path):
    run_dir = tmp_path / "runs" / "run_demo"
    run_dir.mkdir(parents=True)
    (run_dir / "report.md").write_text("demo report", encoding="utf-8")

    result = CliRunner().invoke(app, ["report", "latest", "--store-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "demo report" in result.output


def test_scan_is_safe_placeholder_not_real_process_inspector():
    result = CliRunner().invoke(app, ["scan"])

    assert result.exit_code == 0
    assert "not inspected in v0" in result.output


def test_demo_command_creates_report_evidence_and_value_score(tmp_path):
    store_dir = tmp_path / "state"

    result = CliRunner().invoke(app, ["demo", "--store-dir", str(store_dir), "--json"])

    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["status"] == "completed"
    assert summary["exit_code"] == 0
    assert summary["store_dir"] == str(store_dir)
    assert summary["value"]["rating"] == "excellent"
    assert "agentacct serve --store-dir" in summary["dashboard_command"]
    assert summary["dashboard_url"] == "http://127.0.0.1:8765"
    assert summary["report_json_command"].endswith(" --json")
    assert Path(summary["deliverable_path"]).read_text(encoding="utf-8") == "agent-chronicle-demo-deliverable\n"
    report_path = Path(summary["report_md"])
    assert report_path.exists()
    assert Path(summary["stdout_log"]).read_text(encoding="utf-8").startswith("agentacct demo task")
    assert "stderr is captured" in Path(summary["stderr_log"]).read_text(encoding="utf-8")

    report_result = CliRunner().invoke(app, ["report", summary["run_id"], "--store-dir", str(store_dir), "--json"])
    assert report_result.exit_code == 0, report_result.output
    payload = json.loads(report_result.output)
    assert payload["run"]["owned_by_sentinel"] is True
    assert payload["cost"]["event_count"] == 0
    assert payload["cost"]["actual_provider_cost_usd"] == 0
    assert payload["cost"]["billable_cost_usd"] == 0
    assert payload["outcome"]["machine_checks"]["configured"] is True
    assert payload["outcome"]["machine_checks"]["checks"][0]["name"] == "demo-deliverable-file"
    assert payload["outcome"]["machine_checks"]["checks"][0]["before"]["exit_code"] == 1
    assert payload["outcome"]["machine_checks"]["checks"][0]["after"]["exit_code"] == 0
    assert payload["outcome"]["machine_checks"]["resolved_failures"] == 1
    assert payload["outcome"]["judge"]["source"] == "local_demo"
    assert payload["outcome"]["value"]["score"] is not None


def test_demo_rejects_non_positive_budget(tmp_path):
    result = CliRunner().invoke(app, ["demo", "--store-dir", str(tmp_path), "--budget-usd", "0"])

    assert result.exit_code != 0


def test_demo_rejects_non_finite_budget(tmp_path):
    result = CliRunner().invoke(app, ["demo", "--store-dir", str(tmp_path), "--budget-usd", "nan"])

    assert result.exit_code != 0


def test_cost_pricing_catalog_lookup_reads_litellm_catalog(tmp_path):
    catalog_path = tmp_path / "litellm.json"
    catalog_path.write_text(
        json.dumps(
            {
                "gpt-test-cli": {
                    "litellm_provider": "openai",
                    "input_cost_per_token": 0.000001,
                    "output_cost_per_token": 0.000002,
                }
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "cost",
            "pricing-catalog",
            "--catalog-path",
            str(catalog_path),
            "--provider",
            "openai",
            "--model",
            "gpt-test-cli",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["lookup"]["priced"] is True
    assert payload["lookup"]["entry"]["source"] == "litellm_model_cost_map"


def test_cost_pricing_catalog_refresh_downloads_litellm_snapshot(tmp_path, monkeypatch):
    store_dir = tmp_path / "state"

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "gpt-refresh-test": {
                    "litellm_provider": "openai",
                    "input_cost_per_token": 0.000001,
                    "output_cost_per_token": 0.000002,
                }
            }

    def fake_get(url: str, *, follow_redirects: bool, timeout: float) -> FakeResponse:
        assert "model_prices_and_context_window.json" in url
        assert follow_redirects is True
        # 120 s (was 30): the un-compressed ~1.6 MB table has been observed to
        # take >60 s on slow links — the shared fetcher's slow-link timeout.
        assert timeout == 120.0
        return FakeResponse()

    monkeypatch.setattr("httpx.get", fake_get)

    result = CliRunner().invoke(
        app,
        [
            "cost",
            "pricing-catalog",
            "--store-dir",
            str(store_dir),
            "--refresh",
            "--provider",
            "openai",
            "--model",
            "gpt-refresh-test",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["lookup"]["priced"] is True
    assert payload["lookup"]["entry"]["source"] == "litellm_model_cost_map"
    assert payload["refresh"]["entry_count"] == 1
    assert Path(payload["catalog_path"]).exists()
    assert Path(payload["catalog_path"] + ".metadata.json").exists()


def test_event_cli_records_lists_and_redacts_events(tmp_path):
    store_dir = tmp_path / "state"

    record = CliRunner().invoke(
        app,
        [
            "event",
            "record",
            "--store-dir",
            str(store_dir),
            "--source",
            "codex",
            "--event-type",
            "task_note",
            "--run-id",
            "run_demo",
            "--provider",
            "openai",
            "--model",
            "gpt-5.5",
            "--estimated-input-tokens",
            "123",
            "--estimated-output-tokens",
            "45",
            "--estimated-cost-usd",
            "0.00012",
            "--usage-confidence",
            "estimated",
            "--cost-confidence",
            "estimated",
            "--metadata-json",
            '{"summary":"first event","api_key":"fake-secret-for-redaction"}',
            "--json",
        ],
    )

    assert record.exit_code == 0, record.output
    event = json.loads(record.output)["event"]
    assert event["source"] == "codex"
    assert event["event_type"] == "task_note"
    assert event["run_id"] == "run_demo"
    assert event["estimated_input_tokens"] == 123
    assert event["estimated_output_tokens"] == 45
    assert event["estimated_cost_usd"] == 0.00012
    assert event["metadata"]["summary"] == "first event"
    assert event["metadata"]["api_key"] == "[REDACTED]"
    assert "fake-secret-for-redaction" not in _ledger_text(store_dir)

    listed = CliRunner().invoke(app, ["event", "list", "--store-dir", str(store_dir), "--json"])
    assert listed.exit_code == 0, listed.output
    events = json.loads(listed.output)["events"]
    assert events[0]["event_id"] == event["event_id"]

    filtered = CliRunner().invoke(app, ["event", "list", "--store-dir", str(store_dir), "--run-id", "run_demo", "--json"])
    assert filtered.exit_code == 0, filtered.output
    assert json.loads(filtered.output)["events"][0]["event_id"] == event["event_id"]

    human = CliRunner().invoke(app, ["event", "list", "--store-dir", str(store_dir)])
    assert human.exit_code == 0, human.output
    assert "first event" in human.output


def test_event_summary_counts_recent_events_cost_and_tokens(tmp_path, monkeypatch):
    # Hand-seeds malformed/oversized raw lines directly into events.jsonl to test
    # summary tolerance of torn flat-file records — flat-file mechanics only present
    # in legacy mirror mode.
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "0")
    store_dir = tmp_path / "state"
    runner = CliRunner()

    note = runner.invoke(app, ["event", "note", "Reviewed Codex output", "--store-dir", str(store_dir), "--source", "codex", "--run-id", "run_demo"])
    assert note.exit_code == 0, note.output
    record = runner.invoke(
        app,
        [
            "event",
            "record",
            "--store-dir",
            str(store_dir),
            "--source",
            "custom-agent",
            "--event-type",
            "model_usage",
            "--run-id",
            "run_demo",
            "--provider",
            "openai",
            "--estimated-input-tokens",
            "100",
            "--estimated-output-tokens",
            "25",
            "--estimated-cost-usd",
            "0.0005",
            "--usage-confidence",
            "client_reported",
            "--cost-confidence",
            "unknown",
            "--metadata-json",
            json.dumps({"cached_input_tokens": 40, "reasoning_output_tokens": 5}),
        ],
    )
    assert record.exit_code == 0, record.output
    other = runner.invoke(app, ["event", "note", "Other run", "--store-dir", str(store_dir), "--source", "manual", "--run-id", "run_other"])
    assert other.exit_code == 0, other.output

    sensitive = "bearer-redaction-fixture-local-summary-placeholder-1234567890"
    sensitive_record = runner.invoke(
        app,
        [
            "event",
            "record",
            "--store-dir",
            str(store_dir),
            "--source",
            "custom-agent",
            "--event-type",
            "task_note",
            "--run-id",
            "run_demo",
            "--metadata-json",
            json.dumps({"summary": "called " + sensitive, "raw_provider_body": sensitive}),
        ],
    )
    assert sensitive_record.exit_code == 0, sensitive_record.output
    with (store_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "event_id": "evt_malformed",
                    "created_at": 1,
                    "source": "legacy",
                    "event_type": "model_usage",
                    "run_id": "run_demo",
                    "provider": "legacy-provider",
                    "estimated_cost_usd": -1,
                    "estimated_input_tokens": -100,
                    "estimated_output_tokens": 1e9999,
                    "metadata": {"summary": "legacy malformed event"},
                },
                allow_nan=True,
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "event_id": "evt_oversized",
                    "created_at": 2,
                    "source": "legacy",
                    "event_type": "model_usage",
                    "run_id": "run_demo",
                    "provider": "legacy-provider",
                    "estimated_cost_usd": 10**400,
                    "estimated_input_tokens": 10**400,
                    "estimated_output_tokens": 10**400,
                    "metadata": {"summary": "legacy oversized integer event"},
                }
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "event_id": "evt_huge_cost_a",
                    "created_at": 3,
                    "source": "legacy",
                    "event_type": "model_usage",
                    "run_id": "run_huge",
                    "estimated_cost_usd": 1e308,
                    "metadata": {},
                }
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "event_id": "evt_huge_cost_b",
                    "created_at": 4,
                    "source": "legacy",
                    "event_type": "model_usage",
                    "run_id": "run_huge",
                    "estimated_cost_usd": 1e308,
                    "metadata": {},
                }
            )
            + "\n"
        )

    summary_result = runner.invoke(app, ["event", "summary", "--store-dir", str(store_dir), "--run-id", "run_demo", "--json"])
    assert summary_result.exit_code == 0, summary_result.output
    summary = json.loads(summary_result.output)["summary"]
    assert summary["event_count"] == 5
    assert summary["note_count"] == 1
    assert summary["estimated_cost_usd"] == 0.0
    assert summary["estimated_input_tokens"] == 0
    assert summary["estimated_output_tokens"] == 0
    assert summary["estimated_total_tokens"] == 0
    assert summary["cached_input_tokens"] == 0
    assert summary["cache_creation_input_tokens"] == 0
    assert summary["cache_read_input_tokens"] == 0
    assert summary["reasoning_output_tokens"] == 0
    assert summary["total_tokens_including_cached"] == 0
    assert summary["by_source"] == {"codex": 1, "custom-agent": 2, "legacy": 2}
    assert summary["by_type"] == {"model_usage": 3, "note": 1, "task_note": 1}
    assert summary["by_provider"] == {"legacy-provider": 2, "openai": 1}
    assert summary["by_usage_confidence"] == {}
    assert summary["by_cost_confidence"] == {}
    assert summary["tokens_by_provider"] == {}
    assert sensitive not in summary_result.output

    human = runner.invoke(app, ["event", "summary", "--store-dir", str(store_dir), "--run-id", "run_demo"])
    assert human.exit_code == 0, human.output
    assert "agentacct event summary" in human.output
    assert "Events summarized: 5" in human.output
    assert "custom-agent: 2" in human.output
    assert "openai: 1" in human.output
    assert "total=0" in human.output
    assert "cache_create=0" in human.output
    assert "cache_read=0" in human.output
    assert "cached_input=0" in human.output
    assert "reasoning_output=0" in human.output
    assert "Provider token usage:" not in human.output
    assert sensitive not in human.output

    huge = runner.invoke(app, ["event", "summary", "--store-dir", str(store_dir), "--run-id", "run_huge", "--json"])
    assert huge.exit_code == 0, huge.output
    assert "Infinity" not in huge.output
    huge_summary = json.loads(huge.output)["summary"]
    assert huge_summary["event_count"] == 2
    assert huge_summary["estimated_cost_usd"] == 0.0


def test_event_note_records_plain_summary_without_json(tmp_path):
    store_dir = tmp_path / "state"

    record = CliRunner().invoke(
        app,
        ["event", "note", "Reviewed Codex output", "--store-dir", str(store_dir), "--source", "codex", "--run-id", "run_demo", "--json"],
    )

    assert record.exit_code == 0, record.output
    event = json.loads(record.output)["event"]
    assert event["source"] == "codex"
    assert event["event_type"] == "note"
    assert event["run_id"] == "run_demo"
    assert event["metadata"]["summary"] == "Reviewed Codex output"

    listed = CliRunner().invoke(app, ["event", "list", "--store-dir", str(store_dir)])
    assert listed.exit_code == 0, listed.output
    assert "Reviewed Codex output" in listed.output


def test_event_note_redacts_secret_shaped_summary_in_storage_and_output(tmp_path):
    store_dir = tmp_path / "state"
    secretish = "Bearer " + "local-redaction-placeholder-1234567890"

    record = CliRunner().invoke(app, ["event", "note", f"called {secretish}", "--store-dir", str(store_dir)])

    assert record.exit_code == 0, record.output
    # Only the secret span is replaced now: the surrounding note survives.
    assert "called [REDACTED_SECRET]" in record.output
    assert secretish not in record.output
    stored = _ledger_text(store_dir)
    assert "called [REDACTED_SECRET]" in stored
    assert secretish not in stored


def test_event_cli_rejects_invalid_metadata_run_id_and_limits(tmp_path):
    bad_metadata = CliRunner().invoke(
        app,
        ["event", "record", "--store-dir", str(tmp_path), "--source", "x", "--event-type", "y", "--metadata-json", "[]"],
    )
    bad_run = CliRunner().invoke(
        app,
        ["event", "record", "--store-dir", str(tmp_path), "--source", "x", "--event-type", "y", "--run-id", "../bad"],
    )
    bad_cost = CliRunner().invoke(
        app,
        ["event", "record", "--store-dir", str(tmp_path), "--source", "x", "--event-type", "y", "--estimated-cost-usd", "nan"],
    )
    bad_metadata_nan = CliRunner().invoke(
        app,
        ["event", "record", "--store-dir", str(tmp_path), "--source", "x", "--event-type", "y", "--metadata-json", '{"bad":NaN}'],
    )
    bad_limit = CliRunner().invoke(app, ["event", "list", "--store-dir", str(tmp_path), "--limit", "0"])
    bad_limit_high = CliRunner().invoke(app, ["event", "list", "--store-dir", str(tmp_path), "--limit", "201"])
    bad_note_run = CliRunner().invoke(app, ["event", "note", "bad", "--store-dir", str(tmp_path), "--run-id", "../bad"])
    bad_list_run = CliRunner().invoke(app, ["event", "list", "--store-dir", str(tmp_path), "--run-id", "../bad"])
    bad_summary_run = CliRunner().invoke(app, ["event", "summary", "--store-dir", str(tmp_path), "--run-id", "../bad"])
    bad_summary_limit = CliRunner().invoke(app, ["event", "summary", "--store-dir", str(tmp_path), "--limit", "0"])
    bad_summary_limit_high = CliRunner().invoke(app, ["event", "summary", "--store-dir", str(tmp_path), "--limit", "201"])

    assert bad_metadata.exit_code != 0
    assert bad_run.exit_code != 0
    assert "invalid run_id" in bad_run.output
    assert bad_cost.exit_code != 0
    assert bad_metadata_nan.exit_code != 0
    assert bad_limit.exit_code != 0
    assert bad_limit_high.exit_code != 0
    assert bad_note_run.exit_code != 0
    assert "invalid run_id" in bad_note_run.output
    assert bad_list_run.exit_code != 0
    assert "invalid run_id" in bad_list_run.output
    assert bad_summary_run.exit_code != 0
    assert "invalid run_id" in bad_summary_run.output
    assert bad_summary_limit.exit_code != 0
    assert bad_summary_limit_high.exit_code != 0
