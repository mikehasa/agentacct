from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agentacct.task_continuations import (
    CONTINUATION_ACTIONS_FILENAME,
    CONTINUATION_STORE_DIRNAME,
    USER_CONFIRMATION,
    ClientSessionRef,
    ContinuationAction,
    ContinuationTaskError,
    ContinuationTaskStore,
)


def _session(name: str, *, client: str = "codex") -> ClientSessionRef:
    return ClientSessionRef(client=client, client_session_id=name)


def _stored_actions(store: ContinuationTaskStore) -> list[dict]:
    return [
        json.loads(line)
        for line in store.actions_path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("{") and line.strip().endswith("}")
    ]


def test_fresh_store_derives_singletons_without_persisting_a_task(tmp_path: Path) -> None:
    store = ContinuationTaskStore(tmp_path)

    assert store.project().tasks == ()
    assert not (tmp_path / CONTINUATION_STORE_DIRNAME).exists()

    with pytest.raises(ContinuationTaskError, match="at least two sessions"):
        store.create_task([_session("only-one")], confirmed_by="local-user")

    assert not store.actions_path.exists()
    assert not (tmp_path / "events.jsonl").exists()


def test_explicit_link_atomically_creates_one_user_confirmed_task(tmp_path: Path) -> None:
    store = ContinuationTaskStore(tmp_path)
    first = _session("session-a")
    second = _session("session-b")

    result = store.link_sessions(first, second, confirmed_by="local-user", title="Fix auth")

    assert result.changed is True
    assert result.action_type == "create"
    task = result.projection.get_task(result.task_id or "")
    assert task is not None
    assert task.title == "Fix auth"
    assert task.sessions == (first, second)
    assert [membership.role for membership in task.memberships] == ["primary", "continuation"]
    assert {membership.confirmation for membership in task.memberships} == {USER_CONFIRMATION}
    assert store.actions_path == tmp_path / CONTINUATION_STORE_DIRNAME / CONTINUATION_ACTIONS_FILENAME
    actions = _stored_actions(store)
    assert len(actions) == 1
    assert actions[0]["action_type"] == "create"
    assert actions[0]["confirmation"] == USER_CONFIRMATION
    assert ContinuationAction.from_dict(actions[0]).record_hash == actions[0]["record_hash"]
    assert not (tmp_path / "events.jsonl").exists()

    before_retry = store.actions_path.read_bytes()
    retry = store.link_sessions(first, second, confirmed_by="local-user")
    assert retry.changed is False
    assert retry.task_id == result.task_id
    assert store.actions_path.read_bytes() == before_retry


def test_link_rename_and_clear_title_are_append_only(tmp_path: Path) -> None:
    store = ContinuationTaskStore(tmp_path)
    first, second, third = (_session("session-a"), _session("session-b"), _session("session-c"))
    created = store.link_sessions(first, second, confirmed_by="local-user", title="Initial title")
    prefix = store.actions_path.read_bytes()

    linked = store.link_sessions(second, third, confirmed_by="local-user")
    renamed = store.rename_task(linked.task_id or "", "Continuation task", confirmed_by="local-user")
    cleared = store.rename_task(renamed.task_id or "", None, confirmed_by="local-user")

    assert linked.action_type == "link"
    assert renamed.action_type == "rename"
    assert cleared.action_type == "rename"
    assert store.actions_path.read_bytes().startswith(prefix)
    assert [row["action_type"] for row in _stored_actions(store)] == ["create", "link", "rename", "rename"]
    task = store.project().get_task(created.task_id or "")
    assert task is not None
    assert task.title is None
    assert task.sessions == (first, second, third)


