import XCTest
@testable import agentacct

/// The external recorder path exists for one situation: a reboot left the
/// recorder down, and this app owns no recorder it can prove and start (a
/// development build, or a CLI installed with pipx/uv). These tests pin what it
/// will and will not do with a binary the app did not install.
final class ExternalRecorderCLITests: XCTestCase {
    // MARK: resolution (pure)

    func testCandidatesPreferTheDocumentedLauncherAndKeepPathOrder() {
        let home = URL(fileURLWithPath: "/Users/example", isDirectory: true)
        let candidates = ExternalRecorderCLIResolver.candidates(
            home: home,
            environment: ["PATH": "/opt/homebrew/bin:/usr/bin:/opt/homebrew/bin"]
        )

        XCTAssertEqual(candidates.map(\.path), [
            "/Users/example/.local/bin/agentacct",
            "/opt/homebrew/bin/agentacct",
            "/usr/bin/agentacct",
        ])
    }

    func testLaunchPointsFollowLinksButSkipAppOwnedAndBundledPaths() throws {
        let root = try makeTemporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let fm = FileManager.default
        let home = root.appendingPathComponent("home", isDirectory: true)
        let bin = home.appendingPathComponent(".local/bin", isDirectory: true)
        try fm.createDirectory(at: bin, withIntermediateDirectories: true)

        // A uv-style launcher: a symlink in ~/.local/bin to the tool's own bin.
        let uv = root.appendingPathComponent("uv/tools/agentacct/bin", isDirectory: true)
        try fm.createDirectory(at: uv, withIntermediateDirectories: true)
        let uvBinary = uv.appendingPathComponent("agentacct")
        try writeExecutable("#!/bin/sh\nexec python -m agentacct \"$@\"\n", to: uvBinary)
        let launcher = bin.appendingPathComponent("agentacct")
        try fm.createSymbolicLink(at: launcher, withDestinationURL: uvBinary)

        let bundled = root.appendingPathComponent("agentacct.app/Contents/Resources/cli", isDirectory: true)
        try fm.createDirectory(at: bundled, withIntermediateDirectories: true)
        try writeExecutable("#!/bin/sh\n", to: bundled.appendingPathComponent("agentacct"))

        let owned = home.appendingPathComponent(".local/share/agentacct/cli", isDirectory: true)
        try fm.createDirectory(at: owned, withIntermediateDirectories: true)
        try writeExecutable("#!/bin/sh\n", to: owned.appendingPathComponent("agentacct"))

        let launchPoints = ExternalRecorderCLIResolver.launchPoints(
            home: home,
            environment: ["PATH": "\(bundled.path):\(owned.path):\(root.path)"],
            fileManager: fm,
            bundleURL: root.appendingPathComponent("agentacct.app", isDirectory: true)
        )

        // Only the symlinked launcher survives: the app-owned location and the
        // app's own bundle are never candidates, and a PATH entry holding no
        // `agentacct` is dropped. The launcher still resolves to the real file.
        XCTAssertEqual(launchPoints.map(\.path), [launcher.path])
        XCTAssertEqual(launchPoints[0].resolvingSymlinksInPath().path, uvBinary.path)

        // A broken link or a plain directory is not a launch point either.
        try fm.removeItem(at: launcher)
        try fm.createDirectory(at: launcher, withIntermediateDirectories: true)
        XCTAssertTrue(ExternalRecorderCLIResolver.launchPoints(
            home: home,
            environment: ["PATH": ""],
            fileManager: fm,
            bundleURL: root.appendingPathComponent("agentacct.app", isDirectory: true)
        ).isEmpty)
    }

    func testVersionBannerAcceptsOnlyTheCLIsOwnBanner() {
        XCTAssertEqual(ExternalRecorderCLI.version(fromBanner: "agentacct 0.12.4"), "0.12.4")
        XCTAssertEqual(ExternalRecorderCLI.version(fromBanner: "  agentacct 1.0.0\n"), "1.0.0")
        XCTAssertNil(ExternalRecorderCLI.version(fromBanner: "agentacct"))
        XCTAssertNil(ExternalRecorderCLI.version(fromBanner: "agentacct 0.12"))
        XCTAssertNil(ExternalRecorderCLI.version(fromBanner: "agentacct 0.12.4-rc1"))
        XCTAssertNil(ExternalRecorderCLI.version(fromBanner: "python 3.13.0"))
    }

    func testExternalDetailNamesTheBinaryAndTheBoundary() {
        let cli = ExternalRecorderCLI(
            launcher: URL(fileURLWithPath: "/Users/example/.local/bin/agentacct"),
            executable: URL(fileURLWithPath: "/Users/example/uv/tools/agentacct/bin/agentacct"),
            version: "0.12.4",
            banner: "agentacct 0.12.4"
        )
        XCTAssertEqual(
            RecorderStartTarget.external(cli).detail,
            "Starts agentacct 0.12.4 at /Users/example/.local/bin/agentacct — not installed or managed by this app."
        )
        XCTAssertNil(RecorderStartTarget.appOwned.detail)
    }

