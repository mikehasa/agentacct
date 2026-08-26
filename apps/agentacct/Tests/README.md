# Visual snapshot testing

This is the contributor entry point for reviewing macOS UI changes. The
harness renders the real SwiftUI surface from versioned synthetic endpoint
responses, so a human or coding agent can reproduce it without starting the
daemon, connecting an account, or creating another snapshot system.

## Quick start

Run the ordinary deterministic and unit checks during development:

```bash
swift test
```

Discover the available visual regression suites:

```bash
./Scripts/visual-snapshots list
```

Check whether the current host exactly matches the reviewed-reference renderer:

```bash
./Scripts/visual-snapshots check-environment
```

This project currently discovers `DashboardVisualRegressionTests`. An empty
list remains valid for another project or branch before its first visual suite.

Verify one suite before changing its UI:

```bash
./Scripts/visual-snapshots verify DashboardVisualRegressionTests
open .build/visual-snapshot-failures  # only when verification fails
```

Verification is read-only. A mismatch fails and writes three review artifacts:

- `*.expected.png` — the reviewed image in Git;
- `*.actual.png` — the current render;
- `*.diff.png` — changed pixels in magenta, unchanged pixels dimmed.

After the new UI has been reviewed, explicitly replace only that suite:

```bash
./Scripts/visual-snapshots record \
  Tests/agentacctTests/DashboardVisualRegressionTests.swift
git diff -- Tests/agentacctTests/ReferenceImages
```

Record validates every PNG, writes references atomically, and immediately runs
the selected test again in verify mode. It never stages or commits files and is
rejected in CI. If a new render is already within the narrow antialiasing
tolerance, the command retains the existing reference byte-for-byte to prevent
meaningless Git churn.

