from __future__ import annotations

import itertools
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .env_compat import read_env_alias

LITELLM_MODEL_COST_MAP_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
PRICING_CATALOG_SNAPSHOT_RELATIVE_PATH = Path("pricing") / "litellm_model_prices.json"
PRICING_CATALOG_METADATA_SUFFIX = ".metadata.json"

# Defined here (not cost.py) so the snapshot-refresh path can honor a user pin
# without a circular import; cost.py re-exports it for its many importers.
PRICING_CATALOG_PATH_ENV = "AGENTACCT_PRICING_CATALOG_PATH"
# TTL auto-refresh opt-out (read through read_env_alias: the pre-rename
# AGENT_CHRONICLE_PRICING_AUTO_REFRESH / AGENT_SENTINEL_PRICING_AUTO_REFRESH
# spellings are accepted forever).
PRICING_AUTO_REFRESH_ENV = "AGENTACCT_PRICING_AUTO_REFRESH"
PRICING_SNAPSHOT_TTL_DAYS = 7.0
# The un-compressed ~1.6 MB table has been observed to take >60 s on slow
# links; 120 s keeps the force-now and auto-refresh paths from false-failing.
PRICING_REFRESH_TIMEOUT_SECONDS = 120.0
# After a failed fetch, no new attempt for this long: a dead/slow link costs
# at most ONE (up to 120 s) attempt per hour per store, never one per
# import/watch tick.
PRICING_REFRESH_BACKOFF_SECONDS = 3600.0
# Sidecar timestamps recorded further in the future than this are treated as
# clock skew (stale), so a wrong clock at fetch time can never freeze the
# auto-refresh after the clock is corrected.
PRICING_CLOCK_SKEW_TOLERANCE_SECONDS = 300.0
_AUTO_REFRESH_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})
_SNAPSHOT_UNREADABLE_ERROR = "snapshot unreadable"


def normalize_pricing_key(value: object) -> str:
    return str(value or "").strip().lower()


def _optional_nonnegative_float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _first_nonnegative_float(data: dict[str, Any], names: Iterable[str]) -> float | None:
    for name in names:
        number = _optional_nonnegative_float(data.get(name))
        if number is not None:
            return number
    return None


@dataclass(frozen=True)
class PricingCatalogEntry:
    provider: str
    model: str
    input_cost_per_1m: float
    output_cost_per_1m: float
    cache_write_5m_cost_per_1m: float | None = None
    cache_write_1h_cost_per_1m: float | None = None
    cache_read_cost_per_1m: float | None = None
    cost_multiplier: float = 1.0
    # frozen default: "pricing_source" values are stored in event metadata
    # forever (pre-rename vocabulary) — never rename.
    source: str = "agent_sentinel_builtin"
    source_provider: str | None = None
    source_model: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", normalize_pricing_key(self.provider))
        object.__setattr__(self, "model", normalize_pricing_key(self.model))
        object.__setattr__(self, "input_cost_per_1m", float(self.input_cost_per_1m))
        object.__setattr__(self, "output_cost_per_1m", float(self.output_cost_per_1m))
        object.__setattr__(self, "cost_multiplier", float(self.cost_multiplier or 1.0))
        if self.source_provider is not None:
            object.__setattr__(self, "source_provider", normalize_pricing_key(self.source_provider))
        if self.source_model is not None:
            object.__setattr__(self, "source_model", normalize_pricing_key(self.source_model))

    @property
    def key(self) -> tuple[str, str]:
        return self.provider, self.model

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "input_cost_per_1m": self.input_cost_per_1m,
            "output_cost_per_1m": self.output_cost_per_1m,
            "cache_write_5m_cost_per_1m": self.cache_write_5m_cost_per_1m,
            "cache_write_1h_cost_per_1m": self.cache_write_1h_cost_per_1m,
            "cache_read_cost_per_1m": self.cache_read_cost_per_1m,
            "cost_multiplier": self.cost_multiplier,
            "source": self.source,
            "source_provider": self.source_provider,
            "source_model": self.source_model,
        }


