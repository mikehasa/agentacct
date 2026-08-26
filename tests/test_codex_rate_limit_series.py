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


def test_series_respects_the_recorded_watermark(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    path = _write_rollout(
        home,
        "rollout-2026-08-26T00-00-00-cccc.jsonl",
        [
            _rollout_line("2026-08-26T10:00:00Z", 1.0),
            _rollout_line("2026-08-26T11:00:00Z", 2.0),
            _rollout_line("2026-08-26T12:00:00Z", 3.0),
        ],
    )
    full = rl.read_codex_rate_limits_series(home, now=1788000000.0)
    assert len(full) == 3
    watermark = float(full[1].captured_at)  # the 11:00 reading is recorded
    later = rl.read_codex_rate_limits_series(home, since_epoch=watermark, now=1788000000.0)
    used = [window.used_percent for snapshot in later for window in snapshot.windows]
    assert used == [3.0]  # strictly-newer only: no re-import bloat
    # A file whose mtime predates the watermark is skipped unread.
    import os

    os.utime(path, (watermark - 10, watermark - 10))
    assert rl.read_codex_rate_limits_series(home, since_epoch=watermark, now=1788000000.0) == []


def test_watermark_helper_reads_the_newest_recorded_codex_capture(tmp_path: Path) -> None:
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
    assert rl.latest_recorded_captured_at(events, client="codex") == 120.0
    assert rl.latest_recorded_captured_at([], client="codex") == 0.0
