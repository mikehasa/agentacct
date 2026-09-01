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
        ExpectedArtifact(filename: "dashboard-weekly-reference-light.png", pixelsWide: 2240, pixelsHigh: 1800),
        ExpectedArtifact(filename: "dashboard-weekly-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1800),
        ExpectedArtifact(filename: "dashboard-trust-unavailable-light.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "dashboard-trust-unavailable-dark.png", pixelsWide: 2240, pixelsHigh: 1600),
    ]

    @MainActor
    func testFixturePreloadsEveryDashboardDataLane() throws {
        let fixture = try DashboardSnapshotFixture.load(from: dashboardFixtureURL())
        let store = DashboardStore(preloaded: fixture)

        XCTAssertEqual(store.usageLastUpdated?.timeIntervalSince1970, fixture.glance.generatedAt)

        XCTAssertEqual(store.receiptTasks.count, 4)
        XCTAssertEqual(store.totalReceiptTasks, 4)
        XCTAssertEqual(store.attention?.total, 2)
        XCTAssertEqual(store.attention?.counts.failedCheck, 1)
        XCTAssertEqual(store.attention?.counts.blocker, 1)
        XCTAssertEqual(store.ingestion?.state, "healthy")
        // Two plan lanes + two limit clients: the fixture demos the
        // multi-agent Plan and usage card (codex metered + claude-code
        // metered/calibrating), plus a usage-only hermes row.
        XCTAssertEqual(store.planClients.count, 2)
        XCTAssertEqual(store.usage?.byPeriod?.count, 7)
        XCTAssertEqual(fixture.glance.limits.count, 2)
        XCTAssertEqual(fixture.glance.usage.byClient?.count, 3)
        XCTAssertEqual(fixture.glance.recentSessions.count, 2)
        XCTAssertNotNil(fixture.glance.usage.windows.first { $0.label == "today" })
    }

    @MainActor
    func testUnavailableTrustStateRetainsSuccessOnlyToProveErrorPrecedence() throws {
        let fixture = try DashboardSnapshotFixture.load(from: dashboardFixtureURL())
        let store = DashboardStore(
            preloaded: fixture,
            workState: .shiftBriefUnavailable
        )

        XCTAssertNotNil(store.attention)
        XCTAssertEqual(store.ingestion?.state, "healthy")
        XCTAssertNotNil(store.attentionError)
        XCTAssertNotNil(store.ingestionFailure)
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

    @MainActor
    func testMinimumViewportKeepsRecentWorkInTheInitialReadingPath() throws {
        let fixture = try DashboardSnapshotFixture.load(from: dashboardFixtureURL())
        let configurations = DashboardSnapshotConfiguration.reviewConfigurations.filter {
            $0.viewport == "minimum"
        }
        XCTAssertEqual(configurations.count, 2, "Both minimum-window appearances must be covered")
        let outputDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-dashboard-layout-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: outputDirectory) }

        let rendered = try DashboardSnapshotRenderer.render(
            fixture: fixture,
            outputDirectory: outputDirectory,
            configurations: configurations
        )
        for imageURL in rendered {
            let image = try VisualSnapshotImage(contentsOf: imageURL)
            let windowBackground = rgba(in: image, x: 10, topY: 200)

            // At 2x, the 16pt section spacing should leave a visible background
            // separator before y=500pt. Sampling both columns prevents a shorter
            // attention-card background from hiding an over-tall signal rail.
            let separatorRows = (850..<1_000).filter { topY in
                pixelsMatch(rgba(in: image, x: 100, topY: topY), windowBackground)
                    && pixelsMatch(rgba(in: image, x: 1_280, topY: topY), windowBackground)
            }
            XCTAssertGreaterThanOrEqual(
                longestConsecutiveRun(separatorRows),
                24,
                "\(imageURL.lastPathComponent) must expose Recent work after the decision row; supporting signals must not consume the entire first viewport"
            )
        }
    }

    @MainActor
    func testExtremeTokenBucketsRenderWithoutTrapping() throws {
        let fixtureURL = try dashboardFixtureURL()
        let original = try String(contentsOf: fixtureURL, encoding: .utf8)
        let extreme = original.replacingOccurrences(
            of: #""fresh_tokens": 22500000"#,
            with: #""fresh_tokens": 9223372036854775807"#
        )
        XCTAssertNotEqual(extreme, original, "The test must replace a real token bucket")

        let fixture = try DashboardSnapshotFixture.decode(Data(extreme.utf8))
        let configuration = try XCTUnwrap(
            DashboardSnapshotConfiguration.reviewConfigurations.first {
                $0.filename == "dashboard-minimum-light.png"
            }
        )
        let outputDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-dashboard-extreme-tokens-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: outputDirectory) }

        let rendered = try DashboardSnapshotRenderer.render(
            fixture: fixture,
            outputDirectory: outputDirectory,
            configurations: [configuration]
        )
        XCTAssertEqual(rendered.count, 1)
        XCTAssertTrue(FileManager.default.fileExists(atPath: try XCTUnwrap(rendered.first).path))
    }

    func testRejectsUnsupportedVersionedFixtureSchemas() throws {
        let fixtureURL = try dashboardFixtureURL()
        let validFixture = try String(contentsOf: fixtureURL, encoding: .utf8)
        let schemas = [
            (payload: "glance", supported: GlanceClient.supportedGlanceSchema),
            (payload: "plan", supported: DashboardSnapshotFixture.supportedPlanSchema),
            (payload: "attention", supported: DashboardSnapshotFixture.supportedAttentionSchema),
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
        let generatedAtRange = try XCTUnwrap(
            fixtureJSON.range(
                of: #"    "generated_at": [0-9]+,\n"#,
                options: .regularExpression
            )
        )
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

        XCTAssertEqual(Theme.resetText(1_000_000 + 6 * 86_400 + 13 * 3_600), "Resets in 6 days 13 hr")
        XCTAssertEqual(agoText(1_000_000 - 3_600), "1 hr ago")
    }

    private func dashboardFixtureURL() throws -> URL {
        try XCTUnwrap(
            Bundle.module.url(forResource: "dashboard", withExtension: "json")
        )
    }

    private func rgba(
        in image: VisualSnapshotImage,
        x: Int,
        topY: Int
    ) -> (UInt8, UInt8, UInt8, UInt8) {
        precondition((0..<image.width).contains(x))
        precondition((0..<image.height).contains(topY))
        return image.rgba.withUnsafeBytes { storage in
            let pixels = storage.bindMemory(to: UInt8.self)
            let offset = (topY * image.width + x) * 4
            return (
                pixels[offset],
                pixels[offset + 1],
                pixels[offset + 2],
                pixels[offset + 3]
            )
        }
    }

    private func pixelsMatch(
        _ left: (UInt8, UInt8, UInt8, UInt8),
        _ right: (UInt8, UInt8, UInt8, UInt8),
        tolerance: Int = 1
    ) -> Bool {
        zip([left.0, left.1, left.2, left.3], [right.0, right.1, right.2, right.3])
            .allSatisfy { abs(Int($0.0) - Int($0.1)) <= tolerance }
    }

    private func longestConsecutiveRun(_ values: [Int]) -> Int {
        guard var previous = values.first else { return 0 }
        var longest = 1
        var current = 1
        for value in values.dropFirst() {
            if value == previous + 1 {
                current += 1
                longest = max(longest, current)
            } else {
                current = 1
            }
            previous = value
        }
        return longest
    }
}
