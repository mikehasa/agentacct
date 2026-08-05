import Foundation

// Finds and talks to the local agentacct daemon.
//
// Discovery follows the documented reader contract (glance.py): the daemon
// claims `<store>/local-api.json` (0600) carrying the actual bound port and a
// per-boot bearer token; a missing/stale file or a dead port both mean
// "disconnected" (identical UX), and a 401 means the daemon restarted with a
// fresh token — re-read the file and retry once.

struct Discovery: Decodable {
    let schema: String
    let host: String?
    let port: Int
    let token: String
    let pid: Int?
    let version: String?
}

enum GlanceClientError: Error, CustomStringConvertible {
    case noDiscovery(String)
    case http(Int)
    case transport(String)
    case incompatible(daemonVersion: String, schema: String)

    var description: String {
        switch self {
        case .noDiscovery(let path):
            return "no discovery file at \(path)"
        case .http(let code):
            return "daemon answered HTTP \(code)"
        case .transport(let message):
            return message
        case .incompatible(let version, let schema):
            return "daemon \(version) serves \(schema)"
        }
    }
}

struct GlanceSnapshot {
    let glance: Glance
    let daemonVersion: String
}

final class GlanceClient {
    static let supportedGlanceSchema = "agentacct.glance.v1"

    private let session: URLSession

    init() {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 20
        session = URLSession(configuration: config)
    }

    static func storeDir() -> URL {
        let env = ProcessInfo.processInfo.environment
        if let override = env["AGENTACCT_STORE_DIR"], !override.isEmpty {
            return URL(fileURLWithPath: (override as NSString).expandingTildeInPath)
        }
        let home = FileManager.default.homeDirectoryForCurrentUser
        return home.appendingPathComponent(".local/state/agentacct/state")
    }

    static func discoveryPath() -> URL {
        storeDir().appendingPathComponent("local-api.json")
    }

    func loadDiscovery() throws -> Discovery {
        let path = Self.discoveryPath()
        guard let data = try? Data(contentsOf: path) else {
            throw GlanceClientError.noDiscovery(path.path)
        }
        guard let discovery = try? JSONDecoder().decode(Discovery.self, from: data),
              discovery.schema == "agentacct.local-api-discovery.v1"
        else {
            throw GlanceClientError.noDiscovery(path.path)
        }
        return discovery
    }

    func fetch() async throws -> GlanceSnapshot {
        do {
            return try await fetchOnce(discovery: loadDiscovery())
        } catch GlanceClientError.http(401) {
            // The daemon restarted with a fresh per-boot token: the reader
            // contract says re-read the discovery file and retry once.
            return try await fetchOnce(discovery: loadDiscovery())
        }
    }

    private func fetchOnce(discovery: Discovery) async throws -> GlanceSnapshot {
        let version: VersionInfo = try await get("/v1/version", discovery: discovery)
        guard version.glanceSchema == Self.supportedGlanceSchema else {
            // An incompatible daemon is a first-class state, never a parse error.
            throw GlanceClientError.incompatible(
                daemonVersion: version.version, schema: version.glanceSchema
            )
        }
        let glance: Glance = try await get("/v1/glance", discovery: discovery)
        return GlanceSnapshot(glance: glance, daemonVersion: version.version)
    }

    private func get<T: Decodable>(_ path: String, discovery: Discovery) async throws -> T {
        let host = discovery.host ?? "127.0.0.1"
        guard let url = URL(string: "http://\(host):\(discovery.port)\(path)") else {
            throw GlanceClientError.transport("bad daemon URL")
        }
        var request = URLRequest(url: url)
        request.setValue("Bearer \(discovery.token)", forHTTPHeaderField: "Authorization")
        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw GlanceClientError.transport(error.localizedDescription)
        }
        guard let http = response as? HTTPURLResponse else {
            throw GlanceClientError.transport("not an HTTP response")
        }
        guard http.statusCode == 200 else {
            throw GlanceClientError.http(http.statusCode)
        }
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw GlanceClientError.transport("payload decode failed: \(error.localizedDescription)")
        }
    }
}
