import XCTest
@testable import agentacct

final class NativeSetupFlowTests: XCTestCase {
    @MainActor
    func testPackagedReviewFailureRetryUsesOnlySyntheticDependencies() async {
        let model = SetupModel(reviewPhase: .failed("Synthetic failure"), selectedClient: .codex)
        XCTAssertNil(model.bundledCLIDir)
        XCTAssertFalse(model.shouldAutomaticallyUpgradeCLI)
        XCTAssertTrue(model.canRunInteractiveSetup)
        XCTAssertFalse(model.canReconnectRecorder)
        XCTAssertEqual(model.recordingStorePath, "/synthetic-review/state")

        model.reset()
        await model.setUp()

        XCTAssertEqual(model.phase, .done)
        XCTAssertEqual(model.selectedClient, .codex)
        XCTAssertEqual(model.log, [
            "Running: agentacct onboard --agent codex --yes",
            "Synthetic review only: onboard --agent codex --yes",
            "No configuration files were changed.",
        ])
        XCTAssertNotNil(model.onboardingCompletedAt)
    }

    @MainActor
    func testNamedClientIsTheOnlyOnboardingTarget() async {
        var commands: [[String]] = []
        let model = SetupModel(
            installer: { URL(fileURLWithPath: "/test-recorder") },
            processRunner: { _, arguments in
                commands.append(arguments)
                return AsyncThrowingStream { $0.finish() }
            }
        )
        model.selectClientForSetup(.claudeCode)

        await model.setUp()

        XCTAssertEqual(commands, [["onboard", "--agent", "claude-code", "--yes"]])
        XCTAssertEqual(model.phase, .done)
        XCTAssertNotNil(model.onboardingCompletedAt)
        XCTAssertFalse(model.isRunningOnboard)
    }

    @MainActor
    func testFailureRetainsTargetAndRetryCreatesBoundaryOnlyAfterSuccess() async {
        var attempts = 0
        var commands: [[String]] = []
        let model = SetupModel(
            installer: { URL(fileURLWithPath: "/test-recorder") },
            processRunner: { _, arguments in
                attempts += 1
                commands.append(arguments)
                let fail = attempts == 1
                return AsyncThrowingStream {
                    $0.yield("Settings merge attempted")
                    if fail { $0.finish(throwing: ProcessRunnerError.nonzeroExit(7)) }
                    else { $0.finish() }
                }
            }
        )
        model.selectClientForSetup(.codex)
        await model.setUp()

        XCTAssertEqual(model.phase, .failed("Recorder setup exited with status 7."))
        XCTAssertNil(model.onboardingCompletedAt)
        XCTAssertEqual(model.selectedClient, .codex)
        model.reset()
        await model.setUp()

        XCTAssertEqual(model.phase, .done)
        XCTAssertNotNil(model.onboardingCompletedAt)
        XCTAssertEqual(commands, Array(repeating: ["onboard", "--agent", "codex", "--yes"], count: 2))
    }

    @MainActor
    func testClientSelectionCannotChangeDuringOnboarding() async {
        var model: SetupModel!
        model = SetupModel(
            installer: { URL(fileURLWithPath: "/test-recorder") },
            processRunner: { _, _ in
                model.selectClientForSetup(.hermes)
                return AsyncThrowingStream { $0.finish() }
            }
        )
        model.selectClientForSetup(.codex)

        await model.setUp()

        XCTAssertEqual(model.selectedClient, .codex)
    }

    @MainActor
    func testDevelopmentBuildDoesNotOfferAnInstaller() {
        let model = SetupModel(bundleResourceURL: nil, bundleInfoDictionary: nil)

        XCTAssertFalse(model.canRunInteractiveSetup)
        XCTAssertFalse(model.shouldOfferSetup)
    }

    func testCaptureConfirmationRequiresThisClientAndFreshIdentifiedEvidence() {
        let boundary = Date(timeIntervalSince1970: 1_000)
        let fresh = SetupCaptureConfirmation(clientID: "codex", eventID: "event-1", observedAt: boundary.addingTimeInterval(1), taskID: "task-1")
        let old = SetupCaptureConfirmation(clientID: "codex", eventID: "event-old", observedAt: boundary, taskID: nil)
        let unidentified = SetupCaptureConfirmation(clientID: "codex", eventID: " \n", observedAt: boundary.addingTimeInterval(1), taskID: nil)

        XCTAssertTrue(fresh.confirms(client: .codex, after: boundary))
        XCTAssertFalse(fresh.confirms(client: .claudeCode, after: boundary))
        XCTAssertFalse(fresh.confirms(client: .codex, after: nil))
        XCTAssertFalse(fresh.confirms(client: nil, after: boundary))
        XCTAssertFalse(old.confirms(client: .codex, after: boundary))
        XCTAssertFalse(unidentified.confirms(client: .codex, after: boundary))
    }

