import AppKit
import ImageIO
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
        ExpectedArtifact(filename: "dashboard-reference-light.png", pixelsWide: 2240, pixelsHigh: 1360),
        ExpectedArtifact(filename: "dashboard-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1360),
    ]

    @MainActor
    func testRendersEveryDashboardReviewConfiguration() throws {
        let fixtureURL = try XCTUnwrap(
            Bundle.module.url(forResource: "dashboard", withExtension: "json")
        )
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL)
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
            let firstPixels = try pixelData(at: imageURL)
            let secondPixels = try pixelData(
                at: secondOutputDirectory.appendingPathComponent(artifact.filename)
            )
            let difference = pixelDifference(firstPixels, secondPixels)
            // Core Graphics can round antialiased edges one 8-bit color step
            // differently between renders. Larger or more widespread changes
            // indicate dynamic data, animation, or an unstable layout.
            XCTAssertLessThanOrEqual(
                difference.maximumDelta,
                1,
                "Repeated fixture renders exceeded the one-step antialiasing budget"
            )
            XCTAssertLessThanOrEqual(
                Double(difference.changedBytes) / Double(firstPixels.count),
                0.001,
                "Repeated fixture renders changed more than 0.1% of normalized pixel bytes"
            )
        }

        let light = try pixelData(
            at: outputDirectory.appendingPathComponent("dashboard-reference-light.png")
        )
        let dark = try pixelData(
            at: outputDirectory.appendingPathComponent("dashboard-reference-dark.png")
        )
        let appearanceDifference = pixelDifference(light, dark)
        XCTAssertGreaterThan(
            appearanceDifference.maximumDelta,
            32,
            "Light and dark review artifacts must have visibly different colors"
        )
        XCTAssertGreaterThan(
            Double(appearanceDifference.changedBytes) / Double(light.count),
            0.05,
            "Light and dark review artifacts must differ across a meaningful part of the dashboard"
        )
    }

    func testRejectsUnsupportedVersionedFixtureSchemas() throws {
        let fixtureURL = try XCTUnwrap(
            Bundle.module.url(forResource: "dashboard", withExtension: "json")
        )
        let validFixture = try String(contentsOf: fixtureURL, encoding: .utf8)
        let schemas = [
            (payload: "glance", supported: GlanceClient.supportedGlanceSchema),
            (payload: "sessions", supported: DashboardSnapshotFixture.supportedSessionsSchema),
            (payload: "plan", supported: DashboardSnapshotFixture.supportedPlanSchema),
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

    private func pixelData(at url: URL) throws -> Data {
        let source = try XCTUnwrap(CGImageSourceCreateWithURL(url as CFURL, nil), url.lastPathComponent)
        let image = try XCTUnwrap(CGImageSourceCreateImageAtIndex(source, 0, nil), url.lastPathComponent)
        let bytesPerRow = image.width * 4
        var pixels = Data(count: bytesPerRow * image.height)
        let colorSpace = try XCTUnwrap(CGColorSpace(name: CGColorSpace.sRGB))
        let rendered = pixels.withUnsafeMutableBytes { storage -> Bool in
            guard let context = CGContext(
                data: storage.baseAddress,
                width: image.width,
                height: image.height,
                bitsPerComponent: 8,
                bytesPerRow: bytesPerRow,
                space: colorSpace,
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
            ) else { return false }
            context.setBlendMode(.copy)
            context.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
            return true
        }
        XCTAssertTrue(rendered, "Could not normalize \(url.lastPathComponent) to RGBA8")
        return pixels
    }

    private func pixelDifference(_ first: Data, _ second: Data) -> (changedBytes: Int, maximumDelta: Int) {
        precondition(first.count == second.count)
        return zip(first, second).reduce(into: (changedBytes: 0, maximumDelta: 0)) { result, pair in
            let delta = abs(Int(pair.0) - Int(pair.1))
            if delta > 0 { result.changedBytes += 1 }
            result.maximumDelta = max(result.maximumDelta, delta)
        }
    }
}
