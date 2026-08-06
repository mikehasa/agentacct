#!/bin/bash
# Freeze the agentacct Python CLI into a standalone, no-Python-required binary.
#
# Output: packaging/dist/agentacct/  (PyInstaller *onedir* bundle — the
# executable plus its _internal/ libs). onedir over onefile on purpose: onefile
# re-extracts ~24MB to a temp dir on EVERY launch (seconds of latency), and this
# binary is launched constantly — by every MCP connection, every hook, the
# daemon. onedir starts in ~0.5s warm.
#
# The frozen binary is a drop-in for a pip/pipx `agentacct`: it runs the CLI, the
# `serve` daemon (uvicorn/fastapi), the `mcp serve` stdio server, `onboard`, AND
# — via the interpreter-emulation entry (packaging/pyinstaller_entry.py) — the
# Claude Code hooks that invoke it as `<cli> -m agentacct.statusline_hook` and
# `<cli> <script>.py`.
#
# This does NOT sign or notarize. The app-bundle build (build-app.sh) embeds the
# output; signing/notarization happen at DMG time once a Developer ID is set up.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
BUILD_VENV="$HERE/.buildvenv"
DIST_DIR="$HERE/dist"
WORK_DIR="$HERE/build"

echo "==> agentacct CLI freeze (PyInstaller onedir)"
cd "$REPO_ROOT"

# Isolated build venv so the freeze never touches the dev/live environment.
if [[ ! -x "$BUILD_VENV/bin/pyinstaller" ]]; then
    echo "==> creating build venv + installing package + pyinstaller"
    python3 -m venv "$BUILD_VENV"
    "$BUILD_VENV/bin/pip" install --quiet --upgrade pip
    "$BUILD_VENV/bin/pip" install --quiet . pyinstaller
else
    # Refresh the package so the frozen binary tracks the current source.
    echo "==> refreshing package in existing build venv"
    "$BUILD_VENV/bin/pip" install --quiet --upgrade . >/dev/null
fi

VERSION="$("$BUILD_VENV/bin/agentacct" --version | awk '{print $2}')"
echo "==> freezing agentacct $VERSION"

rm -rf "$DIST_DIR" "$WORK_DIR" "$HERE"/*.spec

# --collect-all for packages with dynamic imports / data files PyInstaller's
# static analysis misses: agentacct (package-data: INSTALL.md, example JSON),
# uvicorn (loop/protocol autodiscovery), textual (CSS assets). pydantic submods
# + the uvicorn auto hidden-imports cover the runtime dispatch tables.
"$BUILD_VENV/bin/pyinstaller" --noconfirm --onedir --name agentacct \
    --distpath "$DIST_DIR" --workpath "$WORK_DIR" --specpath "$HERE" \
    --collect-all agentacct \
    --collect-all uvicorn \
    --collect-all textual \
    --collect-submodules pydantic \
    --hidden-import=agentacct.statusline_hook \
    --hidden-import=uvicorn.loops.auto \
    --hidden-import=uvicorn.protocols.http.auto \
    --hidden-import=uvicorn.protocols.websockets.auto \
    --hidden-import=uvicorn.lifespan.on \
    "$HERE/pyinstaller_entry.py"

BIN="$DIST_DIR/agentacct/agentacct"
echo "==> smoke-testing the frozen binary"
"$BIN" --version
"$BIN" mcp serve --help >/dev/null && echo "    mcp serve: ok"
"$BIN" serve --help >/dev/null && echo "    serve: ok"
"$BIN" onboard --help >/dev/null && echo "    onboard: ok"

echo "==> done: $DIST_DIR/agentacct  ($(du -sh "$DIST_DIR/agentacct" | awk '{print $1}'))"
