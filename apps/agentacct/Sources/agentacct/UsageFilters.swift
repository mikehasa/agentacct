import Foundation

// MARK: - The filter value

/// The Usage page's date filter. The presets ride the cube's own whitelist
/// (`days=7|30|90|all`); Today and a custom interval go out as an explicit
/// closed range (`start=&end=`), which the daemon resolves and echoes back.
enum UsageDateFilter: Equatable, Hashable {
    case days(Int)
    case today
    case all
    case custom(start: String, end: String)

    /// True for the two forms that travel as `start=&end=`.
    var isExplicit: Bool {
        switch self {
        case .today, .custom: return true
        case .days, .all: return false
        }
    }

    /// True when the plan lane's own window (1...90 days) is this same window.
    /// The two only diverge for an open-ended range or one wider than the plan
    /// endpoint accepts — the About copy names that difference instead of
    /// implying the plan numbers cover the whole usage range.
    var fitsPlanWindow: Bool {
        switch self {
        case .days(let days): return (1...UsageFilter.planDayLimit).contains(days)
        case .today: return true
        case .all: return false
        case .custom(let start, let end):
            guard let span = UsageDayStamp.span(start, end) else { return false }
            return span <= UsageFilter.planDayLimit
        }
    }
}

/// The four filters the Usage page applies, each one independent: the recorded
/// range plus one value per agent/model/provider lane. They combine with AND
/// (the daemon's own rule), and changing one never touches another.
struct UsageFilter: Equatable, Hashable {
    /// The value a list filter carries when it narrows nothing. It is also the
    /// daemon's parameter default, so a filter at `all` is omitted from the
    /// request instead of being sent as a no-op.
    static let anyValue = "all"

    var range: UsageDateFilter = .days(7)
    var client = UsageFilter.anyValue
    var model = UsageFilter.anyValue
    var provider = UsageFilter.anyValue

    /// True when at least one of the three list filters narrows the rows.
    var narrowsAnything: Bool {
        client != Self.anyValue || model != Self.anyValue || provider != Self.anyValue
    }

    /// The unfiltered `/usage/summary` read the three list menus draw their
    /// options from: every saved row, all time, no client/model/provider. Held
    /// apart from the filtered read on purpose — a menu that only offered what
    /// the current filter left standing would erase the other choices.
    static let optionsPath = "/usage/summary?days=all"

    /// The `/usage/summary` path for this filter. The three list filters are
    /// omitted at their default so an unfiltered request stays the exact path
    /// the page always asked for; an explicit range sends `start`/`end` and
    /// never `days`.
    func summaryPath(today: String = UsageDayStamp.today()) -> String {
        var items: [String] = []
        if client != Self.anyValue { items.append("client=\(Self.encoded(client))") }
        if model != Self.anyValue { items.append("model=\(Self.encoded(model))") }
        if provider != Self.anyValue { items.append("provider=\(Self.encoded(provider))") }
        switch range {
        case .days(let days):
            items.append("days=\(days)")
            items.append("granularity=daily")
        case .all:
            // The cube's own locked granularity rule bounds an all-time series
            // (weekly); the payload's echo names what was used.
            items.append("days=all")
        case .today:
            items.append("start=\(Self.encoded(today))")
            items.append("end=\(Self.encoded(today))")
        case .custom(let start, let end):
            items.append("start=\(Self.encoded(start))")
            items.append("end=\(Self.encoded(end))")
        }
        return "/usage/summary?" + items.joined(separator: "&")
    }

    /// `/v1/plan` accepts 1...90 days and nothing else. That endpoint's own
    /// window is this number, and its labels print exactly this number — never
    /// the usage range when the two differ.
    func planDays() -> Int {
        switch range {
        case .days(let days):
            return min(max(days, 1), Self.planDayLimit)
        case .today:
            return 1
        case .all:
            return Self.planDayLimit
        case .custom(let start, let end):
            return min(max(UsageDayStamp.span(start, end) ?? Self.planDayLimit, 1), Self.planDayLimit)
        }
    }

    static let planDayLimit = 90

    /// Query-value encoding: everything outside RFC 3986's unreserved set is
    /// percent-escaped, so a model name with a slash or colon stays one value.
    static func encoded(_ value: String) -> String {
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "._-"))
        return value.addingPercentEncoding(withAllowedCharacters: allowed) ?? value
    }
}

// MARK: - Day stamps

/// `YYYY-MM-DD` day stamps — the wire form an explicit range accepts. The app
/// and the daemon share one machine, so the process's own calendar day is the
/// one `date.today()` uses there.
enum UsageDayStamp {
    static func today(calendar: Calendar = .current, date: Date = SnapshotMode.currentDate) -> String {
        text(date, calendar: calendar)
    }

