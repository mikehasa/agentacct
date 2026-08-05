"""Tests for the provider rate-limit foundation (`rate_limit_observed`)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from agentacct import rate_limits as rl
from agentacct.cli import app
from agentacct.service import SentinelService


# --------------------------------------------------------------------------- #
# normalization
# --------------------------------------------------------------------------- #


def test_normalize_codex_full_object():
    raw = {
        "limit_id": "codex",
        "primary": {"used_percent": 52.0, "window_minutes": 10080, "resets_at": 1785259795},
        "secondary": {"used_percent": 12.5, "window_minutes": 300, "resets_at": 1785000000},
        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
        "plan_type": "pro",
        "rate_limit_reached_type": None,
    }
    snap = rl.normalize_codex_rate_limits(
        raw, captured_at="2026-07-25T10:37:02.336Z", session_id="sess-1", source_file="/x.jsonl"
    )
    assert snap is not None
    assert snap.client == "codex"
    assert snap.plan_type == "pro"
    assert snap.credits == {"has_credits": False, "unlimited": False, "balance": "0"}
    kinds = {w.kind: w for w in snap.windows}
    assert set(kinds) == {"7d", "5h"}
    assert kinds["7d"].used_percent == 52.0
    assert kinds["7d"].resets_at == 1785259795
    assert kinds["5h"].window_minutes == 300
    # ISO-8601 with a trailing Z parses to a real epoch.
    assert snap.captured_at is not None and snap.captured_at > 1_700_000_000
    # limit_id / limit_name (top-level bucket identity) are preserved.
    assert snap.limit_id == "codex"
    assert snap.limit_name is None


def test_normalize_codex_preserves_nondefault_bucket_identity():
    """A model-specific / Spark bucket keeps its own limit_id + limit_name."""
    raw = {
        "limit_id": "gpt-5-spark",
        "limit_name": "GPT-5 Spark",
        "secondary": {"used_percent": 3.0, "window_minutes": 10080, "resets_at": 9},
        "plan_type": "pro",
    }
    snap = rl.normalize_codex_rate_limits(raw)
    assert snap is not None
    assert snap.limit_id == "gpt-5-spark"
    assert snap.limit_name == "GPT-5 Spark"


def test_run_id_segregates_nondefault_codex_buckets():
    """Different limit_id = different quota bucket = different stream. The
    default 'codex' bucket keeps the legacy run_id (no migration churn); any
    other bucket branches into its own stream so it can't contaminate the
    default series' transition history."""
    default = rl.normalize_codex_rate_limits(
        {"limit_id": "codex", "secondary": {"used_percent": 40.0, "window_minutes": 10080}}
    )
    spark = rl.normalize_codex_rate_limits(
        {"limit_id": "gpt-5-spark", "secondary": {"used_percent": 40.0, "window_minutes": 10080}}
    )
    missing = rl.normalize_codex_rate_limits(
        {"secondary": {"used_percent": 40.0, "window_minutes": 10080}}
    )
    assert rl.snapshot_run_id(default) == rl.CODEX_RUN_ID
    assert rl.snapshot_run_id(missing) == rl.CODEX_RUN_ID  # absent id → legacy stream
    assert rl.snapshot_run_id(spark) == f"{rl.CODEX_RUN_ID}:gpt-5-spark"
    assert rl.snapshot_run_id(spark) != rl.snapshot_run_id(default)
    # Same window state, different bucket → different state signatures, so the
    # transition dedup never collapses two buckets into one meter.
    assert rl.snapshot_state_signature(spark) != rl.snapshot_state_signature(default)


def test_snapshot_to_event_carries_bucket_identity():
    snap = rl.normalize_codex_rate_limits(
        {"limit_id": "gpt-5-spark", "limit_name": "GPT-5 Spark",
         "secondary": {"used_percent": 3.0, "window_minutes": 10080}}
    )
    event = rl.snapshot_to_event(snap)
    assert event["metadata"]["limit_id"] == "gpt-5-spark"
    assert event["metadata"]["limit_name"] == "GPT-5 Spark"
    assert event["run_id"] == f"{rl.CODEX_RUN_ID}:gpt-5-spark"


def test_normalize_codex_rejects_empty_and_malformed():
    assert rl.normalize_codex_rate_limits(None) is None
    assert rl.normalize_codex_rate_limits({}) is None
    assert rl.normalize_codex_rate_limits({"primary": {"window_minutes": 300}}) is None  # no used_percent
    # A window with no usable percent is skipped, not crashed on.
    snap = rl.normalize_codex_rate_limits(
        {"primary": {"used_percent": None}, "secondary": {"used_percent": 5.0, "window_minutes": 300}}
    )
    assert snap is not None
    assert [w.kind for w in snap.windows] == ["5h"]


