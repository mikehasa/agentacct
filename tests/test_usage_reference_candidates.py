"""Usage references use the same manifest and atomic promotion gates as Dashboard."""

from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import struct
import zlib


APP = Path(__file__).resolve().parents[1] / "apps" / "agentacct"
SCRIPTS = APP / "Scripts"
REFERENCES = APP / "Tests" / "agentacctTests" / "ReferenceImages" / "macos-26-xcode-26.6-arm64-2x"
COMMIT = "1" * 40
RENDERER = "macos-test-arm64-2x"


def usage_inventory():
    spec = importlib.util.spec_from_file_location("candidate_tools", SCRIPTS / "dashboard_reference_candidate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.USAGE_IMAGES


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    data = zlib.compress((b"\0" + b"\xff" * width) * height)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", data) + chunk(b"IEND", b"")


def run_tool(name: str, *args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPTS / name), *map(str, args)],
                          text=True, capture_output=True, check=False)


def candidate(tmp_path: Path) -> Path:
    root = tmp_path / "candidate"
    images = root / "images"
    images.mkdir(parents=True)
    for name, (width, height) in usage_inventory().items():
        (images / name).write_bytes(png(width, height))
    result = run_tool("package-dashboard-reference-candidate", "--suite", "usage",
                      "--images", images, "--output", root / "manifest.json",
                      "--source-commit", COMMIT, "--renderer-id", RENDERER,
                      "--runner-image", "test", "--runner-image-version", "1")
    assert result.returncode == 0, result.stderr
    return root


def test_usage_bundle_rejects_dashboard_scope_and_preserves_other_references(tmp_path):
    root = candidate(tmp_path)
    args = ("--candidate", root, "--source-commit", COMMIT, "--renderer-id", RENDERER)
    wrong_scope = run_tool("validate-dashboard-reference-candidate", *args)
    assert wrong_scope.returncode != 0
    checked = run_tool("validate-dashboard-reference-candidate", "--suite", "usage", *args)
    assert checked.returncode == 0, checked.stderr
    refs = tmp_path / "references"
    destination = refs / RENDERER
    destination.mkdir(parents=True)
    for path in REFERENCES.glob("dashboard-*.png"):
        shutil.copyfile(path, destination / path.name)
    before = {path.name: path.read_bytes() for path in destination.iterdir()}
    unreviewed = run_tool("promote-dashboard-reference-candidate", "--suite", "usage", *args,
                         "--references-root", refs)
    assert unreviewed.returncode != 0
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == before
    promoted = run_tool("promote-dashboard-reference-candidate", "--suite", "usage", *args,
                        "--references-root", refs, "--reviewed")
    assert promoted.returncode == 0, promoted.stderr
    for name, data in before.items():
        assert (destination / name).read_bytes() == data
    for image in (root / "images").iterdir():
        assert (destination / image.name).read_bytes() == image.read_bytes()


def test_usage_manifest_tampering_cannot_be_promoted(tmp_path):
    root = candidate(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["artifacts"][0]["sha256"] = "0" * 64
    (root / "manifest.json").write_text(json.dumps(manifest))
    refs = tmp_path / "references"
    refs.mkdir()
    result = run_tool("promote-dashboard-reference-candidate", "--suite", "usage",
                      "--candidate", root, "--source-commit", COMMIT,
                      "--renderer-id", RENDERER, "--references-root", refs, "--reviewed")
    assert result.returncode != 0
    assert not list(refs.iterdir())
