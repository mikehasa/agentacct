import Foundation

// The PURE half of `Scripts/a11y-smoke.sh`: it never touches the GUI, the
// accessibility APIs or the keyboard. It takes a recorded run — one dumped
// accessibility tree and one focused element per navigation step — and decides
// whether the app is keyboard- and VoiceOver-reachable.
//
// Keeping the judging here (rather than inside the driver) means the rules are
// unit-tested in the normal Swift suite, on fixtures, with no running app:
// see `AccessibilitySmokeJudgeTests`. The driver in `Scripts/a11y-smoke-driver.swift`
// compiles THIS FILE alongside itself, so the tool and the tests can never
// judge a run differently.

/// One node of a dumped accessibility tree. Every field is optional because a
/// real AX element may answer `nil` to any attribute; the point of the smoke
/// test is to notice exactly that.
struct AccessibilityNode: Codable, Equatable {
    var role: String?
    var subrole: String?
    /// `AXIdentifier` — the stable handle the app sets with
    /// `.accessibilityIdentifier(...)`.
    var identifier: String?
    /// `AXTitle`.
    var title: String?
    /// `AXDescription` — what `.accessibilityLabel(...)` produces.
    var label: String?
    /// `AXValue`, stringified.
    var value: String?
    var help: String?
    var actions: [String]?
    var children: [AccessibilityNode]?

    /// Every node of this subtree, self first.
    var flattened: [AccessibilityNode] {
        [self] + (children ?? []).flatMap(\.flattened)
    }

    /// The names a screen reader could speak for this node.
    var spokenText: [String] {
        [title, label, value, help]
            .compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    /// True when nothing names this element — the failure a keyboard user hits
    /// as "focused: (blank)".
    var isUnnamed: Bool {
        let named = [title, label].compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }
        return named.allSatisfy(\.isEmpty)
    }

    /// All text in this subtree, joined — what the record's checks are read from.
    var subtreeText: String {
        flattened.flatMap(\.spokenText).joined(separator: "\n")
    }
}

/// One navigation step the driver performed, with what it observed afterwards.
struct AccessibilityStep: Codable, Equatable {
    /// `dashboard` / `work` / `record` / `back` — see `AccessibilitySmokeJudge.requiredSteps`.
    var name: String
    /// The whole window tree after this step (nil when the driver could not read it).
    var tree: AccessibilityNode?
    /// The focused element after this step (nil when nothing was focused).
    var focus: AccessibilityNode?
    /// How the driver got here, for the report (`Tab ×3`, `Return`, …).
    var input: String?
}

struct AccessibilitySmokeRun: Codable, Equatable {
    var app: String?
    var pid: Int?
    var steps: [AccessibilityStep]
}

/// One reason the run failed, with a stable code so a report can be diffed.
struct AccessibilitySmokeFailure: Equatable {
    let code: String
    let message: String

    var line: String { "\(code): \(message)" }
}

/// The rules. Every check is a statement about what a keyboard-only or
/// VoiceOver user can reach; none of them look at pixels.
enum AccessibilitySmokeJudge {

    /// The navigation the driver must have performed. A run that skipped a step
    /// is a failed run, not a passing one — silence is never success here.
    static let requiredSteps = ["dashboard", "work", "record", "back"]

    /// Identifier prefixes the Work surface puts on each selectable receipt row
    /// (`WorkPane`: `work.table.task.<id>` in the table, `work.master.task.<id>`
    /// in the master list).
    static let rowIdentifierPrefixes = ["work.table.task.", "work.master.task."]

    /// The four facts a reviewer opens a record FOR. Each is anchored on
    /// something the app already publishes, so the check cannot drift into
    /// asserting display wording (that is the parity suite's job):
    /// the verdict's AX label, the two record-summary tiles, and the next-step row.
    struct RecordFact {
        let code: String
        let description: String
        /// An `AXIdentifier` that must exist in the record subtree, and carry text.
        let identifier: String?
        /// A prefix one of the subtree's spoken strings must start with.
        let spokenPrefix: String?
    }

    static let recordFacts: [RecordFact] = [
        .init(code: "record-verdict", description: "the verdict headline",
              identifier: nil, spokenPrefix: "Verdict:"),
        .init(code: "record-coverage", description: "the coverage tile",
              identifier: "receipt.summary.coverage", spokenPrefix: nil),
        .init(code: "record-checks", description: "the checks tile",
              identifier: "receipt.summary.checks", spokenPrefix: nil),
        .init(code: "record-next-step", description: "the recorded next step",
              identifier: "next-step", spokenPrefix: nil),
    ]

    static func isRow(_ node: AccessibilityNode) -> Bool {
        guard let identifier = node.identifier else { return false }
        return rowIdentifierPrefixes.contains { identifier.hasPrefix($0) }
    }

