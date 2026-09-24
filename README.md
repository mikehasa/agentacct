# agentacct

English · [简体中文](README.zh-CN.md)

[![tests](https://github.com/mikehasa/agentacct/actions/workflows/tests.yml/badge.svg)](https://github.com/mikehasa/agentacct/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/agentacct.svg)](https://pypi.org/project/agentacct/)
[![Python](https://img.shields.io/pypi/pyversions/agentacct.svg)](https://pypi.org/project/agentacct/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

**agentacct answers one simple question: what the fuck are my agents actually doing?**

See what your coding agents have been working on, across projects and clients: recent activity, tool calls, recorded steps, checks, tokens, and estimated cost. Open a task for its Work Receipt and follow the activity timeline from attempt to result. Built from the local session logs of Claude Code, Codex, OpenCode, Hermes, and others. No account, no cloud, nothing leaves your computer.

![Sessions and a Work Receipt: a latest-first task list beside the recorded outcome, session count, estimated cost, claim coverage, check results, and activity timeline.](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/app-work-receipt.png)

<sub>Screenshots show a synthetic demo workspace; your install renders your own local data.</sub>

## Install

```bash
pipx install agentacct
agentacct onboard   # once per machine: finds your agents, sets up a local store, starts the recorder
agentacct tui       # the terminal app
```

Requires Python >= 3.11 on macOS or Linux; Windows is supported only via WSL. For a native window with no Python, download the signed, notarized **macOS app** from the [latest release](https://github.com/mikehasa/agentacct/releases/latest) (macOS 14+).

Either way, open a new agent session afterwards: hooks and MCP servers bind at session start.

The per-agent setup, a `--scope project` install, and the `uv`/`venv` alternatives are in [INSTALL.md](INSTALL.md).

## Follow what happened

Sessions opens with the most recently active tasks. Search by task or project, or choose another sort when you need it. Each receipt brings together the participating sessions, recorded outcome, estimated cost, claims, and check results; its activity timeline comes first, with steps and full check details one click away.

An agent's own "done" is **Reported**. A **Verified** result needs current passing checks after the last recorded change. Claims and checks stay separate, and a failed attempt stays visible in the history alongside later results. A recorded failure is useful context, not automatically a request for you to intervene.

## A day's work, across every agent you run

![Work groups show participating clients, recent sessions, token totals, and estimated cost, with a drilldown into shared project activity.](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/app-work.png)

Point a work group at a project folder to see which agents worked there and what they did most recently. Open the group for session details and a shared timeline. Each session keeps its own evidence; group totals are sums of the participating sessions, with partial coverage and overlap labeled.

## Start with recent activity

![Dashboard overview with recent session status, recorded task count, seven-day usage, and a latest-activity table. Recorded issues are folded below the activity list.](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/app-dashboard.png)

The Dashboard shows the latest recorded work across your projects, with its client, activity time, outcome, evidence, and cost. Historical failed checks and blockers remain available in **Recorded issues**. They do not decide the default order or become a list of things you must fix. Right-click a **Recorded issues** row to copy a brief from recorded facts.

## Understand the usage behind the work

![Usage with a daily chart, Fresh, Cache, Total, and Cost columns, and a selected-date breakdown by client and model with separate cache reads and writes. Dates represent session totals by activity date.](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/app-usage.png)

Select a date to compare clients and models—for example, the same model through Codex and Hermes. The token chart remembers your **Fresh** or **All tokens** choice. Tables show fresh, cache, and total tokens side by side; the client/model breakdown separates cache reads and writes. Hover a **Fresh** value for input and output counts.

Dates show **session totals by activity date**; sessions spanning several days are not split into exact daily consumption. Provider-reported quota windows and reset times are available on the same page. Tokens come from client records; costs are pricing-table estimates marked `≈`, never an invoice.

## Also in the terminal

![agentacct tui: a Needs review block whose first item is a blocked claude-code task with its recorded reason, next step, and MCP-record provenance; a Right now rail with Working now, Capacity, Usage change, and Evidence trust; a Recent work table with outcome, evidence, and estimated cost; and a usage history sparkline.](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/tui-dashboard.png)

`agentacct tui` provides receipts, usage, capacity, and its own review overview in your shell — including the folder-anchored **Work** tab, where each group draws its sessions across every agent on one cross-agent timeline you can open (`↵`) and zoom/scrub by keyboard. Press `?` for the keys.

## Honest by design

agentacct is early alpha and would rather show you a gap than a guess.

- **Reported is not Verified.** An agent's claim can never dress up as verification.
- **Estimates are labeled.** `≈$` is a pricing-table estimate, `~$` a known-partial subtotal; there is no invoice access. [docs/usage-truth-table.md](docs/usage-truth-table.md) says what each path can prove.
- **Missing beats wrong.** Every join between usage and recorded work carries a confidence label (`exact`, `high`, `medium`, `low`); an unproven link shows as a gap, not a zero.
- **Support is per capability, not per logo.** Claude Code and Codex carry the fullest receipts today; the [coverage matrix](docs/coverage-matrix.md) rates every lane for OpenCode, Hermes, DeepSeek Harness, Kimi Code, OpenClaw, and Cursor separately, and `agentacct capabilities agents` prints the same matrix from your installed version.
- **It records the work, not the conversation.** Tool names and categories, touched files, credential-scrubbed commands, exit codes, tokens, and recorded steps. Never your prompts, the model's responses, or transcripts; see the [privacy threat model](docs/multi-source-privacy-threat-model.md).
- **Nothing phones home.** It reads only the local session files of detected clients, listens only on `127.0.0.1`, and never stores or requests a provider API key.

## How it works

agentacct is local-first Agent Work Intelligence: two evidence streams, kept separate and joined on real client ids instead of guesses.

- **What it used** comes from each client's own session files: tokens as the client reported them, costs as pricing-table estimates.
- **What it did** comes from the steps and checks the agent records over MCP as it works, plus machine checks such as test runs. How a check was observed decides its evidence tier, never the agent's wording.
- **The join** links the two by session and transcript id and labels every attribution with a confidence. Claude Code binds real ids through an installed hook bridge; Codex and OpenCode are matched from their own session logs at import time; Hermes joins recorded work only on ids the agent passes explicitly. The receipt says which path it used.

Join mechanics, the confidence glossary, and the MCP tool list are in [docs/reference.md](docs/reference.md); per-agent integration detail is in [docs/coding-agent-integrations.md](docs/coding-agent-integrations.md).

## Let your coding agent install it

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

## Uninstall

```bash
agentacct stop                 # stop the managed sync + local API (owned processes only)
agentacct uninstall-autostart  # only if you installed autostart
pipx uninstall agentacct
```

Then delete the store at `~/.local/state/agentacct/state` (keep it if you want the history) and remove the entries that `agentacct onboard` added to your client configs; they are listed per client in [docs/coding-agent-integrations.md](docs/coding-agent-integrations.md). A `--scope project` install keeps everything in that repo's `.agent-sentinel/` directory instead.

## More

- Worked examples: [when an agent says *done*](docs/examples/when-an-agent-says-done.md) · [Claude Code vs Codex on one task](docs/examples/compare-claude-code-and-codex.md)
- [Reference](docs/reference.md) · [Coverage matrix](docs/coverage-matrix.md) · [Architecture](docs/architecture.md) · [Safety boundaries](docs/safety-boundaries.md) · [Full flow demo](docs/full-demo.md)
- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md) covers running the tests and regenerating the screenshots above from the synthetic demo store.
- Feedback: open an issue naming the agent you use and the join, attribution, or receipt that looked wrong, or what a receipt would need to show before you trusted a run. Scrub API keys and private paths from logs first.
