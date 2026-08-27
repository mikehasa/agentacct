import SwiftUI

// agentacct, native: a persistent MenuBarExtra glance plus the full window
// (task-first Work Receipts, merged Usage & limits, and Sources). The menu bar
// is for looking; the window is for digging — clicking a recent session
// resolves its Task in Work. All aggregation and honesty logic lives in the
// Python daemon; this process only renders what the API vouches for.

struct AgentacctApp: App {
    @StateObject private var glance = GlanceState()
    @StateObject private var dashboard = DashboardStore()
    @StateObject private var selection = AppSelection()

    var body: some Scene {
        MenuBarExtra {
            MenuContent()
                .environmentObject(glance)
                .environmentObject(dashboard)
                .environmentObject(selection)
        } label: {
            // The Stamped Tile mark as a template image, so the system tints
            // it for light/dark/tinted menu bars. The weekly-plan % lives in
            // the dropdown and the window — the menu BAR shows no number
            // (the provider's own menu already does).
            Image(nsImage: MenuBarMark.templateImage())
                .accessibilityLabel("agentacct")
        }
        .menuBarExtraStyle(.window)

        Window("agentacct", id: "main") {
            MainWindow()
                .environmentObject(glance)
                .environmentObject(dashboard)
                .environmentObject(selection)
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1040, height: 640)
    }
}
