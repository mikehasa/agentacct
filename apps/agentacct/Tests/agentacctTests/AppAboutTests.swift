import AppKit
import XCTest
@testable import agentacct

final class AppAboutTests: XCTestCase {
    @MainActor
    func testPresentOpensNativeAboutPanel() throws {
        let application = NSApplication.shared
        application.windows
            .filter { $0.title == "About agentacct" }
            .forEach { $0.close() }

        AppAbout.present(identity: AppBuildIdentity(infoDictionary: [
            "CFBundleShortVersionString": "0.10.1",
            "AgentacctGitCommit": "0123456789abcdef0123456789abcdef01234567",
        ]))
        RunLoop.current.run(until: Date().addingTimeInterval(0.1))

        let panel = try XCTUnwrap(
            application.windows.first { $0 is NSPanel && $0.isVisible },
            "AppKit should create a visible standard About panel; windows: \(application.windows.map(\.title))"
        )
        XCTAssertTrue(panel is NSPanel)
        XCTAssertTrue(panel.isVisible)
        let labels = Self.descendants(of: panel.contentView)
            .compactMap { ($0 as? NSTextField)?.stringValue }
        XCTAssertTrue(
            labels.contains("Version 0.10.1 (0123456789ab)"),
            "The native panel should present release and Git build together; labels: \(labels)"
        )
        panel.close()
    }

    func testPanelOptionsUseMarketingVersionAndShortSourceBuild() {
        let identity = AppBuildIdentity(infoDictionary: [
            "CFBundleShortVersionString": "0.10.1",
            "AgentacctGitCommit": "0123456789abcdef0123456789abcdef01234567",
            "AgentacctBuildDescription": "v0.10.1-3-g0123456",
        ])

        let options = AppAbout.panelOptions(for: identity)

        XCTAssertEqual(options[.applicationVersion] as? String, "0.10.1")
        XCTAssertEqual(options[.version] as? String, "0123456789ab")
    }

    func testDirtySourceBuildIsExplicitInAboutPanel() {
        let identity = AppBuildIdentity(infoDictionary: [
            "CFBundleShortVersionString": "0.10.1",
            "AgentacctGitCommit": "0123456789abcdef0123456789abcdef01234567",
            "AgentacctBuildDescription": "v0.10.1-3-g0123456-dirty",
        ])

        let options = AppAbout.panelOptions(for: identity)

        XCTAssertEqual(options[.version] as? String, "0123456789ab-dirty")
    }

    func testReleaseWithoutCommitSuppressesBundleBuildFallback() {
        let identity = AppBuildIdentity(infoDictionary: [
            "CFBundleShortVersionString": "0.10.1",
        ])

        let options = AppAbout.panelOptions(for: identity)

        XCTAssertEqual(options[.applicationVersion] as? String, "0.10.1")
        XCTAssertEqual(options[.version] as? String, "")
    }

    func testDevelopmentBuildDoesNotInventVersionMetadata() {
        let identity = AppBuildIdentity(infoDictionary: [:])

        XCTAssertTrue(AppAbout.panelOptions(for: identity).isEmpty)
    }

    private static func descendants(of view: NSView?) -> [NSView] {
        guard let view else { return [] }
        return view.subviews + view.subviews.flatMap { descendants(of: $0) }
    }
}
