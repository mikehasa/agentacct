import SwiftUI

struct SetupSnapshotConfiguration {
    let colorScheme: ColorScheme

    var filename: String {
        "setup-failure-\(colorScheme == .dark ? "dark" : "light").png"
    }

    static let reviewConfigurations: [Self] = [
        Self(colorScheme: .light),
        Self(colorScheme: .dark),
    ]
}

enum SetupSnapshotRenderer {
    static let failureMessage = "Recorder setup exited with status 9."
    static let failureLog = [
        "Running: agentacct onboard --agent auto --yes",
        "Configured Claude Code MCP server",
        "Could not update ~/.codex/config.toml: permission denied",
        "error: Recorder setup exited with status 9.",
    ]

    @MainActor
    static func render(
        outputDirectory: URL,
        configurations: [SetupSnapshotConfiguration] = SetupSnapshotConfiguration.reviewConfigurations
    ) throws -> [URL] {
        try FileManager.default.createDirectory(
            at: outputDirectory,
            withIntermediateDirectories: true
        )
        SnapshotMode.enabled = true
        defer {
            SnapshotMode.enabled = false
            SnapshotScheme.override = nil
        }

        return try configurations.map { configuration in
            SnapshotScheme.override = configuration.colorScheme
            let setup = SetupModel(
                preloaded: .failed(failureMessage),
                log: failureLog
            )
            let view = SetupSheet(setup: setup, onClose: {})
                .fixedSize(horizontal: true, vertical: true)
                .environment(\.colorScheme, configuration.colorScheme)
                .environment(\.displayScale, 2)
                .environment(\.layoutDirection, .leftToRight)
                .environment(\.dynamicTypeSize, .medium)
                .environment(\.controlSize, .regular)
                .environment(\.legibilityWeight, nil)
                .environment(\.appearsActive, true)
                .transaction { transaction in
                    transaction.disablesAnimations = true
                }
            let size = try SnapshotImageWriter.renderedSize(view)
            let outputURL = outputDirectory.appendingPathComponent(configuration.filename)
            try SnapshotImageWriter.render(view, to: outputURL, size: size)
            return outputURL
        }
    }
}
