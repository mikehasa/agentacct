"""Visual suites share fixed-inventory, manifest and atomic promotion gates."""

from __future__ import annotations

import json
import re
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import struct
import zlib

import pytest


APP = Path(__file__).resolve().parents[1] / "apps" / "agentacct"
SCRIPTS = APP / "Scripts"
REFERENCES = APP / "Tests" / "agentacctTests" / "ReferenceImages" / "macos-26-xcode-26.6-arm64-2x"
COMMIT = "1" * 40
RENDERER = "macos-test-arm64-2x"


def suite_inventory(suite="usage"):
    spec = importlib.util.spec_from_file_location("candidate_tools", SCRIPTS / "dashboard_reference_candidate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SUITE_IMAGES[suite]


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    data = zlib.compress((b"\0" + b"\xff" * width) * height)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", data) + chunk(b"IEND", b"")


def run_tool(name: str, *args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPTS / name), *map(str, args)],
                          text=True, capture_output=True, check=False)


def candidate(tmp_path: Path, suite: str = "usage") -> Path:
    root = tmp_path / "candidate"
    images = root / "images"
    images.mkdir(parents=True)
    for name, (width, height) in suite_inventory(suite).items():
        (images / name).write_bytes(png(width, height))
    result = run_tool("package-dashboard-reference-candidate", "--suite", suite,
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
    # Exercise the real upgrade from the complete older 20-image Usage suite,
    # alongside the unaffected Dashboard suite, to the expanded 24-image suite.
    for path in REFERENCES.glob("*.png"):
        if path.name.startswith("dashboard-") or (path.name.startswith("usage-") and not path.name.startswith("usage-day-clients-")):
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
        if not name.startswith("usage-"):
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


@pytest.mark.parametrize("suite,expected_count", [("dashboard", 12), ("work", 84), ("sources", 4)])
def test_candidate_inventory_matches_swift_review_contract(suite, expected_count):
    inventory = suite_inventory(suite)
    swift = (APP / "Tests" / "agentacctTests" / f"{suite.title()}VisualRegressionTests.swift").read_text()
    filenames = re.findall(r'"(' + suite + r'-[^"\n]+\.png)"', swift)
    assert len(inventory) == expected_count
    assert set(inventory) == set(filenames)
    # These independent reviewed PNG headers catch drift in the trusted dimensions.
    for name, dimensions in inventory.items():
        assert struct.unpack(">II", (REFERENCES / name).read_bytes()[16:24]) == dimensions


@pytest.mark.parametrize("suite", ["dashboard", "work", "sources"])
def test_complete_suite_promotes_without_touching_other_suites(tmp_path, suite):
    root = candidate(tmp_path, suite)
    args = ("--candidate", root, "--source-commit", COMMIT, "--renderer-id", RENDERER)
    checked = run_tool("validate-dashboard-reference-candidate", "--suite", suite, *args)
    assert checked.returncode == 0, checked.stderr
    wrong_suite = "work" if suite != "work" else "sources"
    assert run_tool("validate-dashboard-reference-candidate", "--suite", wrong_suite, *args).returncode != 0
    refs = tmp_path / "references"
    destination = refs / RENDERER
    shutil.copytree(REFERENCES, destination)
    before = {path.name: path.read_bytes() for path in destination.iterdir()}
    # Missing review cannot create, remove or overwrite any reference.
    rejected = run_tool("promote-dashboard-reference-candidate", "--suite", suite, *args,
                        "--references-root", refs)
    assert rejected.returncode != 0
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == before
    promoted = run_tool("promote-dashboard-reference-candidate", "--suite", suite, *args,
                        "--references-root", refs, "--reviewed")
    assert promoted.returncode == 0, promoted.stderr
    for name, data in before.items():
        if name not in suite_inventory(suite):
            assert (destination / name).read_bytes() == data
    for image in (root / "images").iterdir():
        assert (destination / image.name).read_bytes() == image.read_bytes()
    again = run_tool("promote-dashboard-reference-candidate", "--suite", suite, *args,
                     "--references-root", refs, "--reviewed")
    assert again.returncode == 0, again.stderr
    assert "no files changed" in again.stdout


@pytest.mark.parametrize("suite", ["work", "sources"])
@pytest.mark.parametrize("damage", ["missing", "tampered_hash", "extra"])
def test_invalid_suite_candidate_preserves_existing_references(tmp_path, suite, damage):
    root = candidate(tmp_path, suite)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    image = root / "images" / manifest["artifacts"][0]["filename"]
    if damage == "missing":
        image.unlink()
    elif damage == "extra":
        shutil.copyfile(image, root / "images" / "unreviewed.png")
    else:
        manifest["artifacts"][0]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest))
    refs = tmp_path / "references"
    destination = refs / RENDERER
    shutil.copytree(REFERENCES, destination)
    before = {path.name: path.read_bytes() for path in destination.iterdir()}
    rejected = run_tool("promote-dashboard-reference-candidate", "--suite", suite,
                        "--candidate", root, "--source-commit", COMMIT,
                        "--renderer-id", RENDERER, "--references-root", refs, "--reviewed")
    assert rejected.returncode != 0
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == before
    assert sorted(path.name for path in refs.iterdir()) == [RENDERER]


def test_dashboard_promotes_complete_prior_inventory_but_rejects_partial(tmp_path):
    root = candidate(tmp_path, "dashboard")
    refs = tmp_path / "references"
    destination = refs / RENDERER
    destination.mkdir(parents=True)
    older = {f"dashboard-{viewport}-{appearance}.png"
             for viewport in ("minimum", "reference") for appearance in ("light", "dark")}
    for name in older:
        shutil.copyfile(REFERENCES / name, destination / name)
    missing = "dashboard-reference-light.png"
    (destination / missing).unlink()
    args = ("--suite", "dashboard", "--candidate", root, "--source-commit", COMMIT,
            "--renderer-id", RENDERER, "--references-root", refs, "--reviewed")
    before = {path.name: path.read_bytes() for path in destination.iterdir()}
    assert run_tool("promote-dashboard-reference-candidate", *args).returncode != 0
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == before
    shutil.copyfile(REFERENCES / missing, destination / missing)
    promoted = run_tool("promote-dashboard-reference-candidate", *args)
    assert promoted.returncode == 0, promoted.stderr
    assert {path.name for path in destination.iterdir()} == set(suite_inventory("dashboard"))
