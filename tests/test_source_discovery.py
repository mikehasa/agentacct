from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_chronicle.cli import app
from agent_chronicle.source_discovery import discover_usage_sources


def _make_codex_source(root: Path) -> Path:
    codex_home = root / "codex-home"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "rollout-test.jsonl").write_text('{"type":"event_msg"}\n', encoding="utf-8")
    con = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        con.execute("create table threads (id text primary key, tokens_used integer, model text)")
        con.execute("insert into threads values (?, ?, ?)", ("codex-session", 123, "gpt-5.5"))
        con.execute("insert into threads values (?, ?, ?)", ("codex-review-session", 456, "codex-auto-review"))
        con.commit()
    finally:
        con.close()
    return codex_home


def _make_claude_source(root: Path) -> Path:
    claude_home = root / "claude-home"
    project = claude_home / "projects" / "project"
    project.mkdir(parents=True)
    (project / "session.jsonl").write_text('{"type":"assistant"}\n', encoding="utf-8")
    return claude_home


def _make_hermes_source(root: Path) -> Path:
    hermes_home = root / "hermes-home"
    hermes_home.mkdir()
    con = sqlite3.connect(hermes_home / "state.db")
    try:
        con.execute(
            """
            create table sessions (
                id text primary key,
                model text,
                started_at real,
                input_tokens integer,
                output_tokens integer,
                estimated_cost_usd real,
                actual_cost_usd real
            )
            """
        )
        con.execute("insert into sessions values (?, ?, ?, ?, ?, ?, ?)", ("hermes-session", "gpt-5-mini", 1_750_000_000.0, 100, 20, 0.11, 0.42))
        con.commit()
    finally:
        con.close()
    return hermes_home


def test_discover_usage_sources_reports_found_and_pending_sources(tmp_path):
    codex_home = _make_codex_source(tmp_path)
    claude_home = _make_claude_source(tmp_path)
    hermes_home = _make_hermes_source(tmp_path)
    openclaw_home = tmp_path / "openclaw-home"
    openclaw_home.mkdir()
    (openclaw_home / "session.jsonl").write_text("{}", encoding="utf-8")

    sources = {
        source.client: source
        for source in discover_usage_sources(
            codex_home=codex_home,
            claude_home=claude_home,
            hermes_home=hermes_home,
            openclaw_home=openclaw_home,
            opencode_home=tmp_path / "missing-opencode",
        )
    }

    assert sources["codex"].status == "found"
    assert sources["codex"].session_count == 2
    assert sources["codex"].importer == "agentacct usage import-local --client codex"
    assert sources["claude-code"].status == "found"
    assert sources["hermes"].status == "found"
    assert sources["hermes"].cost_confidence == "client_reported"
    assert sources["openclaw"].status == "found"
    assert sources["openclaw"].usage_confidence == "unknown"
    assert sources["openclaw"].importer == "agentacct usage import-local --client openclaw"
    assert sources["opencode"].status == "missing"


def test_opencode_native_db_is_detected_but_not_importable(tmp_path):
    opencode_home = tmp_path / "opencode-home"
    opencode_home.mkdir()
    con = sqlite3.connect(opencode_home / "opencode.db")
    con.close()

    source = next(
        row
        for row in discover_usage_sources(
            codex_home=tmp_path / "missing-codex",
            claude_home=tmp_path / "missing-claude",
            hermes_home=tmp_path / "missing-hermes",
            openclaw_home=tmp_path / "missing-openclaw",
            opencode_home=opencode_home,
        )
        if row.client == "opencode"
    )

    assert source.status == "found"
    assert source.importer is None
    assert source.usage_confidence == "unknown"
    assert "native database parsing is pending" in " ".join(source.notes)


def test_usage_discover_sources_cli_json(tmp_path):
    codex_home = _make_codex_source(tmp_path)
    hermes_home = _make_hermes_source(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "usage",
            "discover-sources",
            "--codex-home",
            str(codex_home),
            "--hermes-home",
            str(hermes_home),
            "--claude-home",
            str(tmp_path / "missing-claude"),
            "--opencode-home",
            str(tmp_path / "missing-opencode"),
            "--openclaw-home",
            str(tmp_path / "missing-openclaw"),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    by_client = {source["client"]: source for source in payload["sources"]}
    assert by_client["codex"]["status"] == "found"
    assert by_client["codex"]["usage_confidence"] == "client_reported"
    assert by_client["hermes"]["status"] == "found"
    assert "model(s) detected" in " ".join(by_client["hermes"]["notes"])
    assert by_client["openclaw"]["status"] == "missing"


def test_codex_rollout_only_source_is_detected_for_session_observation(tmp_path):
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "rollout-observation-only.jsonl").write_text(
        '{"type":"session_meta","payload":{"id":"observation-only"}}\n',
        encoding="utf-8",
    )

    source = next(
        row
        for row in discover_usage_sources(
            codex_home=codex_home,
            claude_home=tmp_path / "missing-claude",
            hermes_home=tmp_path / "missing-hermes",
            openclaw_home=tmp_path / "missing-openclaw",
            opencode_home=tmp_path / "missing-opencode",
        )
        if row.client == "codex"
    )

    assert source.status == "found"
    assert source.session_count == 1
    assert source.usage_confidence == "unknown"
    assert source.importer == "agentacct usage import-local --client codex"
    assert "rollout session identity" in " ".join(source.notes)


