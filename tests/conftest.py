"""Suite-wide store-safety net (Batch 4).

Two protections against tests touching a REAL Agent Chronicle ledger:

1. An autouse function-scoped fixture points ``AGENT_CHRONICLE_STORE_DIR`` at a
   per-test temporary directory. pytest's cwd is inside the development repo —
   often a ``.claude/worktrees/<name>`` worktree whose store resolution remaps
   to the OWNING repository — so any command or service constructed without an
   explicit store would otherwise walk up to the real dogfood store.
   Tests that exercise resolver precedence themselves (env var, project
   walk-up, explicit failure) opt out with
   ``monkeypatch.delenv("AGENT_CHRONICLE_STORE_DIR", raising=False)`` plus
   ``monkeypatch.chdir(tmp_path)``.

2. An autouse session-scoped tripwire hashes the reachable real store file(s)
   at session start and asserts the bytes are unchanged at session end.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from agentacct.store_resolution import ENV_STORE_DIR, LEGACY_ENV_STORE_DIR

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parent


def _real_store_files() -> list[Path]:
    """Real ledger + hook-context files a stray test could reach from this checkout.

    Guards both this repo root's store and — when the suite runs inside a
    ``.claude/worktrees/<name>`` worktree — the owning repository's store,
    which the resolver's worktree remap makes reachable. Beyond events.jsonl
    this also hashes the store's client-context files (legacy single slot AND
    per-session dir): hook capture does not go through the env-isolated
    resolver, so a stray hook-path test would corrupt live attribution state
    without ever touching the ledger file.
    """
    candidates = [_REPO_ROOT]
    marker = f"{os.sep}.claude{os.sep}worktrees{os.sep}"
    repo_text = str(_REPO_ROOT)
    if marker in repo_text:
        owner = repo_text.split(marker, 1)[0]
        if owner:
            candidates.append(Path(owner))
    files: list[Path] = []
    for root in candidates:
        state = root / ".agent-sentinel" / "state"
        events = state / "events.jsonl"
        if events.is_file():
            files.append(events)
        context_root = state / "client-context"
        if context_root.is_dir():
            files.extend(sorted(path for path in context_root.rglob("*.json") if path.is_file()))
    return files


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def _isolated_default_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test's store resolution to a private temp directory.

    Isolation is bound to the pre-rename ``AGENT_CHRONICLE_STORE_DIR`` alias
    (still a fully recognized store-dir name). The suite's many opt-out sites
    clear that exact name, and its many override sites re-set it, so keeping
    the isolation on that one alias preserves the whole suite's single
    store-env design across the ``AGENTACCT_*`` primary rename. The new-primary
    and oldest aliases are cleared so an ambient developer export can neither
    leak in nor trip the three-name conflict-refuse check.
    """
    monkeypatch.delenv(ENV_STORE_DIR, raising=False)  # AGENTACCT_STORE_DIR (new primary)
    monkeypatch.delenv("AGENT_SENTINEL_STORE_DIR", raising=False)  # oldest alias
    monkeypatch.setenv(LEGACY_ENV_STORE_DIR, str(tmp_path / "isolated-default-store"))


@pytest.fixture(autouse=True)
def _disable_pricing_auto_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """No network from tests: the pricing snapshot's TTL auto-refresh would
    otherwise issue a real GET of the LiteLLM table from any test that runs
    an estimate-costs import against a store without a fresh snapshot.
    Tests that exercise the auto-refresh itself re-enable it with
    ``monkeypatch.setenv("AGENT_CHRONICLE_PRICING_AUTO_REFRESH", "1")`` and
    mock ``httpx.get``."""
    monkeypatch.setenv("AGENT_CHRONICLE_PRICING_AUTO_REFRESH", "0")


@pytest.fixture(autouse=True)
def _pin_canonical_live_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite deterministic under an ambient canonical-live flag.

    A developer shell exporting AGENT_CHRONICLE_CANONICAL_LIVE_WRITE=1 would
    otherwise flip every SentinelService in the suite into shadow-writing
    (and fail the flag-off byte-identity tests). Tests exercise the shadow
    through the constructor override or an explicit ``monkeypatch.setenv``.
    """

    monkeypatch.delenv("AGENTACCT_CANONICAL_LIVE_WRITE", raising=False)
    monkeypatch.delenv("AGENT_CHRONICLE_CANONICAL_LIVE_WRITE", raising=False)
    monkeypatch.delenv("AGENT_SENTINEL_CANONICAL_LIVE_WRITE", raising=False)


@pytest.fixture(autouse=True)
def _pin_canonical_read_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same determinism guard for the phase-4 canonical READ flag.

    An ambient AGENT_CHRONICLE_CANONICAL_READ export would flip every read
    surface in the suite onto the canonical path (and fail the flag-off
    byte-identity tests). Tests exercise the reader through the constructor
    override or an explicit ``monkeypatch.setenv``.
    """

    monkeypatch.delenv("AGENTACCT_CANONICAL_READ", raising=False)
    monkeypatch.delenv("AGENT_CHRONICLE_CANONICAL_READ", raising=False)
    monkeypatch.delenv("AGENT_SENTINEL_CANONICAL_READ", raising=False)


