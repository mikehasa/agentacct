import Foundation
import XCTest
@testable import agentacct

final class UsageVisualRegressionTests: XCTestCase {
    private let expectedFilenames = [
        "usage-minimum-light.png",
        "usage-minimum-dark.png",
        "usage-reference-light.png",
        "usage-reference-dark.png",
        "usage-weekly-reference-light.png",
        "usage-weekly-reference-dark.png",
        "usage-disconnected-reference-light.png",
        "usage-disconnected-reference-dark.png",
    ]

    @MainActor
    func testUsageReviewMatrix() throws {
        let environment = ProcessInfo.processInfo.environment
        guard environment["AGENTACCT_VERIFY_VISUAL_BASELINES"] == "1" else {
            throw XCTSkip("Run visual baselines through ./Scripts/visual-snapshots verify")
        }
        let platformID = try XCTUnwrap(
            environment["AGENTACCT_SNAPSHOT_PLATFORM_ID"],
            "The visual snapshot CLI must supply AGENTACCT_SNAPSHOT_PLATFORM_ID"
        )
        guard Self.isSafePathComponent(platformID) else {
            return XCTFail("Invalid visual snapshot platform identifier: \(platformID)")
        }
        let fixtureURL = try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL)
        let outputDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-usage-visuals-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: outputDirectory) }

        let rendered = try UsageSnapshotRenderer.render(
            fixture: fixture, outputDirectory: outputDirectory
        )
        XCTAssertEqual(rendered.map(\.lastPathComponent), expectedFilenames)

        let mode = try VisualSnapshotMode.resolve(environment: environment)
        let referenceDirectory = Self.referenceRoot.appendingPathComponent(platformID, isDirectory: true)
        let artifactDirectory = environment["AGENTACCT_SNAPSHOT_ARTIFACT_DIR"]
            .map { URL(fileURLWithPath: $0, isDirectory: true) }
            ?? Self.defaultArtifactDirectory
        for actualURL in rendered {
            let filename = actualURL.lastPathComponent
            do {
                _ = try VisualSnapshotHarness.assertSnapshot(
                    name: actualURL.deletingPathExtension().lastPathComponent,
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
