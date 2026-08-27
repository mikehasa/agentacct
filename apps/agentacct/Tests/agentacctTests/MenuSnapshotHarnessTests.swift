import AppKit
import XCTest
@testable import agentacct

final class MenuSnapshotHarnessTests: XCTestCase {
    private struct ExpectedArtifact {
        let filename: String
        let pixelsWide: Int
        let pixelsHigh: Int
    }

    private let expectedArtifacts = [
        ExpectedArtifact(filename: "menu-connected-light.png", pixelsWide: 720, pixelsHigh: 966),
        ExpectedArtifact(filename: "menu-connected-dark.png", pixelsWide: 720, pixelsHigh: 966),
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
                VisualSnapshotTolerance.renderingNoise.maximumChannelDelta
            )
            XCTAssertLessThanOrEqual(
                difference.changedChannelFraction,
                VisualSnapshotTolerance.renderingNoise.maximumChangedChannelFraction
            )
        }

        let appearanceDifference = try VisualSnapshotHarness.compare(
            expectedURL: firstDirectory.appendingPathComponent("menu-connected-light.png"),
            actualURL: firstDirectory.appendingPathComponent("menu-connected-dark.png")
        )
        XCTAssertGreaterThan(appearanceDifference.maximumChannelDelta, 32)
        XCTAssertGreaterThan(appearanceDifference.changedPixelFraction, 0.05)
        XCTAssertFalse(SnapshotMode.enabled)
        XCTAssertNil(SnapshotScheme.override)
    }

    private func fixtureURL() throws -> URL {
        try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
    }
}
