import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

struct VisualSnapshotTolerance: Equatable {
    let maximumChannelDelta: Int
    let maximumChangedChannelFraction: Double

    /// Absorbs only known one-step raster rounding, not visible UI changes.
    static let renderingNoise = Self(
        maximumChannelDelta: 1,
        maximumChangedChannelFraction: 0.001
    )
}

enum VisualSnapshotMode: String, Equatable {
    case verify
    case record

    static func resolve(environment: [String: String] = ProcessInfo.processInfo.environment) throws -> Self {
        let value = environment["AGENTACCT_SNAPSHOT_MODE"] ?? Self.verify.rawValue
        guard let mode = Self(rawValue: value) else {
            throw VisualSnapshotError.invalidMode(value)
        }
        if mode == .record, isTruthy(environment["CI"]) {
            throw VisualSnapshotError.recordingDisabledInCI
        }
        return mode
    }

    private static func isTruthy(_ value: String?) -> Bool {
        guard let value else { return false }
        return !["", "0", "false", "no"].contains(value.lowercased())
    }
}

enum VisualSnapshotResult: Equatable {
    case matched
    case recorded
    case retained
}

struct VisualSnapshotComparison: Equatable {
    let expectedWidth: Int
    let expectedHeight: Int
    let actualWidth: Int
    let actualHeight: Int
    let changedPixels: Int
    let totalPixels: Int
    let changedChannels: Int
    let totalChannels: Int
    let maximumChannelDelta: Int

    var dimensionsMatch: Bool {
        expectedWidth == actualWidth && expectedHeight == actualHeight
    }

    var changedPixelFraction: Double {
        guard totalPixels > 0 else { return dimensionsMatch ? 0 : 1 }
        return Double(changedPixels) / Double(totalPixels)
    }

    var changedChannelFraction: Double {
        guard totalChannels > 0 else { return dimensionsMatch ? 0 : 1 }
        return Double(changedChannels) / Double(totalChannels)
    }

    func isWithin(_ tolerance: VisualSnapshotTolerance) -> Bool {
        dimensionsMatch
            && maximumChannelDelta <= tolerance.maximumChannelDelta
            && changedChannelFraction <= tolerance.maximumChangedChannelFraction
    }
}

enum VisualSnapshotError: LocalizedError {
    case invalidMode(String)
    case recordingDisabledInCI
    case missingReference(URL)
    case cannotDecode(URL)
    case cannotCreateBitmap(URL)
    case cannotEncode(URL)
    case mismatch(
        name: String,
        comparison: VisualSnapshotComparison,
        tolerance: VisualSnapshotTolerance,
        artifactDirectory: URL
    )

    var errorDescription: String? {
        switch self {
        case .invalidMode(let value):
            return "Invalid AGENTACCT_SNAPSHOT_MODE '\(value)'; expected 'verify' or 'record'."
        case .recordingDisabledInCI:
            return "Visual snapshot recording is disabled in CI. "
                + "Record locally, review the image diff, and commit it explicitly."
        case .missingReference(let url):
            return "Missing visual reference at \(url.path). Record the selected test "
                + "explicitly with ./Scripts/visual-snapshots record <test-selector-or-path>."
        case .cannotDecode(let url):
            return "Could not decode PNG at \(url.path)."
        case .cannotCreateBitmap(let url):
            return "Could not normalize \(url.path) to sRGB RGBA8 pixels."
        case .cannotEncode(let url):
            return "Could not encode visual diff at \(url.path)."
        case .mismatch(let name, let comparison, let tolerance, let artifactDirectory):
            let changed = String(format: "%.4f%%", comparison.changedPixelFraction * 100)
            let changedChannels = String(format: "%.4f%%", comparison.changedChannelFraction * 100)
            let allowedChannels = String(format: "%.4f%%", tolerance.maximumChangedChannelFraction * 100)
            return """
            Visual snapshot \(name) changed: expected \(comparison.expectedWidth)x\(comparison.expectedHeight), \
            got \(comparison.actualWidth)x\(comparison.actualHeight); \(changed) of pixels and \
            \(changedChannels) of channels changed (allowed channels \(allowedChannels)); \
            maximum channel delta \(comparison.maximumChannelDelta) \
            (allowed \(tolerance.maximumChannelDelta)). Expected, actual, and diff artifacts: \
            \(artifactDirectory.path)
            """
        }
    }
}

struct VisualSnapshotImage {
    let width: Int
    let height: Int
    let rgba: Data

    init(width: Int, height: Int, rgba: Data) {
        precondition(width >= 0 && height >= 0)
        precondition(rgba.count == width * height * 4)
        self.width = width
        self.height = height
        self.rgba = rgba
    }