    static func text(_ date: Date, calendar: Calendar) -> String {
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
    }

    static func date(_ stamp: String, calendar: Calendar = .current) -> Date? {
        let parts = stamp.split(separator: "-")
        guard parts.count == 3,
              let year = Int(parts[0]), let month = Int(parts[1]), let day = Int(parts[2]) else { return nil }
        var components = DateComponents()
        components.year = year
        components.month = month
        components.day = day
        return calendar.date(from: components)
    }

    /// Inclusive day count between two stamps; nil when either is unreadable or
    /// the range runs backwards. Calendar arithmetic in UTC: a day count does
    /// not depend on which side of midnight the reader sits.
    static func span(_ start: String, _ end: String) -> Int? {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0) ?? .gmt
        guard let startDate = date(start, calendar: calendar),
              let endDate = date(end, calendar: calendar),
              let days = calendar.dateComponents([.day], from: startDate, to: endDate).day,
              days >= 0 else { return nil }
        return days + 1
    }
}

// MARK: - Naming one range

/// How a date filter is named. One value so the menu face, the results summary,
/// the section captions, the date table's reset button and the capacity
/// ledger's column header all name the same range the same way.
struct UsageRangeLabel: Equatable {
    /// "7d" — the results summary's first part.
    let short: String
    /// "7d" — the Date menu's resting face.
    let menuFace: String
    /// "last 7 days" — section captions.
    let long: String
    /// "7d" — the capacity ledger's "Recorded use · 7d" column.
    let compact: String
    /// "All 7d" — the date table's "whole range, not one day" button.
    let allDays: String
    let isAllTime: Bool

    init(range: UsageDateFilter) {
        switch range {
        case .days(let days):
            short = "\(days)d"
            menuFace = "\(days)d"
            long = "last \(days) days"
            compact = "\(days)d"
            allDays = "All \(days)d"
            isAllTime = false
        case .today:
            short = "Today"
            menuFace = "Today"
            long = "today"
            compact = "Today"
            allDays = "All today"
            isAllTime = false
        case .all:
            short = "All"
            menuFace = "All"
            long = "all recorded days"
            compact = "All"
            allDays = "All recorded days"
            isAllTime = true
        case .custom(let start, let end):
            short = "\(start) – \(end)"
            menuFace = "Custom range"
            long = "\(start) to \(end)"
            compact = "custom range"
            allDays = "All days in range"
            isAllTime = false
        }
    }
}

// MARK: - What the results area says it is showing

/// The committed filter, in the filter bar's own left-to-right order
/// (date · agent · model · provider). For an explicit range the date part is
/// the window the daemon itself resolved, so a clamped or shifted resolution is
/// named rather than papered over.
struct UsageFilterSummary: Equatable {
    let parts: [String]

    var text: String { parts.joined(separator: " · ") }

    static func build(filter: UsageFilter, echo: UsageFiltersEcho?) -> Self {
        var parts: [String] = []
        if filter.range.isExplicit, let resolved = echo?.resolvedRange {
            parts.append(resolved)
        } else {
            parts.append(UsageRangeLabel(range: filter.range).short)
        }
        if filter.client != UsageFilter.anyValue { parts.append(filter.client) }
        if filter.model != UsageFilter.anyValue { parts.append(filter.model) }
        if filter.provider != UsageFilter.anyValue { parts.append(filter.provider) }
        return .init(parts: parts)
    }
}

// MARK: - Honesty guards on the payload

/// A payload whose `filters_echo` does not confirm the filter it was requested
/// with. A recorder that ignores the newer parameters would otherwise render
/// unfiltered numbers under a filtered label, so the pane names the mismatch
/// and hides those numbers instead of showing a wider set as if it were
/// filtered.
struct UsageFilterMismatch: Equatable {
    /// The parts of the request the response did not confirm, in the bar's own
    /// order, named for the copy.
    let unconfirmed: [String]

    var detail: String {
        "The recorder's own filter echo does not confirm \(Self.list(unconfirmed)), so these numbers may cover a wider set of rows than the filter asks for. They stay hidden rather than wear a label they do not match — update the recorder and refresh."
    }

    /// "a", "a and b", "a, b and c": the list reads as a list at any length.
    private static func list(_ items: [String]) -> String {
        switch items.count {
        case 0: return "the filter it was asked for"
        case 1: return items[0]
        case 2: return "\(items[0]) and \(items[1])"
        default: return items.dropLast().joined(separator: ", ") + " and " + (items.last ?? "")
        }
    }

