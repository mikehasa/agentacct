import Foundation
import Darwin
import CryptoKit
import CoreFoundation

struct CLIPayloadIdentity: Codable, Equatable {
    let sha256: String
    let entryCount: Int

    var isValid: Bool {
        entryCount > 0
            && sha256.count == 64
            && sha256.unicodeScalars.allSatisfy {
                ("0"..."9").contains(Character($0)) || ("a"..."f").contains(Character($0))
            }
    }
}

/// A deterministic content identity for the complete frozen onedir payload.
/// It never follows symlinks, rejects special or group/world-writable entries,
/// and binds each entry's type, unambiguous relative path, POSIX mode, size,
/// and (for files) SHA-256. Versioned targets also reject hard links so an
/// out-of-tree alias cannot change bytes behind an otherwise stable path.
enum CLIPayloadInspector {
    static func identity(
        at root: URL,
        excludingTopLevelNames: Set<String> = [],
        excludingTopLevelPrefixes: [String] = [],
        requireSingleLinkFiles: Bool = true,
        requireCurrentUserOwner: Bool = false
    ) -> CLIPayloadIdentity? {
        let fm = FileManager.default
        guard regularDirectory(root),
              let rootAttributes = try? fm.attributesOfItem(atPath: root.path),
              let rootPermissions = rootAttributes[.posixPermissions] as? NSNumber,
              rootPermissions.intValue & 0o022 == 0,
              !requireCurrentUserOwner || currentUserOwns(rootAttributes)
        else { return nil }
        var records: [Data] = [record(
            kind: 0x44,
            path: Data(),
            mode: UInt64(rootPermissions.intValue & 0o777),
            size: 0,
            digest: Data()
        )]

        func visit(_ directory: URL, relativeParent: String) -> Bool {
            guard let children = try? fm.contentsOfDirectory(
                at: directory,
                includingPropertiesForKeys: [.isDirectoryKey, .isRegularFileKey, .isSymbolicLinkKey],
                options: []
            ) else { return false }

            for child in children.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
                let name = child.lastPathComponent
                if relativeParent.isEmpty,
                   excludingTopLevelNames.contains(name)
                    || (relativeParent.isEmpty
                        && excludingTopLevelPrefixes.contains(where: { name.hasPrefix($0) })) {
                    continue
                }
                let relative = relativeParent.isEmpty ? name : "\(relativeParent)/\(name)"
                guard !relative.utf8.contains(0),
                      let values = try? child.resourceValues(
                          forKeys: [.isDirectoryKey, .isRegularFileKey, .isSymbolicLinkKey]
                      ),
                      values.isSymbolicLink != true,
                      let attributes = try? fm.attributesOfItem(atPath: child.path),
                      let permissions = attributes[.posixPermissions] as? NSNumber,
                      permissions.intValue & 0o022 == 0,
                      !requireCurrentUserOwner || currentUserOwns(attributes)
                else { return false }

                let mode = permissions.intValue & 0o777
                let pathBytes = Data(relative.utf8)
                if values.isDirectory == true {
                    records.append(record(
                        kind: 0x44,
                        path: pathBytes,
                        mode: UInt64(mode),
                        size: 0,
                        digest: Data()
                    ))
                    guard visit(child, relativeParent: relative) else { return false }
                } else if values.isRegularFile == true {
                    guard let size = attributes[.size] as? NSNumber,
                          let links = attributes[.referenceCount] as? NSNumber,
                          (!requireSingleLinkFiles || links.uint64Value == 1),
                          let digest = fileSHA256(child)
                    else { return false }
                    records.append(record(
                        kind: 0x46,
                        path: pathBytes,
                        mode: UInt64(mode),
                        size: size.uint64Value,
                        digest: digest
                    ))
                } else {
                    return false
                }
            }
            return true
        }

        guard visit(root, relativeParent: ""), !records.isEmpty else { return nil }
        var tree = SHA256()
        for record in records { tree.update(data: record) }
        return CLIPayloadIdentity(
            sha256: Data(tree.finalize()).map { String(format: "%02x", $0) }.joined(),
            entryCount: records.count
        )
    }

    private static func regularDirectory(_ url: URL) -> Bool {
        guard let values = try? url.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey]) else {
            return false
        }
        return values.isDirectory == true && values.isSymbolicLink != true
    }

    private static func currentUserOwns(_ attributes: [FileAttributeKey: Any]) -> Bool {
        guard let owner = attributes[.ownerAccountID] as? NSNumber else { return false }
        return owner.uint32Value == Darwin.geteuid()
    }

    static func fileSHA256(_ url: URL) -> Data? {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? handle.close() }
        var hasher = SHA256()
        do {
            while let chunk = try handle.read(upToCount: 1_048_576), !chunk.isEmpty {
                hasher.update(data: chunk)
            }
        } catch {
            return nil
        }
        return Data(hasher.finalize())
    }

    private static func record(
        kind: UInt8,
        path: Data,
        mode: UInt64,
        size: UInt64,
        digest: Data
    ) -> Data {
        var data = Data([kind])
        append(UInt64(path.count), to: &data)
        data.append(path)
        append(mode, to: &data)
        append(size, to: &data)
        append(UInt64(digest.count), to: &data)
        data.append(digest)
        return data
    }

    private static func append(_ value: UInt64, to data: inout Data) {
        var bigEndian = value.bigEndian
        withUnsafeBytes(of: &bigEndian) { data.append(contentsOf: $0) }
    }
}