    // MARK: starting (model)

    @MainActor
    func testStartRecorderRunsTheUserInstalledCLIInTheBackground() async throws {
        let env = try ExternalRecorderFixture()
        defer { env.remove() }
        let model = env.model()

        await model.resolveRecorderStartTargetIfNeeded()
        let resolved = try XCTUnwrap(model.externalRecorder)
        XCTAssertEqual(resolved.launcher.path, env.launcher.path)
        XCTAssertEqual(resolved.executable.path, env.uvBinary.path)
        XCTAssertEqual(resolved.version, "0.12.4")
        XCTAssertEqual(model.recorderStartTarget, .external(resolved))

        let started = await model.startRecorder()
        XCTAssertTrue(started)
        XCTAssertEqual(env.commands, [
            ["--version"],
            ["start", "--no-sync-clients", "--store-dir", env.store.path, "--json"],
            ["status", "--store-dir", env.store.path, "--json"],
        ])
        XCTAssertEqual(env.executables.map(\.path), [env.launcher.path, env.uvBinary.path, env.uvBinary.path])
        XCTAssertEqual(model.reconnectPhase, .done)
        XCTAssertNotNil(model.reconnectCompletedAt)
        XCTAssertTrue(model.reconnectLog.contains { $0.contains("agentacct 0.12.4") && $0.contains("does not manage") })
        // Nothing was installed, copied or written for this binary.
        XCTAssertTrue(env.writes.isEmpty)
    }

    @MainActor
    func testStartRecorderRefusesABinaryThatIsNotAnAgentacctCLI() async throws {
        let env = try ExternalRecorderFixture(banner: "python 3.13.0")
        defer { env.remove() }
        let model = env.model()

        let started = await model.startRecorder()
        XCTAssertFalse(started)
        XCTAssertNil(model.externalRecorder)
        XCTAssertNil(model.recorderStartTarget)
        XCTAssertNotNil(model.externalRecorderUnavailableReason)
        // One probe, and no recorder command at all.
        XCTAssertEqual(env.commands, [["--version"]])
    }

    @MainActor
    func testStartRecorderRefusesAnUnidentifiableDisplayStore() async throws {
        let env = try ExternalRecorderFixture()
        defer { env.remove() }
        let model = env.model(displayStore: { throw CocoaError(.fileNoSuchFile) })

        let started = await model.startRecorder()
        XCTAssertFalse(started)
        // Only the resolution probe ran; no recorder command was issued.
        XCTAssertEqual(env.commands, [["--version"]])
        XCTAssertEqual(model.reconnectPhase, .failed("The displayed recorder store could not be identified. Resolve the store configuration before starting a recorder."))
        XCTAssertNil(model.reconnectCompletedAt)
    }

    @MainActor
    func testStartRecorderWithoutAnyCandidateRunsNothing() async throws {
        let env = try ExternalRecorderFixture(installLauncher: false)
        defer { env.remove() }
        let model = env.model()

        let started = await model.startRecorder()
        XCTAssertFalse(started)
        XCTAssertTrue(env.commands.isEmpty)
        XCTAssertNil(model.recorderStartTarget)
    }

    @MainActor
    func testUnreadyRecorderStaysFailedAndCanBeRetried() async throws {
        var statusCalls = 0
        let env = try ExternalRecorderFixture()
        defer { env.remove() }
        let model = env.model(statusLines: { _ in
            statusCalls += 1
            return statusCalls == 1
                ? ["{\"state\":\"starting\",\"store_dir\":\"\(env.store.path)\"}"]
                : [env.readyRuntimeStatus]
        })

        let first = await model.startRecorder()
        XCTAssertFalse(first)
        XCTAssertEqual(model.reconnectPhase, .failed("The recorder start command finished, but the endpoint and watcher are not ready. Review the reconnect output and try again."))
        XCTAssertNil(model.reconnectCompletedAt)

        let second = await model.startRecorder()
        XCTAssertTrue(second)
        XCTAssertEqual(model.reconnectPhase, .done)
        XCTAssertNotNil(model.reconnectCompletedAt)
    }

    @MainActor
    func testFailedStartCommandIsSurfacedAndNeverClaimsSuccess() async throws {
        let env = try ExternalRecorderFixture(startFailure: ProcessRunnerError.nonzeroExit(1))
        defer { env.remove() }
        let model = env.model()

        let started = await model.startRecorder()
        XCTAssertFalse(started)
        guard case .failed(let message) = model.reconnectPhase else {
            return XCTFail("expected a failed start, got \(model.reconnectPhase)")
        }
        XCTAssertTrue(message.contains("status 1"))
        XCTAssertTrue(model.reconnectLog.contains { $0.hasPrefix("Start stopped:") })
        XCTAssertNil(model.reconnectCompletedAt)
    }

