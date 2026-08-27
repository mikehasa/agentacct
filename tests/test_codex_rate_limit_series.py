"""The dense codex weekly-meter series importer (calibration food).

One snapshot per scan tick starves the weekly fit; the series reader imports
the in-file history — watermark-bounded, replay-burst-collapsed, default
bucket only.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentacct import rate_limits as rl


def _rollout_line(iso_ts: str, used: float, *, limit_id: str | None = "codex") -> str:
    payload = {
        "rate_limits": {
            "limit_id": limit_id,
            "primary": {
                "used_percent": used,
                "window_minutes": 10080,
                "resets_at": 1788273707,
            },
            "secondary": None,
            "plan_type": "pro",
        }
    }
    return json.dumps({"timestamp": iso_ts, "payload": payload})


def _write_rollout(root: Path, name: str, lines: list[str]) -> Path:
    sessions = root / "sessions" / "2026" / "08" / "26"
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_series_collapses_replay_bursts_and_merges_files(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    # A replayed prefix: three readings stamped milliseconds apart (the fork
    # copy), then the genuine live turns minutes apart.
    _write_rollout(
        home,
        "rollout-2026-08-26T00-00-00-aaaa.jsonl",
        [
            _rollout_line("2026-08-26T10:00:00.000Z", 1.0),
            _rollout_line("2026-08-26T10:00:00.050Z", 2.0),
            _rollout_line("2026-08-26T10:00:00.100Z", 3.0),  # burst survivor
            _rollout_line("2026-08-26T10:30:00Z", 4.0),
            _rollout_line("2026-08-26T11:00:00Z", 5.0),
        ],
    )
    _write_rollout(
        home,
        "rollout-2026-08-26T01-00-00-bbbb.jsonl",
        [
            _rollout_line("2026-08-26T12:00:00Z", 6.0),
            # A non-default bucket must never pollute the account series.
            _rollout_line("2026-08-26T12:30:00Z", 99.0, limit_id="spark"),
            "not json at all",
        ],
    )
    series = rl.read_codex_rate_limits_series(home, now=1788000000.0)
    used = [window.used_percent for snapshot in series for window in snapshot.windows]
    assert used == [3.0, 4.0, 5.0, 6.0]
    times = [snapshot.captured_at for snapshot in series]
    assert times == sorted(times)


def test_series_backfill_is_idempotent_via_the_exclusion_set(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    _write_rollout(
        home,
        "rollout-2026-08-26T00-00-00-cccc.jsonl",
        [
            _rollout_line("2026-08-26T10:00:00Z", 1.0),
            _rollout_line("2026-08-26T10:30:00Z", 1.0),  # transition no-op, dropped
            _rollout_line("2026-08-26T11:00:00Z", 2.0),
            _rollout_line("2026-08-26T12:00:00Z", 3.0),
        ],
    )
    full = rl.read_codex_rate_limits_series(home, now=1788000000.0)
    used = [window.used_percent for snapshot in full for window in snapshot.windows]
    assert used == [1.0, 2.0, 3.0]  # deterministic transition-collapse

    # BACKFILL: sparse tick-recording caught only the 12:00 state — the
    # readings BETWEEN recorded snapshots still import.
    recorded = {float(full[-1].captured_at)}
    backfill = rl.read_codex_rate_limits_series(
        home, exclude_captured_at=recorded, now=1788000000.0
    )
    used = [window.used_percent for snapshot in backfill for window in snapshot.windows]
    assert used == [1.0, 2.0]

    # After the backfill, every survivor is recorded — a re-scan is a no-op.
    recorded |= {float(snapshot.captured_at) for snapshot in backfill}
    assert rl.read_codex_rate_limits_series(
        home, exclude_captured_at=recorded, now=1788000000.0
    ) == []


def test_recorded_captured_ats_reads_one_clients_stream(tmp_path: Path) -> None:
    events = [
        {
            "event_type": "rate_limit_observed",
            "created_at": 50.0,
            "metadata": {"client": "codex", "captured_at": 120.0},
        },
        {
            "event_type": "rate_limit_observed",
            "created_at": 60.0,
            "metadata": {"client": "codex", "captured_at": 80.0},
        },
        {
            "event_type": "rate_limit_observed",
            "created_at": 999.0,
            "metadata": {"client": "claude-code", "captured_at": 500.0},
        },
        {"event_type": "other", "created_at": 999.0, "metadata": {"client": "codex"}},
    ]
    assert rl.recorded_captured_ats(events, client="codex") == {120.0, 80.0}
    assert rl.recorded_captured_ats([], client="codex") == set()
