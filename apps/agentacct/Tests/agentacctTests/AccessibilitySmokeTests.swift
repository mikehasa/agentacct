import Foundation
import XCTest

/// The rules behind `Scripts/a11y-smoke.sh`, exercised WITHOUT a running app.
///
/// The script's driver talks to a live process; everything it decides is in
/// `AccessibilitySmokeJudge`, which is pure — a recorded run in, failures out.
/// These tests feed it recordings (a healthy one, and one broken in each way
/// the script promises to catch) so the guardrail itself is covered even though
/// the GUI is never driven here.
final class AccessibilitySmokeJudgeTests: XCTestCase {

    // MARK: - fixtures

    private func row(identifier: String = "work.table.task.abc",
                     role: String? = "AXRow",
                     label: String? = "Add rate limit to login",
                     actions: [String]? = ["AXPress", "AXShowMenu"]) -> AccessibilityNode {
        AccessibilityNode(role: role, identifier: identifier, label: label, actions: actions)
    }

    private func healthyRun() -> AccessibilitySmokeRun {
        let listRow = row()
        return AccessibilitySmokeRun(app: "agentacct", pid: 42, steps: [
            AccessibilityStep(
                name: "dashboard",
                tree: AccessibilityNode(role: "AXWindow", title: "agentacct"),
                focus: AccessibilityNode(role: "AXButton",
                                         identifier: "dashboard.shift-brief.view-queue",
                                         label: "Open Attention"),
                input: "Tab"
            ),
            AccessibilityStep(
                name: "work",
                tree: AccessibilityNode(role: "AXWindow", title: "agentacct", children: [
                    AccessibilityNode(role: "AXTable", identifier: "work.table",
                                      label: "Work receipts", children: [listRow])
                ]),
                focus: listRow,
                input: "Tab ×n → row"
            ),
            AccessibilityStep(
                name: "record",
                tree: AccessibilityNode(role: "AXWindow", children: [
                    AccessibilityNode(role: "AXStaticText",
                                      label: "Verdict: Reported — 0/1 checked"),
                    AccessibilityNode(role: "AXGroup", identifier: "receipt.summary.coverage",
                                      label: "Coverage", value: "0/1"),
                    AccessibilityNode(role: "AXGroup", identifier: "receipt.summary.checks",
                                      label: "Checks", value: "0/2"),
                    AccessibilityNode(role: "AXGroup", identifier: "next-step",
                                      label: "Recorded next step", value: "rerun pytest"),
                ]),
                focus: AccessibilityNode(role: "AXButton", identifier: "work.breadcrumb.back",
                                         label: "Back to all records"),
                input: "Return"
            ),
            AccessibilityStep(
                name: "back",
                tree: AccessibilityNode(role: "AXWindow"),
                focus: listRow,
                input: "Escape"
            ),
        ])
    }

    private func codes(_ run: AccessibilitySmokeRun) -> Set<String> {
        Set(AccessibilitySmokeJudge.judge(run).map(\.code))
    }

    private func mutate(
        _ run: AccessibilitySmokeRun,
        step name: String,
        _ change: (inout AccessibilityStep) -> Void
    ) -> AccessibilitySmokeRun {
        var copy = run
        guard let index = copy.steps.firstIndex(where: { $0.name == name }) else { return copy }
        change(&copy.steps[index])
        return copy
    }

    // MARK: - the healthy run

    func testAHealthyRunPassesAndExitsZero() {
        XCTAssertEqual(AccessibilitySmokeJudge.judge(healthyRun()), [])
        let report = AccessibilitySmokeJudge.report(healthyRun())
        XCTAssertEqual(report.exitCode, 0)
        XCTAssertTrue(report.text.contains("PASS"), report.text)
    }

    /// The report must name each step and what held focus, so a failing run is
    /// actionable without re-running the tool.
    func testReportNamesEveryStepAndItsFocus() {
        let text = AccessibilitySmokeJudge.report(healthyRun()).text
        for step in AccessibilitySmokeJudge.requiredSteps {
            XCTAssertTrue(text.contains(step), "the report omits the \(step) step:\n\(text)")
        }
        XCTAssertTrue(text.contains("work.table.task.abc"), text)
    }