def test_normalize_claude_sample():
    snap = rl.normalize_claude_plan_usage_sample(
        {"t": 1785677629903, "org": "org-1", "u": {"fh": 5, "sd": 24}}, source_file="/p.json"
    )
    assert snap is not None
    assert snap.client == "claude-code"
    assert snap.org == "org-1"
    kinds = {w.kind: w for w in snap.windows}
    assert kinds["5h"].used_percent == 5.0 and kinds["5h"].window_minutes == rl.FIVE_HOUR_MINUTES
    assert kinds["7d"].used_percent == 24.0 and kinds["7d"].resets_at is None
    assert snap.captured_at == pytest.approx(1785677629.903, rel=1e-9)


def test_normalize_claude_rejects_malformed():
    assert rl.normalize_claude_plan_usage_sample(None) is None
    assert rl.normalize_claude_plan_usage_sample({"t": 1, "org": "o"}) is None  # no u
    assert rl.normalize_claude_plan_usage_sample({"u": {}}) is None  # no windows


# --------------------------------------------------------------------------- #
# idempotency + event building
# --------------------------------------------------------------------------- #


def _codex_snapshot(used=52.0, captured=1000.0):
    return rl.normalize_codex_rate_limits(
        {"primary": {"used_percent": used, "window_minutes": 10080, "resets_at": 42}, "plan_type": "pro"},
        captured_at=captured,
    )


def test_state_signature_ignores_time_and_balance_but_tracks_values():
    a = _codex_snapshot(used=52.0, captured=1000.0)
    b = _codex_snapshot(used=52.0, captured=9999.0)  # same values, later time
    c = _codex_snapshot(used=53.0, captured=1000.0)  # changed value
    assert rl.snapshot_state_signature(a) == rl.snapshot_state_signature(b)
    assert rl.snapshot_state_signature(a) != rl.snapshot_state_signature(c)
    # A drifting credits.balance must NOT change the signature (else a pay-as-you-go
    # account floods the ledger with a snapshot every scan).
    d = rl.normalize_codex_rate_limits(
        {
            "primary": {"used_percent": 52.0, "window_minutes": 10080, "resets_at": 42},
            "plan_type": "pro",
            "credits": {"has_credits": True, "unlimited": False, "balance": "10.00"},
        },
        captured_at=1000.0,
    )
    e = rl.normalize_codex_rate_limits(
        {
            "primary": {"used_percent": 52.0, "window_minutes": 10080, "resets_at": 42},
            "plan_type": "pro",
            "credits": {"has_credits": True, "unlimited": False, "balance": "9.50"},
        },
        captured_at=2000.0,
    )
    assert rl.snapshot_state_signature(d) == rl.snapshot_state_signature(e)


def test_snapshot_to_event_shape_and_writer_invariants():
    codex_event = rl.snapshot_to_event(_codex_snapshot())
    assert codex_event["event_type"] == "rate_limit_observed"
    assert codex_event["event_type"].split("_")[0] not in {"section", "task"}
    assert codex_event["source"] == rl.CODEX_SOURCE
    assert codex_event["run_id"] == rl.CODEX_RUN_ID  # codex limits are account-wide
    md = codex_event["metadata"]
    assert md["client"] == "codex"
    assert "state_signature" in md
    assert "idempotency_key" not in md  # dedup is transition-based, not global
    assert "sentinel_semantic_kind" not in md  # must not be swept into the work feed
    # numbers live in metadata, never in the top-level usage fields
    assert codex_event["estimated_input_tokens"] is None
    assert codex_event["estimated_output_tokens"] is None

    claude_snap = rl.normalize_claude_plan_usage_sample({"t": 1, "org": "abc", "u": {"fh": 1, "sd": 2}})
    claude_event = rl.snapshot_to_event(claude_snap)
    assert claude_event["run_id"] == "claude_plan_usage_abc"  # per-org stream
    assert claude_event["source"] == rl.CLAUDE_SOURCE


# --------------------------------------------------------------------------- #
# readers (filesystem, fail-soft)
# --------------------------------------------------------------------------- #


