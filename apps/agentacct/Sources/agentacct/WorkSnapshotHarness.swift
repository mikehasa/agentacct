import SwiftUI

enum WorkSnapshotState: String {
    case table
    case receipt
    case receiptActionsFiles = "receipt-actions-files"
    case receiptActionsCommands = "receipt-actions-commands"
    case receiptActionsTools = "receipt-actions-tools"
    case empty
    case listError = "list-error"
    case receiptLoading = "receipt-loading"
    case receiptError = "receipt-error"

    var storeState: SnapshotWorkStoreState {
        switch self {
        case .table, .receipt, .receiptActionsFiles, .receiptActionsCommands, .receiptActionsTools:
            return .populated
        case .empty: return .empty
        case .listError: return .listError
        case .receiptLoading: return .receiptLoading
        case .receiptError: return .receiptError
        }
    }

    var selectsReceipt: Bool {
        switch self {
        case .receipt, .receiptActionsFiles, .receiptActionsCommands, .receiptActionsTools,
             .receiptLoading, .receiptError:
            return true
        case .table, .empty, .listError: return false
        }
    }

    var actionDetailKind: ActionDetailKind? {
        switch self {
        case .receiptActionsFiles: return .files
        case .receiptActionsCommands: return .commands
        case .receiptActionsTools: return .tools
        case .table, .receipt, .empty, .listError, .receiptLoading, .receiptError: return nil
        }
    }
}

struct WorkSnapshotConfiguration {
    let state: WorkSnapshotState
    let viewport: String
    let width: CGFloat
    let height: CGFloat
    let colorScheme: ColorScheme

    var filename: String {
        let appearance = colorScheme == .dark ? "dark" : "light"
        return "work-\(state.rawValue)-\(viewport)-\(appearance).png"
    }

    static let reviewConfigurations: [Self] = {
        let core = [WorkSnapshotState.table, .receipt].flatMap { state in
            [
                Self(state: state, viewport: "minimum", width: 960, height: 560, colorScheme: .light),
                Self(state: state, viewport: "minimum", width: 960, height: 560, colorScheme: .dark),
                Self(state: state, viewport: "reference", width: 1120, height: 800, colorScheme: .light),
                Self(state: state, viewport: "reference", width: 1120, height: 800, colorScheme: .dark),
            ]
        }
        let transient = [
            WorkSnapshotState.empty,
            .listError,
            .receiptLoading,
            .receiptError,
        ].flatMap { state in
            [
                Self(state: state, viewport: "reference", width: 1120, height: 800, colorScheme: .light),
                Self(state: state, viewport: "reference", width: 1120, height: 800, colorScheme: .dark),
            ]
        }
        let actionDetails = [
            WorkSnapshotState.receiptActionsFiles,
            .receiptActionsCommands,
            .receiptActionsTools,
        ].flatMap { state in
            [ColorScheme.light, .dark].map { scheme in
                Self(
                    state: state,
                    viewport: "detail",
                    width: 1120,
                    height: 1000,
                    colorScheme: scheme
                )
            }
        }
        return core + actionDetails + transient
    }()
}

enum WorkSnapshotRenderer {
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
        configurations: [WorkSnapshotConfiguration] = WorkSnapshotConfiguration.reviewConfigurations
    ) throws -> [URL] {
        guard let generatedAt = fixture.glance.generatedAt else {
            throw SnapshotError.missingFixtureDate
        }
        guard let work = fixture.work else {
            throw SnapshotError.missingWorkFixture
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

        let glance = GlanceState(preloaded: fixture.glanceSnapshot)
        return try configurations.map { configuration in
            SnapshotScheme.override = configuration.colorScheme
            let dashboard = DashboardStore(
                preloaded: fixture,
                workState: configuration.state.storeState
            )
            let selection = AppSelection()
            selection.pane = .work
            selection.taskId = configuration.state.selectsReceipt ? work.receipt.taskId : nil

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
                .environment(\.workActionDetailsSelectionForSnapshot, configuration.state.actionDetailKind)
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
