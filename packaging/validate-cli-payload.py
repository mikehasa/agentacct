#!/usr/bin/env python3
"""Verify a frozen onedir payload satisfies the installer's symlink contract.

PyInstaller preserves the Python.framework aliases (Versions/Current and the
top-level binary/Resources) on macOS. Those aliases must be KEPT — codesign
needs a framework's canonical symlinks to treat it as a signable bundle — but
they must be safe: every symlink relative, resolving inside this build output,
with no external, broken, cyclic, world-writable, or special entry.

The app's install-time payload identity validator
(``SetupModel.CLIPayloadInspector``) enforces the same contract and binds each
alias into the payload fingerprint. This is the build-time gate that fails
closed before signing. It never modifies the payload — a materialized
(symlink-free) framework is not a valid signable bundle, so aliases stay put.

Run before smoke tests, provenance stamping, and distribution signing.
"""

from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path


def validate_payload(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Frozen payload must be a regular directory")
    root = root.resolve()

    def check(path: Path, ancestors: frozenset[Path]) -> None:
        if path.is_symlink():
            link_target = os.readlink(path)
            if os.path.isabs(link_target):
                raise ValueError(f"Payload alias target is absolute: {path}")
            if ".." in link_target.split("/"):
                # ".." after a symlinked component escapes the payload, so forbid
                # it outright, matching the installer's confinement rule. A
                # forward-only relative target stays inside the tree.
                raise ValueError(f"Payload alias target uses '..': {path}")
        try:
            target = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError(f"Broken or cyclic payload alias: {path}") from error
        if not target.is_relative_to(root):
            raise ValueError(f"Payload alias leaves build output: {path}")
        mode = target.stat().st_mode
        if mode & 0o022:
            raise ValueError(f"Payload entry is writable by other users: {path}")
        if stat.S_ISDIR(mode):
            if target in ancestors:
                raise ValueError(f"Cyclic payload directory alias: {path}")
            for child in target.iterdir():
                check(child, ancestors | {target})
        elif not stat.S_ISREG(mode):
            raise ValueError(f"Unsupported payload entry: {path}")

    # Validate the complete alias graph; the payload is never mutated.
    check(root, frozenset())


if __name__ == "__main__":
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", type=Path)
    try:
        validate_payload(parser.parse_args().payload)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)