def test_read_claude_plan_usage_latest_per_org(tmp_path):
    path = tmp_path / "plan-usage-history.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "samples": [
                    {"t": 100, "org": "A", "u": {"fh": 10, "sd": 10}},
                    {"t": 300, "org": "A", "u": {"fh": 40, "sd": 20}},  # latest for A
                    {"t": 200, "org": "B", "u": {"fh": 5, "sd": 5}},
                ],
            }
        ),
        encoding="utf-8",
    )
    snaps = rl.read_claude_plan_usage_latest(path)
    by_org = {s.org: s for s in snaps}
    assert set(by_org) == {"A", "B"}
    a_five = {w.kind: w.used_percent for w in by_org["A"].windows}["5h"]
    assert a_five == 40.0  # the later sample won


def test_read_claude_plan_usage_missing_and_malformed(tmp_path):
    assert rl.read_claude_plan_usage_latest(tmp_path / "nope.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert rl.read_claude_plan_usage_latest(bad) == []
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"version": 2, "samples": []}), encoding="utf-8")
    assert rl.read_claude_plan_usage_latest(empty) == []


def _write_codex_rollout(root, day, name, *, timestamp, rate_limits):
    d = root / "sessions" / "2026" / "07" / day
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"rollout-{name}.jsonl"
    line = {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": {"input_tokens": 1, "total_tokens": 1}},
            "rate_limits": rate_limits,
        },
    }
    f.write_text(json.dumps(line) + "\n", encoding="utf-8")
    return f


def test_read_codex_rate_limits_latest_picks_freshest(tmp_path):
    home = tmp_path / "codex"
    _write_codex_rollout(
        home, "28", "old",
        timestamp="2026-07-28T10:00:00.000Z",
        rate_limits={"primary": {"used_percent": 10.0, "window_minutes": 10080, "resets_at": 1}},
    )
    _write_codex_rollout(
        home, "29", "new",
        timestamp="2026-07-29T10:00:00.000Z",
        rate_limits={"primary": {"used_percent": 88.0, "window_minutes": 10080, "resets_at": 2}, "plan_type": "pro"},
    )
    snap = rl.read_codex_rate_limits_latest(home)
    assert snap is not None
    assert snap.plan_type == "pro"
    assert snap.windows[0].used_percent == 88.0  # the later-captured file won


def test_read_codex_latest_ignores_rewritten_outer_timestamp(tmp_path):
    """The replay bug: a fork copies history into a new file and the recorder
    stamps each copied TokenCount with a fresh now_utc(), so a REPLAYED old
    limit payload can carry a LATER outer timestamp than the genuine current
    one. Selection must go by file mtime (which replay does not rewrite), not by
    the outer timestamp — else a stale, replayed snapshot surfaces as 'latest'.
    """
    import os

    home = tmp_path / "codex"
    # The genuine active file: current state (55%), but an EARLIER outer stamp.
    active = _write_codex_rollout(
        home, "29", "active",
        timestamp="2026-07-29T09:00:00.000Z",
        rate_limits={"limit_id": "codex", "secondary": {"used_percent": 55.0, "window_minutes": 10080, "resets_at": 5}, "plan_type": "pro"},
    )
    # A fork/replay file: stale value (10%) but a LATER (rewritten) outer stamp.
    replay = _write_codex_rollout(
        home, "28", "replay",
        timestamp="2026-07-30T23:00:00.000Z",  # numerically newer, but rewritten
        rate_limits={"limit_id": "codex", "secondary": {"used_percent": 10.0, "window_minutes": 10080, "resets_at": 1}},
    )
    # Force filesystem truth: the active file is genuinely the most recent write.
    os.utime(replay, (1_785_000_000, 1_785_000_000))   # older mtime
    os.utime(active, (1_785_600_000, 1_785_600_000))   # newer mtime
    snap = rl.read_codex_rate_limits_latest(home)
    assert snap is not None
    # mtime wins: the genuine current 55%, NOT the replayed 10% with the newer
    # (rewritten) outer timestamp.
    assert snap.windows[0].used_percent == 55.0
    assert snap.plan_type == "pro"


def test_read_codex_rate_limits_missing_root(tmp_path):
    assert rl.read_codex_rate_limits_latest(tmp_path / "no-codex") is None


def test_read_codex_rollout_without_rate_limits_is_ignored(tmp_path):
    home = tmp_path / "codex"
    d = home / "sessions" / "2026" / "07" / "30"
    d.mkdir(parents=True, exist_ok=True)
    (d / "rollout-x.jsonl").write_text(
        json.dumps({"timestamp": "2026-07-30T10:00:00Z", "payload": {"type": "token_count", "info": {"total_token_usage": {}}}}) + "\n",
        encoding="utf-8",
    )
    assert rl.read_codex_rate_limits_latest(home) is None