def test_session_membership_is_exclusive_and_linking_tasks_merges_them(tmp_path: Path) -> None:
    store = ContinuationTaskStore(tmp_path)
    first, second, third, fourth, fifth = (
        _session("session-a"),
        _session("session-b"),
        _session("session-c"),
        _session("session-d"),
        _session("session-e"),
    )
    earlier = store.link_sessions(first, second, confirmed_by="local-user", title="Earlier task")
    later = store.link_sessions(third, fourth, confirmed_by="local-user", title="Later task")
    before_conflict = store.actions_path.read_bytes()

    with pytest.raises(ContinuationTaskError, match="already belongs"):
        store.create_task([first, fifth], confirmed_by="local-user")
    assert store.actions_path.read_bytes() == before_conflict

    merged = store.link_sessions(second, third, confirmed_by="local-user")

    assert merged.changed is True
    assert merged.action_type == "link"
    assert merged.task_id == earlier.task_id
    assert len(merged.projection.tasks) == 1
    task = merged.projection.tasks[0]
    assert task.sessions == (first, second, third, fourth)
    assert task.title == "Earlier task"
    assert task.merged_from_task_ids == (later.task_id,)
    assert merged.projection.resolve_task_id(later.task_id or "") == earlier.task_id
    assert later.task_id in merged.projection.inactive_task_ids
    assert all(merged.projection.task_for(session) == task for session in task.sessions)
    merge_action = _stored_actions(store)[-1]
    assert merge_action["action_type"] == "link"
    assert merge_action["source_task_id"] == later.task_id

    renamed_via_old_id = store.rename_task(later.task_id or "", "Merged task", confirmed_by="local-user")
    assert renamed_via_old_id.task_id == earlier.task_id
    assert renamed_via_old_id.projection.tasks[0].title == "Merged task"


def test_unlink_dissolves_the_last_singleton_back_to_default_state(tmp_path: Path) -> None:
    store = ContinuationTaskStore(tmp_path)
    first, second, third, fourth = (
        _session("session-a"),
        _session("session-b"),
        _session("session-c"),
        _session("session-d"),
    )
    created = store.link_sessions(first, second, confirmed_by="local-user")
    store.link_sessions(second, third, confirmed_by="local-user")

    reduced = store.unlink_session(created.task_id or "", second, confirmed_by="local-user")
    reduced_task = reduced.projection.get_task(created.task_id or "")
    assert reduced_task is not None
    assert reduced_task.sessions == (first, third)

    dissolved = store.unlink_session(created.task_id or "", third, confirmed_by="local-user")
    assert dissolved.projection.tasks == ()
    assert dissolved.projection.task_for(first) is None
    assert created.task_id in dissolved.projection.inactive_task_ids

    replacement = store.link_sessions(first, fourth, confirmed_by="local-user")
    assert replacement.task_id != created.task_id
    assert replacement.projection.tasks[0].sessions == (first, fourth)
    assert [row["action_type"] for row in _stored_actions(store)] == [
        "create",
        "link",
        "unlink",
        "unlink",
        "create",
    ]


def test_projection_is_replay_deterministic_and_torn_lines_never_gain_state(tmp_path: Path) -> None:
    store = ContinuationTaskStore(tmp_path)
    first, second, third = (_session("session-a"), _session("session-b"), _session("session-c"))
    store.link_sessions(first, second, confirmed_by="local-user")
    expected = store.project().to_dict()

    assert ContinuationTaskStore(tmp_path).project().to_dict() == expected

    store.actions_path.write_bytes(store.actions_path.read_bytes() + b'{"torn":')
    damaged = store.project()
    assert damaged.invalid_record_count == 1
    assert damaged.tasks[0].sessions == (first, second)

    store.link_sessions(second, third, confirmed_by="local-user")
    recovered = ContinuationTaskStore(tmp_path).project()
    assert recovered.invalid_record_count == 1
    assert recovered.tasks[0].sessions == (first, second, third)
    assert b'{"torn":\n{' in store.actions_path.read_bytes()


def test_missing_integrity_hash_cannot_create_membership(tmp_path: Path) -> None:
    store = ContinuationTaskStore(tmp_path)
    first, second = _session("session-a"), _session("session-b")
    valid = ContinuationAction.create(
        action_type="create",
        task_id="ctask_" + ("1" * 32),
        sessions=(first, second),
        created_by="local-user",
    ).to_dict()
    valid["record_hash"] = None
    store.continuation_root.mkdir(parents=True)
    store.actions_path.write_text(json.dumps(valid) + "\n", encoding="utf-8")

    projection = store.project()

    assert projection.tasks == ()
    assert projection.invalid_record_count == 1
    assert projection.issues[0].code == "invalid_record"


def test_concurrent_duplicate_link_creates_exactly_one_task_action(tmp_path: Path) -> None:
    first, second = _session("session-a"), _session("session-b")

    def link_once() -> bool:
        return ContinuationTaskStore(tmp_path).link_sessions(
            first,
            second,
            confirmed_by="local-user",
        ).changed

    with ThreadPoolExecutor(max_workers=2) as pool:
        changed = list(pool.map(lambda _index: link_once(), range(2)))

    store = ContinuationTaskStore(tmp_path)
    assert sorted(changed) == [False, True]
    assert len(_stored_actions(store)) == 1
    assert len(store.project().tasks) == 1
