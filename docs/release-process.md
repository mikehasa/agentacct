# Release process

This is the process for publishing an `agentacct X.Y.Z` release. It is one
path, not a menu: the version bump, the notarized macOS DMG, and the two
publication gates happen in this order, the same way every time.

Two things make a release incomplete, and both are checked by CI rather than by
memory:

- the tag and the version disagree (the `build` job refuses to build);
- the published release carries no `.dmg` asset (the `release-dmg-asset` job
  blocks the real-PyPI job).

The DMG is a first-class artifact of every release. It is not "the CLI release
plus a nice-to-have app": the `.app` embeds the frozen Python CLI, so the DMG
is how a non-developer gets this version at all.

## Conventions (do not drift)

- **Release commit + PR title**: `chore(release): X.Y.Z`
- **GitHub release title**: exactly `agentacct X.Y.Z`
- **Release branch**: `release-X.Y.Z`, cut from `origin/main`, developed in a
  git worktree rather than in a live checkout.
- **Credentials never enter the repository.** The maintainer holds a
  *Developer ID Application* certificate and a `notarytool` keychain profile in
  the login keychain; the release runs pass them through environment variables
  (`DEVELOPER_ID`, `RELEASE_TEAM_ID`, `NOTARY_PROFILE`) and never read or print
  the secret. On the maintainer's machine the concrete values, the one-time
  owner setup, and the shell incantations live in the machine-local annex
  `RELEASE.local.md` (gitignored) — everything below works from a fresh clone
  if you have those credentials.

## 1. Version bump — two authored places, one derived value

CI's `build` job fails with "tag does not match pyproject version" unless the
tag and `pyproject.toml` agree, so keep the two authored sources in sync:

1. `pyproject.toml` → `version = "X.Y.Z"`
2. `CHANGELOG.md` → a new `## [X.Y.Z] — YYYY-MM-DD` section under
   `## [Unreleased]`, plus the `[Unreleased]` / `[X.Y.Z]` link refs at the
   bottom.

`apps/agentacct/Scripts/build-app.sh` derives `CFBundleShortVersionString` and
`CFBundleVersion` from `pyproject.toml`; there is no third version string.

## 2. PR → merge → pin the release commit

Open `release-X.Y.Z` → `main`, wait for CI green, squash-merge, then pin the
immutable squash commit and build from a **fresh detached worktree at that
exact SHA** — never from the release branch, whose pre-squash commit has the
same content but a different identity:

```bash
RELEASE_SHA="$(gh pr view <n> --json mergeCommit --jq .mergeCommit.oid)"
git fetch origin main --tags
git worktree add --detach <build-worktree> "$RELEASE_SHA"
test -z "$(git -C <build-worktree> status --porcelain=v1 --untracked-files=normal)"
```

The tag target, the app's `AgentacctGitCommit`, the embedded CLI provenance and
the release completion marker must all equal `RELEASE_SHA`; content equivalence
is not a substitute for identity equality.

## 3. Prepare the DMG (mandatory)

The DMG embeds the frozen Python CLI (`build-app.sh` copies
`packaging/dist/agentacct/` into `agentacct.app/Contents/Resources/cli`), so a
Python-only change changes what the DMG *runs*. Reusing an old DMG across a
backend fix silently ships users the old backend.

### 3a. Preflight — reuse or rebuild? (never skip)

Diff the DMG payload since the previous release tag and decide:

```bash
git diff v<prev>...HEAD --stat -- src/agentacct apps/agentacct packaging
git diff v<prev>...HEAD -- pyproject.toml | grep -iE '^\+|^-' | grep -i depend
```

- any change under `src/agentacct/**`, `apps/agentacct/**`, `packaging/**`, or
  the `[project].dependencies` list → **rebuild** (3c);
- only docs / tests / CHANGELOG / other unshipped files → **reuse** (3b);
- when in doubt, rebuild. A needless rebuild is cheap; a stale DMG is not.

Record the decision and the one-line diff evidence in the release PR or the
release notes so the call is auditable.

### 3b. Reuse (payload unchanged)

Download the previous release's DMG and verify it as-is. Its internal
`CFBundleShortVersionString` is honest about which build it is, so never rename
it to imply a version it does not contain.

```bash
gh release download v<prev> --repo <owner>/<repo> --pattern "*.dmg" --dir <tmp>
ASSET_VERSION=<version in the reused DMG filename>
ASSET_SHA="$(git rev-list -n 1 "v$ASSET_VERSION")"
bash packaging/verify-dmg.sh <tmp>/agentacct-$ASSET_VERSION.dmg \
    "$ASSET_VERSION" "$RELEASE_TEAM_ID" "$ASSET_SHA"
```

