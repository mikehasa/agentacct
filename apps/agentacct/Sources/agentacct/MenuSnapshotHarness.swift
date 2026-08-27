import SwiftUI

struct MenuSnapshotConfiguration {
    let colorScheme: ColorScheme

    var filename: String {
        let appearance = colorScheme == .dark ? "dark" : "light"
        return "menu-connected-\(appearance).png"
    }

    static let reviewConfigurations: [Self] = [
        Self(colorScheme: .light),
        Self(colorScheme: .dark),
    ]
}

enum MenuSnapshotRenderer {
    private static let snapshotLocale = Locale(identifier: "en_US_POSIX")
    private static let snapshotTimeZone = TimeZone(secondsFromGMT: 0)!

    private static var snapshotCalendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.locale = snapshotLocale
        calendar.timeZone = snapshotTimeZone
        return calendar
    }

    static let reviewBuildIdentity = AppBuildIdentity(infoDictionary: [
        "CFBundleShortVersionString": "0.10.1",
        "AgentacctGitCommit": "0123456789abcdef0123456789abcdef01234567",
        "AgentacctBuildDescription": "v0.10.1-3-g0123456",
    ])

    @MainActor
    static func render(
        fixture: DashboardSnapshotFixture,
        outputDirectory: URL,
        buildIdentity: AppBuildIdentity = reviewBuildIdentity,
        configurations: [MenuSnapshotConfiguration] = MenuSnapshotConfiguration.reviewConfigurations
    ) throws -> [URL] {
        guard let generatedAt = fixture.glance.generatedAt else {
            throw SnapshotError.missingFixtureDate
        }
        try FileManager.default.createDirectory(
            at: outputDirectory,
            withIntermediateDirectories: true
        )

        SnapshotMode.enabled = true
        SnapshotMode.setFixtureDate(Date(timeIntervalSince1970: generatedAt))
        defer {
            SnapshotMode.enabled = false
            SnapshotMode.setFixtureDate(nil)
            SnapshotScheme.override = nil
        }

        let glance = GlanceState(preloaded: fixture.glanceSnapshot)
        let dashboard = DashboardStore(preloaded: fixture)
        let selection = AppSelection()

        return try configurations.map { configuration in
            SnapshotScheme.override = configuration.colorScheme
            let view = MenuContent(
                buildIdentity: buildIdentity,
                lastUpdatedTextOverride: "just now",
                launchAtLoginInitialState: false
            )
            .environmentObject(glance)
            .environmentObject(dashboard)
            .environmentObject(selection)
            .background(Theme.canvas)
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
            try SnapshotImageWriter.render(view, to: outputURL)
            return outputURL
        }
    }
}
