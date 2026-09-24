import XCTest
@testable import agentacct

/// The Usage page's filter row: the parameter assembly one filter change sends,
/// the options the three menus may offer, the wording of the results line, and
/// the two honest states — no matching rows, and a payload whose own echo does
/// not confirm the filter it was asked for.
final class UsageFilterTests: XCTestCase {
    // MARK: - Request assembly

    func testOneFilterChangeSendsEveryFieldAndOmitsTheDefaults() {
        let today = "2026-09-24"

        // The default filter is the exact path the page asked for before the
        // filter row existed.
        XCTAssertEqual(UsageFilter().summaryPath(today: today), "/usage/summary?days=7&granularity=daily")
        XCTAssertEqual(UsageFilter(range: .days(30)).summaryPath(today: today), "/usage/summary?days=30&granularity=daily")
        XCTAssertEqual(UsageFilter(range: .days(90)).summaryPath(today: today), "/usage/summary?days=90&granularity=daily")
        // All recorded days rides the cube's own preset and lets its locked
        // granularity rule bound the series (the echo names what it used).
        XCTAssertEqual(UsageFilter(range: .all).summaryPath(today: today), "/usage/summary?days=all")
        // Today is a one-day closed range: the preset whitelist has no "1".
        XCTAssertEqual(
            UsageFilter(range: .today).summaryPath(today: today),
            "/usage/summary?start=2026-09-24&end=2026-09-24"
        )

        // An explicit range sends start/end and never days, with the three list
        // filters in the bar's own order — and omitted entirely at "all".
        XCTAssertEqual(
            UsageFilter(range: .custom(start: "2026-08-01", end: "2026-09-01"), client: "codex", model: "gpt-5.5", provider: "openai").summaryPath(today: today),
            "/usage/summary?client=codex&model=gpt-5.5&provider=openai&start=2026-08-01&end=2026-09-01"
        )
        XCTAssertEqual(
            UsageFilter(range: .custom(start: "2026-08-01", end: "2026-09-01")).summaryPath(today: today),
            "/usage/summary?start=2026-08-01&end=2026-09-01"
        )
        // A model name with separators stays one value.
        XCTAssertEqual(
            UsageFilter(range: .days(7), model: "claude/opus:4").summaryPath(today: today),
            "/usage/summary?model=claude%2Fopus%3A4&days=7&granularity=daily"
        )
        // The options read is its own unfiltered call.
        XCTAssertEqual(UsageFilter.optionsPath, "/usage/summary?days=all")
    }

    // MARK: - Naming a range

    func testEveryRangeNamesItselfForEverySurfaceItAppearsOn() {
        let seven = UsageRangeLabel(range: .days(7))
        XCTAssertEqual(seven.short, "7d")
        XCTAssertEqual(seven.long, "last 7 days")
        XCTAssertEqual(seven.compact, "7d")
        XCTAssertEqual(seven.allDays, "All 7d")
        XCTAssertFalse(seven.isAllTime)

        let today = UsageRangeLabel(range: .today)
        XCTAssertEqual([today.short, today.menuFace, today.compact], ["Today", "Today", "Today"])
        XCTAssertEqual(today.long, "today")
        XCTAssertEqual(today.allDays, "All today")

        let all = UsageRangeLabel(range: .all)
        XCTAssertEqual(all.short, "All")
        XCTAssertEqual(all.long, "all recorded days")
        XCTAssertEqual(all.allDays, "All recorded days")
        XCTAssertTrue(all.isAllTime)

        let custom = UsageRangeLabel(range: .custom(start: "2026-08-01", end: "2026-09-01"))
        XCTAssertEqual(custom.short, "2026-08-01 – 2026-09-01")
        XCTAssertEqual(custom.long, "2026-08-01 to 2026-09-01")
        XCTAssertEqual(custom.menuFace, "Custom range")
        XCTAssertEqual(custom.compact, "custom range")
        XCTAssertEqual(custom.allDays, "All days in range")
        XCTAssertFalse(custom.isAllTime)
    }

