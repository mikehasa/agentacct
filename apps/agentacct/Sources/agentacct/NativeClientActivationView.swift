import AppKit
import SwiftUI

/// Guidance for an existing connection. It has no installer or SetupModel
/// reference, so reopening another client's steps cannot select that client,
/// rerun onboarding, or advance the capture boundary.
@MainActor
struct NativeClientActivationView: View {
    let client: SetupClient
    let boundary: Date
    let capture: SetupCaptureConfirmation?
    let savedSetupLog: [String]?
    let onClose: () -> Void
    let onOpenWork: () -> Void
    let onOpenCapture: ((String) -> Void)?
    let canViewSavedWork: Bool

    @State private var showingLog = false
    private var layout = NativeSetupLayout()
    @AccessibilityFocusState private var headingFocused: Bool

    init(
        client: SetupClient,
        boundary: Date,
        capture: SetupCaptureConfirmation?,
        savedSetupLog: [String]? = nil,
        onClose: @escaping () -> Void,
        onOpenWork: @escaping () -> Void,
        onOpenCapture: ((String) -> Void)? = nil,
        canViewSavedWork: Bool = true
    ) {
        self.client = client
        self.boundary = boundary
        self.capture = capture
        self.savedSetupLog = savedSetupLog
        self.onClose = onClose
        self.onOpenWork = onOpenWork
        self.onOpenCapture = onOpenCapture
        self.canViewSavedWork = canViewSavedWork
        _showingLog = State(initialValue: SnapshotMode.enabled && SnapshotMode.reviewExpandSetupDetails)
    }

    private var confirmed: Bool { capture?.confirms(client: client, after: boundary) == true }

    var body: some View {
        VStack(spacing: 0) {
            layout.row(spacing: Space.m) {
                Label("\(client.title) connection", systemImage: "waveform.path")
                    .workFont(.rowLabel).foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
                if !layout.stacksControls { Spacer() }
                Button("Back", action: onClose)
                    .buttonStyle(QuietButtonStyle(tint: Theme.accent))
                    .accessibilityLabel("Close activation guide")
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, Space.gutter)
            .padding(.vertical, Space.l)
            Divider().overlay(Theme.cardLine)

            ScrollView {
                VStack(alignment: .leading, spacing: Space.xl) {
                    VStack(alignment: .leading, spacing: Space.m) {
                        Text(confirmed ? "\(client.title) capture confirmed" : "Finish connecting \(client.title)")
                            .workFont(.titlePage).tracking(Type.titlePageTracking)
                            .foregroundStyle(Theme.ink).accessibilityAddTraits(.isHeader)
                            .accessibilityFocused($headingFocused)
                        Text(confirmed
                             ? "A recorded event from this client arrived after its latest setup or reconnect."
                             : "Your existing connection is waiting for a fresh recorded event. Follow the activation steps in \(client.title), then inspect the result in Work.")
                            .workFont(.body).foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    if confirmed {
                        Text("This confirms one recorded event. Check Sources for separate usage, identity or history issues.")
                            .workFont(.body).foregroundStyle(Theme.muted)
                    }

                    if !confirmed {
                        VStack(alignment: .leading, spacing: Space.xl) {
                            informationRow(symbol: "1.circle", title: "Activate \(client.title)",
                                           detail: SetupConfigurationPlan.activationInstruction(for: client))
                            informationRow(symbol: "2.circle", title: "Run a small task",
                                           detail: "In the new session, ask the agent to inspect a project and record a short work section with agentacct.")
                            informationRow(symbol: "3.circle", title: "Check the recorded work",
                                           detail: "Open Work to inspect the session's evidence. If nothing arrives, check Sources and the saved setup output for the reported cause.")
                        }
                        .padding(Space.cardPad)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
                        .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.cardLine))
                        DisclosureGroup("Capture check details") {
                            Text("Waiting for an event from \(client.title) recorded after \(boundary.formatted(date: .abbreviated, time: .standard)). Starting the recorder alone does not confirm capture.")
                                .workFont(.caption).foregroundStyle(Theme.muted)
                                .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                                .padding(.top, Space.s)
                        }.workFont(.caption)
                    }

                    if let capture, confirmed {
                        Text("Observed \(capture.observedAt.formatted(date: .abbreviated, time: .standard)) · \(capture.eventID)")
                            .workFont(.dataSmall).foregroundStyle(Theme.muted).textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                        if capture.taskID == nil {
                            Text("The capture is confirmed. Its Work item has not yet been linked.")
                                .workFont(.body).foregroundStyle(Theme.muted)
                        }
                    }

                    if let savedSetupLog, !savedSetupLog.isEmpty {
                        setupOutput(savedSetupLog)
                    } else {
                        informationRow(symbol: "doc.text", title: "Previous setup output unavailable",
                                       detail: "Output was not saved for this connection. The activation steps are general guidance; exact consent commands and skipped configuration details from the earlier setup are unavailable here.",
                                       tint: Theme.muted)
                    }
                }
                .frame(maxWidth: 880, alignment: .leading)
                .padding(Space.gutter)
                .frame(maxWidth: .infinity, alignment: .center)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)

            Divider().overlay(Theme.cardLine)
            layout.row(spacing: Space.l) {
                Text(canViewSavedWork ? "Your existing setup and capture date are preserved." : "Recorder recovery must finish before this window can open Work.")
                    .workFont(.caption).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
                if !layout.stacksControls { Spacer() }
                if canViewSavedWork {
                    Button("Open Work", action: onOpenWork).buttonStyle(NativeSetupActionStyle())
                }
                if canViewSavedWork, confirmed, let taskID = capture?.taskID, let onOpenCapture {
                    Button("Open captured work") { onOpenCapture(taskID) }
                        .buttonStyle(NativeSetupActionStyle(prominent: true))
                        .keyboardShortcut(.defaultAction)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, Space.gutter)
            .padding(.vertical, Space.l)
            .background(Theme.chrome)
        }
        .workFont(.body)
        .background(Theme.canvas)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("client-activation-guide-\(client.rawValue)")
        .onChange(of: confirmed) { headingFocused = true }
    }

