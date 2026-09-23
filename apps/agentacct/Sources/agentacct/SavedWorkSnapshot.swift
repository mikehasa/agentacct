import CryptoKit
import Foundation

/// A bounded copy of responses the app actually read. It is never a recorder
/// database, and cannot prove anything happened after an entry's saved date.
struct SavedWorkSnapshot: Codable {
    struct Entry: Codable {
        let path: String
        let receivedAt: Date
        let data: Data
        var requestStartedAt: Date? = nil
        var evidenceDate: Date? {
            if let projection = WorkProjectionMetadata.from(data) { return projection.builtDate }
            return receivedAt
        }
    }
    var schema = 1
    let storePath: String
    var entries: [String: Entry] = [:]

    static func accepts(_ path: String) -> Bool {
        path == "/v1/tasks?limit=200" || path.hasPrefix("/v1/receipt?task=")
            || path.hasPrefix("/v1/session?client=")
            || (path.hasPrefix("/v1/task-timeline?task=") && !path.contains("&"))
    }
    var collectionDate: Date? { entries["/v1/tasks?limit=200"]?.evidenceDate }
    var hasWork: Bool {
        guard let data = entries["/v1/tasks?limit=200"]?.data,
              let payload = try? JSONDecoder().decode(ReceiptTasksPayload.self, from: data) else { return false }
        return !payload.tasks.isEmpty
    }
    func value<T: Decodable>(_ path: String) throws -> T {
        guard let entry = entries[path] else { throw SavedWorkError.notSaved }
        return try JSONDecoder().decode(T.self, from: entry.data)
    }
    static func canonicalPath(_ store: URL) -> String { store.standardizedFileURL.resolvingSymlinksInPath().path }
    static func location(store: URL, cacheRoot: URL? = nil) -> URL {
        let root = cacheRoot ?? FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/agentacct/Saved Work", isDirectory: true)
        let hash = SHA256.hash(data: Data(canonicalPath(store).utf8)).map { String(format: "%02x", $0) }.joined()
        return root.appendingPathComponent(hash + ".json")
    }
    static func load(store: URL, cacheRoot: URL? = nil) -> SavedWorkSnapshot? {
        let url = location(store: store, cacheRoot: cacheRoot)
        guard let size = try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize,
              size <= 40_000_000, let data = try? Data(contentsOf: url),
              let saved = try? JSONDecoder().decode(Self.self, from: data),
              saved.schema == 1, saved.storePath == canonicalPath(store),
              saved.entries.allSatisfy({ accepts($0.key) && $0.value.path == $0.key }) else { return nil }
        return saved
    }
    static func current() -> SavedWorkSnapshot? {
        guard !SnapshotMode.enabled, let store = try? GlanceClient.storeDir() else { return nil }
        return load(store: store)
    }
}

enum SavedWorkError: LocalizedError {
    case notSaved, readOnly
    var errorDescription: String? {
        switch self {
        case .notSaved: return "This detail was not saved on this Mac. Reconnect the recorder to load it."
        case .readOnly: return "Saved work is read-only. Reconnect the recorder before changing a finding."
        }
    }
}

actor SavedWorkCache {
    static let shared = SavedWorkCache()
    private var memory: [String: SavedWorkSnapshot] = [:]
    private var invalidatedAt: [String: Date] = [:]

    func invalidate(store: URL, at date: Date = Date(), cacheRoot: URL? = nil) {
        let url = SavedWorkSnapshot.location(store: store, cacheRoot: cacheRoot)
        invalidatedAt[url.path] = date
        memory[url.path] = SavedWorkSnapshot(storePath: SavedWorkSnapshot.canonicalPath(store))
        try? FileManager.default.removeItem(at: url)
    }

    func record(path: String, data: Data, store: URL, receivedAt: Date = Date(), requestStartedAt: Date? = nil, cacheRoot: URL? = nil) {
        guard SavedWorkSnapshot.accepts(path), data.count <= 8_000_000,
              WorkProjectionMetadata.from(data)?.state != "pending",
              WorkProjectionMetadata.from(data)?.available != false else { return }
        let url = SavedWorkSnapshot.location(store: store, cacheRoot: cacheRoot)
        let key = url.path
        if let invalidated = invalidatedAt[key], (requestStartedAt ?? receivedAt) <= invalidated { return }
        var saved = memory[key] ?? SavedWorkSnapshot.load(store: store, cacheRoot: cacheRoot)
            ?? SavedWorkSnapshot(storePath: SavedWorkSnapshot.canonicalPath(store))
        let started = requestStartedAt ?? receivedAt
        if let old = saved.entries[path], (old.requestStartedAt ?? old.receivedAt) > started { return }
        // Avoid rewriting an unchanged large session every three seconds. The
        // persisted date remains the actual earlier response date, conservatively.
        if let old = saved.entries[path], old.data == data,
           receivedAt.timeIntervalSince(old.receivedAt) < 300 {
            var advanced = old
            advanced.requestStartedAt = started
            saved.entries[path] = advanced
            memory[key] = saved
            return
        }
        saved.entries[path] = .init(path: path, receivedAt: receivedAt, data: data, requestStartedAt: started)
        var candidates = saved.entries.values.filter { $0.path != "/v1/tasks?limit=200" }
            .sorted { $0.receivedAt < $1.receivedAt }
        var bytes = saved.entries.values.reduce(0) { $0 + $1.data.count }
        while (bytes > 24_000_000 || saved.entries.count > 256), !candidates.isEmpty {
            let removed = candidates.removeFirst()
            saved.entries.removeValue(forKey: removed.path)
            bytes -= removed.data.count
        }
        do {
            let directory = url.deletingLastPathComponent()
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            try JSONEncoder().encode(saved).write(to: url, options: [.atomic])
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
            memory[key] = saved
            if memory.count > 4, let other = memory.keys.first(where: { $0 != key }) { memory.removeValue(forKey: other) }
        } catch {
            // Saving review copies is best-effort and must not fail a live read.
        }
    }
}
