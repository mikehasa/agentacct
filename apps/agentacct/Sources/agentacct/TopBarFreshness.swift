import SwiftUI

/// The toolbar's honesty stamp: two independent freshness lanes, neither of
/// which ever borrows the other's time.
///
/// - `localData` is `DashboardStore.lastUpdated` — how old the local data this
///   window shows is: the newest completed work-receipt generation the daemon
///   served, or the read time when the daemon publishes no generation metadata.
/// - `recordedUsage` is `DashboardStore.usageLastUpdated` — how old the
///   recorded token and cost import this window shows is, as of the last
///   successful read of it. The import's own write time is not a value the
///   store is given, so the copy never claims it.
///
/// A stamp the store never received reads as `time unavailable`. The presenter
/// never estimates, rounds up, or substitutes "now" for a time it was not
/// given, and the only relative-time formatter it uses is the app's own
/// `dashboardFreshnessText`.
struct TopBarFreshness: Equatable {
    let localData: Date?
    let recordedUsage: Date?

    static let unavailableText = "time unavailable"

    var summary: String { "Local data · \(localDataText)" }
    var localDataText: String { Self.stampText(localData) }
    var recordedUsageText: String { Self.stampText(recordedUsage) }

    var hasLocalData: Bool { localData != nil }

    /// Unavailable local data is not an error, but it must not wear the green
    /// dot that certifies a completed snapshot.
    var dotTint: Color { hasLocalData ? Theme.green : Theme.amber }

    /// Both lanes, spelled out, for readers who never hover.
    var accessibilityLabel: String {
        "Local data last refreshed \(localDataText). "
            + "Recorded usage last read \(recordedUsageText)."
    }

    /// The hover help names each lane and its provenance, so "Local data" is
    /// never mistaken for a promise about the recorded-usage import or vice
    /// versa. The recorded-usage lane reports when this window last READ the
    /// imported rows — the import's own write time is not something the store
    /// is given, and the chip does not imply one.
    var help: String {
        [
            localData.map {
                "Local data last refreshed \(dashboardFreshnessText($0)) — the newest completed "
                    + "work-receipt snapshot read from the local recorder."
            } ?? "Local data refresh time unavailable — no completed work-receipt snapshot has "
                + "been read for this window yet.",
            recordedUsage.map {
                "Recorded usage last read \(dashboardFreshnessText($0)) — the locally imported "
                    + "token and cost rows."
            } ?? "Recorded usage read time unavailable — this window has not read the locally "
                + "imported token and cost rows yet.",
        ].joined(separator: "\n")
    }

    /// `dashboardFreshnessText` reports a zero or future stamp as unavailable,
    /// which is exactly the case where printing an age would be a guess.
    private static func stampText(_ date: Date?) -> String {
        guard let date else { return unavailableText }
        return dashboardFreshnessText(date)
    }
}

/// The always-present freshness chip. It renders in every state: a window that
/// has not refreshed yet says so instead of dropping the indicator, which is
/// otherwise indistinguishable from a fresh one.
struct TopBarFreshnessIndicator: View {
    let freshness: TopBarFreshness

    var body: some View {
        HStack(spacing: 5) {
            Circle().fill(freshness.dotTint).frame(width: 5, height: 5)
            Text(freshness.summary)
        }
        .workFont(.dataSmall)
        .foregroundStyle(Theme.muted)
        .help(freshness.help)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(freshness.accessibilityLabel)
        .accessibilityIdentifier("dashboard.freshness")
    }
}
