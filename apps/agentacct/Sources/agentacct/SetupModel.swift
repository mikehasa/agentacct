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
    private let installer: (() throws -> URL)?
    private let processRunner: (URL, [String]) -> AsyncThrowingStream<String, Error>

    init(
        installer: (() throws -> URL)? = nil,
        processRunner: @escaping (URL, [String]) -> AsyncThrowingStream<String, Error> = ProcessRunner.run
    ) {
        self.installer = installer
        self.processRunner = processRunner
    }

    /// Deterministic state injection for offscreen review renders. Live setup
    /// still uses the production initializer above and reaches these states
    /// only through `setUp()`.
    init(preloaded phase: Phase, log: [String]) {
        self.phase = phase
        self.log = Array(log.suffix(200))
        installer = nil
        processRunner = ProcessRunner.run
    }

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
            try Task.checkCancellation()
            let executable = try installer?() ?? installCLI()
            try Task.checkCancellation()
            try await runOnboard(executable: executable)
            try Task.checkCancellation()
            phase = .done
        } catch is CancellationError {
            phase = .idle
        } catch {
            phase = .failed(error.localizedDescription)
            append("error: \(error.localizedDescription)")
        }
    }

    func reset() { phase = .idle }

    // MARK: steps

    private func installCLI() throws -> URL {
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
        return installedBinary
    }

    private func runOnboard(executable: URL) async throws {
        phase = .working("Configuring your coding agents…")
        append("Running: agentacct onboard --agent auto --yes")
        // Run from the INSTALLED binary so onboard stamps the stable installed
        // path into every hook/MCP config (verified end-to-end).
        let stream = processRunner(executable, ["onboard", "--agent", "auto", "--yes"])
        for try await line in stream {
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
    /// stream succeeds only when the process exits with status zero.
    static func run(executable: URL, arguments: [String]) -> AsyncThrowingStream<String, Error> {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        // A frozen binary resolves its own paths; keep the user's environment
        // (HOME, PATH) so onboard writes to the real ~/.claude, ~/.codex.
        process.environment = ProcessInfo.processInfo.environment

        return AsyncThrowingStream<String, Error> { continuation in
            let handle = pipe.fileHandleForReading
            let state = ProcessStreamState(continuation: continuation)
            continuation.onTermination = { @Sendable termination in
                guard case .cancelled = termination else { return }
                handle.readabilityHandler = nil
                if process.isRunning {
                    process.terminate()
                }
            }
            handle.readabilityHandler = { fh in
                let chunk = fh.availableData
                state.receive(chunk)
                if chunk.isEmpty {
                    // Stop after EOF; leaving the handler installed would busy-spin
                    // on repeated empty reads.
                    fh.readabilityHandler = nil
                }
            }
            process.terminationHandler = { process in
                state.didTerminate(status: process.terminationStatus)
            }
            do {
                try process.run()
                // The child has its own descriptor after launch. Close the
                // parent's writer so the reader observes EOF after child exit.
                pipe.fileHandleForWriting.closeFile()
            } catch {
                handle.readabilityHandler = nil
                pipe.fileHandleForWriting.closeFile()
                state.didFailToLaunch(error)
            }
        }
    }
}

private final class ProcessStreamState: @unchecked Sendable {
    private let continuation: AsyncThrowingStream<String, Error>.Continuation
    private let lock = NSLock()
    private var buffer = Data()
    private var reachedEOF = false
    private var terminationStatus: Int32?
    private var finished = false

    init(continuation: AsyncThrowingStream<String, Error>.Continuation) {
        self.continuation = continuation
    }

    func receive(_ chunk: Data) {
        lock.lock()
        defer { lock.unlock() }
        guard !finished else { return }

        if chunk.isEmpty {
            if !buffer.isEmpty, let tail = String(data: buffer, encoding: .utf8) {
                continuation.yield(tail)
            }
            buffer.removeAll()
            reachedEOF = true
            finishIfReady()
            return
        }

        buffer.append(chunk)
        while let newline = buffer.firstIndex(of: 0x0A) {
            let lineData = buffer.subdata(in: buffer.startIndex..<newline)
            buffer.removeSubrange(buffer.startIndex...newline)
            if let line = String(data: lineData, encoding: .utf8) {
                continuation.yield(line)
            }
        }
    }

    func didTerminate(status: Int32) {
        lock.lock()
        defer { lock.unlock() }
        guard !finished else { return }
        terminationStatus = status
        finishIfReady()
    }

    func didFailToLaunch(_ error: Error) {
        lock.lock()
        defer { lock.unlock() }
        guard !finished else { return }
        finished = true
        continuation.finish(throwing: error)
    }

    private func finishIfReady() {
        guard reachedEOF, let terminationStatus else { return }
        finished = true
        if terminationStatus == 0 {
            continuation.finish()
        } else {
            continuation.finish(throwing: ProcessRunnerError.nonzeroExit(terminationStatus))
        }
    }
}

enum ProcessRunnerError: LocalizedError, Equatable {
    case nonzeroExit(Int32)

    var errorDescription: String? {
        switch self {
        case .nonzeroExit(let status):
            return "Recorder setup exited with status \(status)."
        }
    }
}
