import XCTest
import Darwin
@testable import agentacct

final class SetupModelTests: XCTestCase {
    private let oldCommit = "1111111111111111111111111111111111111111"
    private let newCommit = "2222222222222222222222222222222222222222"

    func testProcessRunnerStreamsOutputThenThrowsWhenProcessExitsNonzero() async {
        let stream = ProcessRunner.run(
            executable: URL(fileURLWithPath: "/bin/sh"),
            arguments: ["-c", "printf 'configuration failed\\n'; exit 7"]
        )
        var lines: [String] = []
        var receivedError: Error?

        do {
            for try await line in stream { lines.append(line) }
        } catch {
            receivedError = error
        }

        XCTAssertEqual(lines, ["configuration failed"])
        XCTAssertEqual(receivedError as? ProcessRunnerError, .nonzeroExit(7))
    }

    func testProcessRunnerThrowsWhenLaunchFailsWithoutInventingLogOutput() async {
        let stream = ProcessRunner.run(
            executable: URL(fileURLWithPath: "/path/that/does/not/exist/agentacct"),
            arguments: []
        )
        var lines: [String] = []
        var receivedError: Error?

        do {
            for try await line in stream { lines.append(line) }
        } catch {
            receivedError = error
        }

        XCTAssertTrue(lines.isEmpty)
        XCTAssertNotNil(receivedError)
    }

    func testProcessRunnerStreamsOutputAndCompletesForZeroExit() async throws {
        let stream = ProcessRunner.run(
            executable: URL(fileURLWithPath: "/bin/sh"),
            arguments: ["-c", "printf 'configured'"]
        )
        var lines: [String] = []

        for try await line in stream { lines.append(line) }

        XCTAssertEqual(lines, ["configured"])
    }

