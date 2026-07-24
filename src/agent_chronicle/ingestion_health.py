"""Durable, local-only health state for usage ingestion.

The usage ledger answers what was imported.  This module answers a different
question: whether the scanner that supplies that ledger is alive, current, and
honest about failures.  Health state is intentionally separate from evidence
and usage events so a failed scan cannot fabricate activity or cost.

The state file is owner-only and replaced atomically under an advisory lock.
Corrupt or torn state is projected as degraded instead of raising into an
importer or dashboard request; the next successful scan rebuilds it.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


INGESTION_HEALTH_SCHEMA_VERSION = "agent-chronicle.ingestion-health.v1"
INGESTION_HEALTH_DIRNAME = "ingestion-health"
INGESTION_HEALTH_FILENAME = "state.json"
EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE = "evidence_refreshable_usage_failed"

_SOURCE_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}")
_LEASE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_ERROR_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,119}")

_WATCHER_PID_ALIVE = "alive"
_WATCHER_PID_DEAD = "dead"
_WATCHER_PID_UNKNOWN = "unknown"

_ERROR_CODE_PRIORITY = (
    EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE,
    "cursor_state_db_filesystem_read_failed",
    "cursor_active_wal_not_supported",
    "cursor_composer_timestamp_invalid",
    "cursor_state_db_schema_unsupported",
    "cursor_state_db_corrupt",
    "cursor_composer_identity_mismatch",
    "hermes_multiple_source_homes_require_explicit_selection",
    "claude_transcript_unsafe_path",
    "claude_workflow_journal_schema_drift",
    "claude_workflow_journal_validation_truncated",
    "claude_transcript_identity_scan_truncated",
    "alias_migration_incomplete",
    "invalid_observation",
    "source_namespace_conflict",
    "source_watermark_unorderable",
    "same_watermark_conflict",
    "session_observation_conflict",
)

SESSION_OBSERVATION_CONFLICT_ERROR_CODES = (
    "source_namespace_conflict",
    "same_watermark_conflict",
    "source_watermark_unorderable",
    "invalid_observation",
)


def _owner_only(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        # Parent ACLs may still enforce privacy where chmod is unavailable.
        pass


def _now(value: float | None) -> float:
    return float(time.time() if value is None else value)


def _pid_is_dead(pid: int) -> bool:
    """True only when the process provably does not exist.

    A permission error means the PID is alive under another user; any other
    failure is treated as alive so a scan registration is never reaped on
    ambiguous evidence. PID recycling can only make a dead scanner look
    alive, which merely defers cleanup to the stale-after heuristic.
    """

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(parsed, 0)


def _optional_nonnegative_int(value: Any) -> int | None:
    return None if value is None else _nonnegative_int(value)


def _positive_float(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return parsed if parsed > 0 else fallback


def _source_name(value: str) -> str:
    text = str(value or "").strip().lower()
    if not _SOURCE_RE.fullmatch(text):
        raise ValueError("source must be a short lowercase identifier")
    return text


def _source_names(values: Sequence[str]) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(_source_name(value) for value in values))
    if not names:
        raise ValueError("at least one source is required")
    return names


def _lease_id(value: str) -> str:
    text = str(value or "").strip()
    if not _LEASE_RE.fullmatch(text):
        raise ValueError("lease_id is invalid")
    return text


def _error_code(value: str) -> str:
    text = str(value or "").strip().lower().replace(" ", "-")
    if not _ERROR_RE.fullmatch(text):
        return "scan_failed"
    return text


def session_observation_conflict_error_code(reason: Any) -> str:
    """Map a service conflict reason onto the stable health/CLI vocabulary.

    The importer should only see the four reasons below.  Treating an unknown
    service reason as ``invalid_observation`` fails closed without leaking an
    arbitrary exception string into the durable health schema.
    """

    normalized = _error_code(str(reason or ""))
    if normalized in SESSION_OBSERVATION_CONFLICT_ERROR_CODES:
        return normalized
    return "invalid_observation"


def _session_observation_conflict_reasons(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    return {
        code: count
        for code in SESSION_OBSERVATION_CONFLICT_ERROR_CODES
        if (count := _nonnegative_int(value.get(code))) > 0
    }


def _error_codes(values: Any, *, fallback: Any = None) -> list[str]:
    raw_values = values if isinstance(values, list) else []
    normalized: list[str] = []
    for value in raw_values[:16]:
        if not isinstance(value, str) or not value.strip():
            continue
        code = _error_code(value)
        if code not in normalized:
            normalized.append(code)
    if not normalized and fallback:
        normalized.append(_error_code(str(fallback)))
    return normalized


def _primary_error_code(codes: Sequence[str], *, fallback: str = "parse_error") -> str:
    for preferred in _ERROR_CODE_PRIORITY:
        if preferred in codes:
            return preferred
    return str(codes[0]) if codes else fallback


def _short_text(value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:maximum]


def _watcher_pid_liveness(value: Any) -> str:
    """Conservatively classify a recorded process PID without signalling it.

    ``kill(pid, 0)`` performs an existence/permission probe only.  Anything
    except an explicit ``ESRCH`` remains unknown-or-alive, because runtime
    activation or repair must never clear durable state merely because process
    inspection is unavailable.
    """

    pid = _nonnegative_int(value)
    if pid <= 0:
        return _WATCHER_PID_UNKNOWN
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return _WATCHER_PID_DEAD
    except (OSError, OverflowError, ValueError):
        return _WATCHER_PID_UNKNOWN
    return _WATCHER_PID_ALIVE


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": INGESTION_HEALTH_SCHEMA_VERSION,
        "updated_at": None,
        "sources": {},
        "active_scans": {},
        "watcher": None,
    }


def _validate_optional_number(container: Mapping[str, Any], key: str) -> None:
    value = container.get(key)
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"health state field {key} is not a finite number")


def _validate_string_list(container: Mapping[str, Any], key: str) -> None:
    value = container.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"health state field {key} is not a string list")


def _validate_persisted_state(state: Mapping[str, Any]) -> None:
    """Reject schema-shaped but unsafe scalar values before projection.

    The JSON decoder only proves syntax.  This validation keeps a manually
    edited, partially upgraded, or torn-but-parseable state file from turning
    read-only health endpoints into 500s.
    """

    _validate_optional_number(state, "updated_at")
    sources = state.get("sources")
    active_scans = state.get("active_scans")
    if not isinstance(sources, Mapping) or not isinstance(active_scans, Mapping):
        raise ValueError("health state collections are invalid")
    timestamp_fields = ("last_attempt_at", "last_completed_at", "last_success_at", "last_failure_at")
    for source, receipt in sources.items():
        if not isinstance(source, str) or not isinstance(receipt, Mapping):
            raise ValueError("health source receipt is invalid")
        for field in timestamp_fields:
            _validate_optional_number(receipt, field)
        if "error_codes" in receipt:
            _validate_string_list(receipt, "error_codes")
        if "session_observation_conflict_reasons" in receipt:
            reasons = receipt.get("session_observation_conflict_reasons")
            if not isinstance(reasons, Mapping) or any(
                key not in SESSION_OBSERVATION_CONFLICT_ERROR_CODES
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for key, value in reasons.items()
            ):
                raise ValueError("health observation conflict reasons are invalid")
    for scan in active_scans.values():
        if not isinstance(scan, Mapping):
            raise ValueError("health active scan is invalid")
        _validate_optional_number(scan, "started_at")
        _validate_string_list(scan, "sources")
    watcher = state.get("watcher")
    if watcher is not None:
        if not isinstance(watcher, Mapping):
            raise ValueError("health watcher state is invalid")
        for field in ("started_at", "heartbeat_at", "stopped_at", "interval_seconds"):
            _validate_optional_number(watcher, field)
        _validate_string_list(watcher, "sources")


def health_scan_results(diagnostics: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Reduce rich source diagnostics to the durable receipt vocabulary."""

    results: dict[str, dict[str, Any]] = {}
    for source, diagnostic in diagnostics.items():
        error_codes = _error_codes(diagnostic.get("error_codes"))
        observation_conflict_reasons = _session_observation_conflict_reasons(
            diagnostic.get("session_observation_conflict_reasons")
        )
        for code in observation_conflict_reasons:
            if code not in error_codes:
                error_codes.append(code)
        if observation_conflict_reasons and "session_observation_conflict" not in error_codes:
            error_codes.append("session_observation_conflict")
        incomplete_alias_migrations = _nonnegative_int(
            diagnostic.get("incomplete_alias_migrations")
        )
        if (
            incomplete_alias_migrations
            and "alias_migration_incomplete" not in error_codes
        ):
            error_codes.append("alias_migration_incomplete")
        results[str(source)] = {
            "discovered": diagnostic.get("discovered", 0),
            "parsed": diagnostic.get("parsed", 0),
            "skipped": diagnostic.get("skipped", 0),
            "error_count": diagnostic.get("error_count", 0),
            "error_code": (
                _primary_error_code(error_codes)
            ),
            "error_codes": error_codes,
            "watermark": diagnostic.get("watermark"),
            "limit_unit": diagnostic.get("limit_unit"),
            "selected_root_groups": diagnostic.get("selected_root_groups"),
            "returned_root_groups": diagnostic.get("returned_root_groups"),
            "returned_rows": diagnostic.get("returned_rows", 0),
            "excluded_by_limit": diagnostic.get("excluded_by_limit", 0),
            "ignored_non_transcript_files": diagnostic.get(
                "ignored_non_transcript_files", 0
            ),
            "unresolved_identity_files": diagnostic.get(
                "unresolved_identity_files", 0
            ),
            "excluded_by_source_namespace": diagnostic.get("excluded_by_source_namespace", 0),
            "source_namespace_conflicts": diagnostic.get("source_namespace_conflicts", 0),
            "source_namespace_adoptions": diagnostic.get("source_namespace_adoptions", 0),
            "concurrent_refresh_conflicts": diagnostic.get("concurrent_refresh_conflicts", 0),
            "incomplete_alias_migrations": incomplete_alias_migrations,
            "unparsed_selected_rows": diagnostic.get("unparsed_selected_rows", 0),
            "observed_sessions": diagnostic.get("observed_sessions", 0),
            "usage_sessions": diagnostic.get("usage_sessions", 0),
            "sessions_without_usage": diagnostic.get("sessions_without_usage", 0),
            "session_observation_conflicts": diagnostic.get(
                "session_observation_conflicts",
                0,
            ),
            "session_observation_conflict_reasons": observation_conflict_reasons,
        }
    return results


