import Foundation
import XCTest
@testable import agentacct

final class VisualSnapshotHarnessTests: XCTestCase {
    func testModeDefaultsToVerifyAndRejectsUnsafeValues() throws {
        XCTAssertEqual(try VisualSnapshotMode.resolve(environment: [:]), .verify)
        XCTAssertEqual(
            try VisualSnapshotMode.resolve(environment: ["AGENTACCT_SNAPSHOT_MODE": "record"]),
            .record
        )

        XCTAssertThrowsError(
            try VisualSnapshotMode.resolve(environment: ["AGENTACCT_SNAPSHOT_MODE": "replace"])
        ) { error in
            guard case VisualSnapshotError.invalidMode("replace") = error else {
                return XCTFail("Expected invalid-mode error; got \(error)")
            }
        }
        XCTAssertThrowsError(
            try VisualSnapshotMode.resolve(
                environment: ["AGENTACCT_SNAPSHOT_MODE": "record", "CI": "true"]
            )
        ) { error in
            guard case VisualSnapshotError.recordingDisabledInCI = error else {
                return XCTFail("Expected CI recording error; got \(error)")
            }
        }
    }

    func testIdenticalPixelsMatch() {
        let image = image(width: 2, height: 2, fill: [32, 64, 96, 255])
        let comparison = VisualSnapshotHarness.compare(expected: image, actual: image)

        XCTAssertTrue(comparison.isWithin(.renderingNoise))
        XCTAssertEqual(comparison.changedPixels, 0)
        XCTAssertEqual(comparison.maximumChannelDelta, 0)
    }

    func testToleranceBoundsBothMagnitudeAndArea() {
        let expected = image(width: 10, height: 10, fill: [32, 64, 96, 255])
        var smallDelta = expected.rgba
        smallDelta[0] += 1
        let roundingNoise = VisualSnapshotHarness.compare(
            expected: expected,
            actual: VisualSnapshotImage(width: 10, height: 10, rgba: smallDelta)
        )
        XCTAssertTrue(
            roundingNoise.isWithin(
                VisualSnapshotTolerance(maximumChannelDelta: 1, maximumChangedChannelFraction: 0.02)
            )
        )

        var largeDelta = expected.rgba
        largeDelta[0] += 2
        let visibleChange = VisualSnapshotHarness.compare(
            expected: expected,
            actual: VisualSnapshotImage(width: 10, height: 10, rgba: largeDelta)
        )
        XCTAssertFalse(
            visibleChange.isWithin(
                VisualSnapshotTolerance(maximumChannelDelta: 1, maximumChangedChannelFraction: 0.02)
            )
        )

        var widespreadDelta = expected.rgba
        for pixelStart in stride(from: 0, to: widespreadDelta.count, by: 4) {
            widespreadDelta[pixelStart] += 1
        }
        let widespreadNoise = VisualSnapshotHarness.compare(
            expected: expected,
            actual: VisualSnapshotImage(width: 10, height: 10, rgba: widespreadDelta)
        )
        XCTAssertFalse(widespreadNoise.isWithin(.renderingNoise))
    }

    func testMismatchWritesExpectedActualAndDiffArtifacts() throws {
        let temporaryDirectory = try makeTemporaryDirectory(named: "mismatch")
        let expectedURL = temporaryDirectory.appendingPathComponent("expected.png")
        let actualURL = temporaryDirectory.appendingPathComponent("actual.png")
        let artifactDirectory = temporaryDirectory.appendingPathComponent("failures")

        try image(width: 2, height: 2, fill: [32, 64, 96, 255]).writePNG(to: expectedURL)
        try image(width: 2, height: 2, fill: [200, 64, 96, 255]).writePNG(to: actualURL)

        XCTAssertThrowsError(
            try VisualSnapshotHarness.verify(
                name: "sample",
                expectedURL: expectedURL,
                actualURL: actualURL,
                artifactDirectory: artifactDirectory
            )
        )
        XCTAssertTrue(
            FileManager.default.fileExists(
                atPath: artifactDirectory.appendingPathComponent("sample.expected.png").path
            )
        )
        XCTAssertTrue(
            FileManager.default.fileExists(
                atPath: artifactDirectory.appendingPathComponent("sample.actual.png").path
            )
        )
        XCTAssertTrue(
            FileManager.default.fileExists(
                atPath: artifactDirectory.appendingPathComponent("sample.diff.png").path
            )
        )
    }

    func testRecordModeAtomicallyCreatesAValidReference() throws {
        let temporaryDirectory = try makeTemporaryDirectory(named: "record")
        let referenceURL = temporaryDirectory.appendingPathComponent("references/sample.png")
        let actualURL = temporaryDirectory.appendingPathComponent("actual.png")
        let artifactDirectory = temporaryDirectory.appendingPathComponent("failures")
        try image(width: 2, height: 2, fill: [40, 80, 120, 255]).writePNG(to: actualURL)

        let result = try VisualSnapshotHarness.assertSnapshot(
            name: "sample",
            referenceURL: referenceURL,
            actualURL: actualURL,
            artifactDirectory: artifactDirectory,
            mode: .record
        )

        XCTAssertEqual(result, .recorded)
        XCTAssertTrue(FileManager.default.fileExists(atPath: referenceURL.path))
        XCTAssertTrue(
            try VisualSnapshotHarness.compare(expectedURL: referenceURL, actualURL: actualURL)
                .isWithin(.renderingNoise)
        )
    }

