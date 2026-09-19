import XCTest
@testable import agentacct

/// The Diagnostics "Recorder version" card decides whether to offer the
/// one-click Update button purely from the decoded /v1/version payload. These
/// tests pin that decision (notify + one-click, never silent, never for dev)
/// against the exact JSON the daemon sends.
final class SelfUpdatePresentationTests: XCTestCase {
    private func decodeVersion(_ json: String) throws -> VersionInfo {
        try JSONDecoder().decode(VersionInfo.self, from: Data(json.utf8))
    }

    func testUpdateOfferedWhenNewerReleaseAndPackagedInstall() throws {
        let info = try decodeVersion(#"""
        {
          "version": "0.11.0+ecc9d1def776",
          "glance_schema": "agentacct.glance.v1",
          "current": "0.11.0",
          "latest": "0.12.0",
          "update_available": true,
          "is_dev_install": false
        }
        """#)

        XCTAssertEqual(info.displayVersion, "0.11.0")
        XCTAssertEqual(info.latest, "0.12.0")
        XCTAssertTrue(info.offersInAppUpdate, "a packaged install with a newer release must offer the button")
    }

    func testUpdateNeverOfferedForDevInstallEvenIfUpdateAvailable() throws {
        let info = try decodeVersion(#"""
        {
          "version": "0.0.0+source",
          "glance_schema": "agentacct.glance.v1",
          "current": "0.0.0+source",
          "latest": "0.12.0",
          "update_available": false,
          "is_dev_install": true
        }
        """#)

        XCTAssertTrue(info.isDevInstall == true)
        XCTAssertFalse(info.offersInAppUpdate, "a dev/editable build must never be offered the in-app update")
    }

    func testUpdateNotOfferedWhenAlreadyLatest() throws {
        let info = try decodeVersion(#"""
        {
          "version": "0.12.0+abc",
          "glance_schema": "agentacct.glance.v1",
          "current": "0.12.0",
          "latest": "0.12.0",
          "update_available": false,
          "is_dev_install": false
        }
        """#)

        XCTAssertFalse(info.offersInAppUpdate)
        XCTAssertEqual(info.displayVersion, "0.12.0")
    }

    func testOlderDaemonWithoutSelfUpdateFieldsStillDecodes() throws {
        // A pre-update daemon omits the four new keys; decoding must not fail and
        // the card must fall back to the fingerprinted version and offer nothing.
        let info = try decodeVersion(#"""
        {
          "version": "0.9.0+old",
          "glance_schema": "agentacct.glance.v1",
          "store_dir": "/tmp/state"
        }
        """#)

        XCTAssertEqual(info.displayVersion, "0.9.0+old")
        XCTAssertNil(info.latest)
        XCTAssertFalse(info.offersInAppUpdate)
    }
}
