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
| `build-dmg.sh` | Freeze → build the app (embeds the CLI) → sign (if configured) → DMG → notarize (if configured). `--release` validates signing and notary credentials before build work. Release output goes to `packaging/release/`; no-flag local output is isolated in `packaging/local-build/`. |
| `verify-dmg.sh` | Validate stapling, mount the exact DMG read-only, discover its actual mount point, then pin the App to an explicitly trusted Apple Team before verifying App/embedded-CLI version and source identity. |
| `rename-no-replace.c` | Tiny macOS activation helper compiled by the build scripts; `renamex_np(RENAME_EXCL)` prevents a concurrently-created destination from turning a rename into a successful nested copy. |
| `pyinstaller_entry.py` | The frozen binary's entry point (CLI + hook-interpreter emulation). |
| `entitlements.plist` | Hardened-runtime entitlements the embedded PyInstaller binary needs to run notarized. |

`apps/agentacct/Scripts/build-app.sh` embeds `packaging/dist/agentacct/` into
`agentacct.app/Contents/Resources/cli/` only when its recorded commit and clean
source description match the app build. Missing CLI output remains a normal
fast dev build; stale, dirty, or unstamped output fails closed. `build-dmg.sh`
always refreshes the frozen CLI first.

## What setup and launch-time CLI sync do

The packaged app keeps registrations stable without overwriting a live frozen
CLI in place:

```text
~/.local/bin/agentacct                               stable PATH wrapper
  -> ~/.local/share/agentacct/cli/agentacct          stable launcher
       reads ~/.local/share/agentacct/cli/.agentacct-app-target
  -> ~/.local/share/agentacct/cli-versions/<version-commit-uuid>/
                                                     immutable onedir target
```

On first setup, `SetupModel` / `SetupSheet` validate the embedded CLI's clean
source provenance and release version, stage one complete immutable target,
install the stable launchers, and run
`agentacct onboard --agent auto --yes` through that stable path. MCP servers,
hooks, and standing instructions therefore keep pointing at the same launcher.

Before the first local data request on every later packaged-app launch, the app
checks the embedded CLI against the app-owned installed CLI. When the verified
bundle contains a newer version, it stages and validates the complete onedir
before stopping an app-managed runtime, atomically switches only
`.agentacct-app-target`, and then restarts that runtime. A failed start rolls
back to the verified previous target when safe; identity or ownership changes
fail closed.

An exactly verified, agentacct-managed `dev.agentacct.runtime` LaunchAgent is
unloaded and reloaded as part of the upgrade transaction. Its registration uses
the verified stable launcher so later launches do not pin an old immutable
target. Unknown or modified LaunchAgent entries fail closed before changes;
the app does not rewrite a custom autostart configuration.

If the App process exits or is killed after stopping a managed recorder, an
owner-only transaction journal lets the next launch resume or safely restore
that exact runtime. This is process-crash recovery, not a sudden-power-loss
guarantee: the journal and parent directory are not fsync'd.

Previous version directories, legacy side files, and the legacy binary backup
are retained. An MCP server or hook that was already running can therefore keep
using its original immutable files while new launches follow the stable launcher
to the new target. The app does not replace an onedir in place and does not take
over a user-managed/pipx wrapper.

## Build a DMG now (unsigned)

```bash
bash packaging/build-dmg.sh
# -> packaging/local-build/agentacct-<version>.dmg  (unsigned)
```

An unsigned DMG installs, but first launch needs right-click → Open to get past
Gatekeeper. Fine for testing and local use.

This no-flag mode is intentionally for local testing only and never replaces
`packaging/release/`. Its completion marker is
`.agentacct-local-build-complete`, recording `artifact_class=local` plus the
actual signing/notarization state. A distributable release must use
`bash packaging/build-dmg.sh --release`; it validates the named signing identity
and authenticates the keychain notary profile before freezing or building.
Both modes require a clean source tree because the embedded CLI provenance must
identify one exact commit.

## Sign + notarize (once a Developer ID exists)

No code changes — the same `build-dmg.sh` signs and notarizes when the
environment is set:

```bash
# one-time: store notarytool credentials in the keychain
xcrun notarytool store-credentials agentacct-notary \
  --apple-id you@example.com --team-id TEAMID --password <app-specific-password>

export DEVELOPER_ID="Developer ID Application: Your Name (TEAMID)"
export RELEASE_TEAM_ID="TEAMID"  # trusted out-of-band; never derive it from the candidate DMG
export NOTARY_PROFILE="agentacct-notary"
bash packaging/build-dmg.sh --release
# -> signed, notarized, stapled DMG that opens with a normal double-click
```

Only this mode publishes `packaging/release/` and writes
`.agentacct-release-complete` (`artifact_class=release`, exact version/source,
`signed=true`, `notarized=true`). Publication and App installation compile the
no-replace rename helper, so a destination that appears concurrently fails
closed without nesting the new directory or deleting the sole backup. The
release build also mounts and verifies the exact completed DMG through
`verify-dmg.sh` before publication. The verifier requires the expected Apple
Team ID as a trust input and checks the Developer ID Application certificate
requirement before it executes the embedded CLI; it never trusts a Team ID
reported by the candidate DMG itself.

Signing is inside-out: every dylib/.so and the binary in the embedded CLI, then
the `.app`, all with the hardened runtime + `entitlements.plist` (a frozen
Python binary needs `disable-library-validation`, `allow-jit`, and
`allow-unsigned-executable-memory`). Notarization eliminates the ~15s first-run
Gatekeeper scan an unsigned binary otherwise pays.
