"""Provider rate-limit / quota snapshots, read passively from local files.

No credentials, no API calls, no PTY, no statusline: agentacct reads the same
local files the coding agents already write, extracts the provider-reported
usage-limit windows, and records them as ``rate_limit_observed`` events into the
authoritative event log. ``agentacct limits`` renders the latest snapshot.

Sources (both confirmed to carry real, provider-reported data):

* **Codex** — ``~/.codex/sessions/**/rollout-*.jsonl`` ``token_count`` events carry
  a ``payload.rate_limits`` object: ``primary``/``secondary`` windows with
  ``used_percent`` + ``window_minutes`` + ``resets_at``, plus ``credits``,
  ``plan_type`` and ``rate_limit_reached_type``.
* **Claude (desktop app)** — ``~/Library/Application Support/Claude/plan-usage-history.json``
  is the desktop app's rolling series: ``samples: [{t, org, u: {fh, sd}}]`` where
  ``fh`` = 5-hour window used %, ``sd`` = 7-day window used %. No reset time, plan,
  or credits are recorded there — only the two percentages.

Design notes:

* The two ``read_*`` functions touch the filesystem and **fail soft** (return
  ``None``/``[]`` on any absence, wrong OS, or parse error): a limits read must
  never break a usage import that calls it best-effort.
* Recording is **transition-based** (see ``record_snapshots_transitionally``): a
  snapshot is written only when its state SIGNATURE differs from the most recent
  snapshot already recorded for the same stream. So an unchanged limit re-seen on
  every watch tick is a no-op, but a value that changes — including a decline or a
  window reset back to a previously-seen value — is always recorded with a fresh
  observation time, so ``agentacct limits`` shows the CURRENT reading, not a
  high-water mark. The signature deliberately excludes the observation time AND
  the volatile ``credits.balance`` (which drifts every scan) so neither mints a
  spurious snapshot; the full ``credits`` dict is still kept in metadata for
  display.
* Codex limits are account-wide (``limit_id == "codex"``), so the codex stream
  uses one stable ``run_id``; Claude's series is per-org.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

EVENT_TYPE = "rate_limit_observed"

CODEX_SOURCE = "codex-local-rate-limit"
CLAUDE_SOURCE = "claude-desktop-plan-usage"
CLAUDE_STATUSLINE_SOURCE = "claude-code-statusline"

CODEX_RUN_ID = "codex_rate_limit"
CLAUDE_STATUSLINE_RUN_ID = "claude_statusline"

# Snapshot origins (drive source + run_id so the feeds stay distinct streams).
ORIGIN_CODEX_SESSION = "codex_session"
ORIGIN_CLAUDE_PLAN_USAGE = "claude_plan_usage"
ORIGIN_CLAUDE_STATUSLINE = "claude_statusline"

# The terminal-CLI statusLine spool: a lightweight statusLine command
# (`python -m agentacct.statusline_hook`) writes the latest Claude rate-limit
# reading here, and the usage import reads it. Path follows the Claude config
# home (CLAUDE_CONFIG_DIR, else ~/.claude) so it is naturally isolated when that
# home is relocated; overridable via env for tests/custom setups.
STATUSLINE_SPOOL_ENV = "AGENTACCT_STATUSLINE_SPOOL"

# Canonical rolling-window lengths (minutes) used to label windows.
FIVE_HOUR_MINUTES = 300
SEVEN_DAY_MINUTES = 10080

# Env knob: reading the machine-GLOBAL provider files (the real ~/.codex when no
# codex_home is given, and the Claude desktop plan-usage file) can be disabled by
# setting this to a falsey value. The test suite pins it off so hermetic scans
# never read the developer's real machine; production leaves it on.
GLOBAL_LIMIT_SCAN_ENV = "AGENTACCT_SCAN_GLOBAL_LIMITS"
_FALSE_VALUES = {"0", "false", "no", "off"}


def global_limit_scan_enabled() -> bool:
    value = os.environ.get(GLOBAL_LIMIT_SCAN_ENV)
    if value is None:
        return True
    return value.strip().lower() not in _FALSE_VALUES


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateLimitWindow:
    """One provider rate-limit window (a rolling quota bucket)."""

    kind: str  # "5h" | "7d" | "other"
    used_percent: float
    window_minutes: int | None = None
    resets_at: int | None = None  # epoch seconds when the window resets, if known

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "used_percent": self.used_percent,
            "window_minutes": self.window_minutes,
            "resets_at": self.resets_at,
        }


@dataclass(frozen=True)
class RateLimitSnapshot:
    """A single provider-reported limit observation for one client.

    ``origin`` identifies which local source produced it — ``codex_session``,
    ``claude_plan_usage`` (the Claude desktop app's history file), or
    ``claude_statusline`` (the terminal-CLI statusLine spool). It drives the
    recorded event's ``source`` and per-stream ``run_id`` so the three feeds stay
    distinct streams that never collide.
    """

    client: str  # "codex" | "claude-code"
    windows: tuple[RateLimitWindow, ...]
    origin: str = "unknown"
    captured_at: float | None = None  # provider sample/event time, epoch seconds
    plan_type: str | None = None
    credits: Mapping[str, Any] | None = None
    reached_type: str | None = None
    org: str | None = None
    source_session_id: str | None = None
    source_file: str | None = None
    # Codex quota-bucket identity. Different ``limit_id`` values are DIFFERENT
    # quota buckets (default account bucket vs model-specific / Spark / rare
    # buckets) and must never be merged into one time series — see
    # ``snapshot_run_id``. ``limit_name`` is a mutable display label, kept for
    # provenance but never used as identity.
    limit_id: str | None = None
    limit_name: str | None = None


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):  # bool is an int subclass; never a window size
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # Guard non-finite (json.loads accepts bare Infinity/NaN): int(inf) and
        # int(nan) raise, which would abort the whole scan.
        if not math.isfinite(value):
            return None
        return int(value)
    return None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        # A non-finite percentage would produce non-conformant JSON and an
        # "inf%" render; drop it at the source.
        if not math.isfinite(number):
            return None
        return number
    return None


def _epoch_seconds(value: Any) -> float | None:
    """Best-effort conversion of a provider timestamp to epoch seconds.

    Codex writes ISO-8601 strings (``2026-07-25T10:37:02.336Z``); a numeric
    value is assumed to already be epoch seconds.
    """

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None
    return None


def _window_kind(window_minutes: int | None) -> str:
    if window_minutes == FIVE_HOUR_MINUTES:
        return "5h"
    if window_minutes == SEVEN_DAY_MINUTES:
        return "7d"
    return "other"


# ---------------------------------------------------------------------------
# normalization (pure)
# ---------------------------------------------------------------------------


def normalize_codex_rate_limits(
    rate_limits: Any,
    *,
    captured_at: Any = None,
    session_id: str | None = None,
    source_file: str | None = None,
) -> RateLimitSnapshot | None:
    """Turn a Codex ``payload.rate_limits`` object into a snapshot, or ``None``.

    Returns ``None`` when the object is missing/malformed or carries no window
    with a usable ``used_percent``.
    """

    if not isinstance(rate_limits, Mapping):
        return None
    windows: list[RateLimitWindow] = []
    for slot in ("primary", "secondary"):
        window = rate_limits.get(slot)
        if not isinstance(window, Mapping):
            continue
        used = _float_or_none(window.get("used_percent"))
        if used is None:
            continue
        window_minutes = _int_or_none(window.get("window_minutes"))
        windows.append(
            RateLimitWindow(
                kind=_window_kind(window_minutes),
                used_percent=used,
                window_minutes=window_minutes,
                resets_at=_int_or_none(window.get("resets_at")),
            )
        )
    if not windows:
        return None
    credits_raw = rate_limits.get("credits")
    credits = dict(credits_raw) if isinstance(credits_raw, Mapping) else None
    return RateLimitSnapshot(
        client="codex",
        windows=tuple(windows),
        origin=ORIGIN_CODEX_SESSION,
        captured_at=_epoch_seconds(captured_at),
        plan_type=_str_or_none(rate_limits.get("plan_type")),
        credits=credits,
        reached_type=_str_or_none(rate_limits.get("rate_limit_reached_type")),
        source_session_id=session_id,
        source_file=source_file,
        # limit_id / limit_name live at the rate_limits top level (not per
        # window); preserve them so buckets stay distinct streams.
        limit_id=_str_or_none(rate_limits.get("limit_id")),
        limit_name=_str_or_none(rate_limits.get("limit_name")),
    )


def normalize_claude_plan_usage_sample(
    sample: Any,
    *,
    source_file: str | None = None,
) -> RateLimitSnapshot | None:
    """Turn one ``plan-usage-history.json`` sample into a snapshot, or ``None``.

    A sample is ``{"t": <epoch ms>, "org": <id>, "u": {"fh": <5h %>, "sd": <7d %>}}``.
    """

    if not isinstance(sample, Mapping):
        return None
    usage = sample.get("u")
    if not isinstance(usage, Mapping):
        return None
    windows: list[RateLimitWindow] = []
    five_hour = _float_or_none(usage.get("fh"))
    if five_hour is not None:
        windows.append(
            RateLimitWindow(
                kind="5h",
                used_percent=five_hour,
                window_minutes=FIVE_HOUR_MINUTES,
                resets_at=None,
            )
        )
    seven_day = _float_or_none(usage.get("sd"))
    if seven_day is not None:
        windows.append(
            RateLimitWindow(
                kind="7d",
                used_percent=seven_day,
                window_minutes=SEVEN_DAY_MINUTES,
                resets_at=None,
            )
        )
    if not windows:
        return None
    millis = sample.get("t")
    captured_at = (
        float(millis) / 1000.0
        if isinstance(millis, (int, float)) and not isinstance(millis, bool) and math.isfinite(float(millis))
        else None
    )
    return RateLimitSnapshot(
        client="claude-code",
        windows=tuple(windows),
        origin=ORIGIN_CLAUDE_PLAN_USAGE,
        captured_at=captured_at,
        org=_str_or_none(sample.get("org")),
        source_file=source_file,
    )


def normalize_claude_statusline(
    payload: Any,
    *,
    captured_at: float | None = None,
    source_file: str | None = None,
) -> RateLimitSnapshot | None:
    """Turn a Claude Code statusLine payload into a snapshot, or ``None``.

    The statusLine JSON carries ``rate_limits.five_hour`` / ``.seven_day`` with
    ``used_percentage`` + ``resets_at`` (Pro/Max subscriptions only; absent for
    API-key auth). Unlike the desktop plan-usage file, this feed HAS reset times.
    """

    if not isinstance(payload, Mapping):
        return None
    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, Mapping):
        return None
    windows: list[RateLimitWindow] = []
    for key, kind, minutes in (
        ("five_hour", "5h", FIVE_HOUR_MINUTES),
        ("seven_day", "7d", SEVEN_DAY_MINUTES),
    ):
        window = rate_limits.get(key)
        if not isinstance(window, Mapping):
            continue
        used = _float_or_none(window.get("used_percentage"))
        if used is None:
            continue
        windows.append(
            RateLimitWindow(
                kind=kind,
                used_percent=used,
                window_minutes=minutes,
                resets_at=_int_or_none(window.get("resets_at")),
            )
        )
    if not windows:
        return None
    return RateLimitSnapshot(
        client="claude-code",
        windows=tuple(windows),
        origin=ORIGIN_CLAUDE_STATUSLINE,
        captured_at=_epoch_seconds(captured_at),
        source_file=source_file,
    )


# ---------------------------------------------------------------------------
# event building + transition dedup (pure)
# ---------------------------------------------------------------------------


def snapshot_run_id(snapshot: RateLimitSnapshot) -> str:
    """A stable per-stream run_id.

    Codex quota buckets are keyed by ``limit_id``: the default account bucket
    keeps the legacy ``CODEX_RUN_ID`` stream, but any OTHER ``limit_id``
    (model-specific / Spark / rare buckets) gets its own stream so a bucket with
    a different quota can never contaminate the default series' transition
    history (adversarial-review finding: two buckets sharing a window duration
    were merged into one meter). The Claude desktop plan-usage series is
    per-org; the terminal-CLI statusLine feed is its own stream. All are stable
    across scans so consecutive unchanged snapshots collapse to one event.
    """

    if snapshot.origin == ORIGIN_CODEX_SESSION or snapshot.client == "codex":
        # The default account-wide bucket keeps its legacy stream id so this fix
        # does not fork the existing healthy series (case-insensitive, so a
        # "Codex" casing variant does not orphan it). Only genuinely non-default
        # buckets branch into their own stream.
        if _is_default_codex_bucket(snapshot):
            return CODEX_RUN_ID
        return f"{CODEX_RUN_ID}:{snapshot.limit_id}"
    if snapshot.origin == ORIGIN_CLAUDE_STATUSLINE:
        return CLAUDE_STATUSLINE_RUN_ID
    org = snapshot.org or "unknown"
    return f"claude_plan_usage_{org}"


def _snapshot_source(snapshot: RateLimitSnapshot) -> str:
    if snapshot.origin == ORIGIN_CODEX_SESSION or snapshot.client == "codex":
        return CODEX_SOURCE
    if snapshot.origin == ORIGIN_CLAUDE_STATUSLINE:
        return CLAUDE_STATUSLINE_SOURCE
    return CLAUDE_SOURCE


def snapshot_state_signature(snapshot: RateLimitSnapshot) -> str:
    """Content hash over the limit-STATE values that matter.

    Excludes the observation time (so re-seeing the same state is a no-op) AND
    the volatile ``credits.balance`` (which drifts on every scan for pay-as-you-go
    accounts and would otherwise mint a snapshot every tick). Two snapshots with
    the same signature represent the same limit state; a change in any window
    percent/size/reset, plan, reached-state, or credit availability changes it.
    """

    payload = {
        "client": snapshot.client,
        "run_id": snapshot_run_id(snapshot),
        "windows": [
            [w.kind, w.used_percent, w.window_minutes, w.resets_at] for w in snapshot.windows
        ],
        "plan_type": snapshot.plan_type,
        "reached_type": snapshot.reached_type,
        # Only the STABLE credit flags, never the drifting balance.
        "credits": (
            [bool(snapshot.credits.get("has_credits")), bool(snapshot.credits.get("unlimited"))]
            if isinstance(snapshot.credits, Mapping)
            else None
        ),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]
    return f"rl_{digest}"


def snapshot_to_event(snapshot: RateLimitSnapshot) -> dict[str, Any]:
    """Build the ``record_event`` payload for a snapshot (no trusted flags).

    Carries a ``state_signature`` in metadata so the transition-dedup can tell
    whether a stream's state changed. Deliberately sets NO ``idempotency_key`` —
    dedup is transition-based (against the last recorded snapshot of the stream),
    not content-against-all-history, so a value that changes then returns is still
    recorded as the current state.
    """

    metadata: dict[str, Any] = {
        "client": snapshot.client,
        "origin": snapshot.origin,
        "captured_at": snapshot.captured_at,
        "windows": [w.to_dict() for w in snapshot.windows],
        "plan_type": snapshot.plan_type,
        "credits": dict(snapshot.credits) if snapshot.credits else None,
        "reached_type": snapshot.reached_type,
        "org": snapshot.org,
        "source_client_session_id": snapshot.source_session_id,
        "source_file": snapshot.source_file,
        # Bucket identity, persisted so a future per-epoch calibrator can keep
        # buckets distinct (§7.2 recording foundation). limit_id is identity;
        # limit_name is a mutable display label kept only for provenance.
        "limit_id": snapshot.limit_id,
        "limit_name": snapshot.limit_name,
        "state_signature": snapshot_state_signature(snapshot),
    }
    return {
        "source": _snapshot_source(snapshot),
        "event_type": EVENT_TYPE,
        "run_id": snapshot_run_id(snapshot),
        "provider": None,
        "model": None,
        "estimated_input_tokens": None,
        "estimated_output_tokens": None,
        "estimated_cost_usd": None,
        "usage_confidence": None,
        "cost_confidence": None,
        "metadata": metadata,
    }


def _event_ordering(event: Mapping[str, Any]) -> float:
    metadata = event.get("metadata") or {}
    captured = metadata.get("captured_at")
    if isinstance(captured, (int, float)) and not isinstance(captured, bool) and math.isfinite(float(captured)):
        return float(captured)
    created = event.get("created_at")
    return float(created) if isinstance(created, (int, float)) and not isinstance(created, bool) else 0.0


def record_snapshots_transitionally(
    snapshots: Iterable[RateLimitSnapshot],
    *,
    existing_events: Iterable[Mapping[str, Any]],
    record: Callable[[dict[str, Any]], Any],
) -> int:
    """Record each snapshot only if it changes its stream's latest state.

    ``existing_events`` is the current event list (any types; only
    ``rate_limit_observed`` are considered). ``record`` appends one event. Returns
    the number of events actually written. Unchanged consecutive states are a
    no-op; a changed state (including a decline or a reset back to a previously
    seen value) is written with its fresh observation time.
    """

    last_by_stream: dict[tuple[Any, Any], tuple[float, Any]] = {}
    for event in existing_events:
        if event.get("event_type") != EVENT_TYPE:
            continue
        metadata = event.get("metadata") or {}
        key = (event.get("source"), event.get("run_id"))
        ordering = _event_ordering(event)
        current = last_by_stream.get(key)
        if current is None or ordering >= current[0]:
            last_by_stream[key] = (ordering, metadata.get("state_signature"))

    written = 0
    for snapshot in snapshots:
        event = snapshot_to_event(snapshot)
        key = (event["source"], event["run_id"])
        signature = event["metadata"]["state_signature"]
        previous = last_by_stream.get(key)
        if previous is not None and previous[1] == signature:
            continue
        record(event)
        written += 1
        last_by_stream[key] = (_event_ordering(event), signature)
    return written


# ---------------------------------------------------------------------------
# readers (touch the filesystem; fail soft)
# ---------------------------------------------------------------------------


def _codex_sessions_roots(codex_home: Path | str | None = None) -> list[Path]:
    """Resolve codex ``sessions`` roots.

    An explicit ``codex_home`` is a single root. Otherwise ``CODEX_HOME`` is
    honored the same way the rest of agentacct does — a comma-separated list of
    roots — falling back to ``~/.codex``.
    """

    if codex_home is not None:
        return [Path(codex_home).expanduser() / "sessions"]
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        roots = [part.strip() for part in env_home.split(",") if part.strip()]
        if roots:
            return [Path(part).expanduser() / "sessions" for part in roots]
    return [Path.home() / ".codex" / "sessions"]


def default_codex_sessions_root(codex_home: Path | str | None = None) -> Path:
    """The first resolved codex sessions root (kept for callers/tests)."""

    return _codex_sessions_roots(codex_home)[0]


def default_claude_plan_usage_path() -> Path:
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Claude"
        / "plan-usage-history.json"
    )


def _claude_config_home() -> Path:
    """The Claude Code config home: the first CLAUDE_CONFIG_DIR entry, else ~/.claude.

    CLAUDE_CONFIG_DIR is comma-separated for the rest of agentacct (see
    source_paths), so we split the same way — NOT on os.pathsep — to stay
    consistent with the home the usage import actually scans.
    """

    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        parts = [p.strip() for p in configured.split(",") if p.strip()]
        if parts:
            return Path(parts[0]).expanduser()
    return Path.home() / ".claude"


def default_claude_statusline_spool_path() -> Path:
    """Where the statusLine hook writes, and the import reads, the latest Claude
    CLI rate-limit reading. Follows the Claude config home (CLAUDE_CONFIG_DIR else
    ~/.claude). Writer (the hook, run by Claude with its env) and reader (the
    usage import) agree only when they see the SAME CLAUDE_CONFIG_DIR; if a user
    relocates the home for `claude` but not for `agentacct watch`, the CLI feed is
    simply absent (fails soft), not wrong. Override with AGENTACCT_STATUSLINE_SPOOL."""

    override = os.environ.get(STATUSLINE_SPOOL_ENV)
    if override:
        return Path(override).expanduser()
    return _claude_config_home() / "agentacct" / "statusline-latest.json"


def write_claude_statusline_spool(
    rate_limits: Any,
    *,
    captured_at: float,
    path: Path | str | None = None,
) -> bool:
    """Atomically write the latest statusLine rate-limit reading to the spool.

    Best-effort: returns False on any failure (the statusLine command must never
    fail). Overwrites — the spool holds only the latest reading, ingested by the
    usage import.
    """

    target = Path(path).expanduser() if path is not None else default_claude_statusline_spool_path()
    document = {"v": 1, "captured_at": captured_at, "rate_limits": rate_limits}
    # A UNIQUE temp name per writer: several Claude Code sessions write this one
    # spool at ~300ms cadence, so a fixed temp name would let concurrent writers
    # clobber each other's temp and defeat the os.replace atomicity.
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(document), encoding="utf-8")
        os.replace(tmp, target)
        return True
    except (OSError, TypeError, ValueError):
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def read_claude_statusline_latest(
    path: Path | str | None = None,
) -> RateLimitSnapshot | None:
    """Read the statusLine spool into a snapshot, or ``None`` if absent/malformed."""

    target = Path(path).expanduser() if path is not None else default_claude_statusline_spool_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        document = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(document, Mapping):
        return None
    return normalize_claude_statusline(
        {"rate_limits": document.get("rate_limits")},
        captured_at=document.get("captured_at"),
        source_file=str(target),
    )


def _codex_session_id_from_path(path: Path) -> str | None:
    # rollout-2026-07-25T18-36-58-019f98d9-a3d7-7c73-91dc-90f8f31227d7.jsonl
    stem = path.stem
    if stem.startswith("rollout-"):
        return stem[len("rollout-") :] or None
    return stem or None


def _is_default_codex_bucket(snapshot: RateLimitSnapshot) -> bool:
    """Whether a snapshot is the default account-wide Codex bucket.

    ``limit_id`` absent or ``"codex"`` (case-insensitive) is the default plan
    meter; any other id is a distinct quota bucket (model-specific / Spark /
    premium). The match is case-insensitive so a provider casing variant
    (``"Codex"``) is not wrongly forked into its own stream, orphaning the
    legacy default series (adversarial-review finding).
    """

    limit_id = snapshot.limit_id
    return limit_id is None or limit_id.strip().lower() == "codex"


def _read_codex_file_latest_rate_limits(path: Path) -> RateLimitSnapshot | None:
    """The file's latest DEFAULT-bucket ``rate_limits`` snapshot, or None.

    Deliberately keeps the last snapshot whose ``limit_id`` is the default
    account bucket, NOT the last snapshot of any bucket. A rollout interleaves
    lines from every bucket the session touched, so the chronologically-last
    line can be a non-default bucket (e.g. a Spark turn); returning that as the
    account-wide meter would mislabel a model-specific quota as "the codex plan"
    (adversarial-review finding).

    Non-default buckets are currently DROPPED — this reader is the only producer
    of recorded codex limit snapshots, so today only the default account meter
    is recorded and shown. The ``limit_id`` preservation and the per-bucket
    ``snapshot_run_id`` branch are forward-compat scaffolding for a future
    per-bucket reader/calibrator (§7.2); they are not yet fed by any live path.
    """

    latest: RateLimitSnapshot | None = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                # Cheap pre-filter: only token_count lines carry rate_limits.
                if '"rate_limits"' not in line:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, Mapping):
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, Mapping):
                    continue
                try:
                    snapshot = normalize_codex_rate_limits(
                        payload.get("rate_limits"),
                        captured_at=obj.get("timestamp"),
                        session_id=_codex_session_id_from_path(path),
                        source_file=str(path),
                    )
                except Exception:  # noqa: BLE001 - one poison line must not abort the file/scan.
                    continue
                if snapshot is not None and _is_default_codex_bucket(snapshot):
                    latest = snapshot  # file is chronological; keep the last default-bucket line
    except OSError:
        return None
    return latest


def read_codex_rate_limits_latest(
    codex_home: Path | str | None = None,
    *,
    max_files: int = 8,
) -> RateLimitSnapshot | None:
    """The freshest account-wide Codex limit snapshot from recent session files.

    Selection is by **file modification time**, not the rollout's outer
    ``timestamp``. The outer timestamp is untrustworthy for freshness: a
    fork/replay copies history into a NEW file and the recorder stamps each
    copied ``TokenCount`` with a fresh ``now_utc()``, so a replayed OLD limit
    payload can carry a numerically LARGER outer timestamp than the genuine
    current one. Picking the global max over that timestamp (the previous
    behaviour) could therefore surface a stale, replayed snapshot as "latest".

    Filesystem mtime is not rewritten by replay: the active session's file is
    the most-recently-modified, and its LAST chronological DEFAULT-bucket
    ``rate_limits`` line is the genuine current account meter (replayed payloads
    sit in the copied prefix, never after the live turns). So we walk files
    newest-mtime first and return the first that yields a default-bucket
    snapshot. Only the DEFAULT account bucket feeds this account meter — a file
    whose last line is a non-default bucket (a Spark/model-specific turn) does
    not mislabel that bucket as the account plan. Returns ``None`` if no rollout
    carries a default-bucket rate-limit line.
    """

    rollouts: list[Path] = []
    for root in _codex_sessions_roots(codex_home):
        try:
            rollouts.extend(root.rglob("rollout-*.jsonl"))
        except OSError:
            continue
    if not rollouts:
        return None

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    # Secondary key (path) makes an exact-mtime tie deterministic instead of
    # depending on filesystem iteration order (adversarial-review finding).
    rollouts.sort(key=lambda p: (_mtime(p), str(p)), reverse=True)
    for path in rollouts[: max(1, max_files)]:
        try:
            snapshot = _read_codex_file_latest_rate_limits(path)
        except Exception:  # noqa: BLE001 - fail soft per the module contract.
            continue
        if snapshot is not None:
            return snapshot
    return None


def recorded_captured_ats(
    existing_events: Iterable[Mapping[str, Any]], *, client: str
) -> set[float]:
    """Every recorded ``captured_at`` for one client's limit stream.

    The series importer's exclusion set: an in-file reading whose capture time
    is already in the ledger is never imported again. Combined with the
    importer's DETERMINISTIC transition-collapse (same files -> same survivor
    set on every scan), this makes the backfill idempotent: readings BETWEEN
    the sparse tick-recorded snapshots import exactly once, and a re-scan is
    a no-op."""

    captured: set[float] = set()
    for event in existing_events:
        if event.get("event_type") != EVENT_TYPE:
            continue
        metadata = event.get("metadata") or {}
        if str(metadata.get("client") or "") != client:
            continue
        ordering = _event_ordering(event)
        if ordering > 0:
            captured.add(ordering)
    return captured


# Replayed rollout prefixes are stamped with FRESH outer timestamps
# milliseconds apart (the recorder re-stamps each copied TokenCount at replay
# time), so a burst of near-simultaneous readings is replay noise, not a real
# series: collapse each burst to its LAST line — the state at the fork point,
# which IS the genuine account state at that moment.
_CODEX_REPLAY_COLLAPSE_SECONDS = 5.0
# The series import backfills at most this far (the calibration window plus
# margin) — older history cannot influence the fit and only bloats the ledger.
_CODEX_SERIES_BACKFILL_DAYS = 30


def _read_codex_file_rate_limit_series(path: Path) -> list[RateLimitSnapshot]:
    """Every DEFAULT-bucket rate-limit snapshot in one rollout, chronological,
    with replay bursts collapsed. Fails soft to an empty list."""

    snapshots: list[RateLimitSnapshot] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if '"rate_limits"' not in line:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, Mapping):
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, Mapping):
                    continue
                try:
                    snapshot = normalize_codex_rate_limits(
                        payload.get("rate_limits"),
                        captured_at=obj.get("timestamp"),
                        session_id=_codex_session_id_from_path(path),
                        source_file=str(path),
                    )
                except Exception:  # noqa: BLE001 - one poison line must not abort the file.
                    continue
                if (
                    snapshot is not None
                    and _is_default_codex_bucket(snapshot)
                    and snapshot.captured_at is not None
                    and snapshot.captured_at > 0
                ):
                    snapshots.append(snapshot)
    except OSError:
        return []
    # Collapse bursts: keep a reading only when the NEXT one is not within the
    # replay window (the last of each burst always survives).
    collapsed: list[RateLimitSnapshot] = []
    for index, snapshot in enumerate(snapshots):
        if index + 1 < len(snapshots):
            gap = float(snapshots[index + 1].captured_at or 0) - float(snapshot.captured_at or 0)
            if 0 <= gap < _CODEX_REPLAY_COLLAPSE_SECONDS:
                continue
        collapsed.append(snapshot)
    return collapsed


def _snapshot_transition_key(snapshot: RateLimitSnapshot) -> str:
    """The series' transition-collapse identity — EXACTLY the recorder's own
    ``state_signature``, so any reading the recorder would treat as a no-op is
    dropped deterministically before the exclusion filter. A near-copy with a
    different field set (an early draft included the drifting credits balance
    the signature deliberately excludes) let recorder-skipped readings
    resurface on the next scan as phantom re-imports."""

    return snapshot_state_signature(snapshot)


def read_codex_rate_limits_series(
    codex_home: Path | str | None = None,
    *,
    exclude_captured_at: Iterable[float] = (),
    now: float | None = None,
    max_files: int = 400,
) -> list[RateLimitSnapshot]:
    """The DEFAULT-bucket weekly-meter series across recent rollouts, ascending.

    The calibration-density companion to ``read_codex_rate_limits_latest``:
    that reader records one snapshot per scan tick, which starves the weekly
    fit; this one BACKFILLS the in-file history between those sparse ticks.
    Bounds and idempotence:

    * only the backfill window (older history cannot influence the fit);
    * per-file replay bursts collapsed (see ``_CODEX_REPLAY_COLLAPSE_SECONDS``);
    * a DETERMINISTIC transition-collapse over the merged series (consecutive
      identical states drop), so every scan derives the same survivor set;
    * survivors whose capture time is already recorded (``exclude_captured_at``,
      from ``recorded_captured_ats``) are skipped — so the backfill happens
      once and a re-scan is a no-op.
    """

    import time as _time

    now_epoch = now if now is not None else _time.time()
    floor = now_epoch - _CODEX_SERIES_BACKFILL_DAYS * 86400.0
    rollouts: list[Path] = []
    for root in _codex_sessions_roots(codex_home):
        try:
            rollouts.extend(root.rglob("rollout-*.jsonl"))
        except OSError:
            continue

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    rollouts = [path for path in rollouts if _mtime(path) >= floor]
    rollouts.sort(key=lambda p: (_mtime(p), str(p)), reverse=True)
    series: list[RateLimitSnapshot] = []
    for path in rollouts[: max(1, max_files)]:
        try:
            file_series = _read_codex_file_rate_limit_series(path)
        except Exception:  # noqa: BLE001 - fail soft per the module contract.
            continue
        series.extend(
            snapshot
            for snapshot in file_series
            if float(snapshot.captured_at or 0) > floor
        )
    series.sort(key=lambda snapshot: float(snapshot.captured_at or 0))
    collapsed: list[RateLimitSnapshot] = []
    for snapshot in series:
        if collapsed and _snapshot_transition_key(collapsed[-1]) == _snapshot_transition_key(snapshot):
            continue
        collapsed.append(snapshot)
    excluded = {float(value) for value in exclude_captured_at}
    return [
        snapshot
        for snapshot in collapsed
        if float(snapshot.captured_at or 0) not in excluded
    ]


def read_claude_plan_usage_latest(
    path: Path | str | None = None,
) -> list[RateLimitSnapshot]:
    """The latest limit snapshot per org from the desktop app's usage history.

    Returns ``[]`` when the file is absent (non-desktop / non-macOS) or malformed.
    """

    target = Path(path).expanduser() if path is not None else default_claude_plan_usage_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    samples = data.get("samples") if isinstance(data, Mapping) else None
    if not isinstance(samples, Sequence):
        return []
    latest_by_org: dict[Any, Mapping[str, Any]] = {}
    for sample in samples:
        if not isinstance(sample, Mapping):
            continue
        org = sample.get("org")
        millis = sample.get("t")
        current = latest_by_org.get(org)
        if current is None:
            latest_by_org[org] = sample
            continue
        prev_millis = current.get("t")
        if (
            isinstance(millis, (int, float))
            and not isinstance(millis, bool)
            and (
                not isinstance(prev_millis, (int, float))
                or isinstance(prev_millis, bool)
                or millis >= prev_millis
            )
        ):
            latest_by_org[org] = sample
    snapshots: list[RateLimitSnapshot] = []
    for sample in latest_by_org.values():
        snapshot = normalize_claude_plan_usage_sample(sample, source_file=str(target))
        if snapshot is not None:
            snapshots.append(snapshot)
    return snapshots


__all__ = [
    "CLAUDE_SOURCE",
    "CLAUDE_STATUSLINE_RUN_ID",
    "CLAUDE_STATUSLINE_SOURCE",
    "CODEX_RUN_ID",
    "CODEX_SOURCE",
    "EVENT_TYPE",
    "FIVE_HOUR_MINUTES",
    "GLOBAL_LIMIT_SCAN_ENV",
    "ORIGIN_CLAUDE_PLAN_USAGE",
    "ORIGIN_CLAUDE_STATUSLINE",
    "ORIGIN_CODEX_SESSION",
    "SEVEN_DAY_MINUTES",
    "STATUSLINE_SPOOL_ENV",
    "RateLimitSnapshot",
    "RateLimitWindow",
    "default_claude_plan_usage_path",
    "default_claude_statusline_spool_path",
    "default_codex_sessions_root",
    "global_limit_scan_enabled",
    "normalize_claude_plan_usage_sample",
    "normalize_claude_statusline",
    "normalize_codex_rate_limits",
    "read_claude_plan_usage_latest",
    "read_claude_statusline_latest",
    "read_codex_rate_limits_latest",
    "record_snapshots_transitionally",
    "snapshot_run_id",
    "snapshot_state_signature",
    "snapshot_to_event",
    "write_claude_statusline_spool",
]
