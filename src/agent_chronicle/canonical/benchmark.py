"""Benchmark-only importer for the isolated canonical SQLite candidate.

This module is deliberately reachable only through the explicit benchmark
hook.  It does not discover stores or client roots.  Synthetic mode consumes
the harness-created JSONL file; snapshot mode reuses agentacct's existing
read-only Codex source reader against a manifest-verified offline root.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import time
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from .live_paths import LivePathSafetyError, reject_live_state_overlap
from .sqlite import CanonicalRepository, CanonicalStore, canonical_hash
from .snapshot import VerifiedSnapshot
from .types import (
    CostCalculationInput,
    FactInput,
    FactSessionLinkInput,
    SessionEdgeInput,
    SessionInput,
    SourceInstanceInput,
    SQLITE_INT64_MAX,
    TOKEN_FIELDS,
    UsageDayQuery,
    UsageMeasurementInput,
    WorkClaimInput,
)


REQUEST_SCHEMA_VERSION = "agent-chronicle.sqlite-truth-benchmark-request.v1"
_HASH_CHUNK_BYTES = 8 * 1024 * 1024
_MAX_LINE_BYTES = 16 * 1024 * 1024
_MAX_CODEX_ROOT_GROUPS = 1_000_000


class BenchmarkHookError(ValueError):
    """The benchmark request violated the hook's explicit-path contract."""


def _absolute_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str):
        raise BenchmarkHookError(f"{label} must be a string path")
    path = Path(value)
    if not path.is_absolute():
        raise BenchmarkHookError(f"{label} must be an explicit absolute path")
    return path


def _validate_request(request: object) -> tuple[Mapping[str, Any], str, Path, Path, Mapping[str, Any]]:
    if not isinstance(request, Mapping):
        raise BenchmarkHookError("benchmark request must be a mapping")
    if request.get("schema_version") != REQUEST_SCHEMA_VERSION:
        raise BenchmarkHookError("unsupported benchmark request schema")
    mode = request.get("mode")
    if mode not in {"normal", "snapshot"}:
        raise BenchmarkHookError("benchmark mode must be normal or snapshot")
    scratch = _absolute_path(request.get("scratch_dir"), label="scratch_dir")
    candidate = _absolute_path(request.get("candidate_db"), label="candidate_db")
    if not scratch.is_dir() or scratch.is_symlink():
        raise BenchmarkHookError("scratch_dir must be a real existing directory")
    scratch = scratch.resolve(strict=True)
    try:
        reject_live_state_overlap(scratch, label="scratch_dir")
    except LivePathSafetyError as exc:
        raise BenchmarkHookError(str(exc)) from exc
    if candidate.parent.resolve(strict=True) != scratch:
        raise BenchmarkHookError("candidate_db must be directly beneath scratch_dir")
    if candidate.exists() or candidate.is_symlink():
        raise BenchmarkHookError("candidate_db must not already exist")
    source = request.get("source")
    if not isinstance(source, Mapping):
        raise BenchmarkHookError("benchmark source must be a mapping")
    return request, str(mode), scratch, candidate, source


