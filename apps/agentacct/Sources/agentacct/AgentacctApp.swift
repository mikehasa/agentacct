import SwiftUI

// The agentacct menu bar app: a thin display shell over the daemon's
// /v1/glance lane (discovery + bearer token from <store>/local-api.json).
// All aggregation, calibration, and honesty logic lives in the Python daemon;
// this process only renders what the API vouches for.

@main
struct AgentacctApp: App {
    @StateObject private var state = GlanceState()

    var body: some Scene {
        MenuBarExtra {
            MenuContent(state: state)
        } label: {
            Text(state.menuBarTitle)
        }
        .menuBarExtraStyle(.window)
    }
}
