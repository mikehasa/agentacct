import AppKit
import XCTest
@testable import agentacct

final class WorkSnapshotHarnessTests: XCTestCase {
    private struct ExpectedArtifact {
        let filename: String
        let pixelsWide: Int
        let pixelsHigh: Int
    }

    // Keep this contract independent of the production configuration. A
    // removed Work state, viewport, or appearance must fail review loudly.
    private let expectedArtifacts = [
        ExpectedArtifact(filename: "work-table-minimum-light.png", pixelsWide: 1920, pixelsHigh: 1120),
        ExpectedArtifact(filename: "work-table-minimum-dark.png", pixelsWide: 1920, pixelsHigh: 1120),
        ExpectedArtifact(filename: "work-table-reference-light.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-table-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-receipt-minimum-light.png", pixelsWide: 1920, pixelsHigh: 1120),
        ExpectedArtifact(filename: "work-receipt-minimum-dark.png", pixelsWide: 1920, pixelsHigh: 1120),
        ExpectedArtifact(filename: "work-receipt-reference-light.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-receipt-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-receipt-actions-files-detail-light.png", pixelsWide: 2240, pixelsHigh: 2000),
        ExpectedArtifact(filename: "work-receipt-actions-files-detail-dark.png", pixelsWide: 2240, pixelsHigh: 2000),
        ExpectedArtifact(filename: "work-receipt-actions-commands-detail-light.png", pixelsWide: 2240, pixelsHigh: 2000),
        ExpectedArtifact(filename: "work-receipt-actions-commands-detail-dark.png", pixelsWide: 2240, pixelsHigh: 2000),
        ExpectedArtifact(filename: "work-receipt-actions-tools-detail-light.png", pixelsWide: 2240, pixelsHigh: 2000),
        ExpectedArtifact(filename: "work-receipt-actions-tools-detail-dark.png", pixelsWide: 2240, pixelsHigh: 2000),
        ExpectedArtifact(filename: "work-empty-reference-light.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-empty-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-list-error-reference-light.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-list-error-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-receipt-loading-reference-light.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-receipt-loading-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-receipt-error-reference-light.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-receipt-error-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1600),
    ]

    @MainActor
    func testFixturePreloadsRepresentativeWorkReceipt() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let work = try XCTUnwrap(fixture.work)
        let store = DashboardStore(preloaded: fixture)

        XCTAssertEqual(store.receipt?.taskId, work.receipt.taskId)
        XCTAssertEqual(store.preloadedSessions.count, work.sessions.count)
        XCTAssertTrue(store.receiptTasks.contains { $0.decisionStatus.key == "blocked" })
        XCTAssertTrue(store.receiptTasks.contains { $0.decisionStatus.key == "finding" })
        XCTAssertGreaterThanOrEqual(work.receipt.dimensions.actions.touchedFileCount ?? 0, 12)
        XCTAssertGreaterThanOrEqual(work.receipt.dimensions.actions.commandCount ?? 0, 20)
        XCTAssertEqual(
            work.receipt.dimensions.actions.touchedFiles?.count,
            work.receipt.dimensions.actions.touchedFileCount,
            "The fixture must exercise the complete in-place files disclosure"
        )
        XCTAssertEqual(
            work.receipt.dimensions.actions.commands?.count,
            work.receipt.dimensions.actions.commandCount,
            "The fixture must exercise the complete in-place commands disclosure"
        )
        XCTAssertTrue(
            work.receipt.dimensions.actions.gaps?.isEmpty ?? true,
            "Fully available action details must not be described as missing evidence"
        )
        XCTAssertEqual(work.receipt.dimensions.gaps.count, 2)
        XCTAssertGreaterThanOrEqual(work.receipt.dimensions.evidence.checks?.count ?? 0, 6)
        XCTAssertFalse(work.receipt.dimensions.provenance.sourcesPresent?.contains("ci") ?? false)
        XCTAssertGreaterThanOrEqual(work.receipt.sessions?.count ?? 0, 2)
    }

    @MainActor
    func testRendersEveryWorkReviewConfigurationDeterministically() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let firstDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-work-snapshots-\(UUID().uuidString)")
        let secondDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-work-snapshots-\(UUID().uuidString)")
        defer {
            try? FileManager.default.removeItem(at: firstDirectory)
            try? FileManager.default.removeItem(at: secondDirectory)
        }

        let rendered = try WorkSnapshotRenderer.render(
            fixture: fixture,
            outputDirectory: firstDirectory
        )
        _ = try WorkSnapshotRenderer.render(
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
            expectedURL: firstDirectory.appendingPathComponent("work-receipt-reference-light.png"),
            actualURL: firstDirectory.appendingPathComponent("work-receipt-reference-dark.png")
        )
        XCTAssertGreaterThan(appearanceDifference.maximumChannelDelta, 32)
        XCTAssertGreaterThan(appearanceDifference.changedPixelFraction, 0.05)