def test_codex_zero_token_state_rows_are_detected_without_claiming_usage(tmp_path):
    codex_home = _make_codex_source(tmp_path)
    next((codex_home / "sessions").glob("*.jsonl")).unlink()
    con = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        con.execute("update threads set tokens_used = 0")
        con.commit()
    finally:
        con.close()

    source = next(
        row
        for row in discover_usage_sources(
            codex_home=codex_home,
            claude_home=tmp_path / "missing-claude",
            hermes_home=tmp_path / "missing-hermes",
            openclaw_home=tmp_path / "missing-openclaw",
            opencode_home=tmp_path / "missing-opencode",
        )
        if row.client == "codex"
    )

    assert source.status == "found"
    assert source.session_count == 2
    assert source.usage_confidence == "unknown"
    assert source.importer == "agentacct usage import-local --client codex"


def test_codex_source_session_count_is_db_and_rollout_identity_union(tmp_path):
    codex_home = _make_codex_source(tmp_path)
    rollout = next((codex_home / "sessions").glob("*.jsonl"))
    rollout.write_text(
        '{"type":"session_meta","payload":{"id":"rollout-only-session"}}\n',
        encoding="utf-8",
    )

    source = next(
        row
        for row in discover_usage_sources(
            codex_home=codex_home,
            claude_home=tmp_path / "missing-claude",
            hermes_home=tmp_path / "missing-hermes",
            openclaw_home=tmp_path / "missing-openclaw",
            opencode_home=tmp_path / "missing-opencode",
        )
        if row.client == "codex"
    )

    assert source.session_count == 3


def test_codex_rollout_source_symlink_is_ignored_fail_closed(tmp_path):
    outside = tmp_path / "outside-rollout.jsonl"
    outside.write_text(
        '{"type":"session_meta","payload":{"id":"outside"}}\n',
        encoding="utf-8",
    )
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "rollout-linked.jsonl").symlink_to(outside)

    source = next(
        row
        for row in discover_usage_sources(
            codex_home=codex_home,
            claude_home=tmp_path / "missing-claude",
            hermes_home=tmp_path / "missing-hermes",
            openclaw_home=tmp_path / "missing-openclaw",
            opencode_home=tmp_path / "missing-opencode",
        )
        if row.client == "codex"
    )

    assert source.status == "missing"
    assert source.file_count == 0
    assert source.session_count is None


def test_codex_sessions_root_symlink_is_ignored_fail_closed(tmp_path):
    outside = tmp_path / "outside-sessions"
    outside.mkdir()
    (outside / "rollout-outside.jsonl").write_text(
        '{"type":"session_meta","payload":{"id":"outside"}}\n',
        encoding="utf-8",
    )
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "sessions").symlink_to(outside, target_is_directory=True)

    source = next(
        row
        for row in discover_usage_sources(
            codex_home=codex_home,
            claude_home=tmp_path / "missing-claude",
            hermes_home=tmp_path / "missing-hermes",
            openclaw_home=tmp_path / "missing-openclaw",
            opencode_home=tmp_path / "missing-opencode",
        )
        if row.client == "codex"
    )

    assert source.status == "missing"
    assert source.file_count == 0
    assert source.session_count is None


@pytest.mark.parametrize(
    ("relative_path", "expected_file_count"),
    [
        ("archived_sessions/rollout-archived.jsonl", 0),
        ("sessions/not-a-rollout.jsonl", 0),
    ],
)
def test_codex_source_discovery_matches_rollout_only_importer_scope(
    tmp_path,
    relative_path,
    expected_file_count,
):
    codex_home = tmp_path / "codex-home"
    path = codex_home / relative_path
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"type":"session_meta","payload":{"id":"not-imported"}}\n',
        encoding="utf-8",
    )

    source = next(
        row
        for row in discover_usage_sources(
            codex_home=codex_home,
            claude_home=tmp_path / "missing-claude",
            hermes_home=tmp_path / "missing-hermes",
            openclaw_home=tmp_path / "missing-openclaw",
            opencode_home=tmp_path / "missing-opencode",
        )
        if row.client == "codex"
    )

    assert source.status == "missing"
    assert source.file_count == expected_file_count
    assert source.session_count is None


def test_usage_discover_sources_cli_human_output(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "usage",
            "discover-sources",
            "--codex-home",
            str(tmp_path / "missing-codex"),
            "--claude-home",
            str(tmp_path / "missing-claude"),
            "--opencode-home",
            str(tmp_path / "missing-opencode"),
            "--hermes-home",
            str(tmp_path / "missing-hermes"),
            "--openclaw-home",
            str(tmp_path / "missing-openclaw"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Detected local usage sources" in result.output
    assert "Codex" in result.output
    assert "Discovery is read-only" in result.output


def test_corrupt_codex_database_is_reported_as_error_not_missing(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "state_5.sqlite").write_text(
        "not a sqlite database",
        encoding="utf-8",
    )

    codex = next(
        source
        for source in discover_usage_sources(
            codex_home=codex_home,
            claude_home=tmp_path / "missing-claude",
            opencode_home=tmp_path / "missing-opencode",
            hermes_home=tmp_path / "missing-hermes",
            openclaw_home=tmp_path / "missing-openclaw",
        )
        if source.client == "codex"
    )

    assert codex.status == "error"
    assert codex.session_count is None
    assert codex.importer is None
    assert "could not be read" in " ".join(codex.notes)