class PricingCatalog:
    def __init__(
        self,
        entries: Iterable[PricingCatalogEntry] = (),
        *,
        provider_aliases: dict[str, str] | None = None,
    ) -> None:
        self._entries: dict[tuple[str, str], PricingCatalogEntry] = {}
        self._provider_aliases = {
            normalize_pricing_key(key): normalize_pricing_key(value)
            for key, value in (provider_aliases or {}).items()
        }
        for entry in entries:
            self.add(entry)

    def add(self, entry: PricingCatalogEntry) -> None:
        self._entries[entry.key] = entry

    def merged(self, entries: Iterable[PricingCatalogEntry]) -> PricingCatalog:
        catalog = PricingCatalog(provider_aliases=self._provider_aliases)
        catalog._entries.update(self._entries)
        for entry in entries:
            existing = catalog._entries.get(entry.key)
            if (
                existing is not None
                and existing.source == "agent_sentinel_builtin"
                and existing.cost_multiplier != 1.0
            ):
                # A deliberate builtin CONVENTION row — e.g. the ("codex",
                # "gpt-5.5") 2.5x ccusage fast-pricing multiplier — is never
                # silently replaced by an external row carrying the same
                # exact key (a LiteLLM upstream row shipping
                # litellm_provider "codex", or a pinned file with one, would
                # otherwise drop the multiplier to 1.0 with no warning).
                # External entries still win for keys the builtin lacks and
                # for plain list-price builtin rows.
                continue
            catalog.add(entry)
        return catalog

    def lookup(self, provider: str, model: str, *, allow_default: bool = False) -> PricingCatalogEntry | None:
        provider_key = normalize_pricing_key(provider)
        model_key = normalize_pricing_key(model)
        if not provider_key or not model_key:
            return None
        entry = self._entries.get((provider_key, model_key))
        if entry is not None:
            return entry
        alias_provider = self._provider_aliases.get(provider_key)
        if alias_provider:
            entry = self._entries.get((alias_provider, model_key))
            if entry is not None:
                return entry
        if allow_default:
            return self._entries.get(("default", "default"))
        return None

    def entries(self) -> list[PricingCatalogEntry]:
        return sorted(self._entries.values(), key=lambda entry: (entry.provider, entry.model, entry.source))

    def source_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self._entries.values():
            counts[entry.source] = counts.get(entry.source, 0) + 1
        return counts


def _litellm_provider_from_model_key(model_key: str) -> str | None:
    if "/" not in model_key:
        return None
    provider, _ = model_key.split("/", 1)
    return normalize_pricing_key(provider)


def _litellm_model_aliases(model_key: str, provider: str) -> list[str]:
    normalized = normalize_pricing_key(model_key)
    aliases = [normalized]
    provider_prefix = f"{normalize_pricing_key(provider)}/"
    if normalized.startswith(provider_prefix) and len(normalized) > len(provider_prefix):
        aliases.append(normalized[len(provider_prefix) :])
    return list(dict.fromkeys(alias for alias in aliases if alias))


def entries_from_litellm_model_cost_map(data: dict[str, Any], *, source: str = "litellm_model_cost_map") -> list[PricingCatalogEntry]:
    entries: list[PricingCatalogEntry] = []
    for model_key, raw_details in data.items():
        if model_key == "sample_spec" or not isinstance(raw_details, dict):
            continue
        input_per_token = _first_nonnegative_float(raw_details, ("input_cost_per_token", "prompt_cost_per_token"))
        output_per_token = _first_nonnegative_float(raw_details, ("output_cost_per_token", "completion_cost_per_token"))
        if input_per_token is None or output_per_token is None:
            continue
        provider = normalize_pricing_key(raw_details.get("litellm_provider") or _litellm_provider_from_model_key(str(model_key)))
        if not provider:
            continue
        cache_read_per_token = _first_nonnegative_float(
            raw_details,
            (
                "cache_read_input_token_cost",
                "cache_read_cost_per_token",
                "input_cache_read_cost_per_token",
                "input_cost_per_cache_read_token",
            ),
        )
        cache_write_5m_per_token = _first_nonnegative_float(
            raw_details,
            (
                "cache_creation_input_token_cost",
                "cache_creation_input_token_cost_5m",
                "cache_creation_5m_input_token_cost",
                "cache_write_input_token_cost",
                "input_cache_write_cost_per_token",
            ),
        )
        cache_write_1h_per_token = _first_nonnegative_float(
            raw_details,
            (
                "cache_creation_input_token_cost_1h",
                "cache_creation_1h_input_token_cost",
            ),
        )
        for model in _litellm_model_aliases(str(model_key), provider):
            entries.append(
                PricingCatalogEntry(
                    provider=provider,
                    model=model,
                    input_cost_per_1m=input_per_token * 1_000_000,
                    output_cost_per_1m=output_per_token * 1_000_000,
                    cache_write_5m_cost_per_1m=cache_write_5m_per_token * 1_000_000
                    if cache_write_5m_per_token is not None
                    else None,
                    cache_write_1h_cost_per_1m=cache_write_1h_per_token * 1_000_000
                    if cache_write_1h_per_token is not None
                    else None,
                    cache_read_cost_per_1m=cache_read_per_token * 1_000_000
                    if cache_read_per_token is not None
                    else None,
                    source=source,
                    source_provider=provider,
                    source_model=str(model_key),
                )
            )
    return entries


