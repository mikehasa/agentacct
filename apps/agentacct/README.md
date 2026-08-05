# agentacct — the menu bar app (skeleton)

A thin SwiftUI `MenuBarExtra` shell over the daemon's `/v1` lane: today's cost
in the menu bar; usage windows, provider limit bars, plan-calibration status,
and recent sessions (with per-session weekly-plan share where calibrated) in
the dropdown. All aggregation and honesty logic stays in the Python daemon —
this app only renders what `/v1/glance` vouches for.

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

## Skeleton status / roadmap

- [x] discovery + bearer + version handshake + 30s poll
- [x] menu bar: today's cost (complete `$` / partial `~$` / `—`, never a fake $0)
- [x] dropdown: usage windows · limit bars with stale marker · recent sessions
      with status glyphs and calibrated-only plan shares · calibrating footnote
- [ ] adaptive poll cadence (menu-open recency / Low Power Mode — CodexBar's
      2–30 min policy)
- [ ] session drill-down, notifications on limit thresholds
- [ ] Sparkle updates, Developer ID signing + notarization, brew cask
- [ ] app icon + proper menu bar iconography (text-only today)

A `contrib/swiftbar/agentacct.30s.sh` plugin covers the same glance for
SwiftBar/xbar users (and doubles as a reference client for the API).
