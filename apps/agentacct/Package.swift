// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "agentacct",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "agentacct",
            path: "Sources/agentacct"
        ),
        .testTarget(
            name: "agentacctTests",
            dependencies: ["agentacct"],
            path: "Tests/agentacctTests",
            // Baselines are compared directly from the source checkout. They
            // are review artifacts, not resources copied into the test bundle.
            exclude: ["ReferenceImages"],
            resources: [.process("Fixtures")]
        )
    ]
)
