"""Narrow repository contracts for the canonical SQLite spike.

The protocols keep client adapters, legacy readers, projections, and the
SQLite implementation from becoming one storage-shaped service object.
"""

from __future__ import annotations

from typing import Mapping, Protocol, Sequence

from .types import (
    CheckpointBoundary,
    CostCalculationInput,
    FactSessionLinkInput,
    RecoveredWorkClaimInput,
    Session,
    SessionEdgeInput,
    SessionInput,
    SourceInstance,
    SourceInstanceInput,
    TaskAnchor,
    TaskCurrent,
    TaskCursor,
    UsageDayQuery,
    UsageMeasurementInput,
    WorkClaimInput,
    WriteResult,
)


class SourceRepository(Protocol):
    def get_source(
        self,
        client: str,
        representation: str,
        namespace_digest: bytes,
    ) -> SourceInstance | None: ...

    def get_or_create_source(self, value: SourceInstanceInput) -> SourceInstance: ...


class SessionRepository(Protocol):
    def reconcile_session(self, value: SessionInput) -> WriteResult: ...

    def absorb_session_observation(self, value: SessionInput) -> WriteResult: ...

    def add_session_edge(self, value: SessionEdgeInput) -> WriteResult: ...

    def get_sessions(self, keys: Sequence[tuple[int, str]]) -> Mapping[tuple[int, str], Session]: ...

    def get_session(self, source_instance_id: int, client_session_id: str) -> Session | None: ...


class TaskAnchorRepository(Protocol):
    def get_or_create_task_anchor(self, primary_session_id: int) -> TaskAnchor: ...

    def get_task_anchor(self, public_task_id: str) -> TaskAnchor | None: ...


class FactRepository(Protocol):
    def append_work_claim(self, value: WorkClaimInput) -> WriteResult: ...

    def link_fact_to_session(self, value: FactSessionLinkInput) -> WriteResult: ...

    def append_recovered_work_claim(
        self,
        value: WorkClaimInput,
        link: FactSessionLinkInput,
    ) -> tuple[WriteResult, WriteResult]: ...

    def append_recovered_work_claims(
        self,
        values: Sequence[RecoveredWorkClaimInput],
    ) -> Sequence[tuple[WriteResult, WriteResult]]: ...


class UsageRepository(Protocol):
    def reconcile_usage(
        self,
        value: UsageMeasurementInput,
        *,
        checkpoint: tuple[CheckpointBoundary, str] | None = None,
    ) -> WriteResult: ...

    def record_cost(self, value: CostCalculationInput) -> WriteResult: ...


class CandidateQueries(Protocol):
    def get_task(self, public_task_id: str) -> TaskCurrent | None: ...

    def list_tasks(
        self,
        *,
        limit: int,
        before: TaskCursor | None = None,
    ) -> Sequence[TaskCurrent]: ...

    def usage_days(self, query: UsageDayQuery) -> Sequence[Mapping[str, object]]: ...

    def list_sessions(
        self,
        *,
        limit: int,
        before: tuple[int, int] | None = None,
    ) -> Sequence[Session]: ...

    def get_sessions_by_ids(
        self, session_ids: Sequence[int]
    ) -> Mapping[int, Session]: ...

    def get_source_instances_by_ids(
        self, source_instance_ids: Sequence[int]
    ) -> Mapping[int, SourceInstance]: ...

    def valid_lineage_edges(
        self, child_session_ids: Sequence[int]
    ) -> Mapping[int, tuple[int, str]]: ...

    def health_summary(self) -> Mapping[str, object]: ...


__all__ = [
    "CandidateQueries",
    "FactRepository",
    "SessionRepository",
    "SourceRepository",
    "TaskAnchorRepository",
    "UsageRepository",
]
