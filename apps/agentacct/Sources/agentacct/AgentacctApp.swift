import SwiftUI

// agentacct, native: a persistent MenuBarExtra glance plus the full window
// (task-first Work Receipts, merged Usage & limits, and Sources). The menu bar
// is for looking; the window is for digging — clicking a recent session
// resolves its Task in Work. All aggregation and honesty logic lives in the
// Python daemon; this process only renders what the API vouches for.

struct AgentacctApp: App {
    @NSApplicationDelegateAdaptor(AgentacctAppDelegate.self) private var appDelegate
    @State private var dashboard = DashboardStore()
    @State private var selection = AppSelection()
    @State private var windowLaunch = NativeWindowLaunchRequest()
    @AppStorage("reading-size.v1") private var readingSize = 0

    private var lifecycle: AppLifecycleCoordinator { appDelegate.lifecycle }

    var body: some Scene {
        // SwiftUI presents the first eligible scene at launch. Put the full
        // window first so first-run setup is visible immediately on macOS 14.
        Window("agentacct", id: "main") {
            MainWindow(setup: lifecycle.setup, lifecycle: lifecycle)
                .environment(lifecycle.glance)
                .environment(dashboard)
                .environment(selection)
                .environment(\.dynamicTypeSize, ReadingSize.dynamicTypeSize(readingSize))
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1320, height: 860)
        .commands {
            NativeWindowCommands(id: "main", title: "agentacct", launch: windowLaunch)
            ReadingSizeCommands(selection: $readingSize)
            SectionCommands()
            RefreshCommands()
        }

        MenuBarExtra {
            MenuContent(
                awaitRecorderSynchronization: {
                    await lifecycle.waitUntilReady()
                },
                onStartRecorder: {
                    // Another surface may already be restarting the recorder (they
                    // share one SetupModel). Report that as "in progress", not a
                    // failure, so a second tap never spuriously opens the window.
                    if lifecycle.setup.reconnectPhase == .working { return true }
                    let started = await lifecycle.setup.reconnectRecorder()
                    if started {
                        lifecycle.glance.refreshNow()
                        await dashboard.refresh()
                    }
                    return started
                }
            )
                .environment(lifecycle.glance)
                .environment(dashboard)
                .environment(selection)
        } label: {
            // The Stamped Tile mark as a template image, so the system tints
            // it for light/dark/tinted menu bars. The weekly-plan % lives in
            // the dropdown and the window — the menu BAR shows no number
            // (the provider's own menu already does).
            Image(nsImage: MenuBarMark.templateImage())
                .accessibilityLabel("agentacct")
        }
        .menuBarExtraStyle(.window)
    }
}
