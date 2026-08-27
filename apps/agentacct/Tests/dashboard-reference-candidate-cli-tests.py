#!/usr/bin/env python3
"""Dependency-free CLI tests for dashboard reference candidate packaging."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


TESTS_DIR = Path(__file__).resolve().parent
APP_DIR = TESTS_DIR.parent
PACKAGER = APP_DIR / "Scripts" / "package-dashboard-reference-candidate"
REFERENCE_ROOT = TESTS_DIR / "agentacctTests" / "ReferenceImages"
SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
RENDERER_ID = "macos-test-build-xcode-test-build-arm64-2x"
EXPECTED_DIMENSIONS = {
    "dashboard-minimum-dark.png": (1920, 1120),
    "dashboard-minimum-light.png": (1920, 1120),
    "dashboard-reference-dark.png": (2240, 1600),
    "dashboard-reference-light.png": (2240, 1600),
}


def run_packager(images: Path, output: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    values = {
        "source_commit": SOURCE_COMMIT,
        "renderer_id": RENDERER_ID,
        "runner_image": "macos-test-arm64",
        "runner_image_version": "20260827.1",
        **overrides,
    }
    return subprocess.run(
        [
            str(PACKAGER),
            "--images",
            str(images),
            "--output",
            str(output),
            "--source-commit",
            values["source_commit"],
            "--renderer-id",
            values["renderer_id"],
            "--runner-image",
            values["runner_image"],
            "--runner-image-version",
            values["runner_image_version"],
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def require_failure(result: subprocess.CompletedProcess[str], message: str) -> None:
    require(result.returncode != 0, message)


def main() -> None:
    reference_directories = sorted(
        path
        for path in REFERENCE_ROOT.iterdir()
        if path.is_dir()
        and {image.name for image in path.glob("*.png")} == set(EXPECTED_DIMENSIONS)
    )
    require(reference_directories, "expected at least one complete reference directory")

    with tempfile.TemporaryDirectory(prefix="agentacct-candidate-tests-") as temporary:
        temporary_root = Path(temporary)
        images = temporary_root / "images"
        shutil.copytree(reference_directories[0], images)
        output = temporary_root / "manifest.json"

        first = run_packager(images, output)
        require(first.returncode == 0, first.stderr)
        first_payload = output.read_bytes()
        manifest = json.loads(first_payload)
        require(manifest["schema_version"] == 1, "manifest schema version changed")
        require(manifest["source_commit"] == SOURCE_COMMIT, "manifest lost source identity")
        require(manifest["renderer_id"] == RENDERER_ID, "manifest lost renderer identity")
        require(
            manifest["runner_image"]
            == {"name": "macos-test-arm64", "version": "20260827.1"},
            "manifest lost hosted runner image identity",
        )

        artifacts = manifest["artifacts"]
        require(
            [artifact["filename"] for artifact in artifacts] == sorted(EXPECTED_DIMENSIONS),
            "manifest artifact order is not deterministic",
        )
        for artifact in artifacts:
            filename = artifact["filename"]
            require(
                (artifact["pixel_width"], artifact["pixel_height"])
                == EXPECTED_DIMENSIONS[filename],
                f"manifest recorded incorrect dimensions for {filename}",
            )
            expected_hash = hashlib.sha256((images / filename).read_bytes()).hexdigest()
            require(artifact["sha256"] == expected_hash, f"manifest hash is wrong for {filename}")

        second = run_packager(images, output)
        require(second.returncode == 0, second.stderr)
        require(output.read_bytes() == first_payload, "identical input changed manifest bytes")

        invalid_commit = run_packager(
            images, temporary_root / "invalid-commit.json", source_commit="main"
        )
        require_failure(invalid_commit, "packager accepted a branch instead of a full commit")
        require("full lowercase 40-character Git SHA" in invalid_commit.stderr, invalid_commit.stderr)

        missing_images = temporary_root / "missing"
        shutil.copytree(images, missing_images)
        (missing_images / "dashboard-minimum-dark.png").unlink()
        missing = run_packager(missing_images, temporary_root / "missing.json")
        require_failure(missing, "packager accepted an incomplete matrix")
        require("inventory mismatch" in missing.stderr, missing.stderr)

        unexpected_images = temporary_root / "unexpected"
        shutil.copytree(images, unexpected_images)
        shutil.copy(
            unexpected_images / "dashboard-minimum-dark.png",
            unexpected_images / "unreviewed.png",
        )
        unexpected = run_packager(unexpected_images, temporary_root / "unexpected.json")
        require_failure(unexpected, "packager accepted an unexpected PNG")
        require("unexpected: unreviewed.png" in unexpected.stderr, unexpected.stderr)

        extra_file_images = temporary_root / "extra-file"
        shutil.copytree(images, extra_file_images)
        (extra_file_images / "notes.txt").write_text("not part of the candidate", encoding="utf-8")
        extra_file = run_packager(extra_file_images, temporary_root / "extra-file.json")
        require_failure(extra_file, "packager accepted an unexpected non-image file")
        require("unexpected: notes.txt" in extra_file.stderr, extra_file.stderr)

        wrong_dimensions = temporary_root / "wrong-dimensions"
        shutil.copytree(images, wrong_dimensions)
        shutil.copy(
            wrong_dimensions / "dashboard-minimum-dark.png",
            wrong_dimensions / "dashboard-reference-dark.png",
        )
        wrong_size = run_packager(wrong_dimensions, temporary_root / "wrong-size.json")
        require_failure(wrong_size, "packager accepted incorrect dimensions")
        require("must be 2240x1600; got 1920x1120" in wrong_size.stderr, wrong_size.stderr)

        malformed_images = temporary_root / "malformed"
        shutil.copytree(images, malformed_images)
        (malformed_images / "dashboard-reference-light.png").write_bytes(b"not a PNG")
        malformed = run_packager(malformed_images, temporary_root / "malformed.json")
        require_failure(malformed, "packager accepted a malformed PNG")
        require("is not a PNG" in malformed.stderr, malformed.stderr)

        corrupt_images = temporary_root / "corrupt"
        shutil.copytree(images, corrupt_images)
        corrupt_image = corrupt_images / "dashboard-reference-light.png"
        corrupt_bytes = bytearray(corrupt_image.read_bytes())
        corrupt_bytes[len(corrupt_bytes) // 2] ^= 1
        corrupt_image.write_bytes(corrupt_bytes)
        corrupt = run_packager(corrupt_images, temporary_root / "corrupt.json")
        require_failure(corrupt, "packager accepted a PNG with a corrupt chunk")
        require("invalid checksum" in corrupt.stderr, corrupt.stderr)

        symlinked_images = temporary_root / "symlinked"
        shutil.copytree(images, symlinked_images)
        linked_image = symlinked_images / "dashboard-reference-light.png"
        linked_image.unlink()
        linked_image.symlink_to(images / "dashboard-reference-light.png")
        symlinked = run_packager(symlinked_images, temporary_root / "symlinked.json")
        require_failure(symlinked, "packager accepted a symlinked candidate")
        require("must be a regular file" in symlinked.stderr, symlinked.stderr)

    print("dashboard reference candidate CLI tests passed")


if __name__ == "__main__":
    main()
