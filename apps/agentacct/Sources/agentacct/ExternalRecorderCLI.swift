import Foundation

/// A user-installed `agentacct` the app did not install and does not manage.
///
/// After a reboot the recorder is gone, and a machine whose CLI came from
/// pipx/uv (or a development build with no embedded CLI) has no app-owned
/// recorder to reconnect to. This type is the one thing such a machine can
/// start, and it is used only for that: the app runs the binary, reports what
/// it started, and never installs, updates, replaces or signals it.
struct ExternalRecorderCLI: Equatable {
    /// The path a user would have typed, kept for display.
    let launcher: URL
    /// The resolved regular file actually executed.
    let executable: URL
    /// The version parsed from the CLI's own `--version` banner.
    let version: String
    /// The banner verbatim, so the UI can show exactly what the binary said.
    let banner: String

    /// The honest caption shown next to the start control. It names the binary
    /// and states the boundary: this recorder is not app-managed, so the app
    /// can start it but cannot repair, upgrade or stop it.
    var detail: String {
        "Starts agentacct \(version) at \(launcher.path) — not installed or managed by this app."
    }

    /// Accepts only the CLI's own version banner ("agentacct 0.12.4"). Anything
    /// else is not a binary this app will run on the user's behalf.
    static func version(fromBanner text: String) -> String? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.hasPrefix("agentacct ") else { return nil }
        let value = String(trimmed.dropFirst("agentacct ".count))
        let fields = value.split(separator: ".", omittingEmptySubsequences: false)
        guard fields.count == 3,
              fields.allSatisfy({ !$0.isEmpty && $0.allSatisfy(\.isNumber) })
        else { return nil }
        return value
    }
}

/// What a user's explicit "Start recorder" click would run.
enum RecorderStartTarget: Equatable {
    /// The recorder this app installed and verified (the existing path).
    case appOwned
    /// A user-installed CLI the app can run but does not manage.
    case external(ExternalRecorderCLI)

    /// nil for the app-owned recorder: the surrounding UI already describes it.
    var detail: String? {
        switch self {
        case .appOwned: return nil
        case .external(let cli): return cli.detail
        }
    }
}

/// Path-only resolution of the CLI a user installed themselves. No process is
/// run here; the caller probes candidates with `--version` before using one.
enum ExternalRecorderCLIResolver {
    /// The user-facing install locations in the order a login shell would find
    /// them: the documented `~/.local/bin` entry point first, then PATH entries.
    static func candidates(home: URL, environment: [String: String]) -> [URL] {
        var results: [URL] = []
        var seen = Set<String>()
        func add(_ url: URL) {
            let key = url.standardizedFileURL.path
            guard seen.insert(key).inserted else { return }
            results.append(url)
        }
        add(home.appendingPathComponent(".local/bin/agentacct"))
        for entry in (environment["PATH"] ?? "").split(separator: ":") where !entry.isEmpty {
            add(URL(fileURLWithPath: String(entry), isDirectory: true).appendingPathComponent("agentacct"))
        }
        return results
    }

    /// Candidates that are executable regular files after symlink resolution,
    /// excluding the app-owned recorder location and this app's own bundle:
    /// those are the `reconnectRecorder` path's business, not this one's.
    static func launchPoints(
        home: URL,
        environment: [String: String],
        fileManager: FileManager = .default,
        bundleURL: URL = Bundle.main.bundleURL
    ) -> [URL] {
        let ownedDirectory = home
            .appendingPathComponent(".local/share/agentacct/cli", isDirectory: true)
            .standardizedFileURL.resolvingSymlinksInPath()
        let bundle = bundleURL.standardizedFileURL.resolvingSymlinksInPath().path
        return candidates(home: home, environment: environment).filter { candidate in
            // Symlinked launch points count: pipx/uv install a script that execs
            // its own interpreter, and the resolved target is the proof of what
            // would actually run.
            let resolved = candidate.standardizedFileURL.resolvingSymlinksInPath()
            guard isExecutableRegularFile(resolved, fileManager: fileManager) else { return false }
            guard !isInside(resolved, directory: ownedDirectory) else { return false }
            guard resolved.path != bundle, !resolved.path.hasPrefix(bundle + "/") else { return false }
            return true
        }
    }

    private static func isExecutableRegularFile(_ url: URL, fileManager: FileManager) -> Bool {
        guard let values = try? url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey]),
              values.isRegularFile == true, values.isSymbolicLink != true
        else { return false }
        return fileManager.isExecutableFile(atPath: url.path)
    }

    private static func isInside(_ url: URL, directory: URL) -> Bool {
        url.path == directory.path || url.path.hasPrefix(directory.path + "/")
    }
}