    func testPlanWindowIsItsOwnBoundAndNotTheUsageRange() {
        XCTAssertEqual(UsageFilter(range: .days(7)).planDays(), 7)
        XCTAssertEqual(UsageFilter(range: .days(30)).planDays(), 30)
        XCTAssertEqual(UsageFilter(range: .days(90)).planDays(), 90)
        XCTAssertEqual(UsageFilter(range: .today).planDays(), 1)
        // /v1/plan accepts 1...90 days: an open-ended or wider filter asks for
        // its maximum, and the plan labels print that number.
        XCTAssertEqual(UsageFilter(range: .all).planDays(), 90)
        XCTAssertEqual(UsageFilter(range: .custom(start: "2026-08-01", end: "2026-08-14")).planDays(), 14)
        XCTAssertEqual(UsageFilter(range: .custom(start: "2026-01-01", end: "2026-09-01")).planDays(), 90)

        XCTAssertFalse(UsageDateFilter.all.fitsPlanWindow)
        XCTAssertTrue(UsageDateFilter.days(30).fitsPlanWindow)
        XCTAssertTrue(UsageDateFilter.today.fitsPlanWindow)
        XCTAssertTrue(UsageDateFilter.custom(start: "2026-08-01", end: "2026-09-01").fitsPlanWindow)
        XCTAssertFalse(UsageDateFilter.custom(start: "2026-01-01", end: "2026-09-01").fitsPlanWindow)
    }

    // MARK: - What the results area says it is showing

    func testResultsLineNamesTheFiltersInTheBarsOwnOrder() throws {
        let filter = UsageFilter(range: .days(7), client: "codex", model: "gpt-5.5")
        XCTAssertEqual(UsageFilterSummary.build(filter: filter, echo: nil).text, "7d · codex · gpt-5.5")

        XCTAssertEqual(UsageFilterSummary.build(filter: UsageFilter(), echo: nil).text, "7d")
        XCTAssertEqual(UsageFilterSummary.build(filter: UsageFilter(range: .all), echo: nil).text, "All")
        XCTAssertEqual(
            UsageFilterSummary.build(
                filter: UsageFilter(range: .days(30), client: "hermes", model: "gpt-5.4-mini", provider: "openai"),
                echo: nil
            ).text,
            "30d · hermes · gpt-5.4-mini · openai"
        )
    }

    func testAnExplicitRangeShowsTheWindowTheDaemonResolved() throws {
        let filter = UsageFilter(range: .custom(start: "2026-08-01", end: "2026-09-01"), client: "codex")
        let resolved = try decodeEcho("""
        {"client":"codex","range_mode":"explicit","resolved_start":"2026-08-02","resolved_end":"2026-09-01"}
        """)

        // The daemon's own window wins: a shifted or clamped resolution is
        // named, never papered over with the requested dates.
        XCTAssertEqual(UsageFilterSummary.build(filter: filter, echo: resolved).text, "2026-08-02 – 2026-09-01 · codex")
        // Without one, the request's own dates are named.
        XCTAssertEqual(UsageFilterSummary.build(filter: filter, echo: nil).text, "2026-08-01 – 2026-09-01 · codex")
        XCTAssertEqual(resolved.resolvedRange, "2026-08-02 – 2026-09-01")
        // A preset keeps its preset name; the echo's window rides in the help.
        XCTAssertEqual(
            UsageFilterSummary.build(filter: UsageFilter(range: .days(7)), echo: resolved).text,
            "7d"
        )
    }

    // MARK: - The empty result