    func testRecordModeReplacesAnInvalidReferenceWithAValidRender() throws {
        let temporaryDirectory = try makeTemporaryDirectory(named: "repair")
        let referenceURL = temporaryDirectory.appendingPathComponent("references/sample.png")
        let actualURL = temporaryDirectory.appendingPathComponent("actual.png")
        let artifactDirectory = temporaryDirectory.appendingPathComponent("failures")
        try FileManager.default.createDirectory(
            at: referenceURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try Data("invalid PNG".utf8).write(to: referenceURL)
        try image(width: 2, height: 2, fill: [40, 80, 120, 255]).writePNG(to: actualURL)

        let result = try VisualSnapshotHarness.assertSnapshot(
            name: "sample",
            referenceURL: referenceURL,
            actualURL: actualURL,
            artifactDirectory: artifactDirectory,
            mode: .record
        )

        XCTAssertEqual(result, .recorded)
        XCTAssertTrue(
            try VisualSnapshotHarness.compare(
                expectedURL: referenceURL,
                actualURL: actualURL
            ).isWithin(.renderingNoise)
        )
    }

    func testRecordModeRetainsAnEquivalentReferenceAndClearsStaleArtifacts() throws {
        let temporaryDirectory = try makeTemporaryDirectory(named: "retain")
        let referenceURL = temporaryDirectory.appendingPathComponent("references/sample.png")
        let actualURL = temporaryDirectory.appendingPathComponent("actual.png")
        let artifactDirectory = temporaryDirectory.appendingPathComponent("failures")
        try FileManager.default.createDirectory(
            at: referenceURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try FileManager.default.createDirectory(at: artifactDirectory, withIntermediateDirectories: true)
        try image(width: 2, height: 2, fill: [40, 80, 120, 255]).writePNG(to: referenceURL)
        try image(width: 2, height: 2, fill: [41, 80, 120, 255]).writePNG(to: actualURL)
        let originalReference = try Data(contentsOf: referenceURL)
        try writeStaleArtifacts(named: "sample", to: artifactDirectory)

        let result = try VisualSnapshotHarness.assertSnapshot(
            name: "sample",
            referenceURL: referenceURL,
            actualURL: actualURL,
            artifactDirectory: artifactDirectory,
            mode: .record,
            tolerance: VisualSnapshotTolerance(
                maximumChannelDelta: 1,
                maximumChangedChannelFraction: 0.25
            )
        )

        XCTAssertEqual(result, .retained)
        XCTAssertEqual(try Data(contentsOf: referenceURL), originalReference)
        XCTAssertEqual(
            try FileManager.default.contentsOfDirectory(atPath: artifactDirectory.path),
            []
        )
    }

    func testDimensionMismatchDoesNotLeaveAStaleDiffImage() throws {
        let temporaryDirectory = try makeTemporaryDirectory(named: "dimensions")
        let expectedURL = temporaryDirectory.appendingPathComponent("expected.png")
        let actualURL = temporaryDirectory.appendingPathComponent("actual.png")
        let artifactDirectory = temporaryDirectory.appendingPathComponent("failures")
        try FileManager.default.createDirectory(at: artifactDirectory, withIntermediateDirectories: true)
        try image(width: 2, height: 2, fill: [40, 80, 120, 255]).writePNG(to: expectedURL)
        try image(width: 3, height: 2, fill: [40, 80, 120, 255]).writePNG(to: actualURL)
        try Data("stale".utf8).write(
            to: artifactDirectory.appendingPathComponent("sample.diff.png")
        )

        XCTAssertThrowsError(
            try VisualSnapshotHarness.verify(
                name: "sample",
                expectedURL: expectedURL,
                actualURL: actualURL,
                artifactDirectory: artifactDirectory
            )
        )
        XCTAssertTrue(
            FileManager.default.fileExists(
                atPath: artifactDirectory.appendingPathComponent("sample.expected.png").path
            )
        )
        XCTAssertTrue(
            FileManager.default.fileExists(
                atPath: artifactDirectory.appendingPathComponent("sample.actual.png").path
            )
        )
        XCTAssertFalse(
            FileManager.default.fileExists(
                atPath: artifactDirectory.appendingPathComponent("sample.diff.png").path
            )
        )
    }

    func testDimensionChangeCannotMatch() {
        let expected = image(width: 2, height: 2, fill: [32, 64, 96, 255])
        let actual = image(width: 3, height: 2, fill: [32, 64, 96, 255])
        let comparison = VisualSnapshotHarness.compare(expected: expected, actual: actual)

        XCTAssertFalse(comparison.dimensionsMatch)
        XCTAssertFalse(comparison.isWithin(.renderingNoise))
        XCTAssertEqual(comparison.changedPixelFraction, 1)
        XCTAssertEqual(comparison.changedChannelFraction, 1)
    }

    func testSuccessfulVerifyClearsStaleFailureArtifacts() throws {
        let temporaryDirectory = try makeTemporaryDirectory(named: "verify-cleanup")
        let expectedURL = temporaryDirectory.appendingPathComponent("expected.png")
        let actualURL = temporaryDirectory.appendingPathComponent("actual.png")
        let artifactDirectory = temporaryDirectory.appendingPathComponent("failures")
        try FileManager.default.createDirectory(at: artifactDirectory, withIntermediateDirectories: true)
        try image(width: 2, height: 2, fill: [40, 80, 120, 255]).writePNG(to: expectedURL)
        try image(width: 2, height: 2, fill: [40, 80, 120, 255]).writePNG(to: actualURL)
        try writeStaleArtifacts(named: "sample", to: artifactDirectory)

        try VisualSnapshotHarness.verify(
            name: "sample",
            expectedURL: expectedURL,
            actualURL: actualURL,
            artifactDirectory: artifactDirectory
        )

        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: artifactDirectory.path), [])
    }