# --------------------------------------------------------------------------- #
# classification: stored, not usage, not work-feed, retained in v1
# --------------------------------------------------------------------------- #


def test_event_classification_is_safe():
    from agentacct.canonical.lane_inventory import (
        canonical_lanes_for_event,
        must_retain_in_v1,
        retention_reason,
    )
    from agentacct.usage_truth import is_local_usage_import_event

    event = rl.snapshot_to_event(_codex_snapshot())
    assert is_local_usage_import_event(event) is False  # never counted as usage
    assert must_retain_in_v1(event) is True  # stored, never dropped
    assert retention_reason("rate_limit_observed")  # honestly registered
    lanes = canonical_lanes_for_event(event)
    assert "usage" not in lanes
    assert "work_claim" not in lanes


def test_not_swept_into_work_feed():
    from agentacct.work_ledger import _is_work_event

    event = rl.snapshot_to_event(_codex_snapshot())
    assert _is_work_event(event) is False


# --------------------------------------------------------------------------- #
# recording is idempotent end-to-end
# --------------------------------------------------------------------------- #


def _count_rate_limit_events(service):
    return sum(1 for e in service.list_all_events() if e.get("event_type") == "rate_limit_observed")


def test_transition_dedup_records_only_on_change_including_return():
    recorded: list = []

    def record(event):
        recorded.append(event)

    # first ever → recorded
    n = rl.record_snapshots_transitionally(
        [_codex_snapshot(used=52.0, captured=1000.0)], existing_events=[], record=record
    )
    assert n == 1 and len(recorded) == 1
    # unchanged since last recorded → no-op
    n = rl.record_snapshots_transitionally(
        [_codex_snapshot(used=52.0, captured=2000.0)], existing_events=list(recorded), record=record
    )
    assert n == 0 and len(recorded) == 1
    # changed value → recorded
    n = rl.record_snapshots_transitionally(
        [_codex_snapshot(used=60.0, captured=3000.0)], existing_events=list(recorded), record=record
    )
    assert n == 1 and len(recorded) == 2
    # value RETURNS to a previously-seen 52 (a decline/reset). This is the bug the
    # adversarial review caught: global dedup would drop it and `limits` would show
    # the 60 high-water mark forever. Transition dedup records it.
    n = rl.record_snapshots_transitionally(
        [_codex_snapshot(used=52.0, captured=4000.0)], existing_events=list(recorded), record=record
    )
    assert n == 1 and len(recorded) == 3
    # and the newest recorded event carries the fresh observation time
    assert (recorded[-1]["metadata"]["captured_at"]) == 4000.0


def test_transition_dedup_ignores_credit_balance_drift():
    recorded: list = []

    def snap(balance):
        return rl.normalize_codex_rate_limits(
            {
                "primary": {"used_percent": 10.0, "window_minutes": 10080, "resets_at": 7},
                "credits": {"has_credits": True, "unlimited": False, "balance": balance},
            },
            captured_at=1000.0,
        )

    rl.record_snapshots_transitionally([snap("10.00")], existing_events=[], record=recorded.append)
    # balance drifts but the rate-limit window is unchanged → no new event
    rl.record_snapshots_transitionally([snap("9.98")], existing_events=list(recorded), record=recorded.append)
    rl.record_snapshots_transitionally([snap("9.90")], existing_events=list(recorded), record=recorded.append)
    assert len(recorded) == 1


def test_non_finite_values_fail_soft(tmp_path):
    # int(inf)/int(nan) would raise; the reader must fail soft and drop the field.
    snap = rl.normalize_codex_rate_limits(
        {"primary": {"used_percent": 50.0, "window_minutes": float("inf"), "resets_at": float("nan")}}
    )
    assert snap is not None
    window = snap.windows[0]
    assert window.used_percent == 50.0
    assert window.window_minutes is None and window.resets_at is None
    # a non-finite used_percent drops that window entirely (keeps the finite one)
    claude = rl.normalize_claude_plan_usage_sample({"t": 1, "org": "o", "u": {"fh": float("nan"), "sd": 20}})
    assert [w.kind for w in claude.windows] == ["7d"]
    # and a non-finite value never reaches the JSON output as a NaN/Infinity token
    service = SentinelService(tmp_path)
    service.record_event(rl.snapshot_to_event(claude))
    js = CliRunner().invoke(app, ["limits", "--store-dir", str(tmp_path), "--json"])
    assert js.exit_code == 0
    assert "NaN" not in js.stdout and "Infinity" not in js.stdout
    json.loads(js.stdout)  # strict parse must succeed


