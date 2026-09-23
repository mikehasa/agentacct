import Foundation
import XCTest
@testable import agentacct

final class WorkProjectionTests: XCTestCase {
    func testSnapshotHeaderOnlyOptsInEligibleGetsWithoutChangingURL() throws {
        for path in ["/v1/tasks?limit=200", "/v1/receipt?task=a%26b", "/v1/session?client=codex&session_id=x", "/v1/sessions?limit=50", "/v1/task-timeline?task=a&cursor=opaque", "/v1/attention?limit=5"] {
            let url = try XCTUnwrap(URL(string: "http://127.0.0.1:4444" + path))
            let request = GlanceClient.authenticatedGetRequest(url: url, path: path, token: "test")
            XCTAssertEqual(request.url, url)
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(request.value(forHTTPHeaderField: "X-Agentacct-Read-Mode"), "snapshot")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer test")
        }
        for path in ["/v1/glance", "/v1/plan?days=7", "/v1/tasks-other", "/v1/disposition"] {
            XCTAssertFalse(GlanceClient.usesSnapshotReadMode(path))
        }
    }

    func testPendingIsPreparationNotAnEmptySuccessOrTransportFailure() throws {
        let data = Data(#"{"detail":"Preparing work receipts","projection":{"state":"pending","built_at":null,"generation":null,"error":null}}"#.utf8)
        do {
            let _: ReceiptTasksPayload = try GlanceClient.decodeGetPayload(data, statusCode: 202, path: "/v1/tasks?limit=200")
            XCTFail("A first build must never publish an empty successful collection")
        } catch let pending as WorkProjectionPending {
            XCTAssertEqual(pending.projection, .pending)
            XCTAssertEqual(pending.localizedDescription, "Preparing work receipts")
            XCTAssertTrue(pending.projection.needsRefresh)
            XCTAssertNil(pending.projection.builtDate)
        } catch { XCTFail("Pending was misclassified: \(error)") }
    }

    func testAllReadModelsDecodeOptionalMetadataAndKeepLegacyPayloadsCompatible() throws {
        let fixtureURL = try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL)
        XCTAssertNil(fixture.tasks.projection)
        XCTAssertNil(fixture.attention.projection)
        XCTAssertNil(fixture.work?.receipt.projection)
        XCTAssertNil(fixture.work?.sessions.first?.projection)
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: fixtureURL)) as? [String: Any])
        let metadata: [String: Any] = ["state": "updating", "available": true, "built_at": 1234, "generation": "generation-a"]
        for key in ["tasks", "attention"] {
            var payload = try XCTUnwrap(object[key] as? [String: Any])
            payload["projection"] = metadata
            object[key] = payload
        }
        var work = try XCTUnwrap(object["work"] as? [String: Any])
        var receipt = try XCTUnwrap(work["receipt"] as? [String: Any])
        receipt["projection"] = metadata
        work["receipt"] = receipt
        var sessions = try XCTUnwrap(work["sessions"] as? [[String: Any]])
        for index in sessions.indices { sessions[index]["projection"] = metadata }
        work["sessions"] = sessions
        object["work"] = work
        let projected = try DashboardSnapshotFixture.decode(JSONSerialization.data(withJSONObject: object))
        for projection in [projected.tasks.projection, projected.attention.projection, projected.work?.receipt.projection,
                           projected.work?.sessions.first?.projection] {
            XCTAssertEqual(projection?.generation, "generation-a")
            XCTAssertEqual(projection?.builtAt, 1234)
        }
        let index = try JSONDecoder().decode(V1SessionsPayload.self, from: JSONSerialization.data(withJSONObject:
            ["schema": "agentacct.v1-sessions.v1", "sessions": [], "projection": metadata]))
        XCTAssertEqual(index.projection?.generation, "generation-a")
        let data = Data(#"{"schema":"agentacct.tasks.v1","tasks":[],"projection":{"state":"updating","built_at":1234,"generation":"generation-a","error":null}}"#.utf8)
        let tasks: ReceiptTasksPayload = try GlanceClient.decodeGetPayload(data, statusCode: 200, path: "/v1/tasks?limit=200")
        XCTAssertEqual(tasks.projection?.builtDate, Date(timeIntervalSince1970: 1234))
        XCTAssertEqual(tasks.projection?.generation, "generation-a")
        XCTAssertEqual(tasks.projection?.statusText, "Updating work receipts")
        XCTAssertTrue(try XCTUnwrap(tasks.projection).needsRefresh)
    }

    func testSavedFreshnessUsesBuildTimeAndPendingCannotOverwriteLastGoodCopy() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let cache = SavedWorkCache()
        let path = "/v1/tasks?limit=200"
        let data = Data(#"{"schema":"agentacct.tasks.v1","tasks":[],"projection":{"state":"updating","built_at":100,"generation":"g1"}}"#.utf8)
        await cache.record(path: path, data: data, store: root, receivedAt: Date(timeIntervalSince1970: 900), cacheRoot: root)
        let saved = try XCTUnwrap(SavedWorkSnapshot.load(store: root, cacheRoot: root))
        XCTAssertEqual(saved.collectionDate, Date(timeIntervalSince1970: 100))
        XCTAssertEqual(saved.entries[path]?.receivedAt, Date(timeIntervalSince1970: 900))
        let pending = Data(#"{"projection":{"state":"pending","built_at":null}}"#.utf8)
        await cache.record(path: path, data: pending, store: root, receivedAt: Date(timeIntervalSince1970: 1000), cacheRoot: root)
        XCTAssertEqual(SavedWorkSnapshot.load(store: root, cacheRoot: root)?.entries[path]?.data, data)
    }

    func testUnknownBuildTimeNeverFallsBackToReceivedNow() {
        let date = Date(timeIntervalSince1970: 900)
        let projected = SavedWorkSnapshot.Entry(path: "/v1/tasks?limit=200", receivedAt: date,
            data: Data(#"{"projection":{"state":"updating","built_at":null}}"#.utf8))
        XCTAssertNil(projected.evidenceDate)
        let legacy = SavedWorkSnapshot.Entry(path: "/v1/tasks?limit=200", receivedAt: date, data: Data(#"{}"#.utf8))
        XCTAssertEqual(legacy.evidenceDate, date)
        XCTAssertFalse(WorkProjectionMetadata(state: "future-state", builtAt: nil, generation: nil, error: nil).isCurrent)
    }

    @MainActor func testPendingAndUpdatingPreserveHonestStoreStateAndDisableDispositions() throws {
        let fixtureURL = try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL)
        let fresh = DashboardStore(preloaded: fixture)
        XCTAssertTrue(fresh.canMutateReceipts)
        let pending = DashboardStore(preloaded: fixture, workState: .projectionPending)
        XCTAssertEqual(pending.receiptListProjection?.state, "pending")
        XCTAssertNil(pending.totalReceiptTasks)
        XCTAssertNil(pending.receiptListError)
        XCTAssertNil(pending.receiptError)
        XCTAssertNil(pending.attentionError)
        XCTAssertNil(pending.receiptListLastUpdated)
        XCTAssertNil(pending.lastUpdated)
        XCTAssertTrue(pending.projectedCollectionsNeedRefresh)
        XCTAssertFalse(pending.canMutateReceipts)
        let updating = DashboardStore(preloaded: fixture, workState: .projectionUpdating)
        XCTAssertEqual(updating.receiptTasks.count, fixture.tasks.tasks.count)
        XCTAssertNotNil(updating.receipt)
        XCTAssertNil(updating.receiptError)
        XCTAssertEqual(updating.receiptProjection?.builtAt, fixture.glance.generatedAt)
        XCTAssertTrue(updating.projectedCollectionsNeedRefresh)
        XCTAssertFalse(updating.canMutateReceipts)
    }
    func testUnavailableReadFailsClosedEvenOnHTTP200() throws {
        let data = Data(#"{"projection":{"state":"error","available":false,"built_at":null,"error":"retry"},"tasks":[]}"#.utf8)
        do {
            let _: ReceiptTasksPayload = try GlanceClient.decodeGetPayload(data, statusCode: 200, path: "/v1/tasks?limit=200")
            XCTFail("Unavailable data must never decode as a successful empty collection")
        } catch let pending as WorkProjectionPending {
            XCTAssertEqual(pending.projection.available, false)
            let old = WorkProjectionMetadata(state: "current", builtAt: 100, generation: "old", error: nil)
            XCTAssertNil(pending.projection.retainingBuild(from: old).builtDate)
        } catch { XCTFail("Wrong read state: \(error)") }
    }

    func testInvalidationDeletesSavedCopiesAndRejectsLateOldResponses() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let cache = SavedWorkCache()
        let path = "/v1/receipt?task=a"
        let data = Data(#"{"projection":{"state":"current","available":true,"built_at":100}}"#.utf8)
        await cache.record(path: path, data: data, store: root, receivedAt: Date(timeIntervalSince1970: 110), cacheRoot: root)
        XCTAssertNotNil(SavedWorkSnapshot.load(store: root, cacheRoot: root))
        await cache.invalidate(store: root, at: Date(timeIntervalSince1970: 150), cacheRoot: root)
        XCTAssertNil(SavedWorkSnapshot.load(store: root, cacheRoot: root))
        await cache.record(path: path, data: data, store: root, receivedAt: Date(timeIntervalSince1970: 200),
                           requestStartedAt: Date(timeIntervalSince1970: 120), cacheRoot: root)
        XCTAssertNil(SavedWorkSnapshot.load(store: root, cacheRoot: root))
        await cache.record(path: path, data: data, store: root, receivedAt: Date(timeIntervalSince1970: 220),
                           requestStartedAt: Date(timeIntervalSince1970: 210), cacheRoot: root)
        XCTAssertNotNil(SavedWorkSnapshot.load(store: root, cacheRoot: root))
    }

    @MainActor func testUnavailableGenerationClearsVisibleWorkAndBlocksDisposition() async throws {
        let fixtureURL = try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL)
        let store = DashboardStore(preloaded: fixture)
        XCTAssertFalse(store.receiptTasks.isEmpty)
        XCTAssertNotNil(store.receipt)
        var unavailable = WorkProjectionMetadata.pending
        unavailable.available = false
        store.invalidateProjectedWork(unavailable)
        XCTAssertTrue(store.receiptTasks.isEmpty)
        XCTAssertNil(store.receipt)
        XCTAssertNil(store.attention)
        XCTAssertTrue(store.preloadedSessions.isEmpty)
        XCTAssertNil(store.lastUpdated)
        XCTAssertFalse(store.canMutateReceipts)
        XCTAssertEqual(store.projectionSafetyRevision, 1)
        do {
            try await store.postDisposition(kind: "blocker", action: "mark_reviewed", expectedRevision: 1, note: nil)
            XCTFail("Unsafe receipt mutation must be refused before network IO")
        } catch is WorkProjectionReadOnly {} catch { XCTFail("Wrong error: \(error)") }
    }

    func testPendingSessionLinkResolvesAfterCurrentCollectionArrives() throws {
        let fixtureURL = try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL)
        let task = try XCTUnwrap(fixture.tasks.tasks.first { $0.primaryRoot != nil })
        let sessionID = try XCTUnwrap(task.primaryRoot?.sessionKey)
        XCTAssertEqual(workSessionResolution(for: sessionID, in: [], projection: .pending), .pending(sessionID))
        let current = WorkProjectionMetadata(state: "current", builtAt: 100, generation: "first", error: nil)
        XCTAssertEqual(workSessionResolution(for: sessionID, in: fixture.tasks.tasks, projection: current), .task(task.taskId))
        XCTAssertEqual(workSessionResolution(for: "missing", in: fixture.tasks.tasks, projection: current), .unresolved("missing"))
        var updating = current
        updating.state = "updating"
        XCTAssertEqual(workSessionResolution(for: "missing", in: fixture.tasks.tasks, projection: updating), .pending("missing"))
    }

    @MainActor func testFailedProjectedRefreshKeepsEvidenceButDisablesDisposition() throws {
        let fixtureURL = try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: fixtureURL)) as? [String: Any])
        let metadata: [String: Any] = ["state": "current", "available": true, "built_at": 1234, "generation": "first"]
        for key in ["tasks", "attention"] {
            var payload = try XCTUnwrap(object[key] as? [String: Any])
            payload["projection"] = metadata
            object[key] = payload
        }
        var work = try XCTUnwrap(object["work"] as? [String: Any])
        var receipt = try XCTUnwrap(work["receipt"] as? [String: Any])
        receipt["projection"] = metadata
        work["receipt"] = receipt
        object["work"] = work
        let fixture = try DashboardSnapshotFixture.decode(JSONSerialization.data(withJSONObject: object))
        let store = DashboardStore(preloaded: fixture)
        XCTAssertTrue(store.canMutateReceipts)
        XCTAssertEqual(store.lastUpdated, Date(timeIntervalSince1970: 1234))
        store.recordReceiptListFailure("HTTP 503")
        store.recordAttentionFailure("connection failed")
        store.recordReceiptFailure("request timed out")
        XCTAssertEqual(store.receiptTasks.count, fixture.tasks.tasks.count)
        XCTAssertEqual(store.receipt?.taskId, fixture.work?.receipt.taskId)
        XCTAssertEqual(store.attention?.items.count, fixture.attention.items.count)
        XCTAssertFalse(store.canMutateReceipts)
        XCTAssertTrue(store.projectedCollectionsNeedRefresh)
        for projection in [store.receiptListProjection, store.attentionProjection, store.receiptProjection] {
            XCTAssertEqual(projection?.state, "error")
            XCTAssertEqual(projection?.builtAt, 1234)
            XCTAssertEqual(projection?.generation, "first")
            XCTAssertEqual(projection?.available, true)
        }
        let legacy = DashboardStore(preloaded: try DashboardSnapshotFixture.load(from: fixtureURL))
        legacy.recordReceiptListFailure("connection failed")
        XCTAssertNil(legacy.receiptListProjection)
    }

    @MainActor func testProjectionReviewStatesRender() throws {
        let fixtureURL = try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL)
        let requested = ProcessInfo.processInfo.environment["AGENTACCT_PROJECTION_REVIEW_DIR"]
        let output = requested.map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { if requested == nil { try? FileManager.default.removeItem(at: output) } }
        let configs: [WorkSnapshotConfiguration] = [WorkSnapshotState.projectionPending, .projectionUpdating].flatMap { state in
            [WorkSnapshotConfiguration(state: state, viewport: "reference", width: 1120, height: 900, colorScheme: .light),
             WorkSnapshotConfiguration(state: state, viewport: "reference", width: 1120, height: 900, colorScheme: .dark)]
        }
        let rendered = try WorkSnapshotRenderer.render(fixture: fixture, outputDirectory: output, configurations: configs)
        XCTAssertEqual(rendered.count, 4)
    }

}