    @MainActor
    func testExplicitStartSucceedsWhereReconnectMustRefuse() async throws {
        let env = try ExternalRecorderFixture()
        defer { env.remove() }
        let model = env.model()

        // The app-owned reconnect stays refused: this build owns no recorder.
        let reconnected = await model.reconnectRecorder()
        XCTAssertFalse(reconnected)
        XCTAssertTrue(env.commands.isEmpty)

        // The explicit, labeled start runs the verified user-installed CLI.
        let started = await model.startRecorder()
        XCTAssertTrue(started)
        XCTAssertEqual(env.commands.first, ["--version"])
        XCTAssertEqual(model.reconnectPhase, .done)
    }

    // MARK: helpers

    private func makeTemporaryRoot() throws -> URL {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-external-cli-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        return root
    }

    private func writeExecutable(_ contents: String, to url: URL) throws {
        try contents.write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path)
    }
}

/// A temporary home whose `.local/bin/agentacct` is a uv-style symlink, plus a
/// recording process runner. Nothing on the host machine is touched.
@MainActor
private final class ExternalRecorderFixture {
    let root: URL
    let home: URL
    let launcher: URL
    let uvBinary: URL
    let store: URL
    private(set) var commands: [[String]] = []
    private(set) var executables: [URL] = []
    private(set) var writes: [String] = []
    private let banner: String
    private let startFailure: Error?

    var readyRuntimeStatus: String {
        "{\"state\":\"running\",\"store_dir\":\"\(store.path)\",\"dashboard_health\":\"healthy\",\"watcher\":\"running\",\"processes\":[{\"role\":\"dashboard\",\"state\":\"running\"}]}"
    }

    init(
        banner: String = "agentacct 0.12.4",
        installLauncher: Bool = true,
        startFailure: Error? = nil
    ) throws {
        let fm = FileManager.default
        self.banner = banner
        self.startFailure = startFailure
        root = fm.temporaryDirectory.appendingPathComponent("agentacct-external-start-\(UUID().uuidString)", isDirectory: true)
        home = root.appendingPathComponent("home", isDirectory: true)
        store = root.appendingPathComponent("state", isDirectory: true)
        let bin = home.appendingPathComponent(".local/bin", isDirectory: true)
        launcher = bin.appendingPathComponent("agentacct")
        uvBinary = root.appendingPathComponent("uv/tools/agentacct/bin/agentacct")
        try fm.createDirectory(at: bin, withIntermediateDirectories: true)
        try fm.createDirectory(at: uvBinary.deletingLastPathComponent(), withIntermediateDirectories: true)
        try fm.createDirectory(at: store, withIntermediateDirectories: true)
        try "#!/bin/sh\nexec python -m agentacct \"$@\"\n".write(to: uvBinary, atomically: true, encoding: .utf8)
        try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: uvBinary.path)
        if installLauncher {
            try fm.createSymbolicLink(at: launcher, withDestinationURL: uvBinary)
        }
    }

    func model(
        displayStore: (() throws -> URL)? = nil,
        statusLines: (([String]) -> [String])? = nil
    ) -> SetupModel {
        let statusSource = statusLines
        return SetupModel(
            processRunner: { [self] executable, arguments in
                commands.append(arguments)
                executables.append(executable)
                if arguments == ["--version"] {
                    return Self.stream(lines: [banner])
                }
                if arguments.first == "start" {
                    if let startFailure { return Self.stream(lines: [], failure: startFailure) }
                    return Self.stream(lines: ["agentacct runtime: running"])
                }
                if arguments.first == "status" {
                    return Self.stream(lines: statusSource?(arguments) ?? [readyRuntimeStatus])
                }
                return Self.stream(lines: [])
            },
            homeDirectory: home,
            bundleResourceURL: nil,
            bundleInfoDictionary: nil,
            storeDirectory: { [store] in store },
            displayStoreDirectory: displayStore ?? { [store] in store },
            environment: ["PATH": ""],
            copyDirectory: { [self] _, _ in writes.append("copy") },
            writeWrapper: { [self] _, _ in writes.append("wrapper") },
            readinessPause: {}
        )
    }

    func remove() {
        try? FileManager.default.removeItem(at: root)
    }

    private static func stream(
        lines: [String],
        failure: Error? = nil
    ) -> AsyncThrowingStream<String, Error> {
        AsyncThrowingStream { continuation in
            for line in lines { continuation.yield(line) }
            if let failure { continuation.finish(throwing: failure) } else { continuation.finish() }
        }
    }
}
