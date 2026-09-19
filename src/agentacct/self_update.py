"""Version-check and self-update for the packaged (uv-tool) install.

The daemon should never silently drift onto an old release again. This module
answers two questions cheaply and honestly:

* is a newer agentacct published on PyPI than the one running now?
* is this a packaged install we may update in place, or a developer/editable
  checkout we must refuse to touch?

The version check reads a short-TTL sidecar cache (``<store>/cache/``) and only
hits the network on a background thread, so request handlers never block. The
apply step shells ``uv tool install agentacct==<latest> --force --no-cache`` and
is refused outright for a dev/editable checkout (defense in depth alongside the
API/CLI guards). Nothing here reads or writes the event ledger or the evidence
store — self-update is an operational action, not recorded work.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import version as _version

PYPI_JSON_URL = "https://pypi.org/pypi/agentacct/json"
VERSION_CACHE_TTL_SECONDS = 6 * 3600
_NETWORK_TIMEOUT_SECONDS = 2.0
_SIDECAR_RELATIVE = ("cache", "pypi-version.json")


@dataclass(frozen=True)
class UpdateStatus:
    current: str
    latest: str | None
    update_available: bool
    is_dev_install: bool
    checked_at: float | None
    source: str  # "pypi" | "cache" | "offline"

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "latest": self.latest,
            "update_available": self.update_available,
            "is_dev_install": self.is_dev_install,
            "checked_at": self.checked_at,
            "source": self.source,
        }


def _version_tuple(value: str) -> tuple[int, ...]:
    """Parse the release segment of a version into a comparable int tuple.

    Local/build metadata (``+source``, ``+ecc9d1``) and pre-release suffixes are
    dropped: we only compare the public ``X.Y.Z`` release, which is all PyPI
    exposes as ``info.version``. Non-numeric or empty input sorts lowest.
    """

    release = value.strip().split("+", 1)[0].split("-", 1)[0]
    parts: list[int] = []
    for chunk in release.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _is_newer(latest: str, current: str) -> bool:
    lt = _version_tuple(latest)
    ct = _version_tuple(current)
    return bool(lt) and lt > ct


def is_dev_install() -> bool:
    """True when agentacct runs from a developer/editable checkout, which must
    never be replaced by ``uv tool install``.

    Three independent signals (any one is enough): the bare-checkout version
    sentinel, an editable ``direct_url.json`` marker, or an import path that is
    not under a ``site-packages`` / uv ``tools`` directory (i.e. a repo ``src``).
    """

    if _version.package_version() == "0.0.0+source":
        return True
    try:
        from importlib import metadata as importlib_metadata

        direct_url = importlib_metadata.distribution("agentacct").read_text("direct_url.json")
        if direct_url:
            info = json.loads(direct_url)
            if isinstance(info, dict) and bool((info.get("dir_info") or {}).get("editable")):
                return True
    except Exception:
        pass
    try:
        import agentacct

        module_file = Path(agentacct.__file__).resolve()
    except Exception:
        return False
    segments = {segment.lower() for segment in module_file.parts}
    if "site-packages" in segments or "dist-packages" in segments:
        return False
    # uv installs tools under .../uv/tools/<name>/... — treat that as packaged.
    if "tools" in segments and "uv" in segments:
        return False
    return True


def query_pypi_latest(*, timeout: float = _NETWORK_TIMEOUT_SECONDS) -> str | None:
    """The latest published version from PyPI, or None on any failure.

    Never raises: a version check must degrade to "unknown", not break the
    caller. Sends nothing about this machine — a plain GET of a public file.
    """

    try:
        import httpx

        response = httpx.get(PYPI_JSON_URL, follow_redirects=True, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        latest = payload["info"]["version"]
        return str(latest) if latest else None
    except Exception:
        return None


def _sidecar_path(store_dir: Path | str) -> Path:
    return Path(store_dir).expanduser().joinpath(*_SIDECAR_RELATIVE)


def _read_sidecar(path: Path) -> tuple[str | None, float | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    latest = payload.get("latest")
    fetched_at = payload.get("fetched_at")
    latest_str = str(latest) if isinstance(latest, str) and latest else None
    fetched = float(fetched_at) if isinstance(fetched_at, (int, float)) and not isinstance(fetched_at, bool) else None
    return latest_str, fetched


def _write_sidecar(path: Path, latest: str | None, fetched_at: float) -> None:
    """Atomic 0600 write of the version cache; best-effort (never raises)."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".pypi-version-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"latest": latest, "fetched_at": fetched_at}, handle, sort_keys=True)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
    except OSError:
        pass


