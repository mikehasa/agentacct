#!/usr/bin/env python3
"""Dependency-free CLI tests for downloaded reference candidate validation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import struct
import sys
import tempfile
from unittest import mock
import zlib


TESTS_DIR = Path(__file__).resolve().parent
APP_DIR = TESTS_DIR.parent
PACKAGER = APP_DIR / "Scripts" / "package-dashboard-reference-candidate"
VALIDATOR = APP_DIR / "Scripts" / "validate-dashboard-reference-candidate"
PROMOTER = APP_DIR / "Scripts" / "promote-dashboard-reference-candidate"
REFERENCE_ROOT = TESTS_DIR / "agentacctTests" / "ReferenceImages"
SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER_COMMIT = "89abcdef0123456789abcdef0123456789abcdef"
RENDERER_ID = "macos-test-build-xcode-test-build-arm64-2x"

sys.path.insert(0, str(APP_DIR / "Scripts"))
from dashboard_reference_candidate import promote_candidate_bundle  # noqa: E402


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
    write_manifest(candidate)


def write_manifest(candidate: Path) -> None:
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


def promote(
    candidate: Path,
    references_root: Path,
    *,
    reviewed: bool = True,
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    values = {
        "source_commit": SOURCE_COMMIT,
        "renderer_id": RENDERER_ID,
        **overrides,
    }
    command = [
        str(PROMOTER),
        "--candidate",
        str(candidate),
        "--source-commit",
        values["source_commit"],
        "--renderer-id",
        values["renderer_id"],
        "--references-root",
        str(references_root),
    ]
    if reviewed:
        command.append("--reviewed")
    return run(command)


def add_png_text_chunk(path: Path) -> None:
    payload = path.read_bytes()
    require(payload[-8:-4] == b"IEND", "test PNG does not end in IEND")
    chunk_type = b"tEXt"
    chunk_data = b"agentacct\x00reviewed-candidate"
    checksum = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
    chunk = (
        struct.pack(">I", len(chunk_data))
        + chunk_type
        + chunk_data
        + struct.pack(">I", checksum)
    )
    path.write_bytes(payload[:-12] + chunk + payload[-12:])


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

        references_root = root / "references"
        references_root.mkdir()
        destination = references_root / RENDERER_ID
        shutil.copytree(reference_directories[0], destination)

        not_reviewed = promote(candidate, references_root, reviewed=False)
        require_failure(not_reviewed, "promoter accepted a candidate without review confirmation")
        require("--reviewed" in not_reviewed.stderr, not_reviewed.stderr)

        unchanged = promote(candidate, references_root)
        require(unchanged.returncode == 0, unchanged.stderr)
        require("no files changed" in unchanged.stdout, unchanged.stdout)

        changed_candidate = root / "changed-candidate"
        shutil.copytree(candidate, changed_candidate)
        add_png_text_chunk(changed_candidate / "images" / "dashboard-minimum-dark.png")
        write_manifest(changed_candidate)

        rollback_root = root / "rollback-references"
        rollback_root.mkdir()
        rollback_destination = rollback_root / RENDERER_ID
        shutil.copytree(reference_directories[0], rollback_destination)
        rollback_before = {
            path.name: path.read_bytes() for path in rollback_destination.iterdir()
        }
        real_replace = os.replace

        def fail_staging_swap(source: object, destination_path: object) -> None:
            source_path = Path(source)
            if (
                source_path.name.startswith(f".{RENDERER_ID}.candidate-")
                and Path(destination_path) == rollback_destination
            ):
                raise OSError("simulated candidate swap failure")
            real_replace(source, destination_path)

        with mock.patch(
            "dashboard_reference_candidate.os.replace",
            side_effect=fail_staging_swap,
        ):
            try:
                promote_candidate_bundle(
                    changed_candidate,
                    SOURCE_COMMIT,
                    RENDERER_ID,
                    rollback_root,
                )
            except OSError as error:
                require("simulated candidate swap failure" in str(error), str(error))
            else:
                raise AssertionError("promotion swap failure did not propagate")
        require(
            {
                path.name: path.read_bytes()
                for path in rollback_destination.iterdir()
            }
            == rollback_before,
            "failed directory swap did not restore the original references",
        )
        require(
            [path.name for path in rollback_root.iterdir()] == [RENDERER_ID],
            "failed directory swap left staging or backup directories behind",
        )

        before_wrong_identity = {
            path.name: path.read_bytes() for path in destination.iterdir()
        }
        rejected_promotion = promote(
            changed_candidate,
            references_root,
            source_commit=OTHER_COMMIT,
        )
        require_failure(rejected_promotion, "promoter accepted the wrong source identity")
        require(
            {path.name: path.read_bytes() for path in destination.iterdir()}
            == before_wrong_identity,
            "failed promotion changed references",
        )

        promoted = promote(changed_candidate, references_root)
        require(promoted.returncode == 0, promoted.stderr)
        require("Promoted four reviewed reference images" in promoted.stdout, promoted.stdout)
        require(
            {path.name: path.read_bytes() for path in destination.iterdir()}
            == {
                path.name: path.read_bytes()
                for path in (changed_candidate / "images").iterdir()
            },
            "promotion did not install the complete candidate set",
        )

        repeated = promote(changed_candidate, references_root)
        require(repeated.returncode == 0, repeated.stderr)
        require("no files changed" in repeated.stdout, repeated.stdout)

        symlink_root = root / "symlink-references"
        symlink_root.mkdir()
        (symlink_root / RENDERER_ID).symlink_to(reference_directories[0])
        symlink_destination = promote(candidate, symlink_root)
        require_failure(symlink_destination, "promoter followed a destination symlink")
        require("must not be a symlink" in symlink_destination.stderr, symlink_destination.stderr)

        (destination / "notes.txt").write_text("unexpected", encoding="utf-8")
        unexpected_destination = promote(candidate, references_root)
        require_failure(
            unexpected_destination,
            "promoter replaced a destination with unexpected files",
        )
        require("inventory mismatch" in unexpected_destination.stderr, unexpected_destination.stderr)

    print("dashboard reference candidate intake CLI tests passed")


if __name__ == "__main__":
    main()
