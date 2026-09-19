import Foundation
import XCTest
@testable import agentacct

/// The app is a RENDERER. Every decision word, evidence-tier word, gap label,
/// cost-basis word, source label, attention reason and named absence is decided
/// once in Python (`src/agentacct/display_vocabulary.py` + the receipt
/// reducers) and arrives on the payload; Swift prints it. A string literal in
/// `Sources/agentacct` that spells one of those words is a SECOND vocabulary —
/// it survives a wording change in Python and starts disagreeing with the CLI,
/// the TUI and the exported Markdown for the same fact.
///
/// This lint scans the Swift sources for those literals outside the allowlists
/// below. It is the Swift half of the cross-surface guardrail; the Python half
/// is `tests/test_surface_parity.py`, which additionally pins
/// `Self.bannedVocabulary` to the Python tables so this list cannot go stale.
///
/// ## What is allowed, and why
///
/// 1. **Payload keys.** A literal in a comparison (`state == "unpriced"`) or a
///    `case` label is a key the payload carries, not text a reader sees.
/// 2. **`PayloadAbsence` (V1Model.swift).** The documented neutral fallbacks a
///    model shows ONLY when a reducer field is missing from an older payload.
///    A missing field cannot come from the payload, so its name lives here.
/// 3. **Snapshot harnesses and fixtures.** Those files SYNTHESIZE a daemon
///    payload for offline rendering: their strings are data standing in for the
///    reducer, not a rendering copy.
/// 4. **Named sites.** A short, explicit list of other fallbacks, each with the
///    reason it must stay — see `allowedSites`.
///
/// A word whose LABEL is identical to its payload KEY cannot be classified by a
/// scanner at all, so it is exempt by construction — see `keyShapedExemptions`.
final class VocabularyLintTests: XCTestCase {

    // MARK: - the vocabulary Swift may not spell

    /// Every human-facing label the Python vocabulary owns. Kept in one flat
    /// list so the Python parity suite can diff it against the real tables.
    static let bannedVocabulary: Set<String> = [
        // DECISION_LABELS — the decision axis.
        "Verified", "Finding", "Finding superseded", "Finding resolved",
        "Blocker resolved", "Failed", "Blocked", "Reported", "Resolved",
        "Mostly done", "Handed off", "Ended open", "Inactive", "In progress",
        "No work recorded", "No outcome recorded",
        // TIER_LABELS — the evidence axis ("unchecked" is key-shaped; see below).
        "externally verified", "independently checked", "self-checked",
        // gap / coverage words.
        "Not yet proven", "claimed, unproven", "not gradeable", "not graded",
        "not check-relevant",
        // COST_BASIS_LABELS + the named cost absences.
        "pricing estimate", "client-reported", "provider-billed",
        "user subscription", "subscription", "subscription cost unavailable",
        "mixed basis", "cost basis not reported", "subscription equivalent",
        "approximate subscription share", "no usage recorded", "unpriced",
        // SOURCE_LABELS + ASSERTED_BY_LABELS — who said it.
        "Client log", "Hook-captured", "Agent-reported", "Transcript scan",
        "CI or provider", "Git", "No source recorded", "Inferred", "You",
        "Machine check",
        // ATTENTION_REASON_LABELS — why a Task needs you.
        "Failed check", "Blocker", "Failed run", "Check could not run",
        // CHECK_RESULT_LABELS — a recorded check's own words.
        "Passed", "Could not run", "Skipped", "Result not recorded",
        "Could not reproduce",
        // The absence budget. Every "we did not capture this" on the record
        // page collapses into ONE line composed by `not_captured_line()`; a
        // Swift-spelled copy would be a second budget with its own noun order.
        "not captured",
        // The record page's first EXEMPT absence. The reducer now emits it as
        // `dimensions.task.goal_absent_text`, and the goal line renders that
        // field or nothing — so there is no Swift fallback to allow.
        "No goal was recorded for this task.",
        // NOT banned yet, deliberately: `NEXT_STEP_ABSENT` ("No next step
        // recorded."). Python owns the words, but no payload field carries
        // them, so `NextStepRow.absence` is still the only source and banning
        // it would leave the app with nothing to print. Ban it in the same
        // change that makes the reducer emit the SS4 next-step absence.
    ]

    /// Vocabulary whose display label is character-identical to the payload key
    /// it labels, so no scanner can tell a rendered copy from a key literal.
    /// Listed (rather than silently dropped) so the exemption is auditable.
    static let keyShapedExemptions: Set<String> = ["unchecked"]