    init(contentsOf url: URL) throws {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
            throw VisualSnapshotError.cannotDecode(url)
        }
        let bytesPerRow = image.width * 4
        var pixels = Data(count: bytesPerRow * image.height)
        guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB) else {
            throw VisualSnapshotError.cannotCreateBitmap(url)
        }
        let rendered = pixels.withUnsafeMutableBytes { storage -> Bool in
            guard let context = CGContext(
                data: storage.baseAddress,
                width: image.width,
                height: image.height,
                bitsPerComponent: 8,
                bytesPerRow: bytesPerRow,
                space: colorSpace,
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
                    | CGBitmapInfo.byteOrder32Big.rawValue
            ) else { return false }
            context.setBlendMode(.copy)
            context.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
            return true
        }
        guard rendered else {
            throw VisualSnapshotError.cannotCreateBitmap(url)
        }
        self.init(width: image.width, height: image.height, rgba: pixels)
    }

    func writePNG(to url: URL) throws {
        var pixels = rgba
        guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
              let image = pixels.withUnsafeMutableBytes({ storage -> CGImage? in
                  guard let context = CGContext(
                      data: storage.baseAddress,
                      width: width,
                      height: height,
                      bitsPerComponent: 8,
                      bytesPerRow: width * 4,
                      space: colorSpace,
                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
                          | CGBitmapInfo.byteOrder32Big.rawValue
                  ) else { return nil }
                  return context.makeImage()
              }),
              let destination = CGImageDestinationCreateWithURL(
                  url as CFURL,
                  UTType.png.identifier as CFString,
                  1,
                  nil
              ) else {
            throw VisualSnapshotError.cannotEncode(url)
        }
        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else {
            throw VisualSnapshotError.cannotEncode(url)
        }
    }
}

