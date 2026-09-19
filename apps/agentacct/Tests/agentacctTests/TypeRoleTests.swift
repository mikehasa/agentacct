import XCTest
@testable import agentacct

/// Monospace is for values (numbers, ids, paths); labels set in the sans face.
final class TypeRoleTests: XCTestCase {
    func testCapsLabelsAreNotMonospaced() {
        XCTAssertFalse(WorkFontRole.labelCaps.metrics.monospaced)
        XCTAssertEqual(WorkFontRole.labelCaps.metrics.size, 11)
    }

    func testValuesStayMonospaced() {
        XCTAssertTrue(WorkFontRole.kpi.metrics.monospaced)
        XCTAssertTrue(WorkFontRole.dataSmall.metrics.monospaced)
    }
}
