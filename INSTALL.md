# agentacct — install runbook for coding agents

This file is written for the CODING AGENT performing the install (Claude Code, Codex, Hermes, OpenCode, OpenClaw, or any MCP-capable agent). agentacct is public: install it with `pipx install agentacct` (or straight from the public repository with `pipx install git+https://github.com/mikehasa/agentacct`). The fast path is `agentacct onboard`, which installs agentacct once per machine (global by default, zero repo files) — see Step 2. The per-client sections (Step 3) are the manual, per-project equivalent; run them from the target repository's root for the client you are running inside.

<!-- Consistency contract: the command blocks, notes, and capability matrix below
     are defined in src/agentacct/install_guide.py and embedded here verbatim.
     tests/test_install_guide.py fails if this file drifts from the module.
     Edit the module first, then mirror the change here. -->

## What you are installing

agentacct is local-first Agent Work Intelligence for coding agents:
- a usage ledger of client-reported tokens, imported from the coding agent's own local session files;
- an MCP work context: sections and events the agent records while working, joined to usage with honest confidence labels;
- a local JSON API (`agentacct serve`, http://127.0.0.1:8765) for scripts and native shells, and `agentacct tui` — the live terminal dashboard.

Hard rules for this install:

- Observe-only. NEVER store, request, or echo provider API keys — nothing in this install needs one.
- No telemetry. All state stays local to this machine: `agentacct onboard` uses one global store by default; with `--scope project` it uses a per-repo store under `.agent-sentinel/` (gitignored; the directory keeps the pre-rename name `.agent-sentinel/` for compatibility with existing stores). Either way, only the client config files each path writes change.
- Do not modify global/profile client configuration without showing the exact command and asking first.
- If `agentacct` is not on PATH, run it via its durable absolute path — never a throwaway temp venv. MCP config writes embed the absolute executable automatically.

## Step 1 — install the CLI

Skip this step if `agentacct` already runs. Preferred (isolated, on PATH):

```bash
pipx install agentacct
```

or:

```bash
uv tool install agentacct
```

To install the latest source straight from the public repository (before or without a PyPI release):

```bash
pipx install git+https://github.com/mikehasa/agentacct
```

Fallback without pipx/uv — install into a dedicated venv:

```bash
python3 -m venv "$HOME/.agentacct"
"$HOME/.agentacct/bin/python" -m pip install agentacct
```

Verify:

```bash
command -v agentacct
```

If `command -v` prints nothing, use the absolute path for every command below: pipx installs to `$HOME/.local/bin/agentacct`; the dedicated-venv fallback is `$HOME/.agentacct/bin/agentacct`. Requires Python >= 3.11 on macOS or Linux (Windows via WSL).

## Step 2 — onboard (install once, global by default)

Run:

```bash
agentacct onboard
agentacct status
```

`onboard` installs agentacct ONCE per machine and writes ZERO files into any repo: it registers user-scope MCP, instruction, and supported client-specific hook/plugin surfaces against one global store (default `~/.local/state/agentacct/state`; older `~/.agent-sentinel-global/state` stores are still recognized), performs one real local usage sync, and starts continuous sync plus the local JSON API runtime. It merges the Claude Code hook and `ENABLE_TOOL_SEARCH` env into `~/.claude/settings.json` only with your ok (pass `--yes` for a non-interactive run); Codex and Hermes still require their client-native one-time hook trust/consent steps. To select a client explicitly, use `--agent codex`, `--agent claude-code`, `--agent hermes`, or `--agent opencode`. For the legacy per-repo install, add `--scope project`: it changes project-local files only and gives this repo its own `.agent-sentinel/` store (see Step 3 and "Global install by hand" below).

The command finishing successfully means agentacct is configured; it does not mean a real Task has appeared. **Open a NEW agent session (in ANY repo) after onboarding.** MCP servers and hooks bind when the client session starts, so the session that ran onboarding cannot see the newly registered tools. agentacct stays in an honest waiting state until a real recognized client session appears; a demo row does not count.

The managed local runtime survives the invoking shell and uses one store. Its lifecycle is:

```bash
agentacct start
agentacct status
agentacct stop
agentacct repair
```

- `start` is idempotent and starts continuous local usage sync plus the localhost JSON API.
- `status` reports API, sync, and ingestion readiness.
- `stop` signals only processes whose complete agentacct ownership proof still matches.
- `repair` clears dead or corrupt owned runtime state without adopting or killing an unknown process.

The local JSON API listens at http://127.0.0.1:8765 by default (the TUI reads the store directly and does not need it). Everything stays local to this machine (the global store by default, or a per-repo `.agent-sentinel/` store under `--scope project`); onboarding neither requests provider API keys nor uploads session data.

## Migration notes (formerly Agent Sentinel)

agentacct was formerly Agent Sentinel; pre-rename installs keep working without re-setup:

- Environment variables: every `AGENTACCT_*` variable also accepts its pre-rename `AGENT_CHRONICLE_*` and `AGENT_SENTINEL_*` names — old names are accepted indefinitely, the new name wins when more than one is set, and conflicting store-dir values refuse instead of silently splitting the ledger. There is no runtime deprecation nag; the old names simply keep working.
- Binaries: the published package ships only the `agentacct` console script. Pre-rename MCP registrations, hook wrappers, and launchd jobs that resolve the old `agent-chronicle` / `agent-sentinel` binary names are still recognized at runtime, but the old console scripts are no longer shipped (they collide with unrelated PyPI packages of those names).
- Pre-rename `agent-sentinel` MCP registrations and `agent-sentinel:begin` instruction blocks stay recognized; stored data and the `.agent-sentinel/` store directories keep their pre-rename spellings forever (data format, not branding).
- Caveat — foreign PyPI package collision: an unrelated PyPI project named `agent-sentinel` (0.5.0) also installs a `bin/agent-sentinel` script. If both packages land in the SAME environment, the later install silently overwrites that script (pip prints no warning), and `pip uninstall agent-sentinel` (the foreign package) deletes the alias out from under agentacct — pre-rename registrations and wrappers resolving the old binary name then fail until `pip install --force-reinstall agentacct`. Do not install both in one environment; pipx refuses the second install because the app names collide.

## Step 3 — advanced: manual per-project client setup

Use this per-repo (`--scope project`) path when client auto-detection is unavailable, when you want a repo to keep its own store, or when you need to configure integrations separately. Run everything from the target repository root.

### Claude Code

```bash
agentacct init --agent claude-code --write-mcp
agentacct hooks claude-code install
# REQUIRED (attribution keystone): activate the hook bridge — merge BOTH the
# "hooks" and "env" blocks from .claude/settings.agent-sentinel.example.json
# into .claude/settings.local.json (create that file with the example's exact
# contents if it does not exist; merge, never overwrite existing hooks or env
# keys).
agentacct hooks claude-code doctor
agentacct mcp doctor
agentacct usage import-local --client all --dry-run
agentacct serve
```

- The hook bridge is REQUIRED here: it captures real session/transcript ids at session start (SessionStart) and on every tool call (PreToolUse) — the only path to exact/high-confidence joins between usage and recorded work — and its SessionStart response injects the record-your-work directive into every new session (delivery only: without the `env` block below, delivered directives still record nothing).
- Recorded work context needs all three levers of the merged settings, each covering a different failure: the SessionStart hook entry is delivery (without it the directive never reaches the session), `"env": {"ENABLE_TOOL_SEARCH": "auto"}` makes the agentacct MCP tools arrive directly callable (without it they stay deferred and un-primed sessions demonstrably record nothing), and the hook bridge itself supplies the join ids (without it recorded work is never session-linked).
- The example settings embed machine-local absolute paths, so `.claude/settings.local.json` (machine-local, not committed) is the right target.
- REQUIRED final step: start a NEW Claude Code session (or run `/mcp` to reconnect) after setup — MCP servers and hooks bind at session start, so the session that performed the install cannot see the agentacct tools and records nothing.
- `mcp doctor` saying `hook context: absent` right after install is expected — the context appears once the first hooked Claude Code session starts or runs a tool.
- Re-running `hooks claude-code install` over existing hook files requires `--force` (do re-run it after upgrades: the wrapper is regenerated in place and its filename never changes, so existing settings keep working).

### Codex

```bash
agentacct init --agent codex --write-mcp
agentacct doctor
agentacct mcp doctor
agentacct usage import-local --client all --dry-run
agentacct serve
```

- `init --write-mcp` writes the project `.codex/config.toml` MCP block with an absolute store path.
- Project-scope onboarding does not install the Codex hook. Default global onboarding does: it writes the user-scope MCP block and standing instructions plus the client-specific v1 PreToolUse/SessionEnd hook, which starts only after a new Codex session grants one-time hook trust. This installed v1 bridge is separate from the generic Evidence v2 Codex manifest, which remains render-only/manual.
- REQUIRED final step: start a NEW Codex session in this repo after install — Codex binds MCP servers at session start, so the agentacct tools never appear in the session that performed the install, and nothing is recorded until the first fresh session.
- This project's recorded work context appears when `agentacct tui` or `agentacct serve` uses THIS project's store; views backed by other project stores show these sessions as usage-only, labeled with this project's name.
- Codex cannot pass its own session id in-session; at usage import, agentacct pairs each agentacct-recorded event with the Codex session log that created it (client-log evidence), so recorded work earns high-confidence session links once the session's rollout is imported — never `exact`, and a section evidenced by more than one session links to none. The client name is implied the same way, so codex agents need not pass `client` explicitly (a wrong explicit name is conflict-vetoed, never trusted).
- Register the server as `agentacct` (every agentacct setup path writes that name). Pre-rename `agent-sentinel` registrations are equally recognized forever — existing installs do not need to re-register. Any OTHER custom registration name loses session links — skipped-but-counted in work insights, never guessed.

### Hermes / OpenCode / OpenClaw / other MCP clients

```bash
agentacct init --agent hermes
agentacct setup mcp --agent hermes
agentacct mcp doctor
agentacct usage import-local --client all --dry-run
agentacct serve
```

Replace `hermes` with `opencode` or `openclaw` for those clients.

- This section is the legacy project-scope path. These clients keep MCP config in profile/global or client-specific locations, so project `setup mcp` PREVIEWS the exact registration command instead of writing it. Default global onboarding is different: `onboard --agent hermes` writes the Hermes MCP profile plus its observe-only v1 shell hooks (one-time consent and gateway restart still required), while `onboard --agent opencode` writes OpenCode MCP config, global rules, and its observe-only v1 plugin. Neither client has a generic Evidence v2 manifest adapter.
- First remove any stale pre-rename server, e.g. `opencode mcp remove agent-sentinel` (also `agent-chronicle`): only `agentacct` ships now, so a leftover old-name entry launches a command that no longer exists (ENOENT), which the client reports as a crashed MCP server.
- Show the previewed command to the user and ask before modifying global agent configuration, then paste it into that client's own MCP setup flow.
- For an unlisted MCP-capable client, use `--agent generic` to get a portable stdio server definition.

Every section ends the same way: `agentacct serve` is long-running: start it in the background or hand the command to the user; local JSON API at http://127.0.0.1:8765 (the HTML dashboard is retired — `agentacct tui` is the interactive surface). The dry-run import only previews; drop `--dry-run` when the user wants the ledger populated — API reads never write.

## What the Task view shows

agentacct groups recognized root client sessions into stable Tasks. Open a Task in the macOS app (or `agentacct tui`), or fetch it from the local JSON API by its opaque public id:

```text
GET http://127.0.0.1:8765/v1/receipt?task=task_<opaque-id>
GET http://127.0.0.1:8765/v1/tasks
```

The Task view is a decision brief, not a raw event dump: it summarizes what was attempted, the current outcome, strongest proof, usage/cost basis, unresolved findings, and a next action only when an owner was explicitly recorded. A bounded evidence timeline remains available underneath.

Task state has three independent axes: **execution** (queued/running/finished/cancelled/lost), **outcome** (unknown/reported/verified/blocked/open finding), and **control** (ready/awaiting approval/policy hold/control failure). A failed target-product check is an outcome finding; it is not automatically an agentacct failure or a user action.

## Optional — govern an agentacct-owned local attempt

agentacct's control surface (the `agentacct control ...` commands) governs only processes agentacct launches itself. Existing Codex, Claude Code, and other orchestrator processes remain observed-only.

Register an existing workspace and a fixed argv array (never a shell command string):

```bash
agentacct control register-workspace \
  --store-dir .agent-sentinel/state --root . --workspace-id project

agentacct control register-agent \
  --store-dir .agent-sentinel/state \
  --agent-id local-agent --display-name "Local agent" \
  --argv-json '["/absolute/command","arg"]'
```

Then use the control CLI: `agentacct control create-task` creates a pending Task Contract, `agentacct control list`/`status` review its workspace/adapter/checks, and `agentacct control launch` explicitly starts it (state is also readable as JSON at `GET /api/control`). Contract creation never starts a process. Workspace-write attempts require an expiring, single-use approval (`control request-approval` / `control decide-approval`) before the supervisor will launch them.

The same flow is available from the CLI:

```bash
agentacct control plan \
  --store-dir .agent-sentinel/state \
  --objective "Run the bounded local task" \
  --workspace-id project --agent-id local-agent \
  --permission-envelope-json '{"mutation_mode":"read_only"}' \
  --success-check "the owned process exits successfully"

agentacct control status --store-dir .agent-sentinel/state
agentacct control launch --help
```

`control launch` stays in the foreground until the attempt is terminal; the long-running daemon owns the persistent supervisor. An attempt freezes the current registered agent revision, and launch fails closed if that agent's argv changes after the attempt or approval was created; create a new attempt to authorize the new revision. Control status and product JSON are sanitized: they never return registered absolute paths, argv, PIDs/process groups, executable/cwd fingerprints, manifest ids, or ownership nonces.

## Global install by hand (single-user machine)

This is what `agentacct onboard` does for you by default: it installs agentacct ONCE per machine — one user-scope MCP server, hook, local API runtime, and ONE global store — so machine-wide usage AND machine-wide work context land in one ledger viewed through the macOS app, `agentacct tui`, or the JSON API, with ZERO files written into any repo. The default onboard store is the XDG state dir (`~/.local/state/agentacct/state`); older global stores (`~/.agent-sentinel-global/state`) are still recognized. The runbook below is the do-it-by-hand equivalent — use it when you want to wire the registrations yourself or point them at an explicit store. The per-agent sections above are the other path: `--scope project` installs per repository, giving each repo its own store and MCP registration.

```bash
AGENTACCT_BIN="$(command -v agentacct 2>/dev/null || true)"
case "$AGENTACCT_BIN" in
  /*) ;;
  *)
    if [ -x "$HOME/.local/bin/agentacct" ]; then
      AGENTACCT_BIN="$HOME/.local/bin/agentacct"
    elif [ -x "$HOME/.agentacct/bin/agentacct" ]; then
      AGENTACCT_BIN="$HOME/.agentacct/bin/agentacct"
    else
      echo "Could not resolve an absolute executable agentacct path; install with the steps above or substitute one manually." >&2
      exit 1
    fi
    ;;
esac
case "$AGENTACCT_BIN" in
  /*) ;;
  *) echo "Resolved agentacct path is not absolute: $AGENTACCT_BIN" >&2; exit 1 ;;
esac
[ -x "$AGENTACCT_BIN" ] || { echo "agentacct is not executable: $AGENTACCT_BIN" >&2; exit 1; }
AGENTACCT_GLOBAL_STORE="$("$AGENTACCT_BIN" setup global-store-path)"  # exact onboard resolver: override/XDG/populated legacy store
[ -n "$AGENTACCT_GLOBAL_STORE" ] || { echo "agentacct did not resolve a global store" >&2; exit 1; }
mkdir -p "$AGENTACCT_GLOBAL_STORE"
printf 'BIN=%s\nSTORE=%s\n' "$AGENTACCT_BIN" "$AGENTACCT_GLOBAL_STORE"  # save these concrete absolute values for later commands
# the two `mcp add` lines below rewrite USER-LEVEL client config (~/.claude.json, ~/.codex/config.toml): show them to the user and ask first (ground rule)
# NO `claude`/`codex` CLI on PATH? (common: the Claude Code desktop app and ChatGPT.app's Codex ship none) — skip the two `mcp add` lines and hand-write the same registration per the "no CLI" note below.
claude mcp add --scope user agentacct -- "$AGENTACCT_BIN" mcp serve --store-dir "$AGENTACCT_GLOBAL_STORE"
codex mcp add agentacct -- "$AGENTACCT_BIN" mcp serve --store-dir "$AGENTACCT_GLOBAL_STORE"
"$AGENTACCT_BIN" hooks claude-code install --project-dir "$HOME" --store-dir "$AGENTACCT_GLOBAL_STORE" --user-settings-example  # wrapper homes in ~/.claude/hooks/ (NOT the store dir): a store move must never vanish the hook and brick sessions
# merge the printed "hooks" AND "env" blocks into ~/.claude/settings.json (user-level; ask the user first; merge, never overwrite existing keys)
"$AGENTACCT_BIN" hooks codex install --store-dir "$AGENTACCT_GLOBAL_STORE"  # installs the client-specific v1 PreToolUse + SessionEnd bridge; approve Codex's one-time hook trust prompt in a NEW session
# standing "record your work" instructions — this is what fills the work ledger with context (writes ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md; ask the user first, --dry-run to preview)
"$AGENTACCT_BIN" setup instructions --agent claude-code --user --store-dir "$AGENTACCT_GLOBAL_STORE"
"$AGENTACCT_BIN" setup instructions --agent codex --user --store-dir "$AGENTACCT_GLOBAL_STORE"
"$AGENTACCT_BIN" serve --store-dir "$AGENTACCT_GLOBAL_STORE"
```

- No CLI? The `claude mcp add` / `codex mcp add` lines need the `claude` and `codex` CLIs on PATH — but the Claude Code desktop app and ChatGPT.app's Codex read their config files directly and often ship NO such CLI, so those two lines fail with `command not found`. Register by hand instead, using the concrete absolute `BIN` and `STORE` values printed by the block: (1) Codex — add to `~/.codex/config.toml` a table `[mcp_servers.agentacct]` with `command = "BIN"` and `args = ["mcp", "serve", "--store-dir", "STORE"]`; (2) Claude Code — add to `~/.claude.json`, under the top-level `mcpServers` object, `"agentacct": {"type": "stdio", "command": "BIN", "args": ["mcp", "serve", "--store-dir", "STORE"]}` (edit this JSON while no Claude Code session is running, or a live session may overwrite your change). Then start NEW client sessions.
- `setup global-store-path` calls the exact `agentacct onboard` resolver: existing records win in order (operator override, XDG-shaped canonical store, pre-rename global store); with no records, a valid absolute operator override is the creation target, otherwise the canonical store is. The override aliases `AGENTACCT_GLOBAL_STORE_DIR`, `AGENT_CHRONICLE_GLOBAL_STORE_DIR`, and `AGENT_SENTINEL_GLOBAL_STORE_DIR` may be set to the same non-empty value for compatibility; different non-empty values fail closed in this command, onboarding, and the App so clients cannot split one ledger. This prevents a copy-paste install from ignoring an override or silently splitting history. Never reuse the older silent-fallback path `~/.agent-sentinel`.
- Every registration embeds an explicit absolute `--store-dir` on purpose: GUI-launched clients (Claude Code desktop, Codex.app) do not inherit shell environment variables, so `AGENTACCT_STORE_DIR` (or its pre-rename aliases `AGENT_CHRONICLE_STORE_DIR` / `AGENT_SENTINEL_STORE_DIR`) is shell convenience only — never the mechanism.
- The printed settings example includes `"env": {"ENABLE_TOOL_SEARCH": "auto"}` alongside the hooks — merge that block too (never overwriting env keys the user already has): without it the agentacct MCP tools stay deferred in Claude Code and un-primed sessions record nothing, however well the hooks are wired.
- The Codex hook command installs agentacct's client-specific v1 activity/lifecycle bridge in `~/.codex/hooks.json`; it is separate from the render-only generic Evidence v2 manifest. Codex will not fire it until a new session approves the one-time hook trust prompt.
- REQUIRED cleanup in every repo you switch to global mode: remove the `agentacct` (or pre-rename `agent-chronicle`/`agent-sentinel`) entry from that repo's `.mcp.json` and the `[mcp_servers.agentacct]` (or pre-rename `[mcp_servers.agent-chronicle]` / `[mcp_servers.agent-sentinel]`) block from its `.codex/config.toml` (and stop merging its per-project hooks block) — a project-scope entry silently shadows the user-scope server and pins that repo's MCP context to its old per-project store.
- Point any `usage watch` / `usage import-local` daemons at the concrete `STORE` printed by the block, ONE watch daemon per store: `BIN usage watch --store-dir "STORE"` (replace `BIN` and `STORE` with those printed absolute values). By default each session is imported once at first observation and never updated; add `--refresh` if the daemon should keep growing sessions' totals current (replace semantics).
- REQUIRED in this manual runbook to fill the work views (TUI / JSON API) with work context: `setup instructions` writes a short, idempotent 'record your work as sections' block into `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` (merged inside `<!-- agentacct:begin -->`/`<!-- agentacct:end -->` markers, so your own content is never touched; re-run to update — pre-rename `agent-chronicle:begin` and `agent-sentinel:begin` blocks are recognized and replaced — add `--remove` to strip it, `--dry-run` to preview). Default global onboarding already installs these standing instructions for supported clients; manual MCP registration alone does not.
- What you get and what stays behind: one store with machine-wide usage and all NEW work context; MCP context already recorded in per-project stores can be folded in with `BIN usage merge-store --from <repo>/.agent-sentinel/state --into "STORE"` (replace the placeholders with the printed absolute values; dedup-safe, additive-only, `--dry-run` first) — or inspect an old store in place with `BIN tui --store-dir <repo>/.agent-sentinel/state` (or `BIN serve --store-dir … --port 8790` for the JSON API).
- As with every section: start NEW client sessions after registering — running sessions never see newly added MCP servers or hooks.

## What this setup can and cannot claim

- Claude Code: automatic high-confidence joins between usage and recorded work via the installed hook bridge (SessionStart and PreToolUse capture real session/transcript ids). Exact attribution still requires ids authored explicitly on the recording call; hook-derived ids are not bound to that MCP request. Recorded work context needs all three levers: the merged hooks settings entry including SessionStart (delivery — the SessionStart hook is the only path proven to make un-primed sessions record work, and it adds session-start/resume id capture; PreToolUse still captures the session/transcript ids on every tool call), `ENABLE_TOOL_SEARCH=auto` in the settings `env` block (discoverability — without it the agentacct tools stay deferred and un-primed sessions record nothing), and the hook bridge itself (join keys — without it recorded sections fall back to project-level context, never session-linked).
- Codex: `agentacct onboard --scope global --agent codex` writes user-scope MCP config and standing instructions and installs an observe-only v1 PreToolUse/SessionEnd hook; start a new session and grant the one-time hook trust before it fires. Project-scope onboarding writes MCP config and instructions but not that hook. Semantic sections still join to usage by client-log evidence (high, never `exact`). The separate generic Evidence v2 Codex manifest remains render-only/manual and is not enabled by onboarding.
- Hermes: `agentacct onboard --scope global --agent hermes` writes the user-scope MCP registration and an observe-only v1 shell-hook bridge for tool activity, recognized check exit codes, per-turn liveness, and the first-turn record-your-work nudge. One-time hook consent and a running-gateway restart are still required; unsafe hooks YAML is preserved and leaves tools-only setup. Project-scope setup only previews the profile command. Hermes has no generic Evidence v2 manifest adapter.
- OpenCode: `agentacct onboard --scope global --agent opencode` writes user-scope MCP config, global rules, and an observe-only v1 plugin for tool activity and recognized check exit codes; a new session auto-loads the plugin. Project-scope setup only previews the user-config command. Native `opencode.db` session totals remain the usage path (JSON export fallback; per-message granularity pending). OpenCode has no generic Evidence v2 manifest adapter.
- OpenClaw: local JSONL usage import plus a manual MCP registration preview; agentacct does not yet join `sessions.json` routing metadata or install typed plugin hooks.
- Cursor: the primary `User/globalStorage/state.vscdb` can produce observation-only composer sessions through an explicit local import/refresh. It never emits usage or cost, never scans backups or ai-tracking stores, and onboarding does not install or activate it. Metadata-only hook payload normalization remains a separate manual primitive.
- Generic MCP clients: recorded work context only unless a separate trusted usage importer exists; join confidence depends on ids the client actually exposes.
- Generic Evidence v2 capture is a separate render-only/manual path for Claude Code, Codex, and Cursor: `capture manifest` does not edit host settings, and onboarding does not enable those manifests. The installed Codex/Hermes/OpenCode v1 bridges above may feed activity/check evidence through their own spool/import paths, but neither capture family reports token/cost truth or invents named work steps; MCP remains the richer semantic source.
- All clients: imported tokens are client_reported (read from the client's own local session files); costs are estimates from a local pricing table — never provider invoices. Pricing paths keep a store-local LiteLLM price snapshot fresh automatically (7-day TTL; disable with `AGENTACCT_PRICING_AUTO_REFRESH=0`).
- Always: local-first, observe-only, no telemetry, no provider API keys stored or requested. (The pricing snapshot refresh is a plain GET of LiteLLM's public price table — nothing about your machine, store, or usage is sent.)

Finish the installation by relaying this capability matrix to the user, along with the files you changed, whether `mcp doctor` passed, the `agentacct tui` command, and the local API URL.
