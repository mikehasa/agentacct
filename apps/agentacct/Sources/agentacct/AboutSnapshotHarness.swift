import AppKit

struct AboutSnapshotConfiguration {
    let appearanceName: NSAppearance.Name
    let filename: String

    static let reviewConfigurations: [Self] = [
        Self(appearanceName: .aqua, filename: "about-panel-light.png"),
        Self(appearanceName: .darkAqua, filename: "about-panel-dark.png"),
    ]
}

enum AboutSnapshotError: LocalizedError {
    case applicationIconUnavailable(URL)
    case appearanceUnavailable(NSAppearance.Name)
    case panelUnavailable
    case rootViewUnavailable
    case bitmapUnavailable
    case pngEncodingFailed

    var errorDescription: String? {
        switch self {
        case .applicationIconUnavailable(let url):
            return "The About snapshot application icon is unavailable at \(url.path)"
        case .appearanceUnavailable(let name):
            return "About snapshot appearance is unavailable: \(name.rawValue)"
        case .panelUnavailable:
            return "AppKit did not create a visible standard About panel"
        case .rootViewUnavailable:
            return "The standard About panel has no renderable root view"
        case .bitmapUnavailable:
            return "The standard About panel could not create a bitmap representation"
        case .pngEncodingFailed:
            return "The standard About panel could not encode a PNG"
        }
    }
}

/// Renders the native panel used by `AppAbout.present`, including AppKit's
/// window chrome. The application name and icon are explicit because SwiftPM
/// tests do not run from the packaged app bundle that supplies them in normal
/// operation.
enum AboutSnapshotRenderer {
    static let reviewBuildIdentity = AppBuildIdentity(infoDictionary: [
        "CFBundleShortVersionString": "0.10.1",
        "AgentacctGitCommit": "0123456789abcdef0123456789abcdef01234567",
        "AgentacctBuildDescription": "v0.10.1-3-g0123456",
    ])

    @MainActor
    static func render(
        outputDirectory: URL,
        applicationIcon: NSImage,
        buildIdentity: AppBuildIdentity = reviewBuildIdentity,
        configurations: [AboutSnapshotConfiguration] = AboutSnapshotConfiguration.reviewConfigurations
    ) throws -> [URL] {
        try FileManager.default.createDirectory(
            at: outputDirectory,
            withIntermediateDirectories: true
        )

        let application = NSApplication.shared
        let previousAppearance = application.appearance
        let previousApplicationIcon = application.applicationIconImage
        defer {
            application.appearance = previousAppearance
            application.applicationIconImage = previousApplicationIcon
        }

        application.applicationIconImage = applicationIcon
        let previouslyVisiblePanels = Set(
            application.windows.compactMap { window -> ObjectIdentifier? in
                guard let panel = window as? NSPanel, panel.isVisible else { return nil }
                return ObjectIdentifier(panel)
            }
        )
        var options = AppAbout.panelOptions(for: buildIdentity)
        options[.applicationName] = "agentacct"
        options[.applicationIcon] = applicationIcon
        application.orderFrontStandardAboutPanel(options: options)
        RunLoop.current.run(until: Date().addingTimeInterval(0.05))

        guard let panel = application.windows.compactMap({ $0 as? NSPanel }).first(where: {
            $0.isVisible && !previouslyVisiblePanels.contains(ObjectIdentifier($0))
        }) else {
            throw AboutSnapshotError.panelUnavailable
        }
        defer { panel.close() }

        return try configurations.map { configuration in
            guard let appearance = NSAppearance(named: configuration.appearanceName) else {
                throw AboutSnapshotError.appearanceUnavailable(configuration.appearanceName)
            }
            application.appearance = appearance
            panel.appearance = appearance
            panel.contentView?.layoutSubtreeIfNeeded()
            panel.displayIfNeeded()
            RunLoop.current.run(until: Date().addingTimeInterval(0.05))

            guard let rootView = panel.contentView?.superview else {
                throw AboutSnapshotError.rootViewUnavailable
            }
            let outputURL = outputDirectory.appendingPathComponent(configuration.filename)
            try writePNG(of: rootView, to: outputURL)
            return outputURL
        }
    }

    @MainActor
    private static func writePNG(of view: NSView, to url: URL) throws {
        guard let representation = view.bitmapImageRepForCachingDisplay(in: view.bounds) else {
            throw AboutSnapshotError.bitmapUnavailable
        }
        view.cacheDisplay(in: view.bounds, to: representation)
        guard let png = representation.representation(using: .png, properties: [:]) else {
            throw AboutSnapshotError.pngEncodingFailed
        }
        try png.write(to: url)
    }
}
