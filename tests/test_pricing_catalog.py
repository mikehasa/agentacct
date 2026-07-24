import json
import time
from pathlib import Path

import httpx

from agent_chronicle.cost import (
    LEGACY_PRICING_CATALOG_PATH_ENV,
    PRICING_CATALOG_PATH_ENV,
    estimate_model_cost_breakdown_usd,
    estimate_model_cost_usd,
    has_model_price,
    model_pricing_entry,
    reset_pricing_catalog_cache,
)
from agent_chronicle.pricing_catalog import (
    default_pricing_catalog_snapshot_path,
    ensure_fresh_pricing_snapshot,
    pricing_catalog_metadata_path,
    read_pricing_snapshot_metadata,
    write_litellm_pricing_snapshot,
)

_LITELLM_TTL_FIXTURE = {
    "gpt-ttl-test": {
        "litellm_provider": "openai",
        "input_cost_per_token": 0.000001,
        "output_cost_per_token": 0.000003,
    }
}


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _install_fake_fetch(monkeypatch, payload=None, *, error=None):
    calls = []

    def _get(url, **kwargs):
        calls.append(url)
        if error is not None:
            raise error
        return _FakeResponse(payload)

    monkeypatch.setattr(httpx, "get", _get)
    return calls


def _enable_auto_refresh(monkeypatch):
    monkeypatch.setenv("AGENT_CHRONICLE_PRICING_AUTO_REFRESH", "1")
    monkeypatch.delenv("AGENT_SENTINEL_PRICING_AUTO_REFRESH", raising=False)
    monkeypatch.delenv(PRICING_CATALOG_PATH_ENV, raising=False)
    monkeypatch.delenv(LEGACY_PRICING_CATALOG_PATH_ENV, raising=False)


