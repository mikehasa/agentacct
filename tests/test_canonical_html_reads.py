"""P1 first cut — HTML dashboard read migration to canonical.

The HTML analog of test_canonical_read_canary / test_canonical_reader_surfaces:
the four migrated surfaces (/sessions HTML, /tokens, task detail, and the
composite Overview ``/``) must, per the locked principle, (1) render
byte-compatible v1 with NO canonical label when the read flag is off, (2) render
an honest-subset canonical body labeled ``data-canonical-read="canonical"`` when
the flag is on and the store serves, and (3) render v1 under a VISIBLE
``data-canonical-read="v1_fallback"`` banner when the flag is on but the store
cannot honestly serve — never a silent v1 render. The Overview has no 1:1
canonical twin: it composes the task, usage, and session reads all-or-nothing,
short-circuiting to the labeled v1 page on the first read that cannot serve.

Store/flag conventions follow test_canonical_reader.py: stores are imported into
tmp_path and promoted to live; the flag is pinned off suite-wide by conftest and
turned on here with monkeypatch.setenv.
"""

from __future__ import annotations

from typing import Any

import pytest

import agentacct.api as api
from agentacct.canonical_read import CANONICAL_READ_ENV

from test_canonical_reader import _api_client, _imported_live_store
from test_canonical_reader_surfaces import _work_ledger_events

_HTML = {"accept": "text/html"}


def _served_store(tmp_path, name):
    return _imported_live_store(tmp_path, _work_ledger_events(), name=name)


# --- sessions ---------------------------------------------------------------


def test_sessions_html_flag_off_is_v1_unlabeled(tmp_path):
    client = _api_client(_served_store(tmp_path, "sess-off"))
    body = client.get("/sessions", headers=_HTML).text
    assert "data-canonical-read" not in body


def test_sessions_html_flag_on_served_is_canonical(tmp_path, monkeypatch):
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(_served_store(tmp_path, "sess-on"))
    resp = client.get("/sessions", headers=_HTML)
    assert resp.status_code == 200
    body = resp.text
    assert 'data-canonical-read="canonical"' in body
    assert 'data-canonical-surface="session_list"' in body
    assert "Served from the canonical store" in body
    # base-fact table: no projection gate, filter params echoed as not applied
    assert "not applied to this canonical page" in body
    assert "Not represented in the canonical model" in body
    assert 'data-canonical-read="v1_fallback"' not in body


def test_sessions_html_flag_on_unavailable_falls_back_labeled(tmp_path, monkeypatch):
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(tmp_path / "empty-store")  # no chronicle.sqlite3
    resp = client.get("/sessions", headers=_HTML)
    assert resp.status_code == 200
    body = resp.text
    assert 'data-canonical-read="v1_fallback"' in body
    assert 'data-canonical-surface="session_list"' in body
    assert 'data-canonical-reason="store_absent"' in body
    assert "Showing v1 fallback data" in body
    health = client.get("/health").json()
    assert health["canonical_read"]["surfaces"]["session_list"]["unavailable"] >= 1


def test_sessions_html_flag_on_crash_falls_back_labeled(tmp_path, monkeypatch):
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")

    def _boom(*_a, **_k):
        raise RuntimeError("canonical sessions render exploded")

    monkeypatch.setattr(api, "_render_canonical_sessions_page", _boom)
    client = _api_client(_served_store(tmp_path, "sess-crash"))
    resp = client.get("/sessions", headers=_HTML)
    assert resp.status_code == 200
    body = resp.text
    assert 'data-canonical-read="v1_fallback"' in body
    assert 'data-canonical-reason="error"' in body
    health = client.get("/health").json()
    assert health["canonical_read"]["surfaces"]["session_list"]["errors"] >= 1


# --- tokens / usage ---------------------------------------------------------


def test_tokens_html_flag_off_is_v1_unlabeled(tmp_path):
    client = _api_client(_served_store(tmp_path, "tok-off"))
    body = client.get("/tokens").text
    assert "data-canonical-read" not in body


