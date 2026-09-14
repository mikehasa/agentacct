"""The frozen framework aliases are kept, but only when they are safe.

codesign needs a framework's canonical symlinks, so the build-time gate
validates them in place instead of dereferencing them: safe aliases (relative,
in-payload, non-cyclic) are preserved untouched, and anything unsafe fails the
build before signing.
"""

import importlib.util
from pathlib import Path

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "validate_cli_payload", Path(__file__).parents[1] / "packaging/validate-cli-payload.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
validate_payload = _MODULE.validate_payload


def _framework_fixture(root: Path) -> None:
    version = root / "Python.framework/Versions/3.14"
    version.mkdir(parents=True)
    binary = version / "Python"
    binary.write_bytes(b"synthetic executable")
    binary.chmod(0o755)
    (version / "Resources").mkdir()
    (version / "Resources/Info.plist").write_text("fixture")
    (version.parent / "Current").symlink_to("3.14", target_is_directory=True)
    (root / "Python.framework/Python").symlink_to("Versions/Current/Python")
    (root / "Python.framework/Resources").symlink_to("Versions/Current/Resources", target_is_directory=True)
    (root / "Python").symlink_to("Python.framework/Python")


def test_valid_framework_aliases_are_preserved(tmp_path):
    root = tmp_path / "cli"
    root.mkdir()
    _framework_fixture(root)
    links_before = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_symlink())

    validate_payload(root)  # must not raise

    # The canonical aliases codesign needs are still present and untouched.
    links_after = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_symlink())
    assert links_after == links_before
    assert (root / "Python").is_symlink()
    assert (root / "Python.framework/Versions/Current").is_symlink()
    # Idempotent: validating again is still a no-op that mutates nothing.
    validate_payload(root)
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_symlink()) == links_before


@pytest.mark.parametrize(
    "kind",
    [
        "external_file",
        "external_directory",
        "absolute_internal",
        "relative_dotdot",
        "symlink_through_symlink",
        "broken",
        "link_cycle",
        "directory_cycle",
        "fifo",
        "shared_write",
    ],
)
def test_invalid_payload_is_rejected(tmp_path, kind):
    root = tmp_path / "cli"
    root.mkdir()
    sentinel = root / "agentacct"
    sentinel.write_text("keep original")
    inode = sentinel.stat().st_ino
    alias = root / "alias"
    if kind == "external_file":
        outside = tmp_path / "private"
        outside.write_text("must not read")
        alias.symlink_to(outside)
    elif kind == "external_directory":
        alias.symlink_to(tmp_path, target_is_directory=True)
    elif kind == "absolute_internal":
        # An absolute target, even one inside the payload, is rejected: an
        # installed/relocated payload must not depend on the build machine path.
        alias.symlink_to(sentinel.resolve())
    elif kind == "relative_dotdot":
        # A relative target that climbs out of the payload with "..".
        alias.symlink_to("../outside")
    elif kind == "symlink_through_symlink":
        # "escape" is lexically in-root (pivot/../..) but "pivot/.." resolves
        # against pivot's physical target, so it would climb out. The ".." is
        # rejected outright.
        (root / "sub").mkdir()
        (root / "pivot").symlink_to("sub", target_is_directory=True)
        alias.symlink_to("pivot/../../outside")
    elif kind == "broken":
        alias.symlink_to("absent")
    elif kind == "link_cycle":
        alias.symlink_to("other")
        (root / "other").symlink_to("alias")
    elif kind == "directory_cycle":
        alias.symlink_to(".", target_is_directory=True)
    elif kind == "fifo":
        import os

        os.mkfifo(alias)
    else:
        sentinel.chmod(0o666)

    with pytest.raises(ValueError):
        validate_payload(root)
    # The payload is never mutated, valid or not.
    assert sentinel.read_text() == "keep original"
    assert sentinel.stat().st_ino == inode


def test_linked_root_is_rejected(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "cli"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="regular directory"):
        validate_payload(alias)