    // MARK: - allowlists

    /// Files that BUILD a payload rather than render one: snapshot harnesses
    /// and fixtures stand in for the daemon so the app can be rendered offline.
    static let payloadFixtureSuffixes = [
        "SnapshotHarness.swift", "SnapshotFixtures.swift", "ReviewHarness.swift",
    ]

    /// `(file, literal, why it must stay)`. Anything not on this list, and not
    /// in a key position, is a violation.
    static let allowedSites: [(file: String, literal: String, reason: String)] = [
        ("V1Model.swift", "no usage recorded",
         "PayloadAbsence: the named absence when an older payload omits the cost state"),
        ("V1Model.swift", "unpriced",
         "PayloadAbsence: the named absence when an older payload omits the cost state"),
        ("V1Model.swift", "not gradeable",
         "PayloadAbsence: the coverage tile's absent face when the payload omits it"),
        ("V1Model.swift", "cost basis not reported",
         "PayloadAbsence: a cost figure whose basis the payload did not carry"),
        ("Theme.swift", "externally verified",
         "EvidenceTierStyle.forGrade: last-resort tier label, pinned to TIER_LABELS by the Python parity suite"),
        ("Theme.swift", "independently checked",
         "EvidenceTierStyle.forGrade: last-resort tier label, pinned to TIER_LABELS by the Python parity suite"),
        ("Theme.swift", "self-checked",
         "EvidenceTierStyle.forGrade: last-resort tier label, pinned to TIER_LABELS by the Python parity suite"),
        ("Theme.swift", "not graded",
         "EvidenceTierStyle.forGrade: last-resort label for the ungraded tier, pinned to EVIDENCE_GRADE_LABELS by the Python parity suite"),
        ("WorkPane.swift", "Verified",
         "WorkGroup tab title; the payload's decision legend label wins when it is present"),
        ("WorkPane.swift", "Reported",
         "WorkGroup tab title; the payload's decision legend label wins when it is present"),
        ("WorkPane.swift", "In progress",
         "WorkGroup tab title; the payload's decision legend label wins when it is present"),
        ("WorkPane.swift", "No work recorded",
         "WorkGroup tab title; the payload's decision legend label wins when it is present"),
        ("RecordingHealth.swift", "In progress",
         "recorder SETUP progress, not a Task decision — a different subject entirely"),
        ("NativeSetupFlow.swift", "In progress",
         "setup-step progress, not a Task decision — a different subject entirely"),
    ]

    // MARK: - the scan

