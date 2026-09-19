from __future__ import annotations

import json
import subprocess
import time

import httpx
import pytest

import agentacct.self_update as su
from agentacct import version as version_mod


def test_version_tuple_and_is_newer():
    assert su._version_tuple("0.11.0+ecc9d1def776") == (0, 11, 0)
    assert su._version_tuple("0.0.0+source") == (0, 0, 0)
    assert su._is_newer("0.12.0", "0.11.0")
    assert su._is_newer("0.11.1", "0.11.0")
    assert not su._is_newer("0.11.0", "0.11.0")
    assert not su._is_newer("0.10.9", "0.11.0")


def test_is_dev_install_true_for_source_sentinel(monkeypatch):
    monkeypatch.setattr(version_mod, "package_version", lambda: "0.0.0+source")
    assert su.is_dev_install() is True


def test_is_dev_install_true_for_src_checkout():
    # The test process imports agentacct from the repo's src/ tree, which is not
    # under site-packages / uv tools — the dev-checkout signal.
    assert su.is_dev_install() is True


def test_query_pypi_latest_returns_info_version(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"info": {"version": "9.9.9"}}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    assert su.query_pypi_latest() == "9.9.9"


def test_query_pypi_latest_none_on_error(monkeypatch):
    def _boom(*a, **k):
        raise httpx.HTTPError("nope")

    monkeypatch.setattr(httpx, "get", _boom)
    assert su.query_pypi_latest() is None


def test_update_status_cache_hit_does_no_network(tmp_path, monkeypatch):
    sidecar = su._sidecar_path(tmp_path)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps({"latest": "0.99.0", "fetched_at": time.time()}), encoding="utf-8")
    called = {"n": 0}

    def _count(**_kwargs):
        called["n"] += 1
        return "1.0.0"

    monkeypatch.setattr(su, "query_pypi_latest", _count)
    monkeypatch.setattr(version_mod, "package_version", lambda: "0.11.0")
    monkeypatch.setattr(su, "is_dev_install", lambda: False)

    status = su.update_status(store_dir=tmp_path, allow_network=True)

    assert called["n"] == 0  # fresh cache -> no network
    assert status.latest == "0.99.0"
    assert status.update_available is True
    assert status.source == "cache"


def test_update_status_writes_0600_sidecar_on_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(su, "query_pypi_latest", lambda **_k: "0.20.0")
    monkeypatch.setattr(version_mod, "package_version", lambda: "0.11.0")
    monkeypatch.setattr(su, "is_dev_install", lambda: False)

    status = su.update_status(store_dir=tmp_path, allow_network=True)

    assert status.latest == "0.20.0"
    assert status.source == "pypi"
    assert status.update_available is True
    latest, fetched = su._read_sidecar(su._sidecar_path(tmp_path))
    assert latest == "0.20.0"
    assert fetched is not None
    assert oct(su._sidecar_path(tmp_path).stat().st_mode)[-3:] == "600"


def test_update_status_dev_never_offers_update(tmp_path, monkeypatch):
    monkeypatch.setattr(su, "query_pypi_latest", lambda **_k: "99.0.0")
    monkeypatch.setattr(version_mod, "package_version", lambda: "0.11.0")
    monkeypatch.setattr(su, "is_dev_install", lambda: True)

    status = su.update_status(store_dir=tmp_path, allow_network=True)

    assert status.is_dev_install is True
    assert status.update_available is False


def test_update_status_allow_network_false_is_offline_when_no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(version_mod, "package_version", lambda: "0.11.0")
    monkeypatch.setattr(su, "is_dev_install", lambda: False)

    def _forbidden(**_k):
        raise AssertionError("must not hit the network when allow_network=False")

    monkeypatch.setattr(su, "query_pypi_latest", _forbidden)
    status = su.update_status(store_dir=tmp_path, allow_network=False)
    assert status.latest is None
    assert status.update_available is False
    assert status.source == "offline"


def test_apply_update_refuses_dev(monkeypatch):
    monkeypatch.setattr(su, "is_dev_install", lambda: True)
    with pytest.raises(RuntimeError):
        su.apply_update("1.0.0")


def test_apply_update_runs_uv(monkeypatch):
    monkeypatch.setattr(su, "is_dev_install", lambda: False)
    monkeypatch.setattr(version_mod, "package_version", lambda: "0.11.0")
    captured = {}

    class _Completed:
        stdout = "ok"

    def _run(argv, **_kwargs):
        captured["argv"] = argv
        return _Completed()

    monkeypatch.setattr(subprocess, "run", _run)
    result = su.apply_update("0.12.0")
    assert captured["argv"] == ["uv", "tool", "install", "agentacct==0.12.0", "--force", "--no-cache"]
    assert result["from"] == "0.11.0"
    assert result["to"] == "0.12.0"