    private func informationRow(symbol: String, title: String, detail: String, tint: Color = Theme.accent) -> some View {
        HStack(alignment: .top, spacing: Space.m) {
            Image(systemName: symbol).workFont(size: 19, weight: .regular, relativeTo: .body).foregroundStyle(tint)
                .frame(width: layout.iconColumnWidth).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: Space.s) {
                Text(title).workFont(.rowLabel).foregroundStyle(Theme.ink)
                Text(detail).workFont(.body).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func setupOutput(_ lines: [String]) -> some View {
        DisclosureGroup(isExpanded: $showingLog) {
            VStack(alignment: .leading, spacing: Space.m) {
                layout.row(spacing: Space.m) {
                    Text("Saved output from \(client.title)'s setup. Review warnings and consent steps.")
                        .workFont(.caption).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                    if !layout.stacksControls { Spacer() }
                    Button("Copy output") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(lines.joined(separator: "\n"), forType: .string)
                    }.buttonStyle(NativeSetupActionStyle())
                }
                ScrollView([.horizontal, .vertical]) {
                    Text(lines.joined(separator: "\n"))
                        .workFont(.dataSmall).foregroundStyle(Theme.ink).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading).padding(Space.m)
                }
                .frame(height: layout.outputHeight)
                .background(Theme.chrome, in: RoundedRectangle(cornerRadius: Metrics.radius))
                .accessibilityIdentifier("client-activation-output-viewport")
            }
            .padding(.top, Space.m)
        } label: {
            Text("Saved setup output · \(Fmt.count(lines.count, "line"))").workFont(.rowLabel).foregroundStyle(Theme.ink)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("client-activation-output")
    }
}
