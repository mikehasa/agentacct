# agentacct — install runbook for coding agents

This file is written for the CODING AGENT performing the install (Claude Code, Codex, Hermes, OpenCode, OpenClaw, or any MCP-capable agent). agentacct is public: install it with `pipx install agentacct` (or straight from the public repository with `pipx install git+https://github.com/mikehasa/agentacct`). Work from the target repository's root and follow the section for the client you are running inside.

<!-- Consistency contract: the command blocks, notes, and capability matrix below
     are defined in src/agent_chronicle/install_guide.py and embedded here verbatim.
     tests/test_install_guide.py fails if this file drifts from the module.
     Edit the module first, then mirror the change here. -->

## What you are installing

agentacct is local-first Agent Work Intelligence for coding agents:
- a usage ledger of client-reported tokens, imported from the coding agent's own local session files;
- an MCP work context: sections and events the agent records while working, joined to usage with honest confidence labels;
- a local dashboard (`agentacct serve`, http://127.0.0.1:8765).

Hard rules for this install:

- Observe-only. NEVER store, request, or echo provider API keys — nothing in this install needs one.
- No telemetry. All state stays local to this machine: in the project store under `.agent-sentinel/` (gitignored; the store directory keeps the pre-rename name `.agent-sentinel/` for compatibility with existing stores) by default, or in the global store directory if you follow the optional Global install section, plus the client config files named below.
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

## Step 2 — preferred project onboarding

From the target repository root, run:

```bash
agentacct onboard
agentacct status
```

`onboard` detects an importable local usage source, initializes the project-local store and recording configuration, performs one real local usage sync, and starts continuous sync plus the dashboard. It changes project-local files only: no provider keys, billing connection, or global client settings are required. To select a client explicitly, use `--agent codex` or `--agent claude-code`.

The command finishing successfully means agentacct is configured; it does not mean a real Task has appeared. **Open a NEW agent session in this project after onboarding.** MCP servers and hooks bind when the client session starts, so the session that ran onboarding cannot see the newly registered tools. agentacct stays in an honest waiting state until a real recognized client session appears; a demo row does not count.

The managed local runtime survives the invoking shell and uses one project-local store. Its lifecycle is:

```bash
agentacct start
agentacct status
agentacct stop
agentacct repair
```

- `start` is idempotent and starts continuous local usage sync plus the localhost dashboard.
- `status` reports dashboard, sync, and ingestion readiness.
- `stop` signals only processes whose complete agentacct ownership proof still matches.
- `repair` clears dead or corrupt owned runtime state without adopting or killing an unknown process.

The dashboard is at http://127.0.0.1:8765 by default. Everything stays local to this machine under `.agent-sentinel/` (gitignored); onboarding neither requests provider API keys nor uploads session data.

## Migration notes (formerly Agent Sentinel)

agentacct was formerly Agent Sentinel; pre-rename installs keep working without re-setup:

- Environment variables: every `AGENT_CHRONICLE_*` variable accepts its pre-rename `AGENT_SENTINEL_*` alias — old names are accepted indefinitely, the new name wins when both are set, and conflicting store-dir values refuse instead of silently splitting the ledger. There is no runtime deprecation nag; the old names simply keep working.
- Binaries: the published package ships only agentacct-branded console scripts — `agentacct` plus the `agentacct-claude` / `agentacct-codex` wrappers. Pre-rename MCP registrations, hook wrappers, and launchd jobs that resolve the old `agent-chronicle` / `agent-sentinel` binary names are still recognized at runtime, but the old console scripts are no longer shipped (they collide with unrelated PyPI packages of those names).
- Pre-rename `agent-sentinel` MCP registrations and `agent-sentinel:begin` instruction blocks stay recognized; stored data and the `.agent-sentinel/` store directories keep their pre-rename spellings forever (data format, not branding).
- Caveat — foreign PyPI package collision: an unrelated PyPI project named `agent-sentinel` (0.5.0) also installs a `bin/agent-sentinel` script. If both packages land in the SAME environment, the later install silently overwrites that script (pip prints no warning), and `pip uninstall agent-sentinel` (the foreign package) deletes the alias out from under agentacct — pre-rename registrations and wrappers resolving the old binary name then fail until `pip install --force-reinstall agentacct`. Do not install both in one environment; pipx refuses the second install because the app names collide.

## Step 3 — advanced manual client setup

Use this fallback when client auto-detection is unavailable or you need to configure integrations separately. Run everything from the target repository root.

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
- REQUIRED final step: start a NEW Codex session in this repo after install — Codex binds MCP servers at session start, so the agentacct tools never appear in the session that performed the install, and nothing is recorded until the first fresh session.
- This project's recorded work context appears on THIS project's dashboard (`agentacct serve` run here); other projects' dashboards will show these sessions as usage-only, labeled with this project's name.
- Codex cannot pass its own session id in-session; at usage import, agentacct pairs each agentacct-recorded event with the Codex session log that created it (client-log evidence), so recorded work earns high-confidence session links once the session's rollout is imported — never `exact`, and a section evidenced by more than one session links to none. The client name is implied the same way, so codex agents need not pass `client` explicitly (a wrong explicit name is conflict-vetoed, never trusted).
- Register the server as `agentacct` (every agentacct setup path writes that name). Pre-rename `agent-sentinel` registrations are equally recognized forever — existing installs do not need to re-register. Any OTHER custom registration name loses session links — skipped-but-counted in the dashboard insights, never guessed.

### Hermes / OpenCode / OpenClaw / other MCP clients

```bash
agentacct init --agent hermes
agentacct setup mcp --agent hermes
agentacct mcp doctor
agentacct usage import-local --client all --dry-run
agentacct serve
```

Replace `hermes` with `opencode` or `openclaw` for those clients.

- These clients keep MCP config in profile/global or client-specific locations, so `setup mcp` PREVIEWS the exact registration command instead of writing it.
- First remove any stale pre-rename server, e.g. `opencode mcp remove agent-sentinel` (also `agent-chronicle`): only `agentacct` ships now, so a leftover old-name entry launches a command that no longer exists (ENOENT), which the client reports as a crashed MCP server.
- Show the previewed command to the user and ask before modifying global agent configuration, then paste it into that client's own MCP setup flow.
- For an unlisted MCP-capable client, use `--agent generic` to get a portable stdio server definition.

Every section ends the same way: `agentacct serve` is long-running: start it in the background or hand the command to the user; dashboard at http://127.0.0.1:8765. The dry-run import only previews; drop `--dry-run` (or click the dashboard's "Refresh & save usage" button) when the user wants the ledger populated — reloading a dashboard page writes nothing (the Overview/Tokens/Sessions pages render saved rows only; the read-only scan runs on the Raw data page).

## What the Task page shows

The dashboard groups recognized root client sessions into stable Tasks. Open a Task from the dashboard or use its opaque public route:

```text
http://127.0.0.1:8765/tasks/task_<opaque-id>
http://127.0.0.1:8765/api/tasks/task_<opaque-id>
```

The page is a decision brief, not a raw event dump: it summarizes what was attempted, the current outcome, strongest proof, usage/cost basis, unresolved findings, and a next action only when an owner was explicitly recorded. A bounded evidence timeline remains available underneath.

Task state has three independent axes: **execution** (queued/running/finished/cancelled/lost), **outcome** (unknown/reported/verified/blocked/open finding), and **control** (ready/awaiting approval/policy hold/control failure). A failed target-product check is an outcome finding; it is not automatically an agentacct failure or a user action.

## Optional — govern an agentacct-owned local attempt

The dashboard's **Control** page governs only processes agentacct launches itself. Existing Codex, Claude Code, Paperclip, OpenLIT, Entire, and other orchestrator processes remain observed-only.

Register an existing workspace and a fixed argv array (never a shell command string):

```bash
agentacct control register-workspace \
  --store-dir .agent-sentinel/state --root . --workspace-id project

agentacct control register-agent \
  --store-dir .agent-sentinel/state \
  --agent-id local-agent --display-name "Local agent" \
  --argv-json '["/absolute/command","arg"]'
```

Then open `http://127.0.0.1:8765/control`, create a pending Task Contract, review its workspace/adapter/checks, and explicitly press **Launch**. Contract creation never starts a process. Workspace-write attempts require an expiring, single-use approval before the supervisor will launch them.

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

`control launch` stays in the foreground until the attempt is terminal; the long-running dashboard owns the persistent web supervisor. An attempt freezes the current registered agent revision, and launch fails closed if that agent's argv changes after the attempt or approval was created; create a new attempt to authorize the new revision. Control status and product JSON are sanitized: they never return registered absolute paths, argv, PIDs/process groups, executable/cwd fingerprints, manifest ids, or ownership nonces.

## Global install (single-user machine, optional)

The per-agent sections above install agentacct per repository: each repo gets its own store, dashboard, and MCP registration. On a single-user machine you can instead register ONE user-scope MCP server, hook, and dashboard against ONE global store — machine-wide usage AND machine-wide work context on a single dashboard, no per-repo install step. State then lives in the global store directory below instead of each repository.

```bash
mkdir -p "$HOME/.agent-sentinel-global/state"
AGENTACCT_BIN="$(command -v agentacct)"
[ -n "$AGENTACCT_BIN" ] || AGENTACCT_BIN="$HOME/.agentacct-cli/.venv/bin/agentacct"  # non-PATH install: substitute YOUR absolute path from Step 1 — an empty command would register a dead server
# the two `mcp add` lines below rewrite USER-LEVEL client config (~/.claude.json, ~/.codex/config.toml): show them to the user and ask first (ground rule)
# NO `claude`/`codex` CLI on PATH? (common: the Claude Code desktop app and ChatGPT.app's Codex ship none) — skip the two `mcp add` lines and hand-write the same registration per the "no CLI" note below.
claude mcp add --scope user agentacct -- "$AGENTACCT_BIN" mcp serve --store-dir "$HOME/.agent-sentinel-global/state"
codex mcp add agentacct -- "$AGENTACCT_BIN" mcp serve --store-dir "$HOME/.agent-sentinel-global/state"
agentacct hooks claude-code install --project-dir "$HOME" --store-dir "$HOME/.agent-sentinel-global/state" --user-settings-example  # wrapper homes in ~/.claude/hooks/ (NOT the store dir): a store move must never vanish the hook and brick sessions
# merge the printed "hooks" AND "env" blocks into ~/.claude/settings.json (user-level; ask the user first; merge, never overwrite existing keys)
# standing "record your work" instructions — this is what fills the dashboard with work context (writes ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md; ask the user first, --dry-run to preview)
agentacct setup instructions --agent claude-code --user --store-dir "$HOME/.agent-sentinel-global/state"
agentacct setup instructions --agent codex --user --store-dir "$HOME/.agent-sentinel-global/state"
agentacct serve --store-dir "$HOME/.agent-sentinel-global/state"
```

- No CLI? The `claude mcp add` / `codex mcp add` lines need the `claude` and `codex` CLIs on PATH — but the Claude Code desktop app and ChatGPT.app's Codex read their config files directly and often ship NO such CLI, so those two lines fail with `command not found`. Register by hand instead, using the absolute path from `command -v agentacct` (call it BIN): (1) Codex — add to `~/.codex/config.toml` a table `[mcp_servers.agentacct]` with `command = "BIN"` and `args = ["mcp", "serve", "--store-dir", "$HOME/.agent-sentinel-global/state"]`; (2) Claude Code — add to `~/.claude.json`, under the top-level `mcpServers` object, `"agentacct": {"type": "stdio", "command": "BIN", "args": ["mcp", "serve", "--store-dir", "<the same absolute store path>"]}` (edit this JSON while no Claude Code session is running, or a live session may overwrite your change). Then start NEW client sessions.
- The store is `$HOME/.agent-sentinel-global/state` — deliberately NOT the legacy `~/.agent-sentinel` directory, which older versions used as a silent fallback store; reusing it would mix that stray data into global mode. The directory keeps its pre-rename `agent-sentinel` name for compatibility with existing global stores.
- Every registration embeds an explicit absolute `--store-dir` on purpose: GUI-launched clients (Claude Code desktop, Codex.app) do not inherit shell environment variables, so `AGENT_CHRONICLE_STORE_DIR` (or its pre-rename alias `AGENT_SENTINEL_STORE_DIR`) is shell convenience only — never the mechanism.
- The printed settings example includes `"env": {"ENABLE_TOOL_SEARCH": "auto"}` alongside the hooks — merge that block too (never overwriting env keys the user already has): without it the agentacct MCP tools stay deferred in Claude Code and un-primed sessions record nothing, however well the hooks are wired.
- REQUIRED cleanup in every repo you switch to global mode: remove the `agentacct` (or pre-rename `agent-chronicle`/`agent-sentinel`) entry from that repo's `.mcp.json` and the `[mcp_servers.agentacct]` (or pre-rename `[mcp_servers.agent-chronicle]` / `[mcp_servers.agent-sentinel]`) block from its `.codex/config.toml` (and stop merging its per-project hooks block) — a project-scope entry silently shadows the user-scope server and pins that repo's MCP context to its old per-project store.
- Point any `usage watch` / `usage import-local` daemons at the same store, ONE watch daemon per store: `agentacct usage watch --store-dir "$HOME/.agent-sentinel-global/state"`. By default each session is imported once at first observation and never updated; add `--refresh` if the daemon should keep growing sessions' totals current (the dashboard's "Refresh & save usage" replace semantics).
- REQUIRED to fill the dashboard with work context: `setup instructions` writes a short, idempotent 'record your work as sections' block into `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` (merged inside `<!-- agent-chronicle:begin -->`/`<!-- agent-chronicle:end -->` markers, so your own content is never touched; re-run to update — pre-rename `agent-sentinel:begin` blocks are recognized and replaced — add `--remove` to strip it, `--dry-run` to preview). Without it, global mode records tokens but almost no work context, because global installs no standing instruction the way a per-project setup does.
- What you get and what stays behind: one dashboard with machine-wide usage and all NEW work context; MCP context already recorded in per-project stores can be folded in with `agentacct usage merge-store --from <repo>/.agent-sentinel/state --into "$HOME/.agent-sentinel-global/state"` (dedup-safe, additive-only, `--dry-run` first) — or browse an old store in place with `agentacct serve --store-dir <repo>/.agent-sentinel/state --port 8790`.
- As with every section: start NEW client sessions after registering — running sessions never see newly added MCP servers or hooks.

