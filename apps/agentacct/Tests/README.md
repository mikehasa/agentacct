# Dashboard snapshot testing

This is the contributor entry point for reviewing dashboard UI changes. The
harness renders the real SwiftUI dashboard from a synthetic API fixture, so a
human or coding agent can reproduce the same review surfaces without starting
the daemon, connecting an account, or rebuilding test infrastructure.

## Quick start

Requirements: macOS 14 or newer and Swift 5.9 or newer.

From `apps/agentacct`:

```bash
swift test
swift run agentacct --snapshot-dashboard-fixture \
  Tests/agentacctTests/Fixtures/dashboard.json \
  /tmp/agentacct-dashboard-review
open /tmp/agentacct-dashboard-review
```

The command writes this fixed review matrix at 2x scale:

| Artifact | Viewport | Pixel size |
| --- | --- | --- |
| `dashboard-minimum-light.png` | 960 × 560 pt, light | 1920 × 1120 px |
| `dashboard-minimum-dark.png` | 960 × 560 pt, dark | 1920 × 1120 px |
| `dashboard-reference-light.png` | 1120 × 680 pt, light | 2240 × 1360 px |
| `dashboard-reference-dark.png` | 1120 × 680 pt, dark | 2240 × 1360 px |

Generated PNGs belong in a temporary directory outside the repository. Do not
commit them unless a later change intentionally introduces reviewed golden
baselines.

On every pull request and push to `main`, the `macOS app and dashboard
snapshots` CI job runs the same tests, builds the release executable, and
publishes the four PNGs as `dashboard-review-<commit SHA>` for 14 days. Download
that artifact from the workflow run when a reviewer does not have a local macOS
environment.

## What is under test

- `Fixtures/dashboard.json` is one coherent set of synthetic `/v1/glance`,
  `/v1/sessions`, `/v1/plan`, and usage responses. It contains no personal data.
- `DashboardSnapshotFixture` decodes those responses through the same wire
  models as the live app and rejects unsupported versioned schemas.
- `DashboardStore(preloaded:)` injects the decoded state without network access.
- `DashboardSnapshotRenderer` renders the real `MainWindow` at two supported
  viewport sizes and both system appearances. Snapshot mode disables live
  refresh, bounds scroll content to the viewport, and explicitly includes the
  packaged app's setup entry point.
- `DashboardSnapshotHarnessTests` verifies the exact artifact matrix and pixel
  dimensions, meaningful light/dark differences, and bounded repeat-render
  stability.

The current tests do **not** approve the dashboard's taste or compare it with a
reviewed golden image. A human must still inspect all four PNGs for hierarchy,
spacing, clipping, contrast, wording, and cross-appearance consistency. A
future baseline stage can automate visual-diff review after the design is
approved.

## Extend the harness without rebuilding it

### Change representative data

Edit `Fixtures/dashboard.json`, keeping it synthetic and internally coherent.
Use realistic boundary cases that change the UI meaningfully, such as long
labels, missing optional values, failed work, or large metrics. Keep every
versioned `schema` value aligned with the endpoint contract. Run `swift test`
afterward; decoding or schema drift should fail with the affected payload name.

Do not put access tokens, discovery files, home-directory paths, live account
data, or current timestamps in a fixture. Prefer fixed values and stable text.

### Add a viewport or appearance

1. Add the configuration to `DashboardSnapshotConfiguration.reviewConfigurations`.
2. Add its literal filename and 2x pixel dimensions to `expectedArtifacts` in
   `DashboardSnapshotHarnessTests`. The test expectation is intentionally
   independent of production configuration so an accidental removal fails.
3. Regenerate the artifacts and inspect the entire matrix, not only the new file.
4. Update the matrix table above.

### Add a scenario

Prefer a separate named fixture when the scenario represents a distinct product
state. Keep viewport and appearance configuration shared. Add a focused test
for the state-specific contract instead of weakening the core matrix. If the
scenario needs time, locale, packaging, or other environment state, inject that
state explicitly at the render boundary—never read it implicitly during a
snapshot.

## Determinism rules

- Snapshot rendering must not contact the daemon, inspect personal state, or
  depend on whether the executable came from SwiftPM or an app bundle.
- Use fixed fixture values. Inject clocks, locale, or packaging state if a new
  UI element depends on them.
- Disable animation or wait for a documented settled state. Repeated renders
  allow at most one 8-bit color step on at most 0.1% of normalized pixel bytes
  for Core Graphics antialiasing.
- Test decoded pixels, not PNG file bytes. PNG metadata or compression can
  change without changing what the user sees.
- Keep generated review artifacts outside Git until golden-baseline ownership,
  update workflow, and failure diagnostics are intentionally designed.

## Review checklist

Before asking for review:

1. Run `swift test` and a release build with `swift build -c release`.
2. Generate all four artifacts with the quick-start command.
3. Inspect minimum and reference viewports in both light and dark appearances.
4. Check for clipping, unintended scrolling, unstable relative-time text,
   missing controls, low contrast, and information hierarchy regressions.
5. Confirm fixture changes are synthetic, minimal, and exercise an intentional
   product state.
6. Review the complete branch diff and keep generated PNGs out of Git.

## Troubleshooting

- **Unsupported schema:** update the fixture only after confirming the app's
  endpoint model supports the new schema. The error names the rejected payload.
- **No image produced:** run on macOS 14+ from a logged-in graphical session;
  SwiftUI's `ImageRenderer` requires the macOS rendering stack.
- **Unexpected live values:** rendering must use
  `--snapshot-dashboard-fixture`; the older `--snapshot` command intentionally
  exercises live daemon data.
- **Wrong output size:** point sizes are rendered at 2x. Update the production
  configuration, the independent test expectation, and this guide together.