@pytest.fixture(autouse=True)
def _pin_event_log_authoritative_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test at the product default: the SQLite log is authoritative.

    ``AGENTACCT_EVENT_LOG_AUTHORITATIVE`` defaults to ON, so a fresh store is
    SQLite-only and an existing events.jsonl store auto-adopts the log. Clearing
    the var (and its pre-rename aliases) insulates the suite from a developer
    shell that exported ``=0``, which would otherwise silently flip every store
    back to flat-file mirror mode. Tests that specifically exercise mirror mode
    opt in with ``monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "0")``.
    """

    monkeypatch.delenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", raising=False)
    monkeypatch.delenv("AGENT_CHRONICLE_EVENT_LOG_AUTHORITATIVE", raising=False)
    monkeypatch.delenv("AGENT_SENTINEL_EVENT_LOG_AUTHORITATIVE", raising=False)


@pytest.fixture(autouse=True)
def _disable_global_provider_limit_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the machine-GLOBAL provider-limit file scan off for every test.

    The rate-limit foundation reads global provider files (the real ``~/.codex``
    when no codex_home is given, and the Claude desktop plan-usage history) as a
    side effect of a usage import. Hermetic tests must never touch the developer's
    real machine, so ``AGENTACCT_SCAN_GLOBAL_LIMITS`` is pinned off here (an
    explicit codex_home is still scanned — that is a local, isolated path). A test
    that specifically exercises the global scan opts in with
    ``monkeypatch.setenv("AGENTACCT_SCAN_GLOBAL_LIMITS", "1")``.
    """

    monkeypatch.setenv("AGENTACCT_SCAN_GLOBAL_LIMITS", "0")


@pytest.fixture(autouse=True)
def _disable_global_subagent_role_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the machine-GLOBAL subagent-role transcript scan off for every test.

    The TUI reads Claude subagent transcripts (``~/.claude/projects/.../subagents``)
    to show a child session's role/task. Hermetic tests must never read the
    developer's real machine, so ``AGENTACCT_SCAN_SUBAGENT_ROLES`` is pinned off
    (a test that needs roles passes an explicit ``projects_root`` / sets
    ``AGENTACCT_CLAUDE_PROJECTS_ROOT`` at a tmp dir). Also clear any real root
    override that might leak in from the environment.
    """

    monkeypatch.setenv("AGENTACCT_SCAN_SUBAGENT_ROLES", "0")
    monkeypatch.delenv("AGENTACCT_CLAUDE_PROJECTS_ROOT", raising=False)


@pytest.fixture(autouse=True)
def _allow_test_client_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Starlette TestClient's default ``Host: testserver`` working.

    The shipped ``DEFAULT_ALLOWED_HOSTS`` no longer carries the test-only
    ``testserver`` hostname (it has no place in a production allowlist). The
    suite re-injects it through the SAME ``extra_allowed_hosts`` seam real
    deployments use for ``--allow-host``, so every TestClient call site keeps
    passing without the prod default trusting a test hostname. Both app
    factories reference ``install_localhost_guard`` as a module global, so
    patching those two references covers create_local_api_app and
    proxy.create_app (including the apps the CLI serve commands build).
    """
    import agentacct.api as api_module
    import agentacct.proxy as proxy_module
    from agentacct import localhost_guard

    real_install = localhost_guard.install_localhost_guard

    def _install_with_testserver(app, extra_allowed_hosts=()):
        real_install(app, (*tuple(extra_allowed_hosts), "testserver"))

    monkeypatch.setattr(api_module, "install_localhost_guard", _install_with_testserver)
    monkeypatch.setattr(proxy_module, "install_localhost_guard", _install_with_testserver)


@pytest.fixture(autouse=True, scope="session")
def _real_store_tripwire():
    """Fail the session if ANY test mutated a real dogfood ledger."""
    before = {path: _digest(path) for path in _real_store_files()}
    yield
    changed = [
        path
        for path, digest in before.items()
        if (not path.is_file()) or _digest(path) != digest
    ]
    assert not changed, (
        "REAL Agent Chronicle store modified during the test session: "
        + ", ".join(str(path) for path in changed)
        + ". The dogfood ledger is read-only for tests. Find the test that "
        "resolved a real store (missing --store-dir / env isolation). "
        "Note: a concurrent live agent session writing usage events would "
        "also trip this check — re-run the suite to distinguish."
    )