    func testProcessRunnerTerminatesChildWhenConsumerIsCancelled() async throws {
        let fm = FileManager.default
        let directory = fm.temporaryDirectory
            .appendingPathComponent("agentacct-process-runner-\(UUID().uuidString)", isDirectory: true)
        try fm.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: directory) }
        let terminationMarker = directory.appendingPathComponent("terminated")
        let ready = expectation(description: "child process started")
        let script = """
        marker="$1"
        (sleep 4; kill -TERM $$) &
        watchdog=$!
        trap 'kill "$watchdog" 2>/dev/null; printf terminated > "$marker"; exit 0' TERM
        printf 'ready\\n'
        while :; do sleep 0.05; done
        """
        let stream = ProcessRunner.run(
            executable: URL(fileURLWithPath: "/bin/sh"),
            arguments: ["-c", script, "agentacct-process-runner", terminationMarker.path]
        )
        let consumer = Task {
            do {
                for try await line in stream where line == "ready" { ready.fulfill() }
            } catch {
                // Cancellation is asserted through the child-owned marker.
            }
        }

        await fulfillment(of: [ready], timeout: 2)
        consumer.cancel()
        await consumer.value

        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(2))
        while !fm.fileExists(atPath: terminationMarker.path), clock.now < deadline {
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertEqual(
            try? String(contentsOf: terminationMarker, encoding: .utf8),
            "terminated"
        )
    }

    @MainActor
    func testSetupModelFailsAfterStreamingNonzeroExit() async {
        let model = makeModel(lines: ["provider configured"], failure: ProcessRunnerError.nonzeroExit(9))

        await model.setUp()

        XCTAssertEqual(model.phase, .failed("Recorder setup exited with status 9."))
        XCTAssertEqual(model.log, [
            "Running: agentacct onboard --agent auto --yes",
            "provider configured",
            "error: Recorder setup exited with status 9.",
        ])
    }

    @MainActor
    func testSetupModelFailsWhenLaunchThrows() async {
        let model = makeModel(failure: StubError.launchFailed)

        await model.setUp()

        XCTAssertEqual(model.phase, .failed("The recorder could not be launched."))
    }

    @MainActor
    func testSetupModelCompletesOnlyAfterSuccessfulStream() async {
        let model = makeModel(lines: ["provider configured"])

        await model.setUp()

        XCTAssertEqual(model.phase, .done)
        XCTAssertEqual(model.log, ["Running: agentacct onboard --agent auto --yes", "provider configured"])
    }

    @MainActor
    func testSetupModelReturnsToIdleWhenProcessStreamIsCancelled() async {
        let model = makeModel(failure: CancellationError())

        await model.setUp()

        XCTAssertEqual(model.phase, .idle)
    }

    @MainActor
    func testAutomaticUpgradeRequiresPackagedProvenanceToMatchAppIdentity() throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit)
        defer { fixture.remove() }
        let model = fixture.model(infoCommit: oldCommit)

        XCTAssertNil(model.bundledCLIDir)
        XCTAssertFalse(model.shouldAutomaticallyUpgradeCLI)
    }

    @MainActor
    func testAutomaticUpgradeRefusesWhenAnotherAppInstanceHoldsTransactionLock() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit)
        defer { fixture.remove() }
        try FileManager.default.createDirectory(
            at: fixture.transactionLockFile.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        let descriptor = fixture.transactionLockFile.path.withCString { path in
            Darwin.open(path, O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, S_IRUSR | S_IWUSR)
        }
        XCTAssertGreaterThanOrEqual(descriptor, 0)
        XCTAssertEqual(flock(descriptor, LOCK_EX | LOCK_NB), 0)
        defer {
            _ = flock(descriptor, LOCK_UN)
            Darwin.close(descriptor)
        }
        var commands: [[String]] = []
        let model = fixture.model { _, arguments in
            commands.append(arguments)
            return Self.stream(lines: [])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(
            outcome,
            .failed("Another agentacct App window is already updating the recorder. Wait for it to finish, then try again.")
        )
        XCTAssertTrue(commands.isEmpty)
        XCTAssertEqual(fixture.installedCommit(), oldCommit)
        XCTAssertTrue(fixture.versionTargets.isEmpty)
    }

    @MainActor
    func testAutomaticUpgradeNeverDowngradesANewerInstalledCLI() async throws {
        let fixture = try UpgradeFixture(
            bundleCommit: oldCommit,
            bundleVersion: "0.10.0",
            installedCommit: newCommit,
            installedVersion: "0.11.0"
        )
        defer { fixture.remove() }
        var runtimeCommands: [[String]] = []
        let model = fixture.model { _, arguments in
            runtimeCommands.append(arguments)
            return Self.stream(lines: [])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(outcome, .notNeeded)
        XCTAssertTrue(runtimeCommands.isEmpty)
        XCTAssertEqual(fixture.installedCommit(), newCommit)
    }

    @MainActor
    func testAutomaticUpgradeDoesNotTakeOverStableCLIWithoutAppWrapper() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit)
        defer { fixture.remove() }
        try fixture.replaceWrapperWithUserManagedFile()
        var commands: [[String]] = []
        let model = fixture.model { _, arguments in
            commands.append(arguments)
            return Self.stream(lines: [])
        }

        XCTAssertFalse(model.shouldAutomaticallyUpgradeCLI)
        let outcome = await model.upgradeInstalledCLIIfNeeded()
        XCTAssertEqual(outcome, .notNeeded)
        XCTAssertTrue(commands.isEmpty)
        XCTAssertEqual(fixture.installedCommit(), oldCommit)
    }

    @MainActor
    func testVersionedUpgradeRepairsMissingOuterWrapperThenContinuesWithoutOnboarding() async throws {
        let fixture = try UpgradeFixture(
            bundleCommit: newCommit,
            installedCommit: oldCommit,
            installedAsVersioned: true
        )
        defer { fixture.remove() }
        try fixture.removeOuterWrapper()
        var commands: [String] = []
        let model = fixture.model { _, arguments in
            commands.append(arguments[0])
            if arguments[0] == "status" {
                return Self.stream(lines: ["{\"processes\":[]}"])
            }
            return Self.stream(lines: ["{}"])
        }

        XCTAssertTrue(model.shouldAutomaticallyUpgradeCLI)
        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(outcome, .upgraded)
        XCTAssertEqual(commands, ["status"])
        XCTAssertEqual(fixture.installedCommit(), newCommit)
        XCTAssertEqual(
            try String(contentsOf: fixture.wrapper, encoding: .utf8),
            fixture.outerWrapperContents
        )
    }

    @MainActor
    func testVersionedUpgradeRepairsMissingStableLauncherThenContinuesWithoutOnboarding() async throws {
        let fixture = try UpgradeFixture(
            bundleCommit: newCommit,
            installedCommit: oldCommit,
            installedAsVersioned: true
        )
        defer { fixture.remove() }
        try fixture.removeStableLauncher()
        var commands: [String] = []
        let model = fixture.model { _, arguments in
            commands.append(arguments[0])
            if arguments[0] == "status" {
                return Self.stream(lines: ["{\"processes\":[]}"])
            }
            return Self.stream(lines: ["{}"])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(outcome, .upgraded)
        XCTAssertEqual(commands, ["status"])
        XCTAssertEqual(fixture.installedCommit(), newCommit)
        XCTAssertEqual(try fixture.stableLauncher(), fixture.expectedStableLauncherContents)
    }

    @MainActor
    func testMissingStableLauncherRepairPreservesCompetingDestinationWithoutRunningCommand() async throws {
        let fixture = try UpgradeFixture(
            bundleCommit: newCommit,
            installedCommit: oldCommit,
            installedAsVersioned: true
        )
        defer { fixture.remove() }
        try fixture.removeStableLauncher()
        let competingContents = "#!/bin/sh\necho user-won-race\n"
        var commands: [[String]] = []
        let model = fixture.model(
            processRunner: { _, arguments in
                commands.append(arguments)
                return Self.stream(lines: [])
            },
            transactionLockObserver: {
                try! competingContents.write(
                    to: fixture.installedBinary,
                    atomically: true,
                    encoding: .utf8
                )
            }
        )

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(outcome, .notNeeded)
        XCTAssertTrue(commands.isEmpty)
        XCTAssertEqual(
            try String(contentsOf: fixture.installedBinary, encoding: .utf8),
            competingContents
        )
        XCTAssertEqual(fixture.installedCommit(), oldCommit)
    }

    @MainActor
    func testCurrentVersionedInstallRepairsMissingWrapperWithoutOnboarding() async throws {
        let fixture = try UpgradeFixture(
            bundleCommit: newCommit,
            installedCommit: newCommit,
            installedVersion: "0.11.0",
            installedAsVersioned: true
        )
        defer { fixture.remove() }
        try fixture.removeOuterWrapper()
        var commands: [[String]] = []
        let model = fixture.model { _, arguments in
            commands.append(arguments)
            return Self.stream(lines: [])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(outcome, .notNeeded)
        XCTAssertTrue(commands.isEmpty)
        XCTAssertEqual(
            try String(contentsOf: fixture.wrapper, encoding: .utf8),
            fixture.outerWrapperContents
        )
        XCTAssertFalse(commands.contains(["onboard", "--agent", "auto", "--yes"]))
    }

    @MainActor
    func testOlderVersionedInstallWithBothLaunchersMissingFailsClosed() async throws {
        let fixture = try UpgradeFixture(
            bundleCommit: newCommit,
            installedCommit: oldCommit,
            installedAsVersioned: true
        )
        defer { fixture.remove() }
        try fixture.removeStableLauncherAndOuterWrapper()
        var commands: [[String]] = []
        let model = fixture.model { _, arguments in
            commands.append(arguments)
            return Self.stream(lines: [])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(
            outcome,
            .failed("The app-managed recorder target is incomplete or changed, so no recorder command was run. Reinstall agentacct before reopening the App.")
        )
        XCTAssertTrue(commands.isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.wrapper.path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.installedBinary.path))
        XCTAssertEqual(fixture.installedCommit(), oldCommit)
    }

    @MainActor
    func testCurrentTargetWithLocallyRestampedDifferentPayloadAndNoLaunchersFailsClosed() async throws {
        let fixture = try UpgradeFixture(
            bundleCommit: newCommit,
            installedCommit: newCommit,
            installedVersion: "0.11.0",
            installedAsVersioned: true
        )
        defer { fixture.remove() }
        try fixture.mutateSelectedSideFileWithoutChangingSize()
        try fixture.restampSelectedPayloadIdentity()
        try fixture.removeStableLauncherAndOuterWrapper()
        var commands: [[String]] = []
        let model = fixture.model { _, arguments in
            commands.append(arguments)
            return Self.stream(lines: [])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(
            outcome,
            .failed("The app-managed recorder target is incomplete or changed, so no recorder command was run. Reinstall agentacct before reopening the App.")
        )
        XCTAssertTrue(commands.isEmpty)
        XCTAssertEqual(fixture.installedCommit(), newCommit)
    }

    @MainActor
    func testAutomaticUpgradeRefusesUnknownAutostartBeforeAnyMutationOrCommand() async throws {
        let fixture = try UpgradeFixture(
            bundleCommit: newCommit,
            installedCommit: oldCommit,
            installedAsVersioned: true
        )
        defer { fixture.remove() }
        try fixture.installUnknownAutostartFile()
        let oldTarget = fixture.selectedTarget
        var commands: [[String]] = []
        var copied = false
        let model = fixture.model(
            processRunner: { _, arguments in
                commands.append(arguments)
                return Self.stream(lines: [])
            },
            copyDirectory: { _, _ in copied = true }
        )

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(
            outcome,
            .failed("The agentacct autostart configuration changed or is not app-managed, so the recorder update paused. Run `agentacct uninstall-autostart`, reopen the App, then run `agentacct install-autostart` again.")
        )
        XCTAssertTrue(commands.isEmpty)
        XCTAssertFalse(copied)
        XCTAssertEqual(fixture.selectedTarget, oldTarget)
        XCTAssertFalse(fixture.runtimeTransactionExists)
        XCTAssertEqual(fixture.versionTargets.count, 1)
    }

    @MainActor
    func testManagedAutostartRefusesLoadedDescriptorMismatchAndUnrecognizedMissingServiceErrors() async throws {
        for variant in ["argv", "program", "path", "duplicate", "nested-spoof", "wrong-domain", "wrong-missing-label"] {
            let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit, installedAsVersioned: true)
            defer { fixture.remove() }
            try fixture.installManagedAutostartFile()
            let arguments = try fixture.autostartArguments()
            var output = fixture.launchdPrintOutput(arguments: arguments)
            switch variant {
            case "argv": output = fixture.launchdPrintOutput(arguments: ["/tmp/other"] + Array(arguments.dropFirst()))
            case "program": output = output.replacingOccurrences(of: "\tprogram = \(fixture.installedBinary.path)", with: "\tprogram = /tmp/other")
            case "path": output = output.replacingOccurrences(of: "\tpath = \(fixture.autostartFile.path)", with: "\tpath = /tmp/other.plist")
            case "duplicate": output = output.replacingOccurrences(of: "\ttype = LaunchAgent", with: "\tpath = \(fixture.autostartFile.path)\n\ttype = LaunchAgent")
            case "nested-spoof": output = output.replacingOccurrences(of: "\tprogram =", with: "\tenvironment = {\n\tprogram =")
            default: break
            }
            fixture.launchdPrintOverride = {
                if variant == "wrong-domain" { return Self.stream(lines: ["Could not find domain"], failure: ProcessRunnerError.nonzeroExit(112)) }
                if variant == "wrong-missing-label" { return Self.stream(lines: ["Bad request.\nCould not find service \"another.service\" in domain for user gui: \(Darwin.geteuid())"], failure: ProcessRunnerError.nonzeroExit(113)) }
                return Self.stream(lines: [output])
            }
            var calls: [String] = []
            var copied = false
            let model = fixture.model(processRunner: { _, arguments in
                calls.append(arguments[0])
                return Self.stream(lines: [])
            }, copyDirectory: { _, _ in copied = true })

            guard case .failed = await model.upgradeInstalledCLIIfNeeded() else { return XCTFail("expected loaded descriptor refusal: \(variant)") }

            XCTAssertFalse(copied, variant)
            XCTAssertTrue(calls.isEmpty, variant)
            XCTAssertFalse(fixture.runtimeTransactionExists, variant)
            XCTAssertEqual(fixture.installedCommit(), oldCommit, variant)
        }
    }

    @MainActor
    func testManagedAutostartReadinessRejectsConcurrentlyReloadedOriginalDirectTarget() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit, installedAsVersioned: true)
        defer { fixture.remove() }
        let oldTarget = try XCTUnwrap(fixture.selectedTarget)
        try fixture.installManagedAutostartFile(executable: oldTarget.appendingPathComponent("agentacct"))
        let originalArguments = try fixture.autostartArguments()
        let model = fixture.model { _, arguments in
            if arguments[0] == "status" { return Self.stream(lines: ["{\"processes\":[]}"]) }
            if arguments[0] == "bootstrap", fixture.installedCommit() == self.newCommit {
                // Simulate another client loading the old descriptor while
                // our plist on disk already names the updated stable launcher.
                fixture.simulateLoadedLaunchdArguments(originalArguments)
            }
            return Self.stream(lines: [])
        }

        guard case .failed = await model.upgradeInstalledCLIIfNeeded() else { return XCTFail("expected old supervisor refusal") }

        XCTAssertEqual(fixture.selectedTarget, oldTarget)
        XCTAssertEqual(try fixture.autostartArguments(), originalArguments)
        XCTAssertEqual(fixture.autostartReadinessChecks, 1, "only restored old runtime may reach readiness check")
        XCTAssertFalse(fixture.runtimeTransactionExists)
    }

    @MainActor
    func testManagedAutostartRequiresRunningSupervisorAndHealthyRuntimeBeforeClearingJournal() async throws {
        for failure in ["supervisor", "api", "watcher", "store"] {
            let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit, installedAsVersioned: true)
            defer { fixture.remove() }
            try fixture.installManagedAutostartFile()
            fixture.autostartReadinessOverride = {
                XCTAssertTrue(fixture.runtimeTransactionExists)
                guard fixture.installedCommit() == self.newCommit else { return fixture.readyRuntimeStatus }
                switch failure {
                case "api": return fixture.readyRuntimeStatus.replacingOccurrences(of: "healthy", with: "unhealthy")
                case "watcher": return fixture.readyRuntimeStatus.replacingOccurrences(of: "external", with: "stopped")
                case "store": return fixture.readyRuntimeStatus.replacingOccurrences(of: fixture.store.path, with: "/tmp/other-store")
                default: return fixture.readyRuntimeStatus
                }
            }
            var calls: [String] = []
            let model = fixture.model { _, arguments in
                let command = arguments[0]
                calls.append("\(command):\(fixture.installedCommit() ?? "missing")")
                if command == "status" { return Self.stream(lines: ["{\"processes\":[]}"]) }
                if command == "bootstrap", failure == "supervisor" {
                    fixture.simulatedLaunchdRunning = fixture.installedCommit() == self.oldCommit
                }
                return Self.stream(lines: [])
            }

            guard case .failed(let message) = await model.upgradeInstalledCLIIfNeeded() else { return XCTFail("expected readiness failure: \(failure)") }

            XCTAssertTrue(message.contains("did not become ready"), failure)
            XCTAssertEqual(fixture.installedCommit(), oldCommit, failure)
            XCTAssertFalse(fixture.runtimeTransactionExists, "journal clears only after old recorder is verified healthy")
            XCTAssertTrue(calls.contains("bootstrap:\(oldCommit)"), failure)
            XCTAssertEqual(fixture.autostartReadinessChecks, failure == "supervisor" ? 1 : 21, failure)
        }
    }

    @MainActor
    func testManagedAutostartUpgradeBootsOutBeforeStopAndMigratesDirectTargetPreservingSettings() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit, installedAsVersioned: true)
        defer { fixture.remove() }
        let oldTarget = try XCTUnwrap(fixture.selectedTarget)
        try fixture.installManagedAutostartFile(executable: oldTarget.appendingPathComponent("agentacct"), host: "::1", port: 9987)
        var calls: [String] = []
        let model = fixture.model { executable, arguments in
            let command = arguments[0]
            calls.append(command)
            if command == "status" { return Self.stream(lines: ["{\"processes\":[{\"state\":\"running\"}]}"]) }
            if command == "bootout" {
                XCTAssertEqual(executable.path, "/bin/launchctl")
                XCTAssertTrue(fixture.runtimeTransactionExists, "journal must precede unloading the supervisor")
                XCTAssertEqual(fixture.installedCommit(), self.oldCommit)
            }
            if command == "bootstrap" {
                XCTAssertEqual(fixture.installedCommit(), self.newCommit)
                XCTAssertEqual(try? fixture.autostartArguments(), [fixture.installedBinary.path, "start", "--foreground", "--store-dir", fixture.store.path, "--host", "::1", "--port", "9987"])
            }
            return Self.stream(lines: [])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(outcome, .upgraded)
        XCTAssertEqual(calls, ["status", "bootout", "stop", "bootstrap"])
        XCTAssertFalse(fixture.runtimeTransactionExists)
        XCTAssertEqual(fixture.installedCommit(), newCommit)
    }

    @MainActor
    func testManagedAutostartUpgradeBootstrapsEnabledSupervisorWhenChildrenAreStopped() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit)
        defer { fixture.remove() }
        try fixture.installManagedAutostartFile()
        var calls: [String] = []
        let model = fixture.model { _, arguments in
            calls.append(arguments[0])
            if arguments[0] == "status" { return Self.stream(lines: ["{\"processes\":[]}"]) }
            if arguments[0] == "bootout" { return Self.stream(lines: [], failure: ProcessRunnerError.nonzeroExit(3)) }
            return Self.stream(lines: [])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(outcome, .upgraded)
        XCTAssertEqual(calls, ["status", "bootout", "stop", "bootstrap"])
        XCTAssertFalse(fixture.runtimeTransactionExists)
    }

    @MainActor
    func testManagedAutostartBootstrapFailureRollsBackCLIAndOriginalPlist() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit, installedAsVersioned: true)
        defer { fixture.remove() }
        let oldTarget = try XCTUnwrap(fixture.selectedTarget)
        try fixture.installManagedAutostartFile(executable: oldTarget.appendingPathComponent("agentacct"))
        let original = try Data(contentsOf: fixture.autostartFile)
        var calls: [String] = []
        let model = fixture.model { _, arguments in
            let command = arguments[0]
            let commit = fixture.installedCommit() ?? "missing"
            calls.append("\(command):\(commit)")
            if command == "status" { return Self.stream(lines: ["{\"processes\":[{\"state\":\"running\"}]}"]) }
            if command == "bootstrap", commit == self.newCommit { return Self.stream(lines: [], failure: StubError.startFailed) }
            return Self.stream(lines: [])
        }

        guard case .failed = await model.upgradeInstalledCLIIfNeeded() else { return XCTFail("expected rollback") }

        XCTAssertEqual(fixture.selectedTarget, oldTarget)
        XCTAssertEqual(try Data(contentsOf: fixture.autostartFile), original)
        XCTAssertFalse(fixture.runtimeTransactionExists)
        XCTAssertEqual(calls, ["status:\(oldCommit)", "bootout:\(oldCommit)", "stop:\(oldCommit)", "bootstrap:\(newCommit)", "bootout:\(newCommit)", "stop:\(newCommit)", "bootstrap:\(oldCommit)"])
    }

    @MainActor
    func testManagedAutostartBootoutFailurePreservesJournalAndRecoversOnNextAppLaunch() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit, installedAsVersioned: true)
        defer { fixture.remove() }
        try fixture.installManagedAutostartFile()
        var initialCalls: [String] = []
        let initial = fixture.model { _, arguments in
            initialCalls.append(arguments[0])
            if arguments[0] == "status" { return Self.stream(lines: ["{\"processes\":[]}"]) }
            return Self.stream(lines: [], failure: ProcessRunnerError.nonzeroExit(5))
        }
        guard case .failed = await initial.upgradeInstalledCLIIfNeeded() else { return XCTFail("expected bootout failure") }
        XCTAssertEqual(initialCalls, ["status", "bootout", "bootout"])
        XCTAssertEqual(fixture.installedCommit(), oldCommit)
        XCTAssertTrue(fixture.runtimeTransactionExists)
        XCTAssertEqual(fixture.versionTargets.count, 2, "journal's prepared target must remain recoverable")
        var recoveredCalls: [String] = []
        let reopened = fixture.model { _, arguments in
            recoveredCalls.append(arguments[0])
            if arguments[0] == "bootout" { return Self.stream(lines: [], failure: ProcessRunnerError.nonzeroExit(3)) }
            return Self.stream(lines: [])
        }

        let outcome = await reopened.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(outcome, .upgraded)
        XCTAssertEqual(recoveredCalls, ["bootout", "stop", "bootstrap"])
        XCTAssertFalse(fixture.runtimeTransactionExists)
        XCTAssertEqual(fixture.installedCommit(), newCommit)
    }

    @MainActor
    func testManagedAutostartCrashRecoveryResumesSwitchedTargetWithOriginalOrMigratedPlist() async throws {
        for leaveOriginalPlist in [false, true] {
            let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit, installedAsVersioned: true)
            defer { fixture.remove() }
            let oldTarget = try XCTUnwrap(fixture.selectedTarget)
            try fixture.installManagedAutostartFile(executable: oldTarget.appendingPathComponent("agentacct"), port: 9987)
            let original = try Data(contentsOf: fixture.autostartFile)
            let initial = fixture.model { _, arguments in
                if arguments[0] == "status" { return Self.stream(lines: ["{\"processes\":[]}"]) }
                if arguments[0] == "bootstrap" {
                    if leaveOriginalPlist { try? original.write(to: fixture.autostartFile) }
                    return Self.stream(lines: [], failure: StubError.startFailed)
                }
                if arguments[0] == "bootout", fixture.installedCommit() == self.newCommit {
                    return Self.stream(lines: [], failure: ProcessRunnerError.nonzeroExit(5))
                }
                return Self.stream(lines: [])
            }
            guard case .failed = await initial.upgradeInstalledCLIIfNeeded() else { return XCTFail("expected interrupted supervisor recovery") }
            XCTAssertEqual(fixture.installedCommit(), newCommit)
            XCTAssertTrue(fixture.runtimeTransactionExists)
            XCTAssertEqual(try fixture.autostartArguments().first, leaveOriginalPlist ? oldTarget.appendingPathComponent("agentacct").path : fixture.installedBinary.path)
            var calls: [String] = []
            let reopened = fixture.model { _, arguments in
                calls.append(arguments[0])
                return Self.stream(lines: [])
            }

            let outcome = await reopened.upgradeInstalledCLIIfNeeded()

            XCTAssertEqual(outcome, .upgraded)
            XCTAssertEqual(calls, ["bootout", "stop", "bootstrap"])
            XCTAssertFalse(fixture.runtimeTransactionExists)
            XCTAssertEqual(try fixture.autostartArguments(), [fixture.installedBinary.path, "start", "--foreground", "--store-dir", fixture.store.path, "--port", "9987"])
        }
    }

    @MainActor
    func testManagedAutostartChangedDuringStopIsNeverReloadedOrOverwritten() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit, installedAsVersioned: true)
        defer { fixture.remove() }
        try fixture.installManagedAutostartFile()
        let oldTarget = fixture.selectedTarget
        var calls: [String] = []
        let model = fixture.model { _, arguments in
            calls.append(arguments[0])
            if arguments[0] == "status" { return Self.stream(lines: ["{\"processes\":[]}"]) }
            if arguments[0] == "bootout" { try? fixture.installUnknownAutostartFile() }
            return Self.stream(lines: [])
        }

        guard case .failed = await model.upgradeInstalledCLIIfNeeded() else { return XCTFail("expected changed configuration refusal") }

        XCTAssertEqual(calls, ["status", "bootout"])
        XCTAssertEqual(fixture.selectedTarget, oldTarget)
        XCTAssertEqual(try String(contentsOf: fixture.autostartFile, encoding: .utf8), "managed")
        XCTAssertTrue(fixture.runtimeTransactionExists)
        var recoveryCalls: [String] = []
        let reopened = fixture.model { _, arguments in
            recoveryCalls.append(arguments[0])
            return Self.stream(lines: [])
        }
        guard case .failed = await reopened.upgradeInstalledCLIIfNeeded() else { return XCTFail("expected recovery refusal") }
        XCTAssertTrue(recoveryCalls.isEmpty)
    }

    @MainActor
    func testManagedAutostartRejectsUnknownExecutableStoreAndUnsafePermissions() async throws {
        for variant in ["executable", "store", "permissions", "extra-key"] {
            let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit, installedAsVersioned: true)
            defer { fixture.remove() }
            try fixture.installManagedAutostartFile(executable: variant == "executable" ? URL(fileURLWithPath: "/tmp/unowned-cli") : nil)
            if variant == "permissions" {
                try FileManager.default.setAttributes([.posixPermissions: 0o666], ofItemAtPath: fixture.autostartFile.path)
            }
            if variant == "extra-key" {
                var document = try XCTUnwrap(PropertyListSerialization.propertyList(from: Data(contentsOf: fixture.autostartFile), format: nil) as? [String: Any])
                document["EnvironmentVariables"] = ["PYTHONPATH": "/tmp/unknown"]
                try PropertyListSerialization.data(fromPropertyList: document, format: .xml, options: 0).write(to: fixture.autostartFile)
            }
            var calls: [String] = []
            let model = fixture.model(processRunner: { _, arguments in
                calls.append(arguments[0])
                return Self.stream(lines: [])
            }, storeDirectory: { variant == "store" ? fixture.store.appendingPathComponent("other") : fixture.store })

            guard case .failed = await model.upgradeInstalledCLIIfNeeded() else { return XCTFail("expected refusal: \(variant)") }
            XCTAssertTrue(calls.isEmpty, variant)
            XCTAssertFalse(fixture.runtimeTransactionExists, variant)
            XCTAssertEqual(fixture.versionTargets.count, 1, variant)
        }
    }

    @MainActor
    func testVersionedPayloadIdentityRejectsSameSizeExecutableMutationBeforeAnyCommand() async throws {
        let fixture = try UpgradeFixture(
            bundleCommit: newCommit,
            installedCommit: oldCommit,
            installedAsVersioned: true
        )
        defer { fixture.remove() }
        let oldTarget = try XCTUnwrap(fixture.selectedTarget)
        let originalIdentity = try fixture.selectedExecutableIdentity()
        try fixture.mutateSelectedExecutableWithoutChangingSize()
        XCTAssertEqual(try fixture.selectedExecutableIdentity(), originalIdentity)
        var commands: [[String]] = []
        let model = fixture.model { _, arguments in
            commands.append(arguments)
            return Self.stream(lines: [])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(
            outcome,
            .failed("The app-managed recorder target is incomplete or changed, so no recorder command was run. Reinstall agentacct before reopening the App.")
        )
        XCTAssertTrue(commands.isEmpty)
        XCTAssertEqual(fixture.selectedTarget, oldTarget)
        XCTAssertEqual(fixture.versionTargets.count, 1)
        XCTAssertFalse(fixture.runtimeTransactionExists)
    }

    @MainActor
    func testVersionedPayloadIdentityRejectsInternalSideFileMutationBeforeAnyCommand() async throws {
        let fixture = try UpgradeFixture(
            bundleCommit: newCommit,
            installedCommit: oldCommit,
            installedAsVersioned: true
        )
        defer { fixture.remove() }
        let oldTarget = try XCTUnwrap(fixture.selectedTarget)
        let originalIdentity = try fixture.selectedSideFileIdentity()
        try fixture.mutateSelectedSideFileWithoutChangingSize()
        XCTAssertEqual(try fixture.selectedSideFileIdentity(), originalIdentity)
        var commands: [[String]] = []
        let model = fixture.model { _, arguments in
            commands.append(arguments)
            return Self.stream(lines: [])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(
            outcome,
            .failed("The app-managed recorder target is incomplete or changed, so no recorder command was run. Reinstall agentacct before reopening the App.")
        )
        XCTAssertTrue(commands.isEmpty)
        XCTAssertEqual(fixture.selectedTarget, oldTarget)
        XCTAssertEqual(fixture.versionTargets.count, 1)
        XCTAssertFalse(fixture.runtimeTransactionExists)
    }

    @MainActor
    func testVersionedPayloadIdentityRejectsSharedWritableTargetRootBeforeAnyCommand() async throws {
        let fixture = try UpgradeFixture(
            bundleCommit: newCommit,
            installedCommit: oldCommit,
            installedAsVersioned: true
        )
        defer { fixture.remove() }
        let oldTarget = try XCTUnwrap(fixture.selectedTarget)
        try fixture.makeSelectedTargetSharedWritable()
        var commands: [[String]] = []
        let model = fixture.model { _, arguments in
            commands.append(arguments)
            return Self.stream(lines: [])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(
            outcome,
            .failed("The app-managed recorder target is incomplete or changed, so no recorder command was run. Reinstall agentacct before reopening the App.")
        )
        XCTAssertTrue(commands.isEmpty)
        XCTAssertEqual(fixture.selectedTarget, oldTarget)
        XCTAssertFalse(fixture.runtimeTransactionExists)
    }

    @MainActor
    func testVersionedPayloadIdentityRejectsSharedWritableVersionsRootBeforeAnyCommand() async throws {
        let fixture = try UpgradeFixture(
            bundleCommit: newCommit,
            installedCommit: oldCommit,
            installedAsVersioned: true
        )
        defer { fixture.remove() }
        let oldTarget = try XCTUnwrap(fixture.selectedTarget)
        try fixture.makeVersionsDirectorySharedWritable()
        var commands: [[String]] = []
        let model = fixture.model { _, arguments in
            commands.append(arguments)
            return Self.stream(lines: [])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(
            outcome,
            .failed("The app-managed recorder target is incomplete or changed, so no recorder command was run. Reinstall agentacct before reopening the App.")
        )
        XCTAssertTrue(commands.isEmpty)
        XCTAssertEqual(fixture.selectedTarget, oldTarget)
        XCTAssertFalse(fixture.runtimeTransactionExists)
    }

    @MainActor
    func testSameProvenanceWithIntactLaunchersRepairsRestampedPayloadDrift() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: newCommit, installedVersion: "0.11.0", installedAsVersioned: true)
        defer { fixture.remove() }
        let oldTarget = fixture.selectedTarget
        try fixture.mutateSelectedSideFileWithoutChangingSize()
        try fixture.restampSelectedPayloadIdentity()
        var calls: [String] = []
        let model = fixture.model { _, arguments in
            calls.append(arguments[0])
            return Self.stream(lines: ["{\"processes\":[]}"])
        }

        XCTAssertTrue(model.shouldAutomaticallyUpgradeCLI)
        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(outcome, .upgraded)
        XCTAssertNotEqual(fixture.selectedTarget, oldTarget)
        XCTAssertEqual(calls, ["status"])
        XCTAssertEqual(try String(contentsOf: XCTUnwrap(fixture.selectedTarget).appendingPathComponent("_internal/side-file"), encoding: .utf8), "new-side-files")
    }

    @MainActor
    func testVersionedInstallRejectsSharedWritableStableDirectoryBinaryWrapperAndBin() async throws {
        for name in ["root", "binary", "wrapper", "bin", "marker"] {
            let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit, installedAsVersioned: true)
            defer { fixture.remove() }
            let target = fixture.selectedTarget
            let path: URL
            switch name {
            case "root": path = fixture.installedDirectory
            case "binary": path = fixture.installedBinary
            case "wrapper": path = fixture.wrapper
            case "bin": path = fixture.binDirectory
            default: path = fixture.targetMarker
            }
            try FileManager.default.setAttributes([.posixPermissions: 0o777], ofItemAtPath: path.path)
            var calls: [String] = []
            let model = fixture.model { _, arguments in
                calls.append(arguments[0])
                return Self.stream(lines: [])
            }
            guard case .failed = await model.upgradeInstalledCLIIfNeeded() else { return XCTFail("expected permissions failure: \(name)") }
            XCTAssertTrue(calls.isEmpty, name)
            XCTAssertEqual(fixture.selectedTarget, target, name)
            XCTAssertFalse(fixture.runtimeTransactionExists, name)
        }
    }

    @MainActor
    func testAutomaticUpgradeStagesBeforeStoppingAndPreservesOldCLIWhenCopyFails() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit)
        defer { fixture.remove() }
        var commands: [[String]] = []
        let model = fixture.model(
            processRunner: { _, arguments in
                commands.append(arguments)
                return Self.stream(lines: [])
            },
            copyDirectory: { _, _ in throw StubError.copyFailed }
        )

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(outcome, .failed("The recorder copy failed."))
        XCTAssertTrue(commands.isEmpty)
        XCTAssertEqual(fixture.installedCommit(), oldCommit)
        XCTAssertEqual(try fixture.legacySideFile(), "old-side-files")
    }

    @MainActor
    func testStoppedLegacyUpgradeUsesVersionedTargetWithoutMovingLegacySideFilesOrScanningProcesses() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit)
        defer { fixture.remove() }
        var calls: [(String, String)] = []
        let model = fixture.model { executable, arguments in
            XCTAssertNotEqual(executable.path, "/bin/ps")
            calls.append((arguments[0], fixture.installedCommit() ?? "missing"))
            return Self.stream(lines: ["{\"processes\":[]}"])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(outcome, .upgraded)
        XCTAssertEqual(calls.map(\.0), ["status"])
        XCTAssertEqual(fixture.installedCommit(), newCommit)
        XCTAssertEqual(try fixture.legacySideFile(), "old-side-files")
        XCTAssertTrue(fixture.hasLegacyBinaryBackup)
        XCTAssertEqual(fixture.versionTargets.count, 1)
        XCTAssertTrue(try fixture.stableLauncher().contains("PATH='\(fixture.binDirectory.path)'"))
        XCTAssertFalse(try fixture.stableLauncher().contains("${PATH:-}\""))
    }

    @MainActor
    func testAutomaticUpgradeSupportsLegacyAppInstallWithoutProvenanceStamp() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit)
        defer { fixture.remove() }
        try fixture.removeInstalledProvenance()
        let model = fixture.model { _, _ in Self.stream(lines: ["{\"processes\":[]}"]) }

        XCTAssertTrue(model.shouldAutomaticallyUpgradeCLI)
        let outcome = await model.upgradeInstalledCLIIfNeeded()
        XCTAssertEqual(outcome, .upgraded)
        XCTAssertEqual(fixture.installedCommit(), newCommit)
    }

    @MainActor
    func testAutomaticUpgradeStopsOldRuntimeAndStartsNewRuntimeUsingResolvedStore() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit)
        defer { fixture.remove() }
        var calls: [(String, String, [String])] = []
        let model = fixture.model { _, arguments in
            let command = arguments[0]
            calls.append((command, fixture.installedCommit() ?? "missing", arguments))
            if command == "status" {
                return Self.stream(lines: ["{\"processes\":[{\"state\":\"running\"}]}"])
            }
            return Self.stream(lines: ["{}"])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()
        XCTAssertEqual(outcome, .upgraded)
        XCTAssertEqual(calls.map { "\($0.0):\($0.1)" }, [
            "status:\(oldCommit)", "stop:\(oldCommit)", "start:\(newCommit)",
        ])
        XCTAssertTrue(calls.allSatisfy { $0.2 == [$0.0, "--store-dir", fixture.store.path, "--json"] })
    }

    @MainActor
    func testVersionedUpgradeAtomicallyRetargetsAndRetainsPreviousVersion() async throws {
        let fixture = try UpgradeFixture(
            bundleCommit: newCommit,
            installedCommit: oldCommit,
            installedAsVersioned: true
        )
        defer { fixture.remove() }
        let oldTarget = try XCTUnwrap(fixture.selectedTarget)
        var launcherWrites = 0
        let model = fixture.model(
            processRunner: { _, _ in Self.stream(lines: ["{\"processes\":[]}"]) },
            writeWrapper: { contents, destination in
                launcherWrites += 1
                try contents.write(to: destination, atomically: true, encoding: .utf8)
            }
        )

        let outcome = await model.upgradeInstalledCLIIfNeeded()
        XCTAssertEqual(outcome, .upgraded)
        XCTAssertEqual(fixture.installedCommit(), newCommit)
        XCTAssertTrue(FileManager.default.fileExists(atPath: oldTarget.path))
        XCTAssertEqual(fixture.versionTargets.count, 2)
        XCTAssertEqual(launcherWrites, 0, "a versioned upgrade changes only the target marker")
    }

    @MainActor
    func testOwnershipChangeAfterStopNeverRunsChangedStableBinary() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit)
        defer { fixture.remove() }
        var calls: [String] = []
        let model = fixture.model { _, arguments in
            let command = arguments[0]
            calls.append(command)
            if command == "status" {
                return Self.stream(lines: ["{\"processes\":[{\"state\":\"running\"}]}"])
            }
            if command == "stop" { try? fixture.replaceWrapperWithUserManagedFile() }
            return Self.stream(lines: ["{}"])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        guard case .failed(let message) = outcome else {
            return XCTFail("changed ownership after stop must fail closed")
        }
        XCTAssertTrue(message.contains("no unknown binary was run"))
        XCTAssertEqual(calls, ["status", "stop"])
        XCTAssertEqual(fixture.installedCommit(), oldCommit)
    }

    @MainActor
    func testNewStartFailureRollsBackMarkerRestartsOldRuntimeAndRetainsNewTarget() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit)
        defer { fixture.remove() }
        var calls: [String] = []
        let model = fixture.model { _, arguments in
            let command = arguments[0]
            let commit = fixture.installedCommit() ?? "missing"
            calls.append("\(command):\(commit)")
            if command == "status" {
                return Self.stream(lines: ["{\"processes\":[{\"state\":\"running\"}]}"])
            }
            if command == "start", commit == self.newCommit {
                return Self.stream(lines: [], failure: StubError.startFailed)
            }
            return Self.stream(lines: ["{}"])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(outcome, .failed("The updated recorder could not start."))
        XCTAssertEqual(calls, [
            "status:\(oldCommit)", "stop:\(oldCommit)", "start:\(newCommit)",
            "stop:\(newCommit)", "start:\(oldCommit)",
        ])
        XCTAssertEqual(fixture.installedCommit(), oldCommit)
        XCTAssertEqual(fixture.versionTargets.count, 1, "failed new target is retained, never deleted")
        XCTAssertEqual(try fixture.legacySideFile(), "old-side-files")
    }

    @MainActor
    func testLegacyRollbackRefusesSameSizeBackupSideFileAndPendingTargetMutationsBeforeReplacingLauncher() async throws {
        for mutation in ["binary", "side", "pending-target"] {
            let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit)
            defer { fixture.remove() }
            let pending = mutation == "pending-target" ? try fixture.createInterruptedLegacyMigrationTarget(commit: oldCommit) : nil
            var calls: [String] = []
            let model = fixture.model { _, arguments in
                let command = arguments[0]
                let commit = fixture.installedCommit() ?? "missing"
                calls.append("\(command):\(commit)")
                if command == "status" { return Self.stream(lines: ["{\"processes\":[{\"state\":\"running\"}]}"]) }
                if command == "start", commit == self.newCommit { return Self.stream(lines: [], failure: StubError.startFailed) }
                if command == "stop", commit == self.newCommit {
                    do {
                        if mutation == "binary" { try fixture.mutateLegacyBackupWithoutChangingSize() }
                        else if let pending { try fixture.mutateSideFileWithoutChangingSize(in: pending) }
                        else { try fixture.mutateSideFileWithoutChangingSize(in: fixture.installedDirectory) }
                    } catch { XCTFail("failed mutation fixture: \(error)") }
                }
                return Self.stream(lines: [])
            }
            guard case .failed(let message) = await model.upgradeInstalledCLIIfNeeded() else { return XCTFail("expected refused rollback") }
            XCTAssertTrue(message.contains("restoring the previous CLI failed"), mutation)
            XCTAssertEqual(try fixture.stableLauncher(), fixture.expectedStableLauncherContents, mutation)
            XCTAssertEqual(fixture.installedCommit(), newCommit, mutation)
            XCTAssertFalse(calls.contains("start:\(oldCommit)"), mutation)
            XCTAssertTrue(fixture.runtimeTransactionExists, mutation)
        }
    }

    @MainActor
    func testVersionedRollbackRefusesDriftedOldPayloadAndNeverStartsIt() async throws {
        let fixture = try UpgradeFixture(
            bundleCommit: newCommit,
            installedCommit: oldCommit,
            installedAsVersioned: true
        )
        defer { fixture.remove() }
        let oldTarget = try XCTUnwrap(fixture.selectedTarget)
        var calls: [String] = []
        let model = fixture.model { _, arguments in
            let command = arguments[0]
            let commit = fixture.installedCommit() ?? "missing"
            calls.append("\(command):\(commit)")
            if command == "status" {
                return Self.stream(lines: ["{\"processes\":[{\"state\":\"running\"}]}"])
            }
            if command == "start", commit == self.newCommit {
                return Self.stream(lines: [], failure: StubError.startFailed)
            }
            if command == "stop", commit == self.newCommit {
                try? fixture.mutateSideFileWithoutChangingSize(in: oldTarget)
            }
            return Self.stream(lines: ["{}"])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        guard case .failed(let message) = outcome else { return XCTFail("expected failure") }
        XCTAssertTrue(message.contains("restoring the previous CLI failed"))
        XCTAssertTrue(message.contains("safe rollback did not complete"))
        XCTAssertEqual(calls, [
            "status:\(oldCommit)", "stop:\(oldCommit)", "start:\(newCommit)",
            "stop:\(newCommit)",
        ])
        XCTAssertEqual(fixture.installedCommit(), newCommit)
        XCTAssertEqual(fixture.versionTargets.count, 2)
        XCTAssertTrue(fixture.runtimeTransactionExists)
    }

    @MainActor
    func testFailedRecoveryStopLeavesNewVersionSelectedAndDoesNotStartOldRuntime() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit)
        defer { fixture.remove() }
        var calls: [String] = []
        let model = fixture.model { _, arguments in
            let command = arguments[0]
            let commit = fixture.installedCommit() ?? "missing"
            calls.append("\(command):\(commit)")
            if command == "status" {
                return Self.stream(lines: ["{\"processes\":[{\"state\":\"running\"}]}"])
            }
            if command == "start", commit == self.newCommit {
                return Self.stream(lines: [], failure: StubError.startFailed)
            }
            if command == "stop", commit == self.newCommit {
                return Self.stream(lines: [], failure: StubError.stopFailed)
            }
            return Self.stream(lines: ["{}"])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        guard case .failed(let message) = outcome else { return XCTFail("expected failure") }
        XCTAssertTrue(message.contains("stopping the partial updated recorder failed"))
        XCTAssertEqual(calls, [
            "status:\(oldCommit)", "stop:\(oldCommit)", "start:\(newCommit)", "stop:\(newCommit)",
        ])
        XCTAssertEqual(fixture.installedCommit(), newCommit)
        XCTAssertTrue(fixture.hasLegacyBinaryBackup)
    }

    @MainActor
    func testTryAgainAfterFailedRecoveryEnsuresSelectedPackagedRuntime() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit)
        defer { fixture.remove() }
        var calls: [String] = []
        var newStartAttempts = 0
        var newStopAttempts = 0
        let model = fixture.model { _, arguments in
            let command = arguments[0]
            let commit = fixture.installedCommit() ?? "missing"
            calls.append("\(command):\(commit)")
            if command == "status" {
                let running = commit == self.oldCommit
                return Self.stream(lines: [
                    running
                        ? "{\"processes\":[{\"state\":\"running\"}]}"
                        : "{\"processes\":[]}"
                ])
            }
            if command == "start", commit == self.newCommit {
                newStartAttempts += 1
                if newStartAttempts == 1 {
                    return Self.stream(lines: [], failure: StubError.startFailed)
                }
            }
            if command == "stop", commit == self.newCommit {
                newStopAttempts += 1
                if newStopAttempts == 1 {
                    return Self.stream(lines: [], failure: StubError.stopFailed)
                }
            }
            return Self.stream(lines: ["{}"])
        }

        guard case .failed = await model.upgradeInstalledCLIIfNeeded() else {
            return XCTFail("first activation must fail")
        }
        XCTAssertEqual(fixture.installedCommit(), newCommit)

        model.reset()
        await model.setUp()

        XCTAssertEqual(model.phase, .done)
        XCTAssertEqual(newStartAttempts, 2)
        XCTAssertEqual(Array(calls.suffix(2)), ["stop:\(newCommit)", "start:\(newCommit)"])
        XCTAssertEqual(fixture.installedCommit(), newCommit)
    }

    @MainActor
    func testIdentityChangeDuringRecoveryPreservesBothVersionsAndRunsNoUnknownPath() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit)
        defer { fixture.remove() }
        var calls: [String] = []
        let model = fixture.model { _, arguments in
            let command = arguments[0]
            let commit = fixture.installedCommit() ?? "missing"
            calls.append("\(command):\(commit)")
            if command == "status" {
                return Self.stream(lines: ["{\"processes\":[{\"state\":\"running\"}]}"])
            }
            if command == "start", commit == self.newCommit {
                return Self.stream(lines: [], failure: StubError.startFailed)
            }
            if command == "stop", commit == self.newCommit {
                try? fixture.replaceTargetMarkerWithUnknownPath()
            }
            return Self.stream(lines: ["{}"])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        guard case .failed(let message) = outcome else { return XCTFail("expected failure") }
        XCTAssertTrue(message.contains("identity changed during recovery"))
        XCTAssertEqual(calls, [
            "status:\(oldCommit)", "stop:\(oldCommit)", "start:\(newCommit)", "stop:\(newCommit)",
        ])
        XCTAssertTrue(fixture.hasLegacyBinaryBackup)
        XCTAssertEqual(fixture.versionTargets.count, 1)
    }

    @MainActor
    func testCancellationAfterSwitchStopsNewThenRollsBackAndRestartsOldRuntime() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit)
        defer { fixture.remove() }
        var calls: [String] = []
        let model = fixture.model { _, arguments in
            let command = arguments[0]
            let commit = fixture.installedCommit() ?? "missing"
            calls.append("\(command):\(commit)")
            if command == "status" {
                return Self.stream(lines: ["{\"processes\":[{\"state\":\"running\"}]}"])
            }
            if command == "start", commit == self.newCommit {
                return Self.stream(lines: [], failure: CancellationError())
            }
            return Self.stream(lines: ["{}"])
        }

        guard case .failed = await model.upgradeInstalledCLIIfNeeded() else {
            return XCTFail("cancellation after activation must recover and report failure")
        }
        XCTAssertEqual(calls, [
            "status:\(oldCommit)", "stop:\(oldCommit)", "start:\(newCommit)",
            "stop:\(newCommit)", "start:\(oldCommit)",
        ])
        XCTAssertEqual(fixture.installedCommit(), oldCommit)
        XCTAssertEqual(fixture.versionTargets.count, 1)
    }

    @MainActor
    func testPartialStableLauncherWriteCannotDamageExistingCLIOrWrapper() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit)
        defer { fixture.remove() }
        let originalBinary = try Data(contentsOf: fixture.installedBinary)
        let originalWrapper = try Data(contentsOf: fixture.wrapper)
        let model = fixture.model(
            processRunner: { _, _ in Self.stream(lines: ["{\"processes\":[]}"]) },
            writeWrapper: { _, destination in
                try "partial".write(to: destination, atomically: false, encoding: .utf8)
                throw StubError.wrapperWriteFailed
            }
        )

        let outcome = await model.upgradeInstalledCLIIfNeeded()
        XCTAssertEqual(outcome, .failed("The wrapper write failed."))
        XCTAssertEqual(try Data(contentsOf: fixture.installedBinary), originalBinary)
        XCTAssertEqual(try Data(contentsOf: fixture.wrapper), originalWrapper)
        XCTAssertEqual(fixture.installedCommit(), oldCommit)
        XCTAssertTrue(fixture.versionTargets.isEmpty, "an unselected prepared target is removed")
    }

    @MainActor
    func testInvalidStoreEnvironmentPreventsRuntimeProbeAndSwitch() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit)
        defer { fixture.remove() }
        var commands: [[String]] = []
        let model = fixture.model(
            processRunner: { _, arguments in
                commands.append(arguments)
                return Self.stream(lines: [])
            },
            storeDirectory: {
                throw GlanceStoreResolutionError.relativeStoreEnvironment(
                    name: "AGENTACCT_STORE_DIR",
                    value: "relative"
                )
            }
        )

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        guard case .failed(let message) = outcome else { return XCTFail("expected failure") }
        XCTAssertTrue(message.contains("must be an absolute path"))
        XCTAssertTrue(commands.isEmpty)
        XCTAssertEqual(fixture.installedCommit(), oldCommit)
        XCTAssertTrue(fixture.versionTargets.isEmpty)

        model.reset()
        guard case .failed = await model.upgradeInstalledCLIIfNeeded() else {
            return XCTFail("retry must continue to fail closed")
        }
        XCTAssertTrue(fixture.versionTargets.isEmpty, "retries must not accumulate orphaned onedir copies")
    }

    @MainActor
    func testCleanupPreservesPreparedTargetWhenTargetMarkerIsMalformed() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit)
        defer { fixture.remove() }
        var preparedTarget: URL?
        let model = fixture.model(storeDirectory: {
            guard let target = fixture.versionTargets.first else {
                throw CocoaError(.fileNoSuchFile)
            }
            preparedTarget = target
            try "\(target.path)\nextra-line\n".write(
                to: fixture.targetMarker,
                atomically: true,
                encoding: .utf8
            )
            throw StubError.launchFailed
        })

        let outcome = await model.upgradeInstalledCLIIfNeeded()
        XCTAssertEqual(outcome, .failed("The recorder could not be launched."))
        let target = try XCTUnwrap(preparedTarget)
        XCTAssertTrue(FileManager.default.fileExists(atPath: target.path))
        XCTAssertEqual(fixture.versionTargets, [target])
    }

    @MainActor
    func testCleanupPreservesPreparedTargetWhenTargetMarkerCannotBeRead() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit)
        defer { fixture.remove() }
        var preparedTarget: URL?
        let model = fixture.model(storeDirectory: {
            guard let target = fixture.versionTargets.first else {
                throw CocoaError(.fileNoSuchFile)
            }
            preparedTarget = target
            try FileManager.default.createDirectory(
                at: fixture.targetMarker,
                withIntermediateDirectories: false
            )
            throw StubError.launchFailed
        })

        let outcome = await model.upgradeInstalledCLIIfNeeded()
        XCTAssertEqual(outcome, .failed("The recorder could not be launched."))
        let target = try XCTUnwrap(preparedTarget)
        XCTAssertTrue(FileManager.default.fileExists(atPath: target.path))
        XCTAssertEqual(fixture.versionTargets, [target])
    }

    @MainActor
    func testFirstRunInstallsVersionedCLIAndOnboardsThroughStableLauncher() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: nil)
        defer { fixture.remove() }
        var calls: [(URL, [String])] = []
        let model = fixture.model { executable, arguments in
            calls.append((executable, arguments))
            return Self.stream(lines: ["configured"])
        }

        XCTAssertTrue(model.shouldOfferSetup)
        await model.setUp()

        XCTAssertEqual(model.phase, .done)
        XCTAssertEqual(calls.count, 1)
        XCTAssertEqual(calls.first?.0, fixture.installedBinary)
        XCTAssertEqual(calls.first?.1, ["onboard", "--agent", "auto", "--yes"])
        XCTAssertEqual(fixture.installedCommit(), newCommit)
        XCTAssertEqual(try String(contentsOf: fixture.wrapper, encoding: .utf8), fixture.outerWrapperContents)
        let launcher = try fixture.stableLauncher()
        XCTAssertTrue(launcher.contains(
            "PATH='\(fixture.binDirectory.path)':\"${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}\""
        ))
        XCTAssertTrue(launcher.contains("exec \"$target/agentacct\" \"$@\""))
    }

    @MainActor
    func testFirstRunRejectsBundledCLIVersionThatDoesNotMatchAppVersion() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: nil)
        defer { fixture.remove() }
        var onboardCalls = 0
        let model = fixture.model(
            processRunner: { _, _ in
                onboardCalls += 1
                return Self.stream(lines: [])
            },
            reportedBundledVersion: "0.10.0"
        )

        await model.setUp()

        XCTAssertEqual(
            model.phase,
            .failed("The bundled recorder version does not match this app, so it was not installed.")
        )
        XCTAssertEqual(onboardCalls, 0)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.installedBinary.path))
        XCTAssertTrue(fixture.versionTargets.isEmpty)
    }

    @MainActor
    func testFailedOldRuntimeStopDoesNotAccumulateUnselectedPreparedTarget() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit)
        defer { fixture.remove() }
        var calls: [String] = []
        let model = fixture.model { _, arguments in
            let command = arguments[0]
            calls.append("\(command):\(fixture.installedCommit() ?? "missing")")
            if command == "status" {
                return Self.stream(lines: ["{\"processes\":[{\"state\":\"running\"}]}"])
            }
            if command == "stop" {
                return Self.stream(lines: [], failure: StubError.stopFailed)
            }
            return Self.stream(lines: ["{}"])
        }

        guard case .failed = await model.upgradeInstalledCLIIfNeeded() else {
            return XCTFail("the failed stop must fail the upgrade")
        }

        XCTAssertEqual(calls, ["status:\(oldCommit)", "stop:\(oldCommit)", "start:\(oldCommit)"])
        XCTAssertEqual(fixture.installedCommit(), oldCommit)
        XCTAssertTrue(fixture.versionTargets.isEmpty)
        XCTAssertFalse(fixture.runtimeTransactionExists)
    }

    @MainActor
    func testFirstRunRecoversCrashAfterTargetMarkerBeforeStableLauncher() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: nil)
        defer { fixture.remove() }
        let interruptedTarget = try fixture.createPartialFirstInstall(includeStableLauncher: false)
        var calls: [(URL, [String])] = []
        let model = fixture.model { executable, arguments in
            calls.append((executable, arguments))
            return Self.stream(lines: ["configured"])
        }

        XCTAssertTrue(model.shouldOfferSetup)
        await model.setUp()

        XCTAssertEqual(model.phase, .done)
        XCTAssertEqual(calls.count, 1)
        XCTAssertEqual(calls.first?.0, fixture.installedBinary)
        XCTAssertEqual(calls.first?.1, ["onboard", "--agent", "auto", "--yes"])
        XCTAssertEqual(fixture.selectedTarget, interruptedTarget)
        XCTAssertEqual(fixture.versionTargets.count, 1, "recovery must not stage a duplicate target")
        XCTAssertEqual(try fixture.stableLauncher(), fixture.expectedStableLauncherContents)
        XCTAssertEqual(try String(contentsOf: fixture.wrapper, encoding: .utf8), fixture.outerWrapperContents)
    }

    @MainActor
    func testFirstRunRecoversCrashAfterStableLauncherBeforeOuterWrapper() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: nil)
        defer { fixture.remove() }
        let interruptedTarget = try fixture.createPartialFirstInstall(includeStableLauncher: true)
        var calls: [(URL, [String])] = []
        let model = fixture.model { executable, arguments in
            calls.append((executable, arguments))
            return Self.stream(lines: ["configured"])
        }

        XCTAssertTrue(model.shouldOfferSetup, "an exact launcher without its wrapper remains incomplete")
        await model.setUp()

        XCTAssertEqual(model.phase, .done)
        XCTAssertEqual(calls.count, 1)
        XCTAssertEqual(calls.first?.0, fixture.installedBinary)
        XCTAssertEqual(calls.first?.1, ["onboard", "--agent", "auto", "--yes"])
        XCTAssertEqual(fixture.selectedTarget, interruptedTarget)
        XCTAssertEqual(fixture.versionTargets.count, 1)
        XCTAssertEqual(try String(contentsOf: fixture.wrapper, encoding: .utf8), fixture.outerWrapperContents)
    }

    @MainActor
    func testFirstRunCrashAfterWrapperStillFinishesPendingOnboarding() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: nil)
        defer { fixture.remove() }
        try fixture.createCompletedFirstInstallWithPendingOnboarding()
        var calls: [(URL, [String])] = []
        let model = fixture.model { executable, arguments in
            calls.append((executable, arguments))
            return Self.stream(lines: ["configured"])
        }

        XCTAssertTrue(model.isCLIInstalled)
        XCTAssertTrue(model.shouldOfferSetup, "persistent onboarding intent survives process death")
        await model.setUp()

        XCTAssertEqual(model.phase, .done)
        XCTAssertEqual(calls.count, 1)
        XCTAssertEqual(calls.first?.0, fixture.installedBinary)
        XCTAssertEqual(calls.first?.1, ["onboard", "--agent", "auto", "--yes"])
        XCTAssertFalse(fixture.onboardingPendingExists)
    }

    @MainActor
    func testPersistentRecoveryCompletesUpgradeAfterCrashBeforeTargetSwitch() async throws {
        let fixture = try UpgradeFixture(
            bundleCommit: newCommit,
            installedCommit: oldCommit,
            installedAsVersioned: true
        )
        defer { fixture.remove() }
        let newTarget = try fixture.createInterruptedRuntimeTransaction(selectNewTarget: false)
        var calls: [String] = []
        let model = fixture.model { _, arguments in
            calls.append("\(arguments[0]):\(fixture.installedCommit() ?? "missing")")
            return Self.stream(lines: ["{}"])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(outcome, .upgraded)
        XCTAssertEqual(calls, ["stop:\(oldCommit)", "start:\(newCommit)"])
        XCTAssertEqual(fixture.selectedTarget, newTarget)
        XCTAssertFalse(fixture.runtimeTransactionExists)
    }

    @MainActor
    func testPersistentRecoveryRestartsUpgradeAfterCrashFollowingTargetSwitch() async throws {
        let fixture = try UpgradeFixture(
            bundleCommit: newCommit,
            installedCommit: oldCommit,
            installedAsVersioned: true
        )
        defer { fixture.remove() }
        let newTarget = try fixture.createInterruptedRuntimeTransaction(selectNewTarget: true)
        var calls: [String] = []
        let model = fixture.model { _, arguments in
            calls.append("\(arguments[0]):\(fixture.installedCommit() ?? "missing")")
            return Self.stream(lines: ["{}"])
        }

        XCTAssertFalse(model.shouldAutomaticallyUpgradeCLI, "selected provenance already matches the App")
        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(outcome, .upgraded)
        XCTAssertEqual(calls, ["stop:\(newCommit)", "start:\(newCommit)"])
        XCTAssertEqual(fixture.selectedTarget, newTarget)
        XCTAssertFalse(fixture.runtimeTransactionExists)
    }

    @MainActor
    func testUpgradeRechecksPersistentJournalAfterAcquiringCrossProcessLock() async throws {
        let fixture = try UpgradeFixture(
            bundleCommit: newCommit,
            installedCommit: oldCommit,
            installedAsVersioned: true
        )
        defer { fixture.remove() }
        var wroteCompetingJournal = false
        var calls: [String] = []
        let model = fixture.model(
            processRunner: { _, arguments in
                calls.append("\(arguments[0]):\(fixture.installedCommit() ?? "missing")")
                return Self.stream(lines: ["{}"])
            },
            transactionLockObserver: {
                guard !wroteCompetingJournal else { return }
                wroteCompetingJournal = true
                _ = try! fixture.createInterruptedRuntimeTransaction(selectNewTarget: false)
            }
        )

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(outcome, .upgraded)
        XCTAssertTrue(wroteCompetingJournal)
        XCTAssertEqual(calls, ["stop:\(oldCommit)", "start:\(newCommit)"])
        XCTAssertEqual(fixture.versionTargets.count, 2, "journal recovery must win over a stale normal-upgrade context")
        XCTAssertFalse(fixture.runtimeTransactionExists)
    }

    @MainActor
    func testStaleJournalRecoveryContinuesIntoCurrentBundledUpgrade() async throws {
        let intermediateCommit = "3333333333333333333333333333333333333333"
        let fixture = try UpgradeFixture(
            bundleCommit: newCommit,
            bundleVersion: "0.12.0",
            installedCommit: oldCommit,
            installedAsVersioned: true
        )
        defer { fixture.remove() }
        let staleTarget = try fixture.createInterruptedRuntimeTransaction(
            selectNewTarget: false,
            transactionCommit: intermediateCommit,
            transactionVersion: "0.11.0"
        )
        var calls: [String] = []
        var runtimeRunning = false
        let model = fixture.model { _, arguments in
            let command = arguments[0]
            calls.append("\(command):\(fixture.installedCommit() ?? "missing")")
            switch command {
            case "status":
                return Self.stream(lines: [
                    runtimeRunning
                        ? "{\"processes\":[{\"state\":\"running\"}]}"
                        : "{\"processes\":[]}"
                ])
            case "start":
                runtimeRunning = true
            case "stop":
                runtimeRunning = false
            default:
                break
            }
            return Self.stream(lines: [])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(outcome, .upgraded)
        XCTAssertEqual(fixture.installedCommit(), newCommit)
        XCTAssertNotEqual(fixture.selectedTarget, staleTarget)
        XCTAssertFalse(fixture.runtimeTransactionExists)
        XCTAssertTrue(runtimeRunning)
        XCTAssertEqual(
            calls,
            [
                "stop:\(oldCommit)",
                "start:\(oldCommit)",
                "status:\(oldCommit)",
                "stop:\(oldCommit)",
                "start:\(newCommit)",
            ]
        )
    }

    @MainActor
    func testProcessCrashRecoveryRunsNoCommandAfterStableIdentityChanges() async throws {
        let fixture = try UpgradeFixture(
            bundleCommit: newCommit,
            installedCommit: oldCommit,
            installedAsVersioned: true
        )
        defer { fixture.remove() }
        _ = try fixture.createInterruptedRuntimeTransaction(selectNewTarget: false)
        try fixture.replaceWrapperWithUserManagedFile()
        var calls: [[String]] = []
        let model = fixture.model { _, arguments in
            calls.append(arguments)
            return Self.stream(lines: [])
        }

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(
            outcome,
            .failed("The recorder identity changed during recovery, so its files were preserved.")
        )
        XCTAssertTrue(calls.isEmpty)
        XCTAssertTrue(fixture.runtimeTransactionExists)
        XCTAssertEqual(fixture.versionTargets.count, 2)
    }

    @MainActor
    func testProcessCrashRecoveryRefusesChangedStoreWithoutRunningCommand() async throws {
        let fixture = try UpgradeFixture(
            bundleCommit: newCommit,
            installedCommit: oldCommit,
            installedAsVersioned: true
        )
        defer { fixture.remove() }
        _ = try fixture.createInterruptedRuntimeTransaction(selectNewTarget: false)
        var calls: [[String]] = []
        let changedStore = fixture.root.appendingPathComponent("different-store", isDirectory: true)
        let model = fixture.model(
            processRunner: { _, arguments in
                calls.append(arguments)
                return Self.stream(lines: [])
            },
            storeDirectory: { changedStore }
        )

        let outcome = await model.upgradeInstalledCLIIfNeeded()

        guard case .failed(let message) = outcome else { return XCTFail("expected fail-closed recovery") }
        XCTAssertTrue(message.contains("store changed"))
        XCTAssertTrue(calls.isEmpty)
        XCTAssertTrue(fixture.runtimeTransactionExists)
    }

    @MainActor
    func testInterruptedLegacyMarkerMigrationIsRecognizedAndCompletedOnNextLaunch() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: oldCommit)
        defer { fixture.remove() }
        let interruptedTarget = try fixture.createInterruptedLegacyMigrationTarget(commit: newCommit)
        let model = fixture.model { _, _ in Self.stream(lines: ["{\"processes\":[]}"]) }

        XCTAssertTrue(model.shouldAutomaticallyUpgradeCLI)
        let outcome = await model.upgradeInstalledCLIIfNeeded()

        XCTAssertEqual(outcome, .upgraded)
        XCTAssertEqual(fixture.installedCommit(), newCommit)
        XCTAssertTrue(FileManager.default.fileExists(atPath: interruptedTarget.path))
        XCTAssertEqual(fixture.versionTargets.count, 2)
        XCTAssertEqual(try fixture.legacySideFile(), "old-side-files")
    }

    @MainActor
    func testOnboardingRetryReusesVerifiedInstalledLauncher() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: nil)
        defer { fixture.remove() }
        var onboardAttempts = 0
        let model = fixture.model { executable, arguments in
            XCTAssertEqual(executable, fixture.installedBinary)
            XCTAssertEqual(arguments, ["onboard", "--agent", "auto", "--yes"])
            onboardAttempts += 1
            return Self.stream(
                lines: [],
                failure: onboardAttempts == 1 ? StubError.launchFailed : nil
            )
        }

        await model.setUp()
        XCTAssertEqual(model.phase, .failed("The recorder could not be launched."))
        XCTAssertEqual(fixture.installedCommit(), newCommit)

        model.reset()
        await model.setUp()

        XCTAssertEqual(model.phase, .done)
        XCTAssertEqual(onboardAttempts, 2)
        XCTAssertEqual(fixture.versionTargets.count, 1, "retry must not install a duplicate target")
    }

    @MainActor
    func testFirstRunPreservesPreExistingUserWrapper() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: nil)
        defer { fixture.remove() }
        try FileManager.default.createDirectory(
            at: fixture.wrapper.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        let userWrapper = "#!/bin/sh\necho user-install\n"
        try userWrapper.write(to: fixture.wrapper, atomically: true, encoding: .utf8)
        let model = fixture.model()

        await model.setUp()

        XCTAssertEqual(
            model.phase,
            .failed("The agentacct command wrapper is not a regular app-managed file, so it was not replaced.")
        )
        XCTAssertEqual(try String(contentsOf: fixture.wrapper, encoding: .utf8), userWrapper)
        XCTAssertNil(fixture.installedCommit())
    }

    @MainActor
    func testFirstRunPreservesNonEmptyUnownedStableDirectory() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: nil)
        defer { fixture.remove() }
        try FileManager.default.createDirectory(at: fixture.installedDirectory, withIntermediateDirectories: true)
        let userFile = fixture.installedDirectory.appendingPathComponent("user-data")
        try "mine".write(to: userFile, atomically: true, encoding: .utf8)
        let model = fixture.model()

        await model.setUp()

        XCTAssertEqual(
            model.phase,
            .failed("The stable recorder path is not a regular app-managed directory, so it was not replaced.")
        )
        XCTAssertEqual(try String(contentsOf: userFile, encoding: .utf8), "mine")
    }

    @MainActor
    func testFirstRunPreservesNonEmptyUnownedVersionsDirectory() async throws {
        let fixture = try UpgradeFixture(bundleCommit: newCommit, installedCommit: nil)
        defer { fixture.remove() }
        try FileManager.default.createDirectory(at: fixture.versionsDirectory, withIntermediateDirectories: true)
        let userFile = fixture.versionsDirectory.appendingPathComponent("user-data")
        try "mine".write(to: userFile, atomically: true, encoding: .utf8)
        let model = fixture.model()

        await model.setUp()

        XCTAssertEqual(
            model.phase,
            .failed("The recorder versions path is not empty or app-managed, so it was not changed.")
        )
        XCTAssertEqual(try String(contentsOf: userFile, encoding: .utf8), "mine")
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.installedBinary.path))
    }

    @MainActor
    private func makeModel(lines: [String] = [], failure: Error? = nil) -> SetupModel {
        SetupModel(
            installer: { URL(fileURLWithPath: "/tmp/test-agentacct") },
            processRunner: { _, _ in Self.stream(lines: lines, failure: failure) }
        )
    }

    fileprivate static func stream(
        lines: [String],
        failure: Error? = nil
    ) -> AsyncThrowingStream<String, Error> {
        AsyncThrowingStream { continuation in
            for line in lines { continuation.yield(line) }
            if let failure {
                continuation.finish(throwing: failure)
            } else {
                continuation.finish()
            }
        }
    }

    private enum StubError: LocalizedError {
        case launchFailed
        case copyFailed
        case startFailed
        case stopFailed
        case wrapperWriteFailed

        var errorDescription: String? {
            switch self {
            case .launchFailed: "The recorder could not be launched."
            case .copyFailed: "The recorder copy failed."
            case .startFailed: "The updated recorder could not start."
            case .stopFailed: "The updated recorder could not stop."
            case .wrapperWriteFailed: "The wrapper write failed."
            }
        }
    }
}

