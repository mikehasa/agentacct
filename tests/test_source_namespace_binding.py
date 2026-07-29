from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

from agentacct.client_usage import (
    ClientUsageEvent,
    bind_discovered_usage_source_namespaces,
)
from agentacct.service import SentinelService
from agentacct.task_projection import build_task_projection
from agentacct.work_ledger import build_work_ledger


NAMESPACE_A = "sha256:" + "a" * 64
NAMESPACE_B = "sha256:" + "b" * 64


def _usage_candidate(
    tmp_path: Path,
    *,
    client: str = "codex",
    session_id: str,
    namespace: str | None = None,
    parent_session_id: str | None = None,
    model: str = "gpt-test",
    lane: str | None = None,
) -> ClientUsageEvent:
    return ClientUsageEvent(
        client=client,  # type: ignore[arg-type]
        client_session_id=session_id,
        source_path=tmp_path / f"{session_id}.jsonl",
        title=None,
        cwd="/work/project",
        model=model,
        input_tokens=10,
        output_tokens=2,
        client_session_kind="subagent" if parent_session_id else "root",
        parent_client_session_id=parent_session_id,
        usage_row_lane=lane,
        source_namespace_fingerprint=namespace,
    )


def _record_usage(service: SentinelService, candidate: ClientUsageEvent) -> dict:
    return service.record_event(
        candidate.to_sentinel_event(),
        trusted_usage_import=True,
    )


def _task_projection(service: SentinelService) -> dict:
    ledger = build_work_ledger(service.list_all_events(), store_scope="custom")
    return build_task_projection(
        ledger["session_rollup"]["sessions"],
        ledger["work_items"],
    )


def _event_identity_by_session(service: SentinelService) -> dict[str, tuple[str, float]]:
    return {
        str(event["metadata"]["client_session_id"]): (
            str(event["event_id"]),
            float(event["created_at"]),
        )
        for event in service.list_all_events()
    }


def test_connected_root_child_and_sibling_bind_as_one_atomic_component(
    tmp_path: Path,
) -> None:
    service = SentinelService(tmp_path / "state")
    root = _usage_candidate(tmp_path, session_id="root")
    child = _usage_candidate(
        tmp_path,
        session_id="child",
        parent_session_id="root",
    )
    sibling = _usage_candidate(
        tmp_path,
        session_id="sibling",
        parent_session_id="root",
    )
    for candidate in (root, child, sibling):
        _record_usage(service, candidate)
    identities_before = _event_identity_by_session(service)

    accepted, adopted, conflicts, adoption_counts = (
        bind_discovered_usage_source_namespaces(
            service,
            [replace(child, source_namespace_fingerprint=NAMESPACE_A)],
        )
    )

    assert [candidate.client_session_id for candidate in accepted] == ["child"]
    assert [candidate.client_session_id for candidate in adopted] == ["child"]
    assert conflicts == []
    assert adoption_counts == {"codex": 3}
    rows = {
        event["metadata"]["client_session_id"]: event
        for event in service.list_all_events()
    }
    assert {row["metadata"].get("source_namespace_fingerprint") for row in rows.values()} == {
        NAMESPACE_A
    }
    assert rows["root"]["metadata"].get("parent_source_namespace_fingerprint") is None
    assert rows["child"]["metadata"]["parent_source_namespace_fingerprint"] == NAMESPACE_A
    assert rows["sibling"]["metadata"]["parent_source_namespace_fingerprint"] == NAMESPACE_A
    assert _event_identity_by_session(service) == identities_before
    projection = _task_projection(service)
    assert projection["summary"]["task_count"] == 1
    assert projection["summary"]["session_count"] == 3


def test_children_with_a_missing_shared_root_bind_through_the_virtual_parent(
    tmp_path: Path,
) -> None:
    service = SentinelService(tmp_path / "state")
    first = _usage_candidate(
        tmp_path,
        session_id="first-child",
        parent_session_id="missing-root",
    )
    second = _usage_candidate(
        tmp_path,
        session_id="second-child",
        parent_session_id="missing-root",
    )
    _record_usage(service, first)
    _record_usage(service, second)

    _accepted, _adopted, conflicts, adoption_counts = (
        bind_discovered_usage_source_namespaces(
            service,
            [replace(first, source_namespace_fingerprint=NAMESPACE_A)],
        )
    )

    assert conflicts == []
    assert adoption_counts == {"codex": 2}
    child_rows = service.list_all_events()
    assert {
        event["metadata"].get("source_namespace_fingerprint")
        for event in child_rows
    } == {NAMESPACE_A}
    assert {
        event["metadata"].get("parent_source_namespace_fingerprint")
        for event in child_rows
    } == {NAMESPACE_A}

    _record_usage(
        service,
        _usage_candidate(
            tmp_path,
            session_id="missing-root",
            namespace=NAMESPACE_A,
        ),
    )
    projection = _task_projection(service)
    assert projection["summary"]["task_count"] == 1
    assert projection["summary"]["session_count"] == 3


