#!/usr/bin/env python3
"""Dependency-free CLI tests for downloaded reference candidate validation."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile


TESTS_DIR = Path(__file__).resolve().parent
APP_DIR = TESTS_DIR.parent
PACKAGER = APP_DIR / "Scripts" / "package-dashboard-reference-candidate"
VALIDATOR = APP_DIR / "Scripts" / "validate-dashboard-reference-candidate"
REFERENCE_ROOT = TESTS_DIR / "agentacctTests" / "ReferenceImages"
SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER_COMMIT = "89abcdef0123456789abcdef0123456789abcdef"
RENDERER_ID = "macos-test-build-xcode-test-build-arm64-2x"


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def require_failure(result: subprocess.CompletedProcess[str], message: str) -> None:
    require(result.returncode != 0, message)


def package_candidate(candidate: Path, images: Path) -> None:
    candidate.mkdir()
    shutil.copytree(images, candidate / "images")
    result = run(
        [
            str(PACKAGER),
            "--images",
            str(candidate / "images"),
            "--output",
            str(candidate / "manifest.json"),
            "--source-commit",
            SOURCE_COMMIT,
            "--renderer-id",
            RENDERER_ID,
            "--runner-image",
            "macos26",
            "--runner-image-version",
            "test-image",
        ]
    )
    require(result.returncode == 0, result.stderr)


def validate(candidate: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    values = {
        "source_commit": SOURCE_COMMIT,
        "renderer_id": RENDERER_ID,
        **overrides,
    }
    return run(
        [
            str(VALIDATOR),
            "--candidate",
            str(candidate),
            "--source-commit",
            values["source_commit"],
            "--renderer-id",
            values["renderer_id"],
        ]
    )


def main() -> None:
    reference_directories = sorted(path for path in REFERENCE_ROOT.iterdir() if path.is_dir())
    require(reference_directories, "expected at least one reference directory")

    with tempfile.TemporaryDirectory(prefix="agentacct-candidate-intake-tests-") as temporary:
        root = Path(temporary)
        candidate = root / "candidate"
        package_candidate(candidate, reference_directories[0])

        valid = validate(candidate)
        require(valid.returncode == 0, valid.stderr)
        require("(4 images)" in valid.stdout, valid.stdout)

        wrong_source = validate(candidate, source_commit=OTHER_COMMIT)
        require_failure(wrong_source, "validator accepted the wrong expected source")
        require("candidate source commit" in wrong_source.stderr, wrong_source.stderr)

        wrong_renderer = validate(candidate, renderer_id="macos-other-renderer")
        require_failure(wrong_renderer, "validator accepted the wrong expected renderer")
        require("candidate renderer" in wrong_renderer.stderr, wrong_renderer.stderr)

        tampered = root / "tampered"
        shutil.copytree(candidate, tampered)
        image = tampered / "images" / "dashboard-minimum-dark.png"
        payload = bytearray(image.read_bytes())
        payload[len(payload) // 2] ^= 1
        image.write_bytes(payload)
        tampered_result = validate(tampered)
        require_failure(tampered_result, "validator accepted tampered image bytes")

        duplicate_key = root / "duplicate-key"
        shutil.copytree(candidate, duplicate_key)
        manifest_path = duplicate_key / "manifest.json"
        manifest_text = manifest_path.read_text(encoding="utf-8").rstrip()
        manifest_path.write_text(
            manifest_text[:-1] + ', "schema_version": 1}\n', encoding="utf-8"
        )
        duplicate_result = validate(duplicate_key)
        require_failure(duplicate_result, "validator accepted a duplicate manifest key")
        require("duplicate key" in duplicate_result.stderr, duplicate_result.stderr)

        unknown_field = root / "unknown-field"
        shutil.copytree(candidate, unknown_field)
        unknown_manifest_path = unknown_field / "manifest.json"
        unknown_manifest = json.loads(unknown_manifest_path.read_text(encoding="utf-8"))
        unknown_manifest["untrusted"] = True
        unknown_manifest_path.write_text(json.dumps(unknown_manifest), encoding="utf-8")
        unknown_result = validate(unknown_field)
        require_failure(unknown_result, "validator accepted an unknown manifest field")
        require("fields mismatch" in unknown_result.stderr, unknown_result.stderr)

        extra_root_file = root / "extra-root-file"
        shutil.copytree(candidate, extra_root_file)
        (extra_root_file / "notes.txt").write_text("unexpected", encoding="utf-8")
        extra_result = validate(extra_root_file)
        require_failure(extra_result, "validator accepted an extra root file")
        require("root inventory mismatch" in extra_result.stderr, extra_result.stderr)

        symlinked_manifest = root / "symlinked-manifest"
        shutil.copytree(candidate, symlinked_manifest)
        linked_manifest = symlinked_manifest / "manifest.json"
        linked_manifest.unlink()
        linked_manifest.symlink_to(candidate / "manifest.json")
        symlink_result = validate(symlinked_manifest)
        require_failure(symlink_result, "validator accepted a symlinked manifest")
        require("regular file" in symlink_result.stderr, symlink_result.stderr)

    print("dashboard reference candidate intake CLI tests passed")


if __name__ == "__main__":
    main()