def test_tokens_html_flag_on_served_is_canonical(tmp_path, monkeypatch):
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(_served_store(tmp_path, "tok-on"))
    resp = client.get("/tokens?days=all")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-canonical-read="canonical"' in body
    assert 'data-canonical-surface="usage_days"' in body
    assert "Served from the canonical store" in body
    assert "Measurement days" in body  # totals metric grid rendered
    assert 'data-canonical-read="v1_fallback"' not in body


def test_tokens_html_flag_on_unavailable_falls_back_labeled(tmp_path, monkeypatch):
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(tmp_path / "empty-store")
    resp = client.get("/tokens")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-canonical-read="v1_fallback"' in body
    assert 'data-canonical-surface="usage_days"' in body
    assert 'data-canonical-reason="store_absent"' in body
    health = client.get("/health").json()
    assert health["canonical_read"]["surfaces"]["usage_days"]["unavailable"] >= 1


def test_tokens_html_flag_on_crash_falls_back_labeled(tmp_path, monkeypatch):
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")

    def _boom(*_a, **_k):
        raise RuntimeError("canonical tokens render exploded")

    monkeypatch.setattr(api, "_render_canonical_tokens_page", _boom)
    client = _api_client(_served_store(tmp_path, "tok-crash"))
    resp = client.get("/tokens?days=all")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-canonical-read="v1_fallback"' in body
    assert 'data-canonical-reason="error"' in body
    health = client.get("/health").json()
    assert health["canonical_read"]["surfaces"]["usage_days"]["errors"] >= 1


# --- task detail ------------------------------------------------------------


def _first_canonical_task_id(client) -> str:
    tasks = client.get("/tasks").json()["tasks"]
    assert tasks, "fixture must produce at least one canonical task"
    return tasks[0]["public_task_id"]


def test_task_detail_html_flag_off_is_v1(tmp_path):
    client = _api_client(_served_store(tmp_path, "td-off"))
    # flag off: v1 detail resolves v1 ids; a random canonical-style id 404s in v1
    resp = client.get("/tasks/does-not-exist")
    assert resp.status_code == 404


def test_task_detail_html_flag_on_served_is_canonical(tmp_path, monkeypatch):
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(_served_store(tmp_path, "td-on"))
    task_id = _first_canonical_task_id(client)
    resp = client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-canonical-read="canonical"' in body
    assert 'data-canonical-surface="task_detail"' in body
    assert "Served from the canonical store" in body
    assert "Not represented in the canonical model" in body
    assert 'data-canonical-read="v1_fallback"' not in body


def test_task_detail_html_flag_on_unknown_v1era_id_is_404(tmp_path, monkeypatch):
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(_served_store(tmp_path, "td-404"))
    resp = client.get("/tasks/v1-era-hmac-id-not-in-canonical")
    assert resp.status_code == 404
    assert "separate namespace" in resp.json()["detail"]


def test_task_detail_html_flag_on_crash_returns_503(tmp_path, monkeypatch):
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")

    def _boom(*_a, **_k):
        raise RuntimeError("canonical task detail render exploded")

    monkeypatch.setattr(api, "_render_canonical_task_detail_page", _boom)
    client = _api_client(_served_store(tmp_path, "td-crash"))
    task_id = _first_canonical_task_id(client)
    resp = client.get(f"/tasks/{task_id}")
    assert resp.status_code == 503
    assert "canonical task detail failed to build" in resp.json()["detail"]
    health = client.get("/health").json()
    assert health["canonical_read"]["surfaces"]["task_detail"]["errors"] >= 1


