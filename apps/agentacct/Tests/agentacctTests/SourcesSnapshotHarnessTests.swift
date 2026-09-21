import AppKit
import XCTest
@testable import agentacct

final class SourcesSnapshotHarnessTests: XCTestCase {
    private struct ExpectedArtifact {
        let filename: String
        let pixelsWide: Int
        let pixelsHigh: Int
    }

    // Independent of the production configuration so an accidental removal
    // of a lane or appearance fails here.
    private let expectedArtifacts = [
        ExpectedArtifact(filename: "sources-healthy-reference-light.png", pixelsWide: 2240, pixelsHigh: 1800),
        ExpectedArtifact(filename: "sources-healthy-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1800),
        ExpectedArtifact(filename: "sources-degraded-reference-light.png", pixelsWide: 2240, pixelsHigh: 2600),
        ExpectedArtifact(filename: "sources-degraded-reference-dark.png", pixelsWide: 2240, pixelsHigh: 2600),
    ]

    @MainActor
    func testFixtureCarriesBothDiagnosticsLanes() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let healthy = try XCTUnwrap(fixture.ingestionHealthySources?.ingestion)
        let degraded = try XCTUnwrap(fixture.ingestionDegraded?.ingestion)
        XCTAssertEqual(healthy.state, "healthy")
        XCTAssertEqual(healthy.issues?.count, 0)
        XCTAssertEqual(degraded.state, "degraded")
        XCTAssertEqual(degraded.sources?.count, 6)
        XCTAssertTrue(degraded.sources?.allSatisfy { $0.state == "degraded" } ?? false)
        // The store-wide fault is one issue naming every source, never one
        // copy per source.
        let global = degraded.issues?.filter { $0.code == "evidence_refreshable_usage_failed" } ?? []
        XCTAssertEqual(global.count, 1)
        XCTAssertNil(global.first?.source)
        XCTAssertEqual(global.first?.affectedSources?.count, 6)
    }

    @MainActor
    func testRendersStableDiagnosticsInBothAppearances() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let firstDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-sources-snapshots-\(UUID().uuidString)")
        let secondDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-sources-snapshots-\(UUID().uuidString)")
        defer {
            try? FileManager.default.removeItem(at: firstDirectory)
            try? FileManager.default.removeItem(at: secondDirectory)
        }

        let rendered = try SourcesSnapshotRenderer.render(fixture: fixture, outputDirectory: firstDirectory)
        _ = try SourcesSnapshotRenderer.render(fixture: fixture, outputDirectory: secondDirectory)

        XCTAssertEqual(rendered.map(\.lastPathComponent), expectedArtifacts.map(\.filename))
        for artifact in expectedArtifacts {
            let imageURL = firstDirectory.appendingPathComponent(artifact.filename)
            let image = try XCTUnwrap(NSImage(contentsOf: imageURL), artifact.filename)
            let representation = try XCTUnwrap(image.representations.first, artifact.filename)
            XCTAssertEqual(representation.pixelsWide, artifact.pixelsWide, artifact.filename)
            XCTAssertEqual(representation.pixelsHigh, artifact.pixelsHigh, artifact.filename)

            let difference = try VisualSnapshotHarness.compare(
                expectedURL: imageURL,
                actualURL: secondDirectory.appendingPathComponent(artifact.filename)
            )
            // Diagnostics renders carry the same sub-pixel anti-aliasing noise
            // the menu suite tolerates; a real change moves whole channels.
            XCTAssertLessThanOrEqual(
                difference.maximumChannelDelta,
                VisualSnapshotTolerance.crossMinorRenderingNoise.maximumChannelDelta,
                artifact.filename
            )
        }
    }

    private func fixtureURL() throws -> URL {
        try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
    }
}
