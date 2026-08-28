import AppKit
import SwiftUI
import XCTest
@testable import agentacct

final class UsageSnapshotHarnessTests: XCTestCase {
    private struct ExpectedArtifact {
        let filename: String
        let pixelsWide: Int
        let pixelsHigh: Int
    }

    private let expectedArtifacts = [
        ExpectedArtifact(filename: "usage-minimum-light.png", pixelsWide: 1920, pixelsHigh: 1120),
        ExpectedArtifact(filename: "usage-minimum-dark.png", pixelsWide: 1920, pixelsHigh: 1120),
        ExpectedArtifact(filename: "usage-reference-light.png", pixelsWide: 2240, pixelsHigh: 1800),
        ExpectedArtifact(filename: "usage-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1800),
        ExpectedArtifact(filename: "usage-disconnected-reference-light.png", pixelsWide: 2240, pixelsHigh: 1800),
        ExpectedArtifact(filename: "usage-disconnected-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1800),
    ]

    @MainActor
    func testFixtureProducesJoinedCapacityRows() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let result = UsageCapacitySnapshot.build(
            usage: fixture.usage.byClient,
            limits: fixture.glance.limits,
            plans: fixture.plan.clients,
            showStale: false
        )

        XCTAssertEqual(result.rows.map(\.client), ["claude-code", "codex", "hermes"])
        XCTAssertEqual(result.rows.first?.readings.flatMap { $0.entry.windows ?? [] }.count, 2)
        XCTAssertTrue(try XCTUnwrap(result.rows.last).readings.isEmpty)
    }

    @MainActor
    func testRendersEveryUsageReviewConfigurationDeterministically() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let firstDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-usage-snapshots-\(UUID().uuidString)")
        let secondDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-usage-snapshots-\(UUID().uuidString)")
        defer {
            try? FileManager.default.removeItem(at: firstDirectory)
            try? FileManager.default.removeItem(at: secondDirectory)
        }

        let rendered = try UsageSnapshotRenderer.render(
            fixture: fixture, outputDirectory: firstDirectory
        )
        _ = try UsageSnapshotRenderer.render(fixture: fixture, outputDirectory: secondDirectory)

        XCTAssertEqual(rendered.map(\.lastPathComponent), expectedArtifacts.map(\.filename))
        for artifact in expectedArtifacts {
            let firstURL = firstDirectory.appendingPathComponent(artifact.filename)
            let image = try XCTUnwrap(NSImage(contentsOf: firstURL), artifact.filename)
            let representation = try XCTUnwrap(image.representations.first, artifact.filename)
            XCTAssertEqual(representation.pixelsWide, artifact.pixelsWide)
            XCTAssertEqual(representation.pixelsHigh, artifact.pixelsHigh)
            let difference = try VisualSnapshotHarness.compare(
                expectedURL: firstURL,
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
        XCTAssertFalse(SnapshotMode.enabled)
        XCTAssertFalse(SnapshotMode.boundsScrollContentToViewport)
        XCTAssertNil(SnapshotScheme.override)
    }

    @MainActor
    func testLimitMeterThresholdMarkersAreCenteredOnTheTrack() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-limit-meter-\(UUID().uuidString)")
        let outputURL = directory.appendingPathComponent("meter.png")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer {
            SnapshotScheme.override = nil
            try? FileManager.default.removeItem(at: directory)
        }

        SnapshotScheme.override = .light
        let view = LimitMeter(usedPercent: 0)
            .frame(width: 200)
            .padding(.vertical, 4)
            .background(Theme.card)
            .environment(\.colorScheme, .light)
            .environment(\.displayScale, 2)
        try SnapshotImageWriter.render(
            view,
            to: outputURL,
            size: CGSize(width: 200, height: 16)
        )

        let image = try VisualSnapshotImage(contentsOf: outputURL)
        let markerRows = darkPixelRows(in: image, xRange: 294...306)
        let trackRows = nonWhitePixelRows(in: image, x: 200)
        let firstMarkerColumns = darkPixelColumns(in: image, xRange: 280...320)
        let secondMarkerColumns = darkPixelColumns(in: image, xRange: 340...380)

        XCTAssertEqual(image.height, 32)
        XCTAssertEqual(markerRows.count, 24)
        XCTAssertEqual(trackRows.count, 16)
        XCTAssertEqual(try pixelCenter(of: markerRows), try pixelCenter(of: trackRows), accuracy: 0.25)
        // Pixel indices describe samples centered at n + 0.5. At 2×, the
        // geometric 75% and 90% coordinates therefore land at 300.5/360.5.
        XCTAssertEqual(try pixelCenter(of: firstMarkerColumns), 300.5, accuracy: 0.25)
        XCTAssertEqual(try pixelCenter(of: secondMarkerColumns), 360.5, accuracy: 0.25)
    }

    private func darkPixelRows(in image: VisualSnapshotImage, xRange: ClosedRange<Int>) -> [Int] {
        image.rgba.withUnsafeBytes { storage in
            let pixels = storage.bindMemory(to: UInt8.self)
            return (0..<image.height).filter { y in
                xRange.contains { x in
                    let offset = (y * image.width + x) * 4
                    return pixels[offset] < 180
                        && pixels[offset + 1] < 180
                        && pixels[offset + 2] < 180
                        && pixels[offset + 3] > 240
                }
            }
        }
    }

    private func darkPixelColumns(in image: VisualSnapshotImage, xRange: ClosedRange<Int>) -> [Int] {
        image.rgba.withUnsafeBytes { storage in
            let pixels = storage.bindMemory(to: UInt8.self)
            return xRange.filter { x in
                (0..<image.height).contains { y in
                    let offset = (y * image.width + x) * 4
                    return pixels[offset] < 180
                        && pixels[offset + 1] < 180
                        && pixels[offset + 2] < 180
                        && pixels[offset + 3] > 240
                }
            }
        }
    }

    private func nonWhitePixelRows(in image: VisualSnapshotImage, x: Int) -> [Int] {
        image.rgba.withUnsafeBytes { storage in
            let pixels = storage.bindMemory(to: UInt8.self)
            return (0..<image.height).filter { y in
                let offset = (y * image.width + x) * 4
                return pixels[offset] < 250
                    && pixels[offset + 1] < 250
                    && pixels[offset + 2] < 250
                    && pixels[offset + 3] > 240
            }
        }
    }

    private func pixelCenter(of columns: [Int]) throws -> Double {
        let first = try XCTUnwrap(columns.first)
        let last = try XCTUnwrap(columns.last)
        return (Double(first + last) + 1) / 2
    }

    private func fixtureURL() throws -> URL {
        try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
    }
}
