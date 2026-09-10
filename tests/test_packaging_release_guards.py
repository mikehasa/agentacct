from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_DMG = REPO_ROOT / "packaging" / "build-dmg.sh"
BUILD_APP = REPO_ROOT / "apps" / "agentacct" / "Scripts" / "build-app.sh"
RENAME_NO_REPLACE = REPO_ROOT / "packaging" / "rename-no-replace.c"
VERIFY_DMG = REPO_ROOT / "packaging" / "verify-dmg.sh"
SOURCE_PROVENANCE = REPO_ROOT / "packaging" / "source-provenance.sh"


def _install_recovery_function() -> str:
    script = BUILD_APP.read_text(encoding="utf-8")
    start = script.index("    restore_previous_app_on_abort() {")
    end = script.index("    trap restore_previous_app_on_abort EXIT", start)
    return textwrap.dedent(script[start:end])


def _inline_rollback_condition() -> str:
    script = BUILD_APP.read_text(encoding="utf-8")
    line = next(
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith('if [[ ! -e "$INSTALL_TARGET"')
    )
    return line.removeprefix("if ").removesuffix("; then")


def _activation_failure_block() -> str:
    script = BUILD_APP.read_text(encoding="utf-8")
    start = script.index(
        '    if ! "$INSTALL_RENAME_NO_REPLACE" "$INSTALL_STAGE" "$INSTALL_TARGET"; then'
    )
    end = script.index("    # The complete new app is now live", start)
    return textwrap.dedent(script[start:end])


def _existing_app_ownership_guard(plist_reader: str = "/usr/bin/plutil") -> str:
    script = BUILD_APP.read_text(encoding="utf-8")
    start = script.index("    verify_existing_app_bundle() {")
    end = script.index("    # Stage on the destination filesystem", start)
    return textwrap.dedent(script[start:end]).replace("/usr/bin/plutil", plist_reader)


def _existing_app_verifier_function(plist_reader: str = "/usr/bin/plutil") -> str:
    script = BUILD_APP.read_text(encoding="utf-8")
    start = script.index("    verify_existing_app_bundle() {")
    end = script.index('    if [[ -L "$INSTALL_TARGET"', start)
    return textwrap.dedent(script[start:end]).replace("/usr/bin/plutil", plist_reader)


