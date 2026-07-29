"""AGENTACCT_* / AGENT_CHRONICLE_* / AGENT_SENTINEL_* env-variable aliasing.

The product renamed from Agent Sentinel to Agent Chronicle, then again to
agentacct, but live installs (shell profiles, launchd plists, CI, hook
settings) still export the older ``AGENT_CHRONICLE_*`` and ``AGENT_SENTINEL_*``
names. Every environment read therefore goes through :func:`read_env_alias`:
the new ``AGENTACCT_*`` primary name wins, then the ``AGENT_CHRONICLE_*``
alias, then the oldest ``AGENT_SENTINEL_*`` alias. All three are accepted
silently forever — no deprecation nagging, by decision (Phase 4).

Only READS are aliased. Writers (child-env exports, pricing-catalog
activation) write the new ``AGENTACCT_*`` primary name AND additionally
re-export the legacy ``AGENT_CHRONICLE_*`` / ``AGENT_SENTINEL_*`` names,
because existing user scripts read the old names.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

NEW_ENV_PREFIX = "AGENTACCT_"
# Pre-rename prefixes, newest-first. BOTH stay recognized on reads forever:
# existing shell profiles / launchd plists / CI / hook settings export them.
LEGACY_ENV_PREFIXES = ("AGENT_CHRONICLE_", "AGENT_SENTINEL_")


def env_alias_names(new_name: str) -> tuple[str, ...]:
    """All recognized names for one logical variable, in read/precedence order.

    ``(AGENTACCT_<X>, AGENT_CHRONICLE_<X>, AGENT_SENTINEL_<X>)`` — the new
    primary first, then each pre-rename alias newest-first.
    """
    if not new_name.startswith(NEW_ENV_PREFIX):
        raise ValueError(f"not an {NEW_ENV_PREFIX}* variable name: {new_name}")
    suffix = new_name[len(NEW_ENV_PREFIX) :]
    return (new_name, *(prefix + suffix for prefix in LEGACY_ENV_PREFIXES))


def legacy_env_names(new_name: str) -> tuple[str, ...]:
    """The pre-rename aliases of an ``AGENTACCT_*`` variable, newest-first."""
    return env_alias_names(new_name)[1:]


def legacy_env_name(new_name: str) -> str:
    """The most-recent pre-rename alias (``AGENT_CHRONICLE_*``) of a variable.

    Retained for callers that need a single representative legacy name (store
    resolution's conflict messages, the test suite's store isolation). Use
    :func:`legacy_env_names` when every alias matters.
    """
    return legacy_env_names(new_name)[0]


def read_env_alias(new_name: str, env: Mapping[str, str] | None = None) -> str | None:
    """Read ``new_name``, falling back to its pre-rename aliases.

    The new ``AGENTACCT_*`` name wins, then ``AGENT_CHRONICLE_*``, then
    ``AGENT_SENTINEL_*``; empty/whitespace-only values are treated as unset
    (matching every existing call site's truthiness checks).
    """
    environment: Mapping[str, str] = os.environ if env is None else env
    for name in env_alias_names(new_name):
        value = environment.get(name)
        if value is not None and value.strip():
            return value
    return None
