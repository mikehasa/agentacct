import Foundation
import XCTest
@testable import agentacct

final class AboutVisualRegressionTests: XCTestCase {
    private let expectedFilenames = [
        "about-panel-light.png",
        "about-panel-dark.png",
    ]

    func testNativePanelAppearances() throws {
        let environment = ProcessInfo.processInfo.environment
        guard environment["AGENTACCT_VERIFY_VISUAL_BASELINES"] == "1" else {
            throw XCTSkip("Run visual baselines through ./Scripts/visual-snapshots verify")
        }

        let platformID = try XCTUnwrap(
            environment["AGENTACCT_SNAPSHOT_PLATFORM_ID"],
            "The visual snapshot CLI must supply AGENTACCT_SNAPSHOT_PLATFORM_ID"
        )
        guard Self.isSafePathComponent(platformID) else {
            XCTFail("Invalid visual snapshot platform identifier: \(platformID)")
            return
        }

        let outputDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-about-visuals-\(UUID().uuidString)")
        defer {
            if FileManager.default.fileExists(atPath: outputDirectory.path) {
                try? FileManager.default.removeItem(at: outputDirectory)
            }
        }

        let rendered = try AboutSnapshotProcess.render(outputDirectory: outputDirectory)
        XCTAssertEqual(rendered.map(\.lastPathComponent), expectedFilenames)

        let mode = try VisualSnapshotMode.resolve(environment: environment)
        let referenceDirectory = Self.referenceRoot
            .appendingPathComponent(platformID, isDirectory: true)
        let artifactDirectory = environment["AGENTACCT_SNAPSHOT_ARTIFACT_DIR"]
            .map { URL(fileURLWithPath: $0, isDirectory: true) }
            ?? Self.defaultArtifactDirectory

        for actualURL in rendered {
            let filename = actualURL.lastPathComponent
            let name = actualURL.deletingPathExtension().lastPathComponent
            do {
                _ = try VisualSnapshotHarness.assertSnapshot(
                    name: name,
                    referenceURL: referenceDirectory.appendingPathComponent(filename),
                    actualURL: actualURL,
                    artifactDirectory: artifactDirectory,
                    mode: mode
                )
            } catch {
                XCTFail(error.localizedDescription)
            }
        }
    }

    private static let referenceRoot = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .appendingPathComponent("ReferenceImages", isDirectory: true)

    private static let defaultArtifactDirectory = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .appendingPathComponent(".build/visual-snapshot-failures", isDirectory: true)

    private static func isSafePathComponent(_ value: String) -> Bool {
        !value.isEmpty
            && value != "."
            && value != ".."
            && URL(fileURLWithPath: value).lastPathComponent == value
    }
}
