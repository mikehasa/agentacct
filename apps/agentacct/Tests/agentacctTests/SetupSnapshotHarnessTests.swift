import AppKit
import XCTest
@testable import agentacct

final class SetupSnapshotHarnessTests: XCTestCase {
    private let expectedArtifacts: [(filename: String, pixelsHigh: Int)] = [
        ("setup-idle-light.png", 648),
        ("setup-idle-dark.png", 648),
        ("setup-done-light.png", 980),
        ("setup-done-dark.png", 980),
        ("setup-failure-light.png", 998),
        ("setup-failure-dark.png", 998),
    ]

    @MainActor
    func testRendersSetupStatesDeterministicallyInBothAppearances() throws {
        let firstDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-setup-snapshots-\(UUID().uuidString)")
        let secondDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-setup-snapshots-\(UUID().uuidString)")
        defer {
            try? FileManager.default.removeItem(at: firstDirectory)
            try? FileManager.default.removeItem(at: secondDirectory)
        }

        let rendered = try SetupSnapshotRenderer.render(outputDirectory: firstDirectory)
        _ = try SetupSnapshotRenderer.render(outputDirectory: secondDirectory)

        XCTAssertEqual(rendered.map(\.lastPathComponent), expectedArtifacts.map(\.filename))
        for artifact in expectedArtifacts {
            let firstURL = firstDirectory.appendingPathComponent(artifact.filename)
            let image = try XCTUnwrap(NSImage(contentsOf: firstURL), artifact.filename)
            let representation = try XCTUnwrap(image.representations.first, artifact.filename)
            XCTAssertEqual(representation.pixelsWide, 920)
            XCTAssertEqual(representation.pixelsHigh, artifact.pixelsHigh)

            let difference = try VisualSnapshotHarness.compare(
                expectedURL: firstURL,
                actualURL: secondDirectory.appendingPathComponent(artifact.filename)
            )
            XCTAssertLessThanOrEqual(
                difference.maximumChannelDelta,
                VisualSnapshotTolerance.setupRenderingNoise.maximumChannelDelta
            )
            XCTAssertLessThanOrEqual(
                difference.changedChannelFraction,
                VisualSnapshotTolerance.setupRenderingNoise.maximumChangedChannelFraction
            )
        }

        let appearanceDifference = try VisualSnapshotHarness.compare(
            expectedURL: firstDirectory.appendingPathComponent("setup-idle-light.png"),
            actualURL: firstDirectory.appendingPathComponent("setup-idle-dark.png")
        )
        XCTAssertGreaterThan(appearanceDifference.maximumChannelDelta, 32)
        XCTAssertGreaterThan(appearanceDifference.changedPixelFraction, 0.05)
        XCTAssertNil(SnapshotScheme.override)
    }
}
