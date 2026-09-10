import XCTest
import SQLite3
@testable import agentacct

final class GlanceClientStoreResolutionTests: XCTestCase {
    func testEachExplicitStoreAliasWins() throws {
        for name in [
            "AGENTACCT_STORE_DIR",
            "AGENT_CHRONICLE_STORE_DIR",
            "AGENT_SENTINEL_STORE_DIR",
        ] {
            let fixture = try StoreFixture()
            defer { fixture.remove() }
            try fixture.writeRecords(to: fixture.canonical)
            let override = fixture.root.appendingPathComponent("\(name)/state", isDirectory: true)

            XCTAssertEqual(try fixture.resolve([name: override.path]), override, name)
        }
    }

    func testOrdinaryStoreOverrideChangesGlanceButNotManagedGlobalTarget() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }
        try fixture.writeRecords(to: fixture.legacy)
        let displayOverride = fixture.root.appendingPathComponent("display/state", isDirectory: true)
        let environment = ["AGENTACCT_STORE_DIR": displayOverride.path]

        XCTAssertEqual(try fixture.resolve(environment), displayOverride)
        XCTAssertEqual(try fixture.resolveGlobal(environment), fixture.legacy)
    }

    func testGlobalAliasSelectsTheSameGlanceAndManagedRuntimeStore() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }
        let globalOverride = fixture.root.appendingPathComponent("managed/state", isDirectory: true)
        let environment = ["AGENT_CHRONICLE_GLOBAL_STORE_DIR": globalOverride.path]

        XCTAssertEqual(try fixture.resolve(environment), globalOverride)
        XCTAssertEqual(try fixture.resolveGlobal(environment), globalOverride)
    }

    func testEqualExplicitStoreAliasesAreAccepted() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }
        let override = fixture.root.appendingPathComponent("shared/state", isDirectory: true)

        XCTAssertEqual(
            try fixture.resolve([
                "AGENTACCT_STORE_DIR": "  \(override.path)  ",
                "AGENT_SENTINEL_STORE_DIR": override.path,
            ]),
            override
        )
    }

    func testConflictingExplicitStoreAliasesFailClosed() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }
        let first = fixture.root.appendingPathComponent("first", isDirectory: true)
        let second = fixture.root.appendingPathComponent("second", isDirectory: true)

        XCTAssertThrowsError(
            try fixture.resolve([
                "AGENTACCT_STORE_DIR": first.path,
                "AGENT_CHRONICLE_STORE_DIR": second.path,
            ])
        ) { error in
            guard case GlanceStoreResolutionError.conflictingStoreEnvironment(let values) = error else {
                return XCTFail("unexpected error: \(error)")
            }
            XCTAssertEqual(values, [
                "AGENTACCT_STORE_DIR=\(first.path)",
                "AGENT_CHRONICLE_STORE_DIR=\(second.path)",
            ])
        }
    }

    func testRelativeExplicitStoreAliasFailsInsteadOfFallingBack() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }
        try fixture.writeRecords(to: fixture.canonical)

        XCTAssertThrowsError(
            try fixture.resolve(["AGENT_SENTINEL_STORE_DIR": "relative/state"])
        ) { error in
            XCTAssertEqual(
                error as? GlanceStoreResolutionError,
                .relativeStoreEnvironment(
                    name: "AGENT_SENTINEL_STORE_DIR",
                    value: "relative/state"
                )
            )
        }
    }

    func testEqualGlobalAliasesAreAcceptedAndBlankAliasesAreIgnored() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }
        let shared = fixture.root.appendingPathComponent("shared/state", isDirectory: true)

        XCTAssertEqual(
            try fixture.resolve([
                "AGENTACCT_GLOBAL_STORE_DIR": "  \(shared.path)  ",
                "AGENT_CHRONICLE_GLOBAL_STORE_DIR": shared.path,
                "AGENT_SENTINEL_GLOBAL_STORE_DIR": "   ",
            ]),
            shared
        )
    }

    func testConflictingGlobalAliasesFailClosedBeforePathFiltering() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }
        let absolute = fixture.root.appendingPathComponent("operator/state", isDirectory: true)

        XCTAssertThrowsError(
            try fixture.resolve([
                "AGENTACCT_GLOBAL_STORE_DIR": "relative/state",
                "AGENT_SENTINEL_GLOBAL_STORE_DIR": absolute.path,
            ])
        ) { error in
            guard case GlanceStoreResolutionError.conflictingStoreEnvironment(let values) = error else {
                return XCTFail("unexpected error: \(error)")
            }
            XCTAssertEqual(values, [
                "AGENTACCT_GLOBAL_STORE_DIR=relative/state",
                "AGENT_SENTINEL_GLOBAL_STORE_DIR=\(absolute.path)",
            ])
        }
    }

    func testGlobalAliasUsesFirstNonBlankNameWhenOnlyOneIsSet() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }
        let chronicle = fixture.root.appendingPathComponent("chronicle/state", isDirectory: true)

        XCTAssertEqual(
            try fixture.resolve([
                "AGENTACCT_GLOBAL_STORE_DIR": "   ",
                "AGENT_CHRONICLE_GLOBAL_STORE_DIR": chronicle.path,
            ]),
            chronicle
        )
    }

    func testNonexistentGlobalOverrideDoesNotBeatExistingCanonicalRecords() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }
        try fixture.writeRecords(to: fixture.canonical)
        let nonexistent = fixture.root.appendingPathComponent("missing/state", isDirectory: true)

        XCTAssertEqual(
            try fixture.resolve(["AGENTACCT_GLOBAL_STORE_DIR": nonexistent.path]),
            fixture.canonical
        )
    }

    func testFreshGlobalOverrideIsCreationTargetWhenNoCandidateHasRecords() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }
        let override = fixture.root.appendingPathComponent("fresh-operator/state", isDirectory: true)

        XCTAssertEqual(
            try fixture.resolve(["AGENTACCT_GLOBAL_STORE_DIR": override.path]),
            override
        )
    }

    func testRelativeGlobalOverrideFailsClosed() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }
        try fixture.makeDirectory(fixture.legacy)

        XCTAssertThrowsError(
            try fixture.resolve(["AGENTACCT_GLOBAL_STORE_DIR": "relative/state"])
        ) { error in
            XCTAssertEqual(
                error as? GlanceStoreResolutionError,
                .relativeStoreEnvironment(
                    name: "AGENTACCT_GLOBAL_STORE_DIR",
                    value: "relative/state"
                )
            )
        }
    }

    func testAbsoluteXDGStateHomeDefinesCanonicalStore() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }
        let xdg = fixture.root.appendingPathComponent("xdg", isDirectory: true)
        let expected = xdg.appendingPathComponent("agentacct/state", isDirectory: true)

        XCTAssertEqual(try fixture.resolve(["XDG_STATE_HOME": xdg.path]), expected)
    }

    func testRelativeXDGStateHomeIsIgnored() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }

        XCTAssertEqual(
            try fixture.resolve(["XDG_STATE_HOME": "relative/state"]),
            fixture.canonical
        )
    }

    func testPopulatedLegacyBeatsEmptyCanonical() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }
        try fixture.makeDirectory(fixture.canonical)
        try fixture.writeRecords(to: fixture.legacy)

        XCTAssertEqual(try fixture.resolve(), fixture.legacy)
    }

    func testEmptyCanonicalSQLiteDoesNotHidePopulatedLegacy() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }
        try fixture.writeEmptySQLiteLedger(to: fixture.canonical)
        try fixture.writeRecords(to: fixture.legacy)

        XCTAssertEqual(try fixture.resolve(), fixture.legacy)
    }

    func testInvalidCanonicalSQLiteFailsClosedInsteadOfHidingIt() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }
        try fixture.makeDirectory(fixture.canonical)
        try "not sqlite".write(
            to: fixture.canonical.appendingPathComponent("events.sqlite3"),
            atomically: true,
            encoding: .utf8
        )
        try fixture.writeRecords(to: fixture.legacy)

        XCTAssertThrowsError(try fixture.resolve()) { error in
            guard case GlanceStoreResolutionError.invalidGlobalLedger(let path, _) = error else {
                return XCTFail("unexpected error: \(error)")
            }
            XCTAssertEqual(
                path,
                fixture.canonical.appendingPathComponent("events.sqlite3").path
            )
        }
    }

    func testCanonicalBeatsLegacyWhenBothHaveRecords() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }
        try fixture.writeRecords(to: fixture.canonical)
        try fixture.writeRecords(to: fixture.legacy)

        XCTAssertEqual(try fixture.resolve(), fixture.canonical)
    }

    func testEmptyCanonicalBeatsNonRecordLegacyDirectory() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }
        try fixture.makeDirectory(fixture.canonical)
        try fixture.makeDirectory(fixture.legacy)
        try "configured\n".write(
            to: fixture.legacy.appendingPathComponent("activation.json"),
            atomically: true,
            encoding: .utf8
        )

        XCTAssertEqual(try fixture.resolve(), fixture.canonical)
    }

    func testDiscoveryDoesNotCountAsLedgerRecords() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }
        try fixture.makeDirectory(fixture.canonical)
        try "{}\n".write(
            to: fixture.canonical.appendingPathComponent("local-api.json"),
            atomically: true,
            encoding: .utf8
        )
        try fixture.writeRecords(to: fixture.legacy)

        XCTAssertEqual(try fixture.resolve(), fixture.legacy)
    }

    func testOnlyEmptyLegacyDirectoryStillUsesFreshCanonical() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }
        try fixture.makeDirectory(fixture.legacy)

        XCTAssertEqual(try fixture.resolve(), fixture.canonical)
    }

    func testNoGlobalDirectoryUsesCanonicalAppFallback() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }

        XCTAssertEqual(try fixture.resolve(), fixture.canonical)
    }

    func testCommittedRecordsInLiveSQLiteWALKeepCanonicalStoreSelected() throws {
        let fixture = try StoreFixture()
        defer { fixture.remove() }
        try fixture.writeEmptySQLiteLedger(to: fixture.canonical)
        try fixture.writeRecords(to: fixture.legacy)
        var database: OpaquePointer?
        XCTAssertEqual(sqlite3_open(
            fixture.canonical.appendingPathComponent("events.sqlite3").path,
            &database
        ), SQLITE_OK)
        let connection = try XCTUnwrap(database)
        defer { sqlite3_close(connection) }
        XCTAssertEqual(sqlite3_exec(connection, """
            PRAGMA journal_mode=WAL;
            PRAGMA wal_autocheckpoint=0;
            INSERT INTO event_lines (line) VALUES ('record');
            """, nil, nil, nil), SQLITE_OK)
        let wal = fixture.canonical.appendingPathComponent("events.sqlite3-wal")
        let attributes = try FileManager.default.attributesOfItem(atPath: wal.path)
        XCTAssertGreaterThan((attributes[.size] as? NSNumber)?.intValue ?? 0, 0)

        XCTAssertEqual(try fixture.resolve(), fixture.canonical)
        XCTAssertEqual(try fixture.resolveGlobal(), fixture.canonical)
    }

    func testSymlinkedLedgerArtifactsMatchCLIStoreSelection() throws {
        for filename in ["events.jsonl", "events.sqlite3"] {
            let fixture = try StoreFixture()
            defer { fixture.remove() }
            let external = fixture.root.appendingPathComponent("external", isDirectory: true)
            try fixture.makeDirectory(fixture.canonical)
            try fixture.writeRecords(to: fixture.legacy)
            if filename == "events.jsonl" {
                try fixture.writeRecords(to: external)
            } else {
                try fixture.writeEmptySQLiteLedger(to: external)
                var database: OpaquePointer?
                XCTAssertEqual(sqlite3_open(
                    external.appendingPathComponent(filename).path, &database
                ), SQLITE_OK)
                let connection = try XCTUnwrap(database)
                defer { sqlite3_close(connection) }
                XCTAssertEqual(sqlite3_exec(
                    connection, "INSERT INTO event_lines (line) VALUES ('record')",
                    nil, nil, nil
                ), SQLITE_OK)
            }
            try FileManager.default.createSymbolicLink(
                at: fixture.canonical.appendingPathComponent(filename),
                withDestinationURL: external.appendingPathComponent(filename)
            )

            XCTAssertEqual(try fixture.resolve(), fixture.canonical, filename)
            XCTAssertEqual(try fixture.resolveGlobal(), fixture.canonical, filename)
        }
    }
}

