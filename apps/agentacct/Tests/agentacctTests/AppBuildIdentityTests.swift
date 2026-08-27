import XCTest
@testable import agentacct

final class AppBuildIdentityTests: XCTestCase {
    func testPackagedBuildShowsReleaseAndExactCommit() {
        let identity = AppBuildIdentity(infoDictionary: [
            "CFBundleShortVersionString": "0.10.0",
            "AgentacctGitCommit": "2b836f091ae5ce89504463b5575bea2171f731ee",
            "AgentacctBuildDescription": "v0.10.0-6-g2b836f0",
        ])

        XCTAssertEqual(identity.compactLabel, "v0.10.0 · 2b836f091ae5")
        XCTAssertEqual(
            identity.accessibilityLabel,
            "agentacct version 0.10.0, build 2b836f091ae5"
        )
        XCTAssertEqual(
            identity.detailLabel,
            "v0.10.0-6-g2b836f0 · commit 2b836f091ae5ce89504463b5575bea2171f731ee"
        )
    }

    func testDirtyBuildIsNamedEverywhere() {
        let identity = AppBuildIdentity(infoDictionary: [
            "CFBundleShortVersionString": "0.10.0",
            "AgentacctGitCommit": "2b836f091ae5ce89504463b5575bea2171f731ee",
            "AgentacctBuildDescription": "v0.10.0-6-g2b836f0-dirty",
        ])

        XCTAssertEqual(identity.compactLabel, "v0.10.0 · 2b836f091ae5 · dirty")
        XCTAssertEqual(
            identity.accessibilityLabel,
            "agentacct version 0.10.0, build 2b836f091ae5, dirty working tree"
        )
        XCTAssertEqual(
            identity.detailLabel,
            "v0.10.0-6-g2b836f0-dirty · commit 2b836f091ae5ce89504463b5575bea2171f731ee"
        )
    }

    func testUnpackagedExecutableDoesNotInventAnIdentity() {
        let identity = AppBuildIdentity(infoDictionary: [:])

        XCTAssertEqual(identity.compactLabel, "development build")
        XCTAssertEqual(identity.accessibilityLabel, "agentacct development build")
        XCTAssertEqual(identity.detailLabel, "No packaged build identity")
    }

    func testBlankMetadataIsTreatedAsMissing() {
        let identity = AppBuildIdentity(infoDictionary: [
            "CFBundleShortVersionString": "  ",
            "AgentacctGitCommit": "",
            "AgentacctBuildDescription": "\n",
        ])

        XCTAssertEqual(identity.compactLabel, "development build")
    }
}
