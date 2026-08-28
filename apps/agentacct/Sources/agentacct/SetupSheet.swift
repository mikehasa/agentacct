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

            Text("agentacct records what your coding agents actually do — the work, not just tokens — for this local dashboard. This installs the recorder and configures the agents you have.")
                .font(Type.body)
                .foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)

            VStack(alignment: .leading, spacing: 6) {
                SetupBullet(text: "Installs a self-contained recorder to ~/.local (no Python needed).")
                SetupBullet(text: "Registers agentacct with Claude Code, Codex, and other MCP agents.")
                SetupBullet(text: "Adds the recording hooks and a short \u{201C}record your work\u{201D} instruction. Your own settings are never overwritten.")
                SetupBullet(text: "Everything stays on this machine. No API keys, no uploads.")
            }

            if !setup.log.isEmpty {
                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(alignment: .leading, spacing: 2) {
                            ForEach(Array(setup.log.enumerated()), id: \.offset) { i, line in
                                Text(line)
                                    .font(Type.dataSmall)
                                    .foregroundStyle(Theme.muted)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .id(i)
                            }
                        }
                        .padding(8)
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
                Button {
                    Task { await setup.setUp() }
                } label: {
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
                    Text("Recording is configured.").font(Face.sansFont(14, .medium)).foregroundStyle(Theme.ink)
                }
                Text("Open a NEW agent session (in any project) so it picks up the tools and hooks — the session that ran setup can't see them yet.")
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
                HStack {
                    Button("Close", action: onClose)
                        .buttonStyle(QuietButtonStyle(tint: Theme.muted))
                        .foregroundStyle(Theme.muted)
                    Spacer()
                    Button("Try again") { setup.reset(); Task { await setup.setUp() } }
                        .buttonStyle(.borderedProminent).tint(Theme.accent)
                }
            }
        }
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
