import SwiftUI
import XCTest
@testable import agentacct

/// The top bar's freshness indicator: the chip must render in every state, must
/// say which lane it is reporting, and must never invent a time it was not
/// given.
final class TopBarFreshnessTests: XCTestCase {
    private static let now = Date(timeIntervalSince1970: 1_000)

    @MainActor
    func testChipReportsBothLanesWithTheirOwnAges() {
        SnapshotMode.setFixtureDate(Self.now)
        defer { SnapshotMode.setFixtureDate(nil) }

        let freshness = TopBarFreshness(
            localData: Self.now.addingTimeInterval(-30),
            recordedUsage: Self.now.addingTimeInterval(-240)
        )

        XCTAssertEqual(freshness.summary, "Local data · 30s ago")
        XCTAssertEqual(freshness.localDataText, "30s ago")
        XCTAssertEqual(freshness.recordedUsageText, "4m ago")
        XCTAssertTrue(freshness.hasLocalData)
        XCTAssertTrue(freshness.help.contains("Local data last refreshed 30s ago"))
        XCTAssertTrue(freshness.help.contains("Recorded usage last read 4m ago"))
        XCTAssertEqual(
            freshness.accessibilityLabel,
            "Local data last refreshed 30s ago. Recorded usage last read 4m ago."
        )
    }

    /// The regression this indicator had: no stamp meant no indicator at all,
    /// so a reader could not tell a fresh window from one that never refreshed.
    @MainActor
    func testMissingStampsReadUnavailableInsteadOfZeroOrBlank() {
        SnapshotMode.setFixtureDate(Self.now)
        defer { SnapshotMode.setFixtureDate(nil) }

        let freshness = TopBarFreshness(localData: nil, recordedUsage: nil)

        XCTAssertEqual(freshness.summary, "Local data · time unavailable")
        XCTAssertFalse(freshness.summary.isEmpty)
        XCTAssertFalse(freshness.summary.contains("0s ago"))
        XCTAssertEqual(freshness.localDataText, TopBarFreshness.unavailableText)
        XCTAssertEqual(freshness.recordedUsageText, TopBarFreshness.unavailableText)
        XCTAssertFalse(freshness.hasLocalData)
        XCTAssertEqual(
            freshness.accessibilityLabel,
            "Local data last refreshed time unavailable. Recorded usage last read time unavailable."
        )
        XCTAssertTrue(freshness.help.contains("Local data refresh time unavailable"))
        XCTAssertTrue(freshness.help.contains("Recorded usage read time unavailable"))
    }

    /// One lane having no stamp must not silence or borrow the other's time.
    @MainActor
    func testOneLaneNeverBorrowsTheOthersTime() {
        SnapshotMode.setFixtureDate(Self.now)
        defer { SnapshotMode.setFixtureDate(nil) }

        let localOnly = TopBarFreshness(localData: Self.now.addingTimeInterval(-90), recordedUsage: nil)
        XCTAssertEqual(localOnly.summary, "Local data · 1m ago")
        XCTAssertEqual(localOnly.recordedUsageText, TopBarFreshness.unavailableText)
        XCTAssertFalse(localOnly.help.contains("Recorded usage last read 1m ago"))
        XCTAssertFalse(localOnly.accessibilityLabel.contains("Recorded usage last read 1m ago"))

        let usageOnly = TopBarFreshness(localData: nil, recordedUsage: Self.now.addingTimeInterval(-90))
        XCTAssertEqual(usageOnly.summary, "Local data · time unavailable")
        XCTAssertEqual(usageOnly.recordedUsageText, "1m ago")
        XCTAssertFalse(usageOnly.hasLocalData)
        XCTAssertFalse(usageOnly.accessibilityLabel.contains("Local data last refreshed 1m ago"))
    }

    /// A stamp ahead of the clock (skew between the recorder and this window)
    /// has no honest age: it reads unavailable, never "0s ago" and never a
    /// negative count — the same decision `agoText` already makes.
    @MainActor
    func testAFutureStampIsNeverReportedAsFresh() {
        SnapshotMode.setFixtureDate(Self.now)
        defer { SnapshotMode.setFixtureDate(nil) }

        let freshness = TopBarFreshness(
            localData: Self.now.addingTimeInterval(5),
            recordedUsage: Date(timeIntervalSince1970: 0)
        )

        XCTAssertEqual(freshness.localDataText, TopBarFreshness.unavailableText)
        XCTAssertEqual(freshness.recordedUsageText, TopBarFreshness.unavailableText)
        XCTAssertEqual(freshness.summary, "Local data · time unavailable")
        // A stamp that exists still earns the "received" dot; only its age is
        // unknown, and the text says so.
        XCTAssertTrue(freshness.hasLocalData)
    }

    /// The wiring the chip depends on: every store state yields a chip that can
    /// be rendered, and the recorded-usage lane is never silently dropped.
    @MainActor
    func testEveryStoreStateProducesARenderableChip() throws {
        let fixtureURL = try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL)
        let states: [SnapshotWorkStoreState] = [
            .populated, .projectionUpdating, .projectionPending, .listLoading, .listError, .empty,
        ]

