import SwiftUI

// Entry point. `agentacct --snapshot <dir>` renders every pane from live daemon
// data; the versioned fixture flags render deterministic dashboard, Work, or
// menu review matrices without network or account data. Anything else launches
// the app.

if let flagIndex = CommandLine.arguments.firstIndex(of: "--snapshot-work-fixture") {
    if CommandLine.arguments.count > flagIndex + 2 {
        SnapshotRunner.runWorkFixture(
            fixturePath: CommandLine.arguments[flagIndex + 1],
            outputDir: CommandLine.arguments[flagIndex + 2]
        )
    } else {
        FileHandle.standardError.write(
            Data("usage: agentacct --snapshot-work-fixture <fixture.json> <output-dir>\n".utf8)
        )
        exit(2)
    }
} else if let flagIndex = CommandLine.arguments.firstIndex(of: "--snapshot-menu-fixture") {
    if CommandLine.arguments.count > flagIndex + 2 {
        SnapshotRunner.runMenuFixture(
            fixturePath: CommandLine.arguments[flagIndex + 1],
            outputDir: CommandLine.arguments[flagIndex + 2]
        )
    } else {
        FileHandle.standardError.write(
            Data("usage: agentacct --snapshot-menu-fixture <fixture.json> <output-dir>\n".utf8)
        )
        exit(2)
    }
} else if let flagIndex = CommandLine.arguments.firstIndex(of: "--snapshot-dashboard-fixture") {
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
} else if let flagIndex = CommandLine.arguments.firstIndex(of: "--snapshot") {
    if CommandLine.arguments.count > flagIndex + 1 {
        SnapshotRunner.run(outputDir: CommandLine.arguments[flagIndex + 1])
    } else {
        FileHandle.standardError.write(
            Data("usage: agentacct --snapshot <output-dir>\n".utf8)
        )
        exit(2)
    }
} else {
    AgentacctApp.main()
}
