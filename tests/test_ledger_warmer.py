"""The background projection warmer: when it rebuilds, and that it can never
make a reader see anything but the store as it is at request time.

The timing rules are tested on ``LedgerWarmer`` alone with a fake clock and no
threads. The wiring is tested through the real API: a rebuild the warmer ran
is reused by the next request only while the store is unchanged.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

import agentacct.api as api_module
from agentacct.api import create_local_api_app
from agentacct.client_usage import ClientUsageEvent
from agentacct.ledger_warmer import LedgerWarmer
from agentacct.service import SentinelService

TOKEN = "test-v1-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class _Store:
    """A fake store: a change token the test moves, a clock the test moves."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.token = 1
        self.rebuilds: list[int] = []
        self.rebuild_seconds = 0.0
        self.write_during_rebuild = False
        self.rebuild_error: Exception | None = None
        self.token_error: Exception | None = None

    def clock(self) -> float:
        return self.now

    def read_change_token(self) -> int:
        if self.token_error is not None:
            raise self.token_error
        return self.token

    def rebuild(self) -> None:
        self.rebuilds.append(self.token)
        self.now += self.rebuild_seconds
        if self.write_during_rebuild:
            self.token += 1
            self.write_during_rebuild = False
        if self.rebuild_error is not None:
            raise self.rebuild_error

    def warmer(self, **overrides) -> LedgerWarmer:
        settings = {
            "settle_seconds": 0.4,
            "max_wait_seconds": 2.0,
            "reader_window_seconds": 180.0,
            "rest_factor": 2.0,
        }
        settings.update(overrides)
        return LedgerWarmer(
            read_change_token=self.read_change_token,
            rebuild=self.rebuild,
            clock=self.clock,
            **settings,
        )


def _built(store: _Store, warmer: LedgerWarmer) -> None:
    """Bring the warmer to 'built for the current store', with a reader seen."""

    warmer.rebuild_now()
    warmer.note_reader()
    store.rebuilds.clear()


def test_without_a_reader_a_store_change_is_never_rebuilt():
    store = _Store()
    warmer = store.warmer()
    warmer.rebuild_now()
    store.rebuilds.clear()

    store.token += 1
    for _ in range(40):
        store.now += 0.25
        assert warmer.check_once() is False
    assert store.rebuilds == []
    assert warmer.is_watching() is False


def test_a_change_is_rebuilt_once_after_the_store_goes_quiet():
    store = _Store()
    warmer = store.warmer()
    _built(store, warmer)

    assert warmer.check_once() is False  # unchanged store: nothing to do

    store.token += 1
    assert warmer.check_once() is False  # just changed: wait for it to settle
    store.now += 0.25
    assert warmer.check_once() is False  # 0.25 s quiet < 0.4 s
    store.now += 0.25
    assert warmer.check_once() is True  # 0.5 s quiet: rebuild
    assert store.rebuilds == [2]

    for _ in range(20):
        store.now += 0.25
        assert warmer.check_once() is False  # built and unchanged
    assert store.rebuilds == [2]


def test_a_burst_of_writes_resets_the_quiet_period_but_cannot_postpone_forever():
    store = _Store()
    warmer = store.warmer()
    _built(store, warmer)

    # A write every 0.25 s never lets the store go quiet for 0.4 s ...
    ran_at = None
    for step in range(12):
        store.token += 1
        if warmer.check_once():
            ran_at = step * 0.25
            break
        store.now += 0.25
    # ... so the rebuild runs when the first unbuilt change is 2 s old.
    assert ran_at == 2.0
    assert len(store.rebuilds) == 1


def test_the_warmer_goes_idle_when_readers_stop_and_resumes_when_one_returns():
    store = _Store()
    warmer = store.warmer()
    _built(store, warmer)

    store.now += 181.0  # past the 180 s reader window
    store.token += 1
    for _ in range(8):
        store.now += 0.25
        assert warmer.check_once() is False
    assert store.rebuilds == []

    warmer.note_reader()
    assert warmer.check_once() is False  # the change is noticed now ...
    store.now += 0.5
    assert warmer.check_once() is True  # ... and rebuilt once it has settled
    assert store.rebuilds == [2]


