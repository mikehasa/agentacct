# agentacct

[![tests](https://github.com/mikehasa/agentacct/actions/workflows/tests.yml/badge.svg)](https://github.com/mikehasa/agentacct/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/agentacct.svg)](https://pypi.org/project/agentacct/)
[![Python](https://img.shields.io/pypi/pyversions/agentacct.svg)](https://pypi.org/project/agentacct/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

**Your coding agent says it is done. agentacct shows you the evidence: one honest Work Receipt per task, with what the agent did, what it cost, and how well that is proven, entirely on your machine.**

![A Work Receipt in the macOS app: the task "Add a token-bucket rate limiter to the login API" carries a green Verified badge; two outcome bars summarize 5 steps (4 self-checked, 1 claimed) and 5 recorded checks (5 passed); below, the numbered step spine shows each step's summary, its agent-reported check with exit code and provenance, and the file it touched.](https://raw.githubusercontent.com/mikehasa/agentacct/readme-revamp/docs/assets/app-work-receipt.png)

agentacct is local-first Agent Work Intelligence for coding agents such as Claude Code, Codex, OpenCode, and Hermes. It reads the session logs your agents already write, joins them with the work each session records over MCP, and renders the result as a receipt you can hold the agent to. Read it in the macOS app, in the terminal (`agentacct tui`), or over a loopback-only JSON API.

- **Decision and evidence are separate axes.** *Verified* is reserved for machine-checked completion; an agent saying "done" reads as *Reported* and never raises the evidence bar.
- **Every number carries its basis.** Tokens are client-reported, costs are pricing-table estimates marked `≈`, and every attribution between usage and recorded work carries a confidence label. A gap is shown as a gap, never as a guess or a zero.
- **Nothing leaves your machine.** Your agents' logs are read, never modified; state is plain local files, the only listener is on `127.0.0.1`, and there is no account, no telemetry, and no cloud sync. agentacct never stores or requests a provider API key.

<sub>Screenshots show a synthetic demo workspace; your own install renders your machine's real local data.</sub>

## Quickstart

Requires Python >= 3.11 on macOS or Linux; Windows is supported only via WSL.

```bash
pipx install agentacct
agentacct onboard   # once per machine: detects your agents, sets up a global store, starts the local recorder
agentacct tui       # the live terminal dashboard
```

Prefer a native window and no Python? Download the signed, notarized **macOS app** from the [latest release](https://github.com/mikehasa/agentacct/releases/latest) (macOS 14+). Details, alternatives, and uninstall are under [Install](#install).

Either way, open a **new** agent session afterwards: hooks and MCP servers bind at session start, so the session that ran onboarding cannot become the first recorded task.

## How to read a receipt

Three questions, in the order a skeptical reviewer asks them:

- **Did it finish?** Verified, Reported, In progress, Observed, or Stopped, plus two attention states, Blocked and Open finding.
- **Who says so?** Every check carries an evidence tier (an agent's own claim < an agent-reported check < a hook-observed exit code < CI), and the tier travels with the check into every table as a pip shape.
- **Is the proof still current?** A passing check that predates later recorded work is no longer current, and the receipt says so; a re-run or a later edit never inherits an older green.

![The receipt's activity timeline card: a Live toggle and an activity search above a shared time axis; step cards (Write the failing tests, Implement the token-bucket middleware, Handle bursts + concurrent requests, Code review + document the limits) each read Reported completed, and check cards run from 12 failed (red) through two 12 passed runs to 38 passed, with a scrubber bar at the bottom.](https://raw.githubusercontent.com/mikehasa/agentacct/readme-revamp/docs/assets/app-receipt-timeline.png)

The timeline keeps the red run. A failing check is superseded by the passing one that follows, never averaged away, so you can see when the proof caught up with the code and when it did not. Two worked examples show the idea end to end: [when an agent says *done*](docs/examples/when-an-agent-says-done.md) and [Claude Code vs Codex on the same task](docs/examples/compare-claude-code-and-codex.md).

## What you get

The macOS app has five tabs: **Dashboard · Work · Sessions · Usage · Diagnostics**. This tour walks them in the order of a normal day.

### Dashboard: start with what needs review

![The Dashboard: a Shift Brief naming "Fix the flaky payment test" as the primary attention item (billing-svc · claude-code · 1 failed, 7 passed; recorded reason Failed check, observed 1d ago, provenance MCP record, no next step recorded) with Review evidence and Copy review brief buttons; a Signal rail with Working now, Capacity (codex · 37% headroom · provider reported), Usage change, and Evidence trust (Sources healthy); a Recent work table with Outcome, Evidence, and Cost; and a seven-day usage-history chart.](https://raw.githubusercontent.com/mikehasa/agentacct/readme-revamp/docs/assets/app-dashboard.png)

The Shift Brief leads with the single task that most needs you, its recorded reason, and where the claim came from. **Copy review brief** copies only recorded facts and never reruns anything; each rail signal says *not reported* instead of showing a confident number when its data is missing or stale.

### Sessions: one row per task, with its verdict and its proof

![The Sessions tab: lifecycle tabs (All 19, Attention 1, Verified 4, Reported 11, In progress 1, Observed 1, Stopped 1) above a table with Task, Claims supported, Client, Check runs, Est. cost, and Updated columns; the top row reads Verified, 3/3 claims supported, claude-code, 4/4 passed, ≈$118.00, and a Reported hermes row reads 0/1 claims supported with no check runs.](https://raw.githubusercontent.com/mikehasa/agentacct/readme-revamp/docs/assets/app-sessions.png)

Lifecycle tabs never inflate a claim, the Claims supported column says how many claims are supported and at what evidence tier, Check runs shows real pass/fail tallies, and every cost wears its basis. Open a row to get the receipt at the top of this page, or run `agentacct receipt <task>` (`--markdown` pastes into a PR).

### The receipt's ledger: the work, not just the tokens

![The lower half of a Work Receipt: Usage (53 tool calls captured, split Read 24, Edit 9, Execute 11, Search 6, Plan 3, plus related paths), Cost (≈$118.00 pricing estimate; 47.5M tokens total, 9.5M fresh, 38.0M cache read), Weekly plan (≈0.9%), and Recording (task, agents claude-code · claude-opus-4-8, coverage 3 of 3 claims supported, sources client_log · hook · mcp, no recorded gaps, task ID).](https://raw.githubusercontent.com/mikehasa/agentacct/readme-revamp/docs/assets/app-receipt-detail.png)

Below the step spine the receipt becomes a ledger: tool calls by category, cost with its basis and the fresh-vs-cache split behind it, the weekly-plan estimate, and a Recording block naming the agents, the source of each fact, and any gap the receipt could not close.

### Work: a folder's sessions across every agent you run

![The Work tab: a "billing-svc" card grouped by folder gathers six sessions across Claude Code, Codex, and OpenCode on one Sep 11 to Sep 15 timeline (3 sources, span ~4 days, cost sum of receipts ≈$70.50); an "acme-web" card gathers seven sessions across two sources (~6 days, ≈$207.90); each card notes that its total is a sum of 6 or 7 sessions, not a combined verdict.](https://raw.githubusercontent.com/mikehasa/agentacct/readme-revamp/docs/assets/app-work.png)

Point Work at a project folder and every session that ran there lands on one shared timeline, whichever agent ran it. It is an overlay, not a re-grading: each session keeps its own receipt and evidence tier, and the group's totals are a labeled sum, never a combined verdict.

### Usage: provider capacity beside recorded usage

![The Usage & limits page: per-client provider windows (codex 5-hour 12% used and weekly 63% used with reset times; claude-code 34% and 59%; opencode and hermes report no provider limit) beside each client's seven-day recorded use, then recorded-usage totals, a cost-per-day chart, and a by-model table; the footer notes costs are pricing estimates and fresh tokens exclude cache-read tokens.](https://raw.githubusercontent.com/mikehasa/agentacct/readme-revamp/docs/assets/app-usage.png)

Provider-reported quota windows sit beside each agent's recorded usage, with daily history and per-model attribution below. The weekly Claude plan share appears only once agentacct can calibrate to your own recorded limit history.

### Diagnostics: which agents are recording, and how healthy the store is

![The Diagnostics tab: an Agents card listing Claude Code, Codex, Hermes, and OpenCode as Recording, DeepSeek Harness as Not connected with a Connect button, and OpenClaw and Cursor as Read-only; a Continuous sync card reading Running with a heartbeat 2s ago and scans every 60s; a collapsed Verification connections row reading not connected; and a Local evidence store card.](https://raw.githubusercontent.com/mikehasa/agentacct/readme-revamp/docs/assets/app-diagnostics.png)

One row per agent says plainly whether agentacct is **Recording** it, **Not connected** with a Connect action where onboarding can do the rest, or **Read-only** where agentacct only reads logs (OpenClaw until you add its MCP server by hand; Cursor always). The verification shelf reads *not connected* until independent evidence such as CI actually lands.

## What it is honest about

agentacct is early alpha and would rather show you a gap than a guess. Hold it to these:

- **Reported is not Verified.** A task reads *Verified* only when every current check passes and postdates the newest recorded work; an agent's claim can never dress up as verification.
- **Estimates are labeled as estimates.** There is no subscription-invoice access; costs come from a local pricing table. A bare `$` marks a complete client-reported figure, `≈$` an estimate, `~$` a known-partial subtotal, and the full receipt spells the basis out (`≈$118.00 · pricing estimate`). [docs/usage-truth-table.md](docs/usage-truth-table.md) lists what each path can and cannot prove.
- **Missing beats wrong.** Every join between usage and recorded work carries a confidence label (`exact` / `high` / `medium` / `low`). When a link cannot be proven the receipt shows the gap as a named state, never a dash or a fabricated zero.
- **A grouping is a curation act, not a verdict.** Work-tab folder groups are your own assertion that sessions belong together; each aggregate is a labeled sum, and no session's decision or evidence tier changes by being grouped.
- **Support is per-capability, not per-logo.** Claude Code and Codex carry the fullest receipt today: session, usage, and MCP lanes with dated evidence, while commands, edited files, and tool categories are captured at an experimental tier. The rest, in one line each:
  - Hermes: evidenced usage and MCP lanes, narrower capture surface.
  - OpenCode: records over MCP at a bounded tier; usage import experimental.
  - DeepSeek Harness: MCP self-reporting verified on one machine; usage import experimental.
  - OpenClaw: usage read from its logs at an experimental tier; MCP self-reporting only after you add its server by hand.
  - Cursor: observation-only, never tokens or cost.

  The [coverage matrix](docs/coverage-matrix.md) rates every lane separately as `verified`, `verified_partial`, `experimental`, or unavailable, and `agentacct capabilities agents` prints the same truth for your machine.
- **It records the work, not the conversation.** Locally it keeps what an audit needs: tool categories and names, the files each step touched, single-line credential-scrubbed commands, exit codes, tokens, and recorded work. It does not store your prompts, the model's responses, or transcripts, and metadata-only hooks omit the agent's thoughts and raw tool arguments; the [privacy threat model](docs/multi-source-privacy-threat-model.md) describes the metadata-only hook profile and the fields it denies.
- **No hosted anything, no silent monitoring.** No hosted dashboard, no phone-home telemetry, no cloud account sync. agentacct only reads the local session files of detected clients and never watches unrelated processes; it can pause or stop only runs it launched itself.

Interfaces may change while agentacct is alpha.

## Install

### The macOS app

Download the `.dmg` from the [latest release](https://github.com/mikehasa/agentacct/releases/latest), drag agentacct to Applications, and open it. First launch offers one-click setup of the bundled CLI and the coding agents it finds; no Python required, macOS 14+.

On each launch the app validates its embedded CLI and stages a newer verified one as an immutable version, without disturbing running MCP or hook processes or a pipx install. In-app updates through Sparkle are planned, not shipped (see the [app notes](apps/agentacct/README.md)); the CLI staging layout and recovery boundary are in the [packaging notes](packaging/README.md).

### The CLI

Requires Python >= 3.11 on macOS or Linux; Windows is supported only via WSL.

```bash
pipx install agentacct
agentacct onboard
agentacct tui
```

No `pipx` yet? `brew install pipx` (macOS) or `python3 -m pip install --user pipx`, or use `uv tool install agentacct` instead. [INSTALL.md](INSTALL.md) is the canonical runbook, including a plain-`venv` fallback and the per-client setup.

`onboard` installs once per machine, global by default, with zero files written into your repo: it detects your local agent logs, sets up a global store, runs a first usage sync, and starts the background sync plus the local JSON API on `http://127.0.0.1:8765`. `--scope project` keeps state in the repo's gitignored `.agent-sentinel/` directory instead (the pre-rename spelling is kept for data compatibility).

`agentacct start` / `status` / `stop` / `repair` control the managed runtime; the global store lives at `~/.local/state/agentacct/state` (older `~/.agent-sentinel-global/state` stores are still recognized). `agentacct demo` runs a walkthrough in a throwaway store with no provider keys and no paid API calls.

### Let your coding agent install it

Paste this into your coding agent:

```text
Install and set up agentacct — a local-first agent work ledger that reads my
coding-agent logs read-only and shows honest token usage, cost, and recorded work.

Run `pipx install agentacct`
(or `pipx install git+https://github.com/mikehasa/agentacct`),
then `agentacct onboard` (installs once per machine, global by default, zero
files written into the repo), then tell me how to open `agentacct tui` and the
local JSON API at http://127.0.0.1:8765.

Observe-only: never store, request, or echo any API key; all state stays local
on this machine. Don't modify my global client config without showing the exact
command first.
```

The agent then follows [INSTALL.md](INSTALL.md). `agentacct setup prompt --agent <client>` prints the same prompt.

### Uninstall

```bash
agentacct stop                 # stop the managed sync + local API (owned processes only)
agentacct uninstall-autostart  # only if you installed autostart
pipx uninstall agentacct
```

Then remove what onboarding added:

- Global install: the store at `~/.local/state/agentacct/state` (keep it if you want the history), the agentacct entries in `~/.claude.json`, `~/.claude/settings.json`, the `~/.claude/hooks/` wrapper, `~/.codex/config.toml`, `~/.codex/hooks.json` and `~/.codex/hooks/agentacct_codex_hook.py`, and the OpenCode, Hermes, and dsh entries listed per client in [docs/coding-agent-integrations.md](docs/coding-agent-integrations.md).
- Project install: that repo's `.agent-sentinel/` directory and the agentacct entries in `.mcp.json`, `.claude/settings.local.json`, and `~/.codex/config.toml`.
- A standing instruction block, if you installed one: run `agentacct setup instructions --agent <client> --user --remove` first.

## The terminal app

`agentacct tui` is the app in your shell: the same receipts, evidence, and capacity, keyboard-native. Tabs `1`–`4` switch between **Dashboard**, **Work**, **Usage**, and **Sources**; `↑↓` move, `↵` drills into a receipt's steps, `/` filters, `[`/`]` and `s` switch lifecycle tab and sort, `T` cycles the theme, `p` saves a shareable SVG snapshot, `?` lists every key, `q` quits.

The terminal app has no folder-grouping tab. Its **Work** tab is the receipts list the macOS app calls **Sessions**, and its **Sources** tab is the macOS app's **Diagnostics**.

![agentacct tui, the Dashboard: a Shift Brief with the primary attention item (a Blocked task, its recorded reason, and recorded next step), a Signal rail with working now, capacity, usage change, and evidence trust, a Recent work table with outcome, evidence, and cost, and a fresh-token usage history.](https://raw.githubusercontent.com/mikehasa/agentacct/readme-revamp/docs/assets/tui-dashboard.png)

Select a receipt and press `↵` to drill into its sessions and steps. A currently failing check stays in view under *Needs attention* instead of being averaged away.

![agentacct tui, a receipt's sessions and steps: the failing test surfaced under Needs attention with its exit code and provenance, four passing checks (build, lint, artifact, test) below it, and the two files the task touched.](https://raw.githubusercontent.com/mikehasa/agentacct/readme-revamp/docs/assets/tui-steps.png)

## How it works

agentacct keeps two evidence streams separate and joins them on real client ids instead of guessing.

- **Usage truth** comes from each client's own local session files. Imported tokens are labeled `client_reported`; costs are pricing-table estimates, never provider invoices.
- **Work meaning** comes from what the agent records over MCP while it works (`agentacct_record_section`, `agentacct_record_machine_check`) plus machine checks such as test runs. Each check keeps its evidence tier (agent-reported, hook-observed, or CI), computed from how it was observed, never from the agent's wording.
- **The join** links the two through session and transcript ids and labels every attribution `exact`, `high`, `medium`, or `low`. Claude Code binds real ids through an installed hook bridge at session start and on every tool call; Codex, OpenCode, and Hermes are evidenced from their own session stores at import time; for Codex and OpenCode that store scan also supplies commands, edited files, and tool categories where no hook fires, while Hermes actions come only from its installed hooks. The receipt says which path it used.

The per-client join mechanics, the confidence-label glossary, and the MCP tool list are in [docs/reference.md](docs/reference.md).

## Documentation

- Worked examples: [when an agent says *done*](docs/examples/when-an-agent-says-done.md) · [Claude Code vs Codex on one task](docs/examples/compare-claude-code-and-codex.md)
- [Coverage matrix](docs/coverage-matrix.md): every agent's lanes and how strongly each is proven · [Adapter capability evidence](docs/adapter-capability-evidence.md): the dated evidence behind each rating
- [Install runbook](INSTALL.md) · [Reference](docs/reference.md): daily workflow, confidence labels, MCP tools, verification evidence
- [Usage and cost truth table](docs/usage-truth-table.md) · [Coding agent integrations](docs/coding-agent-integrations.md)
- [Architecture](docs/architecture.md) · [Task Intelligence and the local control plane](docs/task-control-plane.md) · [Multi-source evidence architecture](docs/multi-source-evidence-architecture.md)
- [Privacy threat model](docs/multi-source-privacy-threat-model.md) · [Safety boundaries](docs/safety-boundaries.md) · [Full flow demo](docs/full-demo.md)

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for scope, safety principles, and PR expectations. Run tests from a clone (the pipx install ships no test tooling):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest tests/ -q --tb=short
```

The macOS-app screenshots above are regenerated from a synthetic demo store with `scripts/gen_app_screenshots.py` (needs a built app binary from `apps/agentacct/Scripts/build-app.sh` and macOS 14+); the terminal shots come from `scripts/gen_tui_screenshots.py`.

## Feedback

Open an issue with a bug report, feature request, or integration request. The most useful reports say which agent you use, which join or attribution result looked wrong or missing, and what a receipt would need to show before you trusted a run. Please scrub provider API keys and private paths from any logs before sharing them.
