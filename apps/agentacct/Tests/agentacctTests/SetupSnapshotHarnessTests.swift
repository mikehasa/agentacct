import AppKit
import XCTest
@testable import agentacct

final class SetupSnapshotHarnessTests: XCTestCase {
    private let expectedFilenames = [
        "setup-failure-light.png",
        "setup-failure-dark.png",
    ]

    @MainActor
    func testRendersFailureStateDeterministicallyInBothAppearances() throws {
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

        XCTAssertEqual(rendered.map(\.lastPathComponent), expectedFilenames)
        for filename in expectedFilenames {
            let firstURL = firstDirectory.appendingPathComponent(filename)
            let image = try XCTUnwrap(NSImage(contentsOf: firstURL), filename)
            let representation = try XCTUnwrap(image.representations.first, filename)
            XCTAssertEqual(representation.pixelsWide, 920)
            XCTAssertEqual(representation.pixelsHigh, 1_020)

            let difference = try VisualSnapshotHarness.compare(
                expectedURL: firstURL,
                actualURL: secondDirectory.appendingPathComponent(filename)
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
            expectedURL: firstDirectory.appendingPathComponent("setup-failure-light.png"),
            actualURL: firstDirectory.appendingPathComponent("setup-failure-dark.png")
        )
        XCTAssertGreaterThan(appearanceDifference.maximumChannelDelta, 32)
        XCTAssertGreaterThan(appearanceDifference.changedPixelFraction, 0.05)
        XCTAssertNil(SnapshotScheme.override)
    }
}
