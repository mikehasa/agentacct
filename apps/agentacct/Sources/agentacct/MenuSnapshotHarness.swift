import SwiftUI

struct MenuSnapshotConfiguration {
    enum Density: String {
        case sparse, dense
    }

    let density: Density
    let colorScheme: ColorScheme

    var filename: String {
        let appearance = colorScheme == .dark ? "dark" : "light"
        return "menu-connected-\(density.rawValue)-\(appearance).png"
    }

    static let reviewConfigurations: [Self] = [
        Self(density: .sparse, colorScheme: .light),
        Self(density: .sparse, colorScheme: .dark),
        Self(density: .dense, colorScheme: .light),
        Self(density: .dense, colorScheme: .dark),
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

        return try configurations.map { configuration in
            let selectedGlance: Glance
            switch configuration.density {
            case .sparse:
                selectedGlance = fixture.menuSparseGlance ?? fixture.glance
            case .dense:
                selectedGlance = fixture.glance
            }
            let glance = GlanceState(preloaded: GlanceSnapshot(
                glance: selectedGlance,
                daemonVersion: fixture.daemonVersion
            ))
            let dashboard = DashboardStore(preloaded: fixture)
            let selection = AppSelection()
            SnapshotScheme.override = configuration.colorScheme
            let view = MenuContent(
                buildIdentity: buildIdentity,
                lastUpdatedTextOverride: "just now",
                launchAtLoginInitialState: false,
                snapshotBodyMaxHeight: configuration.density == .dense ? 420 : nil
            )
            .environment(glance)
            .environment(dashboard)
            .environment(selection)
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