enum VisualSnapshotHarness {
    static func assertSnapshot(
        name: String,
        referenceURL: URL,
        actualURL: URL,
        artifactDirectory: URL,
        mode: VisualSnapshotMode,
        tolerance: VisualSnapshotTolerance = .renderingNoise
    ) throws -> VisualSnapshotResult {
        switch mode {
        case .verify:
            try verify(
                name: name,
                expectedURL: referenceURL,
                actualURL: actualURL,
                artifactDirectory: artifactDirectory,
                tolerance: tolerance
            )
            return .matched
        case .record:
            // Decode before replacing a reviewed reference so a truncated or
            // invalid render can never become the new baseline.
            _ = try VisualSnapshotImage(contentsOf: actualURL)
            // Preserve a valid equivalent reference byte-for-byte. An invalid
            // or genuinely changed reference is intentionally replaced.
            if FileManager.default.fileExists(atPath: referenceURL.path),
               let comparison = try? compare(expectedURL: referenceURL, actualURL: actualURL),
               comparison.isWithin(tolerance) {
                try removeFailureArtifacts(name: name, from: artifactDirectory)
                return .retained
            }
            try FileManager.default.createDirectory(
                at: referenceURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try Data(contentsOf: actualURL).write(to: referenceURL, options: .atomic)
            try removeFailureArtifacts(name: name, from: artifactDirectory)
            return .recorded
        }
    }

    static func compare(
        expected: VisualSnapshotImage,
        actual: VisualSnapshotImage
    ) -> VisualSnapshotComparison {
        guard expected.width == actual.width, expected.height == actual.height else {
            let totalPixels = max(
                expected.width * expected.height,
                actual.width * actual.height
            )
            let totalChannels = max(expected.rgba.count, actual.rgba.count)
            return VisualSnapshotComparison(
                expectedWidth: expected.width,
                expectedHeight: expected.height,
                actualWidth: actual.width,
                actualHeight: actual.height,
                // Differently sized images have no pixel-for-pixel identity.
                // Report a complete change instead of the misleading 0% that
                // the dimension guard used to produce in failure messages.
                changedPixels: totalPixels,
                totalPixels: totalPixels,
                changedChannels: totalChannels,
                totalChannels: totalChannels,
                maximumChannelDelta: 255
            )
        }

        var changedPixels = 0
        var changedChannels = 0
        var maximumChannelDelta = 0
        expected.rgba.withUnsafeBytes { expectedBytes in
            actual.rgba.withUnsafeBytes { actualBytes in
                let expected = expectedBytes.bindMemory(to: UInt8.self)
                let actual = actualBytes.bindMemory(to: UInt8.self)
                for pixelStart in stride(from: 0, to: expected.count, by: 4) {
                    var pixelChanged = false
                    for channel in 0..<4 {
                        let delta = abs(Int(expected[pixelStart + channel]) - Int(actual[pixelStart + channel]))
                        maximumChannelDelta = max(maximumChannelDelta, delta)
                        if delta > 0 { changedChannels += 1 }
                        pixelChanged = pixelChanged || delta > 0
                    }
                    if pixelChanged { changedPixels += 1 }
                }
            }
        }
        return VisualSnapshotComparison(
            expectedWidth: expected.width,
            expectedHeight: expected.height,
            actualWidth: actual.width,
            actualHeight: actual.height,
            changedPixels: changedPixels,
            totalPixels: expected.width * expected.height,
            changedChannels: changedChannels,
            totalChannels: expected.rgba.count,
            maximumChannelDelta: maximumChannelDelta
        )
    }

    static func compare(expectedURL: URL, actualURL: URL) throws -> VisualSnapshotComparison {
        guard FileManager.default.fileExists(atPath: expectedURL.path) else {
            throw VisualSnapshotError.missingReference(expectedURL)
        }
        return compare(
            expected: try VisualSnapshotImage(contentsOf: expectedURL),
            actual: try VisualSnapshotImage(contentsOf: actualURL)
        )
    }

    static func verify(
        name: String,
        expectedURL: URL,
        actualURL: URL,
        artifactDirectory: URL,
        tolerance: VisualSnapshotTolerance = .renderingNoise
    ) throws {
        try removeFailureArtifacts(name: name, from: artifactDirectory)
        guard FileManager.default.fileExists(atPath: expectedURL.path) else {
            throw VisualSnapshotError.missingReference(expectedURL)
        }
        let expected = try VisualSnapshotImage(contentsOf: expectedURL)
        let actual = try VisualSnapshotImage(contentsOf: actualURL)
        let comparison = compare(expected: expected, actual: actual)
        if comparison.isWithin(tolerance) {
            return
        }
        try writeFailureArtifacts(
            name: name,
            expectedURL: expectedURL,
            actualURL: actualURL,
            expected: expected,
            actual: actual,
            to: artifactDirectory
        )
        throw VisualSnapshotError.mismatch(
            name: name,
            comparison: comparison,
            tolerance: tolerance,
            artifactDirectory: artifactDirectory
        )
    }

    private static func writeFailureArtifacts(
        name: String,
        expectedURL: URL,
        actualURL: URL,
        expected: VisualSnapshotImage,
        actual: VisualSnapshotImage,
        to directory: URL
    ) throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try copyReplacing(expectedURL, to: directory.appendingPathComponent("\(name).expected.png"))
        try copyReplacing(actualURL, to: directory.appendingPathComponent("\(name).actual.png"))

        guard expected.width == actual.width, expected.height == actual.height else { return }
        var diff = Data(count: actual.rgba.count)
        expected.rgba.withUnsafeBytes { expectedBytes in
            actual.rgba.withUnsafeBytes { actualBytes in
                diff.withUnsafeMutableBytes { diffBytes in
                    let expected = expectedBytes.bindMemory(to: UInt8.self)
                    let actual = actualBytes.bindMemory(to: UInt8.self)
                    let output = diffBytes.bindMemory(to: UInt8.self)
                    for pixelStart in stride(from: 0, to: expected.count, by: 4) {
                        let changed = (0..<4).contains { channel in
                            expected[pixelStart + channel] != actual[pixelStart + channel]
                        }
                        if changed {
                            output[pixelStart] = 255
                            output[pixelStart + 1] = 0
                            output[pixelStart + 2] = 128
                        } else {
                            output[pixelStart] = actual[pixelStart] / 5
                            output[pixelStart + 1] = actual[pixelStart + 1] / 5
                            output[pixelStart + 2] = actual[pixelStart + 2] / 5
                        }
                        output[pixelStart + 3] = 255
                    }
                }
            }
        }
        try VisualSnapshotImage(width: actual.width, height: actual.height, rgba: diff)
            .writePNG(to: directory.appendingPathComponent("\(name).diff.png"))
    }

    private static func copyReplacing(_ source: URL, to destination: URL) throws {
        if FileManager.default.fileExists(atPath: destination.path) {
            try FileManager.default.removeItem(at: destination)
        }
        try FileManager.default.copyItem(at: source, to: destination)
    }

    private static func removeFailureArtifacts(name: String, from directory: URL) throws {
        for suffix in ["expected", "actual", "diff"] {
            let artifact = directory.appendingPathComponent("\(name).\(suffix).png")
            if FileManager.default.fileExists(atPath: artifact.path) {
                try FileManager.default.removeItem(at: artifact)
            }
        }
    }
}
