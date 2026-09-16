# agentacct

English · [简体中文](README.zh-CN.md)

[![tests](https://github.com/mikehasa/agentacct/actions/workflows/tests.yml/badge.svg)](https://github.com/mikehasa/agentacct/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/agentacct.svg)](https://pypi.org/project/agentacct/)
[![Python](https://img.shields.io/pypi/pyversions/agentacct.svg)](https://pypi.org/project/agentacct/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

**agentacct answers one simple question: what the fuck are my agents actually doing?**

Your coding agent says it is done. agentacct shows you what it actually did, what it cost, and how much of that is proven: one Work Receipt per task, built from the session logs your coding agents (Claude Code, Codex, OpenCode, Hermes, and others) already write on your machine. No account, no cloud, nothing leaves your computer.

![The Sessions view: on the left, the task list with one verdict per row (Verified, In Progress, Reported, Observed); on the right, the open receipt for "Add a token-bucket rate limiter to the login API", marked Verified, with two bars (5 steps: 4 self-checked, 1 claimed; 5 checks: 5 passed), the numbered step spine with the latest step expanded to its agent-reported check, exit code, and touched file, and below it the activity timeline where the check cards run from 12 failed to 12 passed, 12 passed, and 38 passed.](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/app-work-receipt.png)

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

## Done, according to whom?

The receipt above answers three questions in order: did it finish, who says so, and is the proof still current.

The agent's own "done" files under **Reported**. A task reads **Verified** only when every current check passes and ran after the last recorded change. A step with no check stays **claimed**, and every check carries its evidence tier: agent-reported, a hook-observed exit code, or CI. In the receipt above, steps 1 to 4 carry agent-reported checks; step 5 is a bare claim, and the receipt says so.

The timeline under the steps keeps the red run. The first test run failed 12; the later runs passed 12, 12, and 38, and nothing is averaged away, so you can see when the proof caught up with the code and when it did not.

## A day's work, across every agent you run

![The Work tab: a "billing-svc" group of 8 sessions from 4 agents over about 9 hours (≈$72.71 as a sum of receipts) on one timeline from 09:05 to 18:20. Two long Claude Code runs anchor the morning and afternoon; shorter Codex and Hermes runs sit inside them; OpenCode runs overlap the edges; the last run, the one that needs attention, carries a red pip.](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/app-work.png)

Point a work group at a project folder and every session that ran there lands on one timeline, whichever agent ran it. Each session keeps its own receipt and evidence; the group's total is a sum of 8 receipts, never a combined verdict on the work.

## Start the day with what needs you

![The Dashboard: a Shift Brief leading with "Fix the flaky payment test" (billing-svc, claude-code, 1 failed and 7 passed, recorded reason Failed check, provenance MCP record) with Review evidence and Copy review brief buttons; a Signal rail with Working now, Capacity, Usage change, and Evidence trust; a Recent work table; and a seven-day usage chart.](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/app-dashboard.png)

The Shift Brief names the one task that most needs a human, its recorded reason, and where that claim came from: here a failed check on "Fix the flaky payment test", recorded over MCP. **Copy review brief** copies only recorded facts and never reruns anything.

## Know your limits before the agent hits them

![Usage & limits: per-client provider windows (codex 5-hour 12% used and weekly 63% used with reset times; claude-code 34% and 59%; opencode and hermes report no provider limit) beside each client's seven-day recorded use, all marked pricing estimate.](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/app-usage.png)

Provider-reported quota windows sit beside what each agent actually used. Tokens are client-reported and costs are pricing-table estimates marked `≈`, never an invoice.

## Also in the terminal

![agentacct tui: a Shift Brief whose primary attention item is a blocked claude-code task with its recorded reason, next step, and MCP-record provenance; a Signal rail with Working now, Capacity, Usage change, and Evidence trust; a Recent work table with outcome, evidence, and estimated cost; and a usage history sparkline.](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/tui-dashboard.png)

`agentacct tui` shows the same Shift Brief, receipts, and capacity in your shell; press `?` for the keys. The terminal app has no folder-grouping tab.

## Honest by design

agentacct is early alpha and would rather show you a gap than a guess.

- **Reported is not Verified.** An agent's claim can never dress up as verification.
- **Estimates are labeled.** `≈$` is a pricing-table estimate, `~$` a known-partial subtotal; there is no invoice access. [docs/usage-truth-table.md](docs/usage-truth-table.md) says what each path can prove.
- **Missing beats wrong.** Every join between usage and recorded work carries a confidence label (`exact`, `high`, `medium`, `low`); an unproven link shows as a gap, not a zero.
- **Support is per capability, not per logo.** Claude Code and Codex carry the fullest receipts today; the [coverage matrix](docs/coverage-matrix.md) rates every lane for OpenCode, Hermes, DeepSeek Harness, OpenClaw, and Cursor separately, and `agentacct capabilities agents` prints the same matrix from your installed version.
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
