import SwiftUI

// The one-click setup surface. Non-developers download the app and press one
// button; this installs the embedded recorder and configures their coding
// agents (MCP servers, hooks, standing instructions) — the same `agentacct
// onboard` the CLI docs describe, driven from a window instead of a terminal.

struct SetupSheet: View {
    @ObservedObject var setup: SetupModel
    var onClose: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: Space.l) {
            HStack(spacing: 9) {
                Circle().fill(Theme.accent.gradient).frame(width: 10, height: 10)
                Text("Set up recording")
                    .font(.system(size: 15, weight: .semibold, design: .rounded))
                    .foregroundStyle(Theme.text)
                Spacer()
            }

            Text("agentacct records what your coding agents actually do — the work, not just tokens — for this local dashboard. This installs the recorder and configures the agents you have.")
                .font(Type.body)
                .foregroundStyle(Theme.textMuted)
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
                                    .font(.system(size: 11, design: .monospaced))
                                    .foregroundStyle(Theme.textMuted)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .id(i)
                            }
                        }
                        .padding(8)
                    }
                    .frame(height: 150)
                    .background(Theme.surface, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).stroke(Theme.border, lineWidth: 1))
                    .onChange(of: setup.log.count) {
                        withAnimation { proxy.scrollTo(setup.log.count - 1, anchor: .bottom) }
                    }
                }
            }

            footer
        }
        .padding(Space.l)
        .frame(width: 460)
        .background(Theme.bg)
    }

    @ViewBuilder
    private var footer: some View {
        switch setup.phase {
        case .idle:
            HStack {
                Button("Not now", action: onClose).buttonStyle(.plain).foregroundStyle(Theme.textMuted)
                Spacer()
                Button {
                    Task { await setup.setUp() }
                } label: {
                    Text("Set up recording").fontWeight(.semibold)
                }
                .buttonStyle(.borderedProminent)
                .tint(Theme.accent)
            }
        case .working(let status):
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(status).font(Type.small).foregroundStyle(Theme.textMuted)
                Spacer()
            }
        case .done:
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 7) {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.green)
                    Text("Recording is configured.").font(Type.body.weight(.medium)).foregroundStyle(Theme.text)
                }
                Text("Open a NEW agent session (in any project) so it picks up the tools and hooks — the session that ran setup can't see them yet.")
                    .font(Type.small).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
                HStack { Spacer(); Button("Done", action: onClose).buttonStyle(.borderedProminent).tint(Theme.accent) }
            }
        case .failed(let message):
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 7) {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Theme.orange)
                    Text("Setup didn't finish").font(Type.body.weight(.medium)).foregroundStyle(Theme.text)
                }
                Text(message).font(Type.small).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
                HStack {
                    Button("Close", action: onClose).buttonStyle(.plain).foregroundStyle(Theme.textMuted)
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
            Text(text).font(Type.small).foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}