    // MARK: - each promised failure

    func testAnUnknownRowRoleFails() {
        let broken = mutate(healthyRun(), step: "work") { step in
            step.tree = AccessibilityNode(role: "AXWindow", children: [
                AccessibilityNode(role: "AXTable", identifier: "work.table", label: "Work receipts",
                                  children: [self.row(role: "AXUnknown")])
            ])
        }
        XCTAssertTrue(codes(broken).contains("work-row-role"), "\(codes(broken))")
    }

    func testARowWithoutAXPressFails() {
        let broken = mutate(healthyRun(), step: "work") { step in
            step.tree = AccessibilityNode(role: "AXWindow", children: [
                AccessibilityNode(role: "AXTable", identifier: "work.table", label: "Work receipts",
                                  children: [self.row(actions: ["AXShowMenu"])])
            ])
        }
        XCTAssertTrue(codes(broken).contains("work-row-press"), "\(codes(broken))")
    }

    func testARowWithNoNameFails() {
        let broken = mutate(healthyRun(), step: "work") { step in
            step.tree = AccessibilityNode(role: "AXWindow", children: [
                AccessibilityNode(role: "AXTable", identifier: "work.table", label: "Work receipts",
                                  children: [self.row(label: nil)])
            ])
        }
        XCTAssertTrue(codes(broken).contains("work-row-unnamed"), "\(codes(broken))")
    }

    /// A Work tree with NO row at all must fail too: an empty tree would
    /// otherwise pass every row check by vacuity, and silence is not success.
    func testAWorkTreeWithNoRowsFailsRatherThanPassingVacuously() {
        let broken = mutate(healthyRun(), step: "work") { step in
            step.tree = AccessibilityNode(role: "AXWindow", children: [
                AccessibilityNode(role: "AXGroup", identifier: "work.table", label: "Work receipts")
            ])
        }
        XCTAssertTrue(codes(broken).contains("work-rows-absent"), "\(codes(broken))")
    }

    func testFocusNeverLandingOnARowFails() {
        var broken = healthyRun()
        for index in broken.steps.indices where AccessibilitySmokeJudge.isRow(broken.steps[index].focus
            ?? AccessibilityNode()) {
            broken.steps[index].focus = AccessibilityNode(role: "AXGroup", identifier: "work.table",
                                                          label: "Work receipts")
        }
        XCTAssertTrue(codes(broken).contains("focus-never-on-a-row"), "\(codes(broken))")
    }

    func testAFocusedElementWithNoNameFails() {
        let broken = mutate(healthyRun(), step: "dashboard") { step in
            step.focus = AccessibilityNode(role: "AXGroup", identifier: "dashboard.signal.cost")
        }
        XCTAssertTrue(codes(broken).contains("focus-unnamed"), "\(codes(broken))")
    }

    func testNothingFocusedAfterAStepFails() {
        let broken = mutate(healthyRun(), step: "record") { $0.focus = nil }
        XCTAssertTrue(codes(broken).contains("focus-absent"), "\(codes(broken))")
    }

    /// Each of the four facts a reviewer opens a record for is checked on its
    /// own, so a report says WHICH one a screen reader cannot hear.
    func testEachMissingRecordFactIsReportedSeparately() {
        let removable: [(String, (AccessibilityNode) -> Bool)] = [
            ("record-verdict", { ($0.label ?? "").hasPrefix("Verdict:") }),
            ("record-coverage", { $0.identifier == "receipt.summary.coverage" }),
            ("record-checks", { $0.identifier == "receipt.summary.checks" }),
            ("record-next-step", { $0.identifier == "next-step" }),
        ]
        for (code, matches) in removable {
            let broken = mutate(healthyRun(), step: "record") { step in
                guard let tree = step.tree else { return }
                var pruned = tree
                pruned.children = (tree.children ?? []).filter { !matches($0) }
                step.tree = pruned
            }
            let found = codes(broken)
            XCTAssertEqual(found, [code], "removing the \(code) fact should report exactly it, got \(found)")
        }
    }

    func testARecordWithNoTreeFails() {
        let broken = mutate(healthyRun(), step: "record") { $0.tree = nil }
        XCTAssertTrue(codes(broken).contains("record-tree-absent"), "\(codes(broken))")
    }

