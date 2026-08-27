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

    var aboutPanelApplicationVersion: String? {
        releaseVersion
    }

    var aboutPanelBuildVersion: String? {
        shortCommit.map { $0 + (isDirty ? "-dirty" : "") }
    }

    private static func nonBlankString(_ value: Any?) -> String? {
        guard let string = value as? String else { return nil }
        let trimmed = string.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}
