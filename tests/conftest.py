"""Suite-wide store-safety net (Batch 4) and render determinism.

Two protections against tests touching a REAL Agent Chronicle ledger, plus a
third fixture (``_deterministic_render_width``) that pins the width CLI tables
render at, so a test's verdict never depends on the terminal that ran it:

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
def _disable_tui_auto_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the TUI's launch/refresh usage import off for every test.

    The TUI freshens the store from the client session files (~/.claude, ~/.codex,
    …) on launch/`r` — a hermetic test must never scan the developer's real logs.
    A test exercising the import opts in with
    ``monkeypatch.setenv("AGENTACCT_TUI_AUTO_IMPORT", "1")`` and monkeypatches the
    importer so it writes only to its tmp store.
    """

    monkeypatch.setenv("AGENTACCT_TUI_AUTO_IMPORT", "0")


@pytest.fixture(autouse=True)
def _allow_test_client_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Starlette TestClient's default ``Host: testserver`` working.

    The shipped ``DEFAULT_ALLOWED_HOSTS`` no longer carries the test-only
    ``testserver`` hostname (it has no place in a production allowlist). The
    suite re-injects it through the SAME ``extra_allowed_hosts`` seam real
    deployments use for ``--allow-host``, so every TestClient call site keeps
    passing without the prod default trusting a test hostname. The app
    factory references ``install_localhost_guard`` as a module global, so
    patching that reference covers create_local_api_app (including the apps
    the CLI serve commands build).
    """
    import agentacct.api as api_module
    from agentacct import localhost_guard

    real_install = localhost_guard.install_localhost_guard

    def _install_with_testserver(app, extra_allowed_hosts=()):
        real_install(app, (*tuple(extra_allowed_hosts), "testserver"))

    monkeypatch.setattr(api_module, "install_localhost_guard", _install_with_testserver)


@pytest.fixture(autouse=True)
def _deterministic_render_width():
    """Render CLI tables at a fixed 80 columns, whatever terminal ran pytest.

    Without this, the suite's verdict depends on the window size of whoever ran
    it. rich reads ``COLUMNS`` once inside ``Console.__init__`` and freezes it in
    ``_width``; ``agentacct.cli.console`` is built at import, so the launching
    terminal's width is baked in for the whole session and no amount of
    ``monkeypatch.setenv("COLUMNS", ...)`` in a test body can move it.

    That is not hypothetical. A header assertion in ``test_now_command.py``
    passed on every wide developer terminal and failed only in CI, because at 80
    columns two adjacent table headers wrapped and both tails read ``tokens``.
    It was not an isolated case: before this fixture, running the suite at 60
    columns failed fifteen tests across six files, and at 40 it failed nineteen.
    With it, 40, 80 and 200 all return the same verdict.

    80 rather than something roomy on purpose: it is the standard narrow
    terminal and the width CI uses, so it keeps the layout under real pressure.
    Pinning wide would have hidden the very defect that prompted this fixture.
    ``_width`` rather than the public ``width`` setter so teardown restores
    ``None`` as ``None`` instead of freezing a concrete number.

    Typer's own help and usage-error output does not go through that console; it
    builds its own from ``typer.rich_utils.MAX_WIDTH``, which is likewise read
    from the environment at import. It is referenced as a module global at call
    time, though, so unlike the console it can simply be reassigned.
    """
    from typer import rich_utils

    from agentacct import cli as cli_module

    console = cli_module.console
    previous_width = console._width
    previous_max = rich_utils.MAX_WIDTH
    console._width = 80
    rich_utils.MAX_WIDTH = 80
    try:
        yield
    finally:
        console._width = previous_width
        rich_utils.MAX_WIDTH = previous_max


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
