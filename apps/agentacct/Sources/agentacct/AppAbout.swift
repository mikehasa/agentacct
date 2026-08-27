import AppKit

/// Keeps build provenance available on demand without competing with the
/// menu-bar glance. AppKit owns the About panel's layout, icon, typography,
/// accessibility, and platform-specific behavior.
enum AppAbout {
    static func panelOptions(
        for identity: AppBuildIdentity
    ) -> [NSApplication.AboutPanelOptionKey: Any] {
        var options: [NSApplication.AboutPanelOptionKey: Any] = [:]
        if let applicationVersion = identity.aboutPanelApplicationVersion {
            options[.applicationVersion] = applicationVersion
            // An explicit empty build suppresses AppKit's CFBundleVersion
            // fallback, which would otherwise repeat the release version when
            // source metadata is unavailable.
            options[.version] = identity.aboutPanelBuildVersion ?? ""
        } else if let buildVersion = identity.aboutPanelBuildVersion {
            options[.version] = buildVersion
        }
        return options
    }

    @MainActor
    static func present(identity: AppBuildIdentity = .current) {
        NSApp.activate(ignoringOtherApps: true)
        NSApp.orderFrontStandardAboutPanel(options: panelOptions(for: identity))
    }
}
