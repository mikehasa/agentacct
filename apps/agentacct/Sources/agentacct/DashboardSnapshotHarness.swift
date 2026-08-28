import AppKit
import SwiftUI

/// Synthetic endpoint payloads for deterministic dashboard review. The
/// fixture intentionally uses the app's wire decoders so API-shape changes
/// cannot silently leave the visual harness on a separate model.
struct DashboardSnapshotFixture: Decodable {
    static let supportedPlanSchema = "agentacct.plan.v1"
    static let supportedTasksSchema = "agentacct.receipt.v1"

    let daemonVersion: String
    let glance: Glance
    let menuSparseGlance: Glance?
    let plan: V1PlanPayload
    let tasks: ReceiptTasksPayload
    let usage: UsageSummary
    let work: WorkSnapshotFixture?

    enum CodingKeys: String, CodingKey {
        case glance, plan, tasks, usage, work
        case menuSparseGlance = "menu_sparse_glance"
        case daemonVersion = "daemon_version"
    }

    static func load(from url: URL) throws -> Self {
        try decode(Data(contentsOf: url))
    }

    static func decode(_ data: Data) throws -> Self {
        let fixture = try JSONDecoder().decode(Self.self, from: data)
        guard fixture.glance.schema == GlanceClient.supportedGlanceSchema else {
            throw SnapshotError.unsupportedSchema(
                payload: "glance",
                actual: fixture.glance.schema,
                expected: GlanceClient.supportedGlanceSchema
            )
        }
        if let menuSparseGlance = fixture.menuSparseGlance,
           menuSparseGlance.schema != GlanceClient.supportedGlanceSchema
        {
            throw SnapshotError.unsupportedSchema(
                payload: "sparse menu glance",
                actual: menuSparseGlance.schema,
                expected: GlanceClient.supportedGlanceSchema
            )
        }
        guard fixture.plan.schema == supportedPlanSchema else {
            throw SnapshotError.unsupportedSchema(
                payload: "plan",
                actual: fixture.plan.schema,
                expected: supportedPlanSchema
            )
        }
        guard fixture.tasks.schema == supportedTasksSchema else {
            throw SnapshotError.unsupportedSchema(
                payload: "tasks",
                actual: fixture.tasks.schema,
                expected: supportedTasksSchema
            )
        }
        if let work = fixture.work {
            guard work.receipt.schemaVersion == supportedTasksSchema else {
                throw SnapshotError.unsupportedSchema(
                    payload: "work receipt",
                    actual: work.receipt.schemaVersion,
                    expected: supportedTasksSchema
                )
            }
            for session in work.sessions where session.schema != WorkSnapshotFixture.supportedSessionSchema {
                throw SnapshotError.unsupportedSchema(
                    payload: "work session",
                    actual: session.schema,
                    expected: WorkSnapshotFixture.supportedSessionSchema
                )
            }
        }
        return fixture
    }

    var glanceSnapshot: GlanceSnapshot {
        GlanceSnapshot(glance: glance, daemonVersion: daemonVersion)
    }
}

/// Detailed endpoint payloads needed by the Work record. The table reuses
/// `tasks`; the selected receipt and expanded session rows need their own
/// fixture lane because deterministic rendering cannot wait for network-backed
/// SwiftUI `.task` loaders.
struct WorkSnapshotFixture: Decodable {
    static let supportedSessionSchema = "agentacct.v1-session-detail.v1"

    let receipt: Receipt
    let sessions: [V1SessionDetail]
}

enum SnapshotError: LocalizedError {
    case unsupportedSchema(payload: String, actual: String, expected: String)
    case missingFixtureDate
    case missingWorkFixture
    case renderProducedNoImage
    case pngEncodingFailed
    case snapshotContentExceedsCanvas(filename: String, requiredHeight: Int, availableHeight: Int)

    var errorDescription: String? {
        switch self {
        case .unsupportedSchema(let payload, let actual, let expected):
            return "fixture \(payload) payload serves \(actual); expected \(expected)"
        case .missingFixtureDate:
            return "fixture glance.generated_at is required to pin relative time labels"
        case .missingWorkFixture:
            return "fixture work payload is required to render Work review snapshots"
        case .renderProducedNoImage:
            return "render produced no image"
        case .pngEncodingFailed:
            return "PNG encoding failed"
        case .snapshotContentExceedsCanvas(let filename, let requiredHeight, let availableHeight):
            return "snapshot \(filename) needs \(requiredHeight) pt of content height; canvas provides \(availableHeight) pt"
        }
    }
}

struct DashboardSnapshotConfiguration {
    let viewport: String
    let width: CGFloat
    let height: CGFloat
    let colorScheme: ColorScheme
    let workState: SnapshotWorkStoreState

    var filename: String {
        let appearance = colorScheme == .dark ? "dark" : "light"
        return "dashboard-\(viewport)-\(appearance).png"
    }

    static let reviewConfigurations: [Self] = [
        Self(viewport: "minimum", width: 960, height: 560, colorScheme: .light, workState: .populated),
        Self(viewport: "minimum", width: 960, height: 560, colorScheme: .dark, workState: .populated),
        // The reference viewport must show the complete dashboard, including
        // chart labels. The shorter minimum pair intentionally verifies the
        // real top-of-scroll experience instead.
        Self(viewport: "reference", width: 1120, height: 800, colorScheme: .light, workState: .populated),
        Self(viewport: "reference", width: 1120, height: 800, colorScheme: .dark, workState: .populated),
        Self(viewport: "attention-unavailable", width: 1120, height: 800, colorScheme: .light, workState: .listErrorWithRetainedData),
        Self(viewport: "attention-unavailable", width: 1120, height: 800, colorScheme: .dark, workState: .listErrorWithRetainedData),
    ]
}

enum DashboardSnapshotRenderer {
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
        configurations: [DashboardSnapshotConfiguration] = DashboardSnapshotConfiguration.reviewConfigurations
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
        // Freeze relative labels at the fixture's own generation time. The
        // defer below restores live-clock behavior even when rendering throws.
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
                workState: configuration.workState
            )
            let selection = AppSelection()
            selection.pane = .dashboard
            // A packaged app consistently offers setup here. Injecting that
            // state keeps SwiftPM and packaged-build snapshots identical.
            let view = MainWindow(canSetUpOverride: true)
                .environment(glance)
                .environment(dashboard)
                .environment(selection)
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
}

enum SnapshotImageWriter {
    @MainActor
    static func renderedSize(
        _ view: some View,
        proposedSize: ProposedViewSize = .unspecified
    ) throws -> CGSize {
        let renderer = ImageRenderer(content: view)
        renderer.scale = 2
        renderer.colorMode = .nonLinear
        renderer.proposedSize = proposedSize
        guard let cgImage = renderer.cgImage else {
            throw SnapshotError.renderProducedNoImage
        }
        return CGSize(
            width: CGFloat(cgImage.width) / renderer.scale,
            height: CGFloat(cgImage.height) / renderer.scale
        )
    }

    @MainActor
    static func render(_ view: some View, to url: URL, size: CGSize? = nil) throws {
        let renderer = ImageRenderer(content: view)
        renderer.scale = 2
        renderer.colorMode = .nonLinear
        if let size {
            renderer.proposedSize = ProposedViewSize(width: size.width, height: size.height)
        }
        guard let cgImage = renderer.cgImage else {
            throw SnapshotError.renderProducedNoImage
        }
        let representation = NSBitmapImageRep(cgImage: cgImage)
        guard let png = representation.representation(using: .png, properties: [:]) else {
            throw SnapshotError.pngEncodingFailed
        }
        try png.write(to: url)
    }
}