def apply_evidence_refreshable_usage_health(
    results: Mapping[str, Mapping[str, Any]],
    *,
    sources: Sequence[str],
    outcome: Any,
) -> dict[str, dict[str, Any]]:
    """Apply a global post-persist Evidence failure to this scan's sources.

    Usage discovery diagnostics are source-shaped, while the complete Evidence
    current-usage reconcile happens once after the v1 ledger is safely saved.
    Keep that reconcile global in the payload, but project any failure onto
    every configured source receipt so ingestion health cannot remain green (or
    report ``issues=[]``) after the derived Evidence write failed.  A later
    clean scan simply writes clean source receipts and therefore self-heals.
    """

    rendered = {
        str(source): dict(result)
        for source, result in results.items()
        if isinstance(result, Mapping)
    }
    if not evidence_refreshable_usage_failed(outcome):
        return rendered

    for source in _source_names(sources):
        result = rendered.setdefault(source, {})
        error_codes = _error_codes(result.get("error_codes"))
        if EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE not in error_codes:
            error_codes.append(EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE)
        result["error_count"] = max(_nonnegative_int(result.get("error_count")), 1)
        result["error_code"] = EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE
        result["error_codes"] = error_codes
    return rendered


def evidence_refreshable_usage_failed(outcome: Any) -> bool:
    """Return whether a post-persist Evidence reconcile needs health warning."""

    if not isinstance(outcome, Mapping):
        return True
    if outcome.get("enabled") is False:
        return False
    if outcome.get("errors"):
        return True
    if _nonnegative_int(outcome.get("conflicts")) or _nonnegative_int(
        outcome.get("existing_conflicts")
    ):
        return True
    return bool(
        outcome.get("complete_requested") is True
        and outcome.get("complete_applied") is not True
    )


