import Foundation
import XCTest
@testable import agentacct

final class ConnectionsPresentationTests: XCTestCase {
    private func connection(_ json: String) -> V1Connection {
        try! JSONDecoder().decode(V1Connection.self, from: Data(json.utf8))
    }

    func testDecodesSnakeCaseKeysAndMapsActiveAgentToWizardClient() {
        let codex = connection(#"{"id":"codex","display_name":"Codex","kind":"active","configured":true,"recording_state":"healthy","scope":"watched","last_success_at":10.0,"issues":[],"status":"recording","primary_action":null}"#)
        XCTAssertEqual(codex.displayName, "Codex")
        XCTAssertEqual(codex.kind, "active")
        XCTAssertTrue(codex.configured)
        XCTAssertEqual(codex.recordingState, "healthy")
        XCTAssertEqual(codex.status, "recording")
        XCTAssertNil(codex.primaryAction)
        // An active agent maps to a wizard client so its row can Connect/Re-sync.
        XCTAssertEqual(codex.setupClient, .codex)
    }

    func testPassiveAndSemiAgentsHaveNoWizardClient() {
        // cursor is observation-only — nothing to set up, so no one-click action.
        let cursor = connection(#"{"id":"cursor","display_name":"Cursor","kind":"passive","configured":false,"recording_state":"healthy","scope":"watched","last_success_at":null,"issues":[],"status":"reading","primary_action":null}"#)
        XCTAssertNil(cursor.setupClient)
        // openclaw's MCP setup is manual, so it has no wizard client either.
        let openclaw = connection(#"{"id":"openclaw","display_name":"OpenClaw","kind":"semi","configured":false,"recording_state":null,"scope":null,"last_success_at":null,"issues":[],"status":"not_connected","primary_action":"connect_manual"}"#)
        XCTAssertNil(openclaw.setupClient)
        XCTAssertEqual(openclaw.primaryAction, "connect_manual")
    }

    func testSemiAgentReadingFromIngestionEvidenceStillHasNoWizardClient() {
        // A genuinely-importing openclaw derives "reading" from ingestion, but a
        // semi agent must NEVER be offered a one-click wizard connect — its MCP
        // step is manual (connect_manual), rendered as a note, not a button.
        let openclaw = connection(#"{"id":"openclaw","display_name":"OpenClaw","kind":"semi","configured":false,"recording_state":"healthy","scope":"watched","last_success_at":5.0,"issues":[],"status":"reading","primary_action":"connect_manual"}"#)
        XCTAssertEqual(openclaw.status, "reading")
        XCTAssertEqual(openclaw.primaryAction, "connect_manual")
        XCTAssertNil(openclaw.setupClient)
    }

    func testConfiguredButIdleActiveAgentDecodesAndKeepsAWizardClient() {
        // connected_idle is the honest "set up but not confirmed recording" state
        // (a stopped/stale watcher, or no live data yet) — still an active agent
        // that can be re-synced.
        let claude = connection(#"{"id":"claude-code","display_name":"Claude Code","kind":"active","configured":true,"recording_state":"healthy","scope":"manual","last_success_at":9.0,"issues":[],"status":"connected_idle","primary_action":null}"#)
        XCTAssertEqual(claude.status, "connected_idle")
        XCTAssertNil(claude.primaryAction)
        XCTAssertEqual(claude.setupClient, .claudeCode)
    }

    func testDegradedAgentCarriesItsPerAgentIssueForThePointToPointFix() {
        let degraded = connection(#"{"id":"codex","display_name":"Codex","kind":"active","configured":true,"recording_state":"degraded","scope":"watched","last_success_at":null,"issues":[{"code":"source_scan_failed","source":"codex","action":"Refresh","severity":"error"}],"status":"needs_attention","primary_action":"resync"}"#)
        XCTAssertEqual(degraded.status, "needs_attention")
        XCTAssertEqual(degraded.primaryAction, "resync")
        XCTAssertEqual(degraded.issues.first?.severity, "error")
        XCTAssertEqual(degraded.setupClient, .codex)
    }
}
