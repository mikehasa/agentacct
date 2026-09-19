import Foundation
import XCTest
@testable import agentacct

/// One font role per presentation field on every surface (K10). Mono is for
/// updating numerals, ids, paths, commands and timestamps; the reducer's prose
/// fields (gap line, named absence, qualifier, reset phrase, subtitle) are
/// sans wherever they are drawn.
final class FieldFontRoleTests: XCTestCase {
    func testProseFieldRolesAreSans() {
        for (field, role) in FieldFont.all {
            XCTAssertFalse(role.metrics.monospaced, "\(field) is set in a mono role")
            XCTAssertGreaterThanOrEqual(role.metrics.size, 12, "\(field) below the 12px floor")
        }
        // Chips name words; only identifiers keep the mono face.
        XCTAssertFalse(ChipFace.role(mono: false).metrics.monospaced)
        XCTAssertTrue(ChipFace.role(mono: true).metrics.monospaced)
    }

    /// Every surface that draws one of these fields uses a sans role: a mention
    /// of the field is never followed, within its statement, by a mono role.
    func testNoSurfaceSetsAProseFieldInTheMonoFace() throws {
        let violations = try Self.sources().flatMap { name, source in
            Self.monoProseViolations(in: source).map { "\(name):\($0)" }
        }
        XCTAssertTrue(
            violations.isEmpty,
            "prose fields set in a mono role (use FieldFont):\n" + violations.joined(separator: "\n")
        )
    }

    /// The scanner catches the known forms and ignores sans roles and metrics.
    func testScannerCatchesMonoProseAndIgnoresSansRoles() {
        let bad = """
        if let gap = presentation.gapLine {
            Text(gap)
                .workFont(.dataSmall).foregroundStyle(Theme.muted)
        }
        Text(item.qualifier).font(Type.dataSmall)
        """
        XCTAssertEqual(Self.monoProseViolations(in: bad).count, 2)
        let good = """
        if let gap = presentation.gapLine {
            Text(gap).workFont(FieldFont.gapLine)
        }
        Text(primary.resetText)
            .font(Type.caption)
        Text(presentation.costDisplayText).workFont(.dataSmall)
        """
        XCTAssertEqual(Self.monoProseViolations(in: good).count, 0)
    }

    /// The ONE face rule: the role decides the face, and it never changes the
    /// reading size — so the same string keeps its place in a column whichever
    /// role it has.
    func testTheOneFaceRuleFollowsTheRoleAtOneReadingSize() {
        for metricRole in [WorkFontRole.kpi, .dataSmall, .dataSmallSemibold] {
            XCTAssertTrue(
                FieldFont.value(metricRole, isMetric: true).metrics.monospaced,
                "a measured figure lost the metric face"
            )
            XCTAssertFalse(
                FieldFont.value(metricRole, isMetric: false).metrics.monospaced,
                "a named state kept the metric face"
            )
        }
        // A caption-sized reading stays caption-sized in either face.
        XCTAssertEqual(
            FieldFont.value(.dataSmall, isMetric: false).metrics.size,
            FieldFont.value(.dataSmall, isMetric: true).metrics.size
        )
        // Roles that are already sans pass through untouched.
        for sansRole in [WorkFontRole.body, .caption, .captionSemibold, .titleCard] {
            XCTAssertEqual(FieldFont.prose(sansRole), sansRole)
        }
    }