@MainActor
private final class UpgradeFixture {
    private let fm = FileManager.default
    private let bundleCommit: String
    private let bundleVersion: String
    private let initialInstalledCommit: String?
    private let installedVersion: String?
    private var simulatedLaunchdLoaded = true
    private var simulatedLaunchdArguments: [String]?
    private var simulatedLaunchdBootstrapped = false
    var simulatedLaunchdRunning = true
    var launchdPrintOverride: (() -> AsyncThrowingStream<String, Error>)?
    var autostartReadinessOverride: (() -> String)?
    private(set) var autostartReadinessChecks = 0
    let root: URL
    let home: URL
    let resources: URL
    let installedDirectory: URL
    let installedBinary: URL
    let versionsDirectory: URL
    let targetMarker: URL
    let wrapper: URL
    let binDirectory: URL
    var runtimeTransactionFile: URL {
        home.appendingPathComponent(".local/share/agentacct/.agentacct-app-cli-transaction")
    }
    var onboardingPendingFile: URL {
        home.appendingPathComponent(".local/share/agentacct/.agentacct-app-onboarding-pending")
    }
    var transactionLockFile: URL {
        home.appendingPathComponent(".local/share/agentacct/.agentacct-app-cli.lock")
    }
    var store: URL { root.appendingPathComponent("store", isDirectory: true) }

