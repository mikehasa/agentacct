#!/bin/bash
# Shared source identity and frozen-CLI provenance checks for macOS packaging.

AGENTACCT_SOURCE_COMMIT_FILE=".agentacct-source-commit"
AGENTACCT_SOURCE_DESCRIPTION_FILE=".agentacct-source-description"

agentacct_project_version() {
    local repo_root="$1"
    awk '
        /^\[project\]$/ { in_project = 1; next }
        /^\[/ { in_project = 0 }
        in_project && /^version[[:space:]]*=/ {
            value = $0
            sub(/^[^=]*=[[:space:]]*"/, "", value)
            sub(/"[[:space:]]*$/, "", value)
            print value
            exit
        }
    ' "$repo_root/pyproject.toml"
}

agentacct_cli_version() {
    local cli="$1"
    local output
    if [[ ! -x "$cli" ]]; then
        echo "ERROR: frozen CLI is missing or not executable: $cli" >&2
        return 1
    fi
    if ! output="$("$cli" --version)"; then
        echo "ERROR: frozen CLI --version failed: $cli" >&2
        return 1
    fi
    if [[ ! "$output" =~ ^agentacct[[:space:]]+([0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
        echo "ERROR: frozen CLI returned an invalid version: $output" >&2
        return 1
    fi
    printf '%s\n' "${BASH_REMATCH[1]}"
}

agentacct_source_commit() {
    git -C "$1" rev-parse HEAD
}

agentacct_source_description() {
    local repo_root="$1"
    local description
    description="$(git -C "$repo_root" describe --tags --always)"
    if [[ -n "$(git -C "$repo_root" status --porcelain=v1 --untracked-files=normal)" ]]; then
        description="${description}-dirty"
    fi
    printf '%s\n' "$description"
}

agentacct_require_clean_source() {
    local repo_root="$1"
    if [[ -n "$(git -C "$repo_root" status --porcelain=v1 --untracked-files=normal)" ]]; then
        echo "ERROR: frozen CLI provenance requires a clean source tree; commit or stash source changes before packaging" >&2
        return 1
    fi
}

agentacct_assert_source_identity() {
    local repo_root="$1"
    local expected_commit="$2"
    local expected_description="$3"
    local current_commit
    local current_description

    current_commit="$(agentacct_source_commit "$repo_root")"
    current_description="$(agentacct_source_description "$repo_root")"
    if [[ "$current_commit" != "$expected_commit" || "$current_description" != "$expected_description" ]]; then
        echo "ERROR: source identity changed during packaging; discard the output and rerun from the pinned release commit" >&2
        return 1
    fi
}

agentacct_write_source_provenance() {
    local repo_root="$1"
    local frozen_cli="$2"

    agentacct_require_clean_source "$repo_root" || return 1
    agentacct_source_commit "$repo_root" >"$frozen_cli/$AGENTACCT_SOURCE_COMMIT_FILE"
    agentacct_source_description "$repo_root" >"$frozen_cli/$AGENTACCT_SOURCE_DESCRIPTION_FILE"
}

agentacct_verify_source_provenance() {
    local repo_root="$1"
    local frozen_cli="$2"
    local commit_file="$frozen_cli/$AGENTACCT_SOURCE_COMMIT_FILE"
    local description_file="$frozen_cli/$AGENTACCT_SOURCE_DESCRIPTION_FILE"
    local expected_commit
    local expected_description
    local frozen_commit
    local frozen_description

    if [[ ! -f "$commit_file" || ! -f "$description_file" ]]; then
        echo "ERROR: frozen CLI provenance is missing from $frozen_cli; rerun packaging/freeze-cli.sh" >&2
        return 1
    fi

    agentacct_require_clean_source "$repo_root" || return 1
    expected_commit="$(agentacct_source_commit "$repo_root")"
    expected_description="$(agentacct_source_description "$repo_root")"
    frozen_commit="$(<"$commit_file")"
    frozen_description="$(<"$description_file")"

    if [[ "$frozen_commit" != "$expected_commit" || "$frozen_description" != "$expected_description" ]]; then
        echo "ERROR: frozen CLI provenance does not match the app source; rerun packaging/freeze-cli.sh" >&2
        return 1
    fi
}