def test_after_a_rebuild_the_warmer_rests_in_proportion_to_its_cost():
    store = _Store()
    warmer = store.warmer()
    _built(store, warmer)
    store.rebuild_seconds = 1.0

    store.token += 1
    warmer.check_once()
    store.now += 0.5
    assert warmer.check_once() is True  # took 1.0 s, so rest 2.0 s
    rested_from = store.now

    store.token += 1
    while store.now - rested_from < 1.9:
        assert warmer.check_once() is False  # changed and settled, but resting
        store.now += 0.25
        warmer.note_reader()
    store.now = rested_from + 2.0
    assert warmer.check_once() is True
    assert store.rebuilds == [2, 3]


def test_a_write_that_lands_during_a_rebuild_is_rebuilt_again():
    store = _Store()
    warmer = store.warmer()
    _built(store, warmer)

    store.token += 1
    warmer.check_once()
    store.now += 0.5
    store.write_during_rebuild = True
    assert warmer.check_once() is True  # built token 2; the store is now at 3
    assert store.token == 3

    assert warmer.check_once() is False  # token 3 is an unbuilt change
    store.now += 0.5
    assert warmer.check_once() is True
    assert store.rebuilds == [2, 3]


def test_a_failing_rebuild_is_swallowed_and_not_retried_until_the_next_change():
    store = _Store()
    warmer = store.warmer()
    _built(store, warmer)
    store.rebuild_error = RuntimeError("store unreadable")

    store.token += 1
    warmer.check_once()
    store.now += 0.5
    assert warmer.check_once() is True  # attempted; the error does not escape
    for _ in range(20):
        store.now += 0.25
        assert warmer.check_once() is False  # no spinning on a persistent failure
    assert store.rebuilds == [2]

    store.rebuild_error = None
    store.token += 1
    warmer.check_once()
    store.now += 0.5
    assert warmer.check_once() is True
    assert store.rebuilds == [2, 3]


def test_an_unreadable_change_token_is_not_a_rebuild_and_not_an_error():
    store = _Store()
    warmer = store.warmer()
    _built(store, warmer)
    store.token_error = OSError("no such store")

    store.now += 1.0
    assert warmer.check_once() is False
    warmer.rebuild_now()
    assert store.rebuilds == []


# --- wired into the API -----------------------------------------------------


def _record_usage(service: SentinelService, *, session_id: str, updated_at: float) -> None:
    event = ClientUsageEvent(
        client="claude-code",
        client_session_id=session_id,
        client_session_kind=None,
        parent_client_session_id=None,
        source_path=Path(f"/tmp/claude-code/{session_id}.jsonl"),
        title=None,
        cwd="/tmp/project",
        model="claude-opus-4-8",
        input_tokens=100,
        output_tokens=0,
        cached_input_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_tokens_reported=True,
        cache_read_tokens_reported=True,
        reasoning_output_tokens=0,
        provider_name="claude-code",
        started_at=updated_at,
        updated_at=updated_at,
        turn_count=1,
        usage_row_lane="model:claude-opus-4-8",
        source_namespace_fingerprint="sha256:claude-code",
        input_tokens_reported=True,
        output_tokens_reported=True,
        reasoning_output_tokens_reported=True,
        total_tokens=100,
        total_tokens_reported=True,
    ).to_sentinel_event()
    service.record_event(event, trusted_usage_import=True)


def _counting_builds(monkeypatch) -> dict[str, int]:
    calls = {"count": 0}
    real_build = api_module.build_work_ledger

    def _counting_build(*args, **kwargs):
        calls["count"] += 1
        return real_build(*args, **kwargs)

    monkeypatch.setattr(api_module, "build_work_ledger", _counting_build)
    return calls