def test_codex_home_env_is_comma_split(tmp_path, monkeypatch):
    root_b = tmp_path / "b"
    _write_codex_rollout(
        root_b, "29", "s",
        timestamp="2026-07-29T10:00:00Z",
        rate_limits={"primary": {"used_percent": 33.0, "window_minutes": 10080, "resets_at": 1}},
    )
    monkeypatch.setenv("CODEX_HOME", f"{tmp_path / 'a'},{root_b}")
    snap = rl.read_codex_rate_limits_latest()  # codex_home=None → CODEX_HOME env
    assert snap is not None and snap.windows[0].used_percent == 33.0


# --------------------------------------------------------------------------- #
# the `limits` command
# --------------------------------------------------------------------------- #


def test_import_with_relocated_homes_does_not_leak_real_machine(tmp_path, monkeypatch):
    """A hermetic import that relocates the homes must record NO rate-limit events
    from the developer's real ~/.codex or ~/Library Claude files — even with the
    global-limit scan explicitly turned ON (the per-home gates still hold)."""
    from agentacct.cli import _local_usage_import_payload

    monkeypatch.setenv("AGENTACCT_SCAN_GLOBAL_LIMITS", "1")  # override the test default
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    codex_home = tmp_path / "codex"
    (codex_home / "sessions").mkdir(parents=True)
    claude_home = tmp_path / "claude"
    claude_home.mkdir()
    store_dir = tmp_path / "state"

    _local_usage_import_payload(
        store_dir=store_dir,
        client="all",
        codex_home=codex_home,
        claude_home=claude_home,
    )
    service = SentinelService(store_dir)
    assert _count_rate_limit_events(service) == 0


def test_claude_config_dir_env_blocks_global_read(tmp_path, monkeypatch):
    """Relocating the Claude home via CLAUDE_CONFIG_DIR (the env the import itself
    honors) must also stop the machine-global desktop plan-usage read, even with
    the global scan on and claude_home param None."""
    from agentacct.cli import _local_usage_import_payload

    monkeypatch.setenv("AGENTACCT_SCAN_GLOBAL_LIMITS", "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "hermetic-claude"))
    codex_home = tmp_path / "codex"
    (codex_home / "sessions").mkdir(parents=True)
    store_dir = tmp_path / "state"

    _local_usage_import_payload(
        store_dir=store_dir,
        client="claude-code",
        codex_home=codex_home,
        claude_home=None,
    )
    service = SentinelService(store_dir)
    assert _count_rate_limit_events(service) == 0


def test_limits_command_empty(tmp_path):
    result = CliRunner().invoke(app, ["limits", "--store-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "No provider limit data" in result.stdout


def test_limits_command_renders_and_json(tmp_path):
    service = SentinelService(tmp_path)
    service.record_event(rl.snapshot_to_event(_codex_snapshot(used=52.0)))
    service.record_event(
        rl.snapshot_to_event(
            rl.normalize_claude_plan_usage_sample({"t": 1785677629903, "org": "org-1", "u": {"fh": 6, "sd": 25}})
        )
    )

    human = CliRunner().invoke(app, ["limits", "--store-dir", str(tmp_path)])
    assert human.exit_code == 0
    assert "codex" in human.stdout
    assert "claude-code" in human.stdout
    assert "5-hour" in human.stdout and "7-day" in human.stdout

    js = CliRunner().invoke(app, ["limits", "--store-dir", str(tmp_path), "--json"])
    assert js.exit_code == 0
    payload = json.loads(js.stdout)
    clients = {entry["client"] for entry in payload["limits"]}
    assert clients == {"codex", "claude-code"}

    # client filter
    only = CliRunner().invoke(app, ["limits", "--store-dir", str(tmp_path), "--client", "codex", "--json"])
    payload2 = json.loads(only.stdout)
    assert {e["client"] for e in payload2["limits"]} == {"codex"}


def test_limits_command_client_all_and_invalid(tmp_path):
    service = SentinelService(tmp_path)
    service.record_event(rl.snapshot_to_event(_codex_snapshot(used=52.0)))
    # --client all is a no-op filter (not a literal match) → shows data, never the
    # false "no data" empty-state.
    ok = CliRunner().invoke(app, ["limits", "--store-dir", str(tmp_path), "--client", "all", "--json"])
    assert ok.exit_code == 0
    assert {e["client"] for e in json.loads(ok.stdout)["limits"]} == {"codex"}
    # an unknown client value errors instead of silently reporting no data
    bad = CliRunner().invoke(app, ["limits", "--store-dir", str(tmp_path), "--client", "claude"])
    assert bad.exit_code != 0
