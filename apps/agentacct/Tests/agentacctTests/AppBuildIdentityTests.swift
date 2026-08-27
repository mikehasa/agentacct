import XCTest
@testable import agentacct

final class AppBuildIdentityTests: XCTestCase {
    func testPackagedBuildProvidesReleaseAndShortSourceBuild() {
        let identity = AppBuildIdentity(infoDictionary: [
            "CFBundleShortVersionString": "0.10.0",
            "AgentacctGitCommit": "2b836f091ae5ce89504463b5575bea2171f731ee",
            "AgentacctBuildDescription": "v0.10.0-6-g2b836f0",
        ])

        XCTAssertEqual(identity.aboutPanelApplicationVersion, "0.10.0")
        XCTAssertEqual(identity.aboutPanelBuildVersion, "2b836f091ae5")
    }

    func testDirtyBuildIsNamedForAboutPanel() {
        let identity = AppBuildIdentity(infoDictionary: [
            "CFBundleShortVersionString": "0.10.0",
            "AgentacctGitCommit": "2b836f091ae5ce89504463b5575bea2171f731ee",
            "AgentacctBuildDescription": "v0.10.0-6-g2b836f0-dirty",
        ])

        XCTAssertEqual(identity.aboutPanelBuildVersion, "2b836f091ae5-dirty")
    }

    func testUnpackagedExecutableDoesNotInventAnIdentity() {
        let identity = AppBuildIdentity(infoDictionary: [:])

        XCTAssertNil(identity.aboutPanelApplicationVersion)
        XCTAssertNil(identity.aboutPanelBuildVersion)
    }

    func testBlankMetadataIsTreatedAsMissing() {
        let identity = AppBuildIdentity(infoDictionary: [
            "CFBundleShortVersionString": "  ",
            "AgentacctGitCommit": "",
            "AgentacctBuildDescription": "\n",
        ])

        XCTAssertNil(identity.aboutPanelApplicationVersion)
        XCTAssertNil(identity.aboutPanelBuildVersion)
    }
}