    /// K10, the defect itself: `not gradeable` was MONO in the Work table's
    /// coverage cell and SANS in the record's coverage tile — same task, same
    /// string. One role source now answers for both, so the two surfaces can no
    /// longer disagree.
    func testTheSameCoverageStringTakesTheSameFaceOnEverySurface() throws {
        let notGradeable = try Self.evidence(#"""
        {"key": "none", "gradeable": false, "checkable_total": 0,
         "coverage_row": "not gradeable",
         "coverage_tile": {"value": null, "absent": "not gradeable",
                           "qualifier": "only step stopped: handed off"}}
        """#)
        let graded = try Self.evidence(#"""
        {"key": "independently_checked", "gradeable": true, "checkable_total": 4,
         "checked_total": 4, "coverage_row": "4/4 independently checked",
         "coverage_tile": {"value": "4/4", "absent": null,
                           "qualifier": "independently checked completed steps"}}
        """#)

        let absence = ReceiptCoveragePresentation(evidence: notGradeable)
        XCTAssertFalse(absence.valueIsMetric, "a named absence claimed the metric role")
        let ratio = ReceiptCoveragePresentation(evidence: graded)
        XCTAssertTrue(ratio.valueIsMetric, "a measured ratio lost the metric role")

        // Table cell (.dataSmall) and record tile (.kpi) resolve from the SAME
        // role, so the face agrees even though the sizes differ.
        for surfaceRole in [WorkFontRole.dataSmall, .kpi] {
            XCTAssertFalse(FieldFont.value(surfaceRole, isMetric: absence.valueIsMetric).metrics.monospaced)
            XCTAssertTrue(FieldFont.value(surfaceRole, isMetric: ratio.valueIsMetric).metrics.monospaced)
        }
    }

    /// A literal prose string set in a mono role fails, wherever it is written.
    func testNoSurfaceSetsALiteralProseStringInTheMonoFace() throws {
        let violations = try Self.sources().flatMap { name, source in
            Self.monoLiteralProseViolations(in: source).map { "\(name):\($0)" }
        }
        XCTAssertTrue(
            violations.isEmpty,
            "prose literals set in a mono role (mono is for figures, ids, paths and commands):\n"
                + violations.joined(separator: "\n")
        )
    }

    /// The literal scanner catches a sentence in mono and ignores identifiers,
    /// single words and sans roles.
    func testLiteralScannerCatchesProseAndIgnoresIdentifiers() {
        let bad = """
        Text("not gradeable").workFont(.dataSmall)
        Text("no usage recorded").font(Type.kpi)
        """
        XCTAssertEqual(Self.monoLiteralProseViolations(in: bad).count, 2)
        let good = """
        Text("not gradeable").workFont(FieldFont.absence)
        Text("agentacct-gui").workFont(.dataSmall)
        Text("--store-dir").workFont(.dataSmall)
        Text(verbatim: "4/4").workFont(.dataSmall)
        Text("no usage recorded").workFont(.body)
        """
        XCTAssertEqual(Self.monoLiteralProseViolations(in: good).count, 0)
    }

    /// Lines where a literal of two or more lower-case words — prose, not an
    /// identifier, path, command or figure — is drawn in a mono role.
    static func monoLiteralProseViolations(in source: String) -> [Int] {
        var results: [Int] = []
        for (index, line) in source.components(separatedBy: "\n").enumerated() {
            let code = line.components(separatedBy: "//").first ?? line
            let range = NSRange(location: 0, length: (code as NSString).length)
            guard monoRole.firstMatch(in: code, range: range) != nil else { continue }
            guard let literal = literalPattern.firstMatch(in: code, range: range) else { continue }
            let text = (code as NSString).substring(with: literal.range(at: 1))
            guard Self.readsAsProse(text) else { continue }
            results.append(index + 1)
        }
        return results
    }

    /// Two or more words made only of letters — an identifier, a path, a flag,
    /// a command or anything carrying a figure is not prose.
    static func readsAsProse(_ text: String) -> Bool {
        let words = text.split(separator: " ")
        guard words.count >= 2 else { return false }
        return words.allSatisfy { word in
            word.allSatisfy { $0.isLetter }
        }
    }

    private static let literalPattern = try! NSRegularExpression(
        pattern: #"Text\(\s*"([^"\\]*)""#
    )

    private static func evidence(_ json: String) throws -> ReceiptEvidence {
        try JSONDecoder().decode(ReceiptEvidence.self, from: Data(json.utf8))
    }

    private static let fieldPattern = try! NSRegularExpression(
        pattern: #"\b(gapLine|ledgerText|qualifier|resetText|freshnessLine|coverageText|coverageValue)\b"#
    )
    private static let monoRole = try! NSRegularExpression(
        pattern: #"workFont\(\.(dataSmall|dataSmallSemibold|kpi|labelCaps)\)|Type\.(dataSmall|dataSmallSemibold|kpi|data|labelCaps)\b"#
    )

    /// Line numbers where a prose-field mention is followed, within the next
    /// four lines of the same view statement, by a mono role.
    static func monoProseViolations(in source: String) -> [Int] {
        let lines = source.components(separatedBy: "\n")
        var results: [Int] = []
        for (index, line) in lines.enumerated() {
            let code = line.components(separatedBy: "//").first ?? line
            let range = NSRange(location: 0, length: (code as NSString).length)
            guard fieldPattern.firstMatch(in: code, range: range) != nil else { continue }
            // Declarations and model code are not render sites.
            if code.contains("let ") && !code.contains("if let") || code.contains("var ") || code.contains("case ") { continue }
            var window = code
            var hasText = code.contains("Text(")
            for next in lines.dropFirst(index + 1).prefix(4) {
                let trimmed = next.trimmingCharacters(in: .whitespaces)
                // The statement ends at the next view or control-flow line; a
                // binding line (`if let gap = …gapLine {`) owns the one Text
                // that follows it.
                if trimmed.hasPrefix("Text(") {
                    if hasText { break }
                    hasText = true
                } else if trimmed.hasPrefix("if ") || trimmed.hasPrefix("}") {
                    break
                }
                window += "\n" + (next.components(separatedBy: "//").first ?? next)
            }
            let windowRange = NSRange(location: 0, length: (window as NSString).length)
            if monoRole.firstMatch(in: window, range: windowRange) != nil {
                results.append(index + 1)
            }
        }
        return results
    }

    private static func sources() throws -> [(String, String)] {
        let directory = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/agentacct", isDirectory: true)
        return try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "swift" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
            .map { ($0.lastPathComponent, try String(contentsOf: $0, encoding: .utf8)) }
    }
}