GitHub displays a changed PNG as a
[2-up, swipe, or onion-skin comparison](https://docs.github.com/en/repositories/working-with-files/using-files/working-with-non-code-files#rendering-and-diffing-images).
The reference update is therefore the reviewer-facing before/after artifact.

## Selecting tests

The canonical identity is the specifier reported by `swift test list`. The CLI
accepts the same common shapes used by native test runners:

| Command target | Selection |
| --- | --- |
| no target | every discovered `*VisualRegressionTests` suite |
| `DashboardVisualRegressionTests` | one suite |
| `DashboardVisualRegressionTests/testDashboard` | one test method |
| `agentacctTests.DashboardVisualRegressionTests/testDashboard` | one fully qualified test |
| `Tests/agentacctTests/DashboardVisualRegressionTests.swift` | the suite matching the file basename |

A file path is only a convenience. SwiftPM does not expose source-file
ownership in its discovered test list, so the basename must map unambiguously
to a discovered `*VisualRegressionTests` suite. Zero or multiple matches fail
before any test runs. There is no separate snapshot ID registry to maintain.

Multiple targets are accepted. Prefer one suite per intentional reference
update so the resulting Git diff stays easy to review.

## Dashboard review matrix

The dashboard renderer owns this complete fixed matrix at 2x scale:

| Artifact | Viewport | Pixel size |
| --- | --- | --- |
| `dashboard-minimum-light.png` | 960 × 560 pt minimum window, light; single-column viewport | 1920 × 1120 px |
| `dashboard-minimum-dark.png` | 960 × 560 pt minimum window, dark; single-column viewport | 1920 × 1120 px |
| `dashboard-reference-light.png` | 1120 × 800 pt standard window, light; complete two-column dashboard | 2240 × 1600 px |
| `dashboard-reference-dark.png` | 1120 × 800 pt standard window, dark; complete two-column dashboard | 2240 × 1600 px |

References live under `Tests/agentacctTests/ReferenceImages/<platform-id>`.
They are read directly from the source checkout and excluded from SwiftPM's
test resources, so the test bundle does not duplicate reviewer-only PNGs.

For an ad-hoc render that does not compare or update references:

```bash
swift run agentacct --snapshot-dashboard-fixture \
  Tests/agentacctTests/Fixtures/dashboard.json \
  /tmp/agentacct-dashboard-review
open /tmp/agentacct-dashboard-review
```

Keep ad-hoc PNGs outside the repository.

## Color contrast guardrail

`Theme.Palette` is the source of truth for each semantic color's light and dark
hex values. Following
[WCAG 2.2 SC 1.4.3](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html),
`ThemeContrastTests` requires small text roles to reach at least 4.5:1 on every
dashboard surface and applies the same target to text inside tinted chips. When
changing a palette value or chip opacity, run:

```bash
swift test --filter ThemeContrastTests
```

Do not lower the target to make a new color pass. Choose a nearby accessible
value, render the four dashboard artifacts, and inspect both appearances.

## Flakiness contract

Image snapshots are renderer output, not portable drawings. Point-Free's
SnapshotTesting explicitly warns that image references must be created and
compared in the same rendering environment. GitHub's hosted runner images are
updated weekly, so an OS label alone is not sufficient.

The CLI fails before comparing or recording reviewed pixels unless the exact
renderer is:

```text
macos-26.5.2-25F84-xcode-26.6-17F113-arm64-2x
```

That identity includes macOS product and build, Xcode version and build,
architecture, and scale. A mismatch is an explicit baseline-platform migration,
not a pixel failure. Never bypass it by copying references between platforms.

GitHub hosted images are mutable even under a versioned runner label. CI always
runs the semantic suite, renders the matrix twice for deterministic comparison,
and uploads a fresh review matrix. It compares that render with reviewed PNGs
only when `check-environment` confirms the exact canonical renderer. This keeps
host-image updates visible without turning an OS update into a false product
regression or silently approving new pixels.

Every nondeterministic input used by the current dashboard has an executable
control:

| Risk | Control |
| --- | --- |
| live or changing data | versioned synthetic JSON; wire decoders reject unsupported schemas |
| wall-clock labels | fixture `glance.generated_at` supplies `SnapshotMode.currentDate`; missing time fails |
| locale and dates | `en_US_POSIX`, Gregorian calendar, and UTC in the process and SwiftUI environment |
| geometry | fixed proposed size, viewport, 2x renderer and display scale, and exact pixel assertions |
| appearance | explicit light/dark scheme and left-to-right layout |
| host UI preferences | fixed dynamic type, control size, legibility weight, and active appearance; the exact OS build pins system fonts |
| animations and hover | animations disabled; no pointer enters the offscreen surface |
| async and local state | snapshot mode suppresses polling, setup prompts, and daemon access |
| map ordering | arrays remain ordered and dictionary-derived chart clients are sorted |
| color profiles | renderer uses non-linear color mode; comparison normalizes both PNGs to sRGB RGBA8 |
| raster rounding | at most one 8-bit channel step across 0.1% of normalized channels |
| baseline churn | a within-budget recording retains the reviewed PNG byte-for-byte |
| stale diagnostics | successful verify or record removes that snapshot's old failure files |
| accidental approval | verify never writes; record is explicit, atomic, reruns verify, and is disabled in CI |

Dimensions must match exactly. Do not loosen the pixel budget to make a real
change pass.

The normal deterministic test renders the four-image matrix twice. When
adopting a new renderer environment, also stress separate processes:

```bash
for run_index in {1..10}; do
  swift test --skip-build \
    --filter '^agentacctTests\.DashboardSnapshotHarnessTests/testRendersEveryDashboardReviewConfiguration$'
done
```

The current renderer passed this check: ten processes, eight images per
process, 80 total renders, and no out-of-budget drift.

## What each layer catches

`DashboardInteractionTests` is the compact semantic layer. Table-driven tests
cover every dashboard destination, active-session states, the task-first review
projection, stale-selection cleanup, cost completeness, missing values, and
Tokens/Cost totals. A clock test prevents relative refresh copy from
reintroducing snapshot flakiness. These tests keep product semantics separate
from pixels without repeating view-tree assertions.

`DashboardSnapshotHarnessTests` catches fixture/schema/store-wiring drift, a
missing appearance or viewport, wrong dimensions, wall-clock leaks, dynamic
content, animation, and same-process instability. It runs wherever Swift tests
run and does not need approved images.

`VisualSnapshotHarnessTests` checks the dependency-free image lifecycle:
normalization, strict magnitude and changed-area tolerances, honest dimension
mismatch diagnostics, expected/actual/diff creation, safe mode parsing, CI
recording rejection, atomic writes, changed-reference replacement, invalid
render rejection, baseline retention, and stale artifact cleanup.

`visual-snapshots-cli-tests.sh` uses a fake SwiftPM test list and renderer
environment. It verifies path and selector resolution, exact filtering,
record-then-verify behavior, CI safety, and fail-fast platform checks without
rendering the app.

Each `*VisualRegressionTests` suite compares its complete surface matrix with
reviewed PNGs. The CLI supplies `AGENTACCT_VERIFY_VISUAL_BASELINES=1`,
`AGENTACCT_SNAPSHOT_MODE`, the exact platform ID, and the failure directory.
Ordinary `swift test` runs compile the visual suites but skip their baseline
comparison, so semantic tests remain useful on non-canonical developer
machines. Always use the CLI when the intent is to verify visual references.

### Dashboard interaction coverage

| Interaction contract | Automated evidence | Installed-app check |
| --- | --- | --- |
| Navigation tabs; recent-work, review, active-work, and Limits destinations | destination matrix, task-first review projection, and stable accessibility identifiers | activate each control and confirm its pane/detail |
| Outcome, evidence, cost, recency, long labels, and responsive layout | projection tests plus the four-image review matrix | resize through the 960 pt minimum and scroll once |
| Tokens/Cost selection, totals, missing values, and mark labels | series tests plus default chart snapshots | hover, click, and keyboard-focus a mark in each series |
| Light/dark appearance, reduced motion, and reduced transparency | explicit scheme matrix; material policy test; animations disabled in snapshots | switch system appearance; enable Reduce Motion and Reduce Transparency, then repeat navigation |
| Refresh and setup presentation | deterministic freshness test and accessibility identifiers | refresh against the local daemon; open and dismiss setup |

ImageRenderer cannot synthesize pointer, keyboard, sheet, or daemon events.
Those boundaries remain a short manual pass until the Swift package gains a
host-app XCUITest target; the identifiers above are the migration path. Do not
duplicate them with brittle view-tree tests in the meantime.

## Intentional update checklist

1. Run `./Scripts/visual-snapshots verify <target>` and confirm that the old
   reference fails only for intended surfaces.
2. Open every expected, actual, and diff artifact. Check hierarchy, spacing,
   clipping, contrast, wording, and light/dark consistency.
3. Have a human review the finished render before recording.
4. Run `./Scripts/visual-snapshots record <same-target>`.
5. Review every changed PNG in Git, including images that seem unchanged.
6. Run verify, `swift test`, and `swift build -c release` again.
7. Commit the UI code and reviewed references together.

Never record automatically after failure. That converts an unexpected
regression into an approved image without review.

## Add a visual regression suite

1. Name the XCTest file and class `FeatureVisualRegressionTests.swift` and
   `FeatureVisualRegressionTests`.
2. Put a versioned synthetic fixture in `Tests/agentacctTests/Fixtures` and
   load it through production wire decoders.
3. Make one test method own the complete meaningful matrix for that surface.
4. Render to a unique temporary directory and remove it with `defer`.
5. Resolve references under
   `ReferenceImages/$AGENTACCT_SNAPSHOT_PLATFORM_ID`.
6. Resolve `VisualSnapshotMode` from the process environment.
7. Call `VisualSnapshotHarness.assertSnapshot` once for each rendered PNG.
8. Add independent assertions for filenames and pixel dimensions so deleting a
   configuration cannot silently shrink the contract.
9. Run the CLI integration test, deterministic render test, record, and verify.
10. Add the complete review matrix to CI artifacts.

Snapshot names must be unique within the shared failure directory. A visual
test should test visual contracts; keep behavior, formatting, and
accessibility assertions in focused semantic tests as well.

## Change representative data

Edit `Fixtures/dashboard.json`, keeping it synthetic and internally coherent.
Use realistic states that alter the UI meaningfully: long labels, absent
optional values, failed work, blocked work, large metrics, and time-relative
labels. Keep every `schema` aligned with the endpoint contract.

Do not add tokens, discovery files, home-directory paths, live account data,
random identifiers, or current timestamps. If a UI element depends on time,
locale, packaging, accessibility settings, or other environment state, inject
that state at the render boundary and add a focused determinism assertion.

## Add a dashboard viewport or appearance

1. Add it to `DashboardSnapshotConfiguration.reviewConfigurations`.
2. Add its literal filename and 2x dimensions to `expectedArtifacts` in
   `DashboardSnapshotHarnessTests`. This independent list makes accidental
   removal fail.
3. Record the expanded matrix and inspect every image.
4. Update the matrix table above and CI review artifact.

Prefer a separately named fixture for a distinct product state and share the
viewport matrix.

## Why there is no snapshot dependency

The workflow follows established open-source tools while keeping its
implementation project-sized:

- [Swift Package Manager](https://docs.swift.org/swiftpm/documentation/packagemanagerdocs/swifttest/)
  supplies discovered selectors and native regex filtering.
- [Point-Free SnapshotTesting](https://github.com/pointfreeco/swift-snapshot-testing)
  informs test-derived references, explicit recording, and exact-environment
  guidance.
- [Uber ios-snapshot-test-case](https://github.com/uber/ios-snapshot-test-case)
  informs separate reference and failure paths and test-derived naming.
- [Cash App Paparazzi](https://github.com/cashapp/paparazzi) informs separate
  record/verify commands, source-controlled goldens, and reviewable diffs.

SnapshotTesting 1.19.4 was evaluated directly. Its API is mature, but its
manifest has moved to Swift tools 6 while this project retains 5.9
compatibility, and its package graph also resolves `swift-syntax` and
`swift-custom-dump`. A clean probe spent 134 seconds fetching before compilation
and had already downloaded about 75 MB when stopped. Its plain `swift test`
failure path also does not preserve the same expected/actual/diff directory
contract without additional glue.

For four dashboard surfaces, the tested local helper is smaller operationally,
keeps strict maximum-delta semantics, and adds no dependency. Reconsider the
library if the project needs framework-level device traits, many snapshot
formats, or enough suites that maintaining the local image lifecycle becomes
more expensive than the package graph.

## CI behavior

The macOS job pins the explicit `macos-26` arm64 runner label and Xcode path,
then invokes the CLI to verify the exact build fingerprint and dashboard
references. On a visual mismatch it
uploads expected, actual, and diff PNGs. It also publishes the complete current
dashboard matrix so a reviewer can evaluate the finished app without macOS.

Because hosted images move, a runner update should fail with the renderer
migration message before visual tests run. Verify the new environment, run the
ten-process stress check, update the one CLI renderer identity, record new
references under its new platform directory, and review that migration
separately from product UI changes.

## Troubleshooting

- **No suites discovered:** visual test classes must end in
  `VisualRegressionTests`; confirm them with `swift test list`.
- **Path does not resolve:** the `.swift` basename must equal its visual test
  class. Use the fully qualified discovered selector for an unusual file.
- **Renderer mismatch:** use canonical CI or treat the new exact build as a
  deliberate platform migration. Do not bypass the guard.
- **Unsupported schema:** update the fixture only after production endpoint
  models support it.
- **No image produced:** run in a logged-in macOS graphical session; SwiftUI
  `ImageRenderer` requires the macOS rendering stack.
- **Unexpected live values:** use `--snapshot-dashboard-fixture`; the legacy
  `--snapshot` command intentionally reads daemon data.
- **Wrong output size:** update the production configuration, independent test
  expectation, references, and this guide together.
- **Failure directory is empty:** environment validation or compilation failed
  before comparison. Read the test output first.
