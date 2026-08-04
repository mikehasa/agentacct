"""Regression tests for issue #53.

At real-world volume the Claude importer reported ``0 sessions / $0.00`` with
exit 0 while its own diagnostics showed the parser had extracted usage. The
cause was a source-global fail-closed: a single file that could not be
identity-resolved (an unrecognized non-transcript bookkeeping file, or a
transient stat failure) marked *every* parsed usage row incomplete, so the
planner withheld all of them.

These tests pin the fixed behavior:
  * an unresolvable, EXCLUDED file no longer withholds unrelated clean sessions,
  * a genuinely malformed SELECTED file is still withheld (safety preserved),
  * the CLI now fails loudly (non-zero exit) instead of a silent $0 when usage
    parsed but was withheld, and exposes ``withheld_incomplete_sessions``.
"""

import json
from pathlib import Path

from typer.testing import CliRunner

from agentacct.cli import app
from agentacct.client_usage import (
    discover_client_usage_with_diagnostics,
    plan_local_usage_import,
    select_usage_import_candidates,
)


def _write_good(project: Path, session_id: str, *, output_tokens: int = 200) -> Path:
    path = project / f"{session_id}.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": session_id,
                "cwd": "/work/project",
                "timestamp": "2026-08-01T00:00:00Z",
                "message": {
                    "model": "claude-opus-4-8",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": output_tokens,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _make_home_with_good_sessions(
    root: Path, *, n_dirs: int = 4, per_dir: int = 2
) -> tuple[Path, set[str]]:
    claude_home = root / "claude-home"
    projects = claude_home / "projects"
    projects.mkdir(parents=True)
    sessions: set[str] = set()
    for d in range(n_dirs):
        project = projects / f"-work-proj{d}"
        project.mkdir()
        for s in range(per_dir):
            sid = f"sess-{d}-{s}"
            _write_good(project, sid)
            sessions.add(sid)
    return claude_home, sessions


def _write_identity_unresolvable(project: Path, name: str, *, nlines: int = 300) -> Path:
    # >256 lines, none carrying a sessionId -> the bounded identity peek gives up
    # and records ``claude_transcript_identity_scan_truncated`` for this file.
    path = project / f"{name}.jsonl"
    path.write_text(
        "\n".join(json.dumps({"type": "summary", "idx": i}) for i in range(nlines)) + "\n",
        encoding="utf-8",
    )
    return path


def _overwrite_malformed(project: Path, session_id: str) -> Path:
    # A valid usage row followed by a malformed line -> the file parses a usage
    # event but flags malformed_transcript_lines, which legitimately withholds.
    path = project / f"{session_id}.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": session_id,
                "timestamp": "2026-08-01T00:00:00Z",
                "message": {
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                },
            }
        )
        + "\n{ this is not json }\n",
        encoding="utf-8",
    )
    return path


def test_unresolvable_excluded_file_does_not_withhold_clean_sessions(tmp_path):
    claude_home, sessions = _make_home_with_good_sessions(tmp_path, n_dirs=4, per_dir=2)
    _write_identity_unresolvable(
        claude_home / "projects" / "-work-proj0", "journal-like"
    )

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=200,
    )

    # Every cleanly-parsed session stays complete despite the one bad file.
    assert result.events
    assert all(event.source_parse_complete for event in result.events)

    imported = {
        candidate.client_session_id
        for candidate in select_usage_import_candidates(
            plan_local_usage_import(result.events, []),
            include_refresh=False,
        )
    }
    assert imported == sessions  # was empty before the fix

    # The failure is still surfaced, so ingestion health / reconciliation
    # authority stay conservative (they gate on these diagnostics, not on the
    # usage rows' completeness).
    diag = result.diagnostics["claude-code"]
    assert "claude_transcript_identity_scan_truncated" in diag["error_codes"]
    assert diag["error_count"] >= 1


def test_malformed_selected_file_is_still_withheld(tmp_path):
    claude_home, _ = _make_home_with_good_sessions(tmp_path, n_dirs=1, per_dir=2)
    _overwrite_malformed(claude_home / "projects" / "-work-proj0", "sess-0-0")

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=200,
    )
    plan = plan_local_usage_import(result.events, [])
    withheld = {
        candidate.client_session_id
        for candidate in plan.incomplete_source_candidates
    }
    assert "sess-0-0" in withheld


def test_cli_import_exits_nonzero_when_usage_is_withheld(tmp_path):
    claude_home, _ = _make_home_with_good_sessions(tmp_path, n_dirs=1, per_dir=1)
    _overwrite_malformed(claude_home / "projects" / "-work-proj0", "sess-0-0")

    result = CliRunner().invoke(
        app,
        [
            "usage", "import-local",
            "--client", "claude-code",
            "--claude-home", str(claude_home),
            "--store-dir", str(tmp_path / "store"),
            "--dry-run",
        ],
    )
    assert result.exit_code == 3
    assert "withheld" in result.output.lower()


def test_cli_import_exit_zero_when_only_excluded_file_unresolvable(tmp_path):
    claude_home, _ = _make_home_with_good_sessions(tmp_path, n_dirs=2, per_dir=2)
    _write_identity_unresolvable(
        claude_home / "projects" / "-work-proj0", "journal-like"
    )

    result = CliRunner().invoke(
        app,
        [
            "usage", "import-local",
            "--client", "claude-code",
            "--claude-home", str(claude_home),
            "--store-dir", str(tmp_path / "store"),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0


def test_cli_import_json_exposes_withheld_field(tmp_path):
    claude_home, sessions = _make_home_with_good_sessions(tmp_path, n_dirs=2, per_dir=2)

    result = CliRunner().invoke(
        app,
        [
            "usage", "import-local",
            "--client", "claude-code",
            "--claude-home", str(claude_home),
            "--store-dir", str(tmp_path / "store"),
            "--dry-run",
            "--json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["withheld_incomplete_sessions"] == 0
    assert payload["importable_sessions"] == len(sessions)