def importer_build_id() -> str:
    """Version plus a short source fingerprint for long-running dev watchers."""

    try:
        version = package_version("agentacct")
    except PackageNotFoundError:
        try:
            version = package_version("agent-chronicle")
        except PackageNotFoundError:
            version = "development"
    digest = hashlib.sha256()
    for filename in (
        "client_usage.py",
        "cli.py",
        "ingestion_health.py",
        "service.py",
        "usage_truth.py",
    ):
        path = Path(__file__).with_name(filename)
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(filename.encode("utf-8"))
    return f"{version}+{digest.hexdigest()[:12]}"


@dataclass(frozen=True)
class WatcherAcquireResult:
    acquired: bool
    reason: str
    active_lease_id: str | None = None
    superseded_lease_id: str | None = None


class IngestionHealthStore:
    """Atomic per-store receipts and watcher lease state."""

    def __init__(self, store_dir: Path | str):
        self.store_dir = Path(store_dir).expanduser()
        self.health_root = self.store_dir / INGESTION_HEALTH_DIRNAME
        self.state_path = self.health_root / INGESTION_HEALTH_FILENAME
        self.lock_path = self.health_root / ".state.lock"

    def _ensure_root(self) -> None:
        self.health_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _owner_only(self.health_root, 0o700)

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        self._ensure_root()
        with self.lock_path.open("a+b") as handle:
            _owner_only(self.lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not self.state_path.exists():
            return _empty_state(), []
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("health state is not an object")
            if value.get("schema_version") != INGESTION_HEALTH_SCHEMA_VERSION:
                raise ValueError("health state schema mismatch")
            if not isinstance(value.get("sources"), dict) or not isinstance(value.get("active_scans"), dict):
                raise ValueError("health state collections are invalid")
            if value.get("watcher") is not None and not isinstance(value.get("watcher"), dict):
                raise ValueError("health watcher state is invalid")
            _validate_persisted_state(value)
            return value, []
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            return _empty_state(), [
                {
                    "code": "health_state_corrupt",
                    "source": None,
                    "action": "Run a fresh usage scan to rebuild sync health.",
                }
            ]

    def _write_unlocked(self, state: Mapping[str, Any]) -> None:
        self._ensure_root()
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        temp_path = self.health_root / f".{INGESTION_HEALTH_FILENAME}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.state_path)
            _owner_only(self.state_path, 0o600)
            directory_fd = os.open(self.health_root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def begin_scan(
        self,
        *,
        sources: Sequence[str],
        scan_limit: int,
        importer_version: str,
        pid: int | None = None,
        started_at: float | None = None,
    ) -> str:
        names = _source_names(sources)
        timestamp = _now(started_at)
        scan_id = f"scan_{uuid.uuid4().hex}"
        with self._locked(exclusive=True):
            state, _issues = self._read_unlocked()
            active_scans = state.setdefault("active_scans", {})
            active_scans[scan_id] = {
                "scan_id": scan_id,
                "sources": list(names),
                "started_at": timestamp,
                "scan_limit": _nonnegative_int(scan_limit),
                "importer_version": _short_text(importer_version, 120) or "unknown",
                "pid": _nonnegative_int(os.getpid() if pid is None else pid),
            }
            receipts = state.setdefault("sources", {})
            for source in names:
                previous = receipts.get(source) if isinstance(receipts.get(source), dict) else {}
                receipts[source] = {
                    **previous,
                    "source": source,
                    "last_attempt_at": timestamp,
                    "last_scan_id": scan_id,
                    "scan_state": "running",
                    "scan_limit": _nonnegative_int(scan_limit),
                    "importer_version": _short_text(importer_version, 120) or "unknown",
                    "pid": _nonnegative_int(os.getpid() if pid is None else pid),
                }
            state["updated_at"] = timestamp
            self._write_unlocked(state)
        return scan_id

    def complete_scan(
        self,
        scan_id: str,
        *,
        results: Mapping[str, Mapping[str, Any]],
        completed_at: float | None = None,
    ) -> None:
        timestamp = _now(completed_at)
        with self._locked(exclusive=True):
            state, _issues = self._read_unlocked()
            active_scans = state.setdefault("active_scans", {})
            scan = active_scans.get(scan_id)
            if not isinstance(scan, dict):
                raise ValueError("scan_id is not active")
            configured_sources = tuple(str(value) for value in scan.get("sources") or ())
            receipts = state.setdefault("sources", {})
            for source in configured_sources:
                result = results.get(source) if isinstance(results.get(source), Mapping) else {}
                error_count = _nonnegative_int(result.get("error_count"))
                result_error_codes = _error_codes(
                    result.get("error_codes"),
                    fallback=(result.get("error_code") or "parse_error")
                    if error_count
                    else None,
                )
                explicit_error_code = (
                    _error_code(str(result.get("error_code")))
                    if error_count and result.get("error_code")
                    else None
                )
                if (
                    explicit_error_code is not None
                    and explicit_error_code not in result_error_codes
                ):
                    result_error_codes.append(explicit_error_code)
                previous = receipts.get(source) if isinstance(receipts.get(source), dict) else {}
                # Manual refreshes may overlap the watcher. A slower, older
                # scan must never overwrite the receipt of a newer attempt
                # merely because it completed later.
                if previous.get("last_scan_id") not in {None, scan_id}:
                    continue
                receipt = {
                    **previous,
                    "source": source,
                    "last_attempt_at": float(scan.get("started_at") or timestamp),
                    "last_completed_at": timestamp,
                    "scan_state": "success" if error_count == 0 else "failed",
                    "discovered": _nonnegative_int(result.get("discovered")),
                    "parsed": _nonnegative_int(result.get("parsed")),
                    "skipped": _nonnegative_int(result.get("skipped")),
                    "error_count": error_count,
                    "error_code": (
                        _primary_error_code(result_error_codes)
                        if error_count
                        else None
                    ),
                    "error_codes": result_error_codes if error_count else [],
                    "scan_limit": _nonnegative_int(scan.get("scan_limit")),
                    "watermark": _short_text(result.get("watermark"), 240),
                    "limit_unit": _short_text(result.get("limit_unit"), 40) or "rows",
                    "selected_root_groups": _optional_nonnegative_int(result.get("selected_root_groups")),
                    "returned_root_groups": _optional_nonnegative_int(
                        result.get("returned_root_groups")
                    ),
                    "returned_rows": _nonnegative_int(result.get("returned_rows")),
                    "excluded_by_limit": _nonnegative_int(result.get("excluded_by_limit")),
                    "ignored_non_transcript_files": _nonnegative_int(
                        result.get("ignored_non_transcript_files")
                    ),
                    "unresolved_identity_files": _nonnegative_int(
                        result.get("unresolved_identity_files")
                    ),
                    "excluded_by_source_namespace": _nonnegative_int(
                        result.get("excluded_by_source_namespace")
                    ),
                    "source_namespace_conflicts": _nonnegative_int(
                        result.get("source_namespace_conflicts")
                    ),
                    "source_namespace_adoptions": _nonnegative_int(
                        result.get("source_namespace_adoptions")
                    ),
                    "concurrent_refresh_conflicts": _nonnegative_int(
                        result.get("concurrent_refresh_conflicts")
                    ),
                    "incomplete_alias_migrations": _nonnegative_int(
                        result.get("incomplete_alias_migrations")
                    ),
                    "unparsed_selected_rows": _nonnegative_int(result.get("unparsed_selected_rows")),
                    "observed_sessions": _nonnegative_int(result.get("observed_sessions")),
                    "usage_sessions": _nonnegative_int(result.get("usage_sessions")),
                    "sessions_without_usage": _nonnegative_int(
                        result.get("sessions_without_usage")
                    ),
                    "session_observation_conflicts": _nonnegative_int(
                        result.get("session_observation_conflicts")
                    ),
                    "session_observation_conflict_reasons": (
                        _session_observation_conflict_reasons(
                            result.get("session_observation_conflict_reasons")
                        )
                    ),
                    "importer_version": _short_text(scan.get("importer_version"), 120) or "unknown",
                    "pid": _nonnegative_int(scan.get("pid")),
                }
                if error_count == 0:
                    receipt["last_success_at"] = timestamp
                    receipt["consecutive_failures"] = 0
                    receipt["last_failure_at"] = previous.get("last_failure_at")
                else:
                    receipt["last_success_at"] = previous.get("last_success_at")
                    receipt["last_failure_at"] = timestamp
                    receipt["consecutive_failures"] = _nonnegative_int(previous.get("consecutive_failures")) + 1
                receipts[source] = receipt
            active_scans.pop(scan_id, None)
            state["updated_at"] = timestamp
            self._write_unlocked(state)

    def fail_scan(self, scan_id: str, *, error_code: str, failed_at: float | None = None) -> None:
        timestamp = _now(failed_at)
        with self._locked(exclusive=True):
            state, _issues = self._read_unlocked()
            active_scans = state.setdefault("active_scans", {})
            scan = active_scans.get(scan_id)
            if not isinstance(scan, dict):
                return
            receipts = state.setdefault("sources", {})
            for source in scan.get("sources") or ():
                name = _source_name(str(source))
                previous = receipts.get(name) if isinstance(receipts.get(name), dict) else {}
                if previous.get("last_scan_id") not in {None, scan_id}:
                    continue
                receipts[name] = {
                    **previous,
                    "source": name,
                    "last_attempt_at": float(scan.get("started_at") or timestamp),
                    "last_completed_at": timestamp,
                    "last_success_at": previous.get("last_success_at"),
                    "last_failure_at": timestamp,
                    "scan_state": "failed",
                    "error_count": max(_nonnegative_int(previous.get("error_count")), 1),
                    "error_code": _error_code(error_code),
                    "error_codes": [_error_code(error_code)],
                    "scan_limit": _nonnegative_int(scan.get("scan_limit")),
                    "importer_version": _short_text(scan.get("importer_version"), 120) or "unknown",
                    "pid": _nonnegative_int(scan.get("pid")),
                    "consecutive_failures": _nonnegative_int(previous.get("consecutive_failures")) + 1,
                }
            active_scans.pop(scan_id, None)
            state["updated_at"] = timestamp
            self._write_unlocked(state)

    @staticmethod
    def _watcher_stale_after(watcher: Mapping[str, Any]) -> float:
        return max(_positive_float(watcher.get("interval_seconds"), 60.0) * 3.0, 30.0)

    def acquire_watcher(
        self,
        *,
        lease_id: str,
        pid: int,
        importer_version: str,
        interval_seconds: float,
        scan_limit: int,
        sources: Sequence[str],
        now: float | None = None,
    ) -> WatcherAcquireResult:
        lease = _lease_id(lease_id)
        names = _source_names(sources)
        timestamp = _now(now)
        with self._locked(exclusive=True):
            state, _issues = self._read_unlocked()
            existing = state.get("watcher") if isinstance(state.get("watcher"), dict) else None
            if existing and existing.get("state") == "running":
                existing_lease = str(existing.get("lease_id") or "") or None
                heartbeat = float(existing.get("heartbeat_at") or existing.get("started_at") or 0.0)
                is_fresh = timestamp - heartbeat <= self._watcher_stale_after(existing)
                if is_fresh and existing_lease != lease:
                    return WatcherAcquireResult(
                        acquired=False,
                        reason="active_watcher_exists",
                        active_lease_id=existing_lease,
                    )
                if is_fresh and existing_lease == lease:
                    return WatcherAcquireResult(acquired=True, reason="already_owned", active_lease_id=lease)
                reason = "stale_watcher_replaced"
                superseded = existing_lease
                superseded_pid = _nonnegative_int(existing.get("pid"))
            else:
                reason = "acquired"
                superseded = None
                superseded_pid = 0
            # A terminated watcher (or manual scan process) can leave its
            # in-flight scan registration behind forever: a clean stop/start
            # never used to reap it, so the health surface reported
            # ``scan_stuck`` permanently and its "Restart usage watch" action
            # could not clear it. Retire scans owned by the superseded stale
            # PID (which may still be alive but hung) and scans whose owning
            # process is provably dead. Scans without a recorded PID are left
            # for the stale-after heuristic — never guessed away.
            active_scans = state.setdefault("active_scans", {})
            for active_scan_id, active_scan in list(active_scans.items()):
                if not isinstance(active_scan, Mapping):
                    continue
                scan_pid = _nonnegative_int(active_scan.get("pid"))
                if scan_pid <= 0:
                    continue
                if (superseded_pid > 0 and scan_pid == superseded_pid) or _pid_is_dead(
                    scan_pid
                ):
                    active_scans.pop(active_scan_id, None)
            state["watcher"] = {
                "state": "running",
                "lease_id": lease,
                "pid": _nonnegative_int(pid),
                "importer_version": _short_text(importer_version, 120) or "unknown",
                "interval_seconds": _positive_float(interval_seconds, 60.0),
                "scan_limit": _nonnegative_int(scan_limit),
                "sources": list(names),
                "started_at": timestamp,
                "heartbeat_at": timestamp,
            }
            state["updated_at"] = timestamp
            self._write_unlocked(state)
            return WatcherAcquireResult(
                acquired=True,
                reason=reason,
                active_lease_id=lease,
                superseded_lease_id=superseded,
            )

    def heartbeat_watcher(self, lease_id: str, *, now: float | None = None) -> bool:
        lease = _lease_id(lease_id)
        timestamp = _now(now)
        with self._locked(exclusive=True):
            state, _issues = self._read_unlocked()
            watcher = state.get("watcher") if isinstance(state.get("watcher"), dict) else None
            if not watcher or watcher.get("state") != "running" or watcher.get("lease_id") != lease:
                return False
            watcher["heartbeat_at"] = timestamp
            state["updated_at"] = timestamp
            self._write_unlocked(state)
            return True

    def release_watcher(self, lease_id: str, *, now: float | None = None) -> bool:
        lease = _lease_id(lease_id)
        timestamp = _now(now)
        with self._locked(exclusive=True):
            state, _issues = self._read_unlocked()
            watcher = state.get("watcher") if isinstance(state.get("watcher"), dict) else None
            if not watcher or watcher.get("lease_id") != lease:
                return False
            watcher["state"] = "stopped"
            watcher["stopped_at"] = timestamp
            watcher["heartbeat_at"] = timestamp
            state["updated_at"] = timestamp
            self._write_unlocked(state)
            return True

    def repair_dead_active_scans(self, *, now: float | None = None) -> tuple[str, ...]:
        """Retire only active scans whose recorded process is definitively gone.

        This is an explicit repair operation, separate from :meth:`snapshot`,
        so health reads never mutate durable state.  PID inspection happens
        outside the store lock, then the exclusive phase removes only the
        exact scan records that were inspected.  A live PID, an invalid or
        missing PID, an inspection error, or a concurrently replaced record is
        preserved.
        """

        if not self.health_root.exists():
            return ()
        with self._locked(exclusive=False):
            state, issues = self._read_unlocked()
        if issues:
            return ()
        active_scans = state.get("active_scans")
        if not isinstance(active_scans, Mapping):
            return ()
        observed = {
            scan_id: dict(scan)
            for scan_id, scan in active_scans.items()
            if isinstance(scan_id, str) and isinstance(scan, Mapping)
        }
        dead = {
            scan_id: scan
            for scan_id, scan in observed.items()
            if _watcher_pid_liveness(scan.get("pid")) == _WATCHER_PID_DEAD
        }
        if not dead:
            return ()

        timestamp = _now(now)
        removed: list[str] = []
        with self._locked(exclusive=True):
            state, issues = self._read_unlocked()
            if issues:
                return ()
            active_scans = state.setdefault("active_scans", {})
            for scan_id, inspected_scan in dead.items():
                current_scan = active_scans.get(scan_id)
                if isinstance(current_scan, Mapping) and dict(current_scan) == inspected_scan:
                    active_scans.pop(scan_id, None)
                    removed.append(scan_id)
            if removed:
                state["updated_at"] = timestamp
                self._write_unlocked(state)
        return tuple(removed)

    def runtime_watcher_snapshot(self) -> tuple[dict[str, Any], bool]:
        """Return runtime health after safely retiring a definitively dead watcher.

        A fresh heartbeat is not process-liveness proof: an abruptly terminated
        watcher can leave both fields behind.  Only a definitive missing-PID
        observation may retire that exact recorded lease.  A live PID, an
        invalid/missing PID, or an inspection error stays external and is never
        signalled by this reconciliation path.
        """

        snapshot = self.snapshot()
        watcher = snapshot.get("watcher") if isinstance(snapshot.get("watcher"), Mapping) else {}
        if watcher.get("state") != "running":
            return snapshot, False
        if _watcher_pid_liveness(watcher.get("pid")) != _WATCHER_PID_DEAD:
            return snapshot, True

        lease_id = str(watcher.get("lease_id") or "")
        if not _LEASE_RE.fullmatch(lease_id):
            # Without a valid exact lease identifier, clearing state would
            # broaden authority beyond the dead process observation.
            return snapshot, True
        self.release_watcher(lease_id)
        refreshed = self.snapshot()
        current = refreshed.get("watcher") if isinstance(refreshed.get("watcher"), Mapping) else {}
        return refreshed, current.get("state") == "running"

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        timestamp = _now(now)
        # A health read must stay read-only on a store that has never run an
        # importer.  The locked path creates the private directory and lock
        # file, which is appropriate for writers but surprising for GET
        # /health, ``usage health``, and dry-run checks.
        if not self.health_root.exists():
            return {
                "schema_version": INGESTION_HEALTH_SCHEMA_VERSION,
                "state": "unknown",
                "last_success_at": None,
                "active_scan_count": 0,
                "sources": [],
                "watcher": {"state": "not_configured"},
                "issues": [],
            }
        with self._locked(exclusive=False):
            state, issues = self._read_unlocked()
        if issues:
            return {
                "schema_version": INGESTION_HEALTH_SCHEMA_VERSION,
                "state": "degraded",
                "last_success_at": None,
                "active_scan_count": 0,
                "sources": [],
                "watcher": {"state": "not_configured"},
                "issues": issues,
            }

        watcher = state.get("watcher") if isinstance(state.get("watcher"), dict) else None
        watcher_projection: dict[str, Any]
        watcher_stale_after = 180.0
        watcher_is_fresh = False
        if watcher:
            watcher_stale_after = self._watcher_stale_after(watcher)
            heartbeat = float(watcher.get("heartbeat_at") or watcher.get("started_at") or 0.0)
            watcher_is_fresh = watcher.get("state") == "running" and timestamp - heartbeat <= watcher_stale_after
            watcher_state = "running" if watcher_is_fresh else "stale" if watcher.get("state") == "running" else "stopped"
            watcher_projection = {
                "state": watcher_state,
                "lease_id": watcher.get("lease_id"),
                "pid": _nonnegative_int(watcher.get("pid")),
                "importer_version": _short_text(watcher.get("importer_version"), 120) or "unknown",
                "interval_seconds": _positive_float(watcher.get("interval_seconds"), 60.0),
                "scan_limit": _nonnegative_int(watcher.get("scan_limit")),
                "sources": list(watcher.get("sources") or ()),
                "started_at": watcher.get("started_at"),
                "heartbeat_at": watcher.get("heartbeat_at"),
            }
            if watcher_state == "stale":
                issues.append(
                    {"code": "watcher_stale", "source": None, "action": "Restart usage watch."}
                )
            stored_version = str(watcher.get("importer_version") or "")
            current_version = importer_build_id()
            watcher_projection["current_importer_version"] = current_version
            if watcher_state == "running" and "+" in stored_version and stored_version != current_version:
                issues.append(
                    {
                        "code": "watcher_version_mismatch",
                        "source": None,
                        "action": "Restart usage watch to load the current importer.",
                    }
                )
        else:
            watcher_projection = {"state": "not_configured"}

        active_scans = state.get("active_scans") if isinstance(state.get("active_scans"), dict) else {}
        for scan in active_scans.values():
            if not isinstance(scan, Mapping):
                continue
            started_at = float(scan.get("started_at") or timestamp)
            if timestamp - started_at > watcher_stale_after:
                issues.append(
                    {
                        "code": "scan_stuck",
                        "source": None,
                        "action": "Restart usage watch.",
                    }
                )
                break

        configured_sources = {
            str(source)
            for source in (watcher.get("sources") if watcher and isinstance(watcher.get("sources"), list) else [])
            if source
        }
        watcher_started_at = float(watcher.get("started_at") or 0.0) if watcher else 0.0
        watcher_importer_version = str(watcher.get("importer_version") or "") if watcher else ""
        source_rows: list[dict[str, Any]] = []
        last_success_values: list[float] = []
        receipts = state.get("sources") if isinstance(state.get("sources"), dict) else {}
        for source in sorted(set(receipts) | configured_sources):
            receipt = receipts.get(source) if isinstance(receipts.get(source), dict) else {}
            last_success = receipt.get("last_success_at")
            last_failure = receipt.get("last_failure_at")
            failed_after_success = bool(last_failure is not None and (last_success is None or float(last_failure) > float(last_success)))
            error_count = _nonnegative_int(receipt.get("error_count"))
            receipt_error_codes = _error_codes(
                receipt.get("error_codes"),
                fallback=receipt.get("error_code") if error_count else None,
            )
            namespace_conflicts = _nonnegative_int(receipt.get("source_namespace_conflicts"))
            incomplete_alias_migrations = _nonnegative_int(
                receipt.get("incomplete_alias_migrations")
            )
            base_state = (
                "degraded"
                if failed_after_success or error_count or namespace_conflicts
                else "healthy"
                if last_success is not None
                else "unknown"
            )
            in_current_watcher_scope = watcher_is_fresh and source in configured_sources
            if in_current_watcher_scope:
                receipt_version_matches = str(receipt.get("importer_version") or "") == watcher_importer_version
                current_success = bool(
                    receipt_version_matches
                    and last_success is not None
                    and float(last_success) >= watcher_started_at
                    and not failed_after_success
                    and error_count == 0
                    and namespace_conflicts == 0
                )
                current_failure = bool(
                    receipt_version_matches
                    and (
                        (
                            last_failure is not None
                            and float(last_failure) >= watcher_started_at
                            and (failed_after_success or error_count > 0)
                        )
                        or (
                            namespace_conflicts > 0
                            and float(receipt.get("last_completed_at") or 0.0) >= watcher_started_at
                        )
                    )
                )
                source_state = "degraded" if current_failure else "healthy" if current_success else "pending"
            else:
                source_state = base_state
            if last_success is not None and (not watcher_is_fresh or source in configured_sources):
                last_success_values.append(float(last_success))
            # A live watcher is accountable only for its configured sources.
            # Historical receipts stay visible, but cannot make the current
            # watcher falsely green or permanently red.
            if source_state == "degraded" and (not watcher_is_fresh or source in configured_sources):
                if "cursor_state_db_filesystem_read_failed" in receipt_error_codes:
                    issues.append(
                        {
                            "code": "source_read_permission_required",
                            "source": source,
                            "action": (
                                "agentacct could not read Cursor's configured primary state database or an "
                                "owned path component. Confirm the Cursor root exists and grant the Dashboard "
                                "process read permission to that root/User/globalStorage/state.vscdb, then retry "
                                "Cursor only from Advanced."
                            ),
                        }
                    )
                elif any(
                    code in receipt_error_codes
                    for code in {
                        "cursor_root_symlink_not_allowed",
                        "cursor_user_dir_symlink_not_allowed",
                        "cursor_global_storage_symlink_not_allowed",
                        "cursor_state_db_symlink_not_allowed",
                        "cursor_state_db_path_shape_invalid",
                        "cursor_state_db_unsafe_path",
                        "cursor_state_db_sidecar_unsafe",
                    }
                ):
                    issues.append(
                        {
                            "code": "source_path_unsafe",
                            "source": source,
                            "action": (
                                "Cursor's configured application-support root, globalStorage directory, or "
                                "primary state.vscdb is symlinked or not an owned regular path. Select the "
                                "real Cursor root and retry Cursor only from Advanced."
                            ),
                        }
                    )
                elif "cursor_active_wal_not_supported" in receipt_error_codes:
                    issues.append(
                        {
                            "code": "source_snapshot_required",
                            "source": source,
                            "action": (
                                "Cursor's primary state.vscdb has an active WAL that this observation-only "
                                "adapter cannot snapshot safely. Quit Cursor completely, then retry Cursor "
                                "only from Advanced; agentacct will not use immutable mode or a backup."
                            ),
                        }
                    )
                elif any(
                    code in receipt_error_codes
                    for code in {
                        "cursor_state_db_replaced_during_scan",
                        "cursor_state_db_changed_during_scan",
                    }
                ):
                    issues.append(
                        {
                            "code": "source_changed_during_scan",
                            "source": source,
                            "action": (
                                "Cursor changed or replaced its primary state database during the read. "
                                "Wait for Cursor to become idle or quit it, then retry Cursor only from "
                                "Advanced. No partial observation was saved."
                            ),
                        }
                    )
                elif any(
                    code in receipt_error_codes
                    for code in {
                        "cursor_state_db_schema_unsupported",
                        "cursor_state_db_corrupt",
                        "cursor_composer_json_invalid",
                        "cursor_composer_scalar_schema_invalid",
                        "cursor_composer_timestamp_invalid",
                        "cursor_composer_identity_mismatch",
                        "cursor_composer_relationship_schema_invalid",
                        "cursor_composer_self_cycle",
                        "cursor_composer_multiple_parents",
                        "cursor_composer_cycle",
                        "cursor_composer_namespace_graph_invalid",
                    }
                ):
                    issues.append(
                        {
                            "code": "source_adapter_incompatible",
                            "source": source,
                            "action": (
                                "Cursor's primary composer store does not match the adapter's verified "
                                "metadata/lineage contract. Refresh alone cannot repair it. Update Agent "
                                "agentacct, restart it, then retry Cursor only from Advanced."
                            ),
                        }
                    )
                elif "claude_transcript_unsafe_path" in receipt_error_codes:
                    issues.append(
                        {
                            "code": "source_path_unsafe",
                            "source": source,
                            "action": (
                                "Claude Code source contains a symlink or non-regular transcript. Remove the "
                                "unsafe link from the configured Claude projects directory, then retry Claude "
                                "only from Advanced."
                            ),
                        }
                    )
                elif (
                    "hermes_multiple_source_homes_require_explicit_selection"
                    in receipt_error_codes
                ):
                    issues.append(
                        {
                            "code": "source_home_selection_required",
                            "source": source,
                            "action": (
                                "Multiple Hermes homes are configured, so agentacct stopped before reading "
                                "or merging them. Set HERMES_HOME to one absolute Hermes home in the dashboard "
                                "process and restart agentacct. Refreshing the unchanged configuration cannot "
                                "repair this."
                            ),
                        }
                    )
                elif any(
                    code in receipt_error_codes
                    for code in {
                        "claude_workflow_journal_schema_drift",
                        "claude_workflow_journal_validation_truncated",
                    }
                ):
                    issues.append(
                        {
                            "code": "source_adapter_incompatible",
                            "source": source,
                            "action": (
                                "Claude Code wrote a workflow journal shape this adapter cannot safely ignore. "
                                "Refresh alone cannot repair it. Update agentacct, restart it, then retry "
                                "Claude only from Advanced."
                            ),
                        }
                    )
                elif "claude_transcript_identity_scan_truncated" in receipt_error_codes:
                    issues.append(
                        {
                            "code": "source_identity_unresolved",
                            "source": source,
                            "action": (
                                "Claude Code transcript identity is outside this adapter's safe scan budget. "
                                "Refresh alone cannot repair it. Update or repair the Claude source, restart "
                                "agentacct, then retry Claude only from Advanced."
                            ),
                        }
                    )
                elif incomplete_alias_migrations:
                    issues.append(
                        {
                            "code": "alias_migration_incomplete",
                            "source": source,
                            "action": (
                                "agentacct preserved the legacy rows because the current client log "
                                "did not reproduce every stored model lane. Repair the log or source "
                                "path, then refresh again."
                            ),
                        }
                    )
                elif "invalid_observation" in receipt_error_codes:
                    issues.append(
                        {
                            "code": "invalid_observation",
                            "source": source,
                            "action": (
                                "agentacct rejected an invalid session-presence record from this adapter. "
                                "Update and restart agentacct, then retry only this source from Advanced; "
                                "refreshing unchanged input cannot repair the record."
                            ),
                        }
                    )
                elif "source_namespace_conflict" in receipt_error_codes or namespace_conflicts:
                    issues.append(
                        {
                            "code": "source_namespace_conflict",
                            "source": source,
                            "action": (
                                "Open Advanced and confirm the affected agent's configured data home. "
                                "Remove the unintended duplicate home or restore the intended path, then "
                                "restart sync and retry only this source."
                            ),
                        }
                    )
                elif "source_watermark_unorderable" in receipt_error_codes:
                    issues.append(
                        {
                            "code": "source_watermark_unorderable",
                            "source": source,
                            "action": (
                                "agentacct found different session revisions without a trustworthy source "
                                "revision time. Update agentacct or repair the source metadata, restart "
                                "sync, then retry only this source from Advanced."
                            ),
                        }
                    )
                elif "same_watermark_conflict" in receipt_error_codes:
                    issues.append(
                        {
                            "code": "same_watermark_conflict",
                            "source": source,
                            "action": (
                                "agentacct found different session contents at the same source revision. "
                                "Inspect the duplicated or rewritten client log, preserve the authoritative "
                                "copy, then retry only this source from Advanced."
                            ),
                        }
                    )
                elif EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE in receipt_error_codes:
                    # The dedicated Evidence issue is appended below. Avoid a
                    # second generic source_scan_failed issue when it is the
                    # only failure, while preserving any more specific source
                    # diagnostic ahead of the global post-persist issue.
                    pass
                else:
                    issues.append(
                        {
                            "code": "source_scan_failed",
                            "source": source,
                            "action": "Refresh now or inspect the source setup.",
                        }
                    )
                if EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE in receipt_error_codes:
                    issues.append(
                        {
                            "code": EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE,
                            "source": source,
                            "action": (
                                "The local usage ledger was saved, but Evidence v2 current-usage "
                                "reconciliation did not reach a clean state. Retry usage refresh; "
                                "if it persists, inspect Evidence v2 health and unresolved conflicts "
                                "before rebuilding or cleaning any store."
                            ),
                        }
                    )
            source_rows.append(
                {
                    "source": source,
                    "state": source_state,
                    "scope": (
                        "watched"
                        if source in configured_sources
                        else "historical"
                        if watcher_is_fresh
                        else "manual"
                    ),
                    "last_attempt_at": receipt.get("last_attempt_at"),
                    "last_success_at": last_success,
                    "last_failure_at": last_failure,
                    "discovered": _nonnegative_int(receipt.get("discovered")),
                    "parsed": _nonnegative_int(receipt.get("parsed")),
                    "skipped": _nonnegative_int(receipt.get("skipped")),
                    "error_count": error_count,
                    "error_code": receipt.get("error_code"),
                    "error_codes": receipt_error_codes,
                    "scan_limit": _nonnegative_int(receipt.get("scan_limit")),
                    "watermark": receipt.get("watermark"),
                    "limit_unit": receipt.get("limit_unit") or "rows",
                    "selected_root_groups": _optional_nonnegative_int(receipt.get("selected_root_groups")),
                    "returned_root_groups": _optional_nonnegative_int(
                        receipt.get("returned_root_groups")
                    ),
                    "returned_rows": _nonnegative_int(receipt.get("returned_rows")),
                    "excluded_by_limit": _nonnegative_int(receipt.get("excluded_by_limit")),
                    "ignored_non_transcript_files": _nonnegative_int(
                        receipt.get("ignored_non_transcript_files")
                    ),
                    "unresolved_identity_files": _nonnegative_int(
                        receipt.get("unresolved_identity_files")
                    ),
                    "excluded_by_source_namespace": _nonnegative_int(
                        receipt.get("excluded_by_source_namespace")
                    ),
                    "source_namespace_conflicts": namespace_conflicts,
                    "source_namespace_adoptions": _nonnegative_int(
                        receipt.get("source_namespace_adoptions")
                    ),
                    "concurrent_refresh_conflicts": _nonnegative_int(
                        receipt.get("concurrent_refresh_conflicts")
                    ),
                    "incomplete_alias_migrations": incomplete_alias_migrations,
                    "unparsed_selected_rows": _nonnegative_int(receipt.get("unparsed_selected_rows")),
                    "observed_sessions": _nonnegative_int(receipt.get("observed_sessions")),
                    "usage_sessions": _nonnegative_int(receipt.get("usage_sessions")),
                    "sessions_without_usage": _nonnegative_int(
                        receipt.get("sessions_without_usage")
                    ),
                    "session_observation_conflicts": _nonnegative_int(
                        receipt.get("session_observation_conflicts")
                    ),
                    "session_observation_conflict_reasons": (
                        _session_observation_conflict_reasons(
                            receipt.get("session_observation_conflict_reasons")
                        )
                    ),
                    "consecutive_failures": _nonnegative_int(receipt.get("consecutive_failures")),
                }
            )

        # Deduplicate identical aggregate issues while preserving order.
        unique_issues: list[dict[str, Any]] = []
        seen_issues: set[tuple[Any, ...]] = set()
        for issue in issues:
            key = (issue.get("code"), issue.get("source"), issue.get("action"))
            if key not in seen_issues:
                seen_issues.add(key)
                unique_issues.append(issue)

        if unique_issues:
            overall_state = "degraded"
        elif watcher_is_fresh and configured_sources and all(
            row["state"] == "healthy" for row in source_rows if row["source"] in configured_sources
        ):
            overall_state = "healthy"
        else:
            # A clean manual scan is useful evidence but not proof that a live
            # watcher still covers future sessions.
            overall_state = "unknown"
        return {
            "schema_version": INGESTION_HEALTH_SCHEMA_VERSION,
            "state": overall_state,
            "last_success_at": max(last_success_values) if last_success_values else None,
            "active_scan_count": len(active_scans),
            "sources": source_rows,
            "source_namespace_conflicts": sum(
                _nonnegative_int(row.get("source_namespace_conflicts")) for row in source_rows
            ),
            "source_namespace_adoptions": sum(
                _nonnegative_int(row.get("source_namespace_adoptions")) for row in source_rows
            ),
            "concurrent_refresh_conflicts": sum(
                _nonnegative_int(row.get("concurrent_refresh_conflicts")) for row in source_rows
            ),
            "incomplete_alias_migrations": sum(
                _nonnegative_int(row.get("incomplete_alias_migrations"))
                for row in source_rows
            ),
            "session_observation_conflicts": sum(
                _nonnegative_int(row.get("session_observation_conflicts"))
                for row in source_rows
            ),
            "session_observation_conflict_reasons": {
                code: sum(
                    _nonnegative_int(
                        (
                            row.get("session_observation_conflict_reasons")
                            if isinstance(
                                row.get("session_observation_conflict_reasons"),
                                Mapping,
                            )
                            else {}
                        ).get(code)
                    )
                    for row in source_rows
                )
                for code in SESSION_OBSERVATION_CONFLICT_ERROR_CODES
                if any(
                    _nonnegative_int(
                        (
                            row.get("session_observation_conflict_reasons")
                            if isinstance(
                                row.get("session_observation_conflict_reasons"),
                                Mapping,
                            )
                            else {}
                        ).get(code)
                    )
                    for row in source_rows
                )
            },
            "watcher": watcher_projection,
            "issues": unique_issues,
        }


__all__ = [
    "EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE",
    "INGESTION_HEALTH_DIRNAME",
    "INGESTION_HEALTH_FILENAME",
    "INGESTION_HEALTH_SCHEMA_VERSION",
    "SESSION_OBSERVATION_CONFLICT_ERROR_CODES",
    "IngestionHealthStore",
    "WatcherAcquireResult",
    "apply_evidence_refreshable_usage_health",
    "evidence_refreshable_usage_failed",
    "health_scan_results",
    "importer_build_id",
    "session_observation_conflict_error_code",
]
