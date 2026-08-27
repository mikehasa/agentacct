import Foundation

private final class AboutSnapshotBundleLocator: NSObject {}

enum AboutSnapshotProcessError: LocalizedError {
    case executableUnavailable(URL)
    case operationFailed(operation: String, underlying: Error)
    case failed(status: Int32, output: String)

    var errorDescription: String? {
        switch self {
        case .executableUnavailable(let url):
            return "The snapshot app executable is unavailable at \(url.path)"
        case .operationFailed(let operation, let underlying):
            return "The snapshot app could not \(operation): \(underlying.localizedDescription)"
        case .failed(let status, let output):
            return "The snapshot app exited with status \(status): \(output)"
        }
    }
}

enum AboutSnapshotProcess {
    static let filenames = [
        "about-panel-light.png",
        "about-panel-dark.png",
    ]

    static func render(outputDirectory: URL) throws -> [URL] {
        guard FileManager.default.isExecutableFile(atPath: executableURL.path) else {
            throw AboutSnapshotProcessError.executableUnavailable(executableURL)
        }
        let processOutputDirectory = executableURL.deletingLastPathComponent()
            .appendingPathComponent("agentacct-about-snapshots-\(UUID().uuidString)")
        defer {
            if FileManager.default.fileExists(atPath: processOutputDirectory.path) {
                try? FileManager.default.removeItem(at: processOutputDirectory)
            }
        }
        do {
            try FileManager.default.createDirectory(
                at: outputDirectory,
                withIntermediateDirectories: true
            )
            try FileManager.default.createDirectory(
                at: processOutputDirectory,
                withIntermediateDirectories: true
            )
        } catch {
            throw AboutSnapshotProcessError.operationFailed(
                operation: "create its output directory",
                underlying: error
            )
        }

        let output = Pipe()
        let process = Process()
        process.executableURL = executableURL
        process.arguments = [
            "--snapshot-about",
            applicationIconURL.path,
            processOutputDirectory.path,
        ]
        process.standardOutput = output
        process.standardError = output
        do {
            try process.run()
        } catch {
            throw AboutSnapshotProcessError.operationFailed(
                operation: "launch",
                underlying: error
            )
        }
        process.waitUntilExit()

        let outputData = output.fileHandleForReading.readDataToEndOfFile()
        let outputText = String(decoding: outputData, as: UTF8.self)
        guard process.terminationStatus == 0 else {
            throw AboutSnapshotProcessError.failed(
                status: process.terminationStatus,
                output: outputText
            )
        }
        do {
            return try filenames.map { filename in
                let sourceURL = processOutputDirectory.appendingPathComponent(filename)
                let destinationURL = outputDirectory.appendingPathComponent(filename)
                try FileManager.default.copyItem(at: sourceURL, to: destinationURL)
                return destinationURL
            }
        } catch {
            throw AboutSnapshotProcessError.operationFailed(
                operation: "copy its rendered images",
                underlying: error
            )
        }
    }

    private static let executableURL = Bundle(for: AboutSnapshotBundleLocator.self).bundleURL
        .deletingLastPathComponent()
        .appendingPathComponent("agentacct")

    private static let applicationIconURL = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .appendingPathComponent("Resources/AppIcon.icns")
}