    func testUnknownModelOrProviderIsNamedAsAbsentInsteadOfGuessed() throws {
        let unknownModel = try decodeEcho("""
        {"model":"gpt-9","model_matches_saved_rows":false}
        """)
        let modelState = UsageFilterEmptyState.build(
            filter: UsageFilter(range: .days(30), client: "codex", model: "gpt-9"),
            echo: unknownModel
        )
        XCTAssertEqual(modelState.title, "No saved rows match these filters")
        XCTAssertTrue(modelState.detail.contains("No saved usage row carries the model “gpt-9”"))
        XCTAssertTrue(modelState.detail.contains("instead of guessing"))

        let unknownProvider = try decodeEcho("""
        {"provider":"not-a-provider","provider_matches_saved_rows":false}
        """)
        let providerState = UsageFilterEmptyState.build(
            filter: UsageFilter(range: .all, provider: "not-a-provider"),
            echo: unknownProvider
        )
        XCTAssertEqual(providerState.title, "No saved rows match these filters")
        XCTAssertTrue(providerState.detail.contains("No saved usage row carries the provider “not-a-provider”"))
    }

    func testEmptyCopyDistinguishesAnEmptyLedgerFromAnEmptyFilterMatch() throws {
        // No filters at all: the ledger itself holds nothing in this range.
        let unfiltered = UsageFilterEmptyState.build(filter: UsageFilter(range: .all), echo: nil)
        XCTAssertEqual(unfiltered.title, "No saved usage rows in this range")
        XCTAssertTrue(unfiltered.detail.contains("all recorded days"))

        // A narrowed filter: the combination matched nothing, and the copy
        // repeats the filter it matched nothing against.
        let narrowed = UsageFilterEmptyState.build(
            filter: UsageFilter(range: .days(7), client: "codex", model: "gpt-5.5"),
            echo: nil
        )
        XCTAssertEqual(narrowed.title, "No saved rows match these filters")
        XCTAssertTrue(narrowed.detail.contains("7d · codex · gpt-5.5"))
    }