def test_litellm_pricing_catalog_extends_model_coverage(tmp_path, monkeypatch):
    catalog_path = tmp_path / "model_prices_and_context_window.json"
    catalog_path.write_text(
        json.dumps(
            {
                "gpt-test-auto": {
                    "litellm_provider": "openai",
                    "input_cost_per_token": 0.000001,
                    "output_cost_per_token": 0.000003,
                    "cache_creation_input_token_cost": 0.00000125,
                    "cache_read_input_token_cost": 0.0000001,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(PRICING_CATALOG_PATH_ENV, str(catalog_path))
    reset_pricing_catalog_cache()

    assert has_model_price("openai", "gpt-test-auto") is True
    entry = model_pricing_entry("openai", "gpt-test-auto")
    assert entry is not None
    assert entry.source == "litellm_model_cost_map"
    assert entry.input_cost_per_1m == 1.0
    assert entry.output_cost_per_1m == 3.0

    breakdown = estimate_model_cost_breakdown_usd(
        "openai",
        "gpt-test-auto",
        input_tokens=1_000_000,
        output_tokens=2_000_000,
        cache_creation_input_tokens=1_000_000,
        cache_read_input_tokens=3_000_000,
    )

    assert breakdown["input_cost_usd"] == 1.0
    assert breakdown["output_cost_usd"] == 6.0
    assert breakdown["cache_creation_cost_usd"] == 1.25
    assert breakdown["cache_read_cost_usd"] == 0.3
    assert breakdown["total_cost_usd"] == 8.55


def test_litellm_catalog_strips_provider_prefix_for_openrouter_models(tmp_path, monkeypatch):
    catalog_path = tmp_path / "litellm.json"
    catalog_path.write_text(
        json.dumps(
            {
                "openrouter/anthropic/claude-test": {
                    "litellm_provider": "openrouter",
                    "input_cost_per_token": 0.000002,
                    "output_cost_per_token": 0.00001,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(PRICING_CATALOG_PATH_ENV, str(catalog_path))
    reset_pricing_catalog_cache()

    assert has_model_price("openrouter", "anthropic/claude-test") is True
    assert has_model_price("openrouter", "openrouter/anthropic/claude-test") is True


def test_agent_sentinel_catalog_shape_can_override_prices(tmp_path, monkeypatch):
    catalog_path = tmp_path / "sentinel-pricing.json"
    catalog_path.write_text(
        json.dumps(
            {
                "pricing": [
                    {
                        "provider": "local-test",
                        "model": "demo-model",
                        "input_cost_per_1m": 2.0,
                        "output_cost_per_1m": 8.0,
                        "source": "test_override",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(PRICING_CATALOG_PATH_ENV, str(catalog_path))
    reset_pricing_catalog_cache()

    breakdown = estimate_model_cost_breakdown_usd(
        "local-test",
        "demo-model",
        input_tokens=500_000,
        output_tokens=250_000,
    )

    assert has_model_price("local-test", "demo-model") is True
    assert model_pricing_entry("local-test", "demo-model").source == "test_override"
    assert breakdown["total_cost_usd"] == 3.0


# ---------------------------------------------------------------------------
# TTL auto-refresh (ensure_fresh_pricing_snapshot)
# ---------------------------------------------------------------------------


def test_ensure_fresh_fetches_when_snapshot_absent_then_skips_while_fresh(tmp_path, monkeypatch):
    _enable_auto_refresh(monkeypatch)
    calls = _install_fake_fetch(monkeypatch, _LITELLM_TTL_FIXTURE)
    store_dir = tmp_path / "state"

    outcome = ensure_fresh_pricing_snapshot(store_dir)

    assert outcome["refreshed"] is True
    assert outcome["reason"] == "refreshed"
    assert len(calls) == 1
    snapshot_path = default_pricing_catalog_snapshot_path(store_dir)
    assert snapshot_path is not None and snapshot_path.exists()
    metadata = read_pricing_snapshot_metadata(snapshot_path)
    assert metadata["source"] == "litellm_model_cost_map"
    assert abs(metadata["fetched_at"] - time.time()) < 60
    assert "last_refresh_error" not in metadata

    # Second call within the TTL: no fetch.
    again = ensure_fresh_pricing_snapshot(store_dir)
    assert again == {"refreshed": False, "reason": "fresh"}
    assert len(calls) == 1


def test_ensure_fresh_refetches_a_stale_snapshot(tmp_path, monkeypatch):
    _enable_auto_refresh(monkeypatch)
    calls = _install_fake_fetch(monkeypatch, _LITELLM_TTL_FIXTURE)
    store_dir = tmp_path / "state"
    snapshot_path = default_pricing_catalog_snapshot_path(store_dir)
    stale_at = time.time() - 8 * 86400
    write_litellm_pricing_snapshot(snapshot_path, _LITELLM_TTL_FIXTURE, source_url="test://seed", fetched_at=stale_at)

    outcome = ensure_fresh_pricing_snapshot(store_dir)

    assert outcome["refreshed"] is True
    assert len(calls) == 1
    metadata = read_pricing_snapshot_metadata(snapshot_path)
    assert metadata["fetched_at"] > stale_at


def test_ensure_fresh_never_fetches_when_catalog_env_is_pinned(tmp_path, monkeypatch):
    _enable_auto_refresh(monkeypatch)
    calls = _install_fake_fetch(monkeypatch, _LITELLM_TTL_FIXTURE)
    store_dir = tmp_path / "state"

    # New env name pinned.
    monkeypatch.setenv(PRICING_CATALOG_PATH_ENV, str(tmp_path / "custom.json"))
    assert ensure_fresh_pricing_snapshot(store_dir) == {"refreshed": False, "reason": "env_pinned"}
    # Pre-rename alias pinned: same refusal (read_env_alias).
    monkeypatch.delenv(PRICING_CATALOG_PATH_ENV, raising=False)
    monkeypatch.setenv(LEGACY_PRICING_CATALOG_PATH_ENV, str(tmp_path / "custom.json"))
    assert ensure_fresh_pricing_snapshot(store_dir) == {"refreshed": False, "reason": "env_pinned"}
    # Callers that pin the env THEMSELVES (serve middleware) pass env_pinned
    # explicitly so their per-request pin does not block the refresh.
    outcome = ensure_fresh_pricing_snapshot(store_dir, env_pinned=False)
    assert outcome["refreshed"] is True

    assert len(calls) == 1  # only the env_pinned=False call fetched


def test_ensure_fresh_disabled_via_env_including_legacy_alias(tmp_path, monkeypatch):
    calls = _install_fake_fetch(monkeypatch, _LITELLM_TTL_FIXTURE)
    store_dir = tmp_path / "state"

    # The suite conftest exports AGENT_CHRONICLE_PRICING_AUTO_REFRESH=0.
    assert ensure_fresh_pricing_snapshot(store_dir) == {"refreshed": False, "reason": "disabled"}
    # Pre-rename alias disables too.
    monkeypatch.delenv("AGENT_CHRONICLE_PRICING_AUTO_REFRESH", raising=False)
    monkeypatch.setenv("AGENT_SENTINEL_PRICING_AUTO_REFRESH", "0")
    assert ensure_fresh_pricing_snapshot(store_dir) == {"refreshed": False, "reason": "disabled"}
    assert calls == []


def test_ensure_fresh_fetch_failure_keeps_stale_snapshot_and_records_error(tmp_path, monkeypatch):
    _enable_auto_refresh(monkeypatch)
    calls = _install_fake_fetch(monkeypatch, error=RuntimeError("network down"))
    store_dir = tmp_path / "state"
    snapshot_path = default_pricing_catalog_snapshot_path(store_dir)
    stale_at = time.time() - 8 * 86400
    write_litellm_pricing_snapshot(snapshot_path, _LITELLM_TTL_FIXTURE, source_url="test://seed", fetched_at=stale_at)
    stale_bytes = snapshot_path.read_bytes()

    outcome = ensure_fresh_pricing_snapshot(store_dir)

    # NEVER raises, never blocks an import; the stale snapshot is kept.
    assert outcome["refreshed"] is False
    assert outcome["reason"] == "error"
    assert "network down" in outcome["error"]
    assert len(calls) == 1
    assert snapshot_path.read_bytes() == stale_bytes
    metadata = read_pricing_snapshot_metadata(snapshot_path)
    assert metadata["fetched_at"] == stale_at  # stale timestamp preserved: retried next scan
    assert "network down" in metadata["last_refresh_error"]
    assert abs(metadata["last_refresh_attempt_at"] - time.time()) < 60


def test_ensure_fresh_backoff_throttles_repeated_failed_fetches(tmp_path, monkeypatch):
    """A dead network must cost at most ONE fetch attempt per backoff window
    per store — never one per import/watch tick (the 120 s slow-link timeout
    would otherwise stall every tick)."""

    _enable_auto_refresh(monkeypatch)
    calls = _install_fake_fetch(monkeypatch, error=RuntimeError("network down"))
    store_dir = tmp_path / "state"
    snapshot_path = default_pricing_catalog_snapshot_path(store_dir)
    now = time.time()
    stale_at = now - 30 * 86400
    write_litellm_pricing_snapshot(snapshot_path, _LITELLM_TTL_FIXTURE, source_url="test://seed", fetched_at=stale_at)

    first = ensure_fresh_pricing_snapshot(store_dir, now=now)
    assert first["reason"] == "error"
    assert len(calls) == 1

    # Watch ticks within the next hour: stale-but-throttled, NO network attempt.
    for tick in range(1, 6):
        outcome = ensure_fresh_pricing_snapshot(store_dir, now=now + tick * 300)
        assert outcome["refreshed"] is False
        assert outcome["reason"] == "throttled"
    assert len(calls) == 1

    # Once the backoff window has passed, the fetch is retried.
    retried = ensure_fresh_pricing_snapshot(store_dir, now=now + 3601)
    assert retried["reason"] == "error"
    assert len(calls) == 2


def test_ensure_fresh_repairs_unreadable_snapshot_despite_fresh_sidecar(tmp_path, monkeypatch):
    """A corrupted snapshot behind a fresh sidecar must count as stale: the
    freshness gate validates the snapshot itself, and the fetch repairs it."""

    _enable_auto_refresh(monkeypatch)
    calls = _install_fake_fetch(monkeypatch, _LITELLM_TTL_FIXTURE)
    store_dir = tmp_path / "state"
    snapshot_path = default_pricing_catalog_snapshot_path(store_dir)
    write_litellm_pricing_snapshot(snapshot_path, _LITELLM_TTL_FIXTURE, source_url="test://seed", fetched_at=time.time())
    snapshot_path.write_text("{ torn garbage", encoding="utf-8")

    outcome = ensure_fresh_pricing_snapshot(store_dir)

    assert outcome["refreshed"] is True
    assert len(calls) == 1
    assert "gpt-ttl-test" in json.loads(snapshot_path.read_text(encoding="utf-8"))
    metadata = read_pricing_snapshot_metadata(snapshot_path)
    assert "last_refresh_error" not in metadata


def test_unreadable_snapshot_repair_respects_backoff_and_records_diagnostic(tmp_path, monkeypatch):
    """When the repair fetch is throttled by the backoff, the sidecar still
    says WHY pricing is falling back to builtin: last_refresh_error records
    the unreadable snapshot."""

    _enable_auto_refresh(monkeypatch)
    calls = _install_fake_fetch(monkeypatch, _LITELLM_TTL_FIXTURE)
    store_dir = tmp_path / "state"
    snapshot_path = default_pricing_catalog_snapshot_path(store_dir)
    now = time.time()
    write_litellm_pricing_snapshot(snapshot_path, _LITELLM_TTL_FIXTURE, source_url="test://seed", fetched_at=now)
    snapshot_path.write_text("", encoding="utf-8")  # empty file: unreadable too
    metadata_path = pricing_catalog_metadata_path(snapshot_path)
    seeded = json.loads(metadata_path.read_text(encoding="utf-8"))
    seeded["last_refresh_attempt_at"] = now - 60  # an attempt just happened
    metadata_path.write_text(json.dumps(seeded), encoding="utf-8")

    outcome = ensure_fresh_pricing_snapshot(store_dir, now=now)

    assert outcome["refreshed"] is False
    assert outcome["reason"] == "throttled"
    assert calls == []
    metadata = read_pricing_snapshot_metadata(snapshot_path)
    assert metadata["last_refresh_error"] == "snapshot unreadable"


def test_ensure_fresh_treats_future_dated_sidecar_as_stale(tmp_path, monkeypatch):
    """Clock-skew clamp: a fetched_at recorded while the system clock was
    wrong (far in the future) must not freeze auto-refresh until wall-clock
    time catches up with the bogus timestamp."""

    _enable_auto_refresh(monkeypatch)
    calls = _install_fake_fetch(monkeypatch, _LITELLM_TTL_FIXTURE)
    store_dir = tmp_path / "state"
    snapshot_path = default_pricing_catalog_snapshot_path(store_dir)
    now = time.time()
    write_litellm_pricing_snapshot(snapshot_path, _LITELLM_TTL_FIXTURE, source_url="test://seed", fetched_at=now + 10 * 365 * 86400)
    # A future-dated attempt timestamp must not freeze the backoff either.
    metadata_path = pricing_catalog_metadata_path(snapshot_path)
    seeded = json.loads(metadata_path.read_text(encoding="utf-8"))
    seeded["last_refresh_attempt_at"] = now + 10 * 365 * 86400
    metadata_path.write_text(json.dumps(seeded), encoding="utf-8")

    outcome = ensure_fresh_pricing_snapshot(store_dir, now=now)

    assert outcome["refreshed"] is True
    assert len(calls) == 1

    # Small forward skew (within the tolerance) still counts as fresh.
    write_litellm_pricing_snapshot(snapshot_path, _LITELLM_TTL_FIXTURE, source_url="test://seed", fetched_at=now + 200)
    assert ensure_fresh_pricing_snapshot(store_dir, now=now) == {"refreshed": False, "reason": "fresh"}
    assert len(calls) == 1


def test_pricing_snapshot_writes_are_atomic_temp_plus_replace(tmp_path, monkeypatch):
    import os as os_module

    import agent_chronicle.pricing_catalog as pricing_catalog_module

    snapshot_path = tmp_path / "pricing" / "litellm_model_prices.json"
    write_litellm_pricing_snapshot(snapshot_path, _LITELLM_TTL_FIXTURE, source_url="test://seed", fetched_at=1.0)
    old_bytes = snapshot_path.read_bytes()

    real_replace = os_module.replace
    replaced: list[tuple[str, str]] = []

    def _guarded_replace(src, dst):
        dst_path = Path(dst)
        if dst_path == snapshot_path:
            # Right up to the atomic swap the destination still holds the
            # complete OLD snapshot: a concurrent reader (the serve
            # re-reader) can never observe a torn/partial file.
            assert dst_path.read_bytes() == old_bytes
            assert ".tmp-" in Path(src).name
        replaced.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(pricing_catalog_module.os, "replace", _guarded_replace)
    write_litellm_pricing_snapshot(
        snapshot_path,
        {"gpt-ttl-test-v2": dict(_LITELLM_TTL_FIXTURE["gpt-ttl-test"])},
        source_url="test://seed2",
        fetched_at=2.0,
    )

    destinations = {dst for _src, dst in replaced}
    assert str(snapshot_path) in destinations
    assert str(pricing_catalog_metadata_path(snapshot_path)) in destinations
    assert "gpt-ttl-test-v2" in json.loads(snapshot_path.read_text(encoding="utf-8"))


def test_concurrent_same_pid_snapshot_writers_use_unique_temp_files(tmp_path, monkeypatch):
    """Two overlapping writers in ONE pid (the dashboard's sync import route
    runs in a threadpool) must never share a temp file: with a shared
    .tmp-<pid> name the loser's os.replace raises FileNotFoundError and the
    final path can transiently hold a torn write."""

    import os as os_module
    import threading

    import agent_chronicle.pricing_catalog as pricing_catalog_module
    from agent_chronicle.pricing_catalog import _write_json_atomic

    final_path = tmp_path / "pricing" / "snap.json"
    real_replace = os_module.replace
    a_wrote_temp = threading.Event()
    b_finished = threading.Event()
    temp_sources: list[str] = []
    errors: list[BaseException] = []

    def _guarded_replace(src, dst):
        temp_sources.append(str(src))
        if threading.current_thread().name == "writer-a":
            a_wrote_temp.set()
            assert b_finished.wait(10)
        return real_replace(src, dst)

    monkeypatch.setattr(pricing_catalog_module.os, "replace", _guarded_replace)

    def _writer_a():
        try:
            _write_json_atomic(final_path, {"writer": "a"})
        except BaseException as exc:  # pragma: no cover - the failure under test
            errors.append(exc)

    thread_a = threading.Thread(target=_writer_a, name="writer-a")
    thread_a.start()
    assert a_wrote_temp.wait(10)
    # Writer B (same pid, different thread) completes a FULL write while A is
    # paused between writing its temp file and its os.replace.
    _write_json_atomic(final_path, {"writer": "b"})
    b_finished.set()
    thread_a.join(10)
    assert not thread_a.is_alive()

    assert errors == []  # shared temp name: A's os.replace raised FileNotFoundError
    assert len(set(temp_sources)) == 2  # per-write unique temp names
    assert json.loads(final_path.read_text(encoding="utf-8")) == {"writer": "a"}


# ---------------------------------------------------------------------------
# codex -> openai provider alias
# ---------------------------------------------------------------------------


def test_codex_alias_prices_openai_keyed_litellm_models_while_builtin_keys_win(tmp_path, monkeypatch):
    catalog_path = tmp_path / "litellm.json"
    catalog_path.write_text(
        json.dumps(
            {
                # The live gap: gpt-5.6* exists only under LiteLLM's openai key.
                "gpt-5.6-sol": {
                    "litellm_provider": "openai",
                    "input_cost_per_token": 0.000005,
                    "output_cost_per_token": 0.00003,
                    "cache_read_input_token_cost": 0.0000005,
                },
                # A snapshot row for gpt-5.5 too: the exact builtin
                # ("codex", "gpt-5.5") key must STILL win over the alias.
                "gpt-5.5": {
                    "litellm_provider": "openai",
                    "input_cost_per_token": 0.000005,
                    "output_cost_per_token": 0.00003,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(PRICING_CATALOG_PATH_ENV, str(catalog_path))
    reset_pricing_catalog_cache()

    assert has_model_price("codex", "gpt-5.6-sol") is True
    aliased = model_pricing_entry("codex", "gpt-5.6-sol")
    assert aliased.source == "litellm_model_cost_map"
    assert aliased.source_provider == "openai"
    assert aliased.input_cost_per_1m == 5.0
    assert aliased.output_cost_per_1m == 30.0
    assert aliased.cost_multiplier == 1.0  # list price: the 2.5x convention is NOT extended

    builtin = model_pricing_entry("codex", "gpt-5.5")
    assert builtin.source == "agent_sentinel_builtin"
    assert builtin.cost_multiplier == 2.5  # deliberate ccusage fast-pricing convention preserved
    assert estimate_model_cost_usd("codex", "gpt-5.5", 1_000_000, 0) == 5.00 * 2.5


def test_builtin_multiplier_rows_survive_exact_key_snapshot_collision(tmp_path, monkeypatch):
    """Beyond the alias path: an external row keyed EXACTLY ("codex",
    "gpt-5.5") (LiteLLM upstream shipping litellm_provider "codex", or a
    pinned file carrying one) must never silently replace the deliberate
    builtin 2.5x ccusage fast-pricing row via the merge. External entries
    still win for codex keys the builtin table lacks."""

    catalog_path = tmp_path / "litellm.json"
    catalog_path.write_text(
        json.dumps(
            {
                # Exact-key collision with the builtin multiplier row.
                "gpt-5.5": {
                    "litellm_provider": "codex",
                    "input_cost_per_token": 0.000005,
                    "output_cost_per_token": 0.00003,
                },
                # A codex-keyed model the builtin never listed: coverage
                # still extends from the external row.
                "gpt-5.7-new": {
                    "litellm_provider": "codex",
                    "input_cost_per_token": 0.000002,
                    "output_cost_per_token": 0.000008,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(PRICING_CATALOG_PATH_ENV, str(catalog_path))
    reset_pricing_catalog_cache()

    protected = model_pricing_entry("codex", "gpt-5.5")
    assert protected is not None
    assert protected.source == "agent_sentinel_builtin"
    assert protected.cost_multiplier == 2.5
    assert estimate_model_cost_usd("codex", "gpt-5.5", 1_000_000, 0) == 5.00 * 2.5

    extended = model_pricing_entry("codex", "gpt-5.7-new")
    assert extended is not None
    assert extended.source == "litellm_model_cost_map"
    assert extended.input_cost_per_1m == 2.0


def test_new_builtin_model_prices_cover_fable_and_gpt_5_4_mini(monkeypatch):
    monkeypatch.delenv(PRICING_CATALOG_PATH_ENV, raising=False)
    reset_pricing_catalog_cache()

    assert has_model_price("claude-code", "claude-fable-5") is True
    fable = estimate_model_cost_breakdown_usd(
        "claude-code",
        "claude-fable-5",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_5m_input_tokens=1_000_000,
        cache_creation_1h_input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
    )
    assert fable["input_cost_usd"] == 10.0
    assert fable["output_cost_usd"] == 50.0
    assert fable["cache_creation_5m_cost_usd"] == 12.5
    assert fable["cache_creation_1h_cost_usd"] == 20.0
    assert fable["cache_read_cost_usd"] == 1.0

    assert has_model_price("openai", "gpt-5.4-mini") is True
    mini = estimate_model_cost_breakdown_usd(
        "openai",
        "gpt-5.4-mini",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
    )
    assert mini["input_cost_usd"] == 0.75
    assert mini["output_cost_usd"] == 4.5
    assert mini["cache_read_cost_usd"] == 0.075