    func testASkippedNavigationStepFails() {
        var broken = healthyRun()
        broken.steps.removeAll { $0.name == "record" }
        XCTAssertTrue(codes(broken).contains("step-missing"), "\(codes(broken))")
    }

    // MARK: - parsing

    func testARunDecodesFromTheDriversJSONAndFlattensNestedTrees() throws {
        let json = """
        {"app": "agentacct", "pid": 7, "steps": [
          {"name": "work", "input": "Tab",
           "focus": {"role": "AXRow", "identifier": "work.master.task.z", "label": "Ship it",
                     "actions": ["AXPress"]},
           "tree": {"role": "AXWindow", "children": [
             {"role": "AXGroup", "identifier": "work.master", "label": "Work receipts", "children": [
               {"role": "AXRow", "identifier": "work.master.task.z", "label": "Ship it",
                "actions": ["AXPress"]}]}]}}
        ]}
        """
        let run = try AccessibilitySmokeJudge.run(fromJSON: Data(json.utf8))
        XCTAssertEqual(run.steps.count, 1)
        let nodes = try XCTUnwrap(run.steps.first?.tree).flattened
        XCTAssertEqual(nodes.count, 3)
        XCTAssertEqual(nodes.filter(AccessibilitySmokeJudge.isRow).count, 1)
        XCTAssertTrue(try XCTUnwrap(run.steps.first?.tree).subtreeText.contains("Ship it"))
        // The master list's rows count as rows too, not just the table's.
        XCTAssertTrue(AccessibilitySmokeJudge.isRow(try XCTUnwrap(run.steps.first?.focus)))
    }

    func testUnnamedIsAboutTitleAndDescriptionOnlyNotValue() {
        // A value alone does not make an element speakable: VoiceOver announces
        // "(blank)" for a control with a value and no label.
        XCTAssertTrue(AccessibilityNode(role: "AXGroup", value: "3/4").isUnnamed)
        XCTAssertFalse(AccessibilityNode(role: "AXGroup", title: "Coverage").isUnnamed)
        XCTAssertFalse(AccessibilityNode(role: "AXGroup", label: "Coverage").isUnnamed)
        XCTAssertTrue(AccessibilityNode(role: "AXGroup", label: "   ").isUnnamed)
    }
}

/// The script itself: present, executable, self-documenting, and pointing at
/// the same judge file these tests cover.
final class AccessibilitySmokeScriptTests: XCTestCase {

    private var appRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()  // agentacctTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // apps/agentacct
    }

    func testScriptIsExecutableAndDocumentsItsUsage() throws {
        let script = appRoot.appendingPathComponent("Scripts/a11y-smoke.sh")
        XCTAssertTrue(FileManager.default.isExecutableFile(atPath: script.path),
                      "Scripts/a11y-smoke.sh must be executable")
        let text = try String(contentsOf: script, encoding: .utf8)
        for expected in ["USAGE", "--pid", "--judge", "EXIT CODES", "Accessibility permission",
                         "LAUNCHES NOTHING"] {
            XCTAssertTrue(text.contains(expected), "the script's header does not document \(expected)")
        }
    }

    /// The script compiles the judge these tests exercise — not a second copy.
    func testScriptCompilesTheJudgeUnderTest() throws {
        let script = appRoot.appendingPathComponent("Scripts/a11y-smoke.sh")
        let text = try String(contentsOf: script, encoding: .utf8)
        XCTAssertTrue(
            text.contains("Tests/agentacctTests/Support/AccessibilitySmokeJudge.swift"),
            "a11y-smoke.sh must compile the judge that AccessibilitySmokeJudgeTests covers"
        )
        let driver = appRoot.appendingPathComponent("Scripts/a11y-smoke-driver.swift")
        XCTAssertTrue(FileManager.default.fileExists(atPath: driver.path))
        let driverText = try String(contentsOf: driver, encoding: .utf8)
        // The driver must not judge anything itself.
        XCTAssertTrue(driverText.contains("AccessibilitySmokeJudge.report"),
                      "the driver must delegate every verdict to the shared judge")
    }
}