def test_task_detail_html_flag_on_unavailable_falls_back_labeled(tmp_path, monkeypatch):
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    # store absent AND the id is a real v1 id: unavailability falls back to v1,
    # labeled. Seed a v1 ledger so the v1 detail resolves an id to render under
    # the banner.
    store = tmp_path / "v1-only-store"
    from test_canonical_reader import _write_ledger

    _write_ledger(store, _work_ledger_events())
    client = _api_client(store)
    # find a v1 task id from the v1 (flag-on-but-store-absent) task list fallback
    tasks_payload = client.get("/tasks").json()
    # flag on + store absent => /tasks is the labeled v1 fallback
    assert tasks_payload["canonical_read"]["source"] == "v1_fallback"
    v1_tasks = tasks_payload.get("tasks") or []
    if not v1_tasks:
        pytest.skip("v1 ledger produced no resolvable task id for this assertion")
    # The opaque URL id is public_task_id; the internal task_id does not resolve
    # through /tasks/{id} (see test_task_detail_api).
    v1_id = v1_tasks[0]["public_task_id"]
    resp = client.get(f"/tasks/{v1_id}")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-canonical-read="v1_fallback"' in body
    assert 'data-canonical-surface="task_detail"' in body


# --- overview (composite: tasks + usage + sessions) -------------------------


def test_overview_html_flag_off_is_v1_unlabeled(tmp_path):
    client = _api_client(_served_store(tmp_path, "ov-off"))
    body = client.get("/", headers=_HTML).text
    assert "data-canonical-read" not in body


def test_overview_html_flag_on_served_is_canonical(tmp_path, monkeypatch):
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(_served_store(tmp_path, "ov-on"))
    resp = client.get("/", headers=_HTML)
    assert resp.status_code == 200
    body = resp.text
    assert 'data-canonical-read="canonical"' in body
    assert 'data-canonical-surface="overview"' in body
    assert "Served from the canonical store" in body
    # composite: all three reads named, three honest sections rendered
    assert "Composed from three canonical reads" in body
    assert "Recent work" in body
    assert "Usage pulse" in body
    assert "Session roster" in body
    assert "Not represented in the canonical model" in body
    # the source note surfaces each read model's freshness (served store is
    # current): the shared note carries store+task-projection freshness, the
    # composite extra_html carries the usage-projection freshness.
    assert "role live · projection current" in body
    assert "usage projection current" in body
    assert 'data-canonical-read="v1_fallback"' not in body


def test_overview_html_flag_on_unavailable_falls_back_labeled(tmp_path, monkeypatch):
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(tmp_path / "empty-store")  # no chronicle.sqlite3
    resp = client.get("/", headers=_HTML)
    assert resp.status_code == 200
    body = resp.text
    assert 'data-canonical-read="v1_fallback"' in body
    # the reads are attempted in order and short-circuit; task_list is first,
    # so a store-absent composite names task_list (never a generic "overview").
    assert 'data-canonical-surface="task_list"' in body
    assert 'data-canonical-reason="store_absent"' in body
    assert "Showing v1 fallback data" in body
    health = client.get("/health").json()
    assert health["canonical_read"]["surfaces"]["task_list"]["unavailable"] >= 1


def test_overview_html_flag_on_crash_falls_back_labeled(tmp_path, monkeypatch):
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")

    def _boom(*_a, **_k):
        raise RuntimeError("canonical overview render exploded")

    # all three reads serve, then composing them crashes -> labeled v1 fallback
    # accounted against a dedicated "overview" lane (the reads stay served).
    monkeypatch.setattr(api, "_render_canonical_overview_page", _boom)
    client = _api_client(_served_store(tmp_path, "ov-crash"))
    resp = client.get("/", headers=_HTML)
    assert resp.status_code == 200
    body = resp.text
    assert 'data-canonical-read="v1_fallback"' in body
    assert 'data-canonical-surface="overview"' in body
    assert 'data-canonical-reason="error"' in body
    health = client.get("/health").json()
    assert health["canonical_read"]["surfaces"]["overview"]["errors"] >= 1


