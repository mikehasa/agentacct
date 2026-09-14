import XCTest
import Foundation
@testable import agentacct

/// The frozen CLI embeds a `Python.framework`, whose canonical layout uses
/// symlinks (`Versions/Current`, the top-level binary and `Resources`).
/// codesign needs those symlinks, so the payload identity must accept aliases
/// that are safe — relative and resolving inside the payload — while still
/// rejecting anything that could alias bytes from outside a stable path.
final class CLIPayloadSymlinkIdentityTests: XCTestCase {
    private var root: URL!

    override func setUpWithError() throws {
        root = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("cli-payload-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        if let root { try? FileManager.default.removeItem(at: root) }
    }

    /// Build a framework whose aliases are all relative and in-payload.
    private func writeFramework() throws {
        let fm = FileManager.default
        let version = root.appendingPathComponent("Python.framework/Versions/3.14", isDirectory: true)
        try fm.createDirectory(at: version, withIntermediateDirectories: true)
        try Data("synthetic executable".utf8).write(to: version.appendingPathComponent("Python"))
        try fm.createDirectory(at: version.appendingPathComponent("Resources"), withIntermediateDirectories: true)
        try Data("fixture".utf8).write(to: version.appendingPathComponent("Resources/Info.plist"))
        try fm.createSymbolicLink(atPath: root.appendingPathComponent("Python.framework/Versions/Current").path,
                                  withDestinationPath: "3.14")
        try fm.createSymbolicLink(atPath: root.appendingPathComponent("Python.framework/Python").path,
                                  withDestinationPath: "Versions/Current/Python")
        try fm.createSymbolicLink(atPath: root.appendingPathComponent("Python.framework/Resources").path,
                                  withDestinationPath: "Versions/Current/Resources")
        try fm.createSymbolicLink(atPath: root.appendingPathComponent("Python").path,
                                  withDestinationPath: "Python.framework/Python")
    }

    func testInPayloadFrameworkSymlinksAreAccepted() throws {
        try writeFramework()
        let identity = CLIPayloadInspector.identity(at: root)
        XCTAssertNotNil(identity, "a framework with safe relative aliases must produce an identity")
        XCTAssertTrue(identity?.isValid == true)
    }

    func testAbsoluteSymlinkTargetIsRejected() throws {
        let fm = FileManager.default
        try Data("x".utf8).write(to: root.appendingPathComponent("agentacct"))
        // Absolute target, even one pointing inside the payload, is rejected:
        // an installed/relocated payload must not depend on the build path.
        try fm.createSymbolicLink(atPath: root.appendingPathComponent("alias").path,
                                  withDestinationPath: root.appendingPathComponent("agentacct").path)
        XCTAssertNil(CLIPayloadInspector.identity(at: root))
    }

    func testEscapingSymlinkTargetIsRejected() throws {
        let fm = FileManager.default
        try Data("x".utf8).write(to: root.appendingPathComponent("agentacct"))
        try fm.createSymbolicLink(atPath: root.appendingPathComponent("alias").path,
                                  withDestinationPath: "../../../../etc/hosts")
        XCTAssertNil(CLIPayloadInspector.identity(at: root))
    }

    func testSymlinkThroughSymlinkEscapeIsRejected() throws {
        let fm = FileManager.default
        try fm.createDirectory(at: root.appendingPathComponent("sub"), withIntermediateDirectories: true)
        // `pivot` is a harmless forward alias; `escape` looks in-root lexically
        // (pivot/../x) but "pivot/.." resolves against pivot's physical target,
        // so it would climb above the payload. The ".." makes it rejected.
        try fm.createSymbolicLink(atPath: root.appendingPathComponent("pivot").path, withDestinationPath: "sub")
        try fm.createSymbolicLink(atPath: root.appendingPathComponent("escape").path,
                                  withDestinationPath: "pivot/../../outside")
        XCTAssertNil(CLIPayloadInspector.identity(at: root))
    }

    func testIdentityIsTamperEvidentToSymlinkTarget() throws {
        let fm = FileManager.default
        try Data("one".utf8).write(to: root.appendingPathComponent("a"))
        try Data("two".utf8).write(to: root.appendingPathComponent("b"))
        try fm.createSymbolicLink(atPath: root.appendingPathComponent("link").path, withDestinationPath: "a")
        let first = CLIPayloadInspector.identity(at: root)
        XCTAssertNotNil(first)

        try fm.removeItem(at: root.appendingPathComponent("link"))
        try fm.createSymbolicLink(atPath: root.appendingPathComponent("link").path, withDestinationPath: "b")
        let second = CLIPayloadInspector.identity(at: root)
        XCTAssertNotNil(second)

        XCTAssertNotEqual(first?.sha256, second?.sha256,
                          "retargeting an alias must change the payload fingerprint")
    }

    func testSymlinkAndRegularFileAtSamePathDifferInIdentity() throws {
        let fm = FileManager.default
        try Data("target".utf8).write(to: root.appendingPathComponent("a"))
        try fm.createSymbolicLink(atPath: root.appendingPathComponent("entry").path, withDestinationPath: "a")
        let asLink = CLIPayloadInspector.identity(at: root)

        try fm.removeItem(at: root.appendingPathComponent("entry"))
        try Data("a".utf8).write(to: root.appendingPathComponent("entry"))
        let asFile = CLIPayloadInspector.identity(at: root)

        XCTAssertNotNil(asLink)
        XCTAssertNotNil(asFile)
        XCTAssertNotEqual(asLink?.sha256, asFile?.sha256,
                          "a symlink and a regular file at the same path are distinct entries")
    }
}
