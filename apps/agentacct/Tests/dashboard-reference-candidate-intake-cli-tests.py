#!/usr/bin/env python3
"""Dependency-free CLI tests for downloaded reference candidate intake."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
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
import dashboard_reference_candidate  # noqa: E402


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


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
    if result.returncode != 0:
        raise AssertionError(result.stderr)


def package_candidate(candidate: Path, images: Path) -> None:
    candidate.mkdir()
    shutil.copytree(images, candidate / "images")
    write_manifest(candidate)


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


def read_manifest(candidate: Path) -> dict[str, object]:
    return json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))


def replace_manifest(candidate: Path, manifest: dict[str, object]) -> None:
    (candidate / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def directory_bytes(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in directory.iterdir()}


def add_png_text_chunk(path: Path) -> None:
    payload = path.read_bytes()
    if payload[-8:-4] != b"IEND":
        raise AssertionError("test PNG does not end in IEND")
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


class CandidateIntakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        reference_directories = sorted(
            path for path in REFERENCE_ROOT.iterdir() if path.is_dir()
        )
        if not reference_directories:
            raise AssertionError("expected at least one reference directory")
        cls.reference_directory = reference_directories[0]

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="agentacct-candidate-intake-tests-"
        )
        self.root = Path(self.temporary.name)
        self.candidate = self.root / "candidate"
        package_candidate(self.candidate, self.reference_directory)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def copy_candidate(self, name: str) -> Path:
        copied = self.root / name
        shutil.copytree(self.candidate, copied)
        return copied

    def changed_candidate(self, name: str = "changed-candidate") -> Path:
        candidate = self.copy_candidate(name)
        add_png_text_chunk(candidate / "images" / "dashboard-minimum-dark.png")
        write_manifest(candidate)
        return candidate

    def references(self, name: str, *, populated: bool = True) -> tuple[Path, Path]:
        references_root = self.root / name
        references_root.mkdir()
        destination = references_root / RENDERER_ID
        if populated:
            shutil.copytree(self.reference_directory, destination)
        return references_root, destination

    def assert_failed(
        self,
        result: subprocess.CompletedProcess[str],
        expected_error: str,
    ) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(expected_error, result.stderr)

    def assert_clean_root(self, references_root: Path) -> None:
        self.assertEqual(
            sorted(path.name for path in references_root.iterdir()),
            [RENDERER_ID],
        )

    def test_valid_candidate_matches_expected_identity(self) -> None:
        result = validate(self.candidate)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("(4 images)", result.stdout)

    def test_wrong_source_or_renderer_is_rejected(self) -> None:
        wrong_source = validate(self.candidate, source_commit=OTHER_COMMIT)
        self.assert_failed(wrong_source, "candidate source commit")

        wrong_renderer = validate(self.candidate, renderer_id="macos-other-renderer")
        self.assert_failed(wrong_renderer, "candidate renderer")

    def test_tampered_image_bytes_are_rejected(self) -> None:
        tampered = self.copy_candidate("tampered")
        image = tampered / "images" / "dashboard-minimum-dark.png"
        payload = bytearray(image.read_bytes())
        payload[len(payload) // 2] ^= 1
        image.write_bytes(payload)

        self.assertNotEqual(validate(tampered).returncode, 0)

    def test_manifest_json_contract_is_strict(self) -> None:
        duplicate_key = self.copy_candidate("duplicate-key")
        manifest_path = duplicate_key / "manifest.json"
        manifest_text = manifest_path.read_text(encoding="utf-8").rstrip()
        manifest_path.write_text(
            manifest_text[:-1] + ', "schema_version": 1}\n',
            encoding="utf-8",
        )
        self.assert_failed(validate(duplicate_key), "duplicate key")

        oversized = self.copy_candidate("oversized-manifest")
        (oversized / "manifest.json").write_bytes(b" " * (64 * 1024 + 1))
        self.assert_failed(validate(oversized), "manifest exceeds")

        unknown_field = self.copy_candidate("unknown-field")
        unknown_manifest = read_manifest(unknown_field)
        unknown_manifest["untrusted"] = True
        replace_manifest(unknown_field, unknown_manifest)
        self.assert_failed(validate(unknown_field), "fields mismatch")

    def test_manifest_types_and_artifact_order_are_strict(self) -> None:
        boolean_schema = self.copy_candidate("boolean-schema")
        boolean_manifest = read_manifest(boolean_schema)
        boolean_manifest["schema_version"] = True
        replace_manifest(boolean_schema, boolean_manifest)
        self.assert_failed(validate(boolean_schema), "schema_version must be integer 1")

        boolean_width = self.copy_candidate("boolean-width")
        width_manifest = read_manifest(boolean_width)
        artifacts = width_manifest["artifacts"]
        self.assertIsInstance(artifacts, list)
        artifacts[0]["pixel_width"] = True
        replace_manifest(boolean_width, width_manifest)
        self.assert_failed(validate(boolean_width), "width is invalid")

        reordered = self.copy_candidate("reordered-artifacts")
        reordered_manifest = read_manifest(reordered)
        reordered_artifacts = reordered_manifest["artifacts"]
        self.assertIsInstance(reordered_artifacts, list)
        reordered_artifacts[0], reordered_artifacts[1] = (
            reordered_artifacts[1],
            reordered_artifacts[0],
        )
        replace_manifest(reordered, reordered_manifest)
        self.assert_failed(validate(reordered), "artifact 0 must describe")

    def test_candidate_inventory_must_be_exact_and_regular(self) -> None:
        extra_root_file = self.copy_candidate("extra-root-file")
        (extra_root_file / "notes.txt").write_text("unexpected", encoding="utf-8")
        self.assert_failed(validate(extra_root_file), "root inventory mismatch")

        symlinked_manifest = self.copy_candidate("symlinked-manifest")
        linked_manifest = symlinked_manifest / "manifest.json"
        linked_manifest.unlink()
        linked_manifest.symlink_to(self.candidate / "manifest.json")
        self.assert_failed(validate(symlinked_manifest), "regular file")

        symlinked_images = self.copy_candidate("symlinked-images")
        shutil.rmtree(symlinked_images / "images")
        (symlinked_images / "images").symlink_to(
            self.candidate / "images",
            target_is_directory=True,
        )
        self.assert_failed(validate(symlinked_images), "regular directory")

        linked_candidate = self.root / "linked-candidate"
        linked_candidate.symlink_to(self.candidate, target_is_directory=True)
        self.assert_failed(validate(linked_candidate), "candidate must be a regular directory")

    def test_promotion_requires_review_and_detects_noop(self) -> None:
        references_root, _ = self.references("references")

        self.assert_failed(
            promote(self.candidate, references_root, reviewed=False),
            "--reviewed",
        )
        unchanged = promote(self.candidate, references_root)
        self.assertEqual(unchanged.returncode, 0, unchanged.stderr)
        self.assertIn("no files changed", unchanged.stdout)

    def test_promotion_can_create_a_new_renderer_directory(self) -> None:
        references_root, destination = self.references(
            "new-renderer-references",
            populated=False,
        )

        result = promote(self.candidate, references_root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            directory_bytes(destination),
            directory_bytes(self.candidate / "images"),
        )
        self.assert_clean_root(references_root)

    def test_changed_promotion_is_complete_and_idempotent(self) -> None:
        references_root, destination = self.references("changed-references")
        changed = self.changed_candidate()

        result = promote(changed, references_root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Promoted four reviewed reference images", result.stdout)
        self.assertEqual(directory_bytes(destination), directory_bytes(changed / "images"))
        self.assert_clean_root(references_root)

        repeated = promote(changed, references_root)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertIn("no files changed", repeated.stdout)

    def test_rejected_identity_does_not_mutate_references(self) -> None:
        references_root, destination = self.references("identity-references")
        before = directory_bytes(destination)

        result = promote(
            self.changed_candidate(),
            references_root,
            source_commit=OTHER_COMMIT,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(directory_bytes(destination), before)

    def test_failed_directory_swap_restores_original_references(self) -> None:
        references_root, destination = self.references("rollback-references")
        before = directory_bytes(destination)
        changed = self.changed_candidate()
        real_replace = os.replace

        def fail_staging_swap(source: object, destination_path: object) -> None:
            source_path = Path(source)
            if (
                source_path.name.startswith(f".{RENDERER_ID}.candidate-")
                and Path(destination_path) == destination
            ):
                raise OSError("simulated candidate swap failure")
            real_replace(source, destination_path)

        with mock.patch(
            "dashboard_reference_candidate.os.replace",
            side_effect=fail_staging_swap,
        ):
            with self.assertRaisesRegex(OSError, "simulated candidate swap failure"):
                dashboard_reference_candidate.promote_candidate_bundle(
                    changed,
                    SOURCE_COMMIT,
                    RENDERER_ID,
                    references_root,
                )

        self.assertEqual(directory_bytes(destination), before)
        self.assert_clean_root(references_root)

    def test_failed_directory_sync_restores_original_references(self) -> None:
        references_root, destination = self.references("sync-failure-references")
        before = directory_bytes(destination)
        changed = self.changed_candidate()
        real_sync_directory = dashboard_reference_candidate._sync_directory

        def fail_reference_root_sync(path: Path) -> None:
            if path == references_root:
                raise OSError("simulated reference-root sync failure")
            real_sync_directory(path)

        with mock.patch(
            "dashboard_reference_candidate._sync_directory",
            side_effect=fail_reference_root_sync,
        ):
            with self.assertRaisesRegex(OSError, "simulated reference-root sync failure"):
                dashboard_reference_candidate.promote_candidate_bundle(
                    changed,
                    SOURCE_COMMIT,
                    RENDERER_ID,
                    references_root,
                )

        self.assertEqual(directory_bytes(destination), before)
        self.assert_clean_root(references_root)

    def test_destination_must_be_an_exact_regular_directory(self) -> None:
        symlink_root, symlink_destination = self.references(
            "symlink-references",
            populated=False,
        )
        symlink_destination.symlink_to(self.reference_directory)
        self.assert_failed(
            promote(self.candidate, symlink_root),
            "must not be a symlink",
        )

        references_root, destination = self.references("extra-destination-references")
        (destination / "notes.txt").write_text("unexpected", encoding="utf-8")
        self.assert_failed(
            promote(self.candidate, references_root),
            "inventory mismatch",
        )


if __name__ == "__main__":
    unittest.main()