@pytest.fixture(
    params=[
        "portable",
        pytest.param(
            "native",
            marks=pytest.mark.skipif(sys.platform != "darwin", reason="Apple plutil is macOS-only"),
        ),
    ]
)
def app_plist_reader(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    """Exercise the unchanged shell gate with real plist data on every OS.

    Linux has no Apple plutil. Its substitute implements only the string-key
    extraction contract used by the gate; macOS also runs with the real tool.
    """
    if request.param == "native":
        return "/usr/bin/plutil"
    extractor = tmp_path / "extract-plist.py"
    extractor.write_text(
        textwrap.dedent(
            """\
            import plistlib
            import sys

            args = sys.argv[1:]
            if len(args) != 6 or args[0] != "-extract" or args[2:5] != ["raw", "-o", "-"]:
                raise SystemExit(2)
            try:
                with open(args[5], "rb") as handle:
                    value = plistlib.load(handle)[args[1]]
                if not isinstance(value, str):
                    raise TypeError("expected a string plist value")
            except (OSError, ValueError, TypeError, KeyError, plistlib.InvalidFileException):
                raise SystemExit(1)
            print(value)
            """
        ),
        encoding="utf-8",
    )
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(extractor))}"


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({}, "--release requires DEVELOPER_ID"),
        ({"DEVELOPER_ID": "Developer ID Application: Test"}, "--release requires NOTARY_PROFILE"),
        ({"NOTARY_PROFILE": "test-profile"}, "--release requires DEVELOPER_ID"),
        (
            {
                "DEVELOPER_ID": "Developer ID Application: Test (ABCDEFGHIJ)",
                "NOTARY_PROFILE": "test-profile",
            },
            "--release requires RELEASE_TEAM_ID",
        ),
    ],
)
def test_release_dmg_fails_before_build_when_credentials_are_incomplete(
    environment: dict[str, str], expected: str
) -> None:
    env = os.environ.copy()
    env.pop("DEVELOPER_ID", None)
    env.pop("NOTARY_PROFILE", None)
    env.pop("RELEASE_TEAM_ID", None)
    env.update(environment)

    completed = subprocess.run(
        ["bash", str(BUILD_DMG), "--release"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert expected in completed.stderr
    assert "[1/5] freezing the CLI" not in completed.stdout


def test_app_installer_never_uses_the_shared_agentacct_process_name() -> None:
    script = BUILD_APP.read_text(encoding="utf-8")

    assert "killall agentacct" not in script
    assert 'application id "dev.agentacct.app"' in script
    assert "APP_PROCESS_PATTERN" in script


def test_failed_release_build_cannot_partially_replace_last_successful_output() -> None:
    script = BUILD_DMG.read_text(encoding="utf-8")

    staging = script.index('BUILD_ROOT="$(mktemp -d "$HERE/.dmg-build.XXXXXX")"')
    freeze = script.index('echo "==> [1/5] freezing the CLI"')
    publish = script.index('"$RENAME_NO_REPLACE" "$OUT_DIR" "$FINAL_OUT_DIR"')
    notarize = script.index('xcrun stapler staple "$DMG"')
    assert staging < freeze < notarize < publish
    assert '.agentacct-release-complete' in script
    assert 'rm -rf "$FINAL_OUT_DIR"' not in script
    assert "previous output preserved at $PUBLISH_BACKUP" in script
    assert '"$RENAME_NO_REPLACE" "$OUT_DIR" "$FINAL_OUT_DIR"' in script


def test_dmg_transaction_workspace_does_not_dirty_pinned_source_identity(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    packaging = repo / "packaging"
    packaging.mkdir(parents=True)
    (packaging / ".gitignore").write_text(
        (REPO_ROOT / "packaging" / ".gitignore").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (repo / "tracked.txt").write_text("pinned\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=agentacct test",
            "-c",
            "user.email=agentacct@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )

    def source_description() -> str:
        completed = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; agentacct_source_description "$2"',
                "agentacct-source-test",
                str(SOURCE_PROVENANCE),
                str(repo),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    baseline = source_description()
    (packaging / ".dmg-build.ABC123").mkdir()

    assert source_description() == baseline
    assert not baseline.endswith("-dirty")


def test_local_dmg_build_cannot_replace_release_ready_output() -> None:
    script = BUILD_DMG.read_text(encoding="utf-8")

    assert 'ARTIFACT_CLASS="release"' in script
    assert 'FINAL_OUT_DIR="$HERE/release"' in script
    assert 'COMPLETION_MARKER_NAME=".agentacct-release-complete"' in script
    assert 'ARTIFACT_CLASS="local"' in script
    assert 'FINAL_OUT_DIR="$HERE/local-build"' in script
    assert 'COMPLETION_MARKER_NAME=".agentacct-local-build-complete"' in script
    assert "artifact_class=%s" in script
    assert "signed=%s" in script
    assert "notarized=%s" in script
    assert "local-build/" in (REPO_ROOT / "packaging" / ".gitignore").read_text(
        encoding="utf-8"
    )


def test_release_preflights_real_credentials_before_build_work() -> None:
    script = BUILD_DMG.read_text(encoding="utf-8")

    identity = script.index("security find-identity -v -p codesigning")
    profile = script.index("xcrun notarytool history")
    build_root = script.index('BUILD_ROOT="$(mktemp -d "$HERE/.dmg-build.XXXXXX")"')
    freeze = script.index('echo "==> [1/5] freezing the CLI"')
    assert identity < build_root < freeze
    assert profile < build_root < freeze
    assert "DEVELOPER_ID must exactly match one available code-signing identity" in script
    assert '--sign "$SIGNING_IDENTITY_HASH"' in script
    signer_pin = script.index(
        'codesign --verify --deep --strict --verbose=2',
        script.index("# ---- [3/5] sign"),
    )
    dmg_create = script.index("hdiutil create")
    assert signer_pin < dmg_create


def test_release_rejects_invalid_team_id_before_build_work() -> None:
    env = os.environ.copy()
    env.update(
        {
            "DEVELOPER_ID": "Developer ID Application: Test (NOT-A-TEAM)",
            "NOTARY_PROFILE": "test-profile",
            "RELEASE_TEAM_ID": "not-a-team",
        }
    )

    completed = subprocess.run(
        ["bash", str(BUILD_DMG), "--release"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "trusted 10-character Apple Team ID" in completed.stderr
    assert "[1/5] freezing the CLI" not in completed.stdout


def test_release_rechecks_pinned_source_identity_before_publish() -> None:
    script = BUILD_DMG.read_text(encoding="utf-8")

    assert 'EXPECTED_SOURCE_COMMIT="$(agentacct_source_commit "$REPO_ROOT")"' in script
    assert script.count("agentacct_assert_source_identity") >= 3
    assert 'plutil -extract AgentacctGitCommit' in script
    assert '"$PLIST_COMMIT" != "$EXPECTED_SOURCE_COMMIT"' in script
    assert '"$EMBEDDED_COMMIT" != "$EXPECTED_SOURCE_COMMIT"' in script
    marker = script.index("artifact_class=%s")
    publish = script.index('"$RENAME_NO_REPLACE" "$OUT_DIR" "$FINAL_OUT_DIR"')
    assert marker < publish


def test_dmg_verifier_uses_actual_attach_result_and_checks_all_identities() -> None:
    script = VERIFY_DMG.read_text(encoding="utf-8")

    assert 'hdiutil attach "$DMG" -nobrowse -readonly -plist' in script
    assert 'system-entities.$index.mount-point' in script
    assert "/Volumes/agentacct" not in script
    assert 'xcrun stapler validate "$DMG"' in script
    assert 'codesign --verify --deep --strict "$APP"' in script
    assert 'spctl -a -vv --type execute "$APP"' in script
    assert 'agentacct_cli_version "$EMBEDDED_CLI/agentacct"' in script
    assert 'plutil -extract AgentacctGitCommit' in script
    assert 'plutil -extract CFBundleIdentifier' in script
    assert '"$BUNDLE_IDENTIFIER" != "dev.agentacct.app"' in script
    assert '"$PACKAGE_TYPE" != "APPL"' in script
    assert '"$BUNDLE_EXECUTABLE" != "agentacct"' in script
    team_check = script.index('if [[ "$ACTUAL_TEAM_ID" != "$EXPECTED_TEAM_ID" ]]')
    app_requirement = script.index('codesign --verify --deep --strict --verbose=2 -R')
    payload_requirement = script.index(
        'codesign --verify --strict --verbose=2 -R "$PAYLOAD_REQUIREMENT"'
    )
    cli_execution = script.index('CLI_VERSION="$(agentacct_cli_version')
    assert team_check < app_requirement < payload_requirement < cli_execution
    assert "1.2.840.113635.100.6.2.6" in script
    assert "1.2.840.113635.100.6.1.13" in script


@pytest.mark.parametrize(
    ("actual_team", "requirement_matches", "expected_exit"),
    [("OTHERTEAM1X", True, 1), ("ABCDEFGHIJ", False, 1), ("ABCDEFGHIJ", True, 0)],
)
def test_dmg_verifier_never_executes_cli_before_signer_trust_passes(
    tmp_path: Path, actual_team: str, requirement_matches: bool, expected_exit: int
) -> None:
    script = VERIFY_DMG.read_text(encoding="utf-8")
    trust_and_execution = script[
        script.index('codesign --verify --deep --strict "$APP"'):
        script.index('APP_COMMIT="$(plutil -extract AgentacctGitCommit')
    ]
    cli = tmp_path / "agentacct"
    executed = tmp_path / "cli-executed"
    cli.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(executed))}\necho 'agentacct 1.2.3'\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    completed = subprocess.run(
        [
            "bash",
            "-c",
            f"""
            set -euo pipefail
            source {shlex.quote(str(SOURCE_PROVENANCE))}
            APP={shlex.quote(str(tmp_path / "agentacct.app"))}
            PLIST={shlex.quote(str(tmp_path / "Info.plist"))}
            EMBEDDED_CLI={shlex.quote(str(tmp_path))}
            EXPECTED_TEAM_ID=ABCDEFGHIJ
            codesign() {{
                if [[ "$1" == "-d" ]]; then
                    printf 'TeamIdentifier=%s\\n' {shlex.quote(actual_team)} >&2
                    return 0
                fi
                for arg in "$@"; do
                    if [[ "$arg" == "-R" ]]; then
                        return {0 if requirement_matches else 1}
                    fi
                done
                return 0
            }}
            spctl() {{ return 0; }}
            plutil() {{ printf 'fixture\\n'; }}
            {trust_and_execution}
            """,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == expected_exit, completed.stderr
    assert executed.exists() is (expected_exit == 0)


def test_release_publish_restores_the_previous_output_if_interrupted() -> None:
    script = BUILD_DMG.read_text(encoding="utf-8")

    cleanup = script.index("cleanup_release_build()")
    rollback = script.index(
        '"$RENAME_NO_REPLACE" "$PUBLISH_BACKUP" "$FINAL_OUT_DIR"', cleanup
    )
    cleanup_stage = script.index('rm -rf "$BUILD_ROOT"', cleanup)
    assert rollback < cleanup_stage
    assert "trap cleanup_release_build EXIT" in script
    assert "trap - EXIT INT TERM" in script
    assert "trap 'exit 130' INT" in script
    assert "trap 'exit 143' TERM" in script
    assert (
        '[[ -e "$PUBLISH_BACKUP_CANDIDATE" || -L "$PUBLISH_BACKUP_CANDIDATE" ]]'
        in script
    )
    assert "PUBLISH_COMMITTED=true" in script
    assert '"$ARTIFACT_CLASS transaction workspace preserved at $BUILD_ROOT"' in script
    assert '"ERROR: $ARTIFACT_CLASS publish ended with an uncommitted destination' in script
    assert script.count(
        '"$RENAME_NO_REPLACE" "$PUBLISH_BACKUP" "$FINAL_OUT_DIR"'
    ) >= 2
    assert '-d "$PUBLISH_BACKUP" && ! -L "$PUBLISH_BACKUP"' in script
    assert '[[ -L "$PUBLISH_BACKUP" || ! -d "$PUBLISH_BACKUP" ]]' in script
    assert "backup identity changed or disappeared during publish" in script
    assert (
        '"$RENAME_NO_REPLACE" "$FINAL_OUT_DIR" "$PUBLISH_BACKUP_CANDIDATE"'
        in script
    )
    assert 'PUBLISH_BACKUP="$PUBLISH_BACKUP_CANDIDATE"' in script


def test_app_installer_stages_then_replaces_instead_of_merging_bundles() -> None:
    script = BUILD_APP.read_text(encoding="utf-8")

    assert 'ditto --rsrc "$APP" /Applications/agentacct.app' not in script
    assert "mktemp -d /Applications/.agentacct-install.XXXXXX" in script
    assert (
        '"$INSTALL_RENAME_NO_REPLACE" "$INSTALL_TARGET" "$INSTALL_BACKUP"'
        in script
    )
    assert '"$INSTALL_RENAME_NO_REPLACE" "$INSTALL_STAGE" "$INSTALL_TARGET"' in script
    assert 'mv "$INSTALL_STAGE" "$INSTALL_TARGET"' not in script


def test_app_installer_preserves_arbitrary_existing_directory(tmp_path: Path, app_plist_reader: str) -> None:
    target = tmp_path / "agentacct.app"
    target.mkdir()
    marker = target / "user-owned.txt"
    marker.write_text("keep me", encoding="utf-8")

    completed = subprocess.run(
        [
            "bash",
            "-c",
            f"""
            INSTALL_TARGET={shlex.quote(str(target))}
            {_existing_app_ownership_guard(app_plist_reader)}
            """,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert marker.read_text(encoding="utf-8") == "keep me"
    assert "refusing to replace an unowned directory" in completed.stderr


def test_app_installer_preserves_foreign_app_bundle(tmp_path: Path, app_plist_reader: str) -> None:
    target = tmp_path / "agentacct.app"
    contents = target / "Contents"
    contents.mkdir(parents=True)
    info = contents / "Info.plist"
    with info.open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": "example.foreign.app",
                "CFBundlePackageType": "APPL",
                "CFBundleExecutable": "agentacct",
            },
            handle,
        )

    completed = subprocess.run(
        [
            "bash",
            "-c",
            f"""
            INSTALL_TARGET={shlex.quote(str(target))}
            {_existing_app_ownership_guard(app_plist_reader)}
            """,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert info.is_file()
    assert "not the app-owned agentacct bundle" in completed.stderr


def test_app_installer_accepts_exact_app_owned_bundle_identity(tmp_path: Path, app_plist_reader: str) -> None:
    target = tmp_path / "agentacct.app"
    contents = target / "Contents"
    contents.mkdir(parents=True)
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": "dev.agentacct.app",
                "CFBundlePackageType": "APPL",
                "CFBundleExecutable": "agentacct",
            },
            handle,
        )

    completed = subprocess.run(
        [
            "bash",
            "-c",
            f"""
            INSTALL_TARGET={shlex.quote(str(target))}
            {_existing_app_ownership_guard(app_plist_reader)}
            """,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_app_installer_revalidation_rejects_symlink_to_owned_bundle(
    tmp_path: Path, app_plist_reader: str,
) -> None:
    owned = tmp_path / "owned.app"
    contents = owned / "Contents"
    contents.mkdir(parents=True)
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": "dev.agentacct.app",
                "CFBundlePackageType": "APPL",
                "CFBundleExecutable": "agentacct",
            },
            handle,
        )
    target = tmp_path / "agentacct.app"
    target.symlink_to(owned, target_is_directory=True)

    completed = subprocess.run(
        [
            "bash",
            "-c",
            f"""
            INSTALL_TARGET={shlex.quote(str(target))}
            {_existing_app_verifier_function(app_plist_reader)}
            verify_existing_app_bundle "$INSTALL_TARGET"
            """,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert target.is_symlink()
    assert "not a regular app directory" in completed.stderr


def test_packaging_fails_closed_when_project_app_and_cli_versions_differ() -> None:
    app_script = BUILD_APP.read_text(encoding="utf-8")
    dmg_script = BUILD_DMG.read_text(encoding="utf-8")

    assert 'agentacct_cli_version "$FROZEN_CLI/agentacct"' in app_script
    assert '"$FROZEN_CLI_VERSION" != "$APP_VERSION"' in app_script
    assert 'plutil -extract CFBundleShortVersionString' in dmg_script
    assert '"$CLI_VERSION" != "$PROJECT_VERSION"' in dmg_script
    assert '"$PLIST_VERSION" != "$PROJECT_VERSION"' in dmg_script


def test_app_installer_preserves_the_only_backup_when_rollback_fails() -> None:
    script = BUILD_APP.read_text(encoding="utf-8")

    rollback_failure = script.index("could not activate the staged app or restore the previous app")
    rollback_cleanup = script.index('rm -rf "$INSTALL_TRANSACTION_DIR"', rollback_failure)
    assert script.index("exit 1", rollback_failure) < rollback_cleanup
    assert "backup preserved at $INSTALL_BACKUP" in script
    assert '[[ ! -e "$INSTALL_TARGET" && ! -L "$INSTALL_TARGET" ]]' in script
    occupied = script.index("target became occupied; backup preserved", rollback_failure)
    assert script.index("exit 1", occupied) < rollback_cleanup


def test_app_installer_exit_trap_restores_only_to_a_missing_target() -> None:
    script = BUILD_APP.read_text(encoding="utf-8")

    arm = script.index("trap restore_previous_app_on_abort EXIT")
    move_old = script.index(
        '"$INSTALL_RENAME_NO_REPLACE" "$INSTALL_TARGET" "$INSTALL_BACKUP"'
    )
    activate = script.index('"$INSTALL_RENAME_NO_REPLACE" "$INSTALL_STAGE" "$INSTALL_TARGET"')
    disarm = script.index("trap - EXIT INT TERM", activate)
    cleanup = script.index('rm -rf "$INSTALL_TRANSACTION_DIR"', disarm)
    assert arm < move_old < activate < disarm < cleanup
    assert '[[ ! -e "${INSTALL_TARGET:-}" && ! -L "${INSTALL_TARGET:-}" ]]' in script
    assert "interrupted install could not restore the previous app; backup preserved" in script
    assert "interrupted install found an occupied target; previous app backup preserved" in script


def test_app_installer_exit_recovery_restores_backup_after_interruption(tmp_path: Path) -> None:
    transaction = tmp_path / "transaction"
    backup = transaction / "previous.app"
    target = tmp_path / "agentacct.app"
    backup.mkdir(parents=True)
    completed = subprocess.run(
        [
            "bash",
            "-c",
            f"""
            INSTALL_TRANSACTION_DIR={shlex.quote(str(transaction))}
            INSTALL_BACKUP={shlex.quote(str(backup))}
            INSTALL_TARGET={shlex.quote(str(target))}
            INSTALL_RENAME_NO_REPLACE=/bin/mv
            {_install_recovery_function()}
            false
            restore_previous_app_on_abort
            """,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert target.is_dir()
    assert not transaction.exists()
    assert "restored previous app after interrupted install" in completed.stderr


def test_app_installer_exit_recovery_preserves_backup_when_target_is_occupied(
    tmp_path: Path,
) -> None:
    transaction = tmp_path / "transaction"
    backup = transaction / "previous.app"
    target = tmp_path / "agentacct.app"
    backup.mkdir(parents=True)
    target.mkdir()
    completed = subprocess.run(
        [
            "bash",
            "-c",
            f"""
            INSTALL_TRANSACTION_DIR={shlex.quote(str(transaction))}
            INSTALL_BACKUP={shlex.quote(str(backup))}
            INSTALL_TARGET={shlex.quote(str(target))}
            INSTALL_RENAME_NO_REPLACE=/bin/mv
            {_install_recovery_function()}
            false
            restore_previous_app_on_abort
            """,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert backup.is_dir()
    assert transaction.is_dir()
    assert "previous app backup preserved" in completed.stderr


def test_app_installer_exit_recovery_preserves_backup_when_target_is_dangling_symlink(
    tmp_path: Path,
) -> None:
    transaction = tmp_path / "transaction"
    backup = transaction / "previous.app"
    target = tmp_path / "agentacct.app"
    backup.mkdir(parents=True)
    target.symlink_to(tmp_path / "missing.app")
    completed = subprocess.run(
        [
            "bash",
            "-c",
            f"""
            INSTALL_TRANSACTION_DIR={shlex.quote(str(transaction))}
            INSTALL_BACKUP={shlex.quote(str(backup))}
            INSTALL_TARGET={shlex.quote(str(target))}
            INSTALL_RENAME_NO_REPLACE=/bin/mv
            {_install_recovery_function()}
            false
            restore_previous_app_on_abort
            """,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert target.is_symlink()
    assert backup.is_dir()
    assert transaction.is_dir()
    assert "previous app backup preserved" in completed.stderr


def test_app_installer_inline_rollback_refuses_dangling_symlink_target(
    tmp_path: Path,
) -> None:
    transaction = tmp_path / "transaction"
    backup = transaction / "previous.app"
    target = tmp_path / "agentacct.app"
    backup.mkdir(parents=True)
    target.symlink_to(tmp_path / "missing.app")
    completed = subprocess.run(
        [
            "bash",
            "-c",
            f"""
            INSTALL_BACKUP={shlex.quote(str(backup))}
            INSTALL_TARGET={shlex.quote(str(target))}
            if {_inline_rollback_condition()}; then
                exit 99
            fi
            """,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert target.is_symlink()
    assert backup.is_dir()


def test_app_installer_activation_failure_preserves_backup_when_target_becomes_occupied(
    tmp_path: Path,
) -> None:
    transaction = tmp_path / "transaction"
    stage = transaction / "agentacct.app"
    backup = transaction / "previous.app"
    target = tmp_path / "agentacct.app"
    stage.mkdir(parents=True)
    backup.mkdir()
    target.symlink_to(tmp_path / "missing.app")
    completed = subprocess.run(
        [
            "bash",
            "-c",
            f"""
            INSTALL_TRANSACTION_DIR={shlex.quote(str(transaction))}
            INSTALL_STAGE={shlex.quote(str(stage))}
            INSTALL_BACKUP={shlex.quote(str(backup))}
            INSTALL_TARGET={shlex.quote(str(target))}
            INSTALL_RENAME_NO_REPLACE=/usr/bin/false
            {_activation_failure_block()}
            """,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert target.is_symlink()
    assert backup.is_dir()
    assert transaction.is_dir()
    assert f"backup preserved at {backup}" in completed.stderr


def test_app_installer_exit_recovery_preserves_backup_when_restore_command_fails(
    tmp_path: Path,
) -> None:
    transaction = tmp_path / "transaction"
    backup = transaction / "previous.app"
    target = tmp_path / "agentacct.app"
    backup.mkdir(parents=True)
    completed = subprocess.run(
        [
            "bash",
            "-c",
            f"""
            INSTALL_TRANSACTION_DIR={shlex.quote(str(transaction))}
            INSTALL_BACKUP={shlex.quote(str(backup))}
            INSTALL_TARGET={shlex.quote(str(target))}
            INSTALL_RENAME_NO_REPLACE=/usr/bin/false
            {_install_recovery_function()}
            false
            restore_previous_app_on_abort
            """,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert not target.exists()
    assert backup.is_dir()
    assert transaction.is_dir()
    assert f"backup preserved at {backup}" in completed.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="renamex_np is a macOS release primitive")
def test_no_replace_rename_rejects_existing_directory_without_nesting(tmp_path: Path) -> None:
    helper = tmp_path / "rename-no-replace"
    compiled = subprocess.run(
        [
            "xcrun",
            "clang",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(RENAME_NO_REPLACE),
            "-o",
            str(helper),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stderr

    source = tmp_path / "release-stage"
    destination = tmp_path / "release"
    source.mkdir()
    destination.mkdir()
    (source / "staged").write_text("new", encoding="utf-8")
    (destination / "current").write_text("old", encoding="utf-8")

    refused = subprocess.run(
        [str(helper), str(source), str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert refused.returncode == 1
    assert source.is_dir()
    assert (source / "staged").read_text(encoding="utf-8") == "new"
    assert (destination / "current").read_text(encoding="utf-8") == "old"
    assert not (destination / source.name).exists()

    activated = tmp_path / "activated"
    moved = subprocess.run(
        [str(helper), str(source), str(activated)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert moved.returncode == 0, moved.stderr
    assert not source.exists()
    assert (activated / "staged").read_text(encoding="utf-8") == "new"
