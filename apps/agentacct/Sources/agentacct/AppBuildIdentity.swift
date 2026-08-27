import Foundation

/// The packaged app's release and source identity. Build metadata lives in the
/// bundle rather than in Swift constants so one release version can still name
/// the exact commit that produced a local, CI, or distributable build.
struct AppBuildIdentity: Equatable {
    private let releaseVersion: String?
    private let gitCommit: String?
    private let buildDescription: String?

    static let current = AppBuildIdentity(infoDictionary: Bundle.main.infoDictionary)

    init(infoDictionary: [String: Any]?) {
        releaseVersion = Self.nonBlankString(
            infoDictionary?["CFBundleShortVersionString"]
        )
        gitCommit = Self.nonBlankString(infoDictionary?["AgentacctGitCommit"])
        buildDescription = Self.nonBlankString(
            infoDictionary?["AgentacctBuildDescription"]
        )
    }

    private var shortCommit: String? {
        gitCommit.map { String($0.prefix(12)) }
    }

    private var isDirty: Bool {
        buildDescription?.hasSuffix("-dirty") == true
    }

    var compactLabel: String {
        switch (releaseVersion, shortCommit) {
        case let (version?, commit?):
            return "v\(version) · \(commit)" + (isDirty ? " · dirty" : "")
        case let (version?, nil):
            return "v\(version)"
        case let (nil, commit?):
            return "build \(commit)" + (isDirty ? " · dirty" : "")
        case (nil, nil):
            return "development build"
        }
    }

    var accessibilityLabel: String {
        var label = "agentacct"
        if let releaseVersion {
            label += " version \(releaseVersion)"
        }
        if let shortCommit {
            label += ", build \(shortCommit)"
        }
        if isDirty {
            label += ", dirty working tree"
        }
        if releaseVersion == nil, shortCommit == nil {
            label += " development build"
        }
        return label
    }

    var detailLabel: String {
        if let buildDescription, let gitCommit {
            return "\(buildDescription) · commit \(gitCommit)"
        }
        if let buildDescription {
            return buildDescription
        }
        if let gitCommit {
            return "commit \(gitCommit)"
        }
        if let releaseVersion {
            return "agentacct \(releaseVersion) · no commit identity embedded"
        }
        return "No packaged build identity"
    }

    private static func nonBlankString(_ value: Any?) -> String? {
        guard let string = value as? String else { return nil }
        let trimmed = string.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}
