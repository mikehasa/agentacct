"""Canonical-read three-state behavior of the kept ``/sessions`` JSON surface.

Originally the P1 HTML dashboard read-migration suite. The HTML display layer
is fully retired (``GET /sessions`` is JSON-only; no accept negotiation), so
the locked three-state principle is asserted on the JSON lane it always
shared: (1) an unlabeled v1 payload when the read flag is off, (2) a payload
labeled ``canonical_read.source == "canonical"`` when the flag is on and the
store serves, and (3) a v1 payload under a VISIBLE
``canonical_read.source == "v1_fallback"`` label when the flag is on but the
store cannot honestly serve — never a silent v1 payload. Each fallback must
also be counted on the ``/health`` per-surface ``session_list`` lane
(``unavailable`` / ``errors``), so a degraded read can never pass unnoticed.

Store/flag conventions follow test_canonical_reader.py: stores are imported
into tmp_path and promoted to live; the flag is pinned off suite-wide by
conftest and turned on here with monkeypatch.setenv.
"""

from __future__ import annotations

import agentacct.api as api
from agentacct.canonical_read import CANONICAL_READ_ENV

from test_canonical_reader import _api_client, _imported_live_store
from test_canonical_reader_surfaces import _work_ledger_events


def _served_store(tmp_path, name):
    return _imported_live_store(tmp_path, _work_ledger_events(), name=name)


# --- sessions ---------------------------------------------------------------


def test_sessions_flag_off_is_v1_unlabeled(tmp_path):
    client = _api_client(_served_store(tmp_path, "sess-off"))
    payload = client.get("/sessions").json()
    assert payload["schema_version"] == "agent-sentinel.work-ledger.v2"
    assert "canonical_read" not in payload


def test_sessions_flag_on_served_is_canonical_labeled(tmp_path, monkeypatch):
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(_served_store(tmp_path, "sess-on"))
    resp = client.get("/sessions")
    assert resp.status_code == 200
    payload = resp.json()
    label = payload["canonical_read"]
    assert label["active"] is True
    assert label["source"] == "canonical"
    assert label["source"] != "v1_fallback"
    assert payload["schema_version"] == "agent-sentinel.session-rollup.v2-canonical"
    # base-fact table: no projection gate; HTML-era filter params are echoed
    # as ignored, and unrepresentable fields stay declared, never synthesized
    filtered = client.get("/sessions", params={"client": "codex"}).json()
    assert filtered["ignored_html_params"] == ["client"]
    assert "observed_unvalidated_lineage" in payload["model_gaps"]["not_served"]


def test_sessions_flag_on_unavailable_falls_back_labeled(tmp_path, monkeypatch):
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(tmp_path / "empty-store")  # no chronicle.sqlite3
    resp = client.get("/sessions")
    assert resp.status_code == 200
    payload = resp.json()
    label = payload["canonical_read"]
    assert label["active"] is False
    assert label["source"] == "v1_fallback"
    assert label["reason"] == "store_absent"
    assert payload["schema_version"] == "agent-sentinel.work-ledger.v2"
    health = client.get("/health").json()
    assert health["canonical_read"]["surfaces"]["session_list"]["unavailable"] >= 1


def test_sessions_flag_on_crash_falls_back_labeled(tmp_path, monkeypatch):
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")

    def _boom(*_a, **_k):
        raise RuntimeError("canonical sessions payload exploded")

    monkeypatch.setattr(api, "_canonical_session_rollup_payload", _boom)
    client = _api_client(_served_store(tmp_path, "sess-crash"))
    resp = client.get("/sessions")
    assert resp.status_code == 200
    payload = resp.json()
    label = payload["canonical_read"]
    assert label["source"] == "v1_fallback"
    assert label["reason"] == "error"
    assert "canonical sessions payload exploded" in label["detail"]
    assert payload["schema_version"] == "agent-sentinel.work-ledger.v2"
    health = client.get("/health").json()
    assert health["canonical_read"]["surfaces"]["session_list"]["errors"] >= 1