def _session_ids(client: TestClient) -> set[str]:
    response = client.get("/v1/sessions?roots_only=false&limit=500", headers=AUTH)
    assert response.status_code == 200
    return {row["client_session_id"] for row in response.json()["sessions"]}


def test_a_warmed_store_answers_the_next_request_without_rebuilding(tmp_path, monkeypatch):
    service = SentinelService(tmp_path)
    _record_usage(service, session_id="root-a", updated_at=time.time() - 60)
    calls = _counting_builds(monkeypatch)
    app = create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN)
    client = TestClient(app)
    warmer = app.state.ledger_warmer

    assert _session_ids(client) == {"root-a"}  # a reader; this request builds
    assert calls["count"] == 1
    assert warmer.is_watching()

    _record_usage(service, session_id="root-b", updated_at=time.time() - 30)
    assert warmer.check_once() is False  # noticed; waiting for the store to settle
    time.sleep(0.45)
    assert warmer.check_once() is True  # rebuilt in the background
    assert calls["count"] == 2

    # The reader pays no rebuild, and sees the store as it is now.
    assert _session_ids(client) == {"root-a", "root-b"}
    assert calls["count"] == 2


def test_a_write_after_the_warm_build_is_never_hidden_from_the_next_reader(tmp_path, monkeypatch):
    service = SentinelService(tmp_path)
    _record_usage(service, session_id="root-a", updated_at=time.time() - 60)
    calls = _counting_builds(monkeypatch)
    app = create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN)
    client = TestClient(app)

    app.state.ledger_warmer.rebuild_now()
    assert calls["count"] == 1

    # The store moves on AFTER the warm build and before anyone reads.
    _record_usage(service, session_id="root-b", updated_at=time.time() - 30)

    # The reader's key is derived from the store as it is now, so the warm
    # build is not reused: this request rebuilds and shows the new session.
    assert _session_ids(client) == {"root-a", "root-b"}
    assert calls["count"] == 2


def test_a_secondary_store_change_alone_is_a_change_the_warmer_rebuilds(tmp_path, monkeypatch):
    service = SentinelService(tmp_path)
    _record_usage(service, session_id="root-a", updated_at=time.time() - 60)
    calls = _counting_builds(monkeypatch)
    app = create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN)
    client = TestClient(app)
    warmer = app.state.ledger_warmer

    assert _session_ids(client) == {"root-a"}
    assert calls["count"] == 1

    # A cost event moves no primary ledger event, only a secondary store the
    # ledger cache key also covers; the warmer's token must cover it too.
    with (tmp_path / "cost_events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "event_id": "cost_test",
                    "created_at": time.time(),
                    "run_id": None,
                    "decision": "record",
                    "reason": "",
                    "estimated_cost_usd": 0.0,
                    "estimated_input_tokens": 0,
                    "estimated_output_tokens": 0,
                },
                sort_keys=True,
            )
            + "\n"
        )
    assert warmer.check_once() is False
    time.sleep(0.45)
    assert warmer.check_once() is True
    assert calls["count"] == 2
    assert _session_ids(client) == {"root-a"}
    assert calls["count"] == 2


def test_the_warmers_own_rebuild_does_not_count_as_a_reader(tmp_path):
    service = SentinelService(tmp_path)
    _record_usage(service, session_id="root-a", updated_at=time.time() - 60)
    app = create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN)
    warmer = app.state.ledger_warmer

    warmer.rebuild_now()
    assert warmer.is_watching() is False  # it must not keep itself awake

    client = TestClient(app)
    assert client.get("/v1/glance", headers=AUTH).status_code == 200
    assert warmer.is_watching() is False  # the glance does not read the ledger

    assert client.get("/v1/tasks", headers=AUTH).status_code == 200
    assert warmer.is_watching() is True
