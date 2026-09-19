import AppKit
import SwiftUI

/// Focused component galleries: every variant of a shared badge or chip on
/// one canvas, so a colour or label change is reviewed once for the whole
/// vocabulary rather than wherever a fixture happens to use it, plus shared
/// card layouts at a data mix no page fixture reaches.
enum WorkComponentSnapshotKind: String, CaseIterable {
    case decisionBadges = "decision-badges"
    case provenanceChips = "provenance-chips"
    case outcomeCards = "outcome-cards"
}

struct WorkComponentSnapshotConfiguration {
    let kind: WorkComponentSnapshotKind
    let width: CGFloat
    let height: CGFloat
    let colorScheme: ColorScheme

    var filename: String {
        let appearance = colorScheme == .dark ? "dark" : "light"
        return "work-components-\(kind.rawValue)-\(appearance).png"
    }

    static let reviewConfigurations: [Self] = [
        (WorkComponentSnapshotKind.decisionBadges, CGFloat(760), CGFloat(760)),
        (.provenanceChips, 760, 420),
        (.outcomeCards, 960, 480),
    ].flatMap { kind, width, height in
        [
            Self(kind: kind, width: width, height: height, colorScheme: .light),
            Self(kind: kind, width: width, height: height, colorScheme: .dark),
        ]
    }
}

enum WorkComponentSnapshotRenderer {
    @MainActor
    static func render(
        outputDirectory: URL,
        configurations: [WorkComponentSnapshotConfiguration] = WorkComponentSnapshotConfiguration.reviewConfigurations
    ) throws -> [URL] {
        try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)

        SnapshotMode.enabled = true
        SnapshotMode.setFixtureDate(Date(timeIntervalSince1970: 1_787_590_000))
        defer {
            SnapshotMode.enabled = false
            SnapshotMode.setFixtureDate(nil)
            SnapshotScheme.override = nil
        }

        return try configurations.map { configuration in
            SnapshotScheme.override = configuration.colorScheme
            let unconstrainedScene = WorkComponentSnapshotScene(kind: configuration.kind)
                .frame(width: configuration.width, alignment: .topLeading)
                .fixedSize(horizontal: false, vertical: true)
                .modifier(WorkComponentSnapshotEnvironment(configuration: configuration))
            let requiredSize = try SnapshotImageWriter.renderedSize(
                unconstrainedScene,
                proposedSize: ProposedViewSize(width: configuration.width, height: nil)
            )
            guard requiredSize.height <= configuration.height else {
                throw SnapshotError.snapshotContentExceedsCanvas(
                    filename: configuration.filename,
                    requiredHeight: Int(ceil(requiredSize.height)),
                    availableHeight: Int(configuration.height)
                )
            }
            let scene = WorkComponentSnapshotScene(kind: configuration.kind)
                .frame(width: configuration.width, height: configuration.height, alignment: .topLeading)
                .clipped()
                .modifier(WorkComponentSnapshotEnvironment(configuration: configuration))
            let outputURL = outputDirectory.appendingPathComponent(configuration.filename)
            try SnapshotImageWriter.render(
                scene,
                to: outputURL,
                size: CGSize(width: configuration.width, height: configuration.height)
            )
            return outputURL
        }
    }
}

private struct WorkComponentSnapshotEnvironment: ViewModifier {
    let configuration: WorkComponentSnapshotConfiguration

    func body(content: Content) -> some View {
        content
            .background(Theme.canvas)
            .environment(\.colorScheme, configuration.colorScheme)
            .environment(\.locale, Locale(identifier: "en_US_POSIX"))
            .environment(\.displayScale, 2)
            .environment(\.layoutDirection, .leftToRight)
            .environment(\.dynamicTypeSize, .medium)
            .environment(\.controlSize, .regular)
            .environment(\.legibilityWeight, nil)
            .environment(\.appearsActive, true)
            .transaction { $0.disablesAnimations = true }
    }
}

struct WorkComponentSnapshotScene: View {
    let kind: WorkComponentSnapshotKind

