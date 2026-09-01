import SwiftUI

struct SourcesSnapshotConfiguration {
    let state: SnapshotSourcesStoreState
    let name: String
    let colorScheme: ColorScheme
    let width: CGFloat
    let height: CGFloat
    let dynamicTypeSize: DynamicTypeSize

    init(
        state: SnapshotSourcesStoreState,
        name: String,
        colorScheme: ColorScheme,
        width: CGFloat = 1_120,
        height: CGFloat = 800,
        dynamicTypeSize: DynamicTypeSize = .medium
    ) {
        self.state = state
        self.name = name
        self.colorScheme = colorScheme
        self.width = width
        self.height = height
        self.dynamicTypeSize = dynamicTypeSize
    }

    var filename: String {
        "sources-\(name)-\(colorScheme == .dark ? "dark" : "light").png"
    }

    static let reviewConfigurations: [Self] = [
        Self(state: .healthy, name: "healthy", colorScheme: .light),
        Self(state: .healthy, name: "healthy", colorScheme: .dark),
        Self(state: .degraded, name: "degraded", colorScheme: .light),
        Self(state: .degraded, name: "degraded", colorScheme: .dark),
        Self(state: .unsupported, name: "unsupported", colorScheme: .light),
        Self(state: .unsupported, name: "unsupported", colorScheme: .dark),
        Self(state: .serviceUnavailable, name: "unavailable", colorScheme: .light),
        Self(state: .serviceUnavailable, name: "unavailable", colorScheme: .dark),
        Self(state: .requestFailed, name: "request-failed", colorScheme: .light),
        Self(state: .requestFailed, name: "request-failed", colorScheme: .dark),
        Self(state: .retainedFailure, name: "saved-failure", colorScheme: .light),
        Self(state: .retainedFailure, name: "saved-failure", colorScheme: .dark),
        Self(
            state: .healthy,
            name: "accessibility",
            colorScheme: .dark,
            height: 1_400,
            dynamicTypeSize: .accessibility3
        ),
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
            let glance = GlanceState(preloaded: fixture.glanceSnapshot)
            let dashboard = DashboardStore(
                preloaded: fixture,
                sourcesState: configuration.state
            )
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
                .environment(\.dynamicTypeSize, configuration.dynamicTypeSize)
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
}
