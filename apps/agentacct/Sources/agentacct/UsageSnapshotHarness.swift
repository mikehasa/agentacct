import AppKit
import SwiftUI

struct UsageSnapshotConfiguration {
    enum CapacityState {
        case connected
        case disconnected
    }

    enum RecordedUsageState {
        case sevenDays
        case ninetyDays

        func storeState(for fixture: DashboardSnapshotFixture) -> SnapshotUsageStoreState {
            switch self {
            case .sevenDays:
                return SnapshotUsageStoreState(days: 7, summary: fixture.usage)
            case .ninetyDays:
                return SnapshotUsageStoreState(days: 90, summary: fixture.usage90Days)
            }
        }
    }

    let viewport: String
    let width: CGFloat
    let height: CGFloat
    let colorScheme: ColorScheme
    let capacityState: CapacityState
    let recordedUsageState: RecordedUsageState

    var filename: String {
        let appearance = colorScheme == .dark ? "dark" : "light"
        return "usage-\(viewport)-\(appearance).png"
    }

    static let reviewConfigurations: [Self] = [
        Self(viewport: "minimum", width: 960, height: 560, colorScheme: .light, capacityState: .connected, recordedUsageState: .sevenDays),
        Self(viewport: "minimum", width: 960, height: 560, colorScheme: .dark, capacityState: .connected, recordedUsageState: .sevenDays),
        Self(viewport: "reference", width: 1120, height: 900, colorScheme: .light, capacityState: .connected, recordedUsageState: .sevenDays),
        Self(viewport: "reference", width: 1120, height: 900, colorScheme: .dark, capacityState: .connected, recordedUsageState: .sevenDays),
        Self(viewport: "weekly-reference", width: 1120, height: 1120, colorScheme: .light, capacityState: .connected, recordedUsageState: .ninetyDays),
        Self(viewport: "weekly-reference", width: 1120, height: 1120, colorScheme: .dark, capacityState: .connected, recordedUsageState: .ninetyDays),
        Self(viewport: "disconnected-reference", width: 1120, height: 900, colorScheme: .light, capacityState: .disconnected, recordedUsageState: .sevenDays),
        Self(viewport: "disconnected-reference", width: 1120, height: 900, colorScheme: .dark, capacityState: .disconnected, recordedUsageState: .sevenDays),
    ]
}

enum UsageSnapshotRenderer {
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
        configurations: [UsageSnapshotConfiguration] = UsageSnapshotConfiguration.reviewConfigurations
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
            let glance: GlanceState
            switch configuration.capacityState {
            case .connected:
                glance = GlanceState(preloaded: fixture.glanceSnapshot)
            case .disconnected:
                glance = GlanceState(
                    preloadedPhase: .disconnected("Synthetic review: live capacity feed unavailable"),
                    lastUpdated: nil
                )
            }
            let dashboard = DashboardStore(
                preloaded: fixture,
                usageState: configuration.recordedUsageState.storeState(for: fixture)
            )
            let selection = AppSelection()
            selection.pane = .usage
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
