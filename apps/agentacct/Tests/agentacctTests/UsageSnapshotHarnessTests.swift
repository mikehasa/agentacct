import AppKit
import XCTest
@testable import agentacct

final class UsageSnapshotHarnessTests: XCTestCase {
    private struct ExpectedArtifact {
        let filename: String
        let pixelsWide: Int
        let pixelsHigh: Int
    }

    private let expectedArtifacts = [
        ExpectedArtifact(filename: "usage-minimum-light.png", pixelsWide: 1920, pixelsHigh: 1120),
        ExpectedArtifact(filename: "usage-minimum-dark.png", pixelsWide: 1920, pixelsHigh: 1120),
        ExpectedArtifact(filename: "usage-reference-light.png", pixelsWide: 2240, pixelsHigh: 1800),
        ExpectedArtifact(filename: "usage-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1800),
        ExpectedArtifact(filename: "usage-disconnected-reference-light.png", pixelsWide: 2240, pixelsHigh: 1800),
        ExpectedArtifact(filename: "usage-disconnected-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1800),
    ]

    @MainActor
    func testFixtureProducesJoinedCapacityRows() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let result = UsageCapacitySnapshot.build(
            usage: fixture.usage.byClient,
            limits: fixture.glance.limits,
            plans: fixture.plan.clients,
            showStale: false
        )

        XCTAssertEqual(result.rows.map(\.client), ["claude-code", "codex", "hermes"])
        XCTAssertEqual(result.rows.first?.readings.flatMap { $0.entry.windows ?? [] }.count, 2)
        XCTAssertTrue(try XCTUnwrap(result.rows.last).readings.isEmpty)
    }

    @MainActor
    func testRendersEveryUsageReviewConfigurationDeterministically() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let firstDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-usage-snapshots-\(UUID().uuidString)")
        let secondDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-usage-snapshots-\(UUID().uuidString)")
        defer {
            try? FileManager.default.removeItem(at: firstDirectory)
            try? FileManager.default.removeItem(at: secondDirectory)
        }

        let rendered = try UsageSnapshotRenderer.render(
            fixture: fixture, outputDirectory: firstDirectory
        )
        _ = try UsageSnapshotRenderer.render(fixture: fixture, outputDirectory: secondDirectory)

        XCTAssertEqual(rendered.map(\.lastPathComponent), expectedArtifacts.map(\.filename))
        for artifact in expectedArtifacts {
            let firstURL = firstDirectory.appendingPathComponent(artifact.filename)
            let image = try XCTUnwrap(NSImage(contentsOf: firstURL), artifact.filename)
            let representation = try XCTUnwrap(image.representations.first, artifact.filename)
            XCTAssertEqual(representation.pixelsWide, artifact.pixelsWide)
            XCTAssertEqual(representation.pixelsHigh, artifact.pixelsHigh)
            let difference = try VisualSnapshotHarness.compare(
                expectedURL: firstURL,
                actualURL: secondDirectory.appendingPathComponent(artifact.filename)
            )
            XCTAssertLessThanOrEqual(
                difference.maximumChannelDelta,
                VisualSnapshotTolerance.renderingNoise.maximumChannelDelta
            )
            XCTAssertLessThanOrEqual(
                difference.changedChannelFraction,
                VisualSnapshotTolerance.renderingNoise.maximumChangedChannelFraction
            )
        }
        XCTAssertFalse(SnapshotMode.enabled)
        XCTAssertFalse(SnapshotMode.boundsScrollContentToViewport)
        XCTAssertNil(SnapshotScheme.override)
    }

    private func fixtureURL() throws -> URL {
        try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
    }
}
