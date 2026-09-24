import SwiftUI

/// The Date menu's choices: the cube's own presets, plus the two explicit-range
/// forms (Today as a one-day closed range, and whatever interval the pickers
/// hold). Kept separate from ``UsageDateFilter`` because the custom interval
/// carries dates the menu itself does not own.
enum UsageDateChoice: String, CaseIterable, Identifiable, Hashable {
    case today
    case days7
    case days30
    case days90
    case all
    case custom

    var id: String { rawValue }

    var title: String {
        switch self {
        case .today: return "Today"
        case .days7: return "7d"
        case .days30: return "30d"
        case .days90: return "90d"
        case .all: return "All"
        case .custom: return "Custom range…"
        }
    }

    init(range: UsageDateFilter) {
        switch range {
        case .today: self = .today
        case .days(7): self = .days7
        case .days(30): self = .days30
        case .days(90): self = .days90
        case .days: self = .custom
        case .all: self = .all
        case .custom: self = .custom
        }
    }
}

/// The Usage page's filter row: Date, Agent, Model and Provider, one control
/// each and every one independent — choosing a range never clears an agent, a
/// model or a provider, and each list menu keeps offering the full set from the
/// unfiltered cube instead of what the current filter left standing.
///
/// Each control commits its own field and nothing else; the store refetches the
/// whole page from that one filter. The Date menu's "Custom range…" reveals the
/// two day pickers, which commit on change with the same one-field rule.
struct UsageFilterBar: View {
    @Environment(DashboardStore.self) var dashboard
    @Environment(\.calendar) private var calendar

    @State private var customStart = Date()
    @State private var customEnd = Date()
    @State private var customRangeSeeded = false

