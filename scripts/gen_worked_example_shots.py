#!/usr/bin/env python3
"""Regenerate the worked-example GUI screenshots from the same seed data the
example docs are generated from.

Seeds a throwaway store with the three worked-example tasks (imported from
``gen_worked_examples``), serves a daemon on it, and renders each receipt in the
REAL macOS app offscreen (``--snapshot``, which sets ``rendersStaticControls`` so
native button chrome draws as clean static primitives on any build machine).
Autocrops the trailing canvas and frames each in macOS window chrome, then writes:

    docs/examples/assets/example-a-receipts-table.png     (the receipts table = the compare view)
    docs/examples/assets/example-a-claude-code-receipt.png
    docs/examples/assets/example-a-codex-receipt.png
    docs/examples/assets/example-b-receipt.png

    PYTHONPATH=src <venv>/python scripts/gen_worked_example_shots.py

Requires the built app at apps/agentacct/.build/agentacct.app (build it with
apps/agentacct/Scripts/build-app.sh) plus Pillow. The screenshots are not under a
byte-drift test (OS rendering varies); regenerate them after a Work-pane redesign,
the same as the README app screenshots.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_BIN = REPO_ROOT / "apps" / "agentacct" / ".build" / "agentacct.app" / "Contents" / "MacOS" / "agentacct"
FAKE_HOME = "/tmp/agentacct-geo-gui-home"
STORE = Path(FAKE_HOME) / ".local" / "state" / "agentacct" / "state"
RAW = Path("/tmp/agentacct-geo-gui-shots")
OUT = REPO_ROOT / "docs" / "examples" / "assets"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import gen_worked_examples as g  # noqa: E402
from agentacct.api import _task_title, build_store_task_projection  # noqa: E402

# key -> (first-step title used by _task_title, output filename)
TARGETS = {
    "flaky": ("Reproduce the flaky total", "example-b-receipt.png"),
    "cc": ("Plan the retry policy", "example-a-claude-code-receipt.png"),
    "cx": ("Add backoff to the HTTP client", "example-a-codex-receipt.png"),
}


def seed() -> None:
    shutil.rmtree(FAKE_HOME, ignore_errors=True)
    STORE.mkdir(parents=True, exist_ok=True)
    g._seed_claude_code_a(STORE)
    g._seed_codex_a(STORE)
    g._seed_example_b(STORE)
    g._backdate(STORE)


def task_ids() -> dict[str, str]:
    proj = build_store_task_projection(STORE)
    by_title = {
        _task_title(t): str(t["public_task_id"])
        for t in proj.get("tasks", [])
        if isinstance(t, dict) and t.get("public_task_id")
    }
    return {key: by_title.get(title, "") for key, (title, _f) in TARGETS.items()}


def _daemon_env() -> dict:
    env = {**os.environ, "HOME": FAKE_HOME, "PYTHONPATH": str(REPO_ROOT / "src"),
           "AGENTACCT_TUI_AUTO_IMPORT": "0", "AGENTACCT_SCAN_GLOBAL_LIMITS": "0"}
    for v in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "AGENTACCT_STORE_DIR",
              "AGENTACCT_GLOBAL_STORE_DIR", "CODEX_HOME", "OPENCODE_DATA_DIR", "HERMES_HOME",
              "OPENCLAW_DIR", "CURSOR_HOME"):
        env.pop(v, None)
    return env


def start_daemon():
    disc = STORE / "local-api.json"
    disc.unlink(missing_ok=True)
    d = subprocess.Popen([sys.executable, "-m", "agentacct.cli", "serve", "--store-dir", str(STORE)],
                         env=_daemon_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(120):
        if disc.exists():
            break
        if d.poll() is not None:
            raise SystemExit("demo daemon exited before writing its discovery file")
        time.sleep(0.5)
    else:
        raise SystemExit("demo daemon never wrote its discovery file")
    time.sleep(1.0)
    return d


def snapshot(task_id: str, tag: str) -> Path:
    dst = RAW / tag
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "AGENTACCT_STORE_DIR": str(STORE)}
    if task_id:
        env["AGENTACCT_SNAPSHOT_TASK"] = task_id
    r = subprocess.run([str(APP_BIN), "--snapshot", str(dst)], env=env,
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise SystemExit(f"snapshot failed ({tag}): {r.stdout}\n{r.stderr}")
    return dst


def autocrop_and_frame(src: Path, dst: Path) -> None:
    from PIL import Image, ImageChops
    im = Image.open(src).convert("RGB")
    bg = Image.new("RGB", im.size, im.getpixel((2, 2)))
    bbox = ImageChops.difference(im, bg).getbbox()
    if bbox:
        pad = 8
        l, t, r2, b = bbox
        im = im.crop((max(0, l - pad), max(0, t - pad), min(im.width, r2 + pad), min(im.height, b + pad)))
    dst.parent.mkdir(parents=True, exist_ok=True)
    im.save(dst)
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        from frame_screenshots import frame
        frame(dst)
    except Exception as exc:  # framing is polish; keep the plain crop if it is unavailable
        print(f"  (frame skipped for {dst.name}: {exc})")


def main() -> None:
    if not APP_BIN.exists():
        raise SystemExit(f"app binary not found: {APP_BIN}\n  build it: apps/agentacct/Scripts/build-app.sh")
    OUT.mkdir(parents=True, exist_ok=True)
    print("seeding worked-example store…")
    seed()
    ids = task_ids()
    daemon = start_daemon()
    try:
        first_tag = None
        for key, (_title, filename) in TARGETS.items():
            tag_dir = snapshot(ids[key], key)
            first_tag = first_tag or tag_dir
            autocrop_and_frame(tag_dir / "window-work-wide-light.png", OUT / filename)
            print(f"  wrote {(OUT / filename).relative_to(REPO_ROOT)}")
        # The receipts TABLE (all three) — Example A's at-a-glance comparison.
        autocrop_and_frame(first_tag / "window-work-table-light.png", OUT / "example-a-receipts-table.png")
        print(f"  wrote {(OUT / 'example-a-receipts-table.png').relative_to(REPO_ROOT)}")
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
    shutil.rmtree(FAKE_HOME, ignore_errors=True)
    print(f"done -> {OUT.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
