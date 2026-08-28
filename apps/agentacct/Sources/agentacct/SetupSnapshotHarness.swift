import SwiftUI

enum SetupSnapshotState: String {
    case idle
    case done
    case failure
}

struct SetupSnapshotConfiguration {
    let state: SetupSnapshotState
    let colorScheme: ColorScheme

    var filename: String {
        "setup-\(state.rawValue)-\(colorScheme == .dark ? "dark" : "light").png"
    }

    static let reviewConfigurations: [Self] = [
        Self(state: .idle, colorScheme: .light),
        Self(state: .idle, colorScheme: .dark),
        Self(state: .done, colorScheme: .light),
        Self(state: .done, colorScheme: .dark),
        Self(state: .failure, colorScheme: .light),
        Self(state: .failure, colorScheme: .dark),
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
    static let successLog = [
        "Running: agentacct onboard --agent auto --yes",
        "Configured Claude Code",
        "Configured Codex",
        "Setup complete",
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
            let setup: SetupModel
            switch configuration.state {
            case .idle:
                setup = SetupModel(preloaded: .idle, log: [])
            case .done:
                setup = SetupModel(preloaded: .done, log: successLog)
            case .failure:
                setup = SetupModel(preloaded: .failed(failureMessage), log: failureLog)
            }
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
