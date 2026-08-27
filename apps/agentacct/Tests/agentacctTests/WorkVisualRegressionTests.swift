import Foundation
import XCTest
@testable import agentacct

final class WorkVisualRegressionTests: XCTestCase {
    // Independent of WorkSnapshotConfiguration: deleting a state, viewport,
    // or appearance from the renderer must fail this reviewed contract.
    private let expectedFilenames = [
        "work-table-minimum-light.png",
        "work-table-minimum-dark.png",
        "work-table-reference-light.png",
        "work-table-reference-dark.png",
        "work-receipt-minimum-light.png",
        "work-receipt-minimum-dark.png",
        "work-receipt-reference-light.png",
        "work-receipt-reference-dark.png",
        "work-receipt-actions-files-detail-light.png",
        "work-receipt-actions-files-detail-dark.png",
        "work-receipt-actions-commands-detail-light.png",
        "work-receipt-actions-commands-detail-dark.png",
        "work-receipt-actions-tools-detail-light.png",
        "work-receipt-actions-tools-detail-dark.png",
        "work-empty-reference-light.png",
        "work-empty-reference-dark.png",
        "work-list-error-reference-light.png",
        "work-list-error-reference-dark.png",
        "work-receipt-loading-reference-light.png",
        "work-receipt-loading-reference-dark.png",
        "work-receipt-error-reference-light.png",
        "work-receipt-error-reference-dark.png",
    ]

    @MainActor
    func testWorkReviewMatrix() throws {
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

        let fixtureURL = try XCTUnwrap(
            Bundle.module.url(forResource: "dashboard", withExtension: "json")
        )
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL)
        let outputDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-work-visuals-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: outputDirectory) }

        let rendered = try WorkSnapshotRenderer.render(
            fixture: fixture,
            outputDirectory: outputDirectory
        )
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
                // Report every missing or changed state in one run.
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
