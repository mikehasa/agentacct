"""Append-only user-confirmed grouping of continuation client sessions.

The imported client session remains the canonical identity.  This store owns a
small, separate product-layer ledger that records only explicit user choices to
group two or more *root* sessions as one agentacct task.  A singleton session
needs no record.  Parent/child resolution deliberately stays outside this
module; callers must pass the root session identities they want to group.

``<store>/continuation-tasks/actions.jsonl`` is the source of truth.  Every
complete action is locked, flushed, and fsynced before returning.  Projection is
a deterministic replay of that append-only file; invalid/torn records remain on
disk and surface as issues without gaining state-changing power.  The module
never reads or rewrites the historical ``events.jsonl`` ledger.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


CONTINUATION_ACTION_SCHEMA_VERSION = "agent-chronicle.continuation-action.v1"
CONTINUATION_PROJECTION_SCHEMA_VERSION = "agent-chronicle.continuation-projection.v1"
CONTINUATION_STORE_DIRNAME = "continuation-tasks"
CONTINUATION_ACTIONS_FILENAME = "actions.jsonl"

USER_CONFIRMATION = "user_confirmed"

_ACTION_TYPES = frozenset({"create", "link", "unlink", "rename"})
_POSITIONS = frozenset({"append", "prepend"})
_ACTION_ID_RE = re.compile(r"cta_[0-9a-f]{32}")
_TASK_ID_RE = re.compile(r"ctask_[0-9a-f]{32}")
_HASH_RE = re.compile(r"sha256:[0-9a-f]{64}")


class ContinuationTaskError(ValueError):
    """Raised when a requested task/session transition would be invalid."""


def _owner_only(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        # Parent ACLs can still enforce privacy when chmod is unavailable.
        pass


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("occurred_at must be a timezone-aware ISO timestamp")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("occurred_at must be a timezone-aware ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("occurred_at must be timezone-aware")
    return value


def _clean_text(value: Any, name: str, *, maximum: int, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned and not optional:
        raise ValueError(f"{name} must not be empty")
    if len(cleaned) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
        raise ValueError(f"{name} must not contain control characters")
    return cleaned or None


def _validate_task_id(value: Any, name: str = "task_id") -> str:
    if not isinstance(value, str) or not _TASK_ID_RE.fullmatch(value):
        raise ValueError(f"{name} is invalid")
    return value


@dataclass(frozen=True, order=True)
class ClientSessionRef:
    """A client-scoped root session identity.

    Session ids are not assumed to be globally unique.  Cross-client links are
    allowed only because this composite identity records the client namespace.
    """

    client: str
    client_session_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "client", _clean_text(self.client, "client", maximum=80))
        object.__setattr__(
            self,
            "client_session_id",
            _clean_text(self.client_session_id, "client_session_id", maximum=240),
        )

    @property
    def key(self) -> tuple[str, str]:
        return (self.client, self.client_session_id)

    def to_dict(self) -> dict[str, str]:
        return {"client": self.client, "client_session_id": self.client_session_id}

    @classmethod
    def from_dict(cls, value: Any) -> "ClientSessionRef":
        if not isinstance(value, Mapping):
            raise ValueError("session reference must be an object")
        if set(value) != {"client", "client_session_id"}:
            raise ValueError("session reference contains unsupported fields")
        return cls(client=value.get("client"), client_session_id=value.get("client_session_id"))


@dataclass(frozen=True)
class ContinuationAction:
    """One immutable user-confirmed action in the continuation ledger."""

    action_id: str
    action_type: str
    task_id: str
    occurred_at: str
    created_by: str
    sessions: tuple[ClientSessionRef, ...] = ()
    session: ClientSessionRef | None = None
    source_task_id: str | None = None
    position: str | None = None
    title: str | None = None
    record_hash: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not _ACTION_ID_RE.fullmatch(self.action_id):
            raise ValueError("action_id is invalid")
        if self.action_type not in _ACTION_TYPES:
            raise ValueError("unsupported continuation action")
        _validate_task_id(self.task_id)
        _validate_timestamp(self.occurred_at)
        object.__setattr__(
            self,
            "created_by",
            _clean_text(self.created_by, "created_by", maximum=120),
        )
        if self.source_task_id is not None:
            _validate_task_id(self.source_task_id, "source_task_id")
        if self.position is not None and self.position not in _POSITIONS:
            raise ValueError("position must be append or prepend")
        object.__setattr__(self, "title", _clean_text(self.title, "title", maximum=240, optional=True))

        if any(not isinstance(item, ClientSessionRef) for item in self.sessions):
            raise ValueError("sessions must contain ClientSessionRef values")
        if self.session is not None and not isinstance(self.session, ClientSessionRef):
            raise ValueError("session must be a ClientSessionRef")
        session_keys = [item.key for item in self.sessions]
        if len(session_keys) != len(set(session_keys)):
            raise ValueError("sessions must be distinct")
        if self.action_type == "create":
            if len(self.sessions) < 2:
                raise ValueError("create requires at least two sessions")
            if self.session is not None or self.source_task_id is not None or self.position is not None:
                raise ValueError("create contains unsupported action fields")
        elif self.action_type == "link":
            if bool(self.session) == bool(self.source_task_id):
                raise ValueError("link requires exactly one session or source_task_id")
            if self.sessions or self.title is not None:
                raise ValueError("link contains unsupported action fields")
            if self.position is None:
                raise ValueError("link requires a position")
            if self.source_task_id == self.task_id:
                raise ValueError("a task cannot be merged into itself")
        elif self.action_type == "unlink":
            if self.session is None:
                raise ValueError("unlink requires a session")
            if self.sessions or self.source_task_id is not None or self.position is not None or self.title is not None:
                raise ValueError("unlink contains unsupported action fields")
        elif self.action_type == "rename":
            if (
                self.sessions
                or self.session is not None
                or self.source_task_id is not None
                or self.position is not None
            ):
                raise ValueError("rename contains unsupported action fields")

        expected_hash = _digest(self._content_dict())
        if self.record_hash and self.record_hash != expected_hash:
            raise ValueError("continuation action record_hash mismatch")
        object.__setattr__(self, "record_hash", expected_hash)

    @classmethod
    def create(
        cls,
        *,
        action_type: str,
        task_id: str,
        created_by: str,
        sessions: Sequence[ClientSessionRef] = (),
        session: ClientSessionRef | None = None,
        source_task_id: str | None = None,
        position: str | None = None,
        title: str | None = None,
    ) -> "ContinuationAction":
        return cls(
            action_id=f"cta_{uuid.uuid4().hex}",
            action_type=action_type,
            task_id=task_id,
            occurred_at=_utc_now(),
            created_by=created_by,
            sessions=tuple(sessions),
            session=session,
            source_task_id=source_task_id,
            position=position,
            title=title,
        )

    def _content_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": CONTINUATION_ACTION_SCHEMA_VERSION,
            "action_id": self.action_id,
            "action_type": self.action_type,
            "task_id": self.task_id,
            "occurred_at": self.occurred_at,
            "created_by": self.created_by,
            "confirmation": USER_CONFIRMATION,
        }
        if self.action_type == "create":
            payload["sessions"] = [item.to_dict() for item in self.sessions]
            payload["title"] = self.title
        elif self.action_type == "link":
            payload["position"] = self.position
            if self.session is not None:
                payload["session"] = self.session.to_dict()
            else:
                payload["source_task_id"] = self.source_task_id
        elif self.action_type == "unlink":
            payload["session"] = self.session.to_dict() if self.session is not None else None
        elif self.action_type == "rename":
            payload["title"] = self.title
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {**self._content_dict(), "record_hash": self.record_hash}

    @classmethod
    def from_dict(cls, value: Any) -> "ContinuationAction":
        if not isinstance(value, Mapping):
            raise ValueError("continuation action must be an object")
        action_type = value.get("action_type")
        common = {
            "schema_version",
            "action_id",
            "action_type",
            "task_id",
            "occurred_at",
            "created_by",
            "confirmation",
            "record_hash",
        }
        action_fields = {
            "create": {"sessions", "title"},
            "link": {"session", "source_task_id", "position"},
            "unlink": {"session"},
            "rename": {"title"},
        }
        required_action_fields = {
            "create": {"sessions", "title"},
            "link": {"position"},
            "unlink": {"session"},
            "rename": {"title"},
        }
        if action_type not in action_fields:
            raise ValueError("unsupported continuation action")
        allowed = common | action_fields[str(action_type)]
        required = common | required_action_fields[str(action_type)]
        if not set(value).issubset(allowed) or not required.issubset(value):
            raise ValueError("continuation action contains unsupported or missing fields")
        if value.get("schema_version") != CONTINUATION_ACTION_SCHEMA_VERSION:
            raise ValueError("unsupported continuation action schema")
        if value.get("confirmation") != USER_CONFIRMATION:
            raise ValueError("continuation action is not user-confirmed")
        if not isinstance(value.get("record_hash"), str) or not _HASH_RE.fullmatch(value["record_hash"]):
            raise ValueError("continuation action record_hash is invalid")
        sessions_value = value.get("sessions") or ()
        if not isinstance(sessions_value, (list, tuple)):
            raise ValueError("sessions must be a list")
        session_value = value.get("session")
        return cls(
            action_id=value.get("action_id"),
            action_type=str(action_type),
            task_id=value.get("task_id"),
            occurred_at=value.get("occurred_at"),
            created_by=value.get("created_by"),
            sessions=tuple(ClientSessionRef.from_dict(item) for item in sessions_value),
            session=ClientSessionRef.from_dict(session_value) if session_value is not None else None,
            source_task_id=value.get("source_task_id"),
            position=value.get("position"),
            title=value.get("title"),
            record_hash=value.get("record_hash"),
        )


@dataclass(frozen=True)
class TaskSessionMembership:
    session: ClientSessionRef
    ordinal: int
    role: str
    linked_at: str
    linked_by: str
    linked_action_id: str
    confirmation: str = USER_CONFIRMATION

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session.to_dict(),
            "ordinal": self.ordinal,
            "role": self.role,
            "linked_at": self.linked_at,
            "linked_by": self.linked_by,
            "linked_action_id": self.linked_action_id,
            "confirmation": self.confirmation,
        }


@dataclass(frozen=True)
class ContinuationTask:
    task_id: str
    title: str | None
    memberships: tuple[TaskSessionMembership, ...]
    created_at: str
    created_by: str
    updated_at: str
    merged_from_task_ids: tuple[str, ...] = ()

    @property
    def sessions(self) -> tuple[ClientSessionRef, ...]:
        return tuple(item.session for item in self.memberships)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "memberships": [item.to_dict() for item in self.memberships],
            "created_at": self.created_at,
            "created_by": self.created_by,
            "updated_at": self.updated_at,
            "merged_from_task_ids": list(self.merged_from_task_ids),
        }


@dataclass(frozen=True)
class ContinuationProjectionIssue:
    line_number: int
    action_id: str | None
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_number": self.line_number,
            "action_id": self.action_id,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True)
class ContinuationProjection:
    tasks: tuple[ContinuationTask, ...] = ()
    task_aliases: tuple[tuple[str, str], ...] = ()
    inactive_task_ids: tuple[str, ...] = ()
    applied_action_ids: tuple[str, ...] = ()
    issues: tuple[ContinuationProjectionIssue, ...] = ()
    invalid_record_count: int = 0

    def resolve_task_id(self, task_id: str) -> str:
        aliases = dict(self.task_aliases)
        seen: set[str] = set()
        current = task_id
        while current in aliases and current not in seen:
            seen.add(current)
            current = aliases[current]
        return current

    def get_task(self, task_id: str) -> ContinuationTask | None:
        resolved = self.resolve_task_id(task_id)
        return next((task for task in self.tasks if task.task_id == resolved), None)

    def task_for(self, session: ClientSessionRef) -> ContinuationTask | None:
        return next((task for task in self.tasks if session in task.sessions), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONTINUATION_PROJECTION_SCHEMA_VERSION,
            "tasks": [task.to_dict() for task in self.tasks],
            "session_task_index": [
                {"session": membership.session.to_dict(), "task_id": task.task_id}
                for task in self.tasks
                for membership in task.memberships
            ],
            "task_aliases": dict(self.task_aliases),
            "inactive_task_ids": list(self.inactive_task_ids),
            "applied_action_ids": list(self.applied_action_ids),
            "issues": [issue.to_dict() for issue in self.issues],
            "invalid_record_count": self.invalid_record_count,
        }


@dataclass(frozen=True)
class TaskMutationResult:
    changed: bool
    task_id: str | None
    action_id: str | None
    action_type: str | None
    projection: ContinuationProjection


@dataclass
class _MutableMembership:
    session: ClientSessionRef
    linked_at: str
    linked_by: str
    linked_action_id: str


@dataclass
class _MutableTask:
    task_id: str
    title: str | None
    memberships: list[_MutableMembership]
    created_at: str
    created_by: str
    updated_at: str
    created_sequence: int
    merged_from_task_ids: set[str] = field(default_factory=set)


@dataclass
class _ProjectionState:
    tasks: dict[str, _MutableTask] = field(default_factory=dict)
    session_to_task: dict[tuple[str, str], str] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    inactive_task_ids: set[str] = field(default_factory=set)
    seen_action_ids: set[str] = field(default_factory=set)
    applied_action_ids: list[str] = field(default_factory=list)
    issues: list[ContinuationProjectionIssue] = field(default_factory=list)
    invalid_record_count: int = 0
    last_line_number: int = 0


def _resolve_alias(state: _ProjectionState, task_id: str) -> str:
    current = task_id
    seen: set[str] = set()
    while current in state.aliases:
        if current in seen:
            raise ContinuationTaskError("task alias cycle detected")
        seen.add(current)
        current = state.aliases[current]
    return current


def _active_task(state: _ProjectionState, task_id: str) -> _MutableTask:
    resolved = _resolve_alias(state, task_id)
    task = state.tasks.get(resolved)
    if task is None:
        raise ContinuationTaskError(f"task is not active: {task_id}")
    return task


def _apply_action(state: _ProjectionState, action: ContinuationAction, *, sequence: int) -> None:
    if action.action_id in state.seen_action_ids:
        raise ContinuationTaskError("duplicate action_id")
    state.seen_action_ids.add(action.action_id)

    if action.action_type == "create":
        if (
            action.task_id in state.tasks
            or action.task_id in state.aliases
            or action.task_id in state.inactive_task_ids
        ):
            raise ContinuationTaskError("task_id already exists")
        conflicting = [item for item in action.sessions if item.key in state.session_to_task]
        if conflicting:
            raise ContinuationTaskError("a session already belongs to an active task")
        task = _MutableTask(
            task_id=action.task_id,
            title=action.title,
            memberships=[
                _MutableMembership(
                    session=item,
                    linked_at=action.occurred_at,
                    linked_by=action.created_by,
                    linked_action_id=action.action_id,
                )
                for item in action.sessions
            ],
            created_at=action.occurred_at,
            created_by=action.created_by,
            updated_at=action.occurred_at,
            created_sequence=sequence,
        )
        state.tasks[task.task_id] = task
        for item in action.sessions:
            state.session_to_task[item.key] = task.task_id

    elif action.action_type == "link" and action.session is not None:
        task = _active_task(state, action.task_id)
        if action.session.key in state.session_to_task:
            raise ContinuationTaskError("a session already belongs to an active task")
        membership = _MutableMembership(
            session=action.session,
            linked_at=action.occurred_at,
            linked_by=action.created_by,
            linked_action_id=action.action_id,
        )
        if action.position == "prepend":
            task.memberships.insert(0, membership)
        else:
            task.memberships.append(membership)
        task.updated_at = action.occurred_at
        state.session_to_task[action.session.key] = task.task_id

    elif action.action_type == "link":
        target = _active_task(state, action.task_id)
        source = _active_task(state, action.source_task_id or "")
        if target.task_id == source.task_id:
            raise ContinuationTaskError("tasks already resolve to the same active task")
        source_members = list(source.memberships)
        if action.position == "prepend":
            target.memberships = source_members + target.memberships
        else:
            target.memberships.extend(source_members)
        if target.title is None:
            target.title = source.title
        target.updated_at = action.occurred_at
        target.merged_from_task_ids.update(source.merged_from_task_ids)
        target.merged_from_task_ids.add(source.task_id)
        del state.tasks[source.task_id]
        state.inactive_task_ids.add(source.task_id)
        state.aliases[source.task_id] = target.task_id
        for alias, destination in list(state.aliases.items()):
            if destination == source.task_id:
                state.aliases[alias] = target.task_id
        for member in source_members:
            state.session_to_task[member.session.key] = target.task_id

    elif action.action_type == "unlink":
        task = _active_task(state, action.task_id)
        assert action.session is not None
        if state.session_to_task.get(action.session.key) != task.task_id:
            raise ContinuationTaskError("session is not a member of the task")
        task.memberships = [item for item in task.memberships if item.session != action.session]
        state.session_to_task.pop(action.session.key, None)
        task.updated_at = action.occurred_at
        if len(task.memberships) < 2:
            # A singleton returns to the default derived state: no active,
            # persisted agentacct task is needed for either session.
            for remaining in task.memberships:
                state.session_to_task.pop(remaining.session.key, None)
            del state.tasks[task.task_id]
            state.inactive_task_ids.add(task.task_id)

    elif action.action_type == "rename":
        task = _active_task(state, action.task_id)
        task.title = action.title
        task.updated_at = action.occurred_at

    state.applied_action_ids.append(action.action_id)


def _freeze(state: _ProjectionState) -> ContinuationProjection:
    tasks: list[ContinuationTask] = []
    for task in sorted(state.tasks.values(), key=lambda item: (item.created_sequence, item.task_id)):
        memberships = tuple(
            TaskSessionMembership(
                session=item.session,
                ordinal=index,
                role="primary" if index == 0 else "continuation",
                linked_at=item.linked_at,
                linked_by=item.linked_by,
                linked_action_id=item.linked_action_id,
            )
            for index, item in enumerate(task.memberships)
        )
        tasks.append(
            ContinuationTask(
                task_id=task.task_id,
                title=task.title,
                memberships=memberships,
                created_at=task.created_at,
                created_by=task.created_by,
                updated_at=task.updated_at,
                merged_from_task_ids=tuple(sorted(task.merged_from_task_ids)),
            )
        )
    return ContinuationProjection(
        tasks=tuple(tasks),
        task_aliases=tuple(sorted(state.aliases.items())),
        inactive_task_ids=tuple(sorted(state.inactive_task_ids)),
        applied_action_ids=tuple(state.applied_action_ids),
        issues=tuple(state.issues),
        invalid_record_count=state.invalid_record_count,
    )


class ContinuationTaskStore:
    """Durable append-only store for explicit root-session task membership."""

    def __init__(self, root: Path | str) -> None:
        if root is None:
            raise ValueError("continuation task store root is required")
        self.root = Path(root).expanduser()
        self.continuation_root = self.root / CONTINUATION_STORE_DIRNAME
        self.actions_path = self.continuation_root / CONTINUATION_ACTIONS_FILENAME
        self.lock_path = self.continuation_root / ".actions.lock"

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        self.continuation_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _owner_only(self.continuation_root, 0o700)
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _append_unlocked(self, action: ContinuationAction) -> None:
        serialized = _canonical_json_bytes(action.to_dict()) + b"\n"
        existed = self.actions_path.exists()
        with self.actions_path.open("a+b") as handle:
            try:
                os.fchmod(handle.fileno(), 0o600)
            except OSError:
                pass
            handle.seek(0, os.SEEK_END)
            if handle.tell():
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    # Preserve the torn bytes and make the next action a fresh,
                    # independently parseable line.
                    handle.seek(0, os.SEEK_END)
                    handle.write(b"\n")
            handle.seek(0, os.SEEK_END)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        if not existed:
            self._fsync_directory(self.continuation_root)

    def _load_state_unlocked(self) -> _ProjectionState:
        state = _ProjectionState()
        if not self.actions_path.exists():
            return state
        try:
            lines = self.actions_path.read_bytes().splitlines()
        except FileNotFoundError:
            return state
        for line_number, raw_line in enumerate(lines, start=1):
            state.last_line_number = line_number
            if not raw_line.strip():
                continue
            action_id: str | None = None
            try:
                line = raw_line.decode("utf-8")
                decoded = json.loads(line)
                if isinstance(decoded, Mapping) and isinstance(decoded.get("action_id"), str):
                    action_id = decoded["action_id"]
                action = ContinuationAction.from_dict(decoded)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                state.invalid_record_count += 1
                state.issues.append(
                    ContinuationProjectionIssue(
                        line_number=line_number,
                        action_id=action_id,
                        code="invalid_record",
                        message=str(exc),
                    )
                )
                continue
            try:
                _apply_action(state, action, sequence=line_number)
            except ContinuationTaskError as exc:
                state.issues.append(
                    ContinuationProjectionIssue(
                        line_number=line_number,
                        action_id=action.action_id,
                        code="rejected_transition",
                        message=str(exc),
                    )
                )
        return state

    def project(self) -> ContinuationProjection:
        # Fresh/default singleton sessions require no persisted task and no
        # continuation store side effect merely to render a homepage.
        if not self.actions_path.exists():
            return ContinuationProjection()
        with self._locked(exclusive=False):
            return _freeze(self._load_state_unlocked())

    @contextmanager
    def locked_projection(self) -> Iterator[ContinuationProjection]:
        """Hold stable root-session membership across a dependent mutation."""

        with self._locked(exclusive=False):
            yield _freeze(self._load_state_unlocked())

    @staticmethod
    def _new_task_id(state: _ProjectionState) -> str:
        while True:
            task_id = f"ctask_{uuid.uuid4().hex}"
            if task_id not in state.tasks and task_id not in state.aliases and task_id not in state.inactive_task_ids:
                return task_id

    def _commit_unlocked(
        self,
        state: _ProjectionState,
        action: ContinuationAction,
        *,
        sequence: int,
    ) -> TaskMutationResult:
        _apply_action(state, action, sequence=sequence)
        self._append_unlocked(action)
        projection = _freeze(state)
        resolved_task_id = projection.resolve_task_id(action.task_id)
        return TaskMutationResult(
            changed=True,
            task_id=resolved_task_id,
            action_id=action.action_id,
            action_type=action.action_type,
            projection=projection,
        )

    def create_task(
        self,
        sessions: Sequence[ClientSessionRef],
        *,
        confirmed_by: str,
        title: str | None = None,
    ) -> TaskMutationResult:
        ordered = tuple(sessions)
        if len(ordered) < 2:
            raise ContinuationTaskError("a persisted continuation task requires at least two sessions")
        with self._locked(exclusive=True):
            state = self._load_state_unlocked()
            task_id = self._new_task_id(state)
            action = ContinuationAction.create(
                action_type="create",
                task_id=task_id,
                created_by=confirmed_by,
                sessions=ordered,
                title=title,
            )
            return self._commit_unlocked(
                state,
                action,
                sequence=state.last_line_number + 1,
            )

    def link_session(
        self,
        task_id: str,
        session: ClientSessionRef,
        *,
        confirmed_by: str,
        position: str = "append",
    ) -> TaskMutationResult:
        _validate_task_id(task_id)
        with self._locked(exclusive=True):
            state = self._load_state_unlocked()
            resolved = _resolve_alias(state, task_id)
            task = _active_task(state, resolved)
            existing = state.session_to_task.get(session.key)
            if existing == task.task_id:
                projection = _freeze(state)
                return TaskMutationResult(False, task.task_id, None, None, projection)
            action = ContinuationAction.create(
                action_type="link",
                task_id=task.task_id,
                created_by=confirmed_by,
                session=session,
                position=position,
            )
            return self._commit_unlocked(
                state,
                action,
                sequence=state.last_line_number + 1,
            )

    def merge_tasks(
        self,
        target_task_id: str,
        source_task_id: str,
        *,
        confirmed_by: str,
        position: str = "append",
    ) -> TaskMutationResult:
        _validate_task_id(target_task_id, "target_task_id")
        _validate_task_id(source_task_id, "source_task_id")
        with self._locked(exclusive=True):
            state = self._load_state_unlocked()
            target = _active_task(state, target_task_id)
            source = _active_task(state, source_task_id)
            if target.task_id == source.task_id:
                projection = _freeze(state)
                return TaskMutationResult(False, target.task_id, None, None, projection)
            action = ContinuationAction.create(
                action_type="link",
                task_id=target.task_id,
                source_task_id=source.task_id,
                created_by=confirmed_by,
                position=position,
            )
            return self._commit_unlocked(
                state,
                action,
                sequence=state.last_line_number + 1,
            )

    def link_sessions(
        self,
        previous_session: ClientSessionRef,
        continuation_session: ClientSessionRef,
        *,
        confirmed_by: str,
        title: str | None = None,
    ) -> TaskMutationResult:
        """Connect two roots, creating/extending/merging tasks as needed.

        Argument order is meaningful only for membership display order.  It does
        not assert turn-level usage ownership or rewrite either client session.
        """

        if previous_session == continuation_session:
            raise ContinuationTaskError("cannot link a session to itself")
        with self._locked(exclusive=True):
            state = self._load_state_unlocked()
            previous_task_id = state.session_to_task.get(previous_session.key)
            continuation_task_id = state.session_to_task.get(continuation_session.key)

            if previous_task_id and continuation_task_id:
                previous_task_id = _resolve_alias(state, previous_task_id)
                continuation_task_id = _resolve_alias(state, continuation_task_id)
                if previous_task_id == continuation_task_id:
                    projection = _freeze(state)
                    return TaskMutationResult(False, previous_task_id, None, None, projection)
                action = ContinuationAction.create(
                    action_type="link",
                    task_id=previous_task_id,
                    source_task_id=continuation_task_id,
                    created_by=confirmed_by,
                    position="append",
                )
            elif previous_task_id:
                action = ContinuationAction.create(
                    action_type="link",
                    task_id=_resolve_alias(state, previous_task_id),
                    session=continuation_session,
                    created_by=confirmed_by,
                    position="append",
                )
            elif continuation_task_id:
                action = ContinuationAction.create(
                    action_type="link",
                    task_id=_resolve_alias(state, continuation_task_id),
                    session=previous_session,
                    created_by=confirmed_by,
                    position="prepend",
                )
            else:
                action = ContinuationAction.create(
                    action_type="create",
                    task_id=self._new_task_id(state),
                    sessions=(previous_session, continuation_session),
                    created_by=confirmed_by,
                    title=title,
                )
            return self._commit_unlocked(
                state,
                action,
                sequence=state.last_line_number + 1,
            )

    def unlink_session(
        self,
        task_id: str,
        session: ClientSessionRef,
        *,
        confirmed_by: str,
    ) -> TaskMutationResult:
        _validate_task_id(task_id)
        with self._locked(exclusive=True):
            state = self._load_state_unlocked()
            task = _active_task(state, task_id)
            action = ContinuationAction.create(
                action_type="unlink",
                task_id=task.task_id,
                session=session,
                created_by=confirmed_by,
            )
            return self._commit_unlocked(
                state,
                action,
                sequence=state.last_line_number + 1,
            )

    def rename_task(
        self,
        task_id: str,
        title: str | None,
        *,
        confirmed_by: str,
    ) -> TaskMutationResult:
        _validate_task_id(task_id)
        with self._locked(exclusive=True):
            state = self._load_state_unlocked()
            task = _active_task(state, task_id)
            cleaned_title = _clean_text(title, "title", maximum=240, optional=True)
            if task.title == cleaned_title:
                projection = _freeze(state)
                return TaskMutationResult(False, task.task_id, None, None, projection)
            action = ContinuationAction.create(
                action_type="rename",
                task_id=task.task_id,
                title=cleaned_title,
                created_by=confirmed_by,
            )
            return self._commit_unlocked(
                state,
                action,
                sequence=state.last_line_number + 1,
            )