private final class StoreFixture {
    private let fm = FileManager.default
    let root: URL
    let home: URL
    let canonical: URL
    let legacy: URL

    init() throws {
        root = fm.temporaryDirectory
            .appendingPathComponent("agentacct-store-resolution-\(UUID().uuidString)", isDirectory: true)
        home = root.appendingPathComponent("home", isDirectory: true)
        canonical = home.appendingPathComponent(".local/state/agentacct/state", isDirectory: true)
        legacy = home.appendingPathComponent(".agent-sentinel-global/state", isDirectory: true)
        try fm.createDirectory(at: home, withIntermediateDirectories: true)
    }

    func resolve(_ environment: [String: String] = [:]) throws -> URL {
        try GlanceClient.storeDir(environment: environment, home: home, fileManager: fm)
    }

    func resolveGlobal(_ environment: [String: String] = [:]) throws -> URL {
        try GlanceClient.globalStoreDir(environment: environment, home: home, fileManager: fm)
    }

    func makeDirectory(_ directory: URL) throws {
        try fm.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    func writeRecords(to store: URL) throws {
        try makeDirectory(store)
        try "record\n".write(
            to: store.appendingPathComponent("events.jsonl"),
            atomically: true,
            encoding: .utf8
        )
    }

    func writeEmptySQLiteLedger(to store: URL) throws {
        try makeDirectory(store)
        let path = store.appendingPathComponent("events.sqlite3").path
        var database: OpaquePointer?
        guard sqlite3_open(path, &database) == SQLITE_OK, let database else {
            if let database { sqlite3_close(database) }
            throw CocoaError(.fileWriteUnknown)
        }
        defer { sqlite3_close(database) }
        guard sqlite3_exec(
            database,
            "CREATE TABLE event_lines (seq INTEGER PRIMARY KEY, line TEXT NOT NULL)",
            nil,
            nil,
            nil
        ) == SQLITE_OK else {
            throw CocoaError(.fileWriteUnknown)
        }
    }

    func remove() {
        try? fm.removeItem(at: root)
    }
}
