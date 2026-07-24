#!/usr/bin/env python3
"""Safety-first benchmark harness for the canonical SQLite truth spike.

``normal`` mode creates a deterministic synthetic JSONL corpus below an
explicit scratch root. ``snapshot`` mode accepts only an explicit offline root
and canonical manifest, delegates every source check to
``agent_chronicle.canonical.snapshot``, and gives the benchmark hook only the
manifest-declared file paths. The harness never discovers agentacct or coding
client state.

The hook contract is one callable accepting a JSON-compatible request mapping
and returning a JSON-compatible mapping. The default target is::

    agent_chronicle.canonical.benchmark:benchmark_sqlite_truth

The request contains a candidate database path beneath the per-run scratch
directory. A hook must create that candidate and must not independently
discover source or store paths.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import json
import math
import os
import resource
import select
import signal
import sqlite3
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from agent_chronicle.canonical.live_paths import (
    LivePathSafetyError,
    reject_live_state_overlap,
)
from agent_chronicle.canonical.safe_scratch import (
    AnchoredRunDirectory,
    ScratchSafetyError,
    create_anchored_run_directory,
)
from agent_chronicle.canonical.snapshot import (
    SNAPSHOT_MANIFEST_VERSION,
    SnapshotManifest,
    SnapshotSafetyError,
    VerifiedSnapshot,
)
from agent_chronicle.canonical.sqlite import CanonicalStore


RESULT_SCHEMA_VERSION = "agent-chronicle.sqlite-truth-benchmark.v1"
REQUEST_SCHEMA_VERSION = "agent-chronicle.sqlite-truth-benchmark-request.v1"
MANIFEST_SCHEMA_VERSION = SNAPSHOT_MANIFEST_VERSION
DEFAULT_HOOK = "agent_chronicle.canonical.benchmark:benchmark_sqlite_truth"
MAX_NORMAL_RECORDS = 1_000_000
SNAPSHOT_IMPORT_PROJECTION_LIMIT_SECONDS = 30.0
SNAPSHOT_MAX_RSS_BYTES = 512 * 1024 * 1024
NORMAL_SIZE_AMPLIFICATION_MAX_RATIO = 2.0
NORMAL_SIZE_GATE_MIN_SOURCE_BYTES = 1024 * 1024
DEFAULT_HOOK_TIMEOUT_SECONDS = 60.0
MAX_HOOK_RESPONSE_BYTES = 16 * 1024 * 1024
_UNSAFE_SCRATCH_COMPONENTS = frozenset(
    {
        ".agent-sentinel",
        ".agent-sentinel-global",
        ".agent-chronicle",
        ".agent-chronicle-global",
        ".codex",
    }
)


class SafetyRefusal(SnapshotSafetyError):
    """Raised before hook import when a harness-level safety invariant fails."""


class BenchmarkFailure(RuntimeError):
    """A structured benchmark execution failure safe to render without traceback."""

    def __init__(
        self,
        message: str,
        *,
        result: Mapping[str, Any] | None = None,
        result_path: Path | None = None,
        cause_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.result = dict(result) if result is not None else None
        self.result_path = result_path
        self.cause_type = cause_type


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _assert_no_symlink_components(path: Path, *, label: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            observed = current.lstat()
        except OSError as exc:
            raise SafetyRefusal(f"{label} is not an existing directory") from exc
        if stat.S_ISLNK(observed.st_mode):
            raise SafetyRefusal(f"{label} may not contain symlink components")


def _require_scratch_root(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise SafetyRefusal("--scratch-root must be an explicit absolute path")
    try:
        reject_live_state_overlap(path, label="--scratch-root")
    except LivePathSafetyError as exc:
        raise SafetyRefusal(str(exc)) from exc
    _assert_no_symlink_components(path, label="--scratch-root")
    try:
        observed = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise SafetyRefusal("--scratch-root is not an existing directory") from exc
    if not stat.S_ISDIR(observed.st_mode):
        raise SafetyRefusal("--scratch-root must be a real directory")
    resolved = path.resolve(strict=True)
    if any(part.casefold() in _UNSAFE_SCRATCH_COMPONENTS for part in resolved.parts):
        raise SafetyRefusal("--scratch-root may not be inside live agentacct state")
    try:
        reject_live_state_overlap(resolved, label="--scratch-root")
    except LivePathSafetyError as exc:
        raise SafetyRefusal(str(exc)) from exc
    return resolved


def _reject_known_live_codex_path(raw: str | Path, *, label: str) -> None:
    """Refuse overlap with active coding-client or agentacct state roots."""

    path = Path(raw).expanduser()
    if not path.is_absolute():
        return
    candidates = [Path.home() / ".codex"]
    configured = os.environ.get("CODEX_HOME")
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.is_absolute():
            candidates.append(configured_path)
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved = path
    for candidate in candidates:
        try:
            live_root = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            live_root = candidate
        if resolved == live_root or resolved.is_relative_to(live_root):
            raise SafetyRefusal(
                f"{label} must be an offline copy, not a live Codex state path"
            )
    try:
        reject_live_state_overlap(path, label=label)
    except LivePathSafetyError as exc:
        raise SafetyRefusal(str(exc)) from exc


def _snapshot_file_payload(snapshot: VerifiedSnapshot) -> list[dict[str, Any]]:
    """Expose only paths that the canonical manifest declared and verified."""

    return [
        {
            "relative_path": item.relative_path,
            "path": str(item.path),
            "size_bytes": item.size_bytes,
            "sha256": item.sha256,
        }
        for item in snapshot.files
    ]


def _snapshot_metadata(snapshot: VerifiedSnapshot) -> dict[str, Any]:
    """Return facts derived from verification, never assertions from JSON."""

    return {
        "version": snapshot.manifest.version,
        "kind": snapshot.kind,
        "manifest_integrity_verified": True,
        "known_live_path": False,
        "file_count": len(snapshot.files),
        "total_size_bytes": sum(item.size_bytes for item in snapshot.files),
        "manifest_sha256": snapshot.manifest.digest_sha256,
    }


def verify_snapshot_manifest(
    root_text: str, manifest_text: str, *, scratch_root: Path
) -> VerifiedSnapshot:
    """Load and verify using the one canonical snapshot safety boundary."""

    _reject_known_live_codex_path(manifest_text, label="--snapshot-manifest")
    _reject_known_live_codex_path(root_text, label="--snapshot-root")
    manifest = SnapshotManifest.load(manifest_text)
    snapshot = VerifiedSnapshot.verify(root_text, manifest)
    if (
        scratch_root == snapshot.root
        or scratch_root.is_relative_to(snapshot.root)
        or snapshot.root.is_relative_to(scratch_root)
    ):
        raise SafetyRefusal("--scratch-root must be outside the verified snapshot")
    return snapshot


def _synthetic_event(index: int) -> dict[str, Any]:
    scenarios = ("identity", "conflict", "correction", "noop", "usage", "task")
    scenario = scenarios[index % len(scenarios)]
    task_number = index // len(scenarios)
    session_id = f"synthetic-session-{task_number:08d}"
    event: dict[str, Any] = {
        "event_id": f"synthetic-event-{index:012d}",
        "created_at": 1_750_000_000.0 + index,
        "source": "sqlite-truth-synthetic-benchmark",
        "event_type": {
            "identity": "session_observed",
            "conflict": "section_checkpoint",
            "correction": "model_usage",
            "noop": "model_usage",
            "usage": "model_usage",
            "task": "task_completed",
        }[scenario],
        "metadata": {
            "fixture_scenario": scenario,
            "client": "codex",
            "client_session_id": session_id,
            "work_id": f"synthetic-work-{task_number:08d}",
            "task_fixture_id": f"synthetic-task-{task_number:08d}",
        },
    }
    if scenario in {"correction", "noop", "usage"}:
        event.update(
            {
                "provider": "codex",
                "model": "gpt-synthetic",
                "estimated_input_tokens": 100 + task_number,
                "estimated_output_tokens": 10 + task_number % 7,
                "usage_confidence": "client_reported",
                "cost_confidence": "unknown",
            }
        )
    if scenario == "conflict":
        event["metadata"]["conflicting_client_session_id"] = (
            f"synthetic-conflict-{task_number:08d}"
        )
    elif scenario == "correction":
        event["metadata"]["correction_sequence"] = 1
    elif scenario == "noop":
        event["metadata"]["idempotency_key"] = f"synthetic-noop-{task_number:08d}"
    return event


def write_normal_corpus(path: Path, record_count: int) -> dict[str, Any]:
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("xb") as handle:
        for index in range(record_count):
            line = (
                json.dumps(
                    _synthetic_event(index),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            handle.write(line)
            digest.update(line)
            size_bytes += len(line)
        handle.flush()
        os.fsync(handle.fileno())
    return {
        "kind": "synthetic_normal",
        "path": str(path),
        "record_count": record_count,
        "size_bytes": size_bytes,
        "sha256": digest.hexdigest(),
        "scenarios": ["identity", "conflict", "correction", "noop", "usage", "task"],
    }


def _write_anchored_normal_corpus(
    run_directory: AnchoredRunDirectory,
    *,
    name: str,
    record_count: int,
) -> dict[str, Any]:
    """Write the generated corpus relative to the pinned run-directory fd."""

    digest = hashlib.sha256()
    size_bytes = 0
    descriptor = run_directory.open_file(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "wb", closefd=True) as handle:
        for index in range(record_count):
            line = (
                json.dumps(
                    _synthetic_event(index),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            handle.write(line)
            digest.update(line)
            size_bytes += len(line)
        handle.flush()
        os.fsync(handle.fileno())
    run_directory.prove_unchanged()
    path = run_directory.path / name
    return {
        "kind": "synthetic_normal",
        "path": str(path),
        "record_count": record_count,
        "size_bytes": size_bytes,
        "sha256": digest.hexdigest(),
        "scenarios": ["identity", "conflict", "correction", "noop", "usage", "task"],
    }


def _load_hook(specification: str) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    module_name, separator, attribute = specification.partition(":")
    if not separator or not module_name or not attribute or ":" in attribute:
        raise BenchmarkFailure("--hook must use module:callable syntax")
    try:
        module = importlib.import_module(module_name)
        hook = getattr(module, attribute)
    except (ImportError, AttributeError) as exc:
        raise BenchmarkFailure(f"benchmark hook is unavailable: {specification}") from exc
    if not callable(hook):
        raise BenchmarkFailure(f"benchmark hook is not callable: {specification}")
    return hook


def _rss_snapshot() -> dict[str, Any]:
    return _rss_payload(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def _rss_payload(raw: float) -> dict[str, Any]:
    multiplier = 1 if sys.platform == "darwin" else 1024
    return {
        "raw": raw,
        "raw_unit": "bytes" if multiplier == 1 else "kibibytes",
        "bytes": int(raw * multiplier),
    }


def _verify_candidate_database(
    path: Path,
    *,
    run_dir: Path,
    run_directory: AnchoredRunDirectory | None = None,
) -> dict[str, Any]:
    try:
        observed = (
            run_directory.stat_child(path.name)
            if run_directory is not None
            else path.lstat()
        )
    except OSError as exc:
        raise BenchmarkFailure("benchmark hook did not create candidate.sqlite3") from exc
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
    ):
        raise BenchmarkFailure("candidate database must be a unique regular non-symlink file")
    if stat.S_IMODE(observed.st_mode) != 0o600:
        raise BenchmarkFailure("candidate database must be owner-only (0600)")
    expected_identity = (int(observed.st_dev), int(observed.st_ino))
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(run_dir.resolve(strict=True)):
        raise BenchmarkFailure("candidate database escaped the per-run scratch directory")
    if run_directory is not None:
        run_directory.prove_unchanged()
    try:
        with CanonicalStore.open(resolved, read_only=True) as store:
            if store.file_identity != expected_identity:
                raise BenchmarkFailure("candidate database path changed before verification")
            store.connection.execute("PRAGMA query_only = ON")
            checks = store.quick_check()
            metadata = store.connection.execute(
                "SELECT store_role, schema_version, canonical_sequence "
                "FROM store_metadata WHERE singleton = 1"
            ).fetchone()
        if run_directory is not None:
            run_directory.prove_unchanged()
    except BenchmarkFailure:
        raise
    except (sqlite3.DatabaseError, ValueError, OSError, RuntimeError) as exc:
        raise BenchmarkFailure("candidate database is not a valid canonical candidate") from exc
    if not checks["ok"] or metadata is None:
        raise BenchmarkFailure("candidate database failed canonical integrity checks")
    try:
        closed_main_db = (
            run_directory.stat_child(path.name)
            if run_directory is not None
            else resolved.stat(follow_symlinks=False)
        )
    except OSError as exc:
        raise BenchmarkFailure("candidate database path changed during verification") from exc
    if (
        not stat.S_ISREG(closed_main_db.st_mode)
        or closed_main_db.st_nlink != 1
        or (int(closed_main_db.st_dev), int(closed_main_db.st_ino))
        != expected_identity
    ):
        raise BenchmarkFailure("candidate database path changed during verification")
    if stat.S_IMODE(closed_main_db.st_mode) != 0o600:
        raise BenchmarkFailure("candidate database must remain owner-only (0600)")
    return {
        "path": path.name,
        "size_bytes": closed_main_db.st_size,
        "closed_main_db_size_bytes": closed_main_db.st_size,
        "size_scope": "closed_main_database_file",
        "quick_check": "ok",
        "foreign_key_violations": 0,
        "store_role": str(metadata["store_role"]),
        "schema_version": int(metadata["schema_version"]),
        "canonical_sequence": int(metadata["canonical_sequence"]),
    }


def _record_phase(
    phases: list[dict[str, Any]], name: str, operation: Callable[[], Any]
) -> Any:
    started = time.perf_counter()
    try:
        value = operation()
    except BaseException:
        ended = time.perf_counter()
        phases.append(
            {
                "name": name,
                "status": "error",
                "perf_counter_start": started,
                "perf_counter_end": ended,
                "seconds": ended - started,
            }
        )
        raise
    ended = time.perf_counter()
    phases.append(
        {
            "name": name,
            "status": "passed",
            "perf_counter_start": started,
            "perf_counter_end": ended,
            "seconds": ended - started,
        }
    )
    return value


def _run_hook_in_fresh_process(
    hook: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    request: Mapping[str, Any],
    *,
    timeout_seconds: float = DEFAULT_HOOK_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one hook in an isolated child and return its process-scoped max RSS.

    ``ru_maxrss`` is a lifetime high-water mark, so subtracting a before value
    from the long-lived CLI/pytest process is invalid. A forked child gives the
    measurement one explicit scope and also prevents hook exceptions from
    printing tracebacks into the benchmark protocol.
    """

    read_fd, write_fd = os.pipe()
    try:
        process_id = os.fork()
    except OSError as exc:
        os.close(read_fd)
        os.close(write_fd)
        raise BenchmarkFailure(
            "could not start fresh benchmark hook process",
            cause_type=type(exc).__name__,
        ) from None

    if process_id == 0:  # pragma: no cover - assertions execute in the parent
        os.close(read_fd)
        try:
            os.setsid()
        except OSError:
            pass
        response: dict[str, Any]
        try:
            # Hook chatter is not part of the JSON protocol. Discard it and
            # return only the structured result/error over the private pipe.
            with (
                open(os.devnull, "w", encoding="utf-8") as discarded_output,
                contextlib.redirect_stdout(discarded_output),
                contextlib.redirect_stderr(discarded_output),
            ):
                raw_result = hook(request)
            if raw_result is None:
                raw_result = {}
            if not isinstance(raw_result, Mapping):
                raise TypeError("benchmark hook must return a mapping or None")
            hook_result = dict(raw_result)
            json.dumps(hook_result, allow_nan=False)
            response = {"ok": True, "hook_result": hook_result}
        except BaseException as exc:
            response = {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc) or type(exc).__name__,
            }
        try:
            encoded = json.dumps(response, allow_nan=False, sort_keys=True).encode(
                "utf-8"
            )
            with os.fdopen(write_fd, "wb", closefd=True) as handle:
                handle.write(encoded)
                handle.flush()
        except BaseException:
            try:
                os.close(write_fd)
            except OSError:
                pass
            os._exit(70)
        os._exit(0)

    os.close(write_fd)
    deadline = time.monotonic() + timeout_seconds
    response_chunks: list[bytes] = []
    response_bytes = 0
    os.set_blocking(read_fd, False)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            try:
                os.killpg(process_id, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(process_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            os.close(read_fd)
            os.wait4(process_id, 0)
            raise BenchmarkFailure(
                f"benchmark hook exceeded {timeout_seconds:g}s wall timeout",
                cause_type="HookTimeout",
            )
        try:
            readable, _, _ = select.select([read_fd], [], [], remaining)
        except InterruptedError:
            continue
        if not readable:
            continue
        chunk = os.read(read_fd, 64 * 1024)
        if not chunk:
            break
        response_bytes += len(chunk)
        if response_bytes > MAX_HOOK_RESPONSE_BYTES:
            try:
                os.killpg(process_id, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(process_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            os.close(read_fd)
            os.wait4(process_id, 0)
            raise BenchmarkFailure(
                "benchmark hook response exceeded the protocol size limit",
                cause_type="HookProtocolLimit",
            )
        response_chunks.append(chunk)
    os.close(read_fd)
    encoded_response = b"".join(response_chunks)
    waited_process_id, wait_status, usage = os.wait4(process_id, 0)
    if waited_process_id != process_id:
        raise BenchmarkFailure("benchmark hook process wait returned the wrong child")
    exit_code = os.waitstatus_to_exitcode(wait_status)
    try:
        response = json.loads(encoded_response)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkFailure(
            "benchmark hook process returned no valid structured result",
            cause_type=type(exc).__name__,
        ) from None
    if exit_code != 0 or not isinstance(response, Mapping):
        raise BenchmarkFailure(
            f"benchmark hook process exited with code {exit_code}",
            cause_type="HookProcessExit",
        )
    if response.get("ok") is not True:
        cause_type = str(response.get("error_type") or "HookError")
        message = str(response.get("error") or "benchmark hook failed")
        raise BenchmarkFailure(
            f"benchmark hook raised {cause_type}: {message}",
            cause_type=cause_type,
        )
    hook_result = response.get("hook_result")
    if not isinstance(hook_result, dict):
        raise BenchmarkFailure("benchmark hook process returned an invalid result")
    rss = _rss_payload(usage.ru_maxrss)
    rss.update(
        {
            "measurement_scope": "single_fresh_hook_process",
            "process_model": "forked_child",
        }
    )
    return hook_result, rss


def _finite_nonnegative_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        return None
    return numeric


def _acceptance_gates(
    *,
    mode: str,
    hook_result: Mapping[str, Any],
    hook_process_rss: Mapping[str, Any],
    source: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    checks = hook_result.get("checks")
    query = hook_result.get("query")
    forbidden_scans = None
    scan_evidence_field = None
    if isinstance(query, Mapping):
        if "unbounded_core_scans" in query:
            scan_evidence_field = "unbounded_core_scans"
            forbidden_scans = query.get(scan_evidence_field)
        elif "forbidden_canonical_scans" in query:
            # Backward-compatible with the first spike hook result while the
            # canonical name migrated to the more precise unbounded wording.
            scan_evidence_field = "forbidden_canonical_scans"
            forbidden_scans = query.get(scan_evidence_field)
    gates: dict[str, dict[str, Any]] = {
        "hook_status": {
            "required": True,
            "passed": hook_result.get("status") == "passed",
            "expected": "passed",
            "observed": hook_result.get("status"),
        },
        "hook_checks": {
            "required": True,
            "passed": isinstance(checks, Mapping) and checks.get("ok") is True,
            "expected": {"ok": True},
            "observed": dict(checks) if isinstance(checks, Mapping) else checks,
        },
        "query_budget": {
            "required": True,
            "passed": isinstance(query, Mapping)
            and query.get("query_budget_passed") is True,
            "expected": True,
            "observed": (
                query.get("query_budget_passed")
                if isinstance(query, Mapping)
                else None
            ),
        },
        "canonical_scan": {
            "required": True,
            "passed": isinstance(forbidden_scans, list) and not forbidden_scans,
            "expected": [],
            "observed": forbidden_scans,
            "evidence_field": scan_evidence_field,
        },
    }
    if mode == "snapshot":
        timings = hook_result.get("timings")
        import_seconds = (
            _finite_nonnegative_number(timings.get("import_seconds"))
            if isinstance(timings, Mapping)
            else None
        )
        projection_seconds = (
            _finite_nonnegative_number(timings.get("projection_seconds"))
            if isinstance(timings, Mapping)
            else None
        )
        combined_seconds = (
            import_seconds + projection_seconds
            if import_seconds is not None and projection_seconds is not None
            else None
        )
        rss_bytes = _finite_nonnegative_number(hook_process_rss.get("bytes"))
        gates.update(
            {
                "snapshot_import_projection": {
                    "required": True,
                    "passed": combined_seconds is not None
                    and combined_seconds <= SNAPSHOT_IMPORT_PROJECTION_LIMIT_SECONDS,
                    "threshold_seconds": SNAPSHOT_IMPORT_PROJECTION_LIMIT_SECONDS,
                    "observed_seconds": combined_seconds,
                    "components": {
                        "import_seconds": import_seconds,
                        "projection_seconds": projection_seconds,
                    },
                },
                "snapshot_max_rss": {
                    "required": True,
                    "passed": rss_bytes is not None
                    and rss_bytes <= SNAPSHOT_MAX_RSS_BYTES,
                    "threshold_bytes": SNAPSHOT_MAX_RSS_BYTES,
                    "observed_bytes": int(rss_bytes) if rss_bytes is not None else None,
                    "measurement_scope": hook_process_rss.get("measurement_scope"),
                },
            }
        )
    else:
        compact_source_bytes = _finite_nonnegative_number(source.get("size_bytes"))
        closed_database_bytes = _finite_nonnegative_number(
            candidate.get("closed_main_db_size_bytes")
        )
        applicable = (
            compact_source_bytes is not None
            and compact_source_bytes >= NORMAL_SIZE_GATE_MIN_SOURCE_BYTES
        )
        ratio = (
            closed_database_bytes / compact_source_bytes
            if compact_source_bytes not in {None, 0.0}
            and closed_database_bytes is not None
            else None
        )
        gates["normal_database_size_amplification"] = {
            "required": True,
            "applicable": applicable,
            "passed": (
                not applicable
                or (
                    ratio is not None
                    and ratio <= NORMAL_SIZE_AMPLIFICATION_MAX_RATIO
                )
            ),
            "threshold_ratio": NORMAL_SIZE_AMPLIFICATION_MAX_RATIO,
            "minimum_source_bytes": NORMAL_SIZE_GATE_MIN_SOURCE_BYTES,
            "compact_source_bytes": (
                int(compact_source_bytes)
                if compact_source_bytes is not None
                else None
            ),
            "closed_main_db_bytes": (
                int(closed_database_bytes)
                if closed_database_bytes is not None
                else None
            ),
            "observed_ratio": ratio,
            "measurement_scope": "closed_main_database_file",
        }
    required_passed = all(
        gate.get("passed") is True
        for gate in gates.values()
        if gate.get("required") is True
    )
    return {"passed": required_passed, "items": gates}


def run_benchmark(arguments: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    overall_started = time.perf_counter()
    started_at = _utc_now()
    rss_before = _rss_snapshot()
    phases: list[dict[str, Any]] = []

    scratch_root = _record_phase(
        phases,
        "validate_scratch_root",
        lambda: _require_scratch_root(arguments.scratch_root),
    )
    snapshot: VerifiedSnapshot | None = None
    if arguments.mode == "snapshot":
        snapshot = _record_phase(
            phases,
            "verify_snapshot_manifest",
            lambda: verify_snapshot_manifest(
                arguments.snapshot_root,
                arguments.snapshot_manifest,
                scratch_root=scratch_root,
            ),
        )

    try:
        run_directory = _record_phase(
            phases,
            "create_anchored_run_directory",
            lambda: create_anchored_run_directory(
                scratch_root,
                prefix="sqlite-truth-benchmark-",
            ),
        )
    except ScratchSafetyError as exc:
        raise SafetyRefusal(str(exc)) from exc
    try:
        return _run_benchmark_in_directory(
            arguments,
            overall_started=overall_started,
            started_at=started_at,
            rss_before=rss_before,
            phases=phases,
            snapshot=snapshot,
            run_directory=run_directory,
        )
    finally:
        run_directory.close()


def _run_benchmark_in_directory(
    arguments: argparse.Namespace,
    *,
    overall_started: float,
    started_at: str,
    rss_before: Mapping[str, Any],
    phases: list[dict[str, Any]],
    snapshot: VerifiedSnapshot | None,
    run_directory: AnchoredRunDirectory,
) -> tuple[dict[str, Any], Path]:
    run_directory.prove_unchanged()
    run_dir = run_directory.path
    candidate_db = run_dir / "candidate.sqlite3"
    result_path = run_dir / "result.json"
    if snapshot is not None:
        candidate_db = _record_phase(
            phases,
            "validate_candidate_target",
            lambda: snapshot.validate_candidate_target(
                candidate_db,
                scratch_root=run_dir,
            ),
        )
    manifest_metadata: dict[str, Any] | None = None
    source_payload: dict[str, Any] | None = None
    hook_result: dict[str, Any] | None = None
    hook_process_rss: dict[str, Any] | None = None
    candidate_metadata: dict[str, Any] | None = None

    try:
        # Import no untrusted hook until every external source and destination
        # path has passed the canonical boundary.
        hook = _record_phase(phases, "load_hook", lambda: _load_hook(arguments.hook))

        if arguments.mode == "normal":
            source_payload = _record_phase(
                phases,
                "generate_normal_corpus",
                lambda: _write_anchored_normal_corpus(
                    run_directory,
                    name="normal-corpus.jsonl",
                    record_count=arguments.records,
                ),
            )
        else:
            assert snapshot is not None
            manifest_metadata = _snapshot_metadata(snapshot)
            source_payload = {
                **manifest_metadata,
                "manifest_kind": manifest_metadata["kind"],
                "kind": "verified_snapshot",
                "snapshot_root": str(snapshot.root),
                "snapshot_manifest": str(snapshot.manifest.path),
                "files": _snapshot_file_payload(snapshot),
            }

        request = {
            "schema_version": REQUEST_SCHEMA_VERSION,
            "mode": arguments.mode,
            "scratch_dir": str(run_dir),
            "candidate_db": str(candidate_db),
            "source": source_payload,
        }
        try:
            run_directory.prove_unchanged()
            hook_result, hook_process_rss = _record_phase(
                phases,
                "canonical_hook",
                lambda: _run_hook_in_fresh_process(
                    hook,
                    request,
                    timeout_seconds=arguments.hook_timeout_seconds,
                ),
            )
            run_directory.prove_unchanged()
        finally:
            # Re-prove the source even when a hook fails, times out, or returns
            # an invalid protocol response. Snapshot mutation is a safety
            # refusal and takes precedence over an ordinary benchmark error.
            if snapshot is not None:
                _record_phase(
                    phases,
                    "verify_snapshot_unchanged",
                    snapshot.verify_unchanged,
                )
            run_directory.prove_unchanged()

        candidate_metadata = _record_phase(
            phases,
            "candidate_quick_check",
            lambda: _verify_candidate_database(
                candidate_db,
                run_dir=run_dir,
                run_directory=run_directory,
            ),
        )
        run_directory.prove_unchanged()
    except SnapshotSafetyError:
        raise
    except (BenchmarkFailure, OSError, sqlite3.DatabaseError) as exc:
        failure = (
            exc
            if isinstance(exc, BenchmarkFailure)
            else BenchmarkFailure(str(exc), cause_type=type(exc).__name__)
        )
        overall_ended = time.perf_counter()
        error_result = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "status": "error",
            "execution_status": "error",
            "acceptance_status": "not_evaluated",
            "error_type": "BenchmarkFailure",
            "mode": arguments.mode,
            "started_at": started_at,
            "completed_at": _utc_now(),
            "hook": arguments.hook,
            "clock": {
                "source": "time.perf_counter",
                "perf_counter_start": overall_started,
                "perf_counter_end": overall_ended,
                "seconds": overall_ended - overall_started,
            },
            "timings": phases,
            "resource": {
                "parent_process_source": "resource.getrusage(RUSAGE_SELF).ru_maxrss",
                "parent_max_rss_before": rss_before,
                "parent_max_rss_after": _rss_snapshot(),
                "hook_process_max_rss": hook_process_rss,
            },
            "manifest": manifest_metadata,
            "source": {
                key: value
                for key, value in (source_payload or {}).items()
                if key not in {"path", "files", "snapshot_root", "snapshot_manifest"}
            },
            "candidate": candidate_metadata,
            "hook_result": hook_result,
            "acceptance": {"passed": None, "items": {}},
            "error": {
                "type": "BenchmarkFailure",
                "cause_type": failure.cause_type,
                "message": str(failure),
            },
            "artifacts": {
                "run_directory": run_dir.name,
                "candidate_db": candidate_db.name,
                "result_json": result_path.name,
            },
        }
        try:
            run_directory.atomic_write_json(result_path.name, error_result)
        except OSError:
            result_path = None
        raise BenchmarkFailure(
            str(failure),
            result=error_result,
            result_path=result_path,
            cause_type=failure.cause_type,
        ) from None

    assert source_payload is not None
    assert hook_result is not None
    assert hook_process_rss is not None
    assert candidate_metadata is not None
    acceptance = _acceptance_gates(
        mode=arguments.mode,
        hook_result=hook_result,
        hook_process_rss=hook_process_rss,
        source=source_payload,
        candidate=candidate_metadata,
    )
    acceptance_status = "passed" if acceptance["passed"] else "failed"
    overall_ended = time.perf_counter()
    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "status": "passed" if acceptance["passed"] else "failed",
        "execution_status": "passed",
        "acceptance_status": acceptance_status,
        "mode": arguments.mode,
        "started_at": started_at,
        "completed_at": _utc_now(),
        "hook": arguments.hook,
        "clock": {
            "source": "time.perf_counter",
            "perf_counter_start": overall_started,
            "perf_counter_end": overall_ended,
            "seconds": overall_ended - overall_started,
        },
        "timings": phases,
        "resource": {
            "parent_process_source": "resource.getrusage(RUSAGE_SELF).ru_maxrss",
            "parent_max_rss_before": rss_before,
            "parent_max_rss_after": _rss_snapshot(),
            "hook_process_max_rss": hook_process_rss,
        },
        "manifest": manifest_metadata,
        "source": {
            key: value
            for key, value in source_payload.items()
            if key not in {"path", "files", "snapshot_root", "snapshot_manifest"}
        },
        "candidate": candidate_metadata,
        "hook_result": hook_result,
        "acceptance": acceptance,
        "artifacts": {
            "run_directory": run_dir.name,
            "candidate_db": candidate_db.name,
            "result_json": result_path.name,
        },
    }
    run_directory.atomic_write_json(result_path.name, result)
    run_directory.prove_unchanged()
    return result, result_path


def _normal_record_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("records must be an integer") from exc
    if not 1 <= count <= MAX_NORMAL_RECORDS:
        raise argparse.ArgumentTypeError(
            f"records must be between 1 and {MAX_NORMAL_RECORDS}"
        )
    return count


def _positive_finite_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("hook timeout must be a number") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("hook timeout must be finite and greater than zero")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark an explicit canonical SQLite candidate without "
            "discovering live stores."
        )
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    normal = subparsers.add_parser("normal", help="benchmark a deterministic synthetic corpus")
    normal.add_argument("--scratch-root", required=True)
    normal.add_argument("--records", type=_normal_record_count, default=6_000)
    normal.add_argument("--hook", default=DEFAULT_HOOK)
    normal.add_argument(
        "--hook-timeout-seconds",
        type=_positive_finite_seconds,
        default=DEFAULT_HOOK_TIMEOUT_SECONDS,
    )

    snapshot = subparsers.add_parser(
        "snapshot", help="benchmark an explicitly manifested offline snapshot"
    )
    snapshot.add_argument("--scratch-root", required=True)
    snapshot.add_argument("--snapshot-root", required=True)
    snapshot.add_argument("--snapshot-manifest", required=True)
    snapshot.add_argument("--hook", default=DEFAULT_HOOK)
    snapshot.add_argument(
        "--hook-timeout-seconds",
        type=_positive_finite_seconds,
        default=DEFAULT_HOOK_TIMEOUT_SECONDS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result, result_path = run_benchmark(arguments)
    except SnapshotSafetyError as exc:
        print(
            json.dumps({"status": "refused", "error": str(exc)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    except BenchmarkFailure as exc:
        payload: dict[str, Any]
        if exc.result is not None:
            payload = dict(exc.result)
            payload["result_path"] = (
                str(exc.result_path) if exc.result_path is not None else None
            )
        else:
            payload = {
                "status": "error",
                "execution_status": "error",
                "acceptance_status": "not_evaluated",
                "error_type": "BenchmarkFailure",
                "error": str(exc),
            }
        print(
            json.dumps(payload, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    except (OSError, sqlite3.DatabaseError) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "execution_status": "error",
                    "acceptance_status": "not_evaluated",
                    "error_type": "BenchmarkFailure",
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    payload = {**result, "result_path": str(result_path)}
    print(json.dumps(payload, sort_keys=True))
    return 0 if result["acceptance_status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