    static func evaluate(filter: UsageFilter, echo: UsageFiltersEcho?) -> UsageFilterMismatch? {
        var unconfirmed: [String] = []
        if filter.client != UsageFilter.anyValue, echo?.client != filter.client {
            unconfirmed.append("the agent filter")
        }
        if filter.model != UsageFilter.anyValue, echo?.model != filter.model {
            unconfirmed.append("the model filter")
        }
        if filter.provider != UsageFilter.anyValue, echo?.provider != filter.provider {
            unconfirmed.append("the provider filter")
        }
        if filter.range.isExplicit, echo?.resolvedStart == nil || echo?.resolvedEnd == nil {
            unconfirmed.append("the resolved date range")
        }
        // A preset verifies itself when the daemon reports the window it
        // resolved: "7d" must mean seven days. A daemon that reports no
        // resolved window (older recorders) is left as it was.
        if case .days(let days) = filter.range,
           let start = echo?.resolvedStart, let end = echo?.resolvedEnd,
           UsageDayStamp.span(start, end) != days {
            unconfirmed.append("the resolved date range")
        }
        return unconfirmed.isEmpty ? nil : Self(unconfirmed: unconfirmed)
    }
}

/// The empty state's copy: the filter matched no saved row. The daemon's own
/// flags say when a value is absent from the ledger entirely, and that is
/// stated as such — never softened into "no data" and never rounded to zero.
struct UsageFilterEmptyState: Equatable {
    let title: String
    let detail: String

    static func build(filter: UsageFilter, echo: UsageFiltersEcho?) -> Self {
        if filter.model != UsageFilter.anyValue, echo?.modelMatchesSavedRows == false {
            return .init(
                title: "No saved rows match these filters",
                detail: "No saved usage row carries the model “\(filter.model)”, so the recorder returned the empty result instead of guessing. Set the model filter back to All to see the rows it does have."
            )
        }
        if filter.provider != UsageFilter.anyValue, echo?.providerMatchesSavedRows == false {
            return .init(
                title: "No saved rows match these filters",
                detail: "No saved usage row carries the provider “\(filter.provider)”, so the recorder returned the empty result instead of guessing. Set the provider filter back to All to see the rows it does have."
            )
        }
        let range = UsageRangeLabel(range: filter.range)
        if !filter.narrowsAnything {
            return .init(
                title: "No saved usage rows in this range",
                detail: "The recorded ledger holds no saved usage row in \(range.long)."
            )
        }
        let summary = UsageFilterSummary.build(filter: filter, echo: echo).text
        return .init(
            title: "No saved rows match these filters",
            detail: "No saved usage row matches \(summary). Widen the date range or set a filter back to All."
        )
    }
}

extension UsageSummary {
    /// True when the payload holds no saved row at all — the cube's own
    /// "the filter matched nothing" shape (empty lanes, and a fully empty
    /// result is never gap-filled). A held or unpriced row is still a row: it
    /// renders its own honest "—", not this state.
    var hasNoSavedRows: Bool {
        if !byClient.isEmpty || !byModel.isEmpty { return false }
        if let rows = totals?.rows, rows > 0 { return false }
        return !(byPeriod ?? []).contains { period in
            (period.usage.rows ?? 0) > 0
                || period.usage.estimatedCostUsd != nil
                || (period.usage.totalTokensIncludingCached ?? period.usage.freshTokens ?? 0) > 0
        }
    }
}

// MARK: - The menus' options

/// Every value the three list menus can offer. Built from the UNFILTERED cube
/// (`days=all`, no client/model/provider) so one applied filter can never
/// remove a neighbouring menu's choices.
struct UsageFilterOptions: Equatable {
    var clients: [String] = []
    var models: [String] = []
    var providers: [String] = []

    static func build(_ usage: UsageSummary) -> Self {
        .init(
            clients: sorted(usage.byClient.compactMap(\.client)),
            models: sorted(usage.byModel.compactMap(\.model)),
            providers: sorted(usage.byModel.compactMap(\.provider))
        )
    }

    /// A menu's options: the unfiltered set plus the committed choice, so a
    /// value the option payload does not carry yet (or no longer carries) still
    /// renders as the selected row instead of blanking the control.
    func clients(keeping selection: String) -> [String] { Self.list(clients, keeping: selection) }
    func models(keeping selection: String) -> [String] { Self.list(models, keeping: selection) }
    func providers(keeping selection: String) -> [String] { Self.list(providers, keeping: selection) }

    private static func list(_ values: [String], keeping selection: String) -> [String] {
        guard selection != UsageFilter.anyValue, !values.contains(selection) else { return values }
        return (values + [selection]).sorted()
    }

    /// Locale-independent order: the same payload renders the same menu in a
    /// snapshot and in the live window.
    private static func sorted(_ values: [String]) -> [String] {
        Array(Set(values.filter { !$0.isEmpty })).sorted()
    }
}