def _entries_from_native_catalog_items(items: Iterable[Any], *, source: str) -> list[PricingCatalogEntry]:
    entries: list[PricingCatalogEntry] = []
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        provider = normalize_pricing_key(raw_item.get("provider"))
        model = normalize_pricing_key(raw_item.get("model"))
        input_cost = _first_nonnegative_float(raw_item, ("input_cost_per_1m", "input_cost_per_million_tokens"))
        output_cost = _first_nonnegative_float(raw_item, ("output_cost_per_1m", "output_cost_per_million_tokens"))
        if not provider or not model or input_cost is None or output_cost is None:
            continue
        entries.append(
            PricingCatalogEntry(
                provider=provider,
                model=model,
                input_cost_per_1m=input_cost,
                output_cost_per_1m=output_cost,
                cache_write_5m_cost_per_1m=_first_nonnegative_float(
                    raw_item,
                    ("cache_write_5m_cost_per_1m", "cache_creation_5m_cost_per_1m", "cache_write_cost_per_1m"),
                ),
                cache_write_1h_cost_per_1m=_first_nonnegative_float(raw_item, ("cache_write_1h_cost_per_1m", "cache_creation_1h_cost_per_1m")),
                cache_read_cost_per_1m=_first_nonnegative_float(raw_item, ("cache_read_cost_per_1m", "cached_input_cost_per_1m")),
                cost_multiplier=_first_nonnegative_float(raw_item, ("cost_multiplier",)) or 1.0,
                source=str(raw_item.get("source") or source),
                source_provider=str(raw_item.get("source_provider") or provider),
                source_model=str(raw_item.get("source_model") or model),
            )
        )
    return entries


# The native catalog format (and its frozen pre-rename default source string
# "agent_sentinel_catalog", stored in event metadata as pricing_source).
def entries_from_native_catalog(data: Any, *, source: str = "agent_sentinel_catalog") -> list[PricingCatalogEntry]:
    if isinstance(data, list):
        return _entries_from_native_catalog_items(data, source=source)
    if not isinstance(data, dict):
        return []
    for key in ("pricing", "entries", "models"):
        items = data.get(key)
        if isinstance(items, list):
            return _entries_from_native_catalog_items(items, source=source)
    return []


def load_pricing_catalog_entries(path: Path, *, source: str | None = None) -> list[PricingCatalogEntry]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    file_source = source or f"file:{path.name}"
    entries = entries_from_native_catalog(payload, source=file_source)
    if entries:
        return entries
    if isinstance(payload, dict):
        return entries_from_litellm_model_cost_map(payload, source="litellm_model_cost_map")
    return []


def default_pricing_catalog_snapshot_path(store_dir: Path | str | None) -> Path | None:
    if store_dir is None:
        return None
    return Path(store_dir).expanduser() / PRICING_CATALOG_SNAPSHOT_RELATIVE_PATH


def pricing_catalog_metadata_path(snapshot_path: Path) -> Path:
    return snapshot_path.with_name(snapshot_path.name + PRICING_CATALOG_METADATA_SUFFIX)


_TEMP_WRITE_COUNTER = itertools.count()


