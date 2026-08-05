import SwiftUI

// Entry point. `agentacct --snapshot <dir>` renders the window and the menu
// dropdown to PNGs from live daemon data (offscreen, no screen capture) —
// design iteration eyes and future README screenshots. Anything else launches
// the app.

if let flagIndex = CommandLine.arguments.firstIndex(of: "--snapshot"),
   CommandLine.arguments.count > flagIndex + 1 {
    SnapshotRunner.run(outputDir: CommandLine.arguments[flagIndex + 1])
} else {
    AgentacctApp.main()
}