// The one-click "Set up recording" flow for the packaged app: install the
// embedded standalone CLI to a stable location and run its `onboard` so a
// machine with no Python gets the MCP servers, hooks, and standing instructions
// configured for its coding agents. This is the exact CLI install flow
// (`agentacct onboard`) the docs describe — the app just drives it for a user
// who never opens a terminal. Idempotent: re-running is safe.
//
// Layout:
//   ~/.local/share/agentacct/cli/agentacct       <- stable launcher
//   ~/.local/share/agentacct/cli/.agentacct-app-target
//   ~/.local/share/agentacct/cli-versions/<id>/  <- immutable onedir CLI
//   ~/.local/bin/agentacct                       <- stable outer wrapper
// Each upgrade installs a complete new onedir and atomically changes only the
// target file. Old directories (and a legacy top-level `_internal`) are kept so
// already-running MCP/hook processes never see their side files replaced.

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
    private var pendingRuntimeRecovery = false

    private let fm = FileManager.default
    private let installer: (() throws -> URL)?
    private let processRunner: (URL, [String]) -> AsyncThrowingStream<String, Error>
    private let home: URL
    private let bundleResourceURL: URL?
    private let bundleInfoDictionary: [String: Any]?
    private let storeDirectory: () throws -> URL
    private let copyDirectory: (URL, URL) throws -> Void
    private let writeWrapper: (String, URL) throws -> Void
    private let transactionLockObserver: (() -> Void)?
    private let readinessPause: () async throws -> Void

    init(
        installer: (() throws -> URL)? = nil,
        processRunner: @escaping (URL, [String]) -> AsyncThrowingStream<String, Error> = ProcessRunner.run,
        homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser,
        bundleResourceURL: URL? = Bundle.main.resourceURL,
        bundleInfoDictionary: [String: Any]? = Bundle.main.infoDictionary,
        storeDirectory: @escaping () throws -> URL = GlanceClient.globalStoreDir,
        copyDirectory: ((URL, URL) throws -> Void)? = nil,
        writeWrapper: ((String, URL) throws -> Void)? = nil,
        transactionLockObserver: (() -> Void)? = nil,
        readinessPause: @escaping () async throws -> Void = { try await Task.sleep(nanoseconds: 500_000_000) }
    ) {
        self.installer = installer
        self.processRunner = processRunner
        home = homeDirectory
        self.bundleResourceURL = bundleResourceURL
        self.bundleInfoDictionary = bundleInfoDictionary
        self.storeDirectory = storeDirectory
        self.copyDirectory = copyDirectory ?? { source, destination in
            try FileManager.default.copyItem(at: source, to: destination)
        }
        self.writeWrapper = writeWrapper ?? { contents, destination in
            try contents.write(to: destination, atomically: true, encoding: .utf8)
        }
        self.transactionLockObserver = transactionLockObserver
        self.readinessPause = readinessPause
    }

    /// Deterministic state injection for offscreen review renders. Live setup
    /// still uses the production initializer above and reaches these states
    /// only through `setUp()`.
    init(preloaded phase: Phase, log: [String]) {
        self.phase = phase
        self.log = Array(log.suffix(200))
        installer = nil
        processRunner = ProcessRunner.run
        home = FileManager.default.homeDirectoryForCurrentUser
        bundleResourceURL = Bundle.main.resourceURL
        bundleInfoDictionary = Bundle.main.infoDictionary
        storeDirectory = GlanceClient.globalStoreDir
        copyDirectory = { source, destination in
            try FileManager.default.copyItem(at: source, to: destination)
        }
        writeWrapper = { contents, destination in
            try contents.write(to: destination, atomically: true, encoding: .utf8)
        }
        transactionLockObserver = nil
        readinessPause = { try await Task.sleep(nanoseconds: 500_000_000) }
    }

    // MARK: locations

    /// The CLI embedded in the app bundle (Contents/Resources/cli), if this is
    /// a packaged build. nil for a dev app built without the frozen CLI.
    var bundledCLIDir: URL? {
        packagedCLI?.directory
    }

    private var installedCLIDir: URL { home.appendingPathComponent(".local/share/agentacct/cli", isDirectory: true) }
    private var installedBinary: URL { installedCLIDir.appendingPathComponent("agentacct") }
    private var targetMarker: URL { installedCLIDir.appendingPathComponent(".agentacct-app-target") }
    private var versionsRoot: URL { home.appendingPathComponent(".local/share/agentacct/cli-versions", isDirectory: true) }
    private var versionsOwnershipMarker: URL { versionsRoot.appendingPathComponent(".agentacct-app-managed") }
    private var transactionLockFile: URL { home.appendingPathComponent(".local/share/agentacct/.agentacct-app-cli.lock") }
    private var runtimeTransactionJournal: URL {
        home.appendingPathComponent(".local/share/agentacct/.agentacct-app-cli-transaction")
    }
    private var onboardingPendingMarker: URL {
        home.appendingPathComponent(".local/share/agentacct/.agentacct-app-onboarding-pending")
    }
    private var managedAutostartFile: URL {
        home.appendingPathComponent("Library/LaunchAgents/dev.agentacct.runtime.plist")
    }
    private var binDir: URL { home.appendingPathComponent(".local/bin", isDirectory: true) }
    private var wrapper: URL { binDir.appendingPathComponent("agentacct") }

    /// True once the CLI has been installed to the stable location by this app.
    var isCLIInstalled: Bool {
        if installedCLIState != nil { return true }
        // A crash between the first install's three atomic file activations can
        // leave an executable launcher without its outer wrapper. Treat only
        // that precisely verified state as incomplete so setup is offered
        // again; an unrelated executable at the stable path is still left
        // alone and does not trigger an automatic takeover prompt.
        if recoverablePartialFirstInstall != nil { return false }
        return fm.isExecutableFile(atPath: installedBinary.path)
    }

    /// Show the first-run setup prompt only when we CAN install (packaged
    /// build) and haven't yet. A dev build (no embedded CLI) never prompts.
    var shouldOfferSetup: Bool {
        bundledCLIDir != nil && (!isCLIInstalled || onboardingPendingForCurrentInstall)
    }

    /// Automatic replacement is deliberately narrower than a manual setup:
    /// only a provenance-validated packaged CLI may replace a stable install
    /// whose exact wrapper proves that this app created and owns it.
    var shouldAutomaticallyUpgradeCLI: Bool {
        if automaticUpgradeContext != nil { return true }
        guard let packaged = packagedCLI,
              let partial = recoverablePartialFirstInstall
        else { return false }
        return partial.provenance != packaged.provenance
    }

    // MARK: run

    func setUp() async {
        guard case .idle = phase else { return }
        if pendingRuntimeRecovery {
            await retryRuntimeAfterFailedUpgrade()
            return
        }
        // A setup-sheet retry after an automatic upgrade failure must use the
        // same stop/swap/start transaction, never the first-run installer path
        // while an older managed runtime may still be active.
        if shouldAutomaticallyUpgradeCLI {
            let outcome = await upgradeInstalledCLIIfNeeded()
            switch outcome {
            case .upgraded, .notNeeded:
                phase = .done
            case .failed:
                break
            }
            return
        }
        log = []
        do {
            let transactionLock: CLITransactionLock?
            if installer == nil {
                transactionLock = try acquireTransactionLock()
            } else {
                transactionLock = nil
            }
            defer { withExtendedLifetime(transactionLock) {} }
            transactionLockObserver?()

            if installer == nil, pathExists(runtimeTransactionJournal) {
                let outcome = await recoverInterruptedRuntimeTransactionHoldingLock()
                if case .failed = outcome {
                    return
                }
                phase = .done
                return
            }

            try Task.checkCancellation()
            let executable: URL
            if let installer {
                executable = try installer()
            } else if let packaged = packagedCLI,
                      let recovered = try await recoverPartialFirstInstallIfNeeded(packaged: packaged) {
                executable = recovered
            } else if let packaged = packagedCLI,
                      installedCLIState?.provenance == packaged.provenance {
                // A prior onboarding attempt may have failed after the CLI was
                // installed successfully. Retry onboarding through that same
                // verified stable launcher instead of treating it as a fresh
                // install and colliding with our own files.
                executable = installedBinary
            } else {
                executable = try await installCLI()
            }
            try Task.checkCancellation()
            try await runOnboard(executable: executable)
            try Task.checkCancellation()
            if installer == nil {
                try completeOnboardingIfPending()
            }
            phase = .done
        } catch is CancellationError {
            phase = .idle
        } catch {
            phase = .failed(error.localizedDescription)
            append("error: \(error.localizedDescription)")
        }
    }

    func reset() { phase = .idle }

    enum AutomaticUpgradeOutcome: Equatable {
        case notNeeded
        case upgraded
        case failed(String)
    }

    /// Upgrade the app-owned stable CLI before the App's first local data request.
    /// This intentionally does not run `onboard`: existing registrations all
    /// point at the stable path, and `start` re-syncs integration metadata when
    /// its version changes. First-run `setUp()` remains install + onboard.
    @discardableResult
    func upgradeInstalledCLIIfNeeded() async -> AutomaticUpgradeOutcome {
        guard case .idle = phase else {
            return .notNeeded
        }
        guard pathExists(runtimeTransactionJournal)
                || automaticUpgradeContext != nil
                || recoverablePartialFirstInstall != nil
                || damagedManagedVersionedInstall
        else {
            return .notNeeded
        }

        log = []
        let transactionLock: CLITransactionLock
        do {
            transactionLock = try acquireTransactionLock()
        } catch {
            let message = error.localizedDescription
            phase = .failed(message)
            append("error: \(message)")
            return .failed(message)
        }
        defer { withExtendedLifetime(transactionLock) {} }
        transactionLockObserver?()

        do {
            // Do not repair or stage anything around an unrecognized launchd
            // unit. Journal recovery validates its own original/migrated copy.
            if pathExists(managedAutostartFile), !pathExists(runtimeTransactionJournal) {
                guard let installed = installedCLIState else {
                    throw SetupError.unsafeAutostart
                }
                _ = try managedAutostart(installed: installed, store: storeDirectory().standardizedFileURL)
            }
            if pathExists(runtimeTransactionJournal), recoverablePartialFirstInstall != nil {
                // The journal snapshots the exact wrapper identity. Recreating
                // a missing launcher here would manufacture a state that no
                // longer matches either side of the interrupted transaction.
                throw SetupError.damagedManagedInstall
            }
            if recoverablePartialFirstInstall != nil {
                _ = try await recoverPartialVersionedInstallIfNeeded()
            }
            if damagedManagedVersionedInstall {
                throw SetupError.damagedManagedInstall
            }
        } catch {
            let message = error.localizedDescription
            phase = .failed(message)
            append("error: \(message)")
            return .failed(message)
        }

        // Re-check after acquiring the cross-process lock. Another App window
        // can create or finish a persistent process-recovery journal between
        // the optimistic probe above and this lock acquisition.
        var recoveryOutcome: AutomaticUpgradeOutcome?
        if pathExists(runtimeTransactionJournal) {
            let outcome = await recoverInterruptedRuntimeTransactionHoldingLock()
            if case .failed = outcome {
                return outcome
            }
            recoveryOutcome = outcome
        }
        // Recovery can deliberately restore an older exact install when this
        // App does not match the transaction's staged target. Recompute the
        // normal upgrade context after the journal is gone so a newer App does
        // not expose local data through that older recorder until next launch.
        guard let context = automaticUpgradeContext else {
            return recoveryOutcome ?? .notNeeded
        }

        var stagedDirectory: URL?
        var preparedTarget: PreparedCLITarget?
        var replacement: CLIReplacement?
        var stoppedRuntime = false
        var newRuntimeMayBeRunning = false
        var runtimeStore: URL?
        var runtimeTransaction: RuntimeTransaction?
        var autostart: ManagedAutostart?
        do {
            let autostartStore = pathExists(managedAutostartFile)
                ? try storeDirectory().standardizedFileURL : nil
            if let autostartStore {
                autostart = try managedAutostart(installed: context.installed, store: autostartStore)
            }
            if let autostart { _ = try await verifiedLaunchdJob(autostart) }
            phase = .working("Updating the recorder…")
            append("Preparing the recorder bundled with this app")
            let staged = try stageCLI(context.packaged)
            stagedDirectory = staged

            // Re-check ownership after staging so a path changed concurrently
            // is never stopped or replaced based on a stale decision.
            guard installedCLIMatches(context.installed) else {
                throw SetupError.stableInstallChanged
            }

            let installedVersion = try await cliReleaseVersion(executable: installedBinary)
            guard installedCLIMatches(context.installed) else {
                throw SetupError.stableInstallChanged
            }
            guard stagedCLIMatches(staged, packaged: context.packaged) else {
                throw SetupError.invalidStagedCLI
            }
            let stagedVersion = try await cliReleaseVersion(
                executable: staged.appendingPathComponent("agentacct")
            )
            guard stagedCLIMatches(staged, packaged: context.packaged) else {
                throw SetupError.invalidStagedCLI
            }
            guard stagedVersion == context.packaged.releaseVersion else {
                throw SetupError.packagedVersionMismatch
            }
            let repairsInstalledBuild = stagedVersion == installedVersion
                && (context.installed.provenance != context.packaged.provenance
                    || context.installed.payloadIdentity != context.packaged.payloadIdentity)
            guard stagedVersion > installedVersion || repairsInstalledBuild else {
                try? fm.removeItem(at: staged)
                stagedDirectory = nil
                append("Kept the installed recorder because it is version \(installedVersion), not older than \(stagedVersion)")
                phase = .idle
                return .notNeeded
            }

            let prepared = try prepareVersionedTarget(
                staged,
                packaged: context.packaged,
                replacing: context.installed
            )
            stagedDirectory = nil
            preparedTarget = prepared

            let resolvedStore = try (autostartStore ?? storeDirectory().standardizedFileURL)
            runtimeStore = resolvedStore
            guard installedCLIMatches(context.installed) else {
                throw SetupError.stableInstallChanged
            }
            let runtimeWasRunning = try await managedRuntimeIsRunning(
                executable: installedBinary,
                store: resolvedStore
            )
            guard installedCLIMatches(context.installed), autostartMatches(autostart) else {
                throw SetupError.stableInstallChanged
            }
            if runtimeWasRunning || autostart != nil {
                phase = .working("Stopping the current recorder…")
                append("Stopping the app-managed recorder before replacement")
                var transaction = RuntimeTransaction(
                    schema: 1,
                    phase: .stopRequested,
                    wasRunning: runtimeWasRunning,
                    storePath: resolvedStore.path,
                    oldInstall: snapshot(context.installed),
                    newTargetPath: prepared.target.path,
                    newBinaryIdentity: prepared.binaryIdentity,
                    newPayloadIdentity: prepared.payloadIdentity,
                    newProvenance: context.packaged.provenance,
                    newReleaseVersion: context.packaged.releaseVersion.description,
                    autostart: autostart
                )
                try writeNewRuntimeTransaction(transaction)
                runtimeTransaction = transaction
                // A refused stop makes a following start a harmless
                // idempotent ensure; a partial stop must be recovered.
                stoppedRuntime = true
                if let autostart {
                    try await stopAutostartSupervisor(autostart)
                }
                guard installedCLIMatches(context.installed), autostartMatches(autostart) else {
                    throw SetupError.stableInstallChanged
                }
                try await runCommand(
                    executable: installedBinary,
                    arguments: try runtimeArguments(command: "stop", store: resolvedStore)
                )
                guard installedCLIMatches(context.installed), autostartMatches(autostart) else {
                    throw SetupError.stableInstallChanged
                }
                transaction = try updateRuntimeTransaction(transaction, phase: .oldStopped)
                runtimeTransaction = transaction
            }

            try Task.checkCancellation()
            guard installedCLIMatches(context.installed), autostartMatches(autostart) else {
                throw SetupError.stableInstallChanged
            }

            phase = .working("Installing the updated recorder…")
            let activated = try activatePreparedCLI(
                prepared,
                packaged: context.packaged,
                replacing: context.installed
            )
            preparedTarget = nil
            replacement = activated
            if let transaction = runtimeTransaction {
                runtimeTransaction = try updateRuntimeTransaction(transaction, phase: .targetSwitched)
            }

            if runtimeWasRunning || autostart != nil {
                phase = .working("Restarting the recorder…")
                append("Starting the updated app-managed recorder")
                newRuntimeMayBeRunning = true
                guard installedCLIMatches(activated.newInstall) else {
                    throw SetupError.stableInstallChanged
                }
                if let autostart {
                    try replaceAutostartContents(autostart, useUpdated: true)
                    try await startAutostartSupervisor(autostart, useUpdated: true, install: activated.newInstall, store: resolvedStore)
                } else {
                    try await runCommand(
                        executable: installedBinary,
                        arguments: try runtimeArguments(command: "start", store: resolvedStore)
                    )
                }
            }

            try Task.checkCancellation()
            guard installedCLIMatches(activated.newInstall), autostartMatches(autostart) else {
                throw SetupError.stableInstallChanged
            }
            if let transaction = runtimeTransaction {
                try removeRuntimeTransaction(transaction)
                runtimeTransaction = nil
            }
            replacement = nil
            append("Updated the app-managed CLI")
            pendingRuntimeRecovery = false
            phase = .idle
            return .upgraded
        } catch {
            if let stagedDirectory {
                try? fm.removeItem(at: stagedDirectory)
            }
            var recoveryFailures: [String] = []
            var mayRollBack = true
            var runtimeRecovered = !stoppedRuntime
            if !autostartMatches(autostart) {
                recoveryFailures.append("the launchd configuration changed, so its supervisor and recorder files were preserved")
                mayRollBack = false
                runtimeRecovered = false
            }
            // `start` may have spawned a managed child before its output stream
            // failed. Recovery only invokes a CLI whose exact target identity
            // is still selected. A failed stop leaves the new version selected
            // and retained; no unknown path is executed or deleted.
            if let replacement, newRuntimeMayBeRunning, mayRollBack {
                if installedCLIMatches(replacement.newInstall) {
                    do {
                        if let autostart {
                            try await stopAutostartSupervisor(autostart)
                        }
                        try await runRecoveryCommand(
                            executable: installedBinary,
                            arguments: try runtimeArguments(command: "stop", store: runtimeStore)
                        )
                    } catch {
                        recoveryFailures.append("stopping the partial updated recorder failed: \(error.localizedDescription)")
                        mayRollBack = false
                    }
                    if mayRollBack, !installedCLIMatches(replacement.newInstall) {
                        recoveryFailures.append("the updated recorder identity changed during recovery, so its target was preserved")
                        mayRollBack = false
                    }
                } else {
                    recoveryFailures.append("the updated recorder identity changed before recovery, so no unknown binary was run")
                    mayRollBack = false
                }
            }

            var restoredPreviousInstall = replacement == nil
            if let replacement, mayRollBack {
                if installedCLIMatches(replacement.newInstall) {
                    do {
                        try rollback(replacement)
                        restoredPreviousInstall = installedCLIMatches(context.installed)
                        if !restoredPreviousInstall {
                            recoveryFailures.append("the previous recorder identity could not be re-verified after rollback")
                        }
                    } catch {
                        recoveryFailures.append("restoring the previous CLI failed: \(error.localizedDescription)")
                    }
                } else {
                    recoveryFailures.append("the stable recorder identity changed, so its preserved backup was not restored over unknown files")
                }
            }

            if stoppedRuntime, restoredPreviousInstall, mayRollBack {
                if installedCLIMatches(context.installed) {
                    do {
                        if let autostart {
                            try await stopAutostartSupervisor(autostart)
                            try replaceAutostartContents(autostart, useUpdated: false)
                            try await startAutostartSupervisor(autostart, useUpdated: false, install: context.installed, store: try runtimeStore ?? storeDirectory())
                        } else {
                            try await restartPreviousRuntimeForRecovery(store: runtimeStore)
                        }
                        runtimeRecovered = true
                    } catch {
                        recoveryFailures.append("restarting the previous recorder failed: \(error.localizedDescription)")
                    }
                } else {
                    recoveryFailures.append("the previous recorder identity could not be re-verified, so no unknown binary was run")
                }
            } else if stoppedRuntime, replacement != nil, !restoredPreviousInstall {
                recoveryFailures.append("the previous recorder was not restarted because the safe rollback did not complete")
            }

            if runtimeRecovered, let transaction = runtimeTransaction {
                do {
                    try removeRuntimeTransaction(transaction)
                    runtimeTransaction = nil
                } catch {
                    runtimeRecovered = false
                    recoveryFailures.append("clearing the persistent recorder recovery journal failed: \(error.localizedDescription)")
                }
            }

            if let preparedTarget, runtimeTransaction == nil {
                try? discardPreparedTargetIfUnselected(
                    preparedTarget,
                    packaged: context.packaged
                )
            }

            pendingRuntimeRecovery = stoppedRuntime && !runtimeRecovered

            let message = ([error.localizedDescription] + recoveryFailures).joined(separator: " ")
            phase = .failed(message)
            append("error: \(message)")
            return .failed(message)
        }
    }

    /// Resume a transaction interrupted by an App crash or process termination,
    /// including SIGKILL, after a previously running managed runtime was
    /// stopped. This does not claim storage durability across sudden power loss:
    /// the owner-only journal names both exact file identities, but the current
    /// implementation does not fsync the file and parent directory. No
    /// command is run until the currently selected install matches one of the
    /// recorded states, and the journal is removed only after restart succeeds.
    private func recoverInterruptedRuntimeTransaction() async -> AutomaticUpgradeOutcome {
        let transactionLock: CLITransactionLock
        do {
            transactionLock = try acquireTransactionLock()
        } catch {
            let message = error.localizedDescription
            phase = .failed(message)
            append("error: \(message)")
            return .failed(message)
        }
        defer { withExtendedLifetime(transactionLock) {} }
        return await recoverInterruptedRuntimeTransactionHoldingLock()
    }

    private func recoverInterruptedRuntimeTransactionHoldingLock() async -> AutomaticUpgradeOutcome {
        log = []
        do {
            phase = .working("Recovering an interrupted recorder update…")
            var transaction = try readRuntimeTransaction()
            let resolvedStore = try storeDirectory().standardizedFileURL
            guard resolvedStore.path == transaction.storePath else {
                throw SetupError.runtimeJournalStoreChanged
            }
            transaction = try updateRuntimeTransaction(transaction, phase: .recovering)

            guard let current = installedCLIState,
                  let expectedNew = expectedNewInstall(from: transaction),
                  case .versioned(let expectedNewTarget) = expectedNew.layout
            else { throw SetupError.recoveryIdentityChanged }
            let currentSnapshot = snapshot(current)
            let currentIsOld = currentSnapshot == transaction.oldInstall
            let currentIsNew = current == expectedNew
            let currentIsInterruptedLegacy = currentInstallIsInterruptedLegacySwitch(
                current,
                transaction: transaction
            )
            guard currentIsOld || currentIsNew || currentIsInterruptedLegacy else {
                throw SetupError.recoveryIdentityChanged
            }

            if let autostart = transaction.autostart {
                try await stopAutostartSupervisor(autostart)
            } else if pathExists(managedAutostartFile) {
                // A new supervisor appeared after this older journal was
                // written; its process ownership is outside the transaction.
                throw SetupError.unsafeAutostart
            }

            append("Stopping the verified recorder before process-crash recovery")
            guard installedCLIMatches(current) else {
                throw SetupError.recoveryIdentityChanged
            }
            try await runRecoveryCommand(
                executable: installedBinary,
                arguments: try runtimeArguments(command: "stop", store: resolvedStore)
            )
            guard installedCLIMatches(current) else {
                throw SetupError.recoveryIdentityChanged
            }

            var selected = current
            var selectedNew = currentIsNew
            if currentIsOld || currentIsInterruptedLegacy {
                let matchingPackagedCLI = packagedCLI.flatMap { packaged -> PackagedCLI? in
                    guard packaged.provenance == transaction.newProvenance,
                          packaged.releaseVersion.description == transaction.newReleaseVersion
                    else { return nil }
                    return packaged
                }
                if let packaged = matchingPackagedCLI {
                    let prepared = PreparedCLITarget(
                        target: expectedNewTarget,
                        binaryIdentity: transaction.newBinaryIdentity,
                        payloadIdentity: transaction.newPayloadIdentity
                    )
                    let activated = try activatePreparedCLI(
                        prepared,
                        packaged: packaged,
                        replacing: current
                    )
                    selected = activated.newInstall
                    selectedNew = true
                    transaction = try updateRuntimeTransaction(transaction, phase: .targetSwitched)
                } else if currentIsInterruptedLegacy {
                    // An older/different App cannot finish a newer legacy
                    // migration. Restore only the exact marker recorded for
                    // the old legacy layout, then restart that exact binary.
                    guard case .legacy = current.layout else {
                        throw SetupError.recoveryIdentityChanged
                    }
                    if let oldPendingPath = transaction.oldInstall.pendingTargetPath {
                        guard let oldPending = validatedVersionTarget(path: oldPendingPath),
                              verifiedRecordedPayloadIdentity(for: oldPending) != nil
                        else {
                            throw SetupError.recoveryIdentityChanged
                        }
                        try replaceTargetMarker(from: expectedNewTarget, with: oldPending)
                    } else {
                        guard readSingleLine(targetMarker) == transaction.newTargetPath else {
                            throw SetupError.recoveryIdentityChanged
                        }
                        try fm.removeItem(at: targetMarker)
                    }
                    guard let restored = installedCLIState,
                          snapshot(restored) == transaction.oldInstall
                    else { throw SetupError.recoveryIdentityChanged }
                    selected = restored
                    selectedNew = false
                }
            }

            guard installedCLIMatches(selected) else {
                throw SetupError.recoveryIdentityChanged
            }
            append("Restarting the verified app-managed recorder")
            if let autostart = transaction.autostart {
                try replaceAutostartContents(autostart, useUpdated: selectedNew)
                try await startAutostartSupervisor(autostart, useUpdated: selectedNew, install: selected, store: resolvedStore)
            } else {
                try await runRecoveryCommand(
                    executable: installedBinary,
                    arguments: try runtimeArguments(command: "start", store: resolvedStore)
                )
            }
            guard installedCLIMatches(selected) else {
                throw SetupError.recoveryIdentityChanged
            }
            try removeRuntimeTransaction(transaction)

            pendingRuntimeRecovery = false
            append("Recovered the interrupted recorder update")
            phase = .idle
            return selectedNew ? .upgraded : .notNeeded
        } catch {
            pendingRuntimeRecovery = true
            let message = error.localizedDescription
            phase = .failed(message)
            append("error: \(message)")
            return .failed(message)
        }
    }

    private func retryRuntimeAfterFailedUpgrade() async {
        if pathExists(runtimeTransactionJournal) {
            let outcome = await recoverInterruptedRuntimeTransaction()
            if case .failed = outcome {
                return
            } else {
                // Recovery returns to idle so the setup sheet can close through
                // its ordinary success state.
                phase = .done
            }
            return
        }
        log = []
        do {
            let transactionLock = try acquireTransactionLock()
            defer { withExtendedLifetime(transactionLock) {} }
            transactionLockObserver?()

            if pathExists(runtimeTransactionJournal) {
                let outcome = await recoverInterruptedRuntimeTransactionHoldingLock()
                if case .failed = outcome {
                    return
                }
                phase = .done
                return
            }

            phase = .working("Recovering the recorder…")
            guard let expected = installedCLIState else {
                throw SetupError.stableInstallChanged
            }
            guard installedCLIMatches(expected) else {
                throw SetupError.stableInstallChanged
            }
            let isRunning = try await managedRuntimeIsRunning(executable: installedBinary)
            guard installedCLIMatches(expected) else {
                throw SetupError.stableInstallChanged
            }
            if !isRunning {
                append("Starting the verified app-managed recorder")
                try await runCommand(
                    executable: installedBinary,
                    arguments: try runtimeArguments(command: "start")
                )
                guard installedCLIMatches(expected) else {
                    throw SetupError.stableInstallChanged
                }
            }
            pendingRuntimeRecovery = false
            append("Recovered the app-managed recorder runtime")
            phase = .done
        } catch {
            let message = error.localizedDescription
            phase = .failed(message)
            append("error: \(message)")
        }
    }

    // MARK: steps

    private func installCLI() async throws -> URL {
        guard let packaged = packagedCLI else {
            throw SetupError.noEmbeddedCLI
        }
        phase = .working("Installing the recorder…")
        append("Installing CLI to ~/.local/share/agentacct/cli")
        let staged = try stageCLI(packaged)
        var preparedTarget: PreparedCLITarget?
        do {
            guard stagedCLIMatches(staged, packaged: packaged) else {
                throw SetupError.invalidStagedCLI
            }
            let stagedVersion = try await cliReleaseVersion(
                executable: staged.appendingPathComponent("agentacct")
            )
            guard stagedCLIMatches(staged, packaged: packaged) else {
                throw SetupError.invalidStagedCLI
            }
            guard stagedVersion == packaged.releaseVersion else {
                throw SetupError.packagedVersionMismatch
            }
            let prepared = try prepareVersionedTarget(staged, packaged: packaged, replacing: nil)
            preparedTarget = prepared
            try ensureOnboardingPending()
            _ = try activatePreparedCLI(prepared, packaged: packaged, replacing: nil)
            preparedTarget = nil
        } catch {
            try? fm.removeItem(at: staged)
            if let preparedTarget {
                try? discardPreparedTargetIfUnselected(preparedTarget, packaged: packaged)
            }
            throw error
        }
        append("Wrote ~/.local/bin/agentacct")
        return installedBinary
    }

    private func runOnboard(executable: URL) async throws {
        phase = .working("Configuring your coding agents…")
        append("Running: agentacct onboard --agent auto --yes")
        // Run from the INSTALLED binary so onboard stamps the stable installed
        // path into every hook/MCP config (verified end-to-end).
        let expectedInstall = executable.standardizedFileURL == installedBinary.standardizedFileURL
            ? installedCLIState
            : nil
        if executable.standardizedFileURL == installedBinary.standardizedFileURL,
           expectedInstall == nil {
            throw SetupError.stableInstallChanged
        }
        let stream = processRunner(executable, ["onboard", "--agent", "auto", "--yes"])
        for try await line in stream {
            append(line)
        }
        if let expectedInstall, !installedCLIMatches(expectedInstall) {
            throw SetupError.stableInstallChanged
        }
    }

    private func append(_ line: String) {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        log.append(trimmed)
        if log.count > 200 { log.removeFirst(log.count - 200) }
    }

    // MARK: packaged/install identity

    private struct CLIProvenance: Codable, Equatable {
        let commit: String
        let description: String
    }

    private struct PackagedCLI {
        let directory: URL
        let provenance: CLIProvenance
        let releaseVersion: ReleaseVersion
        let payloadIdentity: CLIPayloadIdentity
    }

    private struct AutomaticUpgradeContext {
        let packaged: PackagedCLI
        let installed: InstalledCLI
    }

    private struct FileIdentity: Codable, Equatable {
        let device: UInt64
        let inode: UInt64
        let size: UInt64
    }

    private struct StrongFileIdentity: Equatable {
        let file: FileIdentity
        let owner: UInt32
        let mode: Int
        let sha256: Data
    }

    private struct InstalledCLI: Equatable {
        enum Layout: Equatable {
            case legacy(pendingTarget: URL?)
            case versioned(target: URL)
        }

        let layout: Layout
        let provenance: CLIProvenance?
        let binaryIdentity: FileIdentity
        let payloadIdentity: CLIPayloadIdentity
        let wrapperIdentity: FileIdentity
    }

    private var packagedCLI: PackagedCLI? {
        guard let resourceURL = bundleResourceURL,
              normalizedInfoString("CFBundlePackageType") == "APPL",
              normalizedInfoString("CFBundleIdentifier") == "dev.agentacct.app",
              let appCommit = normalizedInfoString("AgentacctGitCommit"),
              let appDescription = normalizedInfoString("AgentacctBuildDescription"),
              let appVersionString = normalizedInfoString("CFBundleShortVersionString"),
              let appVersion = ReleaseVersion(appVersionString),
              isValidCommit(appCommit),
              !appDescription.hasSuffix("-dirty")
        else { return nil }

        let directory = resourceURL.appendingPathComponent("cli", isDirectory: true)
        guard isRegularDirectory(directory),
              isRegularExecutable(directory.appendingPathComponent("agentacct")),
              let provenance = readProvenance(from: directory),
              provenance.commit == appCommit,
              provenance.description == appDescription,
              let payloadIdentity = CLIPayloadInspector.identity(at: directory)
        else { return nil }
        return PackagedCLI(
            directory: directory,
            provenance: provenance,
            releaseVersion: appVersion,
            payloadIdentity: payloadIdentity
        )
    }

    private var automaticUpgradeContext: AutomaticUpgradeContext? {
        guard let packaged = packagedCLI,
              let installed = installedCLIState,
              installed.provenance != packaged.provenance
                || installed.payloadIdentity != packaged.payloadIdentity
        else { return nil }
        return AutomaticUpgradeContext(packaged: packaged, installed: installed)
    }

    private var installedCLIState: InstalledCLI? {
        guard isCurrentUserOwnedDirectoryWithoutSharedWrites(installedCLIDir),
              isCurrentUserOwnedExecutableWithoutSharedWrites(installedBinary),
              isCurrentUserOwnedDirectoryWithoutSharedWrites(binDir),
              isCurrentUserOwnedExecutableWithoutSharedWrites(wrapper),
              readSmallText(wrapper) == wrapperContents,
              let binaryIdentity = fileIdentity(installedBinary),
              let wrapperIdentity = fileIdentity(wrapper)
        else { return nil }

        if readSmallText(installedBinary) == stableLauncherContents {
            guard versionsRootIsAppOwned,
                  let target = readVersionedTarget(),
                  let targetBinaryIdentity = fileIdentity(target.appendingPathComponent("agentacct")),
                  let targetPayloadIdentity = verifiedRecordedPayloadIdentity(for: target)
            else { return nil }
            return InstalledCLI(
                layout: .versioned(target: target),
                provenance: readProvenance(from: target),
                binaryIdentity: targetBinaryIdentity,
                payloadIdentity: targetPayloadIdentity,
                wrapperIdentity: wrapperIdentity
            )
        }

        // Older App releases installed the frozen onedir directly at the
        // stable path. The exact outer wrapper is their backwards-compatible
        // ownership proof. An unexplained target marker makes the mixed layout
        // unsafe instead of being silently overwritten.
        let pendingTarget: URL?
        if pathExists(targetMarker) {
            guard versionsRootIsAppOwned, let target = readVersionedTarget() else { return nil }
            pendingTarget = target
        } else {
            pendingTarget = nil
        }
        guard let payloadIdentity = legacyPayloadIdentity() else { return nil }
        return InstalledCLI(
            layout: .legacy(pendingTarget: pendingTarget),
            provenance: readProvenance(from: installedCLIDir),
            binaryIdentity: binaryIdentity,
            payloadIdentity: payloadIdentity,
            wrapperIdentity: wrapperIdentity
        )
    }

    private struct PartialFirstInstall: Equatable {
        let target: URL
        let provenance: CLIProvenance
        let targetBinaryIdentity: FileIdentity
        let targetPayloadIdentity: CLIPayloadIdentity
        let launcherMissing: Bool
        let wrapperMissing: Bool
    }

    /// Recognizes only states that the first-install transaction itself can
    /// create: a verified packaged target selected by the app-owned marker,
    /// with the two stable launchers either exact or absent. Unknown files are
    /// never classified as recoverable and are therefore never overwritten.
    private var recoverablePartialFirstInstall: PartialFirstInstall? {
        guard isCurrentUserOwnedDirectoryWithoutSharedWrites(installedCLIDir),
              (!pathExists(binDir)
                || isCurrentUserOwnedDirectoryWithoutSharedWrites(binDir)),
              versionsRootIsAppOwned,
              let target = readVersionedTarget(),
              let provenance = readProvenance(from: target),
              let targetBinaryIdentity = fileIdentity(target.appendingPathComponent("agentacct")),
              let targetPayloadIdentity = verifiedRecordedPayloadIdentity(for: target)
        else { return nil }

        let launcherMissing = !pathExists(installedBinary)
        guard launcherMissing || (
            isCurrentUserOwnedExecutableWithoutSharedWrites(installedBinary)
                && readSmallText(installedBinary) == stableLauncherContents
        ) else { return nil }

        let wrapperMissing = !pathExists(wrapper)
        guard wrapperMissing || (
            isCurrentUserOwnedExecutableWithoutSharedWrites(wrapper)
                && readSmallText(wrapper) == wrapperContents
        ) else { return nil }
        guard launcherMissing || wrapperMissing else { return nil }
        // An exact surviving launcher or wrapper anchors ownership for an
        // older target. If both disappeared, only the current signed App's
        // exact packaged provenance is sufficient to identify a first-install
        // crash; an older target with neither launcher fails closed.
        if launcherMissing, wrapperMissing,
           let packaged = packagedCLI {
            guard packaged.provenance == provenance,
                  packaged.payloadIdentity == targetPayloadIdentity
            else { return nil }
        } else if launcherMissing, wrapperMissing {
            return nil
        }

        return PartialFirstInstall(
            target: target,
            provenance: provenance,
            targetBinaryIdentity: targetBinaryIdentity,
            targetPayloadIdentity: targetPayloadIdentity,
            launcherMissing: launcherMissing,
            wrapperMissing: wrapperMissing
        )
    }

    private func partialTargetStillMatches(
        _ partial: PartialFirstInstall
    ) -> Bool {
        readVersionedTarget() == partial.target
            && fileIdentity(partial.target.appendingPathComponent("agentacct")) == partial.targetBinaryIdentity
            && verifiedRecordedPayloadIdentity(for: partial.target) == partial.targetPayloadIdentity
            && readProvenance(from: partial.target) == partial.provenance
    }

    private func recoverPartialFirstInstallIfNeeded(packaged: PackagedCLI) async throws -> URL? {
        guard let partial = recoverablePartialFirstInstall,
              partial.provenance == packaged.provenance
        else { return nil }
        let installed = try await recoverPartialVersionedInstallIfNeeded()
        let targetVersion = try await cliReleaseVersion(executable: installedBinary)
        guard targetVersion == packaged.releaseVersion else {
            throw SetupError.packagedVersionMismatch
        }
        guard installedCLIMatches(installed) else { throw SetupError.stableInstallChanged }
        return installedBinary
    }

    private func recoverPartialVersionedInstallIfNeeded() async throws -> InstalledCLI {
        guard let partial = recoverablePartialFirstInstall else {
            throw SetupError.stableInstallChanged
        }
        append("Finishing an interrupted recorder installation")

        // Validate that the exact selected target is still an executable CLI
        // before repairing either stable launcher. The release need not match
        // this App yet: an older app-owned target is repaired first and then
        // flows through the ordinary upgrade comparison.
        guard partialTargetStillMatches(partial) else { throw SetupError.stableInstallChanged }
        _ = try await cliReleaseVersion(
            executable: partial.target.appendingPathComponent("agentacct")
        )
        guard partialTargetStillMatches(partial) else { throw SetupError.stableInstallChanged }

        if !pathExists(binDir) {
            try fm.createDirectory(at: binDir, withIntermediateDirectories: true)
            try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: binDir.path)
        }
        guard isCurrentUserOwnedDirectoryWithoutSharedWrites(binDir) else {
            throw SetupError.unsafeWrapper
        }

        if partial.launcherMissing {
            guard !pathExists(installedBinary),
                  partialTargetStillMatches(partial)
            else { throw SetupError.stableInstallChanged }
            let launcherTemp = try prepareManagedTemp(
                contents: stableLauncherContents,
                in: installedCLIDir,
                permissions: 0o755,
                useInjectedWriter: true
            )
            defer { try? fm.removeItem(at: launcherTemp) }
            try installPreparedFileWithoutReplacing(launcherTemp, at: installedBinary)
        }

        guard isCurrentUserOwnedExecutableWithoutSharedWrites(installedBinary),
              readSmallText(installedBinary) == stableLauncherContents,
              partialTargetStillMatches(partial)
        else { throw SetupError.stableInstallChanged }

        if partial.wrapperMissing {
            guard !pathExists(wrapper) else { throw SetupError.stableInstallChanged }
            let wrapperTemp = try prepareManagedTemp(
                contents: wrapperContents,
                in: binDir,
                permissions: 0o755,
                useInjectedWriter: true
            )
            defer { try? fm.removeItem(at: wrapperTemp) }
            try installPreparedFileWithoutReplacing(wrapperTemp, at: wrapper)
        }

        guard let installed = installedCLIState,
              installed.provenance == partial.provenance,
              installed.layout == .versioned(target: partial.target),
              installed.binaryIdentity == partial.targetBinaryIdentity,
              installed.payloadIdentity == partial.targetPayloadIdentity
        else { throw SetupError.stableInstallChanged }
        append("Recovered the interrupted recorder installation")
        return installed
    }

    /// Recognize only an already app-owned versioned layout whose marker names
    /// the managed versions root but whose selected target is incomplete. This
    /// makes corruption visible to the startup readiness gate instead of
    /// silently treating the stable shell launcher as a usable installation.
    private var damagedManagedVersionedInstall: Bool {
        guard isRegularDirectory(installedCLIDir),
              isRegularFile(targetMarker),
              let raw = readSingleLine(targetMarker),
              (raw as NSString).isAbsolutePath
        else { return false }
        let target = URL(fileURLWithPath: raw, isDirectory: true)
        guard target.deletingLastPathComponent().standardizedFileURL == versionsRoot.standardizedFileURL else {
            return false
        }
        let launcherOwned = !pathExists(installedBinary)
            || (isRegularExecutable(installedBinary) && readSmallText(installedBinary) == stableLauncherContents)
        let wrapperOwned = !pathExists(wrapper)
            || (isRegularExecutable(wrapper) && readSmallText(wrapper) == wrapperContents)
        guard launcherOwned && wrapperOwned else { return false }
        if !isCurrentUserOwnedDirectoryWithoutSharedWrites(installedCLIDir)
            || !isOwnerOnlyRegularFile(targetMarker)
            || (pathExists(installedBinary)
                && !isCurrentUserOwnedExecutableWithoutSharedWrites(installedBinary))
            || (pathExists(binDir)
                && !isCurrentUserOwnedDirectoryWithoutSharedWrites(binDir))
            || (pathExists(wrapper)
                && !isCurrentUserOwnedExecutableWithoutSharedWrites(wrapper)) {
            return true
        }
        guard versionsRootIsAppOwned else {
            // Exact app launchers plus the exact ownership marker identify a
            // managed layout whose root permissions/ownership drifted. Make
            // that state block local-data readiness; never treat it as an
            // unrelated install and continue without an update.
            return isRegularFile(versionsOwnershipMarker)
                && readSmallText(versionsOwnershipMarker) == versionsOwnershipContents
        }
        return readVersionedTarget() == nil
            || (installedCLIState == nil && recoverablePartialFirstInstall == nil)
    }

    private func installedCLIMatches(_ expected: InstalledCLI) -> Bool {
        installedCLIState == expected
    }

    /// Re-validates a retained versioned target without requiring it to be
    /// selected by the stable marker. Rollback may retarget only to this exact
    /// payload, provenance, binary inode, and unchanged outer wrapper.
    private func retainedVersionedInstallMatches(_ expected: InstalledCLI) -> Bool {
        guard case .versioned(let target) = expected.layout,
              let provenance = expected.provenance,
              versionsRootIsAppOwned,
              target.deletingLastPathComponent().standardizedFileURL
                == versionsRoot.standardizedFileURL,
              isRegularDirectory(target),
              isCurrentUserOwnedExecutableWithoutSharedWrites(
                  target.appendingPathComponent("agentacct")
              ),
              fileIdentity(target.appendingPathComponent("agentacct")) == expected.binaryIdentity,
              verifiedRecordedPayloadIdentity(for: target) == expected.payloadIdentity,
              readProvenance(from: target) == provenance,
              isCurrentUserOwnedExecutableWithoutSharedWrites(installedBinary),
              readSmallText(installedBinary) == stableLauncherContents,
              isCurrentUserOwnedExecutableWithoutSharedWrites(wrapper),
              readSmallText(wrapper) == wrapperContents,
              fileIdentity(wrapper) == expected.wrapperIdentity
        else { return false }
        return true
    }

    private var wrapperContents: String {
        "#!/bin/sh\nexec \"\(installedBinary.path)\" \"$@\"\n"
    }

    private var stableLauncherContents: String {
        """
        #!/bin/sh
        PATH=\(shellQuote(binDir.path)):"${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"
        export PATH
        target_file=\(shellQuote(targetMarker.path))
        IFS= read -r target < "$target_file" || exit 1
        [ -n "$target" ] || exit 1
        exec "$target/agentacct" "$@"

        """
    }

    private var versionsOwnershipContents: String {
        "agentacct-macos-app-cli-versions-v1\n"
    }

    private var onboardingPendingContents: String {
        "agentacct-macos-app-onboarding-pending-v1\n"
    }

    private var versionsRootIsAppOwned: Bool {
        isCurrentUserOwnedDirectoryWithoutSharedWrites(versionsRoot)
            && isOwnerOnlyRegularFile(versionsOwnershipMarker)
            && (try? String(contentsOf: versionsOwnershipMarker, encoding: .utf8)) == versionsOwnershipContents
    }

    private struct TargetPayloadRecord: Codable, Equatable {
        let schema: Int
        let targetName: String
        let identity: CLIPayloadIdentity
    }

    private func payloadIdentityMarker(for target: URL) -> URL {
        versionsRoot.appendingPathComponent(".\(target.lastPathComponent).payload.json")
    }

    private func legacyPayloadIdentity() -> CLIPayloadIdentity? {
        CLIPayloadInspector.identity(
            at: installedCLIDir,
            excludingTopLevelNames: [targetMarker.lastPathComponent],
            excludingTopLevelPrefixes: [
                ".agentacct-legacy-",
                ".agentacct-restore-",
                ".agentacct-write-",
            ],
            requireSingleLinkFiles: false,
            requireCurrentUserOwner: true
        )
    }

    private func legacyRollbackSideIdentity() -> CLIPayloadIdentity? {
        CLIPayloadInspector.identity(
            at: installedCLIDir,
            excludingTopLevelNames: [
                installedBinary.lastPathComponent,
                targetMarker.lastPathComponent,
            ],
            excludingTopLevelPrefixes: [
                ".agentacct-legacy-",
                ".agentacct-restore-",
                ".agentacct-write-",
            ],
            requireSingleLinkFiles: false,
            requireCurrentUserOwner: true
        )
    }

    private func readRecordedPayloadIdentity(for target: URL) -> CLIPayloadIdentity? {
        let marker = payloadIdentityMarker(for: target)
        guard target.deletingLastPathComponent().standardizedFileURL == versionsRoot.standardizedFileURL,
              isOwnerOnlyRegularFile(marker),
              let contents = readSmallText(marker),
              let data = contents.data(using: .utf8),
              let record = try? JSONDecoder().decode(TargetPayloadRecord.self, from: data),
              record.schema == 1,
              record.targetName == target.lastPathComponent,
              record.identity.isValid
        else { return nil }
        return record.identity
    }

    private func verifiedRecordedPayloadIdentity(for target: URL) -> CLIPayloadIdentity? {
        guard let expected = readRecordedPayloadIdentity(for: target),
              CLIPayloadInspector.identity(at: target, requireCurrentUserOwner: true) == expected
        else { return nil }
        return expected
    }

    private func writeRecordedPayloadIdentity(
        _ identity: CLIPayloadIdentity,
        for target: URL
    ) throws {
        guard identity.isValid,
              target.deletingLastPathComponent().standardizedFileURL == versionsRoot.standardizedFileURL
        else { throw SetupError.invalidStagedCLI }
        let record = TargetPayloadRecord(
            schema: 1,
            targetName: target.lastPathComponent,
            identity: identity
        )
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let contents = String(decoding: try encoder.encode(record), as: UTF8.self) + "\n"
        try writeNewManagedFile(
            contents: contents,
            destination: payloadIdentityMarker(for: target),
            permissions: 0o600
        )
        guard verifiedRecordedPayloadIdentity(for: target) == identity else {
            throw SetupError.invalidStagedCLI
        }
    }

    private var onboardingPendingIsAppOwned: Bool {
        guard isOwnerOnlyRegularFile(onboardingPendingMarker) else { return false }
        return readSmallText(onboardingPendingMarker) == onboardingPendingContents
    }

    private var onboardingPendingForCurrentInstall: Bool {
        guard onboardingPendingIsAppOwned,
              let packaged = packagedCLI,
              installedCLIState?.provenance == packaged.provenance
        else { return false }
        return true
    }

    private func ensureOnboardingPending() throws {
        if pathExists(onboardingPendingMarker) {
            guard onboardingPendingIsAppOwned else {
                throw SetupError.unsafeOnboardingMarker
            }
            return
        }
        try writeNewManagedFile(
            contents: onboardingPendingContents,
            destination: onboardingPendingMarker,
            permissions: 0o600
        )
        guard onboardingPendingIsAppOwned else {
            throw SetupError.unsafeOnboardingMarker
        }
    }

    private func completeOnboardingIfPending() throws {
        guard pathExists(onboardingPendingMarker) else { return }
        guard onboardingPendingIsAppOwned else {
            throw SetupError.unsafeOnboardingMarker
        }
        try fm.removeItem(at: onboardingPendingMarker)
        guard !pathExists(onboardingPendingMarker) else {
            throw SetupError.unsafeOnboardingMarker
        }
    }

    private func readVersionedTarget() -> URL? {
        guard isOwnerOnlyRegularFile(targetMarker),
              let raw = readSingleLine(targetMarker),
              (raw as NSString).isAbsolutePath
        else { return nil }
        let target = URL(fileURLWithPath: raw, isDirectory: true)
        guard target.deletingLastPathComponent().standardizedFileURL == versionsRoot.standardizedFileURL,
              isRegularDirectory(target),
              isCurrentUserOwnedExecutableWithoutSharedWrites(
                  target.appendingPathComponent("agentacct")
              ),
              readProvenance(from: target) != nil,
              verifiedRecordedPayloadIdentity(for: target) != nil
        else { return nil }
        return target
    }

    private func shellQuote(_ value: String) -> String {
        "'" + value.replacingOccurrences(of: "'", with: "'\"'\"'") + "'"
    }

    private func readSmallText(_ url: URL, maximumBytes: Int = 8_192) -> String? {
        guard let attributes = try? fm.attributesOfItem(atPath: url.path),
              let size = attributes[.size] as? NSNumber,
              size.intValue <= maximumBytes
        else { return nil }
        return try? String(contentsOf: url, encoding: .utf8)
    }

    private func fileIdentity(_ url: URL) -> FileIdentity? {
        guard let attributes = try? fm.attributesOfItem(atPath: url.path),
              let device = attributes[.systemNumber] as? NSNumber,
              let inode = attributes[.systemFileNumber] as? NSNumber,
              let size = attributes[.size] as? NSNumber
        else { return nil }
        return FileIdentity(
            device: device.uint64Value,
            inode: inode.uint64Value,
            size: size.uint64Value
        )
    }

    private func strongFileIdentity(_ url: URL) -> StrongFileIdentity? {
        guard isRegularFile(url),
              let file = fileIdentity(url),
              let attributes = try? fm.attributesOfItem(atPath: url.path),
              let owner = attributes[.ownerAccountID] as? NSNumber,
              owner.uint32Value == Darwin.geteuid(),
              let permissions = attributes[.posixPermissions] as? NSNumber,
              permissions.intValue & 0o022 == 0,
              let digest = CLIPayloadInspector.fileSHA256(url)
        else { return nil }
        return StrongFileIdentity(
            file: file,
            owner: owner.uint32Value,
            mode: permissions.intValue & 0o777,
            sha256: digest
        )
    }

    private func normalizedInfoString(_ key: String) -> String? {
        guard let string = bundleInfoDictionary?[key] as? String else { return nil }
        let trimmed = string.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private func readProvenance(from directory: URL) -> CLIProvenance? {
        let commitURL = directory.appendingPathComponent(".agentacct-source-commit")
        let descriptionURL = directory.appendingPathComponent(".agentacct-source-description")
        guard isRegularFile(commitURL), isRegularFile(descriptionURL),
              let commit = readSingleLine(commitURL),
              let description = readSingleLine(descriptionURL),
              isValidCommit(commit),
              !description.hasSuffix("-dirty")
        else { return nil }
        return CLIProvenance(commit: commit, description: description)
    }

    private func readSingleLine(_ url: URL) -> String? {
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return nil }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
              !trimmed.contains("\n"),
              !trimmed.contains("\r")
        else { return nil }
        return trimmed
    }

    private func isValidCommit(_ value: String) -> Bool {
        value.count == 40 && value.unicodeScalars.allSatisfy {
            ("0"..."9").contains(Character($0)) || ("a"..."f").contains(Character($0))
        }
    }

    private struct ReleaseVersion: Comparable, CustomStringConvertible {
        let major: Int
        let minor: Int
        let patch: Int

        init?(_ value: String) {
            let fields = value.split(separator: ".", omittingEmptySubsequences: false)
            guard fields.count == 3,
                  fields.allSatisfy({ !$0.isEmpty && $0.allSatisfy(\.isNumber) }),
                  let major = Int(fields[0]),
                  let minor = Int(fields[1]),
                  let patch = Int(fields[2])
            else { return nil }
            self.major = major
            self.minor = minor
            self.patch = patch
        }

        var description: String { "\(major).\(minor).\(patch)" }

        static func < (lhs: Self, rhs: Self) -> Bool {
            (lhs.major, lhs.minor, lhs.patch) < (rhs.major, rhs.minor, rhs.patch)
        }
    }

    private func isRegularDirectory(_ url: URL) -> Bool {
        guard let values = try? url.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey]) else {
            return false
        }
        return values.isDirectory == true && values.isSymbolicLink != true
    }

    private func isCurrentUserOwnedDirectoryWithoutSharedWrites(_ url: URL) -> Bool {
        guard isRegularDirectory(url),
              let attributes = try? fm.attributesOfItem(atPath: url.path),
              let owner = attributes[.ownerAccountID] as? NSNumber,
              owner.uint32Value == Darwin.geteuid(),
              let permissions = attributes[.posixPermissions] as? NSNumber,
              permissions.intValue & 0o022 == 0
        else { return false }
        return true
    }

    private func isCurrentUserOwnedRegularFileWithoutSharedWrites(_ url: URL) -> Bool {
        guard isRegularFile(url),
              let attributes = try? fm.attributesOfItem(atPath: url.path),
              let owner = attributes[.ownerAccountID] as? NSNumber,
              owner.uint32Value == Darwin.geteuid(),
              let permissions = attributes[.posixPermissions] as? NSNumber,
              permissions.intValue & 0o022 == 0
        else { return false }
        return true
    }

    private func isCurrentUserOwnedExecutableWithoutSharedWrites(_ url: URL) -> Bool {
        isCurrentUserOwnedRegularFileWithoutSharedWrites(url)
            && fm.isExecutableFile(atPath: url.path)
    }

    private func isRegularFile(_ url: URL) -> Bool {
        guard let values = try? url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey]) else {
            return false
        }
        return values.isRegularFile == true && values.isSymbolicLink != true
    }

    private func isRegularExecutable(_ url: URL) -> Bool {
        isRegularFile(url) && fm.isExecutableFile(atPath: url.path)
    }

    private func isOwnerOnlyRegularFile(_ url: URL) -> Bool {
        guard isCurrentUserOwnedRegularFileWithoutSharedWrites(url),
              let attributes = try? fm.attributesOfItem(atPath: url.path),
              let permissions = attributes[.posixPermissions] as? NSNumber,
              permissions.intValue & 0o777 == 0o600
        else { return false }
        return true
    }

    private func pathExists(_ url: URL) -> Bool {
        fm.fileExists(atPath: url.path)
            || (try? fm.destinationOfSymbolicLink(atPath: url.path)) != nil
    }

    private final class CLITransactionLock {
        private let descriptor: Int32

        init(file: URL) throws {
            let descriptor = file.path.withCString { path in
                Darwin.open(
                    path,
                    O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW,
                    S_IRUSR | S_IWUSR
                )
            }
            guard descriptor >= 0 else { throw SetupError.unsafeTransactionLock }

            var info = stat()
            guard Darwin.fstat(descriptor, &info) == 0,
                  (info.st_mode & S_IFMT) == S_IFREG,
                  info.st_uid == Darwin.geteuid()
            else {
                Darwin.close(descriptor)
                throw SetupError.unsafeTransactionLock
            }

            guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else {
                let lockError = errno
                Darwin.close(descriptor)
                if lockError == EWOULDBLOCK || lockError == EAGAIN {
                    throw SetupError.updateAlreadyInProgress
                }
                throw SetupError.unsafeTransactionLock
            }
            self.descriptor = descriptor
        }

        deinit {
            _ = flock(descriptor, LOCK_UN)
            Darwin.close(descriptor)
        }
    }

    private func acquireTransactionLock() throws -> CLITransactionLock {
        let parent = transactionLockFile.deletingLastPathComponent()
        if !pathExists(parent) {
            try fm.createDirectory(at: parent, withIntermediateDirectories: true)
            try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: parent.path)
        }
        guard isCurrentUserOwnedDirectoryWithoutSharedWrites(parent) else {
            throw SetupError.unsafeTransactionLock
        }
        return try CLITransactionLock(file: transactionLockFile)
    }

    // MARK: transactional replacement

    private struct CLIReplacement {
        enum PreviousLayout {
            case none
            case legacy(
                binaryBackup: URL,
                binaryIdentity: StrongFileIdentity,
                sideIdentity: CLIPayloadIdentity,
                pendingTarget: URL?
            )
            case versioned(install: InstalledCLI)
        }

        let previous: PreviousLayout
        let newTarget: URL
        let newInstall: InstalledCLI
    }

    private struct PreparedCLITarget: Equatable {
        let target: URL
        let binaryIdentity: FileIdentity
        let payloadIdentity: CLIPayloadIdentity
    }

    private struct InstalledCLISnapshot: Codable, Equatable {
        enum Layout: String, Codable {
            case legacy
            case versioned
        }

        let layout: Layout
        let targetPath: String?
        let pendingTargetPath: String?
        let provenance: CLIProvenance?
        let binaryIdentity: FileIdentity
        let payloadIdentity: CLIPayloadIdentity
        let wrapperIdentity: FileIdentity
    }

    private struct RuntimeTransaction: Codable, Equatable {
        enum Phase: String, Codable {
            case stopRequested
            case oldStopped
            case targetSwitched
            case recovering
        }

        let schema: Int
        let phase: Phase
        let wasRunning: Bool
        let storePath: String
        let oldInstall: InstalledCLISnapshot
        let newTargetPath: String
        let newBinaryIdentity: FileIdentity
        let newPayloadIdentity: CLIPayloadIdentity
        let newProvenance: CLIProvenance
        let newReleaseVersion: String
        let autostart: ManagedAutostart?

        func withPhase(_ phase: Phase) -> Self {
            Self(
                schema: schema,
                phase: phase,
                wasRunning: wasRunning,
                storePath: storePath,
                oldInstall: oldInstall,
                newTargetPath: newTargetPath,
                newBinaryIdentity: newBinaryIdentity,
                newPayloadIdentity: newPayloadIdentity,
                newProvenance: newProvenance,
                newReleaseVersion: newReleaseVersion,
                autostart: autostart
            )
        }
    }

    private func snapshot(_ installed: InstalledCLI) -> InstalledCLISnapshot {
        switch installed.layout {
        case .legacy(let pendingTarget):
            return InstalledCLISnapshot(
                layout: .legacy,
                targetPath: nil,
                pendingTargetPath: pendingTarget?.path,
                provenance: installed.provenance,
                binaryIdentity: installed.binaryIdentity,
                payloadIdentity: installed.payloadIdentity,
                wrapperIdentity: installed.wrapperIdentity
            )
        case .versioned(let target):
            return InstalledCLISnapshot(
                layout: .versioned,
                targetPath: target.path,
                pendingTargetPath: nil,
                provenance: installed.provenance,
                binaryIdentity: installed.binaryIdentity,
                payloadIdentity: installed.payloadIdentity,
                wrapperIdentity: installed.wrapperIdentity
            )
        }
    }

    private func validatedVersionTarget(path: String) -> URL? {
        guard (path as NSString).isAbsolutePath else { return nil }
        let target = URL(fileURLWithPath: path, isDirectory: true)
        guard target.deletingLastPathComponent().standardizedFileURL == versionsRoot.standardizedFileURL,
              isCurrentUserOwnedDirectoryWithoutSharedWrites(target),
              isCurrentUserOwnedExecutableWithoutSharedWrites(
                  target.appendingPathComponent("agentacct")
              ),
              readProvenance(from: target) != nil
        else { return nil }
        return target
    }

    private func expectedNewInstall(from transaction: RuntimeTransaction) -> InstalledCLI? {
        guard versionsRootIsAppOwned,
              let target = validatedVersionTarget(path: transaction.newTargetPath),
              fileIdentity(target.appendingPathComponent("agentacct")) == transaction.newBinaryIdentity,
              verifiedRecordedPayloadIdentity(for: target) == transaction.newPayloadIdentity,
              readProvenance(from: target) == transaction.newProvenance,
              ReleaseVersion(transaction.newReleaseVersion) != nil
        else { return nil }
        return InstalledCLI(
            layout: .versioned(target: target),
            provenance: transaction.newProvenance,
            binaryIdentity: transaction.newBinaryIdentity,
            payloadIdentity: transaction.newPayloadIdentity,
            wrapperIdentity: transaction.oldInstall.wrapperIdentity
        )
    }

    private func currentInstallIsInterruptedLegacySwitch(
        _ current: InstalledCLI,
        transaction: RuntimeTransaction
    ) -> Bool {
        guard transaction.oldInstall.layout == .legacy,
              case .legacy(let pendingTarget) = current.layout,
              pendingTarget?.path == transaction.newTargetPath
        else { return false }
        return current.provenance == transaction.oldInstall.provenance
            && current.binaryIdentity == transaction.oldInstall.binaryIdentity
            && current.payloadIdentity == transaction.oldInstall.payloadIdentity
            && current.wrapperIdentity == transaction.oldInstall.wrapperIdentity
    }

    private func runtimeTransactionContents(_ transaction: RuntimeTransaction) throws -> String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return String(decoding: try encoder.encode(transaction), as: UTF8.self) + "\n"
    }

    private func readRuntimeTransaction() throws -> RuntimeTransaction {
        guard isRegularFile(runtimeTransactionJournal),
              let attributes = try? fm.attributesOfItem(atPath: runtimeTransactionJournal.path),
              let owner = attributes[.ownerAccountID] as? NSNumber,
              owner.uint32Value == Darwin.geteuid(),
              let permissions = attributes[.posixPermissions] as? NSNumber,
              permissions.intValue & 0o777 == 0o600,
              let contents = readSmallText(runtimeTransactionJournal, maximumBytes: 65_536),
              let data = contents.data(using: .utf8),
              let transaction = try? JSONDecoder().decode(RuntimeTransaction.self, from: data),
              transaction.schema == 1,
              transaction.wasRunning || transaction.autostart != nil,
              transaction.oldInstall.payloadIdentity.isValid,
              transaction.newPayloadIdentity.isValid,
              (transaction.storePath as NSString).isAbsolutePath,
              validatedSnapshotPaths(transaction.oldInstall),
              expectedNewInstall(from: transaction) != nil,
              validatedAutostartSnapshot(transaction)
        else { throw SetupError.unsafeRuntimeJournal }
        return transaction
    }

    private func validatedSnapshotPaths(_ snapshot: InstalledCLISnapshot) -> Bool {
        switch snapshot.layout {
        case .legacy:
            guard snapshot.targetPath == nil else { return false }
            if let pending = snapshot.pendingTargetPath {
                return validatedVersionTarget(path: pending) != nil
            }
            // Once a legacy migration switches the stable executable to the
            // launcher, the old bytes live at the transaction-owned hard-link
            // backup. The selected/current InstalledCLI comparison is the
            // dynamic integrity check; decoding validates only this snapshot's
            // shape and persisted digest.
            return true
        case .versioned:
            guard snapshot.pendingTargetPath == nil,
                  let targetPath = snapshot.targetPath,
                  let target = validatedVersionTarget(path: targetPath)
            else { return false }
            return verifiedRecordedPayloadIdentity(for: target) == snapshot.payloadIdentity
        }
    }

    private func writeNewRuntimeTransaction(_ transaction: RuntimeTransaction) throws {
        guard !pathExists(runtimeTransactionJournal) else {
            throw SetupError.unsafeRuntimeJournal
        }
        let contents = try runtimeTransactionContents(transaction)
        guard contents.utf8.count <= 65_536 else { throw SetupError.unsafeRuntimeJournal }
        try writeNewManagedFile(
            contents: contents,
            destination: runtimeTransactionJournal,
            permissions: 0o600
        )
        guard try readRuntimeTransaction() == transaction else {
            throw SetupError.unsafeRuntimeJournal
        }
    }

    @discardableResult
    private func updateRuntimeTransaction(
        _ transaction: RuntimeTransaction,
        phase: RuntimeTransaction.Phase
    ) throws -> RuntimeTransaction {
        guard try readRuntimeTransaction() == transaction else {
            throw SetupError.unsafeRuntimeJournal
        }
        let updated = transaction.withPhase(phase)
        let temp = try prepareManagedTemp(
            contents: try runtimeTransactionContents(updated),
            in: runtimeTransactionJournal.deletingLastPathComponent(),
            permissions: 0o600,
            useInjectedWriter: false
        )
        defer { try? fm.removeItem(at: temp) }
        try atomicReplaceItem(at: temp, destination: runtimeTransactionJournal)
        guard try readRuntimeTransaction() == updated else {
            throw SetupError.unsafeRuntimeJournal
        }
        return updated
    }

    private func removeRuntimeTransaction(_ transaction: RuntimeTransaction) throws {
        guard try readRuntimeTransaction() == transaction else {
            throw SetupError.unsafeRuntimeJournal
        }
        try fm.removeItem(at: runtimeTransactionJournal)
        guard !pathExists(runtimeTransactionJournal) else {
            throw SetupError.unsafeRuntimeJournal
        }
    }

    private func stageCLI(_ packaged: PackagedCLI) throws -> URL {
        let parent = installedCLIDir.deletingLastPathComponent()
        try fm.createDirectory(at: parent, withIntermediateDirectories: true)
        let staged = parent.appendingPathComponent(".cli-stage-\(UUID().uuidString)", isDirectory: true)
        do {
            try copyDirectory(packaged.directory, staged)
            guard isRegularDirectory(staged),
                  isRegularExecutable(staged.appendingPathComponent("agentacct")),
                  readProvenance(from: staged) == packaged.provenance,
                  CLIPayloadInspector.identity(
                      at: staged,
                      requireCurrentUserOwner: true
                  ) == packaged.payloadIdentity
            else {
                throw SetupError.invalidStagedCLI
            }
            return staged
        } catch {
            try? fm.removeItem(at: staged)
            throw error
        }
    }

    private func stagedCLIMatches(_ staged: URL, packaged: PackagedCLI) -> Bool {
        isRegularDirectory(staged)
            && isRegularExecutable(staged.appendingPathComponent("agentacct"))
            && readProvenance(from: staged) == packaged.provenance
            && CLIPayloadInspector.identity(
                at: staged,
                requireCurrentUserOwner: true
            ) == packaged.payloadIdentity
    }

    private func prepareVersionedTarget(
        _ staged: URL,
        packaged: PackagedCLI,
        replacing expected: InstalledCLI?
    ) throws -> PreparedCLITarget {
        if let expected {
            guard installedCLIMatches(expected) else { throw SetupError.stableInstallChanged }
        } else {
            try validateFirstInstallTargets()
        }
        try ensureVersionsRootIsAppOwned()

        let targetName = "v\(packaged.releaseVersion)-\(packaged.provenance.commit.prefix(12))-\(UUID().uuidString)"
        let newTarget = versionsRoot.appendingPathComponent(targetName, isDirectory: true)
        guard !pathExists(newTarget) else { throw SetupError.unsafeVersionsDirectory }
        try fm.moveItem(at: staged, to: newTarget)
        guard isRegularDirectory(newTarget),
              isRegularExecutable(newTarget.appendingPathComponent("agentacct")),
              readProvenance(from: newTarget) == packaged.provenance,
              let newBinaryIdentity = fileIdentity(newTarget.appendingPathComponent("agentacct")),
              let newPayloadIdentity = CLIPayloadInspector.identity(
                  at: newTarget,
                  requireCurrentUserOwner: true
              ),
              newPayloadIdentity == packaged.payloadIdentity
        else { throw SetupError.invalidStagedCLI }
        do {
            try writeRecordedPayloadIdentity(newPayloadIdentity, for: newTarget)
        } catch {
            // This UUID target did not exist before this invocation. Remove it
            // only while its complete payload still matches what we moved;
            // otherwise preserve the changed path for inspection.
            let marker = payloadIdentityMarker(for: newTarget)
            if readRecordedPayloadIdentity(for: newTarget) == newPayloadIdentity {
                try? fm.removeItem(at: marker)
            }
            if CLIPayloadInspector.identity(
                at: newTarget,
                requireCurrentUserOwner: true
            ) == newPayloadIdentity {
                try? fm.removeItem(at: newTarget)
            }
            throw error
        }
        return PreparedCLITarget(
            target: newTarget,
            binaryIdentity: newBinaryIdentity,
            payloadIdentity: newPayloadIdentity
        )
    }

    private func discardPreparedTargetIfUnselected(
        _ prepared: PreparedCLITarget,
        packaged: PackagedCLI
    ) throws {
        // Only this invocation's UUID target is eligible. Once the stable
        // marker may select it, leave it in place for process-crash recovery.
        guard !pathExists(runtimeTransactionJournal),
              targetMarkerExplicitlySelectsDifferentTarget(from: prepared.target),
              versionsRootIsAppOwned,
              prepared.target.deletingLastPathComponent().standardizedFileURL
                == versionsRoot.standardizedFileURL,
              isRegularDirectory(prepared.target),
              isRegularExecutable(prepared.target.appendingPathComponent("agentacct")),
              fileIdentity(prepared.target.appendingPathComponent("agentacct")) == prepared.binaryIdentity,
              verifiedRecordedPayloadIdentity(for: prepared.target) == prepared.payloadIdentity,
              readProvenance(from: prepared.target) == packaged.provenance
        else { return }
        let payloadMarker = payloadIdentityMarker(for: prepared.target)
        try fm.removeItem(at: prepared.target)
        if readRecordedPayloadIdentity(for: prepared.target) == prepared.payloadIdentity {
            try? fm.removeItem(at: payloadMarker)
        }
        guard !pathExists(prepared.target) else {
            throw SetupError.recoveryIdentityChanged
        }
    }

    private func targetMarkerExplicitlySelectsDifferentTarget(from target: URL) -> Bool {
        guard pathExists(targetMarker) else { return true }
        // A malformed, unreadable, or non-regular marker may still be observed
        // by the stable launcher differently from this parser. Preserve the
        // target unless one valid line names a definitely different path.
        guard isOwnerOnlyRegularFile(targetMarker),
              let selected = readSingleLine(targetMarker)
        else {
            return false
        }
        return selected != target.path
    }

    private func activatePreparedCLI(
        _ prepared: PreparedCLITarget,
        packaged: PackagedCLI,
        replacing expected: InstalledCLI?
    ) throws -> CLIReplacement {
        if let expected {
            guard installedCLIMatches(expected) else { throw SetupError.stableInstallChanged }
        } else {
            try validateFirstInstallTargets()
        }
        guard versionsRootIsAppOwned,
              isRegularDirectory(prepared.target),
              isRegularExecutable(prepared.target.appendingPathComponent("agentacct")),
              readProvenance(from: prepared.target) == packaged.provenance,
              fileIdentity(prepared.target.appendingPathComponent("agentacct")) == prepared.binaryIdentity,
              verifiedRecordedPayloadIdentity(for: prepared.target) == prepared.payloadIdentity,
              prepared.payloadIdentity == packaged.payloadIdentity
        else { throw SetupError.invalidStagedCLI }

        let previous: CLIReplacement.PreviousLayout
        switch expected?.layout {
        case .versioned(let oldTarget):
            guard let expected else { throw SetupError.stableInstallChanged }
            previous = .versioned(install: expected)
            guard installedCLIMatches(expected) else { throw SetupError.stableInstallChanged }
            try replaceTargetMarker(from: oldTarget, with: prepared.target)

        case .legacy(let pendingTarget):
            guard let expected,
                  installedCLIMatches(expected)
            else {
                throw SetupError.stableInstallChanged
            }
            let launcherTemp = try prepareManagedTemp(
                contents: stableLauncherContents,
                in: installedCLIDir,
                permissions: 0o755,
                useInjectedWriter: true
            )
            defer { try? fm.removeItem(at: launcherTemp) }
            let backup = installedCLIDir.appendingPathComponent(
                ".agentacct-legacy-\(UUID().uuidString)",
                isDirectory: false
            )
            try fm.linkItem(at: installedBinary, to: backup)
            guard let backupIdentity = strongFileIdentity(backup),
                  let sideIdentity = legacyRollbackSideIdentity()
            else {
                try? fm.removeItem(at: backup)
                throw SetupError.stableInstallChanged
            }
            previous = .legacy(
                binaryBackup: backup,
                binaryIdentity: backupIdentity,
                sideIdentity: sideIdentity,
                pendingTarget: pendingTarget
            )
            do {
                if let pendingTarget {
                    try replaceTargetMarker(from: pendingTarget, with: prepared.target)
                } else {
                    try writeNewTargetMarker(prepared.target)
                }
                try atomicReplaceItem(at: launcherTemp, destination: installedBinary)
            } catch {
                if readSingleLine(targetMarker) == prepared.target.path,
                   readSmallText(installedBinary) != stableLauncherContents {
                    if let pendingTarget {
                        try? replaceTargetMarker(from: prepared.target, with: pendingTarget)
                    } else {
                        try? fm.removeItem(at: targetMarker)
                    }
                }
                throw error
            }

        case nil:
            previous = .none
            try activateFirstInstall(target: prepared.target)
        }

        guard let wrapperIdentity = fileIdentity(wrapper) else {
            throw SetupError.stableInstallChanged
        }
        let newInstall = InstalledCLI(
            layout: .versioned(target: prepared.target),
            provenance: packaged.provenance,
            binaryIdentity: prepared.binaryIdentity,
            payloadIdentity: prepared.payloadIdentity,
            wrapperIdentity: wrapperIdentity
        )
        guard installedCLIMatches(newInstall) else {
            throw SetupError.stableInstallChanged
        }
        return CLIReplacement(
            previous: previous,
            newTarget: prepared.target,
            newInstall: newInstall
        )
    }

    private func validateFirstInstallTargets() throws {
        if pathExists(installedCLIDir) {
            guard isCurrentUserOwnedDirectoryWithoutSharedWrites(installedCLIDir),
                  isDirectoryEmpty(installedCLIDir)
            else { throw SetupError.unsafeInstallDirectory }
        }
        guard !pathExists(installedBinary), !pathExists(targetMarker) else {
            throw SetupError.unsafeInstallDirectory
        }
        if pathExists(binDir) {
            guard isCurrentUserOwnedDirectoryWithoutSharedWrites(binDir) else {
                throw SetupError.unsafeWrapper
            }
        }
        guard !pathExists(wrapper) else { throw SetupError.unsafeWrapper }
    }

    private func ensureVersionsRootIsAppOwned() throws {
        if pathExists(versionsRoot) {
            guard isCurrentUserOwnedDirectoryWithoutSharedWrites(versionsRoot) else {
                throw SetupError.unsafeVersionsDirectory
            }
            if versionsRootIsAppOwned { return }
            guard isDirectoryEmpty(versionsRoot) else {
                throw SetupError.unsafeVersionsDirectory
            }
        } else {
            try fm.createDirectory(at: versionsRoot, withIntermediateDirectories: true)
            try fm.setAttributes(
                [.posixPermissions: 0o755],
                ofItemAtPath: versionsRoot.path
            )
        }
        guard isCurrentUserOwnedDirectoryWithoutSharedWrites(versionsRoot) else {
            throw SetupError.unsafeVersionsDirectory
        }
        try writeNewManagedFile(
            contents: versionsOwnershipContents,
            destination: versionsOwnershipMarker,
            permissions: 0o600
        )
        let entries = try? fm.contentsOfDirectory(atPath: versionsRoot.path)
        guard entries == [versionsOwnershipMarker.lastPathComponent],
              versionsRootIsAppOwned
        else {
            if (try? String(contentsOf: versionsOwnershipMarker, encoding: .utf8)) == versionsOwnershipContents {
                try? fm.removeItem(at: versionsOwnershipMarker)
            }
            throw SetupError.unsafeVersionsDirectory
        }
    }

    private func activateFirstInstall(target: URL) throws {
        if !pathExists(installedCLIDir) {
            try fm.createDirectory(at: installedCLIDir, withIntermediateDirectories: true)
            try fm.setAttributes(
                [.posixPermissions: 0o755],
                ofItemAtPath: installedCLIDir.path
            )
        }
        guard isCurrentUserOwnedDirectoryWithoutSharedWrites(installedCLIDir),
              isDirectoryEmpty(installedCLIDir)
        else {
            throw SetupError.unsafeInstallDirectory
        }
        if !pathExists(binDir) {
            try fm.createDirectory(at: binDir, withIntermediateDirectories: true)
            try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: binDir.path)
        }
        guard isCurrentUserOwnedDirectoryWithoutSharedWrites(binDir) else {
            throw SetupError.unsafeWrapper
        }

        let launcherTemp = try prepareManagedTemp(
            contents: stableLauncherContents,
            in: installedCLIDir,
            permissions: 0o755,
            useInjectedWriter: true
        )
        defer { try? fm.removeItem(at: launcherTemp) }
        let wrapperTemp = try prepareManagedTemp(
            contents: wrapperContents,
            in: binDir,
            permissions: 0o755,
            useInjectedWriter: true
        )
        defer { try? fm.removeItem(at: wrapperTemp) }

        var installedMarker = false
        var installedLauncher = false
        do {
            try writeNewTargetMarker(target)
            installedMarker = true
            try installPreparedFileWithoutReplacing(launcherTemp, at: installedBinary)
            installedLauncher = true
            try installPreparedFileWithoutReplacing(wrapperTemp, at: wrapper)
        } catch {
            if installedLauncher, readSmallText(installedBinary) == stableLauncherContents {
                try? fm.removeItem(at: installedBinary)
            }
            if installedMarker, readSingleLine(targetMarker) == target.path {
                try? fm.removeItem(at: targetMarker)
            }
            throw error
        }
    }

    private func rollback(_ replacement: CLIReplacement) throws {
        guard installedCLIMatches(replacement.newInstall) else {
            throw SetupError.recoveryIdentityChanged
        }
        switch replacement.previous {
        case .none:
            throw SetupError.recoveryIdentityChanged

        case .versioned(let oldInstall):
            guard case .versioned(let oldTarget) = oldInstall.layout,
                  retainedVersionedInstallMatches(oldInstall)
            else { throw SetupError.recoveryIdentityChanged }
            try replaceTargetMarker(from: replacement.newTarget, with: oldTarget)
            guard installedCLIMatches(oldInstall) else {
                throw SetupError.recoveryIdentityChanged
            }

        case .legacy(let backup, let binaryIdentity, let sideIdentity, let pendingTarget):
            guard isCurrentUserOwnedExecutableWithoutSharedWrites(backup),
                  strongFileIdentity(backup) == binaryIdentity,
                  legacyRollbackSideIdentity() == sideIdentity,
                  isOwnerOnlyRegularFile(targetMarker),
                  readSingleLine(targetMarker) == replacement.newTarget.path
            else { throw SetupError.recoveryIdentityChanged }
            // Validate every rollback destination before changing the stable
            // executable, including a pre-existing interrupted-migration marker.
            if let pendingTarget {
                guard validatedVersionTarget(path: pendingTarget.path) == pendingTarget,
                      verifiedRecordedPayloadIdentity(for: pendingTarget) != nil
                else { throw SetupError.recoveryIdentityChanged }
            }
            let restoreTemp = installedCLIDir.appendingPathComponent(
                ".agentacct-restore-\(UUID().uuidString)",
                isDirectory: false
            )
            try fm.linkItem(at: backup, to: restoreTemp)
            defer { try? fm.removeItem(at: restoreTemp) }
            guard strongFileIdentity(backup) == binaryIdentity,
                  strongFileIdentity(restoreTemp) == binaryIdentity,
                  legacyRollbackSideIdentity() == sideIdentity,
                  isOwnerOnlyRegularFile(targetMarker),
                  readSingleLine(targetMarker) == replacement.newTarget.path
            else { throw SetupError.recoveryIdentityChanged }
            if let pendingTarget {
                guard validatedVersionTarget(path: pendingTarget.path) == pendingTarget,
                      verifiedRecordedPayloadIdentity(for: pendingTarget) != nil
                else { throw SetupError.recoveryIdentityChanged }
            }
            try atomicReplaceItem(at: restoreTemp, destination: installedBinary)
            if let pendingTarget {
                try replaceTargetMarker(from: replacement.newTarget, with: pendingTarget)
            } else {
                try fm.removeItem(at: targetMarker)
            }
        }
    }

    private func isDirectoryEmpty(_ directory: URL) -> Bool {
        guard let entries = try? fm.contentsOfDirectory(atPath: directory.path) else { return false }
        return entries.isEmpty
    }

    private func writeNewTargetMarker(_ target: URL) throws {
        try writeNewManagedFile(
            contents: "\(target.path)\n",
            destination: targetMarker,
            permissions: 0o600
        )
    }

    private func replaceTargetMarker(from expected: URL, with target: URL) throws {
        guard isOwnerOnlyRegularFile(targetMarker),
              readSingleLine(targetMarker) == expected.path
        else { throw SetupError.recoveryIdentityChanged }
        let temp = try prepareManagedTemp(
            contents: "\(target.path)\n",
            in: installedCLIDir,
            permissions: 0o600,
            useInjectedWriter: false
        )
        defer { try? fm.removeItem(at: temp) }
        try atomicReplaceItem(at: temp, destination: targetMarker)
    }

    private func writeNewManagedFile(
        contents: String,
        destination: URL,
        permissions: NSNumber
    ) throws {
        let temp = try prepareManagedTemp(
            contents: contents,
            in: destination.deletingLastPathComponent(),
            permissions: permissions,
            useInjectedWriter: false
        )
        defer { try? fm.removeItem(at: temp) }
        try installPreparedFileWithoutReplacing(temp, at: destination)
    }

    private func prepareManagedTemp(
        contents: String,
        in directory: URL,
        permissions: NSNumber,
        useInjectedWriter: Bool
    ) throws -> URL {
        let temp = directory.appendingPathComponent(
            ".agentacct-write-\(UUID().uuidString)",
            isDirectory: false
        )
        do {
            if useInjectedWriter {
                try writeWrapper(contents, temp)
            } else {
                try contents.write(to: temp, atomically: false, encoding: .utf8)
            }
            try fm.setAttributes([.posixPermissions: permissions], ofItemAtPath: temp.path)
            guard isCurrentUserOwnedRegularFileWithoutSharedWrites(temp),
                  let attributes = try? fm.attributesOfItem(atPath: temp.path),
                  let actualPermissions = attributes[.posixPermissions] as? NSNumber,
                  actualPermissions.intValue & 0o777 == permissions.intValue,
                  (try? String(contentsOf: temp, encoding: .utf8)) == contents
            else { throw SetupError.invalidManagedFile }
            return temp
        } catch {
            try? fm.removeItem(at: temp)
            throw error
        }
    }

    private func installPreparedFileWithoutReplacing(_ source: URL, at destination: URL) throws {
        try fm.linkItem(at: source, to: destination)
        try fm.removeItem(at: source)
    }

    private func atomicReplaceItem(at source: URL, destination: URL) throws {
        let result = source.path.withCString { sourcePath in
            destination.path.withCString { destinationPath in
                Darwin.rename(sourcePath, destinationPath)
            }
        }
        guard result == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
    }

    // MARK: managed runtime lifecycle

    /// Both byte representations are journaled before launchd is stopped.
    /// Only the executable changes; configured host/port and logs survive.
    private struct ManagedAutostart: Codable, Equatable {
        let originalContents: String
        let updatedContents: String
        let permissions: Int
    }

    private var launchctl: URL { URL(fileURLWithPath: "/bin/launchctl") }
    private var launchdDomain: String { "gui/\(Darwin.geteuid())" }
    private var launchdService: String { "\(launchdDomain)/dev.agentacct.runtime" }

    private func managedAutostart(installed: InstalledCLI, store: URL) throws -> ManagedAutostart? {
        guard pathExists(managedAutostartFile) else { return nil }
        guard autostartFileHasSafeOwnership,
              let contents = readSmallText(managedAutostartFile),
              let attributes = try? fm.attributesOfItem(atPath: managedAutostartFile.path),
              let permissions = attributes[.posixPermissions] as? NSNumber
        else { throw SetupError.unsafeAutostart }
        var executables = [installedBinary.path, wrapper.path]
        if case .versioned(let target) = installed.layout {
            executables.append(target.appendingPathComponent("agentacct").path)
        }
        guard var document = validatedAutostartDocument(
            contents, store: store, allowedExecutables: executables
        ), var arguments = document["ProgramArguments"] as? [String]
        else { throw SetupError.unsafeAutostart }
        arguments[0] = installedBinary.path
        document["ProgramArguments"] = arguments
        let updated = try PropertyListSerialization.data(fromPropertyList: document, format: .xml, options: 0)
        return ManagedAutostart(
            originalContents: contents,
            updatedContents: String(decoding: updated, as: UTF8.self),
            permissions: permissions.intValue & 0o777
        )
    }

    private var autostartFileHasSafeOwnership: Bool {
        guard isCurrentUserOwnedDirectoryWithoutSharedWrites(managedAutostartFile.deletingLastPathComponent()),
              isRegularFile(managedAutostartFile),
              let attributes = try? fm.attributesOfItem(atPath: managedAutostartFile.path),
              let owner = attributes[.ownerAccountID] as? NSNumber,
              owner.uint32Value == Darwin.geteuid(),
              let links = attributes[.referenceCount] as? NSNumber,
              links.intValue == 1,
              let mode = attributes[.posixPermissions] as? NSNumber
        else { return false }
        return mode.intValue & 0o777 == 0o600 || mode.intValue & 0o777 == 0o644
    }

    private func validatedAutostartDocument(
        _ contents: String, store: URL, allowedExecutables: [String]
    ) -> [String: Any]? {
        guard let data = contents.data(using: .utf8),
              let document = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
              Set(document.keys) == ["Label", "ProgramArguments", "RunAtLoad", "KeepAlive", "StandardOutPath", "StandardErrorPath"],
              document["Label"] as? String == "dev.agentacct.runtime",
              let runAtLoad = document["RunAtLoad"] as? NSNumber,
              CFGetTypeID(runAtLoad) == CFBooleanGetTypeID(), runAtLoad.boolValue,
              let keepAlive = document["KeepAlive"] as? NSNumber,
              CFGetTypeID(keepAlive) == CFBooleanGetTypeID(), keepAlive.boolValue,
              document["StandardOutPath"] as? String == store.deletingLastPathComponent().appendingPathComponent("autostart.out.log").path,
              document["StandardErrorPath"] as? String == store.deletingLastPathComponent().appendingPathComponent("autostart.err.log").path,
              let arguments = document["ProgramArguments"] as? [String],
              arguments.count >= 5, arguments.count <= 9,
              allowedExecutables.contains(arguments[0]),
              Array(arguments[1...4]) == ["start", "--foreground", "--store-dir", store.path]
        else { return nil }
        var remaining = Array(arguments.dropFirst(5))
        if remaining.first == "--host" {
            guard remaining.count >= 2 else { return nil }
            let host = remaining[1]
            let valid = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.:-[]")
            guard !host.isEmpty, host.utf8.count <= 253,
                  host.unicodeScalars.allSatisfy({ valid.contains($0) })
            else { return nil }
            remaining.removeFirst(2)
        }
        if remaining.first == "--port" {
            guard remaining.count == 2, let port = Int(remaining[1]),
                  (1...65535).contains(port), String(port) == remaining[1]
            else { return nil }
            remaining.removeFirst(2)
        }
        guard remaining.isEmpty else { return nil }
        return document
    }

    private func validatedAutostartSnapshot(_ transaction: RuntimeTransaction) -> Bool {
        guard let autostart = transaction.autostart else { return true }
        let store = URL(fileURLWithPath: transaction.storePath, isDirectory: true)
        var executables = [installedBinary.path, wrapper.path]
        if let target = transaction.oldInstall.targetPath {
            executables.append(URL(fileURLWithPath: target).appendingPathComponent("agentacct").path)
        }
        guard autostart.permissions == 0o600 || autostart.permissions == 0o644,
              var original = validatedAutostartDocument(autostart.originalContents, store: store, allowedExecutables: executables),
              let updated = validatedAutostartDocument(autostart.updatedContents, store: store, allowedExecutables: [installedBinary.path]),
              var arguments = original["ProgramArguments"] as? [String]
        else { return false }
        arguments[0] = installedBinary.path
        original["ProgramArguments"] = arguments
        return NSDictionary(dictionary: original).isEqual(to: updated)
    }

    private func autostartMatches(_ expected: ManagedAutostart?) -> Bool {
        guard let expected else { return !pathExists(managedAutostartFile) }
        guard autostartFileHasSafeOwnership,
              let attributes = try? fm.attributesOfItem(atPath: managedAutostartFile.path),
              ((attributes[.posixPermissions] as? NSNumber)?.intValue ?? -1) & 0o777 == expected.permissions,
              let contents = readSmallText(managedAutostartFile)
        else { return false }
        return contents == expected.originalContents || contents == expected.updatedContents
    }

    private func replaceAutostartContents(_ expected: ManagedAutostart, useUpdated: Bool) throws {
        guard autostartMatches(expected) else { throw SetupError.unsafeAutostart }
        let contents = useUpdated ? expected.updatedContents : expected.originalContents
        if readSmallText(managedAutostartFile) == contents { return }
        let temp = try prepareManagedTemp(
            contents: contents, in: managedAutostartFile.deletingLastPathComponent(),
            permissions: NSNumber(value: expected.permissions), useInjectedWriter: false
        )
        defer { try? fm.removeItem(at: temp) }
        guard autostartMatches(expected) else { throw SetupError.unsafeAutostart }
        try atomicReplaceItem(at: temp, destination: managedAutostartFile)
        guard autostartMatches(expected), readSmallText(managedAutostartFile) == contents else {
            throw SetupError.unsafeAutostart
        }
    }

    private func stopAutostartSupervisor(_ expected: ManagedAutostart) async throws {
        guard autostartMatches(expected) else { throw SetupError.unsafeAutostart }
        guard try await verifiedLaunchdJob(expected) != nil else { return }
        do {
            try await runRecoveryCommand(executable: launchctl, arguments: ["bootout", launchdService])
        } catch ProcessRunnerError.nonzeroExit(3) {
            // ESRCH is the explicit already-unloaded state after a crash.
        }
        guard autostartMatches(expected), try await verifiedLaunchdJob(expected) == nil else {
            throw SetupError.unsafeAutostart
        }
    }

    private func startAutostartSupervisor(
        _ expected: ManagedAutostart, useUpdated: Bool, install: InstalledCLI, store: URL
    ) async throws {
        let contents = useUpdated ? expected.updatedContents : expected.originalContents
        guard installedCLIMatches(install), autostartMatches(expected), readSmallText(managedAutostartFile) == contents else {
            throw SetupError.unsafeAutostart
        }
        guard let desiredArguments = autostartArguments(in: contents) else { throw SetupError.unsafeAutostart }
        try await runRecoveryCommand(executable: launchctl, arguments: ["bootstrap", launchdDomain, managedAutostartFile.path])
        // launchd accepting a job does not mean its supervisor or local API
        // started. Retain the journal until status proves both API health and
        // watcher readiness (including a legitimate external watcher).
        for attempt in 0..<20 {
            guard installedCLIMatches(install), autostartMatches(expected) else { throw SetupError.unsafeAutostart }
            let job = try await verifiedLaunchdJob(expected)
            guard installedCLIMatches(install), autostartMatches(expected) else { throw SetupError.unsafeAutostart }
            // Stop/recovery may recognize either journaled descriptor, but
            // readiness must prove the descriptor selected for this start.
            // A concurrently reloaded old direct target is not a successful
            // activation of the newly selected recorder.
            if let job, job.arguments != desiredArguments { throw SetupError.unsafeAutostart }
            if job?.isRunning == true {
                let output = try await runCommand(executable: installedBinary, arguments: runtimeArguments(command: "status", store: store))
                guard installedCLIMatches(install), autostartMatches(expected) else { throw SetupError.unsafeAutostart }
                if let data = output.data(using: .utf8),
                   let status = try? JSONDecoder().decode(RuntimeStatus.self, from: data),
                   status.isReady(store: store) { return }
            }
            if attempt < 19 { try await readinessPause() }
        }
        throw SetupError.autostartNotReady
    }

    private struct LaunchdJob {
        let isRunning: Bool
        let arguments: [String]
    }

    private func autostartArguments(in contents: String) -> [String]? {
        guard let data = contents.data(using: .utf8),
              let document = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any]
        else { return nil }
        return document["ProgramArguments"] as? [String]
    }

    /// `launchctl print` is intentionally parsed conservatively: its output
    /// is not a stable API. A changed format blocks an automatic update rather
    /// than treating a same-label but differently configured job as ours.
    private func verifiedLaunchdJob(_ expected: ManagedAutostart) async throws -> LaunchdJob? {
        guard autostartMatches(expected) else { throw SetupError.unsafeAutostart }
        let runner = processRunner
        let executable = launchctl
        let service = launchdService
        let result: (String, Int32) = try await Task { @MainActor in
            var lines: [String] = []
            do {
                for try await line in runner(executable, ["print", service]) { lines.append(line) }
                return (lines.joined(separator: "\n"), 0)
            } catch ProcessRunnerError.nonzeroExit(let status) {
                return (lines.joined(separator: "\n"), status)
            }
        }.value
        guard autostartMatches(expected) else { throw SetupError.unsafeAutostart }
        let output = result.0.trimmingCharacters(in: .whitespacesAndNewlines)
        if result.1 == 113,
           output == "Bad request.\nCould not find service \"dev.agentacct.runtime\" in domain for user gui: \(Darwin.geteuid())" {
            return nil
        }
        guard result.1 == 0 else { throw SetupError.unsafeAutostart }
        let lines = output.components(separatedBy: "\n")
        guard lines.first == "\(launchdService) = {", lines.last == "}" else { throw SetupError.unsafeAutostart }
        var fields: [String: String] = [:]
        var arguments: [String]?
        var index = 1
        while index < lines.count - 1 {
            let line = lines[index]
            if line == "\targuments = {" {
                guard arguments == nil else { throw SetupError.unsafeAutostart }
                var values: [String] = []
                index += 1
                while index < lines.count - 1, lines[index] != "\t}" {
                    guard lines[index].hasPrefix("\t\t"), !lines[index].hasPrefix("\t\t\t") else { throw SetupError.unsafeAutostart }
                    values.append(String(lines[index].dropFirst(2)))
                    index += 1
                }
                guard index < lines.count - 1, lines[index] == "\t}" else { throw SetupError.unsafeAutostart }
                arguments = values
            } else if line.hasPrefix("\t"), !line.hasPrefix("\t\t"), line.hasSuffix(" = {") {
                // Skip an unrelated nested block as a block, never scan its
                // values for identity fields. Embedded newlines that imitate
                // root fields must fail closed rather than spoof a descriptor.
                index += 1
                while index < lines.count - 1, lines[index] != "\t}" {
                    guard lines[index].hasPrefix("\t\t") else { throw SetupError.unsafeAutostart }
                    index += 1
                }
                guard index < lines.count - 1 else { throw SetupError.unsafeAutostart }
            } else if line.hasPrefix("\t"), !line.hasPrefix("\t\t") {
                guard line != "\t}" else { throw SetupError.unsafeAutostart }
                for key in ["path", "program", "type", "state", "pid"] {
                    let prefix = "\t\(key) = "
                    if line.hasPrefix(prefix) {
                        guard fields[key] == nil else { throw SetupError.unsafeAutostart }
                        fields[key] = String(line.dropFirst(prefix.count))
                    }
                }
            } else if !line.isEmpty {
                throw SetupError.unsafeAutostart
            }
            index += 1
        }
        guard fields["path"] == managedAutostartFile.path, fields["type"] == "LaunchAgent",
              let arguments, !arguments.isEmpty,
              fields["program"] == arguments.first,
              arguments == autostartArguments(in: expected.originalContents)
                || arguments == autostartArguments(in: expected.updatedContents),
              let state = fields["state"]
        else { throw SetupError.unsafeAutostart }
        return LaunchdJob(isRunning: state == "running" && (Int(fields["pid"] ?? "") ?? 0) > 0, arguments: arguments)
    }

    private struct RuntimeStatus: Decodable {
        struct ProcessStatus: Decodable {
            let state: String
            let role: String?
        }
        let processes: [ProcessStatus]
        let state: String?
        let store_dir: String?
        let dashboard_health: String?
        let watcher: String?

        var hasRunningProcess: Bool {
            processes.contains { $0.state == "running" }
        }

        func isReady(store: URL) -> Bool {
            state == "running" && store_dir == store.path && dashboard_health == "healthy"
                && (watcher == "running" || watcher == "external")
                && processes.contains { $0.role == "dashboard" && $0.state == "running" }
        }
    }

    private func runtimeArguments(command: String, store: URL? = nil) throws -> [String] {
        [command, "--store-dir", try (store ?? storeDirectory()).path, "--json"]
    }

    private func managedRuntimeIsRunning(executable: URL, store: URL? = nil) async throws -> Bool {
        append("Checking the app-managed recorder runtime")
        let output = try await runCommand(
            executable: executable,
            arguments: try runtimeArguments(command: "status", store: store)
        )
        guard let data = output.data(using: .utf8),
              let status = try? JSONDecoder().decode(RuntimeStatus.self, from: data)
        else {
            throw SetupError.invalidRuntimeStatus
        }
        return status.hasRunningProcess
    }

    private func cliReleaseVersion(executable: URL) async throws -> ReleaseVersion {
        let output = try await runCommand(executable: executable, arguments: ["--version"])
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard output.hasPrefix("agentacct "),
              let version = ReleaseVersion(String(output.dropFirst("agentacct ".count)))
        else {
            throw SetupError.invalidCLIVersion
        }
        return version
    }

    @discardableResult
    private func runCommand(executable: URL, arguments: [String]) async throws -> String {
        var lines: [String] = []
        for try await line in processRunner(executable, arguments) {
            lines.append(line)
        }
        return lines.joined(separator: "\n")
    }

    private func restartPreviousRuntimeForRecovery(store: URL? = nil) async throws {
        append("Restoring the previous app-managed recorder runtime")
        try await runRecoveryCommand(
            executable: installedBinary,
            arguments: try runtimeArguments(command: "start", store: store)
        )
    }

    private func runRecoveryCommand(executable: URL, arguments: [String]) async throws {
        // An unstructured task does not inherit cancellation from a window
        // disappearing midway through an upgrade. Recovery must still run.
        let runner = processRunner
        try await Task { @MainActor in
            var iterator = runner(executable, arguments).makeAsyncIterator()
            while let _ = try await iterator.next() {}
        }.value
    }

    enum SetupError: LocalizedError {
        case noEmbeddedCLI
        case invalidStagedCLI
        case stableInstallChanged
        case unsafeInstallDirectory
        case unsafeVersionsDirectory
        case unsafeWrapper
        case unsafeTransactionLock
        case updateAlreadyInProgress
        case invalidManagedFile
        case invalidRuntimeStatus
        case invalidCLIVersion
        case packagedVersionMismatch
        case recoveryIdentityChanged
        case unsafeRuntimeJournal
        case runtimeJournalStoreChanged
        case unsafeOnboardingMarker
        case unsafeAutostart
        case autostartNotReady
        case damagedManagedInstall
        var errorDescription: String? {
            switch self {
            case .noEmbeddedCLI:
                return "This build has no embedded CLI. Install agentacct with `pipx install agentacct` instead."
            case .invalidStagedCLI:
                return "The bundled recorder could not be verified after copying. The existing recorder was left unchanged."
            case .stableInstallChanged:
                return "The installed recorder changed while the update was being prepared. It was left unchanged."
            case .unsafeInstallDirectory:
                return "The stable recorder path is not a regular app-managed directory, so it was not replaced."
            case .unsafeVersionsDirectory:
                return "The recorder versions path is not empty or app-managed, so it was not changed."
            case .unsafeWrapper:
                return "The agentacct command wrapper is not a regular app-managed file, so it was not replaced."
            case .unsafeTransactionLock:
                return "The recorder update lock is not a safe app-owned file, so no recorder files were changed."
            case .updateAlreadyInProgress:
                return "Another agentacct App window is already updating the recorder. Wait for it to finish, then try again."
            case .invalidManagedFile:
                return "A recorder launcher file could not be verified before activation."
            case .invalidRuntimeStatus:
                return "The current recorder did not return a valid runtime status, so it was not replaced."
            case .invalidCLIVersion:
                return "The recorder did not return a valid release version, so it was not replaced."
            case .packagedVersionMismatch:
                return "The bundled recorder version does not match this app, so it was not installed."
            case .recoveryIdentityChanged:
                return "The recorder identity changed during recovery, so its files were preserved."
            case .unsafeRuntimeJournal:
                return "The recorder recovery journal is not a safe app-owned file, so no recovery command was run."
            case .runtimeJournalStoreChanged:
                return "The recorder store changed since the interrupted update, so recovery stopped without running a command. Restore the previous store setting and try again."
            case .unsafeOnboardingMarker:
                return "The recorder onboarding marker is not a safe app-owned file, so it was preserved without changing the installation."
            case .unsafeAutostart:
                return "The agentacct autostart configuration changed or is not app-managed, so the recorder update paused. Run `agentacct uninstall-autostart`, reopen the App, then run `agentacct install-autostart` again."
            case .autostartNotReady:
                return "The updated autostart recorder did not become ready. Its recovery journal was retained until the previous recorder could be restored."
            case .damagedManagedInstall:
                return "The app-managed recorder target is incomplete or changed, so no recorder command was run. Reinstall agentacct before reopening the App."
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
