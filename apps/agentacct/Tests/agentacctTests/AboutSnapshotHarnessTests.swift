import AppKit
import XCTest
@testable import agentacct

final class AboutSnapshotHarnessTests: XCTestCase {
    private let expectedFilenames = [
        "about-panel-light.png",
        "about-panel-dark.png",
    ]

    func testRendersStableNativePanelInBothAppearances() throws {
        let firstDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-about-snapshots-\(UUID().uuidString)")
        let secondDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-about-snapshots-\(UUID().uuidString)")
        defer {
            if FileManager.default.fileExists(atPath: firstDirectory.path) {
                try? FileManager.default.removeItem(at: firstDirectory)
            }
            if FileManager.default.fileExists(atPath: secondDirectory.path) {
                try? FileManager.default.removeItem(at: secondDirectory)
            }
        }

        let rendered = try AboutSnapshotProcess.render(outputDirectory: firstDirectory)
        _ = try AboutSnapshotProcess.render(outputDirectory: secondDirectory)

        XCTAssertEqual(rendered.map(\.lastPathComponent), expectedFilenames)
        for filename in expectedFilenames {
            let imageURL = firstDirectory.appendingPathComponent(filename)
            let image = try XCTUnwrap(NSImage(contentsOf: imageURL), filename)
            let representation = try XCTUnwrap(image.representations.first, filename)
            XCTAssertGreaterThan(representation.pixelsWide, 0)
            XCTAssertGreaterThan(representation.pixelsHigh, 0)

            let difference = try VisualSnapshotHarness.compare(
                expectedURL: imageURL,
                actualURL: secondDirectory.appendingPathComponent(filename)
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
            expectedURL: firstDirectory.appendingPathComponent("about-panel-light.png"),
            actualURL: firstDirectory.appendingPathComponent("about-panel-dark.png")
        )
        XCTAssertGreaterThan(appearanceDifference.maximumChannelDelta, 32)
        XCTAssertGreaterThan(appearanceDifference.changedPixelFraction, 0.05)
    }
}