    private var filter: UsageFilter { dashboard.usageFilter }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            HStack(spacing: Space.m) {
                dateMenu
                agentMenu
                modelMenu
                providerMenu
                Spacer()
            }
            if case .custom = filter.range { customRangeRow }
        }
        .task { seedCustomRange() }
    }

    // MARK: - Date

    private var dateMenu: some View {
        let label = UsageRangeLabel(range: filter.range)
        return FilterMenu(
            title: "Date",
            systemImage: "calendar",
            value: label.menuFace,
            // All recorded days is the one choice that narrows nothing; every
            // other preset is a window, so it reads as an active filter.
            isActive: !label.isAllTime,
            help: "Date: \(label.long). Choose a preset or a custom range.",
            identifier: "usage.filters.date",
            selection: dateChoice
        ) {
            ForEach(UsageDateChoice.allCases) { choice in
                Text(choice.title).tag(choice)
            }
        }
        .fixedSize()
    }

    private var dateChoice: Binding<UsageDateChoice> {
        Binding(
            get: { UsageDateChoice(range: filter.range) },
            set: { commit(dateChoice: $0) }
        )
    }

    private func commit(dateChoice choice: UsageDateChoice) {
        var next = filter
        switch choice {
        case .today: next.range = .today
        case .days7: next.range = .days(7)
        case .days30: next.range = .days(30)
        case .days90: next.range = .days(90)
        case .all: next.range = .all
        case .custom:
            seedCustomRange()
            next.range = customRange()
        }
        commit(next)
    }

    @ViewBuilder
    private var customRangeRow: some View {
        if SnapshotMode.enabled && !SnapshotMode.interactiveFixture {
            // Day pickers draw as unrendered chrome offscreen; a static render
            // shows the committed interval as a chip instead.
            Chip(text: UsageRangeLabel(range: filter.range).short, tint: Theme.accent)
        } else {
            HStack(spacing: Space.m) {
                CapsLabel(text: "Range")
                DatePicker("Range start", selection: customStartBinding, in: ...customEnd, displayedComponents: .date)
                    .datePickerStyle(.compact)
                    .labelsHidden()
                    .accessibilityIdentifier("usage.filters.range.start")
                Text("to").workFont(.caption).foregroundStyle(Theme.muted)
                DatePicker("Range end", selection: customEndBinding, in: customStart..., displayedComponents: .date)
                    .datePickerStyle(.compact)
                    .labelsHidden()
                    .accessibilityIdentifier("usage.filters.range.end")
            }
        }
    }

    private var customStartBinding: Binding<Date> {
        Binding(
            get: { customStart },
            set: { newValue in
                customStart = newValue
                commitCustomRange()
            }
        )
    }

    private var customEndBinding: Binding<Date> {
        Binding(
            get: { customEnd },
            set: { newValue in
                customEnd = newValue
                commitCustomRange()
            }
        )
    }

    private func commitCustomRange() {
        var next = filter
        next.range = customRange()
        commit(next)
    }

    /// The interval the two pickers currently hold, in day stamps. The pickers
    /// constrain each other, so an inverted range cannot be picked; the clamp
    /// keeps the request well-formed even if one is set programmatically.
    private func customRange() -> UsageDateFilter {
        .custom(
            start: UsageDayStamp.text(min(customStart, customEnd), calendar: calendar),
            end: UsageDayStamp.text(max(customStart, customEnd), calendar: calendar)
        )
    }

    /// Seed the pickers from the committed interval, or from the last week when
    /// the filter is not custom yet (a sensible starting interval to adjust).
    private func seedCustomRange() {
        guard !customRangeSeeded else { return }
        customRangeSeeded = true
        if case .custom(let start, let end) = filter.range,
           let parsedStart = UsageDayStamp.date(start, calendar: calendar),
           let parsedEnd = UsageDayStamp.date(end, calendar: calendar) {
            customStart = parsedStart
            customEnd = parsedEnd
            return
        }
        customEnd = SnapshotMode.currentDate
        customStart = calendar.date(byAdding: .day, value: -6, to: SnapshotMode.currentDate) ?? SnapshotMode.currentDate
    }

    // MARK: - Agent / Model / Provider

    private var agentMenu: some View {
        listMenu(
            title: "Agent",
            allLabel: "All agents",
            systemImage: "person.crop.circle",
            identifier: "usage.filters.agent",
            selection: listBinding(\.client),
            options: dashboard.usageFilterOptions?.clients(keeping: filter.client)
                ?? fallbackOptions(keeping: filter.client)
        )
    }

    private var modelMenu: some View {
        listMenu(
            title: "Model",
            allLabel: "All models",
            systemImage: "cpu",
            identifier: "usage.filters.model",
            selection: listBinding(\.model),
            options: dashboard.usageFilterOptions?.models(keeping: filter.model)
                ?? fallbackOptions(keeping: filter.model)
        )
    }

    private var providerMenu: some View {
        listMenu(
            title: "Provider",
            allLabel: "All providers",
            systemImage: "cloud",
            identifier: "usage.filters.provider",
            selection: listBinding(\.provider),
            options: dashboard.usageFilterOptions?.providers(keeping: filter.provider)
                ?? fallbackOptions(keeping: filter.provider)
        )
    }

    /// Before the unfiltered option payload lands (or after it failed) the menu
    /// still shows the committed value, so the control never blanks out its own
    /// selection.
    private func fallbackOptions(keeping selection: String) -> [String] {
        selection == UsageFilter.anyValue ? [] : [selection]
    }

    private func listMenu(
        title: String,
        allLabel: String,
        systemImage: String,
        identifier: String,
        selection: Binding<String>,
        options: [String]
    ) -> some View {
        let value = selection.wrappedValue
        return FilterMenu(
            title: title,
            systemImage: systemImage,
            value: value == UsageFilter.anyValue ? allLabel : value,
            isActive: value != UsageFilter.anyValue,
            help: value == UsageFilter.anyValue
                ? "\(title): every saved row. Options come from the unfiltered ledger."
                : "\(title): \(value). Options come from the unfiltered ledger, so they never shrink with a filter.",
            identifier: identifier,
            selection: selection
        ) {
            Text(allLabel).tag(UsageFilter.anyValue)
            ForEach(options, id: \.self) { option in
                Text(option).tag(option)
            }
        }
        .fixedSize()
    }

    private func listBinding(_ keyPath: WritableKeyPath<UsageFilter, String>) -> Binding<String> {
        Binding(
            get: { filter[keyPath: keyPath] },
            set: { value in
                var next = filter
                next[keyPath: keyPath] = value
                commit(next)
            }
        )
    }

    /// One committed change: the store refetches the page from this filter
    /// alone, and the other three fields travel untouched inside it.
    private func commit(_ filter: UsageFilter) {
        guard filter != dashboard.usageFilter else { return }
        Task { await dashboard.setUsageFilter(filter) }
    }
}
