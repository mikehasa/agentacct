# agentacct — the macOS app

A SwiftUI macOS app over the daemon's `/v1` lane. The menu bar shows today's
cost at a glance; its dropdown carries usage windows, provider limit bars,
plan-calibration status, and recent sessions (with per-session weekly-plan
share where calibrated). Click through to the full window: a **Dashboard**
home, a Task-primary **Work** pane whose centerpiece is the **Work Receipt**
for each task, a **Usage** pane (by agent and by model), and a **Limits** pane.
All aggregation and honesty logic stays in the Python daemon — the app only
renders what `/v1` vouches for.

## How it connects

No configuration. The daemon (`agentacct serve`, spawned by `agentacct start`)
claims `<store>/local-api.json` (0600) with its actual port and a per-boot
bearer token; the app reads that file (default store
`~/.local/state/agentacct/state`, override with `AGENTACCT_STORE_DIR`) and
polls `GET /v1/glance` every 30s. A missing file or dead port renders as a
labeled disconnected state with the start command; a schema mismatch renders
as a labeled incompatible state (never a parse error); a 401 re-reads the
discovery file (the daemon restarted with a fresh token).

## Build & run

Requires Xcode (macOS 14+):

```bash
cd apps/agentacct
./Scripts/build-app.sh
open .build/agentacct.app
```

Or during development: `swift run` (menu bar item appears; Ctrl-C to quit).
The Dashboard leads with recent task outcomes and their evidence, followed by
review, active work, plan headroom, and usage history.

Packaged builds show `v<release> · <commit>` in the menu footer. The release
and bundle version come from the repository's `pyproject.toml`, independent of
clone depth. The exact commit and dirty state come from Git (including
untracked source), so the installed app can identify the source that produced
it.

## Test the dashboard UI

The deterministic snapshot harness renders the real dashboard from synthetic
API payloads in light and dark mode, without a running daemon or personal
account data. See [Tests/README.md](Tests/README.md) for the two-command quick
start, artifact matrix, extension guide, determinism rules, and review
checklist.

## Status / roadmap

- [x] discovery + bearer + version handshake + 30s poll
- [x] menu bar: today's cost (complete `$` / partial `~$` / `—`, never a fake $0)
- [x] dropdown: usage windows · live limit bars (stale accounts hidden) ·
      root-only recent sessions with status glyphs and calibrated-only plan
      shares · refresh spinner + updated-ago · click a session to open the
      full window
- [x] the full window: a Dashboard home · a Task-primary Work pane (root task
      list → Work Receipt detail: what ran, files touched, tools, usage, work
      items with check evidence, attribution, subagent rollup) · Usage by agent
      and by model (30d cube) · Limits with a show-stale toggle
- [ ] adaptive poll cadence (menu-open recency / Low Power Mode — CodexBar's
      2–30 min policy)
- [x] usage window picker (7d/30d/90d)
- [ ] per-session plan share in the window
- [ ] notifications on limit thresholds
- [ ] Sparkle updates, Developer ID signing + notarization, brew cask
- [ ] app icon + proper menu bar iconography (text-only today)

A `contrib/swiftbar/agentacct.30s.sh` plugin covers the same glance for
SwiftBar/xbar users (and doubles as a reference client for the API).
