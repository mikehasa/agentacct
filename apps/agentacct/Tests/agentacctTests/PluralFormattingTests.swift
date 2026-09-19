import XCTest
@testable import agentacct

/// Every user-facing count carries a noun numbered to match it.
final class PluralFormattingTests: XCTestCase {
    func testSingularAndPlural() {
        XCTAssertEqual(Fmt.count(1, "session"), "1 session")
        XCTAssertEqual(Fmt.count(0, "session"), "0 sessions")
        XCTAssertEqual(Fmt.count(3, "session"), "3 sessions")
    }

    func testExplicitPluralForm() {
        XCTAssertEqual(Fmt.count(1, "entry", "entries"), "1 entry")
        XCTAssertEqual(Fmt.count(2, "entry", "entries"), "2 entries")
    }

    func testCompoundNounPluralizesTheLastWord() {
        XCTAssertEqual(Fmt.count(1, "tool call"), "1 tool call")
        XCTAssertEqual(Fmt.count(2, "failed check"), "2 failed checks")
    }
}