    func testAnEmptyPayloadIsRecognizedWhileAHeldRowIsNot() throws {
        XCTAssertTrue(try Self.summary().hasNoSavedRows)
        // A held row is still a row: it renders its own honest "—".
        XCTAssertFalse(try Self.summary(byClient: #"{"client":"held-agent","rows":1,"usage_availability":"held"}"#).hasNoSavedRows)
        // A payload whose client lane is absent but whose periods hold rows is
        // not empty either.
        XCTAssertFalse(try Self.summary(byPeriod: #"{"period":"2026-09-01","rows":1,"fresh_tokens":10}"#).hasNoSavedRows)
        XCTAssertFalse(try Self.summary(totals: #"{"rows":1}"#).hasNoSavedRows)
    }

    // MARK: - The disclosure guard

    func testAPayloadThatDoesNotConfirmTheFilterIsNeverShownAsFiltered() throws {
        let filter = UsageFilter(range: .days(30), client: "codex", provider: "openai")

        // A daemon that ignores `provider` echoes no provider field: the pane
        // must hide the numbers rather than pass them off as filtered.
        let ignoring = try decodeEcho(#"{"client":"codex","days":"30"}"#)
        let mismatch = try XCTUnwrap(UsageFilterMismatch.evaluate(filter: filter, echo: ignoring))
        XCTAssertEqual(mismatch.unconfirmed, ["the provider filter"])
        XCTAssertTrue(mismatch.detail.contains("does not confirm the provider filter,"))
        XCTAssertTrue(mismatch.detail.contains("hidden"))

        // An explicit range with no resolved window is the same class of gap.
        let noResolvedWindow = try decodeEcho(#"{"client":"codex","provider":"openai"}"#)
        let rangeMismatch = try XCTUnwrap(UsageFilterMismatch.evaluate(
            filter: UsageFilter(range: .custom(start: "2026-08-01", end: "2026-09-01"), client: "codex", provider: "openai"),
            echo: noResolvedWindow
        ))
        XCTAssertEqual(rangeMismatch.unconfirmed, ["the resolved date range"])

        // Three unconfirmed fields read as a list, not a run-on.
        let several = try XCTUnwrap(UsageFilterMismatch.evaluate(
            filter: UsageFilter(range: .custom(start: "2026-08-01", end: "2026-09-01"), client: "codex", model: "gpt-5.5", provider: "openai"),
            echo: try decodeEcho(#"{"days":"30"}"#)
        ))
        XCTAssertEqual(several.unconfirmed, [
            "the agent filter", "the model filter", "the provider filter", "the resolved date range",
        ])
        XCTAssertTrue(several.detail.contains("the agent filter, the model filter, the provider filter and the resolved date range"))

        // A preset that resolved to its own window is clean; a window the
        // daemon reports as a different length than "7d" claims is not.
        XCTAssertNil(UsageFilterMismatch.evaluate(filter: UsageFilter(range: .days(7)), echo: try decodeEcho(
            #"{"days":"7","range_mode":"days","resolved_start":"2026-09-18","resolved_end":"2026-09-24"}"#
        )))
        XCTAssertEqual(
            UsageFilterMismatch.evaluate(filter: UsageFilter(range: .days(7)), echo: try decodeEcho(
                #"{"days":"7","range_mode":"days","resolved_start":"2026-08-26","resolved_end":"2026-09-24"}"#
            ))?.unconfirmed,
            ["the resolved date range"]
        )

        // A confirmed filter is clean, and so is an old daemon answering an
        // unfiltered preset request: nothing was asked of it that it must echo.
        XCTAssertNil(UsageFilterMismatch.evaluate(filter: filter, echo: try decodeEcho(
            #"{"client":"codex","provider":"openai","days":"30"}"#
        )))
        XCTAssertNil(UsageFilterMismatch.evaluate(filter: UsageFilter(), echo: try decodeEcho(
            #"{"granularity":"daily","days":"7","granularity_requested":"auto"}"#
        )))
        XCTAssertNil(UsageFilterMismatch.evaluate(
            filter: UsageFilter(range: .custom(start: "2026-08-01", end: "2026-09-01")),
            echo: try decodeEcho(#"{"range_mode":"explicit","resolved_start":"2026-08-01","resolved_end":"2026-09-01"}"#)
        ))
    }

    // MARK: - The menus' options

    func testOptionsComeFromTheUnfilteredPayloadNotTheFilteredOne() throws {
        let unfiltered = try Self.summary(
            byClient: #"{"client":"codex"},{"client":"claude-code"},{"client":"hermes"}"#,
            byModel: """
            {"client":"codex","model":"gpt-5.5","provider":"openai"},
            {"client":"claude-code","model":"claude-opus-4-6","provider":"anthropic"},
            {"client":"hermes","model":"gpt-5.4-mini","provider":"openai"}
            """
        )
        let options = UsageFilterOptions.build(unfiltered)

        XCTAssertEqual(options.clients, ["claude-code", "codex", "hermes"])
        XCTAssertEqual(options.models, ["claude-opus-4-6", "gpt-5.4-mini", "gpt-5.5"])
        XCTAssertEqual(options.providers, ["anthropic", "openai"])

        // What the filtered page payload would offer for the same menu: one
        // client. This is why the menus read the unfiltered read instead.
        let filtered = try Self.summary(byClient: #"{"client":"codex"}"#)
        XCTAssertEqual(UsageFilterOptions.build(filtered).clients, ["codex"])

        // A committed value the option payload does not carry still renders as
        // the selected row instead of blanking the control.
        XCTAssertEqual(options.clients(keeping: "a-new-agent"), ["a-new-agent", "claude-code", "codex", "hermes"])
        XCTAssertEqual(options.clients(keeping: UsageFilter.anyValue), ["claude-code", "codex", "hermes"])
        XCTAssertEqual(UsageFilterOptions.build(filtered).models(keeping: "gpt-5.5"), ["gpt-5.5"])
        // An empty lane yields no options: nothing is invented to fill it.
        XCTAssertEqual(UsageFilterOptions.build(try Self.summary()).providers, [])
    }

    @MainActor
    func testFixtureStoreWiresTheMenusFromItsOwnPayload() throws {
        let fixture = try XCTUnwrap(DashboardSnapshotFixture.load(from: XCTUnwrap(
            Bundle.module.url(forResource: "dashboard", withExtension: "json")
        )))
        let store = DashboardStore(preloaded: fixture)

        XCTAssertEqual(store.usageFilter, UsageFilter(range: .days(7)))
        XCTAssertEqual(store.usageRangeLabel.long, "last 7 days")
        XCTAssertEqual(store.usagePlanDays, 7)
        XCTAssertEqual(store.usageFilterOptions?.clients, ["claude-code", "codex", "hermes"])
        XCTAssertEqual(store.usageFilterOptions?.models, ["claude-opus-4-6", "gpt-5.5", "hermes-model"])
        // The fixture's model rows report no provider, so the menu offers none.
        XCTAssertEqual(store.usageFilterOptions?.providers, [])
        // The fixture's own echo confirms what was asked of it.
        XCTAssertNil(store.usageFilterMismatch)
    }

    // MARK: - One change, one request

    @MainActor
    func testAFilterChangeRequestsTheNewFilterAndKeepsTheOthers() async throws {
        let store = try fixtureStore()
        var paths: [String] = []
        var planDays: [Int] = []
        store.usageReaders = UsageReaders(
            summary: { path in
                paths.append(path)
                return try Self.summary(byClient: #"{"client":"codex"}"#)
            },
            plan: { days in
                planDays.append(days)
                return []
            },
            options: { throw GlanceClientError.transport("a filter change reads no options") }
        )

        await store.setUsageFilter(UsageFilter(range: .days(7), client: "codex", model: "gpt-5.5", provider: "openai"))
        // Only the range moves: the three list filters travel with it untouched,
        // each request carrying all four.
        await store.setUsageFilter(UsageFilter(range: .days(30), client: "codex", model: "gpt-5.5", provider: "openai"))

        XCTAssertEqual(paths, [
            "/usage/summary?client=codex&model=gpt-5.5&provider=openai&days=7&granularity=daily",
            "/usage/summary?client=codex&model=gpt-5.5&provider=openai&days=30&granularity=daily",
        ])
        XCTAssertEqual(planDays, [7, 30])
        XCTAssertEqual(store.usageFilter.client, "codex")
        XCTAssertEqual(store.usageFilter.model, "gpt-5.5")
        XCTAssertEqual(store.usageFilter.provider, "openai")
        XCTAssertEqual(store.usage?.byClient.map(\.client), ["codex"])
        XCTAssertNil(store.errorText)
    }

    @MainActor
    func testAFailedFilterChangeKeepsTheOldFilterAndItsDataPaired() async throws {
        let store = try fixtureStore()
        store.usageReaders = UsageReaders(
            summary: { _ in throw GlanceClientError.http(422) },
            plan: { _ in [] },
            options: { throw GlanceClientError.transport("unused") }
        )

        await store.setUsageFilter(UsageFilter(range: .days(30)))

        XCTAssertEqual(store.usageFilter.range, .days(7))
        XCTAssertEqual(store.usage?.byClient.map(\.client), ["codex", "claude-code", "hermes"])
        XCTAssertEqual(store.errorText?.contains("usage range fetch failed"), true)
    }

    @MainActor
    func testASupersededResponseNeverOverwritesTheNewestFilter() async throws {
        let store = try fixtureStore()
        let gate = UsageRequestGate()
        store.usageReaders = UsageReaders(
            summary: { path in
                if path.contains("days=90") {
                    await gate.hold()
                    return try Self.summary(byClient: #"{"client":"slow-90d"}"#)
                }
                return try Self.summary(byClient: #"{"client":"newest"}"#)
            },
            plan: { _ in [] },
            options: { throw GlanceClientError.transport("unused") }
        )

        let slow = Task { await store.setUsageFilter(UsageFilter(range: .days(90))) }
        await gate.waitUntilHeld()
        // The reader switches again while the 90d read is still in flight.
        await store.setUsageFilter(UsageFilter(range: .days(30)))
        await gate.release()
        await slow.value

        XCTAssertEqual(store.usageFilter.range, .days(30))
        XCTAssertEqual(store.usage?.byClient.map(\.client), ["newest"])
    }

    @MainActor
    func testMenuOptionsComeFromTheUnfilteredReadNotThePagesOwnPayload() async throws {
        let store = try fixtureStore()
        var optionReads = 0
        store.usageReaders = UsageReaders(
            // The page's own payload narrows to one client; the option read
            // stays unfiltered, which is the whole point of holding it apart.
            summary: { _ in try Self.summary(byClient: #"{"client":"codex","provider":"openai"}"#) },
            plan: { _ in [] },
            options: {
                optionReads += 1
                return try Self.summary(
                    byClient: #"{"client":"codex"},{"client":"claude-code"}"#,
                    byModel: #"{"client":"codex","model":"gpt-5.5","provider":"openai"}"#
                )
            }
        )

        await store.refreshUsageFilterOptions()
        XCTAssertEqual(optionReads, 1)
        XCTAssertEqual(store.usageFilterOptions?.clients, ["claude-code", "codex"])
        XCTAssertEqual(store.usageFilterOptions?.models, ["gpt-5.5"])
        XCTAssertEqual(store.usageFilterOptions?.providers, ["openai"])

        // A filter change reads the page's payload only: the option set is
        // never re-derived from the response the filter just narrowed.
        await store.setUsageFilter(UsageFilter(range: .days(30), client: "codex"))
        XCTAssertEqual(optionReads, 1)
        XCTAssertEqual(store.usageFilterOptions?.clients, ["claude-code", "codex"])
        XCTAssertEqual(store.usage?.byClient.map(\.client), ["codex"])
    }

    @MainActor
    func testAFailedOptionReadRetainsTheLastOptionSet() async throws {
        let store = try fixtureStore()
        store.usageReaders = UsageReaders(
            summary: { _ in try Self.summary() },
            plan: { _ in [] },
            options: { throw GlanceClientError.transport("option read failed") }
        )

        await store.refreshUsageFilterOptions()

        // The menus keep the choices they had rather than emptying themselves
        // over a transient read failure.
        XCTAssertEqual(store.usageFilterOptions?.clients, ["claude-code", "codex", "hermes"])
    }

    // MARK: - Helpers

    @MainActor
    private func fixtureStore() throws -> DashboardStore {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
        return DashboardStore(preloaded: try DashboardSnapshotFixture.load(from: url))
    }

    private func decodeEcho(_ json: String) throws -> UsageFiltersEcho {
        try JSONDecoder().decode(UsageFiltersEcho.self, from: Data(json.utf8))
    }
}

private extension UsageFilterTests {
    static func summary(
        byClient: String = "",
        byModel: String = "",
        byPeriod: String = "",
        totals: String = "null",
        echo: String = "null"
    ) throws -> UsageSummary {
        let json = """
        {
          "by_client": [\(byClient)],
          "by_model": [\(byModel)],
          "by_period": [\(byPeriod)],
          "totals": \(totals),
          "filters_echo": \(echo)
        }
        """
        return try JSONDecoder().decode(UsageSummary.self, from: Data(json.utf8))
    }
}

/// Holds one scripted usage read open so a test can prove the page's staleness
/// rule: the newest filter wins even when an older read lands afterwards.
private actor UsageRequestGate {
    private var held = false
    private var enteredWaiters: [CheckedContinuation<Void, Never>] = []
    private var released = false
    private var releaseContinuation: CheckedContinuation<Void, Never>?

    func hold() async {
        held = true
        enteredWaiters.forEach { $0.resume() }
        enteredWaiters = []
        guard !released else { return }
        await withCheckedContinuation { releaseContinuation = $0 }
    }

    func waitUntilHeld() async {
        guard !held else { return }
        await withCheckedContinuation { enteredWaiters.append($0) }
    }

    func release() {
        released = true
        releaseContinuation?.resume()
        releaseContinuation = nil
    }
}
