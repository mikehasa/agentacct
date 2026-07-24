from __future__ import annotations

from pathlib import Path

import pytest

from agent_chronicle.canonical.live_paths import (
    LivePathSafetyError,
    reject_live_state_overlap,
)


@pytest.mark.parametrize(
    "component",
    [
        ".agent-sentinel",
        ".agent-sentinel-global",
        ".agent-chronicle",
        ".agent-chronicle-global",
        ".AGENT-SENTINEL",
    ],
)
def test_project_local_live_state_shape_is_rejected_lexically(
    tmp_path: Path,
    component: str,
) -> None:
    scratch = tmp_path / "proj" / component / "state" / "scratch"

    with pytest.raises(LivePathSafetyError, match="may not be inside"):
        reject_live_state_overlap(scratch, label="--scratch-root")


def test_live_state_shape_reached_through_symlink_is_rejected(
    tmp_path: Path,
) -> None:
    live_shaped = tmp_path / "proj" / ".agent-sentinel" / "state"
    live_shaped.mkdir(parents=True)
    alias = tmp_path / "offline-alias"
    alias.symlink_to(live_shaped, target_is_directory=True)

    with pytest.raises(LivePathSafetyError, match=r"\.agent-sentinel"):
        reject_live_state_overlap(alias / "scratch", label="--scratch-root")


def test_live_shaped_name_is_rejected_even_when_it_resolves_elsewhere(
    tmp_path: Path,
) -> None:
    clean = tmp_path / "clean"
    clean.mkdir()
    shape_parent = tmp_path / "proj" / ".agent-sentinel"
    shape_parent.mkdir(parents=True)
    (shape_parent / "state").symlink_to(clean, target_is_directory=True)

    with pytest.raises(LivePathSafetyError, match=r"\.agent-sentinel"):
        reject_live_state_overlap(
            shape_parent / "state" / "scratch",
            label="--scratch-root",
        )


def test_lookalike_components_without_the_live_shape_stay_accepted(
    tmp_path: Path,
) -> None:
    for accepted in (
        tmp_path / "proj" / "agent-sentinel" / "state" / "scratch",
        tmp_path / "proj" / ".agent-sentinel-data" / "state" / "scratch",
    ):
        reject_live_state_overlap(accepted, label="--scratch-root")


def test_bare_live_component_is_rejected_without_state_suffix(tmp_path: Path) -> None:
    for name in (
        ".agent-sentinel",
        ".agent-sentinel-global",
        ".agent-chronicle",
        ".agent-chronicle-global",
    ):
        shaped = tmp_path / "project" / name / "scratch"
        with pytest.raises(LivePathSafetyError, match="may not be inside"):
            reject_live_state_overlap(shaped, label="scratch root")