    private var sourcesDirectory: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()  // agentacctTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // apps/agentacct
            .appendingPathComponent("Sources/agentacct", isDirectory: true)
    }

    func testNoHardCodedDisplayVocabularyInSources() throws {
        let files = try FileManager.default.contentsOfDirectory(
            at: sourcesDirectory, includingPropertiesForKeys: nil
        ).filter { $0.pathExtension == "swift" }
        XCTAssertFalse(files.isEmpty, "no Swift sources found at \(sourcesDirectory.path)")

        var violations: [String] = []
        for file in files.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
            let name = file.lastPathComponent
            if Self.payloadFixtureSuffixes.contains(where: { name.hasSuffix($0) }) { continue }
            let source = try String(contentsOf: file, encoding: .utf8)
            for finding in Self.violations(in: source, file: name) {
                violations.append("\(name):\(finding.line): \"\(finding.phrase)\" in \"\(finding.literal)\"")
            }
        }

        XCTAssertTrue(
            violations.isEmpty,
            """
            Display vocabulary hard-coded in Swift. These words are decided in \
            src/agentacct/display_vocabulary.py and ride the payload — render the \
            payload field instead, or (for a field an older daemon may omit) add a \
            neutral PayloadAbsence fallback that NAMES the absence:
            \(violations.joined(separator: "\n"))
            """
        )
    }

    /// The allowlist must stay honest: an entry that no longer matches anything
    /// is dead weight that quietly widens the lint.
    func testEveryAllowedSiteStillExists() throws {
        var unused: [String] = []
        for site in Self.allowedSites {
            let url = sourcesDirectory.appendingPathComponent(site.file)
            let source = (try? String(contentsOf: url, encoding: .utf8)) ?? ""
            let stripped = Self.stripComments(source)
            if !stripped.contains("\"\(site.literal)\"")
                && !stripped.contains("\(site.literal) ·")
                && !stripped.contains("· \(site.literal)") {
                unused.append("\(site.file): \"\(site.literal)\" — \(site.reason)")
            }
        }
        XCTAssertTrue(
            unused.isEmpty,
            "Stale vocabulary-lint allowlist entries (delete them):\n" + unused.joined(separator: "\n")
        )
    }

    /// Exemptions and allowlists may never overlap the other's job: a
    /// key-shaped exemption must not also sit in the banned list.
    func testExemptionsAndBannedListDoNotOverlap() {
        XCTAssertTrue(Self.bannedVocabulary.isDisjoint(with: Self.keyShapedExemptions))
        for site in Self.allowedSites {
            XCTAssertTrue(
                Self.bannedVocabulary.contains(site.literal),
                "allowlisted site \(site.file):\"\(site.literal)\" is not banned vocabulary — delete it"
            )
            XCTAssertFalse(site.reason.isEmpty, "every allowlist entry needs a reason")
        }
    }

    // MARK: - the scanner's own contract

    /// The scanner must catch a reintroduced copy in each shape it can take,
    /// and must stay silent on comments, key positions and payload rendering.
    func testScannerCatchesReintroducedCopiesAndIgnoresKeysAndComments() {
        let bad = """
        Text(receipt.decisionLabel ?? "Handed off")
        let reason = "Check could not run"
        let line = "\\(cost) · pricing estimate"
        Text("Not yet proven: \\(gap)")
        func tier() -> String { "self-checked" }
        """
        let found = Self.violations(in: bad, file: "Sample.swift").map(\.phrase)
        XCTAssertEqual(
            Set(found),
            ["Handed off", "Check could not run", "pricing estimate", "Not yet proven", "self-checked"],
            "scanner missed a reintroduced copy: \(found)"
        )

        let good = """
        // A comment may say Handed off and pricing estimate freely.
        /* So may a block comment: Not yet proven. */
        if state == "unpriced" { return display }
        switch key {
        case "no usage recorded", "unpriced": return .absent
        }
        Text(PayloadAbsence.text(payload.decisionLabel) ?? PayloadAbsence.coverage)
        Text(payload.verdict.headline)
        """
        XCTAssertEqual(Self.violations(in: good, file: "Sample.swift").map(\.phrase), [])
    }

    /// A phrase that merely occurs inside ordinary prose is not a copy: the
    /// lint fires on the label itself, never on a sentence that uses the words.
    func testProseUsingVocabularyWordsIsNotAViolation() {
        let prose = """
        Text("Show only failed checks that still need attention")
        Text("Review the reported coverage separately.")
        Text("No usage recorded this week")
        """
        XCTAssertEqual(Self.violations(in: prose, file: "Sample.swift").map(\.phrase), [])
    }

    func testAllowlistSuppressesOnlyItsOwnFileAndLiteral() {
        let source = "let fallback = \"self-checked\"\n"
        XCTAssertEqual(Self.violations(in: source, file: "Theme.swift").count, 0)
        XCTAssertEqual(Self.violations(in: source, file: "WorkPane.swift").count, 1)
    }

    // MARK: - implementation

    struct Finding {
        let line: Int
        let phrase: String
        let literal: String
    }

    /// Literals are split on the chip separator ` · ` and on newlines, and each
    /// component must EXACTLY equal a banned phrase. Exact matching is what
    /// keeps "Show only failed checks…" (prose) apart from "Failed check" (a
    /// copied label), and it makes the report actionable.
    static func violations(in source: String, file: String) -> [Finding] {
        let allowed = Set(allowedSites.filter { $0.file == file }.map(\.literal))
        let stripped = stripComments(source)
        let characters = Array(stripped)
        var findings: [Finding] = []
        var index = 0
        var line = 1
        while index < characters.count {
            let character = characters[index]
            if character == "\n" { line += 1; index += 1; continue }
            guard character == "\"" else { index += 1; continue }
            // Read one literal.
            var body = ""
            var cursor = index + 1
            var closed = false
            while cursor < characters.count {
                let next = characters[cursor]
                if next == "\\" && cursor + 1 < characters.count {
                    body.append("\\"); body.append(characters[cursor + 1]); cursor += 2; continue
                }
                if next == "\"" { closed = true; break }
                if next == "\n" { break }
                body.append(next); cursor += 1
            }
            guard closed else { index += 1; continue }
            let literalLine = line
            let startedAt = index
            index = cursor + 1

            if isKeyPosition(characters, before: startedAt) { continue }
            for phrase in phrases(in: body) where bannedVocabulary.contains(phrase) && !allowed.contains(phrase) {
                findings.append(Finding(line: literalLine, phrase: phrase, literal: body))
                break
            }
        }
        return findings
    }

    /// The chip components of a literal, with `\(…)` interpolations replaced by
    /// a placeholder so an interpolated value never joins two components.
    private static func phrases(in body: String) -> [String] {
        var text = ""
        var characters = Array(body)
        var index = 0
        while index < characters.count {
            if characters[index] == "\\" && index + 1 < characters.count && characters[index + 1] == "(" {
                var depth = 0
                while index < characters.count {
                    if characters[index] == "(" { depth += 1 }
                    if characters[index] == ")" { depth -= 1; if depth == 0 { index += 1; break } }
                    index += 1
                }
                text.append("\u{0}")
                continue
            }
            if characters[index] == "\\" && index + 1 < characters.count {
                text.append(characters[index + 1] == "n" ? "\n" : characters[index + 1])
                index += 2
                continue
            }
            text.append(characters[index])
            index += 1
        }
        characters = []
        // The separators the vocabulary itself composes with: the chip dot, the
        // verdict dash, and the `<label>: <text>` gap form.
        var split = text
        for separator in [" · ", " — ", ": "] {
            split = split.replacingOccurrences(of: separator, with: "\n")
        }
        return split
            .split(separator: "\n", omittingEmptySubsequences: false)
            .map { $0.trimmingCharacters(in: CharacterSet(charactersIn: " .:")) }
    }

    /// True when the literal starting at `start` sits in a payload-KEY position:
    /// the right-hand side of `==` / `!=`, or a `case` label (including the
    /// second and later entries of a `case "a", "b":` list).
    private static func isKeyPosition(_ characters: [Character], before start: Int) -> Bool {
        var index = start - 1
        var lineStart = 0
        var scan = start - 1
        while scan >= 0 {
            if characters[scan] == "\n" { lineStart = scan + 1; break }
            scan -= 1
        }
        // Walk back over whitespace, and over any `"…",` entries already in a
        // case list, collecting the operator or keyword that introduces it.
        var sawComma = false
        while index >= lineStart {
            let character = characters[index]
            if character == " " || character == "\t" { index -= 1; continue }
            if character == "," { sawComma = true; index -= 1; continue }
            if character == "\"" && sawComma {
                // Skip the preceding literal and keep walking back.
                index -= 1
                while index >= lineStart && !(characters[index] == "\"" && (index == lineStart || characters[index - 1] != "\\")) {
                    index -= 1
                }
                index -= 1
                continue
            }
            break
        }
        guard index >= lineStart else { return false }
        let head = String(characters[lineStart...index]).trimmingCharacters(in: .whitespaces)
        if head.hasSuffix("==") || head.hasSuffix("!=") { return true }
        if head == "case" || head.hasSuffix(" case") || head.hasSuffix("\ncase") { return true }
        return false
    }

    /// Removes `//` line comments and `/* */` block comments (outside string
    /// literals) while preserving newlines, so reported lines stay accurate.
    static func stripComments(_ source: String) -> String {
        var output = ""
        let characters = Array(source)
        var index = 0
        var inString = false
        var inLine = false
        var blockDepth = 0
        while index < characters.count {
            let character = characters[index]
            if inLine {
                output.append(character == "\n" ? "\n" : " ")
                if character == "\n" { inLine = false }
                index += 1
                continue
            }
            if blockDepth > 0 {
                if character == "*" && index + 1 < characters.count && characters[index + 1] == "/" {
                    blockDepth -= 1; output.append("  "); index += 2; continue
                }
                if character == "/" && index + 1 < characters.count && characters[index + 1] == "*" {
                    blockDepth += 1; output.append("  "); index += 2; continue
                }
                output.append(character == "\n" ? "\n" : " ")
                index += 1
                continue
            }
            if inString {
                if character == "\\" && index + 1 < characters.count {
                    output.append(character); output.append(characters[index + 1]); index += 2; continue
                }
                if character == "\"" { inString = false }
                output.append(character); index += 1; continue
            }
            if character == "/" && index + 1 < characters.count && characters[index + 1] == "/" {
                inLine = true; output.append("  "); index += 2; continue
            }
            if character == "/" && index + 1 < characters.count && characters[index + 1] == "*" {
                blockDepth = 1; output.append("  "); index += 2; continue
            }
            if character == "\"" { inString = true }
            output.append(character)
            index += 1
        }
        return output
    }
}