## What this setup can and cannot claim

- Claude Code: automatic high-confidence joins between usage and recorded work via the installed hook bridge (SessionStart and PreToolUse capture real session/transcript ids). Exact attribution still requires ids authored explicitly on the recording call; hook-derived ids are not bound to that MCP request. Recorded work context needs all three levers: the merged hooks settings entry including SessionStart (delivery — the SessionStart hook is the only path proven to make un-primed sessions record work, and it adds session-start/resume id capture; PreToolUse still captures the session/transcript ids on every tool call), `ENABLE_TOOL_SEARCH=auto` in the settings `env` block (discoverability — without it the agentacct tools stay deferred and un-primed sessions record nothing), and the hook bridge itself (join keys — without it recorded sections fall back to project-level context, never session-linked).
- Codex: session-linked work context via client-log evidence (high) — at usage import, agentacct pairs each agentacct-recorded event with the Codex session log that created it (creation responses only, never read-tool echoes). Sections still never earn `exact` (the link is evidenced from the log at import time, not client-authored in-session), a section evidenced by more than one session links to none, and sessions whose rollouts have not been imported fall back to project-level context only.
- Hermes: local `state.db` usage import plus a manual MCP registration preview; agentacct does not yet install Hermes mechanical hooks.
- OpenCode: captured/exported JSON usage import plus a manual MCP registration preview; agentacct does not yet parse the current official SQLite store or install a realtime plugin.
- OpenClaw: local JSONL usage import plus a manual MCP registration preview; agentacct does not yet join `sessions.json` routing metadata or install typed plugin hooks.
- Cursor: the primary `User/globalStorage/state.vscdb` can produce observation-only composer sessions through an explicit local import/refresh. It never emits usage or cost, never scans backups or ai-tracking stores, and onboarding does not install or activate it. Metadata-only hook payload normalization remains a separate manual primitive.
- Generic MCP clients: recorded work context only unless a separate trusted usage importer exists; join confidence depends on ids the client actually exposes.
- Mechanical Claude Code/Codex/Cursor capture, when manually wired, writes Evidence v2 and projects bounded session activity into the homepage as an observed Task with models/checks when present. It is not activated by onboarding, does not report token/cost truth, and does not invent named work steps; MCP remains the richer semantic source.
- All clients: imported tokens are client_reported (read from the client's own local session files); costs are estimates from a local pricing table — never provider invoices. Pricing paths keep a store-local LiteLLM price snapshot fresh automatically (7-day TTL; disable with `AGENT_CHRONICLE_PRICING_AUTO_REFRESH=0`).
- Always: local-first, observe-only, no telemetry, no provider API keys stored or requested. (The pricing snapshot refresh is a plain GET of LiteLLM's public price table — nothing about your machine, store, or usage is sent.)

Finish the installation by relaying this capability matrix to the user, along with the files you changed, whether `mcp doctor` passed, and the dashboard command.
