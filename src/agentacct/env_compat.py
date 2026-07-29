"""AGENT_CHRONICLE_* / AGENT_SENTINEL_* environment-variable aliasing.

The product renamed from Agent Sentinel to Agent Chronicle, but live installs
(shell profiles, launchd plists, CI, hook settings) still export the old
``AGENT_SENTINEL_*`` names. Every environment read therefore goes through
:func:`read_env_alias`: the new ``AGENT_CHRONICLE_*`` name wins when both are
set, and the old name is accepted silently forever — no deprecation nagging,
by decision (Phase 4).

Only READS are aliased. Writers (child-env exports, pricing-catalog
activation) write the new name; ``runner.py`` additionally exports the legacy
run-id/run-dir names into child environments because existing user scripts
read them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

NEW_ENV_PREFIX = "AGENT_CHRONICLE_"
LEGACY_ENV_PREFIX = "AGENT_SENTINEL_"


def legacy_env_name(new_name: str) -> str:
    """The pre-rename alias of an ``AGENT_CHRONICLE_*`` variable name."""
    if not new_name.startswith(NEW_ENV_PREFIX):
        raise ValueError(f"not an {NEW_ENV_PREFIX}* variable name: {new_name}")
    return LEGACY_ENV_PREFIX + new_name[len(NEW_ENV_PREFIX) :]


def env_alias_names(new_name: str) -> tuple[str, str]:
    """(new, legacy) lookup order for one logical variable."""
    return (new_name, legacy_env_name(new_name))


def read_env_alias(new_name: str, env: Mapping[str, str] | None = None) -> str | None:
    """Read ``new_name``, falling back to its ``AGENT_SENTINEL_*`` alias.

    The new name wins when both are set; empty/whitespace-only values are
    treated as unset (matching every existing call site's truthiness checks).
    """
    environment: Mapping[str, str] = os.environ if env is None else env
    for name in env_alias_names(new_name):
        value = environment.get(name)
        if value is not None and value.strip():
            return value
    return None