def test_foreign_scoped_member_rejects_the_entire_component_without_writing(
    tmp_path: Path,
) -> None:
    service = SentinelService(tmp_path / "state")
    root = _usage_candidate(tmp_path, session_id="root")
    child = _usage_candidate(
        tmp_path,
        session_id="child",
        parent_session_id="root",
    )
    foreign_sibling = _usage_candidate(
        tmp_path,
        session_id="foreign-sibling",
        namespace=NAMESPACE_B,
        parent_session_id="root",
    )
    for candidate in (root, child, foreign_sibling):
        _record_usage(service, candidate)
    before = service.events_path.read_bytes()

    accepted, adopted, conflicts, adoption_counts = (
        bind_discovered_usage_source_namespaces(
            service,
            [replace(child, source_namespace_fingerprint=NAMESPACE_A)],
        )
    )

    assert accepted == []
    assert adopted == []
    assert [candidate.client_session_id for candidate in conflicts] == ["child"]
    assert adoption_counts == {}
    assert service.events_path.read_bytes() == before


def test_conflicting_targets_that_normalize_to_one_claude_key_fail_closed(
    tmp_path: Path,
) -> None:
    service = SentinelService(tmp_path / "state")
    _record_usage(
        service,
        _usage_candidate(
            tmp_path,
            client="claude-code",
            session_id="session",
            model="opus",
            lane="model:opus",
        ),
    )
    before = service.events_path.read_bytes()

    outcome = service.bind_local_usage_source_namespaces(
        {
            ("claude-code", "session"): NAMESPACE_A,
            ("claude-code", "session:model:legacy"): NAMESPACE_B,
        }
    )

    assert outcome["bound_rows"] == 0
    assert outcome["bound_rows_by_client"] == {}
    assert outcome["bound_identities"] == set()
    assert outcome["conflict_bases"] == {("claude-code", "session")}
    assert service.events_path.read_bytes() == before


def test_concurrent_conflicting_binders_have_one_winner_and_one_conflict(
    tmp_path: Path,
) -> None:
    store = tmp_path / "state"
    service = SentinelService(store)
    recorded = _record_usage(
        service,
        _usage_candidate(tmp_path, session_id="race"),
    )
    barrier = Barrier(2)

    def bind(namespace: str) -> tuple[str, dict]:
        barrier.wait()
        outcome = SentinelService(store).bind_local_usage_source_namespaces(
            {("codex", "race"): namespace}
        )
        return namespace, outcome

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(bind, (NAMESPACE_A, NAMESPACE_B)))

    winners = [
        (namespace, outcome)
        for namespace, outcome in results
        if outcome["bound_rows"] == 1
    ]
    losers = [
        (namespace, outcome)
        for namespace, outcome in results
        if outcome["bound_rows"] == 0
    ]
    assert len(winners) == 1
    assert len(losers) == 1
    assert winners[0][1]["conflict_bases"] == set()
    assert losers[0][1]["conflict_bases"] == {("codex", "race")}
    stored = SentinelService(store).list_all_events()
    assert len(stored) == 1
    assert stored[0]["metadata"]["source_namespace_fingerprint"] == winners[0][0]
    assert stored[0]["event_id"] == recorded["event_id"]
    assert stored[0]["created_at"] == recorded["created_at"]


def test_adoption_count_uses_physical_rows_and_preserves_each_event_identity(
    tmp_path: Path,
) -> None:
    service = SentinelService(tmp_path / "state")
    opus = _usage_candidate(
        tmp_path,
        client="claude-code",
        session_id="multi-model",
        model="opus",
        lane="model:opus",
    )
    haiku = _usage_candidate(
        tmp_path,
        client="claude-code",
        session_id="multi-model",
        model="haiku",
        lane="model:haiku",
    )
    for candidate in (opus, haiku):
        _record_usage(service, candidate)
    before = {
        event["metadata"]["usage_row_lane"]: (
            event["event_id"],
            event["created_at"],
        )
        for event in service.list_all_events()
    }

    accepted, adopted, conflicts, adoption_counts = (
        bind_discovered_usage_source_namespaces(
            service,
            [replace(opus, source_namespace_fingerprint=NAMESPACE_A)],
        )
    )

    assert accepted == [replace(opus, source_namespace_fingerprint=NAMESPACE_A)]
    assert adopted == accepted
    assert conflicts == []
    assert adoption_counts == {"claude-code": 2}
    stored = service.list_all_events()
    assert len(stored) == 2
    assert {
        event["metadata"]["usage_row_lane"]: (
            event["event_id"],
            event["created_at"],
        )
        for event in stored
    } == before
    assert {
        event["metadata"].get("source_namespace_fingerprint")
        for event in stored
    } == {NAMESPACE_A}
