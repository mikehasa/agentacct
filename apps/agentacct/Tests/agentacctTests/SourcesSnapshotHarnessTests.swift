import AppKit
import XCTest
@testable import agentacct

final class SourcesSnapshotHarnessTests: XCTestCase {
    private let configurations = SourcesSnapshotConfiguration.reviewConfigurations

    @MainActor
    func testRendersSourceStatesDeterministicallyInBothAppearances() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let firstDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-sources-snapshots-\(UUID().uuidString)")
        let secondDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-sources-snapshots-\(UUID().uuidString)")
        defer {
            try? FileManager.default.removeItem(at: firstDirectory)
            try? FileManager.default.removeItem(at: secondDirectory)
        }

        let rendered = try SourcesSnapshotRenderer.render(
            fixture: fixture,
            outputDirectory: firstDirectory
        )
        _ = try SourcesSnapshotRenderer.render(
            fixture: fixture,
            outputDirectory: secondDirectory
        )

        XCTAssertEqual(rendered.map(\.lastPathComponent), configurations.map(\.filename))
        for configuration in configurations {
            let firstURL = firstDirectory.appendingPathComponent(configuration.filename)
            let image = try XCTUnwrap(NSImage(contentsOf: firstURL), configuration.filename)
            let representation = try XCTUnwrap(image.representations.first, configuration.filename)
            XCTAssertEqual(representation.pixelsWide, Int(configuration.width * 2))
            XCTAssertEqual(representation.pixelsHigh, Int(configuration.height * 2))

            let difference = try VisualSnapshotHarness.compare(
                expectedURL: firstURL,
                actualURL: secondDirectory.appendingPathComponent(configuration.filename)
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

        let appearanceDifference = try VisualSnapshotHarness.compare(
            expectedURL: firstDirectory.appendingPathComponent("sources-healthy-light.png"),
            actualURL: firstDirectory.appendingPathComponent("sources-healthy-dark.png")
        )
        XCTAssertGreaterThan(appearanceDifference.maximumChannelDelta, 32)
        XCTAssertGreaterThan(appearanceDifference.changedPixelFraction, 0.05)
        XCTAssertFalse(SnapshotMode.enabled)
        XCTAssertFalse(SnapshotMode.boundsScrollContentToViewport)
        XCTAssertNil(SnapshotScheme.override)
    }

    private func fixtureURL() throws -> URL {
        try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
    }
}
