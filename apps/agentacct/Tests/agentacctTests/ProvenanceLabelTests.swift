import XCTest
@testable import agentacct

/// Provenance chips show words, not payload tokens; unknown text passes through.
final class ProvenanceLabelTests: XCTestCase {
    func testKnownTokensGetTheTerminalUILabels() {
        XCTAssertEqual(ProvenanceChip.label(for: "client_log"), "Client log")
        XCTAssertEqual(ProvenanceChip.label(for: "mcp"), "MCP record")
        XCTAssertEqual(ProvenanceChip.label(for: "hook"), "Client hook")
        XCTAssertEqual(ProvenanceChip.label(for: "transcript_scan"), "Transcript scan")
        XCTAssertEqual(ProvenanceChip.label(for: "none"), "Not captured")
    }

    func testClientNamesPassThrough() {
        XCTAssertEqual(ProvenanceChip.label(for: "claude-code"), "claude-code")
    }
}
