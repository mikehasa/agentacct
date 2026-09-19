import AppKit
import SwiftUI

/// Deterministic Diagnostics (sources) review renders. The healthy lane pins
/// a fully reporting source ledger; the degraded lane pins six degraded
/// sources sharing one store-wide reconciliation fault, which must read as one
/// issue naming its sources, never one copy per source.
struct SourcesSnapshotConfiguration {
    enum HealthLane: String {
        case healthySources = "healthy-reference"
        case degraded = "degraded-reference"
    }

    let lane: HealthLane
    let width: CGFloat
    let height: CGFloat
    let colorScheme: ColorScheme

    var filename: String {
        let appearance = colorScheme == .dark ? "dark" : "light"
        return "sources-\(lane.rawValue)-\(appearance).png"
    }

    static let reviewConfigurations: [Self] = [
        (HealthLane.healthySources, CGFloat(1120), CGFloat(900)),
        (.degraded, 1120, 1300),
    ].flatMap { lane, width, height in
        [
            Self(lane: lane, width: width, height: height, colorScheme: .light),
            Self(lane: lane, width: width, height: height, colorScheme: .dark),
        ]
    }
}

enum SourcesSnapshotRenderer {
    private static let snapshotLocale = Locale(identifier: "en_US_POSIX")
    private static let snapshotTimeZone = TimeZone(secondsFromGMT: 0)!

    private static var snapshotCalendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.locale = snapshotLocale
        calendar.timeZone = snapshotTimeZone
        return calendar
    }

    @MainActor
    static func render(
        fixture: DashboardSnapshotFixture,
        outputDirectory: URL,
        configurations: [SourcesSnapshotConfiguration] = SourcesSnapshotConfiguration.reviewConfigurations
    ) throws -> [URL] {
        guard let generatedAt = fixture.glance.generatedAt else {
            throw SnapshotError.missingFixtureDate
        }
        try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)

        SnapshotMode.enabled = true
        SnapshotMode.boundsScrollContentToViewport = true
        SnapshotMode.setFixtureDate(Date(timeIntervalSince1970: generatedAt))
        defer {
            SnapshotMode.enabled = false
            SnapshotMode.boundsScrollContentToViewport = false
            SnapshotMode.setFixtureDate(nil)
            SnapshotScheme.override = nil
        }

        return try configurations.map { configuration in
            SnapshotScheme.override = configuration.colorScheme
            let ingestion: V1IngestionSnapshot
            switch configuration.lane {
            case .healthySources:
                guard let lane = fixture.ingestionHealthySources else {
                    throw SnapshotError.missingSourcesFixture(lane: "ingestion_healthy_sources")
                }
                ingestion = lane.ingestion
            case .degraded:
                guard let lane = fixture.ingestionDegraded else {
                    throw SnapshotError.missingSourcesFixture(lane: "ingestion_degraded")
                }
                ingestion = lane.ingestion
            }
            let glance = GlanceState(preloaded: fixture.glanceSnapshot)
            let dashboard = DashboardStore(preloaded: fixture, ingestionOverride: ingestion)
            let selection = AppSelection()
            selection.pane = .sources
            let view = MainWindow(canSetUpOverride: true)
                .environment(glance)
                .environment(dashboard)
                .environment(selection)
                .frame(width: configuration.width, height: configuration.height, alignment: .top)
                .clipped()
                .environment(\.colorScheme, configuration.colorScheme)
                .environment(\.locale, snapshotLocale)
                .environment(\.calendar, snapshotCalendar)
                .environment(\.timeZone, snapshotTimeZone)
                .environment(\.displayScale, 2)
                .environment(\.layoutDirection, .leftToRight)
                .environment(\.dynamicTypeSize, .medium)
                .environment(\.controlSize, .regular)
                .environment(\.legibilityWeight, nil)
                .environment(\.appearsActive, true)
                .transaction { $0.disablesAnimations = true }
            let outputURL = outputDirectory.appendingPathComponent(configuration.filename)
            try SnapshotImageWriter.render(
                view,
                to: outputURL,
                size: CGSize(width: configuration.width, height: configuration.height)
            )
            return outputURL
        }
    }
}
