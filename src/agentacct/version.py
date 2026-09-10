"""Runtime package-version identity shared by public agentacct surfaces."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version


def package_version() -> str:
    """Return the installed agentacct version, or a bare-checkout sentinel.

    Release-facing metadata must use the installed distribution identity.  The
    ingestion watcher intentionally uses a separate build id that adds a source
    fingerprint, because source changes can matter there without a release.
    """

    try:
        return _distribution_version("agentacct")
    except PackageNotFoundError:
        return "0.0.0+source"
