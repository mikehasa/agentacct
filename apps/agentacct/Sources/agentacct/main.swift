import SwiftUI

// Entry point. `agentacct --snapshot <dir>` renders every pane from live daemon
// data; `--snapshot-dashboard-fixture <fixture> <dir>` renders the dashboard
// review matrix without network or account data. Anything else launches the app.

if let flagIndex = CommandLine.arguments.firstIndex(of: "--snapshot-dashboard-fixture") {
    if CommandLine.arguments.count > flagIndex + 2 {
        SnapshotRunner.runDashboardFixture(
            fixturePath: CommandLine.arguments[flagIndex + 1],
            outputDir: CommandLine.arguments[flagIndex + 2]
        )
    } else {
        FileHandle.standardError.write(
            Data("usage: agentacct --snapshot-dashboard-fixture <fixture.json> <output-dir>\n".utf8)
        )
        exit(2)
    }
} else if let flagIndex = CommandLine.arguments.firstIndex(of: "--snapshot"),
          CommandLine.arguments.count > flagIndex + 1 {
    SnapshotRunner.run(outputDir: CommandLine.arguments[flagIndex + 1])
} else {
    AgentacctApp.main()
}
