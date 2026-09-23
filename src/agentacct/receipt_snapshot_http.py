"""The explicit, nonblocking HTTP read lane for native work views."""
from __future__ import annotations

import hashlib
from typing import Any, Callable

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from .receipt_snapshot_builder import session_snapshot_key
from .receipt_snapshot_runtime import ReceiptSnapshotManager
from .task_timeline import SCHEMA as TIMELINE_SCHEMA
from .v1_sessions import slice_sessions_payload


class SnapshotWorkReader:
    def __init__(self, manager: ReceiptSnapshotManager, input_state: Callable[[], tuple[str, str]]) -> None:
        self.manager = manager
        self.input_state = input_state

    def response(self, kind: str, *, key: str = "all", limit: int = 50, offset: int = 0,
                 roots_only: bool = True, client: str | None = None, session_id: str | None = None,
                 cursor: str | None = None) -> JSONResponse:
        if kind == "session":
            key = session_snapshot_key(str(client), str(session_id))
        entry = self.manager.read(kind, key)
        status = self.manager.status()
        if entry is None:
            # Publication may have added this key between the two reads. Do
            # not issue a false 404 for an entry in that completed generation.
            if status["state"] == "current":
                entry = self.manager.read(kind, key)
        if entry is None:
            if status["state"] == "current":
                raise HTTPException(status_code=404, detail="unknown work for this store")
            return JSONResponse({"detail": "Preparing work receipts", "projection": status}, status_code=202)
        try:
            input_token, safety_token = self.input_state()
        except Exception:
            safety_token = None
            input_token = None
        if safety_token != entry.safety_token:
            return JSONResponse({"detail": "Preparing work receipts", "projection": {
                "state": "pending", "built_at": None, "generation": None, "available": False,
            }}, status_code=202)
        # Metadata belongs to the payload's generation, even if publication
        # occurred between manager.read and manager.status.
        status.update(built_at=entry.built_at, generation=entry.generation_id, available=True)
        status["state"] = "current" if input_token == entry.input_token else "error" if status.get("error") else "updating"
        if status["state"] == "current":
            status["error"] = None
        payload = entry.payload
        if kind in {"tasks", "attention"}:
            field = "tasks" if kind == "tasks" else "items"
            rows = payload[field]
            payload = {**payload, field: rows[offset:offset + limit], "total": len(rows),
                       "offset": offset, "limit": limit, "truncated": offset + limit < len(rows)}
        elif kind == "sessions":
            payload = slice_sessions_payload(payload, roots_only=roots_only, limit=limit, offset=offset, client=client)
        elif kind == "timeline":
            # Cursors bind task + generation. A replaced generation expires the
            # cursor explicitly (409) instead of mixing pages from two builds.
            task_hash = hashlib.sha256(key.encode()).hexdigest()[:16]
            if cursor is not None:
                try:
                    generation, scope, encoded = cursor.split(":")
                    offset = int(encoded)
                    if generation != entry.generation_id or scope != task_hash or str(offset) != encoded or offset < 0:
                        raise ValueError()
                except (ValueError, TypeError):
                    raise HTTPException(status_code=409, detail="Timeline history expired; reload the task") from None
            rows = payload["events"]
            if offset > len(rows):
                raise HTTPException(status_code=409, detail="Timeline history expired; reload the task")
            end = len(rows) - offset
            start = max(0, end - limit)
            shown = end - start
            payload = {"schema_version": TIMELINE_SCHEMA, "task_id": key,
                "snapshot_id": f"{entry.generation_id}-{task_hash}", "events": rows[start:end],
                "offset": offset, "shown": shown, "total": len(rows), "truncated": start > 0,
                "next_cursor": f"{entry.generation_id}:{task_hash}:{offset + shown}" if start else None}
        return JSONResponse({**payload, "projection": status})