    var body: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            switch kind {
            case .decisionBadges:
                decisionBadges
            case .provenanceChips:
                provenanceChips
            case .outcomeCards:
                outcomeCards
            }
        }
        .padding(Space.l)
    }

    /// The record's Steps and Checks cards at a reported live mix, side by
    /// side and in the narrow stacked fallback. Only Checks carries the
    /// attention note, so side by side both borders must still share one top
    /// and one bottom edge.
    private var outcomeCards: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            SectionCaption(text: "Outcome cards")
            RecordOutcomeBars(steps: Self.outcomeSteps)
            SectionCaption(text: "Outcome cards, stacked")
                .padding(.top, Space.m)
            RecordOutcomeBars(steps: Self.outcomeSteps)
                .frame(width: 360, alignment: .leading)
        }
    }

    /// 117 steps (44 self-checked, 60 claimed, 7 without evidence, 6 blocked)
    /// carrying 74 agent-reported passes and 6 failures.
    private static let outcomeSteps: [V1Step] = {
        let mix: [(grade: String?, status: String, count: Int)] = [
            ("self_checked", "completed", 44),
            ("claimed", "completed", 60),
            (nil, "completed", 7),
            ("self_checked", "blocked", 6),
        ]
        var steps: [V1Step] = []
        var checkIndex = 0
        func check(_ result: String) -> V1Check {
            defer { checkIndex += 1 }
            return V1Check(
                eventId: "outcome-check-\(checkIndex)",
                createdAt: nil,
                evidenceType: "test",
                result: result,
                summary: nil,
                exitCode: result == "passed" ? 0 : 1,
                sourceType: "mcp_agent_reported",
                checkIdentity: "outcome-check-\(checkIndex)",
                supersessionState: nil,
                supersededByEventId: nil,
                resolutionScope: nil,
                resolutionSummary: nil,
                resolvesBlockedEventId: nil,
                files: nil,
                artifactRef: nil,
                artifactPath: nil,
                artifactUrl: nil,
                commandRedacted: nil,
                artifactPathRedacted: nil,
                artifactUrlRedacted: nil
            )
        }
        for (grade, status, count) in mix {
            for _ in 0..<count {
                let index = steps.count
                let checks: [V1Check]
                switch (grade, status) {
                case ("self_checked", "completed"): checks = (0..<(index < 30 ? 2 : 1)).map { _ in check("passed") }
                case (_, "blocked"): checks = [check("failed")]
                default: checks = []
                }
                steps.append(V1Step(
                    workId: "outcome-step-\(index)",
                    sectionId: nil,
                    title: nil,
                    latestStatus: status,
                    kind: nil,
                    phase: nil,
                    startedAt: nil,
                    updatedAt: nil,
                    summary: nil,
                    files: nil,
                    blocker: nil,
                    nextStep: nil,
                    usage: nil,
                    joinConfidence: nil,
                    evidenceStatus: nil,
                    evidenceGrade: grade,
                    evidenceGradeReason: nil,
                    models: nil,
                    checks: checks
                ))
            }
        }
        return steps
    }()

    /// Every provenance token the daemon emits, as the chip the reader sees
    /// beside the raw token, plus a client name that must pass through.
    private var provenanceChips: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            SectionCaption(text: "Provenance chips")
            ForEach(["client_log", "mcp", "hook", "transcript_scan", "ci", "external", "provider", "none", "claude-code"], id: \.self) { token in
                HStack(alignment: .center, spacing: Space.l) {
                    ProvenanceChip(text: token)
                        .frame(width: 170, alignment: .leading)
                    Text(token)
                        .workFont(.dataSmall)
                        .foregroundStyle(Theme.muted)
                }
            }
        }
    }

    /// Every decision key the legend defines, as the page badge and the row
    /// badge, beside its definition. Claimed, live, inferred, danger and
    /// verified families must each read as their own colour.
    private var decisionBadges: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            SectionCaption(text: "Decision badges")
            ForEach(DecisionLegend.entries) { entry in
                HStack(alignment: .center, spacing: Space.l) {
                    DecisionBadge(key: entry.key, label: entry.label)
                        .frame(width: 150, alignment: .leading)
                    DecisionBadge(key: entry.key, label: entry.label, compact: true)
                        .frame(width: 130, alignment: .leading)
                    Text(entry.definition)
                        .workFont(.caption)
                        .foregroundStyle(Theme.muted)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
            }
        }
    }
}