    func testVerifyRequiresAReviewedReference() throws {
        let temporaryDirectory = try makeTemporaryDirectory(named: "missing-reference")
        let referenceURL = temporaryDirectory.appendingPathComponent("missing.png")
        let actualURL = temporaryDirectory.appendingPathComponent("actual.png")
        let artifactDirectory = temporaryDirectory.appendingPathComponent("failures")
        try image(width: 2, height: 2, fill: [40, 80, 120, 255]).writePNG(to: actualURL)

        XCTAssertThrowsError(
            try VisualSnapshotHarness.verify(
                name: "sample",
                expectedURL: referenceURL,
                actualURL: actualURL,
                artifactDirectory: artifactDirectory
            )
        ) { error in
            guard case VisualSnapshotError.missingReference(let missingURL) = error else {
                return XCTFail("Expected a missing-reference error; got \(error)")
            }
            XCTAssertEqual(missingURL, referenceURL)
        }
    }

    func testRecordModeDoesNotReplaceAReferenceWithAnInvalidRender() throws {
        let temporaryDirectory = try makeTemporaryDirectory(named: "invalid-render")
        let referenceURL = temporaryDirectory.appendingPathComponent("reference.png")
        let actualURL = temporaryDirectory.appendingPathComponent("actual.png")
        let artifactDirectory = temporaryDirectory.appendingPathComponent("failures")
        try image(width: 2, height: 2, fill: [40, 80, 120, 255]).writePNG(to: referenceURL)
        try Data("invalid PNG".utf8).write(to: actualURL)
        let originalReference = try Data(contentsOf: referenceURL)

        XCTAssertThrowsError(
            try VisualSnapshotHarness.assertSnapshot(
                name: "sample",
                referenceURL: referenceURL,
                actualURL: actualURL,
                artifactDirectory: artifactDirectory,
                mode: .record
            )
        ) { error in
            guard case VisualSnapshotError.cannotDecode(let rejectedURL) = error else {
                return XCTFail("Expected the invalid render to be rejected; got \(error)")
            }
            XCTAssertEqual(rejectedURL, actualURL)
        }
        XCTAssertEqual(try Data(contentsOf: referenceURL), originalReference)
    }

    func testRecordModeReplacesAChangedValidReference() throws {
        let temporaryDirectory = try makeTemporaryDirectory(named: "changed-reference")
        let referenceURL = temporaryDirectory.appendingPathComponent("reference.png")
        let actualURL = temporaryDirectory.appendingPathComponent("actual.png")
        let artifactDirectory = temporaryDirectory.appendingPathComponent("failures")
        try image(width: 2, height: 2, fill: [40, 80, 120, 255]).writePNG(to: referenceURL)
        try image(width: 2, height: 2, fill: [120, 80, 40, 255]).writePNG(to: actualURL)

        let result = try VisualSnapshotHarness.assertSnapshot(
            name: "sample",
            referenceURL: referenceURL,
            actualURL: actualURL,
            artifactDirectory: artifactDirectory,
            mode: .record
        )

        XCTAssertEqual(result, .recorded)
        XCTAssertTrue(
            try VisualSnapshotHarness.compare(expectedURL: referenceURL, actualURL: actualURL)
                .isWithin(.renderingNoise)
        )
    }

    private func image(width: Int, height: Int, fill: [UInt8]) -> VisualSnapshotImage {
        VisualSnapshotImage(
            width: width,
            height: height,
            rgba: Data(Array(repeating: fill, count: width * height).flatMap { $0 })
        )
    }

    private func makeTemporaryDirectory(named purpose: String) throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-visual-\(purpose)-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }
        return directory
    }

    private func writeStaleArtifacts(named name: String, to directory: URL) throws {
        for suffix in ["expected", "actual", "diff"] {
            try Data("stale".utf8).write(
                to: directory.appendingPathComponent("\(name).\(suffix).png")
            )
        }
    }
}
