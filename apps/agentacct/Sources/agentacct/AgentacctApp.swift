import SwiftUI

// agentacct, native: a persistent MenuBarExtra glance plus the full window
// (Sessions → detail, Usage by agent/model, Limits). The menu bar is for
// looking; the window is for digging — clicking a session in the dropdown
// opens the window on that session. All aggregation and honesty logic lives
// in the Python daemon; this process only renders what the API vouches for.

@main
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
            Text(glance.menuBarTitle)
        }
        .menuBarExtraStyle(.window)

        Window("agentacct", id: "main") {
            MainWindow()
                .environmentObject(glance)
                .environmentObject(dashboard)
                .environmentObject(selection)
        }
        .defaultSize(width: 980, height: 620)
    }
}
