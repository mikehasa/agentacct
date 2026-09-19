import SwiftUI

/// The one refresh action of the focused main window (C104). The top bar
/// publishes it as a focused scene value, so the app menu's Refresh item and
/// the top-bar refresh glyph run exactly the same closure and share ⌘R.
struct RefreshLocalDataAction {
    let isRefreshing: Bool
    let perform: () -> Void
}

private struct RefreshLocalDataKey: FocusedValueKey {
    typealias Value = RefreshLocalDataAction
}

extension FocusedValues {
    var refreshLocalData: RefreshLocalDataAction? {
        get { self[RefreshLocalDataKey.self] }
        set { self[RefreshLocalDataKey.self] = newValue }
    }
}

enum RefreshCommandText {
    static let title = "Refresh"
    static let shortcut = "\u{2318}R"
    /// Tooltips name the verb and its shortcut: "verb (shortcut)".
    static let help = "Refresh local data (\(shortcut))"
}

struct RefreshCommands: Commands {
    @FocusedValue(\.refreshLocalData) private var refresh

    var body: some Commands {
        CommandGroup(after: .toolbar) {
            Button(RefreshCommandText.title) { refresh?.perform() }
                .buttonStyle(QuietButtonStyle())
                .keyboardShortcut("r", modifiers: .command)
                .disabled(refresh == nil || refresh?.isRefreshing == true)
        }
    }
}

/// Opening one of the app's four sections, published by the focused window's
/// top bar. The menu commands below run the SAME closure the destination tabs
/// and the destination picker run, so ⌘2 lands exactly where clicking Work
/// lands — on the receipts table, with no stale record (K114).
struct OpenSectionAction {
    let open: (MainPane) -> Void
}

private struct OpenSectionKey: FocusedValueKey {
    typealias Value = OpenSectionAction
}

/// Moving keyboard focus into the frontmost surface's search field. A pane
/// without one publishes nothing, and Find is then disabled rather than
/// pretending to work.
struct FocusSearchAction {
    let perform: () -> Void
}

private struct FocusSearchKey: FocusedValueKey {
    typealias Value = FocusSearchAction
}

extension FocusedValues {
    var openSection: OpenSectionAction? {
        get { self[OpenSectionKey.self] }
        set { self[OpenSectionKey.self] = newValue }
    }

    var focusSearch: FocusSearchAction? {
        get { self[FocusSearchKey.self] }
        set { self[FocusSearchKey.self] = newValue }
    }
}

/// The primary destinations as menu commands. The menu bar is the keyboard
/// path that works without changing a system setting, and until now the four
/// sections had none: ⌘1–⌘4 name them with the words their tabs use, and ⌘F
/// is the standard Find, disabled where there is nothing to search.
struct SectionCommands: Commands {
    @FocusedValue(\.openSection) private var openSection
    @FocusedValue(\.focusSearch) private var focusSearch

    var body: some Commands {
        CommandGroup(after: .sidebar) {
            ForEach(Array(MainPane.allCases.enumerated()), id: \.element) { index, pane in
                Button(pane.rawValue) { openSection?.open(pane) }
                    .buttonStyle(QuietButtonStyle())
                    .keyboardShortcut(
                        KeyEquivalent(Character("\(index + 1)")),
                        modifiers: .command
                    )
                    .disabled(openSection == nil)
            }
            Divider()
            Button(FindCommandText.title) { focusSearch?.perform() }
                .buttonStyle(QuietButtonStyle())
                .keyboardShortcut("f", modifiers: .command)
                .disabled(focusSearch == nil)
        }
    }
}

enum FindCommandText {
    static let title = "Find"
    static let shortcut = "\u{2318}F"
}
