# Packaging — distributable app for non-developers

This directory builds a self-contained `agentacct.app` + DMG that a
non-developer can install without Python, pipx, or a terminal. The app embeds a
**frozen standalone CLI** and drives the normal `agentacct onboard` from a
one-click screen.

## Why a frozen CLI

The app is a GUI over the daemon; it cannot replace the CLI. The MCP servers the
coding agents connect to (`agentacct mcp serve`), the daemon (`agentacct serve`),
and the recording hooks are all the Python CLI. So a machine with no Python
needs the CLI as a standalone binary. PyInstaller freezes it (interpreter and
all) into one directory; the app ships that directory and installs it on first
setup.

The frozen binary is a drop-in for a pip/pipx `agentacct` — including the two
Python-interpreter forms the Claude Code hooks invoke it with
(`<cli> -m agentacct.statusline_hook`, `<cli> <hook>.py`), handled by the
interpreter-emulation entry in `pyinstaller_entry.py`.

## Scripts

| Script | What it does |
| --- | --- |
| `freeze-cli.sh` | Freeze the CLI to `packaging/dist/agentacct/` (onedir). Requires a clean source tree, records its provenance, then smoke-tests `mcp serve` / `serve` / `onboard`. |
| `build-dmg.sh` | Full release: freeze → build the app (embeds the CLI) → sign (if configured) → DMG → notarize (if configured). Output in `packaging/release/`. |
| `pyinstaller_entry.py` | The frozen binary's entry point (CLI + hook-interpreter emulation). |
| `entitlements.plist` | Hardened-runtime entitlements the embedded PyInstaller binary needs to run notarized. |

`apps/agentacct/Scripts/build-app.sh` embeds `packaging/dist/agentacct/` into
`agentacct.app/Contents/Resources/cli/` only when its recorded commit and clean
source description match the app build. Missing CLI output remains a normal
fast dev build; stale, dirty, or unstamped output fails closed. `build-dmg.sh`
always refreshes the frozen CLI first.

## What the app's setup does

`SetupModel` / `SetupSheet` (in the app) drive:

1. Copy the embedded CLI → `~/.local/share/agentacct/cli/` (stable location; an
   app upgrade replaces it in place, so every registration stays valid).
2. Write a wrapper at `~/.local/bin/agentacct` (on PATH for terminal use).
3. Run `agentacct onboard --agent auto --yes` from the installed binary — which
   registers the MCP servers, hooks, and standing instructions and stamps the
   stable installed path into every config.

## Build a DMG now (unsigned)

```bash
bash packaging/build-dmg.sh
# -> packaging/release/agentacct-<version>.dmg  (unsigned)
```

An unsigned DMG installs, but first launch needs right-click → Open to get past
Gatekeeper. Fine for testing and local use.

## Sign + notarize (once a Developer ID exists)

No code changes — the same `build-dmg.sh` signs and notarizes when the
environment is set:

```bash
# one-time: store notarytool credentials in the keychain
xcrun notarytool store-credentials agentacct-notary \
  --apple-id you@example.com --team-id TEAMID --password <app-specific-password>

export DEVELOPER_ID="Developer ID Application: Your Name (TEAMID)"
export NOTARY_PROFILE="agentacct-notary"
bash packaging/build-dmg.sh
# -> signed, notarized, stapled DMG that opens with a normal double-click
```

Signing is inside-out: every dylib/.so and the binary in the embedded CLI, then
the `.app`, all with the hardened runtime + `entitlements.plist` (a frozen
Python binary needs `disable-library-validation`, `allow-jit`, and
`allow-unsigned-executable-memory`). Notarization eliminates the ~15s first-run
Gatekeeper scan an unsigned binary otherwise pays.