def test_overview_html_flag_on_usage_unavailable_names_usage(tmp_path, monkeypatch):
    # partial availability: task_list serves, usage read fails -> the composite
    # short-circuits and the banner names usage_days (not a generic "overview").
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    from agentacct.canonical_read import CanonicalReadRuntime, CanonicalReadUnavailable

    def _boom(self, **_kw):
        raise CanonicalReadUnavailable("store_refused", "forced usage failure")

    monkeypatch.setattr(CanonicalReadRuntime, "_usage_day_read", _boom)
    client = _api_client(_served_store(tmp_path, "ov-usage-fail"))
    resp = client.get("/", headers=_HTML)
    assert resp.status_code == 200
    body = resp.text
    assert 'data-canonical-read="v1_fallback"' in body
    assert 'data-canonical-surface="usage_days"' in body
    assert 'data-canonical-reason="store_refused"' in body
    surfaces = client.get("/health").json()["canonical_read"]["surfaces"]
    assert surfaces["usage_days"]["unavailable"] >= 1
    # task_list was reached and served BEFORE the short-circuit fired
    assert surfaces["task_list"]["served"] >= 1


def test_overview_html_flag_on_session_unavailable_names_session(tmp_path, monkeypatch):
    # partial availability: task_list + usage serve, session read fails last.
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    from agentacct.canonical_read import CanonicalReadRuntime, CanonicalReadUnavailable

    def _boom(self, **_kw):
        raise CanonicalReadUnavailable("store_refused", "forced session failure")

    monkeypatch.setattr(CanonicalReadRuntime, "_session_list_read", _boom)
    client = _api_client(_served_store(tmp_path, "ov-session-fail"))
    resp = client.get("/", headers=_HTML)
    assert resp.status_code == 200
    body = resp.text
    assert 'data-canonical-read="v1_fallback"' in body
    assert 'data-canonical-surface="session_list"' in body
    surfaces = client.get("/health").json()["canonical_read"]["surfaces"]
    assert surfaces["session_list"]["unavailable"] >= 1
    # both prior reads served before the session read failed
    assert surfaces["task_list"]["served"] >= 1
    assert surfaces["usage_days"]["served"] >= 1


def test_overview_html_served_caps_and_truncation_labeled(tmp_path, monkeypatch):
    # bounded slice: cap each list to 1, and force the task read truncated so
    # the body caption marks "N+" and names the truncation, and the composite
    # source note ORs the truncation into its page-level label.
    import dataclasses
    from agentacct.canonical_read import CanonicalReadRuntime

    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    monkeypatch.setattr(api, "_CANONICAL_OVERVIEW_TASK_ROWS", 1)
    monkeypatch.setattr(api, "_CANONICAL_OVERVIEW_SESSION_ROWS", 1)
    real_task = CanonicalReadRuntime._task_list_read

    def _trunc_task(self, **kw):
        return dataclasses.replace(real_task(self, **kw), truncated=True)

    monkeypatch.setattr(CanonicalReadRuntime, "_task_list_read", _trunc_task)
    client = _api_client(_served_store(tmp_path, "ov-caps"))
    resp = client.get("/", headers=_HTML)
    assert resp.status_code == 200
    body = resp.text
    # cap applied to both lists; header shown-counts reflect the cap
    assert 'Recent work <span class="note">(1 shown)' in body
    assert 'Session roster <span class="note">(1 shown)' in body
    # truncated task read: count marked N+, truncation named in the body caption
    assert (
        "Showing 1 of 2+ tasks on this page (newest first) · page truncated, "
        "the store holds more." in body
    )
    # non-truncated sessions: plain "of 3", no truncation clause
    assert "Showing 1 of 3 sessions on this page (newest first)." in body
    # composite source note ORs the task truncation into the page-level label
    assert "page truncated — showing a bounded newest-first slice" in body


