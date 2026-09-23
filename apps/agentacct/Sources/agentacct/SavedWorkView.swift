import SwiftUI

private struct SavedWorkReconnectKey: EnvironmentKey {
    static let defaultValue: (() -> Void)? = nil
}

extension EnvironmentValues {
    var savedWorkReconnect: (() -> Void)? {
        get { self[SavedWorkReconnectKey.self] }
        set { self[SavedWorkReconnectKey.self] = newValue }
    }
}

struct SavedWorkView: View {
    let store: DashboardStore
    let onReconnect: () -> Void
    @Environment(AppSelection.self) private var selection
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        VStack(spacing: 0) {
            savedWorkBanner
            WorkPane().environment(store).environment(\.savedWorkReconnect, onReconnect)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("work.saved-snapshot")
    }

    private var savedWorkBanner: some View {
        let layout = dynamicTypeSize.isAccessibilitySize
            ? AnyLayout(VStackLayout(alignment: .leading, spacing: Space.m))
            : AnyLayout(HStackLayout(alignment: .top, spacing: Space.l))
        return layout {
            HStack(alignment: .top, spacing: 16) {
                Image(systemName: "archivebox").foregroundStyle(Theme.muted)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 4) {
                    Text("Saved work · read-only").workFont(.rowLabel)
                    Text("Task list \(store.receiptListProjection == nil ? "saved" : "as of") \(store.savedWork?.collectionDate?.formatted(date: .abbreviated, time: .standard) ?? "at an unknown time"). Only previously opened details are available.")
                    if let date = store.receiptSavedAt, store.receipt?.taskId == selection.taskId {
                        Text(store.receipt?.projection == nil
                             ? "Selected task saved \(date.formatted(date: .abbreviated, time: .standard)). Session copies may have different saved dates."
                             : "Selected task as of \(date.formatted(date: .abbreviated, time: .standard)). Session copies may have different dates.")
                    }
                    Text("Recording status is unavailable here. Changes after these copies were saved are not shown.")
                }
                .foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            }
            if !dynamicTypeSize.isAccessibilitySize { Spacer(minLength: 0) }
            Button("Back to recovery", action: onReconnect)
                .buttonStyle(NativeSetupActionStyle())
                .accessibilityIdentifier("work.saved-snapshot.recovery")
        }
        .workFont(.caption)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(Space.m)
        .background(Theme.tintNeutral)
    }
}
