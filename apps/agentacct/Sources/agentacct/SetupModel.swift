import Foundation

// The one-click "Set up recording" flow for the packaged app: install the
// embedded standalone CLI to a stable location and run its `onboard` so a
// machine with no Python gets the MCP servers, hooks, and standing instructions
// configured for its coding agents. This is the exact CLI install flow
// (`agentacct onboard`) the docs describe — the app just drives it for a user
// who never opens a terminal. Idempotent: re-running is safe.
//
// Layout (matches the validated install simulation):
//   ~/.local/share/agentacct/cli/     <- the onedir CLI (binary + _internal)
//   ~/.local/bin/agentacct            <- a wrapper on PATH -> the real binary
// onboard, run from the installed binary, writes that stable installed path
// into every hook/MCP config, so an app upgrade that replaces the CLI in place
// keeps every registration valid.

@MainActor
final class SetupModel: ObservableObject {
    enum Phase: Equatable {
        case idle
        case working(String)   // a short status line
        case done
        case failed(String)
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var log: [String] = []

    private let fm = FileManager.default

    // MARK: locations

    /// The CLI embedded in the app bundle (Contents/Resources/cli), if this is
    /// a packaged build. nil for a dev app built without the frozen CLI.
    var bundledCLIDir: URL? {
        guard let res = Bundle.main.resourceURL else { return nil }
        let dir = res.appendingPathComponent("cli", isDirectory: true)
        return fm.fileExists(atPath: dir.appendingPathComponent("agentacct").path) ? dir : nil
    }

    private var home: URL { fm.homeDirectoryForCurrentUser }
    private var installedCLIDir: URL { home.appendingPathComponent(".local/share/agentacct/cli", isDirectory: true) }
    private var installedBinary: URL { installedCLIDir.appendingPathComponent("agentacct") }
    private var binDir: URL { home.appendingPathComponent(".local/bin", isDirectory: true) }
    private var wrapper: URL { binDir.appendingPathComponent("agentacct") }

    /// True once the CLI has been installed to the stable location by this app.
    var isCLIInstalled: Bool { fm.isExecutableFile(atPath: installedBinary.path) }

    /// Show the first-run setup prompt only when we CAN install (packaged
    /// build) and haven't yet. A dev build (no embedded CLI) never prompts.
    var shouldOfferSetup: Bool { bundledCLIDir != nil && !isCLIInstalled }

    // MARK: run

    func setUp() async {
        guard case .idle = phase else { return }
        log = []
        do {
            try installCLI()
            try await runOnboard()
            phase = .done
        } catch {
            phase = .failed(error.localizedDescription)
            append("error: \(error.localizedDescription)")
        }
    }

    func reset() { phase = .idle }

    // MARK: steps

    private func installCLI() throws {
        guard let bundled = bundledCLIDir else {
            throw SetupError.noEmbeddedCLI
        }
        phase = .working("Installing the recorder…")
        append("Installing CLI to ~/.local/share/agentacct/cli")
        try fm.createDirectory(at: installedCLIDir.deletingLastPathComponent(),
                               withIntermediateDirectories: true)
        if fm.fileExists(atPath: installedCLIDir.path) {
            try fm.removeItem(at: installedCLIDir)  // replace in place on upgrade
        }
        try fm.copyItem(at: bundled, to: installedCLIDir)

        // Wrapper on PATH so the user can run `agentacct` in a terminal too. A
        // script (not a symlink) is bulletproof for a onedir PyInstaller binary,
        // which resolves its _internal/ next to the real executable.
        try fm.createDirectory(at: binDir, withIntermediateDirectories: true)
        let script = "#!/bin/sh\nexec \"\(installedBinary.path)\" \"$@\"\n"
        try script.write(to: wrapper, atomically: true, encoding: .utf8)
        try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: wrapper.path)
        append("Wrote ~/.local/bin/agentacct")
    }

    private func runOnboard() async throws {
        phase = .working("Configuring your coding agents…")
        append("Running: agentacct onboard --agent auto --yes")
        // Run from the INSTALLED binary so onboard stamps the stable installed
        // path into every hook/MCP config (verified end-to-end).
        let stream = try ProcessRunner.run(
            executable: installedBinary,
            arguments: ["onboard", "--agent", "auto", "--yes"]
        )
        for await line in stream {
            append(line)
        }
    }

    private func append(_ line: String) {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        log.append(trimmed)
        if log.count > 200 { log.removeFirst(log.count - 200) }
    }

    enum SetupError: LocalizedError {
        case noEmbeddedCLI
        var errorDescription: String? {
            switch self {
            case .noEmbeddedCLI:
                return "This build has no embedded CLI. Install agentacct with `pipx install agentacct` instead."
            }
        }
    }
}

// MARK: - subprocess helper

enum ProcessRunner {
    /// Launch a process and yield its merged stdout+stderr line by line. The
    /// AsyncStream finishes when the process exits.
    static func run(executable: URL, arguments: [String]) throws -> AsyncStream<String> {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        // A frozen binary resolves its own paths; keep the user's environment
        // (HOME, PATH) so onboard writes to the real ~/.claude, ~/.codex.
        process.environment = ProcessInfo.processInfo.environment

        return AsyncStream<String> { continuation in
            let handle = pipe.fileHandleForReading
            var buffer = Data()
            let drainTail: () -> Void = {
                if !buffer.isEmpty, let tail = String(data: buffer, encoding: .utf8) {
                    continuation.yield(tail)
                }
                buffer.removeAll()
            }
            handle.readabilityHandler = { fh in
                let chunk = fh.availableData
                if chunk.isEmpty {
                    // EOF: yield any unterminated tail once and STOP — leaving the
                    // handler installed would busy-spin on repeated empty reads and
                    // re-yield the same tail every tick.
                    drainTail()
                    fh.readabilityHandler = nil
                    return
                }
                buffer.append(chunk)
                while let nl = buffer.firstIndex(of: 0x0A) {
                    let lineData = buffer.subdata(in: buffer.startIndex..<nl)
                    buffer.removeSubrange(buffer.startIndex...nl)
                    if let line = String(data: lineData, encoding: .utf8) {
                        continuation.yield(line)
                    }
                }
            }
            process.terminationHandler = { _ in
                // Finish the stream. `buffer` is deliberately NOT touched here —
                // it is owned by the readability queue, and the EOF read above
                // drains the final tail; racing it from this queue would be a
                // data race for a log-only line. Post-finish yields are ignored.
                handle.readabilityHandler = nil
                continuation.finish()
            }
            do {
                try process.run()
            } catch {
                continuation.yield("failed to launch: \(error.localizedDescription)")
                continuation.finish()
            }
        }
    }
}
