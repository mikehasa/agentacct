import XCTest
@testable import agentacct

/// The receipt poll skips republishing when the fingerprint of the raw response
/// matches the one already on screen, so a fingerprint that missed a real
/// change would leave a stale record page showing. These lock the property the
/// skip depends on: equal ONLY for identical bytes.
final class PayloadFingerprintTests: XCTestCase {
    func testIdenticalBytesFingerprintEqual() {
        let bytes = Data("{\"task\":\"t1\",\"checks\":3}".utf8)
        XCTAssertEqual(PayloadFingerprint(bytes), PayloadFingerprint(bytes))
        XCTAssertEqual(PayloadFingerprint(bytes), PayloadFingerprint(Data(bytes)))
    }

    func testOneChangedByteChangesTheFingerprint() {
        let before = Data("{\"task\":\"t1\",\"checks\":3}".utf8)
        let after = Data("{\"task\":\"t1\",\"checks\":4}".utf8)
        XCTAssertEqual(before.count, after.count, "the interesting case is a same-length edit")
        XCTAssertNotEqual(PayloadFingerprint(before), PayloadFingerprint(after))
    }

    func testAppendedBytesChangeTheFingerprint() {
        let before = Data("{\"events\":[]}".utf8)
        let after = Data("{\"events\":[1]}".utf8)
        XCTAssertNotEqual(PayloadFingerprint(before), PayloadFingerprint(after))
    }

    func testEmptyAndNonEmptyDiffer() {
        XCTAssertNotEqual(PayloadFingerprint(Data()), PayloadFingerprint(Data([0])))
        XCTAssertEqual(PayloadFingerprint(Data()), PayloadFingerprint(Data()))
    }

    func testReorderedBytesDiffer() {
        XCTAssertNotEqual(
            PayloadFingerprint(Data([1, 2, 3])),
            PayloadFingerprint(Data([3, 2, 1]))
        )
    }
}

/// The republish gate itself. PayloadFingerprintTests above pins the hash;
/// these pin the DECISION, and specifically the direction that fails quietly:
/// a receipt that really changed must always reach the screen. A gate that is
/// too eager costs a wasted rebuild; a gate that is too clever shows the
/// reviewer stale evidence and says nothing.
final class ReceiptRepublishGateTests: XCTestCase {
    private let a = PayloadFingerprint(Data("receipt-a".utf8))
    private let b = PayloadFingerprint(Data("receipt-b".utf8))

    func testIdenticalBytesForTheShownTaskAreNotRepublished() {
        XCTAssertTrue(DashboardStore.receiptIsAlreadyOnScreen(
            showing: "task_1", taskId: "task_1", incoming: a, stored: a))
    }

    func testAChangedReceiptIsAlwaysRepublished() {
        XCTAssertFalse(DashboardStore.receiptIsAlreadyOnScreen(
            showing: "task_1", taskId: "task_1", incoming: b, stored: a),
            "a receipt whose bytes changed must reach the screen")
    }

    func testADifferentTaskIsAlwaysRepublished() {
        XCTAssertFalse(DashboardStore.receiptIsAlreadyOnScreen(
            showing: "task_other", taskId: "task_1", incoming: a, stored: a),
            "the page is showing another task, so this one has never been drawn")
    }

    func testNothingOnScreenYetIsAlwaysRepublished() {
        XCTAssertFalse(DashboardStore.receiptIsAlreadyOnScreen(
            showing: nil, taskId: "task_1", incoming: a, stored: a))
    }

    func testAnAbsentIncomingFingerprintFailsOpen() {
        XCTAssertFalse(DashboardStore.receiptIsAlreadyOnScreen(
            showing: "task_1", taskId: "task_1", incoming: nil, stored: a),
            "without a fingerprint the gate cannot prove equality, so it must publish")
    }

    func testNoStoredFingerprintFailsOpen() {
        XCTAssertFalse(DashboardStore.receiptIsAlreadyOnScreen(
            showing: "task_1", taskId: "task_1", incoming: a, stored: nil))
    }
}
