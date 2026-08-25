import AppKit
import SwiftUI

/// Synthetic endpoint payloads for deterministic dashboard review. The
/// fixture intentionally uses the app's wire decoders so API-shape changes
/// cannot silently leave the visual harness on a separate model.
struct DashboardSnapshotFixture: Decodable {
    static let supportedSessionsSchema = "agentacct.sessions.v1"
    static let supportedPlanSchema = "agentacct.plan.v1"

    let daemonVersion: String
    let glance: Glance
    let sessions: V1SessionsPayload
    let plan: V1PlanPayload
    let usage: UsageSummary

    enum CodingKeys: String, CodingKey {
        case glance, sessions, plan, usage
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
        guard fixture.sessions.schema == supportedSessionsSchema else {
            throw SnapshotError.unsupportedSchema(
                payload: "sessions",
                actual: fixture.sessions.schema,
                expected: supportedSessionsSchema
            )
        }
        guard fixture.plan.schema == supportedPlanSchema else {
            throw SnapshotError.unsupportedSchema(
                payload: "plan",
                actual: fixture.plan.schema,
                expected: supportedPlanSchema
            )
        }
        return fixture
    }

    var glanceSnapshot: GlanceSnapshot {
        GlanceSnapshot(glance: glance, daemonVersion: daemonVersion)
    }
}

enum SnapshotError: LocalizedError {
    case unsupportedSchema(payload: String, actual: String, expected: String)
    case renderProducedNoImage
    case pngEncodingFailed

    var errorDescription: String? {
        switch self {
        case .unsupportedSchema(let payload, let actual, let expected):
            return "fixture \(payload) payload serves \(actual); expected \(expected)"
        case .renderProducedNoImage:
            return "render produced no image"
        case .pngEncodingFailed:
            return "PNG encoding failed"
        }
    }
}

struct DashboardSnapshotConfiguration {
    let viewport: String
    let width: CGFloat
    let height: CGFloat
    let colorScheme: ColorScheme

    var filename: String {
        let appearance = colorScheme == .dark ? "dark" : "light"
        return "dashboard-\(viewport)-\(appearance).png"
    }

    static let reviewConfigurations: [Self] = [
        Self(viewport: "minimum", width: 960, height: 560, colorScheme: .light),
        Self(viewport: "minimum", width: 960, height: 560, colorScheme: .dark),
        Self(viewport: "reference", width: 1120, height: 680, colorScheme: .light),
        Self(viewport: "reference", width: 1120, height: 680, colorScheme: .dark),
    ]
}

enum DashboardSnapshotRenderer {
    @MainActor
    static func render(
        fixture: DashboardSnapshotFixture,
        outputDirectory: URL,
        configurations: [DashboardSnapshotConfiguration] = DashboardSnapshotConfiguration.reviewConfigurations
    ) throws -> [URL] {
        try FileManager.default.createDirectory(
            at: outputDirectory,
            withIntermediateDirectories: true
        )

        SnapshotMode.enabled = true
        SnapshotMode.boundsScrollContentToViewport = true
        defer {
            SnapshotMode.enabled = false
            SnapshotMode.boundsScrollContentToViewport = false
            SnapshotScheme.override = nil
        }

        let glance = GlanceState(preloaded: fixture.glanceSnapshot)
        let dashboard = DashboardStore(preloaded: fixture)
        let selection = AppSelection()
        selection.pane = .dashboard

        return try configurations.map { configuration in
            SnapshotScheme.override = configuration.colorScheme
            // A packaged app consistently offers setup here. Injecting that
            // state keeps SwiftPM and packaged-build snapshots identical.
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
            let outputURL = outputDirectory.appendingPathComponent(configuration.filename)
            try SnapshotImageWriter.render(view, to: outputURL)
            return outputURL
        }
    }
}

enum SnapshotImageWriter {
    @MainActor
    static func render(_ view: some View, to url: URL) throws {
        let renderer = ImageRenderer(content: view)
        renderer.scale = 2
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
