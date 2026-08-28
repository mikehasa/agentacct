import AppKit
import SwiftUI
import XCTest
@testable import agentacct

final class MenuSnapshotHarnessTests: XCTestCase {
    private struct CompressedMenuHost: Layout {
        func sizeThatFits(
            proposal: ProposedViewSize,
            subviews: Subviews,
            cache: inout ()
        ) -> CGSize {
            subviews[0].sizeThatFits(ProposedViewSize(width: 360, height: 44))
        }

        func placeSubviews(
            in bounds: CGRect,
            proposal: ProposedViewSize,
            subviews: Subviews,
            cache: inout ()
        ) {
            subviews[0].place(
                at: bounds.origin,
                anchor: .topLeading,
                proposal: ProposedViewSize(width: 360, height: 44)
            )
        }
    }

    private struct ExpectedArtifact {
        let filename: String
        let pixelsWide: Int
        let pixelsHigh: Int
    }

    private let expectedArtifacts = [
        ExpectedArtifact(filename: "menu-connected-sparse-light.png", pixelsWide: 720, pixelsHigh: 880),
        ExpectedArtifact(filename: "menu-connected-sparse-dark.png", pixelsWide: 720, pixelsHigh: 880),
        ExpectedArtifact(filename: "menu-connected-dense-light.png", pixelsWide: 720, pixelsHigh: 928),
        ExpectedArtifact(filename: "menu-connected-dense-dark.png", pixelsWide: 720, pixelsHigh: 928),
    ]

    @MainActor
    func testRendersStableConnectedMenuInBothAppearances() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let firstDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-menu-snapshots-\(UUID().uuidString)")
        let secondDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-menu-snapshots-\(UUID().uuidString)")
        defer {
            try? FileManager.default.removeItem(at: firstDirectory)
            try? FileManager.default.removeItem(at: secondDirectory)
        }

        let rendered = try MenuSnapshotRenderer.render(
            fixture: fixture,
            outputDirectory: firstDirectory
        )
        _ = try MenuSnapshotRenderer.render(
            fixture: fixture,
            outputDirectory: secondDirectory
        )

        XCTAssertEqual(rendered.map(\.lastPathComponent), expectedArtifacts.map(\.filename))
        for artifact in expectedArtifacts {
            let imageURL = firstDirectory.appendingPathComponent(artifact.filename)
            let image = try XCTUnwrap(NSImage(contentsOf: imageURL), artifact.filename)
            let representation = try XCTUnwrap(image.representations.first, artifact.filename)
            XCTAssertEqual(representation.pixelsWide, artifact.pixelsWide)
            XCTAssertEqual(representation.pixelsHigh, artifact.pixelsHigh)

            let difference = try VisualSnapshotHarness.compare(
                expectedURL: imageURL,
                actualURL: secondDirectory.appendingPathComponent(artifact.filename)
            )
            XCTAssertLessThanOrEqual(
                difference.maximumChannelDelta,
                VisualSnapshotTolerance.menuRenderingNoise.maximumChannelDelta,
                artifact.filename
            )
            XCTAssertLessThanOrEqual(
                difference.changedChannelFraction,
                VisualSnapshotTolerance.menuRenderingNoise.maximumChangedChannelFraction,
                artifact.filename
            )
        }

        let appearanceDifference = try VisualSnapshotHarness.compare(
            expectedURL: firstDirectory.appendingPathComponent("menu-connected-sparse-light.png"),
            actualURL: firstDirectory.appendingPathComponent("menu-connected-sparse-dark.png")
        )
        XCTAssertGreaterThan(appearanceDifference.maximumChannelDelta, 32)
        XCTAssertGreaterThan(appearanceDifference.changedPixelFraction, 0.05)
        XCTAssertFalse(SnapshotMode.enabled)
        XCTAssertNil(SnapshotScheme.override)
    }

    @MainActor
    func testLiveConnectedMenuProvidesAUsableIntrinsicHeight() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let glance = GlanceState(preloaded: GlanceSnapshot(
            glance: fixture.glance,
            daemonVersion: fixture.daemonVersion
        ))
        let dashboard = DashboardStore(preloaded: fixture)
        let selection = AppSelection()

        SnapshotMode.enabled = false
        let view = MenuContent(
            buildIdentity: MenuSnapshotRenderer.reviewBuildIdentity,
            lastUpdatedTextOverride: "just now",
            launchAtLoginInitialState: false
        )
        .environment(glance)
        .environment(dashboard)
        .environment(selection)

        let hostingView = NSHostingView(rootView: CompressedMenuHost { view })
        let fittingSize = hostingView.fittingSize

        XCTAssertEqual(fittingSize.width, 360, accuracy: 0.5)
        XCTAssertGreaterThanOrEqual(
            fittingSize.height,
            440,
            "the live MenuBarExtra must reserve room for its content above the persistent footer"
        )
        XCTAssertLessThanOrEqual(
            fittingSize.height,
            465,
            "the connected menu must remain capped to a glanceable popover height"
        )
    }

    func testFixtureExercisesPopulatedUsageWindows() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let days = Set(fixture.glance.usage.windows.compactMap(\.days))

        XCTAssertTrue(days.contains(1), "menu review must exercise today's usage")
        XCTAssertTrue(days.contains(7), "menu review must exercise seven-day usage")
        XCTAssertTrue(days.contains(30), "menu review must exercise 30-day usage")

        let sparseDays = Set(try XCTUnwrap(fixture.menuSparseGlance).usage.windows.compactMap(\.days))
        XCTAssertEqual(sparseDays, Set([1, 7, 30]), "sparse menu review must populate every usage row")
        XCTAssertGreaterThan(
            try XCTUnwrap(fixture.menuSparseGlance).recentSessions.count,
            2,
            "sparse menu review must expose the hidden-session destination"
        )
    }

    private func fixtureURL() throws -> URL {
        try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
    }
}