If the older asset lacks the required provenance or fails any verifier check,
switch to a rebuild — never weaken the verifier to preserve the reuse.

### 3c. Rebuild (one command, never hand-assembled)

```bash
DEVELOPER_ID="Developer ID Application: <name> (<TEAMID>)" \
RELEASE_TEAM_ID="<trusted 10-character Apple Team ID>" \
NOTARY_PROFILE="<keychain profile>" \
bash packaging/build-dmg.sh --release

bash packaging/verify-dmg.sh packaging/release/agentacct-X.Y.Z.dmg \
    X.Y.Z "$RELEASE_TEAM_ID" "$RELEASE_SHA"
```

`--release` validates the named signing identity and the keychain notary
profile before doing any build work, freezes the CLI, builds the app with the
CLI embedded, fails closed unless `pyproject.toml`, the app's
`CFBundleShortVersionString` and the embedded CLI's `agentacct --version` all
agree, signs inside-out with the hardened runtime, builds the DMG, submits it
for notarization, staples it, verifies the mounted artifact, and only then
publishes `packaging/release/` by an atomic no-replace rename. A failed attempt
leaves the last successful output untouched. Do not reproduce those steps by
hand: a manual partial run bypasses the fail-closed guards and is not a release
artifact.

`RELEASE_TEAM_ID` must be independently known from the Apple Developer
membership, never inferred from the artifact being verified. `spctl` on the DMG
*file* reports "no usable signature" because the container is unsigned by
design; the verifier runs `spctl` on the `.app` inside it.

## 4. Create the release with its DMG

```bash
gh release create vX.Y.Z --target "$RELEASE_SHA" \
  --title "agentacct X.Y.Z" \
  --notes-file <notes.md> \
  "<path>/agentacct-<asset-version>.dmg#agentacct-<asset-version>.dmg"
```

- `<asset-version>` is the previous payload version for an audited reuse, and
  `X.Y.Z` for a rebuild.
- Use `--notes-file`, never a heredoc.
- The notes must feature the DMG: the download link
  `https://github.com/<owner>/<repo>/releases/download/vX.Y.Z/<dmg-name>` plus
  the auto-listed Assets entry.
- Confirm `git rev-list -n 1 vX.Y.Z` equals `RELEASE_SHA` right after.

Uploading the asset here (rather than by hand later) is what the CI gate
observes; a release published without it cannot reach real PyPI.

## 5. Double trigger — expected, not a bug

`gh release create` fires two `publish.yml` runs: the `release` event (full
chain, including real PyPI) and the `push` tag event (TestPyPI only, its
`publish-pypi` skipped by design). They serialize on a shared concurrency
group, so the tag run often queues behind the release run.

## 6. Owner approves the real-PyPI gate

The `release` run pauses before `publish-pypi` for the `pypi` environment's
required reviewer; it only gets there after `release-dmg-asset` has confirmed
the release carries its DMG. Approve in the run's **Review deployments**
panel. The credential is OIDC — a transient sigstore/OIDC failure is retried by
re-running the job.

## 7. Verify (every time)

- PyPI: `curl -s https://pypi.org/pypi/agentacct/json` reports `info.version`
  and carries both wheel and sdist, not yanked.
- Fresh venv: install the published version and check
  `agentacct --version`.
- Release assets: the `.dmg` is present,
  `https://github.com/<owner>/<repo>/releases/download/vX.Y.Z/agentacct-X.Y.Z.dmg`
  returns HTTP 200, its byte size and SHA-256 match the built file, and
  `packaging/verify-dmg.sh` passes on the **downloaded** copy against the
  honest payload version and commit (the release SHA for a rebuild, the audited
  source tag for a reuse).

## 8. Clean up

Remove the detached build worktree and the release worktree
(`git worktree remove`), delete the remote `release-X.Y.Z` branch (GitHub does
not), and delete the local branch with `git branch -D` (the squash merge leaves
it "unmerged" by SHA).

## The CI gate

`release-dmg-asset` in `.github/workflows/publish.yml` runs on a published
release and fails unless that release carries a `.dmg` asset; real PyPI waits
on it. It polls briefly because `gh release create` publishes the release
before it finishes uploading assets, and it deliberately accepts any `.dmg`
name so an audited reuse (3b) keeps the previous version's filename.

The gate proves the asset exists, not that it is fresh or correctly signed: the
reuse-vs-rebuild preflight (3a) and the mounted-artifact verifier (3c) are what
prove the payload, and they stay part of the process.
