import Foundation
import XCTest
@testable import agentacct

final class InteractionMotionTests: XCTestCase {
    func testButtonInteractionPhaseHasOneDeterministicWinner() {
        XCTAssertEqual(
            buttonInteractionPhase(isEnabled: true, isPressed: false, isHovering: false),
            .idle
        )
        XCTAssertEqual(
            buttonInteractionPhase(isEnabled: true, isPressed: false, isHovering: true),
            .hovered
        )
        XCTAssertEqual(
            buttonInteractionPhase(isEnabled: true, isPressed: true, isHovering: false),
            .pressed
        )
        XCTAssertEqual(
            buttonInteractionPhase(isEnabled: true, isPressed: true, isHovering: true),
            .pressed
        )
        XCTAssertEqual(
            buttonInteractionPhase(isEnabled: false, isPressed: true, isHovering: true),
            .disabled
        )
    }

    func testButtonFeedbackIsDistinctAndKeepsACompactAccessibleTarget() {
        XCTAssertGreaterThanOrEqual(ButtonFeedback.minimumHitDimension, 24)
        XCTAssertEqual(ButtonFeedback.surfaceFillOpacity(for: .idle), 0)
        XCTAssertGreaterThan(
            ButtonFeedback.surfaceFillOpacity(for: .pressed),
            ButtonFeedback.surfaceFillOpacity(for: .hovered)
        )
        XCTAssertGreaterThan(
            ButtonFeedback.quietFillOpacity(for: .pressed, prominent: false),
            ButtonFeedback.quietFillOpacity(for: .hovered, prominent: false)
        )
        XCTAssertLessThan(ButtonFeedback.labelOpacity(for: .disabled), 0.5)
    }

    func testInteractiveControlsDoNotBypassSharedFeedbackPolicy() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let sourceRoot = packageRoot.appendingPathComponent("Sources/agentacct", isDirectory: true)
        let sourceFiles = try FileManager.default.contentsOfDirectory(
            at: sourceRoot,
            includingPropertiesForKeys: nil
        ).filter { $0.pathExtension == "swift" }

        let forbidden: [(needle: String, reason: String)] = [
            (".buttonStyle(.plain)", "plain buttons bypass shared hover, press, and focus feedback"),
            (".onTapGesture", "pointer-only tap gestures must be native keyboard-accessible buttons"),
        ]
        var violations: [String] = []
        let buttonPattern = try NSRegularExpression(
            pattern: #"\bButton(?:<[^>]+>)?\s*(?:\(|\{)"#
        )
        let menuPattern = try NSRegularExpression(pattern: #"\bMenu\s*\{"#)
        let stylePattern = try NSRegularExpression(pattern: #"\.buttonStyle\s*\("#)

        for file in sourceFiles.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
            let source = try String(contentsOf: file, encoding: .utf8)
            let sourceRange = NSRange(source.startIndex..., in: source)
            let controlCount = buttonPattern.numberOfMatches(in: source, range: sourceRange)
                + menuPattern.numberOfMatches(in: source, range: sourceRange)
            let explicitStyleCount = stylePattern.numberOfMatches(in: source, range: sourceRange)
            if explicitStyleCount < controlCount {
                violations.append(
                    "\(file.lastPathComponent): \(controlCount) button/menu controls but only "
                    + "\(explicitStyleCount) explicit button styles"
                )
            }
            for (lineIndex, line) in source.components(separatedBy: .newlines).enumerated() {
                for rule in forbidden where line.contains(rule.needle) {
                    violations.append(
                        "\(file.lastPathComponent):\(lineIndex + 1): \(rule.reason)"
                    )
                }
            }
        }

        XCTAssertTrue(violations.isEmpty, violations.joined(separator: "\n"))
    }

    func testWorkRecordPhaseNeverPresentsAStaleReceiptAsLoaded() {
        XCTAssertEqual(
            workRecordPhaseKey(
                selectedTaskId: "task_new",
                sessionId: nil,
                unresolvedSessionId: nil,
                receiptTaskId: "task_old",
                errorPresent: false
            ),
            .loading(taskId: "task_new")
        )
        XCTAssertEqual(
            workRecordPhaseKey(
                selectedTaskId: "task_new",
                sessionId: nil,
                unresolvedSessionId: nil,
                receiptTaskId: "task_new",
                errorPresent: false
            ),
            .loaded(taskId: "task_new")
        )
        XCTAssertEqual(
            workRecordPhaseKey(
                selectedTaskId: "task_new",
                sessionId: nil,
                unresolvedSessionId: nil,
                receiptTaskId: nil,
                errorPresent: true
            ),
            .failed(taskId: "task_new")
        )
    }

    func testWorkRecordPhaseOnlyPresentsFailureForTheSelectedTask() {
        let error = workReceiptRefreshError(
            selectedTaskId: "task_new",
            errorTaskId: "task_old",
            error: "receipt fetch failed: offline"
        )

        XCTAssertNil(error)
        XCTAssertEqual(
            workRecordPhaseKey(
                selectedTaskId: "task_new",
                sessionId: nil,
                unresolvedSessionId: nil,
                receiptTaskId: nil,
                errorPresent: error != nil
            ),
            .loading(taskId: "task_new")
        )
    }

    func testWorkRecordPhasePreservesUnresolvedAndEmptyStates() {
        XCTAssertEqual(
            workRecordPhaseKey(
                selectedTaskId: nil,
                sessionId: "session_a",
                unresolvedSessionId: "session_a",
                receiptTaskId: nil,
                errorPresent: false
            ),
            .unresolved(sessionId: "session_a")
        )
        XCTAssertEqual(
            workRecordPhaseKey(
                selectedTaskId: nil,
                sessionId: nil,
                unresolvedSessionId: nil,
                receiptTaskId: nil,
                errorPresent: false
            ),
            .empty
        )
    }

    func testChartGeometryAnimationIsBoundedAndReducedMotionSafe() {
        XCTAssertTrue(Motion.animatesChartGeometry(bucketCount: 7, reduceMotion: false))
        XCTAssertTrue(Motion.animatesChartGeometry(bucketCount: 30, reduceMotion: false))
        XCTAssertFalse(Motion.animatesChartGeometry(bucketCount: 31, reduceMotion: false))
        XCTAssertFalse(Motion.animatesChartGeometry(bucketCount: 90, reduceMotion: false))
        XCTAssertFalse(Motion.animatesChartGeometry(bucketCount: 7, reduceMotion: true))
    }

    @MainActor
    func testSetupPhaseKeyIgnoresChangingStatusAndErrorCopy() {
        XCTAssertEqual(setupPhaseKey(for: .idle), .idle)
        XCTAssertEqual(setupPhaseKey(for: .working("Installing…")), .working)
        XCTAssertEqual(setupPhaseKey(for: .working("Configuring…")), .working)
        XCTAssertEqual(setupPhaseKey(for: .done), .done)
        XCTAssertEqual(setupPhaseKey(for: .failed("offline")), .failed)
        XCTAssertEqual(setupPhaseKey(for: .failed("permission denied")), .failed)
    }

    func testRefreshProgressRequiresBothActiveRefreshAndElapsedDelay() {
        XCTAssertFalse(refreshProgressVisible(isRefreshing: false, delayElapsed: false))
        XCTAssertFalse(refreshProgressVisible(isRefreshing: true, delayElapsed: false))
        XCTAssertFalse(refreshProgressVisible(isRefreshing: false, delayElapsed: true))
        XCTAssertTrue(refreshProgressVisible(isRefreshing: true, delayElapsed: true))
    }
}