def test_overview_html_served_pulse_discloses_undated_exclusion(tmp_path, monkeypatch):
    # honesty parity with /tokens: undated usage excluded from the bounded
    # pulse window must be LABELED, not silently dropped.
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    from test_canonical_reader import _undated_usage_event

    events = _work_ledger_events() + [_undated_usage_event(event_id="evt_undated")]
    store = _imported_live_store(tmp_path, events, name="ov-undated")
    client = _api_client(store)
    body = client.get("/", headers=_HTML).text
    assert 'data-canonical-read="canonical"' in body
    assert "Exclusions:" in body
    assert "undated measurement day(s)" in body
    assert "excluded from this bounded window" in body


def test_overview_html_served_source_note_shows_usage_staleness(tmp_path, monkeypatch):
    # a stale (but current) usage projection must render its pending-write count
    # in the composite source note, not silently read as fresh.
    import dataclasses
    from agentacct.canonical_read import CanonicalReadRuntime

    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    real_usage = CanonicalReadRuntime._usage_day_read

    def _stale(self, **kw):
        read = real_usage(self, **kw)
        return dataclasses.replace(
            read, projection={**dict(read.projection), "stale": True, "pending_writes": 3}
        )

    monkeypatch.setattr(CanonicalReadRuntime, "_usage_day_read", _stale)
    client = _api_client(_served_store(tmp_path, "ov-stale"))
    body = client.get("/", headers=_HTML).text
    assert 'data-canonical-read="canonical"' in body
    assert "usage projection current · stale (3 pending write(s))" in body


def test_overview_html_store_divergence_falls_back_labeled(tmp_path, monkeypatch):
    # the three reads are independent snapshots; if a store swap lands mid-read
    # so they observe different store identities, refuse to stamp one store's
    # identity on another's rows -> labeled v1 fallback, accounted to overview.
    import dataclasses
    from agentacct.canonical_read import CanonicalReadRuntime

    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    real_session = CanonicalReadRuntime._session_list_read

    def _diverged(self, **kw):
        read = real_session(self, **kw)
        return dataclasses.replace(
            read, store={**dict(read.store), "store_uuid": "deadbeef-divergent"}
        )

    monkeypatch.setattr(CanonicalReadRuntime, "_session_list_read", _diverged)
    client = _api_client(_served_store(tmp_path, "ov-diverge"))
    resp = client.get("/", headers=_HTML)
    assert resp.status_code == 200
    body = resp.text
    assert 'data-canonical-read="v1_fallback"' in body
    assert 'data-canonical-surface="overview"' in body
    assert "diverging store identities" in body
    health = client.get("/health").json()
    assert health["canonical_read"]["surfaces"]["overview"]["errors"] >= 1


# --- parity: canonical HTML vs canonical JSON twin --------------------------


def test_sessions_html_canonical_parity_with_json(tmp_path, monkeypatch):
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(_served_store(tmp_path, "parity"))
    json_payload = client.get("/sessions").json()  # default accept => canonical JSON
    assert json_payload["canonical_read"]["source"] == "canonical"
    uuid8 = str(json_payload["canonical_read"]["store"]["store_uuid"])[:8]
    shown = json_payload["session_count_shown"]
    html = client.get("/sessions", headers=_HTML).text
    assert f"store {uuid8}" in html
    assert f"({shown} shown)" in html


def test_overview_html_canonical_parity_with_json(tmp_path, monkeypatch):
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(_served_store(tmp_path, "ov-parity"))
    # the composite Overview reads the SAME canonical store the JSON twins do
    json_sessions = client.get("/sessions").json()
    assert json_sessions["canonical_read"]["source"] == "canonical"
    uuid8 = str(json_sessions["canonical_read"]["store"]["store_uuid"])[:8]
    json_tasks = client.get("/tasks").json()
    assert json_tasks["canonical_read"]["source"] == "canonical"
    html = client.get("/", headers=_HTML).text
    assert f"store {uuid8}" in html
    # roster shows a bounded slice of the same sessions the JSON twin lists
    shown = min(json_sessions["session_count_shown"], 25)
    assert f"Session roster <span class=\"note\">({shown} shown)" in html