    func testOpenCodeReviewUsesExistingJSONUnderXDGConfigHome() {
        let plan = SetupConfigurationPlan(
            client: .openCode,
            homeDirectory: URL(fileURLWithPath: "/test-home"),
            environment: ["XDG_CONFIG_HOME": "/custom-config"],
            fileExists: { $0 == "/custom-config/opencode/opencode.json" }
        )

        XCTAssertEqual(plan.changes.map(\.path), [
            "/custom-config/opencode/opencode.json",
            "/custom-config/opencode/AGENTS.md",
            "/custom-config/opencode/plugins/agentacct.js"
        ])
    }

    func testOpenCodeReviewPrefersExistingJSONCAsTheCLIAdapterDoes() {
        let plan = SetupConfigurationPlan(
            client: .openCode,
            homeDirectory: URL(fileURLWithPath: "/test-home"),
            environment: [:],
            fileExists: { $0.hasSuffix("opencode.json") || $0.hasSuffix("opencode.jsonc") }
        )

        XCTAssertEqual(plan.changes.first?.path, "/test-home/.config/opencode/opencode.jsonc")
    }

    func testDeepSeekHarnessReviewTargetsHomePatchAndAgentsUnderDSHHome() {
        let defaultPlan = SetupConfigurationPlan(
            client: .deepseekHarness,
            homeDirectory: URL(fileURLWithPath: "/test-home"),
            environment: [:]
        )
        XCTAssertEqual(defaultPlan.changes.map(\.path), [
            "/test-home/.dsh/cordis.patch.yml",
            "/test-home/.dsh/AGENTS.md"
        ])

        let overriddenPlan = SetupConfigurationPlan(
            client: .deepseekHarness,
            homeDirectory: URL(fileURLWithPath: "/test-home"),
            environment: ["DSH_HOME": "/custom-dsh"]
        )
        XCTAssertEqual(overriddenPlan.changes.map(\.path), [
            "/custom-dsh/cordis.patch.yml",
            "/custom-dsh/AGENTS.md"
        ])
    }

    func testKimiCodeReviewTargetsMCPJSONAndAgentsUnderKimiCodeHome() {
        let defaultPlan = SetupConfigurationPlan(
            client: .kimiCode,
            homeDirectory: URL(fileURLWithPath: "/test-home"),
            environment: [:]
        )
        XCTAssertEqual(defaultPlan.changes.map(\.path), [
            "/test-home/.kimi-code/mcp.json",
            "/test-home/.kimi-code/AGENTS.md"
        ])
        // config.toml carries Kimi Code's provider credentials and never its MCP
        // servers, so it must not appear as a planned change.
        XCTAssertFalse(defaultPlan.changes.contains { $0.path.hasSuffix("config.toml") })

        let overriddenPlan = SetupConfigurationPlan(
            client: .kimiCode,
            homeDirectory: URL(fileURLWithPath: "/test-home"),
            environment: ["KIMI_CODE_HOME": " /custom-kimi "]
        )
        XCTAssertEqual(overriddenPlan.changes.map(\.path), [
            "/custom-kimi/mcp.json",
            "/custom-kimi/AGENTS.md"
        ])
    }

    func testGlobalOnboardingOffersTheClientsTheRecorderConfigures() {
        // The picker's roster, in its display order, is exactly the clients
        // `onboard --agent X` configures at user scope.
        XCTAssertEqual(SetupClient.allCases.map(\.rawValue), [
            "codex", "claude-code", "opencode", "hermes", "dsh", "kimi-code"
        ])
        XCTAssertEqual(SetupClient(rawValue: "kimi-code")?.title, "Kimi Code")
        // A client whose MCP registration stays manual (openclaw) and an
        // observation-only one (cursor) are never onboarding targets here.
        XCTAssertNil(SetupClient(rawValue: "openclaw"))
        XCTAssertNil(SetupClient(rawValue: "cursor"))
    }

    func testKimiCodeActivationRequiresANewSessionForTheMCPRegistration() {
        let instruction = SetupConfigurationPlan.activationInstruction(for: .kimiCode)
        XCTAssertTrue(instruction.contains("new Kimi Code session"))
        XCTAssertTrue(instruction.contains("$KIMI_CODE_HOME/AGENTS.md"))
    }
}
