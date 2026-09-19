import AppKit
import SwiftUI

/// Focused component galleries: every variant of a shared badge or chip on
/// one canvas, so a colour or label change is reviewed once for the whole
/// vocabulary rather than wherever a fixture happens to use it.
enum WorkComponentSnapshotKind: String, CaseIterable {
    case decisionBadges = "decision-badges"
    case provenanceChips = "provenance-chips"
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
            }
        }
        .padding(Space.l)
    }

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
