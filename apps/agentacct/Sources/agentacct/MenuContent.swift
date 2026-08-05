import SwiftUI

// The dropdown: usage · limits · plan · recent sessions, in that order.
// Honesty rules carried over from the TUI: costs are complete-$ / partial-~$ /
// em-dash (never a fabricated $0), plan shares only when calibrated, statuses
// use the blocked > handed_off > in_progress > completed reduction the glance
// already applied server-side.

struct MenuContent: View {
    @ObservedObject var state: GlanceState

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            switch state.phase {
            case .connecting:
                Text("Connecting to the agentacct daemon…")
                    .foregroundStyle(.secondary)
            case .disconnected(let reason):
                disconnectedView(reason: reason)
            case .incompatible(let message):
                Label {
                    Text(message)
                } icon: {
                    Image(systemName: "exclamationmark.triangle")
                }
                .foregroundStyle(.orange)
            case .connected(let snapshot):
                connectedView(snapshot: snapshot)
            }
            Divider()
            footer
        }
        .padding(12)
        .frame(width: 340)
    }

    private func disconnectedView(reason: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("agentacct daemon not reachable", systemImage: "bolt.slash")
                .foregroundStyle(.secondary)
            Text(reason)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text("Start it with:  agentacct start")
                .font(.caption.monospaced())
        }
    }

    @ViewBuilder
    private func connectedView(snapshot: GlanceSnapshot) -> some View {
        let glance = snapshot.glance

        // Usage windows (today / 7d / 30d)
        VStack(alignment: .leading, spacing: 4) {
            Text("Usage").font(.headline)
            ForEach(glance.usage.windows.filter { $0.label != "all time" }, id: \.label) { window in
                HStack {
                    Text(window.label)
                        .frame(width: 90, alignment: .leading)
                        .foregroundStyle(.secondary)
                    Text(window.totals.tokensText + " tok")
                        .frame(width: 80, alignment: .trailing)
                        .font(.body.monospacedDigit())
                    Spacer()
                    Text(window.totals.costText)
                        .font(.body.monospacedDigit())
                }
                .font(.callout)
            }
        }

        if !glance.limits.isEmpty {
            Divider()
            VStack(alignment: .leading, spacing: 6) {
                Text("Limits").font(.headline)
                ForEach(Array(glance.limits.enumerated()), id: \.offset) { _, limit in
                    limitRows(limit)
                }
            }
        }

        if !glance.recentSessions.isEmpty {
            Divider()
            VStack(alignment: .leading, spacing: 4) {
                Text("Recent sessions").font(.headline)
                ForEach(Array(glance.recentSessions.prefix(6).enumerated()), id: \.offset) { _, session in
                    sessionRow(session)
                }
            }
        }

        planFootnote(glance.plan)
    }

    @ViewBuilder
    private func limitRows(_ limit: LimitEntry) -> some View {
        ForEach(Array((limit.windows ?? []).enumerated()), id: \.offset) { _, window in
            if let used = window.usedPercent {
                HStack(spacing: 8) {
                    Text("\(limit.client ?? "?") \(window.kind ?? "")")
                        .frame(width: 110, alignment: .leading)
                        .foregroundStyle(.secondary)
                    ProgressView(value: min(max(used / 100.0, 0), 1))
                        .tint(used >= 90 ? .red : used >= 70 ? .orange : .green)
                    Text(String(format: "%.0f%%", used))
                        .font(.callout.monospacedDigit())
                        .frame(width: 44, alignment: .trailing)
                    if limit.stale == true {
                        Text("stale")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
                .font(.callout)
            }
        }
    }

    private func sessionRow(_ session: RecentSession) -> some View {
        HStack(spacing: 6) {
            Text(session.statusGlyph)
                .frame(width: 14)
                .foregroundStyle(session.status == "blocked" ? .orange : .secondary)
            Text(session.title ?? "\(session.client) · \(session.shortSessionId)")
                .lineLimit(1)
                .truncationMode(.tail)
            Spacer()
            if let pct = session.planPctText {
                Text(pct)
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
        }
        .font(.callout)
    }

    @ViewBuilder
    private func planFootnote(_ plan: [PlanEntry]) -> some View {
        let calibrating = plan.filter { $0.confidence != "calibrated" }.map(\.client)
        if !calibrating.isEmpty {
            Text("plan share calibrating from your own limit history: \(calibrating.joined(separator: ", "))")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }

    private var footer: some View {
        HStack {
            Button("Refresh") { state.refreshNow() }
            Spacer()
            if let updated = state.lastUpdated {
                Text(updated, style: .time)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            Button("Quit") { NSApplication.shared.terminate(nil) }
        }
    }
}
