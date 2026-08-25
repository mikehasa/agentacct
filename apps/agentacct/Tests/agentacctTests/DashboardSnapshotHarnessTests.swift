import AppKit
import XCTest
@testable import agentacct

final class DashboardSnapshotHarnessTests: XCTestCase {
    private struct ExpectedArtifact {
        let filename: String
        let pixelsWide: Int
        let pixelsHigh: Int
    }

    // Keep this expectation independent of the production configuration. If a
    // viewport or appearance is removed accidentally, this test must fail.
    private let expectedArtifacts = [
        ExpectedArtifact(filename: "dashboard-minimum-light.png", pixelsWide: 1920, pixelsHigh: 1120),
        ExpectedArtifact(filename: "dashboard-minimum-dark.png", pixelsWide: 1920, pixelsHigh: 1120),
        ExpectedArtifact(filename: "dashboard-reference-light.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "dashboard-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1600),
    ]

    @MainActor
    func testFixturePreloadsEveryDashboardDataLane() throws {
        let fixture = try DashboardSnapshotFixture.load(from: dashboardFixtureURL())
        let store = DashboardStore(preloaded: fixture)

        XCTAssertEqual(store.receiptTasks.count, 4)
        XCTAssertEqual(store.totalReceiptTasks, 4)
        XCTAssertEqual(store.planClients.count, 1)
        XCTAssertEqual(store.usage?.byPeriod?.count, 7)
        XCTAssertEqual(fixture.glance.limits.count, 1)
        XCTAssertEqual(fixture.glance.recentSessions.count, 2)
        XCTAssertNotNil(fixture.glance.usage.windows.first { $0.label == "today" })
    }

    @MainActor
    func testRendersEveryDashboardReviewConfiguration() throws {
        let fixture = try DashboardSnapshotFixture.load(from: dashboardFixtureURL())
        let outputDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-dashboard-snapshots-\(UUID().uuidString)")
        let secondOutputDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-dashboard-snapshots-\(UUID().uuidString)")
        defer {
            try? FileManager.default.removeItem(at: outputDirectory)
            try? FileManager.default.removeItem(at: secondOutputDirectory)
        }

        let rendered = try DashboardSnapshotRenderer.render(
            fixture: fixture,
            outputDirectory: outputDirectory
        )
        _ = try DashboardSnapshotRenderer.render(
            fixture: fixture,
            outputDirectory: secondOutputDirectory
        )

        XCTAssertEqual(
            rendered.map(\.lastPathComponent),
            expectedArtifacts.map(\.filename)
        )
        for artifact in expectedArtifacts {
            let imageURL = outputDirectory.appendingPathComponent(artifact.filename)
            let image = try XCTUnwrap(NSImage(contentsOf: imageURL), artifact.filename)
            let representation = try XCTUnwrap(image.representations.first, artifact.filename)
            XCTAssertEqual(representation.pixelsWide, artifact.pixelsWide)
            XCTAssertEqual(representation.pixelsHigh, artifact.pixelsHigh)
            let difference = try VisualSnapshotHarness.compare(
                expectedURL: imageURL,
                actualURL: secondOutputDirectory.appendingPathComponent(artifact.filename)
            )
            // Core Graphics can round antialiased edges one 8-bit color step
            // differently between renders. Larger or more widespread changes
            // indicate dynamic data, animation, or an unstable layout.
            XCTAssertLessThanOrEqual(
                difference.maximumChannelDelta,
                VisualSnapshotTolerance.renderingNoise.maximumChannelDelta,
                "Repeated fixture renders exceeded the one-step antialiasing budget"
            )
            XCTAssertLessThanOrEqual(
                difference.changedChannelFraction,
                VisualSnapshotTolerance.renderingNoise.maximumChangedChannelFraction,
                "Repeated fixture renders exceeded the normalized-channel stability budget"
            )
        }

        let appearanceDifference = try VisualSnapshotHarness.compare(
            expectedURL: outputDirectory.appendingPathComponent("dashboard-reference-light.png"),
            actualURL: outputDirectory.appendingPathComponent("dashboard-reference-dark.png")
        )
        XCTAssertGreaterThan(
            appearanceDifference.maximumChannelDelta,
            32,
            "Light and dark review artifacts must have visibly different colors"
        )
        XCTAssertGreaterThan(
            appearanceDifference.changedPixelFraction,
            0.05,
            "Light and dark review artifacts must differ across a meaningful part of the dashboard"
        )
        XCTAssertFalse(SnapshotMode.enabled)
        XCTAssertFalse(SnapshotMode.boundsScrollContentToViewport)
        XCTAssertNil(SnapshotScheme.override)
    }

    func testRejectsUnsupportedVersionedFixtureSchemas() throws {
        let fixtureURL = try dashboardFixtureURL()
        let validFixture = try String(contentsOf: fixtureURL, encoding: .utf8)
        let schemas = [
            (payload: "glance", supported: GlanceClient.supportedGlanceSchema),
            (payload: "plan", supported: DashboardSnapshotFixture.supportedPlanSchema),
            (payload: "tasks", supported: DashboardSnapshotFixture.supportedTasksSchema),
        ]

        for schema in schemas {
            let unsupported = "agentacct.\(schema.payload).v999"
            let invalidFixture = validFixture.replacingOccurrences(
                of: "\"schema\": \"\(schema.supported)\"",
                with: "\"schema\": \"\(unsupported)\""
            )
            XCTAssertNotEqual(invalidFixture, validFixture, "The test must mutate the \(schema.payload) schema")
            XCTAssertThrowsError(try DashboardSnapshotFixture.decode(Data(invalidFixture.utf8))) { error in
                guard case SnapshotError.unsupportedSchema(
                    let payload,
                    let actual,
                    let expected
                ) = error else {
                    return XCTFail("Expected an unsupported-schema error; got \(error)")
                }
                XCTAssertEqual(payload, schema.payload)
                XCTAssertEqual(actual, unsupported)
                XCTAssertEqual(expected, schema.supported)
            }
        }
    }

    @MainActor
    func testRejectsFixtureWithoutSnapshotClock() throws {
        let fixtureURL = try dashboardFixtureURL()
        var fixtureJSON = try String(contentsOf: fixtureURL, encoding: .utf8)
        let generatedAtLine = "    \"generated_at\": 1787590000,\n"
        let generatedAtRange = try XCTUnwrap(fixtureJSON.range(of: generatedAtLine))
        fixtureJSON.removeSubrange(generatedAtRange)
        let fixture = try DashboardSnapshotFixture.decode(Data(fixtureJSON.utf8))

        XCTAssertThrowsError(
            try DashboardSnapshotRenderer.render(
                fixture: fixture,
                outputDirectory: FileManager.default.temporaryDirectory
            )
        ) { error in
            guard case SnapshotError.missingFixtureDate = error else {
                return XCTFail("Expected a missing-fixture-date error; got \(error)")
            }
        }
    }

    @MainActor
    func testSnapshotClockPinsRelativeLabelsToFixtureTime() {
        SnapshotMode.setFixtureDate(Date(timeIntervalSince1970: 1_000_000))
        defer { SnapshotMode.setFixtureDate(nil) }

        XCTAssertEqual(Theme.resetsIn(1_000_000 + 6 * 86_400 + 13 * 3_600), "6d 13h")
        XCTAssertEqual(agoText(1_000_000 - 3_600), "1h ago")
    }

    private func dashboardFixtureURL() throws -> URL {
        try XCTUnwrap(
            Bundle.module.url(forResource: "dashboard", withExtension: "json")
        )
    }
}