        for state in states {
            let store = DashboardStore(preloaded: fixture, workState: state)
            let freshness = TopBarFreshness(
                localData: store.lastUpdated,
                recordedUsage: store.usageLastUpdated
            )
            XCTAssertFalse(freshness.summary.isEmpty, "\(state) rendered a blank freshness chip")
            XCTAssertNotNil(store.usageLastUpdated, "\(state) lost the recorded-usage stamp")
        }
    }

    /// A completed Task-list read always carries a stamp: metadata that omits
    /// its build time keeps the last build this window saw instead of claiming
    /// the collection is un-timed.
    @MainActor
    func testLocalDataStampPrefersTheDaemonsOwnBuildTime() {
        let readAt = Self.now
        let published = Date(timeIntervalSince1970: 900)
        let project = Date(timeIntervalSince1970: 1_234)

        XCTAssertEqual(
            DashboardStore.localDataStamp(
                projection: WorkProjectionMetadata(state: "current", builtAt: 900, generation: "g1", error: nil),
                publishedBuiltAt: nil,
                savedCollectionDate: nil,
                readAt: readAt
            ),
            published
        )
        XCTAssertEqual(
            DashboardStore.localDataStamp(
                projection: WorkProjectionMetadata(state: "updating", builtAt: nil, generation: nil, error: nil),
                publishedBuiltAt: project,
                savedCollectionDate: nil,
                readAt: readAt
            ),
            project
        )
        XCTAssertEqual(
            DashboardStore.localDataStamp(
                projection: nil,
                publishedBuiltAt: nil,
                savedCollectionDate: project,
                readAt: readAt
            ),
            project
        )
        XCTAssertEqual(
            DashboardStore.localDataStamp(
                projection: nil,
                publishedBuiltAt: nil,
                savedCollectionDate: nil,
                readAt: readAt
            ),
            readAt
        )
        XCTAssertNil(
            DashboardStore.localDataStamp(
                projection: WorkProjectionMetadata(state: "pending", builtAt: nil, generation: nil, error: nil),
                publishedBuiltAt: nil,
                savedCollectionDate: nil,
                readAt: readAt
            )
        )
    }

    /// The chip is drawn, not omitted, in both states — the property it lost
    /// when it was a bare `if let` in the toolbar.
    @MainActor
    func testIndicatorPaintsWithAndWithoutStamps() throws {
        SnapshotMode.setFixtureDate(Self.now)
        defer { SnapshotMode.setFixtureDate(nil) }
        let outputDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-topbar-freshness-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: outputDirectory) }

        let fresh = TopBarFreshness(
            localData: Self.now.addingTimeInterval(-30),
            recordedUsage: Self.now.addingTimeInterval(-30)
        )
        let renderedFresh = try renderChip(fresh, named: "fresh", in: outputDirectory)
        let renderedUnavailable = try renderChip(
            TopBarFreshness(localData: nil, recordedUsage: nil),
            named: "unavailable",
            in: outputDirectory
        )

        XCTAssertGreaterThan(renderedFresh.width * renderedFresh.height, 0)
        XCTAssertGreaterThan(renderedUnavailable.width * renderedUnavailable.height, 0)
        XCTAssertGreaterThan(inkedPixels(renderedFresh), 0)
        XCTAssertGreaterThan(inkedPixels(renderedUnavailable), 0)
        XCTAssertNotEqual(renderedFresh.rgba, renderedUnavailable.rgba)
    }

    @MainActor
    private func renderChip(
        _ freshness: TopBarFreshness,
        named name: String,
        in directory: URL
    ) throws -> VisualSnapshotImage {
        let view = TopBarFreshnessIndicator(freshness: freshness)
            .padding(6)
            .background(Color.white)
            .fixedSize(horizontal: true, vertical: true)
            .environment(\.colorScheme, .light)
            .environment(\.displayScale, 2)
            .environment(\.dynamicTypeSize, .medium)
            .transaction { transaction in transaction.disablesAnimations = true }
        let size = try SnapshotImageWriter.renderedSize(view)
        let outputURL = directory.appendingPathComponent("\(name).png")
        try SnapshotImageWriter.render(view, to: outputURL, size: size)
        return try VisualSnapshotImage(contentsOf: outputURL)
    }

    /// Anything that is not the white backing counts as drawn content.
    private func inkedPixels(_ image: VisualSnapshotImage) -> Int {
        stride(from: 0, to: image.rgba.count, by: 4).reduce(into: 0) { count, offset in
            let red = image.rgba[image.rgba.startIndex + offset]
            let green = image.rgba[image.rgba.startIndex + offset + 1]
            let blue = image.rgba[image.rgba.startIndex + offset + 2]
            if red < 240 || green < 240 || blue < 240 { count += 1 }
        }
    }
}