def update_status(*, store_dir: Path | str, allow_network: bool = True) -> UpdateStatus:
    """Current vs latest, reading the TTL sidecar and only hitting the network
    when it is stale and ``allow_network`` is set.

    ``allow_network=False`` (the request-handler path) is guaranteed non-blocking:
    it answers from the cache alone. A dev/editable install never reports an
    available update, whatever PyPI says.
    """

    current = _version.package_version()
    dev = is_dev_install()
    sidecar = _sidecar_path(store_dir)
    cached_latest, fetched_at = _read_sidecar(sidecar)

    now = time.time()
    fresh = fetched_at is not None and (now - fetched_at) < VERSION_CACHE_TTL_SECONDS and fetched_at <= now + 60

    latest = cached_latest
    source = "cache" if cached_latest is not None else "offline"
    checked_at = fetched_at

    if allow_network and not fresh:
        fetched = query_pypi_latest()
        if fetched is not None:
            latest = fetched
            source = "pypi"
            checked_at = now
            _write_sidecar(sidecar, latest, now)

    update_available = bool(latest) and not dev and _is_newer(latest, current)
    return UpdateStatus(
        current=current,
        latest=latest,
        update_available=update_available,
        is_dev_install=dev,
        checked_at=checked_at,
        source=source,
    )


def refresh_in_background(store_dir: Path | str) -> None:
    """Kick a best-effort background version refresh so the next request's
    cache-only read is fresh. Never blocks; swallows every error."""

    def _run() -> None:
        try:
            update_status(store_dir=store_dir, allow_network=True)
        except Exception:
            pass

    try:
        threading.Thread(target=_run, name="agentacct-version-refresh", daemon=True).start()
    except Exception:
        pass


def apply_update(latest: str) -> dict[str, Any]:
    """Install the target version with uv. Refuses a dev/editable checkout.

    Returns {ok, from, to} on success; raises RuntimeError on a dev install or a
    failed install (the caller decides how to surface it).
    """

    if is_dev_install():
        raise RuntimeError("refusing to self-update a development/editable checkout")
    current = _version.package_version()
    argv = ["uv", "tool", "install", f"agentacct=={latest}", "--force", "--no-cache"]
    completed = subprocess.run(argv, check=True, capture_output=True, text=True)
    return {
        "ok": True,
        "from": current,
        "to": latest,
        "stdout": (completed.stdout or "")[-2000:],
    }


def apply_update_argv(latest: str) -> list[str]:
    """The exact uv argv apply_update runs (exposed for tests / logging)."""

    return ["uv", "tool", "install", f"agentacct=={latest}", "--force", "--no-cache"]


def restart_updater_argv(store_dir: Path | str) -> list[str]:
    """Argv for the detached updater the API route spawns."""

    return [sys.executable, "-m", "agentacct", "self-update", "--yes", "--store-dir", str(store_dir)]


__all__ = [
    "PYPI_JSON_URL",
    "VERSION_CACHE_TTL_SECONDS",
    "UpdateStatus",
    "apply_update",
    "apply_update_argv",
    "is_dev_install",
    "query_pypi_latest",
    "refresh_in_background",
    "restart_updater_argv",
    "update_status",
]
