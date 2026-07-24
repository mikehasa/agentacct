"""Shared platform-support guard for every installed console entry point.

The product is POSIX-only: the import/run chain reaches ``fcntl`` (service.py)
and POSIX process groups/signals (runner.py), so on native Windows an entry
point would otherwise die with a traceback before doing anything useful. This
guard fails fast with one actionable sentence instead. It lives in its own
lightweight module (imports only ``sys``) so both ``agent_chronicle.cli`` and
``agent_chronicle.wrappers`` can call it without importing each other.
"""

from __future__ import annotations

import sys


def exit_if_unsupported_platform(platform: str) -> None:
    """Fail fast with one actionable sentence on platforms the product cannot run on.

    Takes the platform string as an argument so the check is unit-testable;
    callers pass the real ``sys.platform``.
    """
    if platform != "win32":
        return
    print(
        "agentacct requires macOS or Linux (POSIX). On Windows, run it inside WSL: https://learn.microsoft.com/windows/wsl/install",
        file=sys.stderr,
    )
    raise SystemExit(2)