def _write_json_atomic(path: Path, payload: Any) -> None:
    """Temp-file + os.replace: a concurrent reader (the serve middleware
    re-reads the snapshot on any mtime/size change) must never observe a
    partially-written JSON file. The temp name is unique PER WRITE (pid +
    counter): two overlapping writers in one pid (the dashboard's sync import
    route runs in a threadpool) must never share a temp file — a shared name
    lets one writer's os.replace move the other's half-written temp into
    place and makes the loser's os.replace raise."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + f".tmp-{os.getpid()}-{next(_TEMP_WRITE_COUNTER)}")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp_path, path)


def write_litellm_pricing_snapshot(
    snapshot_path: Path,
    payload: dict[str, Any],
    *,
    source_url: str,
    fetched_at: float | None = None,
) -> dict[str, Any]:
    entries = entries_from_litellm_model_cost_map(payload, source="litellm_model_cost_map")
    if not entries:
        raise ValueError("LiteLLM pricing snapshot did not contain any parseable model pricing rows")
    _write_json_atomic(snapshot_path, payload)
    metadata = {
        "source": "litellm_model_cost_map",
        "source_url": source_url,
        "fetched_at": fetched_at if fetched_at is not None else time.time(),
        "snapshot_path": str(snapshot_path),
        "entry_count": len(entries),
    }
    _write_json_atomic(pricing_catalog_metadata_path(snapshot_path), metadata)
    return metadata


def read_pricing_snapshot_metadata(snapshot_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(pricing_catalog_metadata_path(snapshot_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def pricing_auto_refresh_disabled() -> bool:
    value = read_env_alias(PRICING_AUTO_REFRESH_ENV)
    return value is not None and value.strip().lower() in _AUTO_REFRESH_DISABLED_VALUES


def fetch_litellm_model_cost_map(
    source_url: str = LITELLM_MODEL_COST_MAP_URL, *, timeout: float = PRICING_REFRESH_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """Plain GET of LiteLLM's public model cost map. No telemetry: nothing
    about this machine, store, or usage is sent — it is a public static file."""

    import httpx

    response = httpx.get(source_url, follow_redirects=True, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("LiteLLM pricing snapshot response was not a JSON object")
    return payload


def _pricing_snapshot_is_readable(snapshot_path: Path) -> bool:
    """Cheap validity check for the freshness gate: a missing, unparseable,
    or empty snapshot counts as stale so the auto-refresh repairs it instead
    of silently serving builtin-only pricing until the TTL expires."""

    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    return isinstance(payload, dict) and bool(payload)


def _sidecar_timestamp(metadata: dict[str, Any], key: str, current_time: float) -> float | None:
    """A finite, non-bool sidecar timestamp, clamped for clock skew: values
    further in the future than the tolerance are ignored (a wrong clock at
    write time must never freeze the TTL or the retry backoff)."""

    value = metadata.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if number > current_time + PRICING_CLOCK_SKEW_TOLERANCE_SECONDS:
        return None
    return number


def _record_refresh_failure(snapshot_path: Path, metadata: dict[str, Any], *, error: str, attempted_at: float) -> None:
    updated = dict(metadata)
    updated["last_refresh_error"] = error
    updated["last_refresh_attempt_at"] = attempted_at
    try:
        _write_json_atomic(pricing_catalog_metadata_path(snapshot_path), updated)
    except Exception:
        # Sidecar bookkeeping must never break the import either.
        pass


def ensure_fresh_pricing_snapshot(
    store_dir: Path | str | None,
    *,
    ttl_days: float = PRICING_SNAPSHOT_TTL_DAYS,
    source_url: str = LITELLM_MODEL_COST_MAP_URL,
    env_pinned: bool | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """TTL auto-refresh of the store-local LiteLLM pricing snapshot.

    Best-effort by contract — this NEVER raises and never blocks or fails an
    import: a failed fetch records ``last_refresh_error`` /
    ``last_refresh_attempt_at`` in the metadata sidecar and keeps the stale
    snapshot (missing beats wrong, stale beats missing).

    Never fetches when:
    - the user pinned ``AGENTACCT_PRICING_CATALOG_PATH`` (or the
      pre-rename alias) — an explicit catalog choice is respected; callers
      that pin the env themselves (the serve middleware) pass ``env_pinned``
      explicitly so their own per-request pin is not mistaken for the user's;
    - ``AGENTACCT_PRICING_AUTO_REFRESH=0`` (or the pre-rename aliases);
    - the sidecar's ``fetched_at`` is within ``ttl_days`` AND the snapshot
      itself still parses (an unreadable/empty snapshot counts as stale so
      the fetch repairs it, and the sidecar records ``last_refresh_error:
      snapshot unreadable`` until it does);
    - ``last_refresh_attempt_at`` is within the retry backoff (reason
      ``throttled``): a stale snapshot on a dead/slow link costs at most one
      network attempt per :data:`PRICING_REFRESH_BACKOFF_SECONDS` per store,
      never one per import/watch tick.

    Sidecar timestamps recorded in the future (wrong clock at write time)
    are clamped: beyond a small tolerance they count as stale/no-attempt so
    a corrected clock can never freeze refreshes.

    The download is a plain GET of LiteLLM's public open-source pricing table
    (no telemetry). Writes are atomic (temp + os.replace) so the serve
    re-reader never sees a torn snapshot.
    """

    outcome: dict[str, Any] = {"refreshed": False}
    try:
        if env_pinned is None:
            env_pinned = read_env_alias(PRICING_CATALOG_PATH_ENV) is not None
        if env_pinned:
            outcome["reason"] = "env_pinned"
            return outcome
        if pricing_auto_refresh_disabled():
            outcome["reason"] = "disabled"
            return outcome
        snapshot_path = default_pricing_catalog_snapshot_path(store_dir)
        if snapshot_path is None:
            outcome["reason"] = "no_store"
            return outcome
        current_time = time.time() if now is None else now
        metadata = read_pricing_snapshot_metadata(snapshot_path)
        fetched_at = _sidecar_timestamp(metadata, "fetched_at", current_time)
        fresh_by_time = fetched_at is not None and (current_time - fetched_at) < float(ttl_days) * 86400.0
        if fresh_by_time:
            if _pricing_snapshot_is_readable(snapshot_path):
                outcome["reason"] = "fresh"
                return outcome
            # Fresh sidecar over an unreadable snapshot (torn write, disk
            # error): treated as stale so the fetch below repairs it —
            # subject to the same backoff — and the sidecar says why pricing
            # is falling back to builtin in the meantime.
            outcome["snapshot_unreadable"] = True
            if metadata.get("last_refresh_error") != _SNAPSHOT_UNREADABLE_ERROR:
                diagnosed = dict(metadata)
                diagnosed["last_refresh_error"] = _SNAPSHOT_UNREADABLE_ERROR
                try:
                    _write_json_atomic(pricing_catalog_metadata_path(snapshot_path), diagnosed)
                except Exception:
                    # Sidecar bookkeeping must never break the import.
                    pass
        # Retry backoff: the sidecar's last_refresh_attempt_at (written on
        # every failed fetch) gates new attempts, so a dead network stalls at
        # most one call per backoff window — never every tick.
        attempted_at = _sidecar_timestamp(metadata, "last_refresh_attempt_at", current_time)
        if attempted_at is not None and (current_time - attempted_at) < PRICING_REFRESH_BACKOFF_SECONDS:
            outcome["reason"] = "throttled"
            return outcome
        try:
            payload = fetch_litellm_model_cost_map(source_url)
            written = write_litellm_pricing_snapshot(snapshot_path, payload, source_url=source_url, fetched_at=current_time)
        except Exception as exc:
            outcome["reason"] = "error"
            outcome["error"] = f"{type(exc).__name__}: {exc}"
            _record_refresh_failure(snapshot_path, metadata, error=outcome["error"], attempted_at=current_time)
            return outcome
        outcome["refreshed"] = True
        outcome["reason"] = "refreshed"
        outcome["metadata"] = written
        return outcome
    except Exception as exc:  # belt-and-braces: auto-refresh is best-effort
        outcome["reason"] = outcome.get("reason") or "error"
        outcome["error"] = f"{type(exc).__name__}: {exc}"
        return outcome
