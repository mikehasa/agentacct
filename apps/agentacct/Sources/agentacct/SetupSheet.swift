import SwiftUI

// The one-click setup surface. Non-developers download the app and press one
// button; this installs the embedded recorder and configures their coding
// agents (MCP servers, hooks, standing instructions) — the same `agentacct
// onboard` the CLI docs describe, driven from a window instead of a terminal.

enum SetupPhaseKey: Equatable {
    case idle
    case working
    case done
    case failed
}

@MainActor
func setupPhaseKey(for phase: SetupModel.Phase) -> SetupPhaseKey {
    switch phase {
    case .idle: return .idle
    case .working: return .working
    case .done: return .done
    case .failed: return .failed
    }
}

struct SetupSheet: View {
    @ObservedObject var setup: SetupModel
    var onClose: () -> Void
    @State private var setupTask: Task<Void, Never>?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var phaseKey: SetupPhaseKey {
        setupPhaseKey(for: setup.phase)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.l) {
            HStack(spacing: 9) {
                Circle().fill(Theme.accent).frame(width: 10, height: 10)
                Text("Set up recording")
                    .font(Type.titleCard)
                    .foregroundStyle(Theme.ink)
                Spacer()
            }

            Text("Record coding activity locally so this dashboard can show work, checks, and usage. Setup installs the recorder and connects supported coding apps where possible.")
                .font(Type.body)
                .foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)

            VStack(alignment: .leading, spacing: 6) {
                SetupBullet(text: "Installs a self-contained local recorder.")
                SetupBullet(text: "Connects supported coding apps, including Claude Code and Codex.")
                SetupBullet(text: "Adds recording support where available and preserves unrelated settings.")
                SetupBullet(text: "Stores recorded dashboard data on this Mac.")
            }

            if !setup.log.isEmpty {
                ScrollViewReader { proxy in
                    Group {
                        if SnapshotMode.enabled {
                            setupLog
                                .frame(maxHeight: .infinity, alignment: .top)
                        } else {
                            ScrollView { setupLog }
                        }
                    }
                    .frame(height: 150)
                    .background(Theme.chrome, in: RoundedRectangle(cornerRadius: Metrics.radius))
                    .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW))
                    .onChange(of: setup.log.count) {
                        guard setup.log.count > 0 else { return }
                        if reduceMotion {
                            proxy.scrollTo(setup.log.count - 1, anchor: .bottom)
                        } else {
                            withAnimation(Motion.contentUpdate) {
                                proxy.scrollTo(setup.log.count - 1, anchor: .bottom)
                            }
                        }
                    }
                }
            }

            ZStack(alignment: .topLeading) {
                footer
                    .id(phaseKey)
                    .transition(.opacity)
            }
            .frame(maxWidth: .infinity, minHeight: 96, alignment: .topLeading)
            .animation(
                reduceMotion ? Motion.reducedCrossfade : Motion.phaseCrossfade,
                value: phaseKey
            )
        }
        .padding(Space.l)
        .frame(width: 460)
        .background(Theme.canvas)
        .onDisappear {
            setupTask?.cancel()
            setupTask = nil
        }
    }

    private var setupLog: some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(Array(setup.log.enumerated()), id: \.offset) { i, line in
                Text(line)
                    .font(Type.dataSmall)
                    .foregroundStyle(Theme.muted)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
                    .id(i)
            }
        }
        .padding(8)
    }

    @ViewBuilder
    private var footer: some View {
        switch setup.phase {
        case .idle:
            HStack {
                Button("Not now", action: onClose)
                    .buttonStyle(QuietButtonStyle(tint: Theme.muted))
                    .foregroundStyle(Theme.muted)
                Spacer()
                Button(action: startSetup) {
                    Text("Set up recording").font(Face.sansFont(13, .semibold))
                }
                .buttonStyle(.borderedProminent)
                .tint(Theme.accent)
            }
        case .working(let status):
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(status).font(Type.caption).foregroundStyle(Theme.muted)
                Spacer()
            }
        case .done:
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 7) {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.green)
                    Text("Setup finished.").font(Face.sansFont(14, .medium)).foregroundStyle(Theme.ink)
                }
                Text("Review the log for apps that still need attention. Start a new coding session for each configured app, then open Sources to confirm recording.")
                    .font(Type.caption).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
                HStack { Spacer(); Button("Done", action: onClose).buttonStyle(.borderedProminent).tint(Theme.accent) }
            }
        case .failed(let message):
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 7) {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Theme.amber)
                    Text("Setup didn't finish").font(Face.sansFont(14, .medium)).foregroundStyle(Theme.ink)
                }
                Text(message).font(Type.caption).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
                Text("Review the final log lines, resolve the reported issue, then try again.")
                    .font(Type.caption)
                    .foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
                HStack {
                    Button("Close", action: onClose)
                        .buttonStyle(QuietButtonStyle(tint: Theme.muted))
                        .foregroundStyle(Theme.muted)
                    Spacer()
                    Button("Try again") {
                        setup.reset()
                        startSetup()
                    }
                    .buttonStyle(.borderedProminent).tint(Theme.accent)
                }
            }
        }
    }

    private func startSetup() {
        setupTask?.cancel()
        setupTask = Task { await setup.setUp() }
    }
}

private struct SetupBullet: View {
    let text: String
    var body: some View {
        HStack(alignment: .top, spacing: 7) {
            Circle().fill(Theme.accent.opacity(0.7)).frame(width: 4, height: 4).padding(.top, 6)
            Text(text).font(Type.caption).foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}
