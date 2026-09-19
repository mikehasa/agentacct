import Foundation
import XCTest
@testable import agentacct

/// `.accessibilityElement(children: .ignore)` on a Button REPLACES the button's
/// own accessibility element (K118). The replacement is a plain group: it has
/// no button role and no press action, so VoiceOver reads it as `AXUnknown`
/// and cannot activate it — the control is visible, focusable with the mouse,
/// and unreachable by keyboard-driven assistive technology.
///
/// A Button already collapses its label into ONE element, so the modifier buys
/// nothing there: `.accessibilityLabel` alone replaces what it says while the
/// role and the press action survive. On a non-interactive container (an
/// `HStack` of Texts, a metric tile) the modifier is correct and stays.
///
/// This lint fires only on the failing shape: the modifier applied directly to
/// a control's modifier chain, which in this codebase always carries a
/// `.buttonStyle(…)` on a line just above it.
final class ButtonAccessibilityRoleLintTests: XCTestCase {

    /// How far above the modifier a `.buttonStyle(…)` still means "this chain
    /// belongs to a Button". Every real site in this codebase is within three
    /// lines (the intervening lines are layout modifiers such as `.padding`).
    private static let chainWindow = 6

    private var sourcesDirectory: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()  // agentacctTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // apps/agentacct
            .appendingPathComponent("Sources/agentacct", isDirectory: true)
    }

    func testNoButtonReplacesItsOwnAccessibilityElement() throws {
        let files = try FileManager.default.contentsOfDirectory(
            at: sourcesDirectory, includingPropertiesForKeys: nil
        ).filter { $0.pathExtension == "swift" }
        XCTAssertFalse(files.isEmpty, "no Swift sources found at \(sourcesDirectory.path)")

        var violations: [String] = []
        for file in files.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
            let source = try String(contentsOf: file, encoding: .utf8)
            for line in Self.violations(in: source) {
                violations.append("\(file.lastPathComponent):\(line)")
            }
        }

        XCTAssertTrue(
            violations.isEmpty,
            """
            A Button's accessibility element is replaced by \
            `.accessibilityElement(children: .ignore)`, so it loses its button \
            role and its press action (K118). Delete the modifier — \
            `.accessibilityLabel` / `.accessibilityValue` / `.accessibilityHint` \
            already say what the button says:
            \(violations.joined(separator: "\n"))
            """
        )
    }

    /// The lint's own contract: it must catch the failing shape and stay silent
    /// on a container that legitimately collapses its children.
    func testLintCatchesTheButtonShapeAndIgnoresPlainContainers() {
        let bad = """
        Button(action: open) { label }
        .buttonStyle(QuietButtonStyle())
        .padding(.horizontal, -6)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(title)
        """
        XCTAssertEqual(Self.violations(in: bad), [4])

        let good = """
        HStack { Text(name); Text(value) }
        .padding(.vertical, 5)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(rowLabel)
        Button(action: open) { label }
        .buttonStyle(QuietButtonStyle())
        .accessibilityLabel(title)
        """
        XCTAssertEqual(Self.violations(in: good), [])
    }

    /// A `.buttonStyle` far above (a different view in the same body) is not
    /// this chain, so it must not make a container's collapse a violation.
    func testDistantButtonStyleIsNotTheSameChain() {
        let source = """
        Button(action: open) { label }
        .buttonStyle(QuietButtonStyle())
        a()
        b()
        c()
        d()
        e()
        f()
        HStack { Text(name) }
        .accessibilityElement(children: .ignore)
        """
        XCTAssertEqual(Self.violations(in: source), [])
    }

    // MARK: - implementation

    /// 1-based line numbers of every `.accessibilityElement(children: .ignore)`
    /// that sits in a modifier chain a `.buttonStyle(…)` opened.
    static func violations(in source: String) -> [Int] {
        let lines = source.components(separatedBy: "\n")
        var found: [Int] = []
        for (index, line) in lines.enumerated() {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard trimmed.hasPrefix("//") == false,
                  trimmed.contains(".accessibilityElement(children: .ignore)")
            else { continue }
            let start = max(0, index - chainWindow)
            let precedingChain = lines[start..<index].contains { candidate in
                let text = candidate.trimmingCharacters(in: .whitespaces)
                return !text.hasPrefix("//") && text.hasPrefix(".buttonStyle(")
            }
            if precedingChain { found.append(index + 1) }
        }
        return found
    }
}