        let stateDifference = try VisualSnapshotHarness.compare(
            expectedURL: firstDirectory.appendingPathComponent("work-table-reference-light.png"),
            actualURL: firstDirectory.appendingPathComponent("work-receipt-reference-light.png")
        )
        XCTAssertGreaterThan(stateDifference.maximumChannelDelta, 32)
        XCTAssertGreaterThan(stateDifference.changedPixelFraction, 0.05)

        let transientDifference = try VisualSnapshotHarness.compare(
            expectedURL: firstDirectory.appendingPathComponent("work-receipt-loading-reference-light.png"),
            actualURL: firstDirectory.appendingPathComponent("work-receipt-error-reference-light.png")
        )
        XCTAssertGreaterThan(
            transientDifference.maximumChannelDelta,
            32,
            "Loading and error states must never collapse to the same renderer placeholder"
        )
        XCTAssertGreaterThan(
            transientDifference.changedPixelFraction,
            0.001,
            "Loading and error states must remain visually distinguishable"
        )
        let disclosureDifference = try VisualSnapshotHarness.compare(
            expectedURL: firstDirectory.appendingPathComponent("work-receipt-reference-light.png"),
            actualURL: firstDirectory.appendingPathComponent("work-receipt-actions-files-detail-light.png")
        )
        XCTAssertGreaterThan(
            disclosureDifference.changedPixelFraction,
            0.005,
            "Collapsed and expanded action details must remain visually distinguishable"
        )
        let categoryDifference = try VisualSnapshotHarness.compare(
            expectedURL: firstDirectory.appendingPathComponent("work-receipt-actions-files-detail-light.png"),
            actualURL: firstDirectory.appendingPathComponent("work-receipt-actions-commands-detail-light.png")
        )
        XCTAssertGreaterThan(
            categoryDifference.changedPixelFraction,
            0.005,
            "Each selected action category must render distinct content and state"
        )
        XCTAssertFalse(SnapshotMode.enabled)
        XCTAssertFalse(SnapshotMode.boundsScrollContentToViewport)
        XCTAssertNil(SnapshotScheme.override)
    }

    func testRejectsUnsupportedWorkPayloadSchemas() throws {
        let validFixture = try String(contentsOf: fixtureURL(), encoding: .utf8)
        let invalidReceipt = validFixture.replacingOccurrences(
            of: "\"schema_version\": \"agentacct.receipt.v1\"",
            with: "\"schema_version\": \"agentacct.receipt.v999\""
        )
        XCTAssertThrowsError(try DashboardSnapshotFixture.decode(Data(invalidReceipt.utf8))) { error in
            guard case SnapshotError.unsupportedSchema(let payload, let actual, let expected) = error else {
                return XCTFail("Expected unsupported Work receipt schema; got \(error)")
            }
            XCTAssertEqual(payload, "work receipt")
            XCTAssertEqual(actual, "agentacct.receipt.v999")
            XCTAssertEqual(expected, DashboardSnapshotFixture.supportedTasksSchema)
        }

        let invalidSession = validFixture.replacingOccurrences(
            of: "\"schema\": \"agentacct.v1-session-detail.v1\"",
            with: "\"schema\": \"agentacct.v1-session-detail.v999\""
        )
        XCTAssertThrowsError(try DashboardSnapshotFixture.decode(Data(invalidSession.utf8))) { error in
            guard case SnapshotError.unsupportedSchema(let payload, let actual, let expected) = error else {
                return XCTFail("Expected unsupported Work session schema; got \(error)")
            }
            XCTAssertEqual(payload, "work session")
            XCTAssertEqual(actual, "agentacct.v1-session-detail.v999")
            XCTAssertEqual(expected, WorkSnapshotFixture.supportedSessionSchema)
        }
    }

    @MainActor
    func testRendererRejectsFixtureWithoutWorkPayload() throws {
        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: fixtureURL())) as? [String: Any]
        )
        var withoutWork = object
        withoutWork.removeValue(forKey: "work")
        let fixture = try DashboardSnapshotFixture.decode(
            JSONSerialization.data(withJSONObject: withoutWork)
        )

        XCTAssertThrowsError(
            try WorkSnapshotRenderer.render(
                fixture: fixture,
                outputDirectory: FileManager.default.temporaryDirectory
            )
        ) { error in
            guard case SnapshotError.missingWorkFixture = error else {
                return XCTFail("Expected missing Work fixture error; got \(error)")
            }
        }
    }

    private func fixtureURL() throws -> URL {
        try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
    }
}
