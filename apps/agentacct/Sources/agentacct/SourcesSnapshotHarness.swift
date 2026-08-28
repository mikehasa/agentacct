import Foundation
import SwiftUI

struct SourcesSnapshotConfiguration {
    let viewport: String
    let width: CGFloat
    let height: CGFloat
    let colorScheme: ColorScheme

    var filename: String {
        let appearance = colorScheme == .dark ? "dark" : "light"
        return "sources-retained-error-\(viewport)-\(appearance).png"
    }

    static let reviewConfigurations: [Self] = [
        Self(viewport: "minimum", width: 960, height: 560, colorScheme: .light),
        Self(viewport: "minimum", width: 960, height: 560, colorScheme: .dark),
        Self(viewport: "reference", width: 1120, height: 760, colorScheme: .light),
        Self(viewport: "reference", width: 1120, height: 760, colorScheme: .dark),
    ]
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
        try FileManager.default.createDirectory(
            at: outputDirectory,
            withIntermediateDirectories: true
        )

        let retainedSnapshot = try JSONDecoder().decode(
            V1IngestionSnapshot.self,
            from: Data(Self.retainedSnapshotJSON.utf8)
        )

        SnapshotMode.enabled = true
        SnapshotMode.boundsScrollContentToViewport = true
        SnapshotMode.setFixtureDate(Date(timeIntervalSince1970: generatedAt))
        SnapshotMode.setFixtureStorePath("~/.local/state/agentacct/state")
        defer {
            SnapshotMode.enabled = false
            SnapshotMode.boundsScrollContentToViewport = false
            SnapshotMode.setFixtureDate(nil)
            SnapshotMode.setFixtureStorePath(nil)
            SnapshotScheme.override = nil
        }

        let glance = GlanceState(preloaded: fixture.glanceSnapshot)
        let dashboard = DashboardStore(
            preloaded: fixture,
            ingestion: retainedSnapshot,
            ingestionError: "source health fetch failed: the local daemon stopped responding"
        )
        let selection = AppSelection()
        selection.pane = .sources

        return try configurations.map { configuration in
            SnapshotScheme.override = configuration.colorScheme
            let view = MainWindow(canSetUpOverride: true)
                .environmentObject(glance)
                .environmentObject(dashboard)
                .environmentObject(selection)
                .frame(
                    width: configuration.width,
                    height: configuration.height,
                    alignment: .top
                )
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
                .transaction { transaction in
                    transaction.disablesAnimations = true
                }
            let outputURL = outputDirectory.appendingPathComponent(configuration.filename)
            try SnapshotImageWriter.render(
                view,
                to: outputURL,
                size: CGSize(width: configuration.width, height: configuration.height)
            )
            return outputURL
        }
    }

    private static let retainedSnapshotJSON = #"""
    {
        "state": "healthy",
        "last_success_at": 1787589940,
        "sources": [
            {
                "source": "claude-code",
                "state": "healthy",
                "scope": "watched",
                "last_success_at": 1787589940,
                "discovered": 18,
                "parsed": 18,
                "skipped": 0,
                "error_count": 0
            }
        ],
        "watcher": {
            "state": "running",
            "interval_seconds": 15,
            "heartbeat_at": 1787589995
        },
        "issues": []
    }
    """#
}
