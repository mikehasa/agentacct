"""Capture stored inputs once and materialize the existing public work views.

The version vector is sampled BEFORE capture. Appends while reading/building
therefore leave the result marked updating; they do not starve publication.
Destructive changes invalidate the entire capture. Separate stores are not a
cross-database transaction: all views are one coherent reduction of the same
captured inputs, not a claim of an atomic timestamp across independent writers.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .evidence_store import EVIDENCE_STORE_DIRNAME, EVIDENCE_PROJECTION_FILENAME, read_evidence_snapshot_state
from .service import SentinelService


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _stat(path: Path) -> tuple[int, ...] | None:
    try:
        value = path.stat()
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns
    except FileNotFoundError:
        return None


def snapshot_input_state(store_dir: Path | str, service: SentinelService) -> tuple[str, str]:
    """Bounded metadata reads only; never decode events, recover evidence or build.

File-backed inputs conservatively invalidate on ANY change. SQLite append-only
activity can retain a previous result, but UPDATE/DELETE/REPLACE cannot. Physical
identity also rejects a replaced database whose logical revision was restored.
"""
    root = Path(store_dir).expanduser().resolve()
    safety: dict[str, Any] = {"root": str(root)}
    versions: dict[str, Any] = {}
    if service.event_log is not None:
        path = service.event_log.db_path
        before = _stat(path)
        try:
            state = service.event_log.snapshot_state()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            # Restoring a pre-snapshot backup must revoke old content but still
            # allow the background worker to install its state/triggers again.
            state = None
        after = _stat(path)
        if before is None or after is None or before[:2] != after[:2]:
            raise RuntimeError("event database changed during read")
        if state is None:
            safety["events"] = ("legacy", before, _stat(Path(str(path) + "-wal")))
        else:
            safety["events"] = (before[:2], state.database_id, state.destructive_revision, state.schema_cookie)
            versions["events"] = state.revision
    # Legacy files may be absorbed on the next worker open. Include them even
    # in SQLite mode: an older client can still append to that bridge.
    safety["legacy_events"] = _stat(root / "events.jsonl")
    identity_path = root / "task-identity" / "secret.key"
    identity_stat = _stat(identity_path)
    if identity_stat is None:
        safety["identity"] = None
    else:
        # TaskIdentityCodec reapplies mode 0600 on open (changing ctime).
        # Hash the bounded secret itself so harmless chmod is not a rebuild,
        # while same-size/mtime-preserving key rotation is still rejected.
        with identity_path.open("rb") as handle:
            secret = handle.read(33)
        if len(secret) != 32 or _stat(identity_path)[:2] != identity_stat[:2]:
            raise RuntimeError("task identity changed during read")
        safety["identity"] = (identity_stat[:2], hashlib.sha256(secret).hexdigest())
    safety["continuations"] = _stat(root / "continuation-tasks" / "actions.jsonl")
    safety["cost"] = _stat(root / "cost_events.jsonl")
    evidence = root / EVIDENCE_STORE_DIRNAME / EVIDENCE_PROJECTION_FILENAME
    before = _stat(evidence)
    if before is not None and service.evidence.enabled:
        try:
            state = read_evidence_snapshot_state(evidence)
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            # An old database has no epoch yet. The worker installs it; until
            # then every mutation invalidates the conservative file signature.
            safety["evidence"] = ("legacy", before, _stat(Path(str(evidence) + "-wal")))
        else:
            after = _stat(evidence)
            if after is None or before[:2] != after[:2]:
                raise RuntimeError("evidence database changed during read")
            safety["evidence"] = (before[:2], state.database_id, state.mechanical_destructive_revision, state.schema_version, state.schema_cookie)
            versions["evidence"] = state.mechanical_revision
    else:
        safety["evidence"] = (None, service.evidence.enabled)
    # Spool changes wake recovery in the worker, but do not themselves authorize
    # an old payload after a destructive projection mutation (the epoch does).
    versions["spools"] = [_stat(evidence.parent / name) for name in ("spool.jsonl", "refreshable-usage.jsonl")]
    runs_root = root / "runs"
    runs = []
    if runs_root.exists():
        for child in runs_root.iterdir():
            if child.is_dir():
                runs.append((child, child.stat().st_mtime_ns))
    runs.sort(key=lambda item: item[1], reverse=True)
    safety["runs"] = [
        (child.name, stamp, [_stat(child / name) for name in ("metadata.json", "outcome.json")])
        for child, stamp in runs[:100]
    ]
    return _digest([safety, versions]), _digest(safety)


def session_snapshot_key(client: str, session_id: str) -> str:
    return json.dumps([client, session_id], separators=(",", ":"))


def _capture_run_reports(service: SentinelService) -> list[dict[str, Any]]:
    """Capture only the canonical report fields consumed by the work ledger.

    Full reports also reread costs several times and tail stdout/stderr, none
    of which participates in receipt construction. Do not read raw logs here.
    """
    from .outcome import read_outcome
    from .reports import default_outcome_schema
    reports = []
    for run in service.list_runs(limit=100):
        run_id = str(run.get("run_id") or "")
        if not run_id:
            continue
        try:
            metadata = service.store.read_metadata(run_id)
            outcome = read_outcome(service.store, run_id) or default_outcome_schema(metadata)
        except (FileNotFoundError, ValueError):
            continue
        # Match build_run_report_payload: its run block deliberately carries
        # no started_at/ended_at, so adding them here would change check time.
        reports.append({"run": {"run_id": run_id}, "outcome": outcome})
    return reports


def build_snapshot_entries(store_dir: Path | str, *, service: SentinelService | None = None
                           ) -> tuple[dict[tuple[str, str], dict[str, Any]], str, str, float]:
    # Lazy import avoids an api -> manager -> builder -> api import cycle.
    from .api import (
        _mechanical_projection_envelopes_for,
        _dashboard_page_data, _dashboard_task_projection, _dashboard_receipt_attention,
        _stamp_task_plan_shares, _store_scope_and_label, _store_display_label,
        _project_identity, _task_title, V1_ATTENTION_SCHEMA_VERSION,
    )
    from .cost import CostLedger
    from .task_continuations import ContinuationTaskStore
    from .task_identity import TaskIdentityCodec
    from .session_observations import build_session_observations
    from .mechanical_checks import build_mechanical_check_events
    from .receipt import RECEIPT_SCHEMA_VERSION, build_receipt, build_receipt_summary, latest_store_activity, session_start_index
    from .receipt import build_attention_reason
    from .task_timeline import build_timeline_events
    from .v1_sessions import build_v1_sessions_view, build_v1_session_detail

    root = Path(store_dir).expanduser().resolve()
    service = service or SentinelService(root)
    TaskIdentityCodec(root)  # initialize before the first version-vector read
    # Initialization/replay is background work. Do it before sampling the token,
    # so migration alone does not make every first capture immediately obsolete.
    if service.evidence.enabled and (root / EVIDENCE_STORE_DIRNAME).exists():
        service.evidence.store
    input_token, safety_token = snapshot_input_state(root, service)
    captured_at = time.time()
    identity = TaskIdentityCodec(root)
    events = service.list_all_events()
    cost_events = sorted(CostLedger(root).read_events(), key=lambda row: float(row.get("created_at") or 0), reverse=True)
    reports = _capture_run_reports(service)
    envelopes, diagnostics = _mechanical_projection_envelopes_for(service, root)
    if diagnostics.get("read_errors"):
        raise RuntimeError("evidence capture failed")
    continuations = ContinuationTaskStore(root).project().to_dict()
    if snapshot_input_state(root, service)[1] != safety_token:
        raise RuntimeError("receipt inputs changed destructively during capture")
    scope, label = _store_scope_and_label(root)
    observations = build_session_observations(envelopes,
        default_project_label=label if scope == "project" else None, diagnostics=diagnostics) if envelopes else []
    data = _dashboard_page_data(events=events, cost_events=cost_events, run_reports=reports,
        session_observations=observations, session_observation_diagnostics=diagnostics,
        mechanical_check_events=build_mechanical_check_events(envelopes),
        store_project_label=label, store_project_identity=_project_identity(str(root.parent.parent)) if scope == "project" else None,
        store_scope=scope, store_label=_store_display_label(root, scope, label),
        continuation_projection=continuations, task_identity=identity)
    projection = _dashboard_task_projection(data)
    _stamp_task_plan_shares(projection, events)
    tasks = [task for task in projection.get("tasks", []) if task.get("public_task_id")]
    latest, starts = latest_store_activity(tasks), session_start_index(tasks)
    tasks.sort(key=lambda task: float(task.get("last_activity_at") or 0), reverse=True)
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    summaries = []
    attention = []
    counts = {"failed_check": 0, "failed_step": 0, "blocker": 0}
    for task in tasks:
        task_id = str(task["public_task_id"])
        kwargs = dict(public_task_id=task_id, title=_task_title(task), latest_store_activity_at=latest, session_starts=starts)
        row = build_receipt_summary(task, **kwargs)
        summaries.append(row)
        entries["receipt", task_id] = build_receipt(task, **kwargs)
        entries["timeline", task_id] = {"events": build_timeline_events(task)}
        reason = build_attention_reason(task, latest_store_activity_at=latest, session_starts=starts)
        if reason is not None:
            order, detail = reason
            counts[str(detail["kind"])] += 1
            attention.append((order, -float(task.get("last_activity_at") or 0), task_id, {**row, "attention": detail}))
    entries["tasks", "all"] = {"schema": RECEIPT_SCHEMA_VERSION, "tasks": summaries,
        "attention": _dashboard_receipt_attention(tasks, latest_store_activity_at=latest, session_starts=starts)}
    attention.sort(key=lambda row: row[:3])
    items = [row[3] for row in attention]
    entries["attention", "all"] = {"schema": V1_ATTENTION_SCHEMA_VERSION, "items": items, "counts": counts, "snapshot": _digest(items)}
    view = build_v1_sessions_view(data.ledger, events, now=captured_at)
    entries["sessions", "all"] = {key: view[key] for key in ("generated_at", "rows", "plan", "total_sessions", "total_root_sessions")}
    for row in view["rows"]:
        client, session = str(row["client"]), str(row["client_session_id"])
        detail = build_v1_session_detail(view, data.ledger, client=client, session_id=session, enrich_roles=False)
        if detail is not None:
            entries["session", session_snapshot_key(client, session)] = detail
    if snapshot_input_state(root, service)[1] != safety_token:
        raise RuntimeError("receipt inputs changed destructively during build")
    return entries, input_token, safety_token, captured_at
