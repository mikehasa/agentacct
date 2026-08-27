"""Shared validation for dashboard reference-image candidate bundles."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import tempfile
import zlib


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
EXPECTED_IMAGES = {
    "dashboard-minimum-dark.png": (1920, 1120),
    "dashboard-minimum-light.png": (1920, 1120),
    "dashboard-reference-dark.png": (2240, 1600),
    "dashboard-reference-light.png": (2240, 1600),
}
FULL_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
SAFE_RENDERER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
MAX_MANIFEST_BYTES = 64 * 1024
MAX_PNG_BYTES = 64 * 1024 * 1024


class CandidateError(ValueError):
    """A candidate bundle violated the promotion contract."""


def single_line(name: str, value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or "\n" in value
        or "\r" in value
    ):
        raise CandidateError(f"{name} must be a non-empty single-line string")
    return value


def validate_source_commit(value: object) -> str:
    source_commit = single_line("source commit", value)
    if FULL_COMMIT_PATTERN.fullmatch(source_commit) is None:
        raise CandidateError("source commit must be a full lowercase 40-character Git SHA")
    return source_commit


def validate_renderer_id(value: object) -> str:
    renderer_id = single_line("renderer id", value)
    if SAFE_RENDERER_PATTERN.fullmatch(renderer_id) is None:
        raise CandidateError("renderer id must be a safe path component")
    return renderer_id


def checked_png_dimensions(path: Path) -> tuple[int, int]:
    if path.is_symlink() or not path.is_file():
        raise CandidateError(f"candidate image must be a regular file: {path.name}")
    with path.open("rb") as stream:
        data = stream.read(MAX_PNG_BYTES + 1)
    if len(data) > MAX_PNG_BYTES:
        raise CandidateError(f"candidate image exceeds {MAX_PNG_BYTES} bytes: {path.name}")
    if not data.startswith(PNG_SIGNATURE):
        raise CandidateError(f"candidate image is not a PNG: {path.name}")

    offset = len(PNG_SIGNATURE)
    dimensions: tuple[int, int] | None = None
    saw_image_data = False
    saw_end = False
    while offset < len(data):
        if len(data) - offset < 12:
            raise CandidateError(f"candidate PNG is truncated: {path.name}")
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            raise CandidateError(f"candidate PNG is truncated: {path.name}")
        chunk_data = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length : chunk_end])[0]
        actual_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise CandidateError(f"candidate PNG has an invalid checksum: {path.name}")

        if dimensions is None:
            if chunk_type != b"IHDR" or length != 13:
                raise CandidateError(f"candidate PNG has an invalid header: {path.name}")
            width, height = struct.unpack(">II", chunk_data[:8])
            dimensions = (width, height)
        elif chunk_type == b"IHDR":
            raise CandidateError(f"candidate PNG has more than one header: {path.name}")
        elif chunk_type == b"IDAT":
            saw_image_data = True

        offset = chunk_end
        if chunk_type == b"IEND":
            if length != 0 or not saw_image_data or offset != len(data):
                raise CandidateError(f"candidate PNG has an invalid end marker: {path.name}")
            saw_end = True
            break

    if dimensions is None or not saw_image_data or not saw_end:
        raise CandidateError(f"candidate PNG is incomplete: {path.name}")
    return dimensions


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            total += len(chunk)
            if total > MAX_PNG_BYTES:
                raise CandidateError(f"candidate image exceeds {MAX_PNG_BYTES} bytes: {path.name}")
            digest.update(chunk)
    return digest.hexdigest()


def candidate_artifacts(images: Path) -> list[dict[str, object]]:
    if images.is_symlink() or not images.is_dir():
        raise CandidateError(f"candidate image directory must be a regular directory: {images}")

    actual_names = {path.name for path in images.iterdir()}
    expected_names = set(EXPECTED_IMAGES)
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        unexpected = sorted(actual_names - expected_names)
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected: {', '.join(unexpected)}")
        raise CandidateError("candidate image inventory mismatch (" + "; ".join(details) + ")")

    artifacts = []
    for filename, expected_dimensions in EXPECTED_IMAGES.items():
        path = images / filename
        dimensions = checked_png_dimensions(path)
        if dimensions != expected_dimensions:
            expected_width, expected_height = expected_dimensions
            actual_width, actual_height = dimensions
            raise CandidateError(
                f"candidate image {filename} must be {expected_width}x{expected_height}; "
                f"got {actual_width}x{actual_height}"
            )
        artifacts.append(
            {
                "filename": filename,
                "pixel_height": dimensions[1],
                "pixel_width": dimensions[0],
                "sha256": file_sha256(path),
            }
        )
    return artifacts


def build_manifest(
    images: Path,
    source_commit: object,
    renderer_id: object,
    runner_image: object,
    runner_image_version: object,
) -> dict[str, object]:
    return {
        "artifacts": candidate_artifacts(images),
        "renderer_id": validate_renderer_id(renderer_id),
        "runner_image": {
            "name": single_line("runner image", runner_image),
            "version": single_line("runner image version", runner_image_version),
        },
        "schema_version": 1,
        "source_commit": validate_source_commit(source_commit),
    }


def write_manifest(path: Path, manifest: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CandidateError(f"candidate manifest contains duplicate key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise CandidateError(f"candidate manifest contains invalid JSON constant: {value}")


def _exact_keys(value: object, name: str, expected: set[str]) -> dict[str, object]:
    if type(value) is not dict:
        raise CandidateError(f"candidate manifest {name} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected: {', '.join(unexpected)}")
        raise CandidateError(
            f"candidate manifest {name} fields mismatch (" + "; ".join(details) + ")"
        )
    return value


def _load_manifest(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise CandidateError("candidate manifest must be a regular file")
    try:
        with path.open("rb") as stream:
            raw_payload = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw_payload) > MAX_MANIFEST_BYTES:
            raise CandidateError(f"candidate manifest exceeds {MAX_MANIFEST_BYTES} bytes")
        payload = raw_payload.decode("utf-8")
        parsed = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidateError(f"candidate manifest is not valid UTF-8 JSON: {error}") from error
    return _exact_keys(
        parsed,
        "top-level",
        {"artifacts", "renderer_id", "runner_image", "schema_version", "source_commit"},
    )


def validate_candidate_bundle(
    candidate: Path,
    expected_source_commit: object,
    expected_renderer_id: object,
) -> dict[str, object]:
    if candidate.is_symlink() or not candidate.is_dir():
        raise CandidateError("candidate must be a regular directory")
    actual_root_names = {path.name for path in candidate.iterdir()}
    if actual_root_names != {"images", "manifest.json"}:
        missing = sorted({"images", "manifest.json"} - actual_root_names)
        unexpected = sorted(actual_root_names - {"images", "manifest.json"})
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected: {', '.join(unexpected)}")
        raise CandidateError("candidate root inventory mismatch (" + "; ".join(details) + ")")

    manifest = _load_manifest(candidate / "manifest.json")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise CandidateError("candidate manifest schema_version must be integer 1")

    source_commit = validate_source_commit(manifest["source_commit"])
    requested_source_commit = validate_source_commit(expected_source_commit)
    if source_commit != requested_source_commit:
        raise CandidateError(
            f"candidate source commit is {source_commit}; expected {requested_source_commit}"
        )

    renderer_id = validate_renderer_id(manifest["renderer_id"])
    requested_renderer_id = validate_renderer_id(expected_renderer_id)
    if renderer_id != requested_renderer_id:
        raise CandidateError(
            f"candidate renderer is {renderer_id}; expected {requested_renderer_id}"
        )

    runner = _exact_keys(manifest["runner_image"], "runner_image", {"name", "version"})
    single_line("runner image", runner["name"])
    single_line("runner image version", runner["version"])

    artifacts = manifest["artifacts"]
    if type(artifacts) is not list or len(artifacts) != len(EXPECTED_IMAGES):
        raise CandidateError(
            f"candidate manifest artifacts must contain exactly {len(EXPECTED_IMAGES)} entries"
        )
    normalized_artifacts = []
    for index, (filename, dimensions) in enumerate(EXPECTED_IMAGES.items()):
        artifact = _exact_keys(
            artifacts[index],
            f"artifact {index}",
            {"filename", "pixel_height", "pixel_width", "sha256"},
        )
        if artifact["filename"] != filename:
            raise CandidateError(
                f"candidate manifest artifact {index} must describe {filename}"
            )
        expected_width, expected_height = dimensions
        if type(artifact["pixel_width"]) is not int or artifact["pixel_width"] != expected_width:
            raise CandidateError(f"candidate manifest width is invalid for {filename}")
        if type(artifact["pixel_height"]) is not int or artifact["pixel_height"] != expected_height:
            raise CandidateError(f"candidate manifest height is invalid for {filename}")
        if (
            type(artifact["sha256"]) is not str
            or SHA256_PATTERN.fullmatch(artifact["sha256"]) is None
        ):
            raise CandidateError(f"candidate manifest SHA-256 is invalid for {filename}")
        normalized_artifacts.append(dict(artifact))

    actual_artifacts = candidate_artifacts(candidate / "images")
    if normalized_artifacts != actual_artifacts:
        raise CandidateError("candidate manifest does not match the candidate image bytes")
    return manifest


def _copy_regular_file(source: Path, destination: Path) -> None:
    with source.open("rb") as source_stream, destination.open("xb") as destination_stream:
        total = 0
        for chunk in iter(lambda: source_stream.read(1024 * 1024), b""):
            total += len(chunk)
            if total > MAX_PNG_BYTES:
                raise CandidateError(
                    f"candidate image exceeds {MAX_PNG_BYTES} bytes: {source.name}"
                )
            destination_stream.write(chunk)
        destination_stream.flush()
        os.fsync(destination_stream.fileno())


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def promote_candidate_bundle(
    candidate: Path,
    expected_source_commit: object,
    expected_renderer_id: object,
    references_root: Path,
) -> tuple[Path, bool]:
    manifest = validate_candidate_bundle(
        candidate,
        expected_source_commit,
        expected_renderer_id,
    )
    if references_root.is_symlink() or not references_root.is_dir():
        raise CandidateError("reference root must be an existing regular directory")

    renderer_id = validate_renderer_id(manifest["renderer_id"])
    destination = references_root / renderer_id
    if destination.is_symlink():
        raise CandidateError("reference destination must not be a symlink")
    if destination.exists():
        existing_artifacts = candidate_artifacts(destination)
        if existing_artifacts == manifest["artifacts"]:
            return destination, False

    staging: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{renderer_id}.candidate-", dir=references_root)
    )
    staging.chmod(0o755)
    backup: Path | None = None
    try:
        for filename in EXPECTED_IMAGES:
            _copy_regular_file(candidate / "images" / filename, staging / filename)
        if candidate_artifacts(staging) != manifest["artifacts"]:
            raise CandidateError("staged reference bytes do not match the validated candidate")
        _sync_directory(staging)

        if destination.exists():
            backup = Path(
                tempfile.mkdtemp(prefix=f".{renderer_id}.backup-", dir=references_root)
            )
            backup.rmdir()
            os.replace(destination, backup)
        try:
            os.replace(staging, destination)
            staging = None
        except OSError:
            if backup is not None:
                os.replace(backup, destination)
                backup = None
            raise
        _sync_directory(references_root)

        if backup is not None:
            shutil.rmtree(backup)
            backup = None
            _sync_directory(references_root)
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
        if backup is not None and backup.exists():
            if not destination.exists():
                os.replace(backup, destination)
            else:
                shutil.rmtree(backup)

    return destination, True