    static func judge(_ run: AccessibilitySmokeRun) -> [AccessibilitySmokeFailure] {
        var failures: [AccessibilitySmokeFailure] = []
        let byName = Dictionary(run.steps.map { ($0.name, $0) }, uniquingKeysWith: { first, _ in first })

        for name in requiredSteps where byName[name] == nil {
            failures.append(.init(code: "step-missing",
                                  message: "the run never reached the \(name) step"))
        }

        // --- the Work surface's rows must be real, pressable rows ------------
        let workTrees = run.steps.filter { $0.name == "work" }.compactMap(\.tree)
        let rows = workTrees.flatMap(\.flattened).filter(isRow)
        if byName["work"] != nil && rows.isEmpty {
            failures.append(.init(
                code: "work-rows-absent",
                message: "no work row (\(rowIdentifierPrefixes.joined(separator: " / "))) was found in the "
                    + "Work tree — an empty tree cannot vouch for the rows"))
        }
        for row in rows {
            let identifier = row.identifier ?? "(no identifier)"
            let role = row.role ?? "(no role)"
            if role == "AXUnknown" || role == "(no role)" {
                failures.append(.init(
                    code: "work-row-role",
                    message: "row \(identifier) reports role \(role) — a selectable receipt row must expose a "
                        + "real role, not an unnamed group"))
            }
            if !(row.actions ?? []).contains("AXPress") {
                failures.append(.init(
                    code: "work-row-press",
                    message: "row \(identifier) exposes no AXPress action, so it cannot be opened without a mouse"))
            }
            if row.isUnnamed {
                failures.append(.init(
                    code: "work-row-unnamed",
                    message: "row \(identifier) has neither AXTitle nor AXDescription"))
            }
        }

        // --- focus must actually land on a row -------------------------------
        let focusedRows = run.steps.compactMap(\.focus).filter(isRow)
        if focusedRows.isEmpty {
            failures.append(.init(
                code: "focus-never-on-a-row",
                message: "Tab focus never landed on a work row; the table is not keyboard-reachable"))
        }

        // --- every focused element must be speakable -------------------------
        for step in run.steps {
            guard let focus = step.focus else {
                failures.append(.init(
                    code: "focus-absent",
                    message: "nothing was focused after the \(step.name) step"
                        + (step.input.map { " (\($0))" } ?? "")))
                continue
            }
            if focus.isUnnamed {
                let identifier = focus.identifier ?? focus.role ?? "(unidentified element)"
                failures.append(.init(
                    code: "focus-unnamed",
                    message: "the element focused after the \(step.name) step (\(identifier)) has no "
                        + "AXDescription and no AXTitle"))
            }
        }

        // --- the opened record must speak the facts it exists for ------------
        if let record = byName["record"] {
            guard let tree = record.tree else {
                failures.append(.init(code: "record-tree-absent",
                                      message: "no accessibility tree was captured for the opened record"))
                return failures.sorted { $0.line < $1.line }
            }
            let nodes = tree.flattened
            let spoken = nodes.flatMap(\.spokenText)
            for fact in recordFacts {
                if let identifier = fact.identifier {
                    let match = nodes.first { $0.identifier == identifier }
                    guard let match, !match.subtreeText.isEmpty else {
                        failures.append(.init(
                            code: fact.code,
                            message: "\(fact.description) (\(identifier)) is missing from the record's "
                                + "accessibility text"))
                        continue
                    }
                }
                if let prefix = fact.spokenPrefix {
                    let match = spoken.first { $0.hasPrefix(prefix) }
                    guard let match, match.count > prefix.count else {
                        failures.append(.init(
                            code: fact.code,
                            message: "\(fact.description) (an AX label starting \"\(prefix)\") is missing from "
                                + "the record's accessibility text"))
                        continue
                    }
                }
            }
        }

        return failures.sorted { $0.line < $1.line }
    }

    /// The human report the script prints. Exit code is 0 only on an empty
    /// failure list AND a run that actually navigated.
    static func report(_ run: AccessibilitySmokeRun) -> (text: String, exitCode: Int32) {
        let failures = judge(run)
        var lines = ["agentacct accessibility smoke — \(run.steps.count) step(s) recorded"]
        for step in run.steps {
            let focus = step.focus.map { node -> String in
                let name = node.spokenText.first ?? "(unnamed)"
                return "\(node.identifier ?? node.role ?? "?") — \(name)"
            } ?? "(nothing focused)"
            lines.append("  \(step.name.padding(toLength: 10, withPad: " ", startingAt: 0))"
                + "\(step.input ?? "")  focus: \(focus)")
        }
        if failures.isEmpty {
            lines.append("PASS — rows are pressable, focus reaches them, and the record speaks its facts.")
            return (lines.joined(separator: "\n"), 0)
        }
        lines.append("FAIL — \(failures.count) accessibility problem(s):")
        lines.append(contentsOf: failures.map { "  \($0.line)" })
        return (lines.joined(separator: "\n"), 1)
    }

    /// Decode a dump written by the driver.
    static func run(fromJSON data: Data) throws -> AccessibilitySmokeRun {
        try JSONDecoder().decode(AccessibilitySmokeRun.self, from: data)
    }
}