def _hash_regular_file(path: Path) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise BenchmarkHookError("normal corpus must be one unique regular file")
        digest = hashlib.sha256()
        size = 0
        while True:
            block = os.read(descriptor, _HASH_CHUNK_BYTES)
            if not block:
                break
            size += len(block)
            digest.update(block)
        after = os.fstat(descriptor)
        identity = lambda item: (
            item.st_dev,
            item.st_ino,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
        if identity(before) != identity(after):
            raise BenchmarkHookError("normal corpus changed while hashing")
        return size, digest.hexdigest()
    finally:
        os.close(descriptor)


def _record(counter: Counter[str], disposition: str) -> None:
    counter[disposition] += 1


def _normal_usage(
    session_id: int,
    event: Mapping[str, Any],
    *,
    source_order: int,
    representation: str = "synthetic-jsonl",
    totals_eligible: bool = True,
    held_reason: str | None = None,
) -> UsageMeasurementInput:
    input_tokens = int(event.get("estimated_input_tokens") or 0)
    output_tokens = int(event.get("estimated_output_tokens") or 0)
    task_number = max(0, source_order)
    base_time = 1_750_000_000_000_000 + task_number
    return UsageMeasurementInput(
        session_id=session_id,
        lane="session_total",
        representation=representation,
        update_semantics="cumulative_snapshot",
        precedence_role="authoritative" if totals_eligible else "fallback",
        granularity="session",
        totals_eligible=totals_eligible,
        provider=str(event.get("provider") or "codex")[:128],
        model=str(event.get("model") or "gpt-synthetic")[:256],
        usage_confidence="synthetic_observed",
        held_reason=held_reason,
        input_tokens=input_tokens,
        input_tokens_reported=True,
        output_tokens=output_tokens,
        output_tokens_reported=True,
        total_tokens=input_tokens + output_tokens,
        total_tokens_reported=True,
        source_order=source_order,
        observed_at_us=base_time,
        updated_at_us=base_time,
    )


def _normal_claim(
    source_instance_id: int,
    *,
    event_id: str,
    task_number: int,
    status: str,
    content_label: str,
    supersedes_fact_id: int | None = None,
) -> WorkClaimInput:
    content = {
        "task_number": task_number,
        "status": status,
        "content_label": content_label,
    }
    return WorkClaimInput(
        fact=FactInput(
            source_instance_id=source_instance_id,
            source_event_id=event_id,
            fact_type="work_claim",
            transport="synthetic-benchmark",
            strength="recorded_claim",
            occurred_at_us=1_750_000_000_000_000 + task_number,
            source_order=task_number,
            content_hash=canonical_hash(content),
            supersedes_fact_id=supersedes_fact_id,
        ),
        event_kind="section",
        outcome_axis="outcome" if status == "completed" else "execution",
        status=status,
        title=f"Synthetic Task {task_number}",
        summary="Synthetic benchmark claim without client content.",
        work_id=f"synthetic-work-{task_number:08d}",
        section_id="sqlite-truth-benchmark",
    )


def _import_normal(
    repository: CanonicalRepository,
    source: Mapping[str, Any],
    *,
    scratch: Path,
) -> dict[str, Any]:
    if source.get("kind") != "synthetic_normal":
        raise BenchmarkHookError("normal mode requires a synthetic_normal source")
    corpus = _absolute_path(source.get("path"), label="normal source path")
    if corpus.parent.resolve(strict=True) != scratch:
        raise BenchmarkHookError("normal source must be directly beneath scratch_dir")
    size, digest = _hash_regular_file(corpus)
    if size != source.get("size_bytes") or digest != source.get("sha256"):
        raise BenchmarkHookError("normal corpus does not match its declared size/hash")
    source_instance = repository.get_or_create_source(
        SourceInstanceInput(
            client="codex",
            adapter="synthetic-benchmark-v1",
            representation="synthetic-jsonl",
            namespace_digest=bytes.fromhex(digest),
            namespace_scheme="sha256-synthetic-corpus-v1",
            source_schema_version="synthetic-normal-v1",
            privacy_label=f"synthetic:{digest[:12]}",
        )
    )
    dispositions: Counter[str] = Counter()
    sessions: dict[str, int] = {}
    tasks: dict[str, str] = {}
    original_facts: dict[str, int] = {}
    session_replay_attempts = 0
    usage_replay_attempts = 0
    replay_physical_writes = 0
    replay_sequence_delta = 0
    lines = 0
    with corpus.open("rb") as handle:
        for lines, raw in enumerate(handle, start=1):
            if len(raw) > _MAX_LINE_BYTES:
                raise BenchmarkHookError("normal corpus line exceeds safety limit")
            event = json.loads(raw)
            if not isinstance(event, Mapping):
                raise BenchmarkHookError("normal corpus row must be an object")
            metadata = event.get("metadata")
            if not isinstance(metadata, Mapping):
                raise BenchmarkHookError("normal corpus metadata must be an object")
            session_key = str(metadata.get("client_session_id") or "")
            if not session_key:
                raise BenchmarkHookError("normal corpus row lacks client_session_id")
            try:
                task_number = int(session_key.rsplit("-", 1)[-1])
            except ValueError as exc:
                raise BenchmarkHookError("normal corpus session identity is malformed") from exc
            base_time = 1_750_000_000_000_000 + task_number
            session_id = sessions.get(session_key)
            if session_id is None:
                result = repository.reconcile_session(
                    SessionInput(
                        source_instance_id=source_instance.source_instance_id,
                        client_session_id=session_key,
                        session_kind="root",
                        started_at_us=base_time,
                        last_activity_at_us=base_time,
                        observation_order=task_number,
                    )
                )
                _record(dispositions, f"session_{result.disposition}")
                assert result.row_id is not None
                session_id = result.row_id
                sessions[session_key] = session_id
                tasks[session_key] = repository.get_or_create_task_anchor(
                    session_id
                ).public_task_id

            scenario = str(metadata.get("fixture_scenario") or "")
            fact_identity = f"synthetic-work-claim-{task_number:08d}"
            if scenario == "conflict":
                original = repository.append_work_claim(
                    _normal_claim(
                        source_instance.source_instance_id,
                        event_id=fact_identity,
                        task_number=task_number,
                        status="started",
                        content_label="original",
                    )
                )
                _record(dispositions, f"fact_{original.disposition}")
                assert original.row_id is not None
                original_facts[session_key] = original.row_id
                repository.link_fact_to_session(
                    FactSessionLinkInput(
                        fact_id=original.row_id,
                        session_id=session_id,
                        method="synthetic-session",
                        confidence="exact",
                        rule_version="benchmark-v1",
                        validation_state="valid",
                    )
                )
                conflict = repository.append_work_claim(
                    _normal_claim(
                        source_instance.source_instance_id,
                        event_id=fact_identity,
                        task_number=task_number,
                        status="completed",
                        content_label="conflicting-reuse",
                    )
                )
                _record(dispositions, f"fact_{conflict.disposition}")
            elif scenario == "correction":
                current = _normal_usage(session_id, event, source_order=task_number * 2 + 2)
                baseline = UsageMeasurementInput(
                    **{
                        **current.__dict__,
                        "input_tokens": max(0, int(current.input_tokens or 0) - 1),
                        "total_tokens": max(0, int(current.total_tokens or 0) - 1),
                        "source_order": task_number * 2 + 1,
                    }
                )
                first = repository.reconcile_usage(baseline)
                second = repository.reconcile_usage(
                    current,
                    checkpoint=("correction", f"synthetic-{task_number:08d}"),
                )
                _record(dispositions, f"usage_{first.disposition}")
                _record(dispositions, f"usage_{second.disposition}")
            elif scenario == "noop":
                current = _normal_usage(session_id, event, source_order=task_number * 2 + 2)
                changes_before = repository.connection.total_changes
                sequence_before = repository.canonical_sequence()
                result = repository.reconcile_usage(current)
                replay_physical_writes += (
                    repository.connection.total_changes - changes_before
                )
                replay_sequence_delta += (
                    repository.canonical_sequence() - sequence_before
                )
                usage_replay_attempts += 1
                _record(dispositions, f"usage_{result.disposition}")
                changes_before = repository.connection.total_changes
                sequence_before = repository.canonical_sequence()
                session_replay = repository.reconcile_session(
                    SessionInput(
                        source_instance_id=source_instance.source_instance_id,
                        client_session_id=session_key,
                        session_kind="root",
                        started_at_us=base_time,
                        last_activity_at_us=base_time,
                        observation_order=task_number,
                    )
                )
                replay_physical_writes += (
                    repository.connection.total_changes - changes_before
                )
                replay_sequence_delta += (
                    repository.canonical_sequence() - sequence_before
                )
                session_replay_attempts += 1
                _record(dispositions, f"session_{session_replay.disposition}")
            elif scenario == "usage":
                held = _normal_usage(
                    session_id,
                    event,
                    source_order=task_number,
                    representation="synthetic-fallback",
                    totals_eligible=False,
                    held_reason="authoritative_representation_available",
                )
                result = repository.reconcile_usage(held)
                _record(dispositions, f"usage_{result.disposition}")
            elif scenario == "task":
                original_id = original_facts.get(session_key)
                completed = repository.append_work_claim(
                    _normal_claim(
                        source_instance.source_instance_id,
                        event_id=f"{fact_identity}-completion",
                        task_number=task_number,
                        status="completed",
                        content_label="explicit-correction",
                        supersedes_fact_id=original_id,
                    )
                )
                _record(dispositions, f"fact_{completed.disposition}")
                assert completed.row_id is not None
                link = repository.link_fact_to_session(
                    FactSessionLinkInput(
                        fact_id=completed.row_id,
                        session_id=session_id,
                        method="synthetic-session",
                        confidence="exact",
                        rule_version="benchmark-v1",
                        validation_state="valid",
                    )
                )
                _record(dispositions, f"link_{link.disposition}")

    final_size, final_digest = _hash_regular_file(corpus)
    if (final_size, final_digest) != (size, digest):
        raise BenchmarkHookError("normal corpus changed during import")
    expected_records = source.get("record_count")
    if isinstance(expected_records, int) and lines != expected_records:
        raise BenchmarkHookError("normal corpus record count changed")
    replay_gate_passed = all(
        (
            session_replay_attempts > 0,
            usage_replay_attempts > 0,
            replay_physical_writes == 0,
            replay_sequence_delta == 0,
        )
    )
    return {
        "source_kind": "synthetic_normal",
        "records": lines,
        "sessions": len(sessions),
        "tasks": len(tasks),
        "dispositions": dict(sorted(dispositions.items())),
        "unchanged_reconciliation": {
            "session_attempts": session_replay_attempts,
            "usage_attempts": usage_replay_attempts,
            "physical_writes": replay_physical_writes,
            "canonical_sequence_delta": replay_sequence_delta,
            "gate_passed": replay_gate_passed,
        },
    }


def _codex_snapshot_path_allowed(relative: str) -> bool:
    pure = PurePosixPath(relative)
    if len(pure.parts) == 1:
        return pure.name == "session_index.jsonl"
    return (
        pure.parts[0] == "sessions"
        and pure.name.startswith("rollout-")
        and pure.suffix == ".jsonl"
    )


def _timestamp_us(value: int | float | None) -> int | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    if number >= 10**14:
        scaled = number
    elif number >= 10**11:
        scaled = number * 1_000
    else:
        scaled = number * 1_000_000
    if not math.isfinite(scaled) or scaled > SQLITE_INT64_MAX:
        return None
    return int(scaled)


def _sqlite_nonnegative_int(value: object) -> int | None:
    return (
        value
        if type(value) is int and 0 <= value <= SQLITE_INT64_MAX
        else None
    )


def _verified_codex_result_source_paths(
    items: list[Any],
    *,
    root: Path,
    verified_rollouts: set[str],
) -> set[str]:
    """Fail closed when the existing reader observes an undeclared rollout.

    The reader recursively discovers rollout files beneath ``root``.  A file
    created after snapshot verification and removed before the final recheck
    would otherwise evade both inventory scans.  Compare the reader's retained
    source paths lexically so a now-removed transient file is still rejected.
    """

    result_source_paths: set[str] = set()
    for item in items:
        source_value = getattr(item, "source_path", None)
        if not isinstance(source_value, (str, os.PathLike)):
            raise BenchmarkHookError("Codex reader returned an invalid source path")
        source_path = Path(source_value)
        if not source_path.is_absolute():
            raise BenchmarkHookError("Codex reader returned a non-absolute source path")
        lexical_path = Path(os.path.abspath(os.fspath(source_path)))
        try:
            relative = lexical_path.relative_to(root).as_posix()
        except ValueError as exc:
            raise BenchmarkHookError(
                "Codex reader returned a source outside the verified snapshot root"
            ) from exc
        if relative not in verified_rollouts:
            raise BenchmarkHookError(
                "Codex reader returned a rollout outside the verified snapshot manifest"
            )
        result_source_paths.add(relative)
    return result_source_paths


def _verified_codex_snapshot(source: Mapping[str, Any]) -> VerifiedSnapshot:
    if (
        source.get("kind") != "verified_snapshot"
        or source.get("manifest_integrity_verified") is not True
    ):
        raise BenchmarkHookError("snapshot mode requires canonical verified snapshot metadata")
    if source.get("known_live_path") is not False:
        raise BenchmarkHookError("snapshot source must not be a known live path")
    if source.get("kind") == "verified_snapshot" and source.get("manifest_sha256") is None:
        raise BenchmarkHookError("verified snapshot manifest digest is missing")
    manifest_kind = source.get("manifest_kind")
    if manifest_kind != "codex":
        raise BenchmarkHookError("default snapshot benchmark supports only kind='codex'")
    root_value = source.get("snapshot_root")
    manifest_value = source.get("snapshot_manifest")
    if not isinstance(root_value, str) or not isinstance(manifest_value, str):
        raise BenchmarkHookError("snapshot root and manifest must be explicit paths")
    try:
        snapshot = VerifiedSnapshot.verify(root_value, manifest_value)
    except ValueError as exc:
        raise BenchmarkHookError(f"snapshot verification failed: {exc}") from exc
    if snapshot.kind != "codex":
        raise BenchmarkHookError("verified snapshot manifest kind must be 'codex'")
    if snapshot.manifest_digest != source.get("manifest_sha256"):
        raise BenchmarkHookError("verified snapshot manifest digest does not match the request")

    declared = source.get("files")
    if not isinstance(declared, list):
        raise BenchmarkHookError("verified snapshot file inventory is missing")
    requested_inventory = {
        (
            str(item.get("relative_path")),
            int(item.get("size_bytes")),
            str(item.get("sha256")),
        )
        for item in declared
        if isinstance(item, Mapping)
    }
    verified_inventory = {
        (item.relative_path, item.size_bytes, item.sha256) for item in snapshot.files
    }
    if requested_inventory != verified_inventory or len(requested_inventory) != len(declared):
        raise BenchmarkHookError("request inventory does not exactly match the verified manifest")
    return snapshot


def _import_codex_snapshot(
    repository: CanonicalRepository,
    source: Mapping[str, Any],
    *,
    snapshot: VerifiedSnapshot,
) -> dict[str, Any]:
    root = snapshot.root
    files: list[Mapping[str, Any]] = [
        {
            "relative_path": item.relative_path,
            "path": str(item.path),
            "size_bytes": item.size_bytes,
            "sha256": item.sha256,
        }
        for item in snapshot.files
    ]
    relative_paths = [str(item["relative_path"]) for item in files]
    forbidden = [path for path in relative_paths if not _codex_snapshot_path_allowed(path)]
    if forbidden:
        raise BenchmarkHookError(
            "Codex benchmark snapshot contains non-source or secret-shaped files"
        )
    verified_rollouts = {
        str(item["relative_path"]): int(item.get("size_bytes") or 0)
        for item in files
        if str(item["relative_path"]) != "session_index.jsonl"
    }

    # Reuse the existing, tested Codex reader.  This is an explicit offline
    # invocation, not a new adapter and not source discovery.
    from agent_chronicle.client_usage import _discover_codex_usage_from_home

    stats: dict[str, Any] = {}
    observations: list[Any] = []
    events = _discover_codex_usage_from_home(
        codex_home=root,
        limit_sessions=_MAX_CODEX_ROOT_GROUPS,
        _discovery_stats=stats,
        _session_observations=observations,
    )
    result_source_paths = _verified_codex_result_source_paths(
        [*observations, *events],
        root=root,
        verified_rollouts=set(verified_rollouts),
    )
    manifest_digest = str(source.get("manifest_sha256"))
    if len(manifest_digest) != 64:
        raise BenchmarkHookError("verified snapshot manifest digest is malformed")
    source_instance = repository.get_or_create_source(
        SourceInstanceInput(
            client="codex",
            adapter="existing-codex-readonly-reader",
            representation="codex-offline-snapshot",
            namespace_digest=bytes.fromhex(manifest_digest),
            namespace_scheme="snapshot-manifest-sha256-v1",
            source_schema_version="codex-existing-reader",
            privacy_label=f"offline:{manifest_digest[:12]}",
        )
    )
    dispositions: Counter[str] = Counter()
    session_ids: dict[str, int] = {}
    invalid_integer_values = 0
    for observation in observations:
        parent_id = observation.parent_client_session_id
        kind = "root" if parent_id is None else "continuation"
        started = _timestamp_us(observation.started_at)
        updated = _timestamp_us(observation.updated_at)
        if observation.started_at is not None and started is None:
            invalid_integer_values += 1
        if observation.updated_at is not None and updated is None:
            invalid_integer_values += 1
        if started is not None and updated is not None and updated < started:
            updated = started
        observation_order = _sqlite_nonnegative_int(
            observation.source_revision_at or observation.updated_at or 0
        )
        if observation_order is None:
            invalid_integer_values += 1
            observation_order = 0
        result = repository.reconcile_session(
            SessionInput(
                source_instance_id=source_instance.source_instance_id,
                client_session_id=observation.client_session_id,
                session_kind=kind,
                started_at_us=started,
                last_activity_at_us=updated,
                observation_order=observation_order,
            )
        )
        _record(dispositions, f"session_{result.disposition}")
        assert result.row_id is not None
        session_ids[observation.client_session_id] = result.row_id

    accepted_edges = 0
    rejected_edges = 0
    for observation in observations:
        parent_key = observation.parent_client_session_id
        if parent_key is None:
            repository.get_or_create_task_anchor(session_ids[observation.client_session_id])
            continue
        child_id = session_ids[observation.client_session_id]
        parent_id = session_ids.get(parent_key)
        if parent_id is None:
            rejected_edges += 1
            continue
        result = repository.add_session_edge(
            SessionEdgeInput(
                child_session_id=child_id,
                parent_session_id=parent_id,
                relation="continuation",
                basis="existing_codex_exact_parent",
                validation_state="valid",
                content_hash=canonical_hash(
                    {"child": observation.client_session_id, "parent": parent_key}
                ),
            )
        )
        _record(dispositions, f"edge_{result.disposition}")
        row = repository.connection.execute(
            "SELECT validation_state FROM session_edges WHERE session_edge_id = ?",
            (result.row_id,),
        ).fetchone()
        if row is not None and row[0] == "valid":
            accepted_edges += 1
        else:
            rejected_edges += 1

    for event in events:
        session_id = session_ids.get(event.client_session_id)
        if session_id is None:
            started = _timestamp_us(event.started_at)
            updated = _timestamp_us(event.updated_at)
            observation_order = _sqlite_nonnegative_int(event.updated_at or 0)
            if observation_order is None:
                invalid_integer_values += 1
                observation_order = 0
            result = repository.reconcile_session(
                SessionInput(
                    source_instance_id=source_instance.source_instance_id,
                    client_session_id=event.client_session_id,
                    session_kind="root",
                    started_at_us=started,
                    last_activity_at_us=updated,
                    observation_order=observation_order,
                )
            )
            assert result.row_id is not None
            session_id = result.row_id
            session_ids[event.client_session_id] = session_id
            repository.get_or_create_task_anchor(session_id)
        wire = event.to_sentinel_event()
        metadata = wire.get("metadata") if isinstance(wire.get("metadata"), Mapping) else {}
        additive = metadata.get("usage_additive") is not False and event.source_parse_complete
        held_reason = None if additive else str(
            metadata.get("usage_normalization_state") or "source_parse_incomplete"
        )[:512]
        cache_creation_reported = event.cache_creation_tokens_reported is True
        cache_read_reported = event.cache_read_tokens_reported is True
        cached_reported = cache_creation_reported or cache_read_reported
        input_reported = event.input_tokens_reported is True
        output_reported = event.output_tokens_reported is True
        reasoning_reported = event.reasoning_output_tokens_reported is True
        total_reported = event.total_tokens_reported is True
        observed = _timestamp_us(event.started_at) or 0
        updated = _timestamp_us(event.updated_at) or observed
        if event.started_at is not None and _timestamp_us(event.started_at) is None:
            invalid_integer_values += 1
        if event.updated_at is not None and _timestamp_us(event.updated_at) is None:
            invalid_integer_values += 1
        cost_microusd = None
        if event.client_reported_cost_usd is not None:
            try:
                raw_cost = (
                    Decimal(str(event.client_reported_cost_usd))
                    * Decimal(1_000_000)
                ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                if raw_cost.is_finite() and 0 <= raw_cost <= SQLITE_INT64_MAX:
                    cost_microusd = int(raw_cost)
                else:
                    invalid_integer_values += 1
            except (ArithmeticError, ValueError):
                invalid_integer_values += 1

        def canonical_token(value: object, reported: bool) -> tuple[int | None, bool]:
            nonlocal invalid_integer_values
            if not reported:
                return None, False
            bounded = _sqlite_nonnegative_int(value)
            if bounded is None:
                invalid_integer_values += 1
                return None, False
            return bounded, True

        input_value, input_reported = canonical_token(
            event.input_tokens,
            input_reported,
        )
        output_value, output_reported = canonical_token(
            event.output_tokens,
            output_reported,
        )
        cached_value, cached_reported = canonical_token(
            event.cached_input_tokens,
            cached_reported,
        )
        cache_creation_value, cache_creation_reported = canonical_token(
            event.cache_creation_input_tokens,
            cache_creation_reported,
        )
        cache_read_value, cache_read_reported = canonical_token(
            event.cache_read_input_tokens,
            cache_read_reported,
        )
        reasoning_value, reasoning_reported = canonical_token(
            event.reasoning_output_tokens,
            reasoning_reported,
        )
        total_value, total_reported = canonical_token(
            event.total_tokens,
            total_reported,
        )
        representation = event.usage_representation or "codex-offline-snapshot"
        precedence = event.usage_precedence_role or "authoritative"
        if precedence == "enrichment" and additive:
            additive = False
            held_reason = "enrichment_not_totals_eligible"
        source_order = _sqlite_nonnegative_int(event.updated_at or 0)
        if source_order is None:
            invalid_integer_values += 1
            source_order = 0
        usage = UsageMeasurementInput(
            session_id=session_id,
            lane=str(event.usage_row_lane or "session_total")[:128],
            representation=representation,
            update_semantics="cumulative_snapshot",
            precedence_role=precedence,
            granularity="session",
            totals_eligible=additive,
            provider=event.provider,
            model=event.model,
            usage_confidence="partial",
            held_reason=held_reason,
            input_tokens=input_value,
            input_tokens_reported=input_reported,
            output_tokens=output_value,
            output_tokens_reported=output_reported,
            cached_input_tokens=cached_value,
            cached_input_tokens_reported=cached_reported,
            cache_creation_input_tokens=(
                cache_creation_value
            ),
            cache_creation_input_tokens_reported=cache_creation_reported,
            cache_read_input_tokens=cache_read_value,
            cache_read_input_tokens_reported=cache_read_reported,
            reasoning_output_tokens=(
                reasoning_value
            ),
            reasoning_output_tokens_reported=reasoning_reported,
            total_tokens=total_value,
            total_tokens_reported=total_reported,
            client_reported_cost_microusd=cost_microusd,
            source_order=source_order,
            observed_at_us=observed,
            updated_at_us=max(observed, updated),
        )
        result = repository.reconcile_usage(
            usage,
            checkpoint=("migration", f"codex-snapshot:{event.client_session_id}"[:256]),
        )
        _record(dispositions, f"usage_{result.disposition}")

    error_count = int(stats.get("error_count") or 0)
    excluded_by_limit = int(stats.get("excluded_by_limit") or 0)
    unparsed_selected_rows = int(stats.get("unparsed_selected_rows") or 0)
    coverage_failures: list[str] = []
    if not observations:
        coverage_failures.append("zero_observed_sessions")
    if not events:
        coverage_failures.append("zero_usage_events")
    if error_count:
        coverage_failures.append("reader_errors")
    if excluded_by_limit:
        coverage_failures.append("excluded_by_limit")
    if unparsed_selected_rows:
        coverage_failures.append("unparsed_selected_rows")
    if invalid_integer_values:
        coverage_failures.append("invalid_sqlite_integer")
    missing_rollout_paths = sorted(set(verified_rollouts) - result_source_paths)
    if missing_rollout_paths:
        coverage_failures.append("incomplete_rollout_coverage")

    return {
        "source_kind": "verified_codex_snapshot",
        "manifest_kind": snapshot.kind,
        "verified_files": len(files),
        "verified_bytes": sum(int(item.get("size_bytes") or 0) for item in files),
        "verified_rollout_files": len(verified_rollouts),
        "verified_rollout_bytes": sum(verified_rollouts.values()),
        "result_source_files": len(result_source_paths),
        "result_source_bytes": sum(verified_rollouts[path] for path in result_source_paths),
        "missing_rollout_files": len(missing_rollout_paths),
        "missing_rollout_path_digests": [
            canonical_hash(path).hex() for path in missing_rollout_paths[:100]
        ],
        "observed_sessions": len(observations),
        "usage_events": len(events),
        "accepted_edges": accepted_edges,
        "rejected_or_unresolved_edges": rejected_edges,
        "reader_diagnostics": {
            key: value
            for key, value in stats.items()
            if isinstance(value, (str, int, float, bool, type(None), list))
        },
        "invalid_sqlite_integer_values": invalid_integer_values,
        "coverage_passed": not coverage_failures,
        "coverage_failures": coverage_failures,
        "dispositions": dict(sorted(dispositions.items())),
    }


def _candidate_file_sizes(candidate: Path) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for label, suffix in (("main", ""), ("wal", "-wal"), ("shm", "-shm")):
        path = Path(f"{candidate}{suffix}")
        try:
            observed = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            sizes[label] = 0
            continue
        if not stat.S_ISREG(observed.st_mode):
            raise BenchmarkHookError(f"candidate {label} file is not regular")
        sizes[label] = int(observed.st_size)
    sizes["total"] = sum(sizes.values())
    return sizes


def _usage_field_presence(repository: CanonicalRepository) -> dict[str, Any]:
    """Return redacted counts that prove missing and explicit zero stay distinct."""

    measurement_rows = int(
        repository.connection.execute(
            "SELECT COUNT(*) FROM usage_measurements"
        ).fetchone()[0]
    )
    fields: dict[str, dict[str, int]] = {}
    for field_name in TOKEN_FIELDS:
        row = repository.connection.execute(
            f"SELECT "
            f"COALESCE(SUM({field_name}_reported), 0), "
            f"COALESCE(SUM(CASE WHEN {field_name}_reported = 0 THEN 1 ELSE 0 END), 0), "
            f"COALESCE(SUM(CASE WHEN {field_name}_reported = 1 "
            f"AND {field_name} = 0 THEN 1 ELSE 0 END), 0) "
            "FROM usage_measurements"
        ).fetchone()
        fields[field_name] = {
            "reported_rows": int(row[0]),
            "missing_rows": int(row[1]),
            "explicit_zero_rows": int(row[2]),
        }
    return {
        "measurement_rows": measurement_rows,
        "fields": fields,
    }


def _churn_evidence(
    repository: CanonicalRepository,
    *,
    candidate: Path,
) -> dict[str, Any]:
    """Measure 1.1M equivalent replay/reprice attempts at the write boundary."""

    source = repository.get_or_create_source(
        SourceInstanceInput(
            client="codex",
            adapter="synthetic-churn-probe-v1",
            representation="benchmark-probe",
            namespace_digest=canonical_hash({"benchmark": "churn-collapse-v1"}),
            namespace_scheme="sha256-benchmark-probe-v1",
            source_schema_version="churn-collapse-v1",
            privacy_label="synthetic:churn-collapse",
        )
    )
    session = repository.reconcile_session(
        SessionInput(
            source_instance_id=source.source_instance_id,
            client_session_id="benchmark-churn-collapse",
            session_kind="root",
            started_at_us=1_750_000_000_000_000,
            last_activity_at_us=1_750_000_000_000_000,
            observation_order=1,
        )
    )
    assert session.row_id is not None
    repository.get_or_create_task_anchor(session.row_id)
    event = {
        "provider": "codex",
        "model": "gpt-synthetic",
        "estimated_input_tokens": 100,
        "estimated_output_tokens": 10,
    }
    authoritative = _normal_usage(
        session.row_id,
        event,
        source_order=1,
        representation="churn-authoritative",
    )
    authoritative_result = repository.reconcile_usage(authoritative)
    assert authoritative_result.row_id is not None
    held = _normal_usage(
        session.row_id,
        event,
        source_order=1,
        representation="churn-held",
        totals_eligible=False,
        held_reason="authoritative_representation_available",
    )
    held_result = repository.reconcile_usage(held)
    assert held_result.row_id is not None

    usage_hash = repository.usage_content_hash(authoritative)
    price_v1 = CostCalculationInput(
        usage_measurement_id=authoritative_result.row_id,
        usage_content_hash=usage_hash,
        price_source="synthetic-catalog",
        price_version="v1",
        price_effective_at_us=1_750_000_000_000_000,
        formula_version="microusd-v1",
        completeness="complete",
        result_microusd=125,
        calculated_at_us=1_750_000_000_000_001,
    )
    price_v2 = CostCalculationInput(
        usage_measurement_id=authoritative_result.row_id,
        usage_content_hash=usage_hash,
        price_source="synthetic-catalog",
        price_version="v2",
        price_effective_at_us=1_750_000_000_000_002,
        formula_version="microusd-v1",
        completeness="complete",
        result_microusd=130,
        calculated_at_us=1_750_000_000_000_003,
    )
    initial_price = repository.record_cost(price_v1)
    valid_reprice = repository.record_cost(price_v2)
    held_price = CostCalculationInput(
        usage_measurement_id=held_result.row_id,
        usage_content_hash=repository.usage_content_hash(held),
        price_source="synthetic-catalog",
        price_version="v2",
        price_effective_at_us=1_750_000_000_000_002,
        formula_version="microusd-v1",
        completeness="complete",
        result_microusd=130,
        calculated_at_us=1_750_000_000_000_004,
    )

    sequence_before = repository.canonical_sequence()
    changes_before = repository.connection.total_changes
    sizes_before = _candidate_file_sizes(candidate)
    projection_rows_before = sum(
        int(repository.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("rm_task_current", "rm_usage_day")
    )
    diagnostics_before = sum(
        int(repository.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("source_conflicts", "migration_issues")
    )

    started = time.perf_counter()
    replay = repository.reconcile_usage_replay_batch(authoritative, attempts=1_000_000)
    held_reprice = repository.record_cost(held_price)
    identical_reprice = repository.record_cost(price_v2)
    elapsed = time.perf_counter() - started

    sequence_after = repository.canonical_sequence()
    changes_after = repository.connection.total_changes
    sizes_after = _candidate_file_sizes(candidate)
    projection_rows_after = sum(
        int(repository.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("rm_task_current", "rm_usage_day")
    )
    diagnostics_after = sum(
        int(repository.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("source_conflicts", "migration_issues")
    )
    database_growth = sizes_after["total"] - sizes_before["total"]
    gate_passed = all(
        (
            replay["canonical_writes"] == 0,
            replay["canonical_sequence_delta"] == 0,
            held_reprice.disposition == "noop",
            identical_reprice.disposition == "noop",
            changes_after == changes_before,
            sequence_after == sequence_before,
            projection_rows_after == projection_rows_before,
            diagnostics_after - diagnostics_before <= 3,
            database_growth < 10_000_000,
        )
    )
    return {
        "collapse_basis": "three validated equivalent batches before the SQLite write boundary",
        "equivalent_attempt_count": 1_100_000,
        "validated_physical_attempts": 3,
        "attempt_groups": {
            "unchanged_cumulative_refresh": {
                "logical_attempts": 1_000_000,
                "result": replay,
            },
            "held_non_additive_reprice": {
                "logical_attempts": 99_000,
                "disposition": held_reprice.disposition,
            },
            "identical_cost_recalculation": {
                "logical_attempts": 1_000,
                "disposition": identical_reprice.disposition,
            },
        },
        "valid_price_revisions": {
            "initial": initial_price.disposition,
            "new_version": valid_reprice.disposition,
        },
        "canonical_writes": changes_after - changes_before,
        "canonical_sequence_delta": sequence_after - sequence_before,
        "projection_row_delta": projection_rows_after - projection_rows_before,
        "diagnostic_row_delta": diagnostics_after - diagnostics_before,
        "database_growth_bytes": database_growth,
        "database_growth_limit_bytes": 10_000_000,
        "candidate_files_before": sizes_before,
        "candidate_files_after": sizes_after,
        "seconds": elapsed,
        "gate_passed": gate_passed,
    }


def _p95_ms(operation: Callable[[], object], *, warmups: int = 5, iterations: int = 25) -> float:
    for _ in range(warmups):
        operation()
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - started) * 1_000)
    samples.sort()
    index = max(0, math.ceil(0.95 * len(samples)) - 1)
    return samples[index]


def _query_evidence(repository: CanonicalRepository) -> dict[str, Any]:
    first_task_row = repository.connection.execute(
        "SELECT public_task_id FROM rm_task_current ORDER BY public_task_id LIMIT 1"
    ).fetchone()
    first_task = str(first_task_row[0]) if first_task_row is not None else "task_" + "0" * 32
    timings = {
        "work_p95_ms": _p95_ms(lambda: repository.list_tasks(limit=50)),
        "task_p95_ms": _p95_ms(lambda: repository.get_task(first_task)),
        "usage_p95_ms": _p95_ms(
            lambda: repository.usage_days(
                UsageDayQuery(
                    start_day="1970-01-01",
                    end_day="9999-12-31",
                    client="codex",
                    limit=400,
                )
            )
        ),
        "sessions_p95_ms": _p95_ms(lambda: repository.list_sessions(limit=100)),
        "health_p95_ms": _p95_ms(repository.health_summary),
    }
    plans = repository.explain_core_queries()
    unbounded_scans = [
        {"query": name, "detail": detail}
        for name, details in plans.items()
        for detail in details
        if "SCAN " in detail.upper()
    ]
    expected_markers = {
        "work": "idx_rm_task_work_recent",
        "task": "sqlite_autoindex_rm_task_current_1",
        "usage": "idx_rm_usage_client_range",
        "sessions": "idx_sessions_recent",
        "health": "INTEGER PRIMARY KEY",
    }
    index_expectations = {
        name: any(marker in detail for detail in plans.get(name, ()))
        for name, marker in expected_markers.items()
    }
    plan_gate_passed = not unbounded_scans and all(index_expectations.values())
    latency_budgets_ms = {
        "work_p95_ms": 750.0,
        "task_p95_ms": 750.0,
        "usage_p95_ms": 750.0,
        "sessions_p95_ms": 750.0,
        "health_p95_ms": 750.0,
    }
    latency_gate_passed = all(
        timings[name] <= budget for name, budget in latency_budgets_ms.items()
    )
    return {
        "p95_ms": timings,
        "plans": plans,
        "unbounded_core_scans": unbounded_scans,
        "index_expectations": index_expectations,
        "plan_gate_passed": plan_gate_passed,
        "latency_gate_passed": latency_gate_passed,
        "latency_budgets_ms": latency_budgets_ms,
        "query_budget_passed": latency_gate_passed and plan_gate_passed,
    }


def benchmark_sqlite_truth(request: object) -> Mapping[str, Any]:
    """Create and benchmark one explicit candidate database."""

    _request, mode, scratch, candidate, source = _validate_request(request)
    verified_snapshot = _verified_codex_snapshot(source) if mode == "snapshot" else None
    phases: dict[str, float] = {}
    started = time.perf_counter()
    try:
        with CanonicalStore.create(candidate, app_version="sqlite-truth-benchmark") as store:
            repository = store.repository()
            import_started = time.perf_counter()
            if mode == "normal":
                imported = _import_normal(repository, source, scratch=scratch)
            else:
                assert verified_snapshot is not None
                imported = _import_codex_snapshot(
                    repository,
                    source,
                    snapshot=verified_snapshot,
                )
            phases["import_seconds"] = time.perf_counter() - import_started

            churn = None
            if mode == "normal":
                churn_started = time.perf_counter()
                churn = _churn_evidence(repository, candidate=candidate)
                phases["churn_probe_seconds"] = time.perf_counter() - churn_started

            projection_started = time.perf_counter()
            projection = repository.rebuild_minimal_read_models()
            phases["projection_seconds"] = time.perf_counter() - projection_started
            phases["reconciliation_seconds"] = (
                phases["import_seconds"] + phases["projection_seconds"]
            )

            query_started = time.perf_counter()
            query = _query_evidence(repository)
            phases["query_evidence_seconds"] = time.perf_counter() - query_started
            checks = store.quick_check()
            counts = dict(repository.table_counts())
            usage_presence = _usage_field_presence(repository)
            candidate_bytes = store.file_bytes()
            candidate_files = _candidate_file_sizes(candidate)
            canonical_sequence = repository.canonical_sequence()

            gates = {
                "sqlite_integrity": bool(checks.get("ok")),
                "bounded_query_latency_and_plans": bool(query["query_budget_passed"]),
            }
            if mode == "normal":
                assert churn is not None
                gates["equivalent_churn_collapse"] = bool(churn["gate_passed"])
                unchanged = imported.get("unchanged_reconciliation")
                gates["exact_replay_zero_write"] = bool(
                    isinstance(unchanged, Mapping)
                    and unchanged.get("gate_passed") is True
                )
            else:
                gates["snapshot_coverage"] = bool(imported.get("coverage_passed"))
            acceptance_passed = all(gates.values())
    finally:
        if verified_snapshot is not None:
            verified_snapshot.verify_unchanged()

    phases["hook_total_seconds"] = time.perf_counter() - started
    return {
        "status": "passed" if acceptance_passed else "failed",
        "acceptance_passed": acceptance_passed,
        "gates": gates,
        "mode": mode,
        "import": imported,
        "churn": churn,
        "projection": projection,
        "query": query,
        "checks": checks,
        "table_counts": counts,
        "usage_field_presence": usage_presence,
        "candidate_bytes_with_wal": candidate_bytes,
        "candidate_files_open": candidate_files,
        "canonical_sequence": canonical_sequence,
        "timings": phases,
    }


__all__ = ["BenchmarkHookError"]