    var outerWrapperContents: String {
        "#!/bin/sh\nexec \"\(installedBinary.path)\" \"$@\"\n"
    }

    private var stableLauncherContents: String {
        """
        #!/bin/sh
        PATH='\(binDirectory.path)':"${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"
        export PATH
        target_file='\(targetMarker.path)'
        IFS= read -r target < "$target_file" || exit 1
        [ -n "$target" ] || exit 1
        exec "$target/agentacct" "$@"

        """
    }

    var expectedStableLauncherContents: String { stableLauncherContents }
    var runtimeTransactionExists: Bool { fm.fileExists(atPath: runtimeTransactionFile.path) }
    var onboardingPendingExists: Bool { fm.fileExists(atPath: onboardingPendingFile.path) }

    init(
        bundleCommit: String,
        bundleVersion: String = "0.11.0",
        installedCommit: String?,
        installedVersion: String? = "0.10.0",
        installedAsVersioned: Bool = false
    ) throws {
        self.bundleCommit = bundleCommit
        self.bundleVersion = bundleVersion
        initialInstalledCommit = installedCommit
        self.installedVersion = installedVersion
        root = fm.temporaryDirectory
            .appendingPathComponent("agentacct-cli-upgrade-\(UUID().uuidString)", isDirectory: true)
        home = root.appendingPathComponent("home", isDirectory: true)
        resources = root.appendingPathComponent("agentacct.app/Contents/Resources", isDirectory: true)
        installedDirectory = home.appendingPathComponent(".local/share/agentacct/cli", isDirectory: true)
        installedBinary = installedDirectory.appendingPathComponent("agentacct")
        versionsDirectory = home.appendingPathComponent(".local/share/agentacct/cli-versions", isDirectory: true)
        targetMarker = installedDirectory.appendingPathComponent(".agentacct-app-target")
        binDirectory = home.appendingPathComponent(".local/bin", isDirectory: true)
        wrapper = binDirectory.appendingPathComponent("agentacct")

        try writeCLI(
            at: resources.appendingPathComponent("cli", isDirectory: true),
            commit: bundleCommit,
            sideFile: "new-side-files"
        )
        if let installedCommit {
            if installedAsVersioned {
                try writeVersionedInstall(commit: installedCommit)
            } else {
                try writeCLI(at: installedDirectory, commit: installedCommit, sideFile: "old-side-files")
            }
            try fm.createDirectory(at: binDirectory, withIntermediateDirectories: true)
            try outerWrapperContents.write(to: wrapper, atomically: true, encoding: .utf8)
            try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: wrapper.path)
        }
    }

    func model(
        infoCommit: String? = nil,
        processRunner: ((URL, [String]) -> AsyncThrowingStream<String, Error>)? = nil,
        copyDirectory: ((URL, URL) throws -> Void)? = nil,
        writeWrapper: ((String, URL) throws -> Void)? = nil,
        storeDirectory: (() throws -> URL)? = nil,
        reportedBundledVersion: String? = nil,
        transactionLockObserver: (() -> Void)? = nil
    ) -> SetupModel {
        let runner: (URL, [String]) -> AsyncThrowingStream<String, Error> = { executable, arguments in
            if arguments == ["--version"] {
                if executable.standardizedFileURL != self.installedBinary.standardizedFileURL,
                   let reportedBundledVersion {
                    return SetupModelTests.stream(lines: ["agentacct \(reportedBundledVersion)"])
                }
                return SetupModelTests.stream(lines: ["agentacct \(self.version(for: executable))"])
            }
            if executable.path == "/bin/launchctl", arguments.first == "print" {
                if let override = self.launchdPrintOverride { return override() }
                if self.simulatedLaunchdLoaded {
                    return SetupModelTests.stream(lines: [self.launchdPrintOutput(arguments: self.simulatedLaunchdArguments ?? (try? self.autostartArguments()) ?? [], running: self.simulatedLaunchdRunning)])
                }
                return SetupModelTests.stream(lines: ["Bad request.", "Could not find service \"dev.agentacct.runtime\" in domain for user gui: \(Darwin.geteuid())"], failure: ProcessRunnerError.nonzeroExit(113))
            }
            if arguments.first == "status", self.simulatedLaunchdBootstrapped {
                self.autostartReadinessChecks += 1
                return SetupModelTests.stream(lines: [self.autostartReadinessOverride?() ?? self.readyRuntimeStatus])
            }
            if executable.path == "/bin/launchctl", arguments.first == "bootstrap" {
                self.simulatedLaunchdLoaded = true
                self.simulatedLaunchdBootstrapped = true
                self.simulatedLaunchdArguments = try? self.autostartArguments()
            }
            let stream = processRunner?(executable, arguments) ?? SetupModelTests.stream(lines: [])
            guard executable.path == "/bin/launchctl", arguments.first == "bootout" else { return stream }
            return AsyncThrowingStream { continuation in
                Task { @MainActor in
                    do {
                        for try await line in stream { continuation.yield(line) }
                        self.simulatedLaunchdLoaded = false
                        self.simulatedLaunchdBootstrapped = false
                        continuation.finish()
                    } catch {
                        if (error as? ProcessRunnerError) == .nonzeroExit(3) {
                            self.simulatedLaunchdLoaded = false
                            self.simulatedLaunchdBootstrapped = false
                        }
                        continuation.finish(throwing: error)
                    }
                }
            }
        }
        return SetupModel(
            processRunner: runner,
            homeDirectory: home,
            bundleResourceURL: resources,
            bundleInfoDictionary: [
                "CFBundlePackageType": "APPL",
                "CFBundleIdentifier": "dev.agentacct.app",
                "CFBundleShortVersionString": bundleVersion,
                "AgentacctGitCommit": infoCommit ?? bundleCommit,
                "AgentacctBuildDescription": description(for: infoCommit ?? bundleCommit),
            ],
            storeDirectory: storeDirectory ?? { self.store },
            copyDirectory: copyDirectory,
            writeWrapper: writeWrapper,
            transactionLockObserver: transactionLockObserver,
            readinessPause: {}
        )
    }

    var selectedTarget: URL? {
        guard let raw = try? String(contentsOf: targetMarker, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines),
              !raw.isEmpty
        else { return nil }
        return URL(fileURLWithPath: raw, isDirectory: true)
    }

    var versionTargets: [URL] {
        guard let entries = try? fm.contentsOfDirectory(
            at: versionsDirectory,
            includingPropertiesForKeys: [.isDirectoryKey]
        ) else { return [] }
        return entries.filter { url in
            (try? url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true
        }
    }

    var hasLegacyBinaryBackup: Bool {
        guard let entries = try? fm.contentsOfDirectory(atPath: installedDirectory.path) else { return false }
        return entries.contains { $0.hasPrefix(".agentacct-legacy-") }
    }

    func stableLauncher() throws -> String {
        try String(contentsOf: installedBinary, encoding: .utf8)
    }

    func legacySideFile() throws -> String {
        try String(
            contentsOf: installedDirectory.appendingPathComponent("_internal/side-file"),
            encoding: .utf8
        )
    }

    func removeInstalledProvenance() throws {
        try fm.removeItem(at: installedDirectory.appendingPathComponent(".agentacct-source-commit"))
        try fm.removeItem(at: installedDirectory.appendingPathComponent(".agentacct-source-description"))
    }

    func replaceWrapperWithUserManagedFile() throws {
        try "#!/bin/sh\necho changed-by-user\n".write(
            to: wrapper,
            atomically: true,
            encoding: .utf8
        )
        try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: wrapper.path)
    }

    func removeOuterWrapper() throws {
        try fm.removeItem(at: wrapper)
    }

    func removeStableLauncher() throws {
        try fm.removeItem(at: installedBinary)
    }

    func removeStableLauncherAndOuterWrapper() throws {
        try fm.removeItem(at: installedBinary)
        try fm.removeItem(at: wrapper)
    }

    var autostartFile: URL { home.appendingPathComponent("Library/LaunchAgents/dev.agentacct.runtime.plist") }

    var readyRuntimeStatus: String {
        "{\"state\":\"running\",\"store_dir\":\"\(store.path)\",\"dashboard_health\":\"healthy\",\"watcher\":\"external\",\"processes\":[{\"role\":\"dashboard\",\"state\":\"running\"}]}"
    }

    func simulateLoadedLaunchdArguments(_ arguments: [String]) {
        simulatedLaunchdArguments = arguments
    }

    func launchdPrintOutput(arguments: [String], running: Bool = true) -> String {
        "gui/\(Darwin.geteuid())/dev.agentacct.runtime = {\n\tpath = \(autostartFile.path)\n\ttype = LaunchAgent\n\tstate = \(running ? "running" : "not running")\n\tprogram = \(arguments.first ?? "unknown")\n\targuments = {\n"
            + arguments.map { "\t\t\($0)" }.joined(separator: "\n")
            + "\n\t}\n" + (running ? "\tpid = 12345\n" : "") + "}\n"
    }

    func installUnknownAutostartFile() throws {
        let path = home.appendingPathComponent("Library/LaunchAgents/dev.agentacct.runtime.plist")
        try fm.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
        try "managed".write(to: path, atomically: true, encoding: .utf8)
    }

    func installManagedAutostartFile(executable: URL? = nil, host: String? = nil, port: Int? = nil) throws {
        var arguments = [(executable ?? installedBinary).path, "start", "--foreground", "--store-dir", store.path]
        if let host { arguments += ["--host", host] }
        if let port { arguments += ["--port", String(port)] }
        let document: [String: Any] = [
            "Label": "dev.agentacct.runtime", "ProgramArguments": arguments,
            "RunAtLoad": true, "KeepAlive": true,
            "StandardOutPath": store.deletingLastPathComponent().appendingPathComponent("autostart.out.log").path,
            "StandardErrorPath": store.deletingLastPathComponent().appendingPathComponent("autostart.err.log").path,
        ]
        try fm.createDirectory(at: autostartFile.deletingLastPathComponent(), withIntermediateDirectories: true)
        try PropertyListSerialization.data(fromPropertyList: document, format: .xml, options: 0).write(to: autostartFile)
        try fm.setAttributes([.posixPermissions: 0o644], ofItemAtPath: autostartFile.path)
    }

    func autostartArguments() throws -> [String] {
        let document = try PropertyListSerialization.propertyList(from: Data(contentsOf: autostartFile), format: nil) as? [String: Any]
        return try XCTUnwrap(document?["ProgramArguments"] as? [String])
    }

    func mutateSelectedExecutableWithoutChangingSize() throws {
        let target = try XCTUnwrap(selectedTarget)
        let executable = target.appendingPathComponent("agentacct")
        let original = try Data(contentsOf: executable)
        var changed = original
        changed[changed.index(before: changed.endIndex)] ^= 0x01
        XCTAssertEqual(changed.count, original.count)
        let handle = try FileHandle(forWritingTo: executable)
        try handle.seek(toOffset: 0)
        try handle.write(contentsOf: changed)
        try handle.truncate(atOffset: UInt64(changed.count))
        try handle.close()
    }

    func mutateLegacyBackupWithoutChangingSize() throws {
        let backup = try XCTUnwrap(fm.contentsOfDirectory(at: installedDirectory, includingPropertiesForKeys: nil).first { $0.lastPathComponent.hasPrefix(".agentacct-legacy-") })
        let original = try Data(contentsOf: backup)
        var changed = original
        changed[changed.index(before: changed.endIndex)] ^= 0x01
        let identity = try identityValues(backup)
        let handle = try FileHandle(forWritingTo: backup)
        try handle.seek(toOffset: 0)
        try handle.write(contentsOf: changed)
        try handle.close()
        XCTAssertEqual(try identityValues(backup), identity)
    }

    func selectedExecutableIdentity() throws -> [UInt64] {
        let target = try XCTUnwrap(selectedTarget)
        return try identityValues(target.appendingPathComponent("agentacct"))
    }

    func selectedSideFileIdentity() throws -> [UInt64] {
        let target = try XCTUnwrap(selectedTarget)
        return try identityValues(target.appendingPathComponent("_internal/side-file"))
    }

    func mutateSelectedSideFileWithoutChangingSize() throws {
        let target = try XCTUnwrap(selectedTarget)
        try mutateSideFileWithoutChangingSize(in: target)
    }

    func mutateSideFileWithoutChangingSize(in target: URL) throws {
        let sideFile = target.appendingPathComponent("_internal/side-file")
        let original = try Data(contentsOf: sideFile)
        let changed = Data(repeating: 0x58, count: original.count)
        XCTAssertEqual(changed.count, original.count)
        let handle = try FileHandle(forWritingTo: sideFile)
        try handle.seek(toOffset: 0)
        try handle.write(contentsOf: changed)
        try handle.truncate(atOffset: UInt64(changed.count))
        try handle.close()
    }

    func restampSelectedPayloadIdentity() throws {
        try writePayloadRecord(for: try XCTUnwrap(selectedTarget))
    }

    func makeSelectedTargetSharedWritable() throws {
        let target = try XCTUnwrap(selectedTarget)
        try fm.setAttributes([.posixPermissions: 0o777], ofItemAtPath: target.path)
    }

    func makeVersionsDirectorySharedWritable() throws {
        try fm.setAttributes([.posixPermissions: 0o777], ofItemAtPath: versionsDirectory.path)
    }

    func replaceTargetMarkerWithUnknownPath() throws {
        try "/tmp/not-an-agentacct-target\n".write(
            to: targetMarker,
            atomically: true,
            encoding: .utf8
        )
    }

    func createInterruptedLegacyMigrationTarget(commit: String) throws -> URL {
        try fm.createDirectory(at: versionsDirectory, withIntermediateDirectories: true)
        try writeVersionsOwnershipMarker()
        let target = versionsDirectory.appendingPathComponent(
            "v0.11.0-\(commit.prefix(12))-interrupted",
            isDirectory: true
        )
        try writeCLI(at: target, commit: commit, sideFile: "interrupted-new-side-files")
        try writePayloadRecord(for: target)
        try "\(target.path)\n".write(to: targetMarker, atomically: true, encoding: .utf8)
        try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: targetMarker.path)
        return target
    }

    func createPartialFirstInstall(includeStableLauncher: Bool) throws -> URL {
        try fm.createDirectory(at: versionsDirectory, withIntermediateDirectories: true)
        try writeVersionsOwnershipMarker()
        let target = versionsDirectory.appendingPathComponent(
            "v\(bundleVersion)-\(bundleCommit.prefix(12))-first-install-crash",
            isDirectory: true
        )
        try writeCLI(at: target, commit: bundleCommit, sideFile: "new-side-files")
        try writePayloadRecord(for: target)
        try fm.createDirectory(at: installedDirectory, withIntermediateDirectories: true)
        try "\(target.path)\n".write(to: targetMarker, atomically: true, encoding: .utf8)
        try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: targetMarker.path)
        if includeStableLauncher {
            try stableLauncherContents.write(to: installedBinary, atomically: true, encoding: .utf8)
            try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: installedBinary.path)
        }
        return target
    }

    func createCompletedFirstInstallWithPendingOnboarding() throws {
        _ = try createPartialFirstInstall(includeStableLauncher: true)
        try fm.createDirectory(at: binDirectory, withIntermediateDirectories: true)
        try outerWrapperContents.write(to: wrapper, atomically: true, encoding: .utf8)
        try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: wrapper.path)
        try "agentacct-macos-app-onboarding-pending-v1\n".write(
            to: onboardingPendingFile,
            atomically: true,
            encoding: .utf8
        )
        try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: onboardingPendingFile.path)
    }

    func createInterruptedRuntimeTransaction(
        selectNewTarget: Bool,
        transactionCommit: String? = nil,
        transactionVersion: String? = nil
    ) throws -> URL {
        guard let oldTarget = selectedTarget else {
            throw CocoaError(.fileNoSuchFile)
        }
        let newCommit = transactionCommit ?? bundleCommit
        let newVersion = transactionVersion ?? bundleVersion
        let newTarget = versionsDirectory.appendingPathComponent(
            "v\(newVersion)-\(newCommit.prefix(12))-runtime-crash",
            isDirectory: true
        )
        try writeCLI(at: newTarget, commit: newCommit, sideFile: "new-side-files")
        try writePayloadRecord(for: newTarget)

        let oldCommit = try String(
            contentsOf: oldTarget.appendingPathComponent(".agentacct-source-commit"),
            encoding: .utf8
        ).trimmingCharacters(in: .whitespacesAndNewlines)
        let journal: [String: Any] = [
            "schema": 1,
            "phase": selectNewTarget ? "targetSwitched" : "oldStopped",
            "wasRunning": true,
            "storePath": store.standardizedFileURL.path,
            "oldInstall": [
                "layout": "versioned",
                "targetPath": oldTarget.path,
                "provenance": [
                    "commit": oldCommit,
                    "description": description(for: oldCommit),
                ],
                "binaryIdentity": try identityJSON(oldTarget.appendingPathComponent("agentacct")),
                "payloadIdentity": try payloadIdentityJSON(oldTarget),
                "wrapperIdentity": try identityJSON(wrapper),
            ],
            "newTargetPath": newTarget.path,
            "newBinaryIdentity": try identityJSON(newTarget.appendingPathComponent("agentacct")),
            "newPayloadIdentity": try payloadIdentityJSON(newTarget),
            "newProvenance": [
                "commit": newCommit,
                "description": description(for: newCommit),
            ],
            "newReleaseVersion": newVersion,
        ]
        if selectNewTarget {
            try "\(newTarget.path)\n".write(to: targetMarker, atomically: true, encoding: .utf8)
            try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: targetMarker.path)
        }
        let data = try JSONSerialization.data(withJSONObject: journal, options: [.sortedKeys])
        try data.write(to: runtimeTransactionFile, options: .atomic)
        try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: runtimeTransactionFile.path)
        return newTarget
    }

    func installedCommit() -> String? {
        let directory = selectedTarget ?? installedDirectory
        return try? String(
            contentsOf: directory.appendingPathComponent(".agentacct-source-commit"),
            encoding: .utf8
        ).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func remove() {
        try? fm.removeItem(at: root)
    }

    private func writeVersionedInstall(commit: String) throws {
        try fm.createDirectory(at: versionsDirectory, withIntermediateDirectories: true)
        try writeVersionsOwnershipMarker()
        let target = versionsDirectory.appendingPathComponent("v0.10.0-\(commit.prefix(12))-existing", isDirectory: true)
        try writeCLI(at: target, commit: commit, sideFile: commit == bundleCommit ? "new-side-files" : "old-version-side-files")
        try writePayloadRecord(for: target)
        try fm.createDirectory(at: installedDirectory, withIntermediateDirectories: true)
        try "\(target.path)\n".write(to: targetMarker, atomically: true, encoding: .utf8)
        try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: targetMarker.path)
        try stableLauncherContents.write(to: installedBinary, atomically: true, encoding: .utf8)
        try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: installedBinary.path)
    }

    private func writeVersionsOwnershipMarker() throws {
        let marker = versionsDirectory.appendingPathComponent(".agentacct-app-managed")
        try "agentacct-macos-app-cli-versions-v1\n".write(
            to: marker,
            atomically: true,
            encoding: .utf8
        )
        try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: marker.path)
    }

    private func writeCLI(at directory: URL, commit: String, sideFile: String? = nil) throws {
        try fm.createDirectory(at: directory, withIntermediateDirectories: true)
        let executable = directory.appendingPathComponent("agentacct")
        try "#!/bin/sh\nexit 0\n".write(to: executable, atomically: true, encoding: .utf8)
        try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: executable.path)
        try "\(commit)\n".write(
            to: directory.appendingPathComponent(".agentacct-source-commit"),
            atomically: true,
            encoding: .utf8
        )
        try "\(description(for: commit))\n".write(
            to: directory.appendingPathComponent(".agentacct-source-description"),
            atomically: true,
            encoding: .utf8
        )
        if let sideFile {
            let internalDirectory = directory.appendingPathComponent("_internal", isDirectory: true)
            try fm.createDirectory(at: internalDirectory, withIntermediateDirectories: true)
            try sideFile.write(
                to: internalDirectory.appendingPathComponent("side-file"),
                atomically: true,
                encoding: .utf8
            )
        }
    }

    private func identityJSON(_ file: URL) throws -> [String: UInt64] {
        let attributes = try fm.attributesOfItem(atPath: file.path)
        guard let device = attributes[.systemNumber] as? NSNumber,
              let inode = attributes[.systemFileNumber] as? NSNumber,
              let size = attributes[.size] as? NSNumber
        else { throw CocoaError(.fileReadUnknown) }
        return [
            "device": device.uint64Value,
            "inode": inode.uint64Value,
            "size": size.uint64Value,
        ]
    }

    private func identityValues(_ file: URL) throws -> [UInt64] {
        let identity = try identityJSON(file)
        return [
            try XCTUnwrap(identity["device"]),
            try XCTUnwrap(identity["inode"]),
            try XCTUnwrap(identity["size"]),
        ]
    }

    private func payloadIdentityJSON(_ directory: URL) throws -> [String: Any] {
        guard let identity = CLIPayloadInspector.identity(at: directory) else {
            throw CocoaError(.fileReadCorruptFile)
        }
        return ["sha256": identity.sha256, "entryCount": identity.entryCount]
    }

    private func writePayloadRecord(for target: URL) throws {
        let record: [String: Any] = [
            "schema": 1,
            "targetName": target.lastPathComponent,
            "identity": try payloadIdentityJSON(target),
        ]
        let data = try JSONSerialization.data(withJSONObject: record, options: [.sortedKeys])
        let marker = versionsDirectory.appendingPathComponent(
            ".\(target.lastPathComponent).payload.json"
        )
        try data.write(to: marker, options: .atomic)
        try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: marker.path)
    }

    private func version(for executable: URL) -> String {
        if executable.standardizedFileURL == installedBinary.standardizedFileURL {
            if (try? String(contentsOf: installedBinary, encoding: .utf8)) != stableLauncherContents,
               initialInstalledCommit != nil {
                return installedVersion ?? "invalid"
            }
            return installedCommit() == initialInstalledCommit
                ? (installedVersion ?? "invalid")
                : bundleVersion
        }
        if let commit = try? String(
            contentsOf: executable.deletingLastPathComponent()
                .appendingPathComponent(".agentacct-source-commit"),
            encoding: .utf8
        ).trimmingCharacters(in: .whitespacesAndNewlines),
           commit == initialInstalledCommit {
            return installedVersion ?? "invalid"
        }
        return bundleVersion
    }

    private func description(for commit: String) -> String {
        "v0.10.6-1-g\(commit.prefix(7))"
    }
}
