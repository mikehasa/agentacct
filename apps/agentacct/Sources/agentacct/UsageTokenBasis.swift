import SwiftUI

/// Both values are normalized by the importer/cube. In particular, Codex's
/// cached input is already part of its raw input; the UI must never add it again.
protocol UsageTokenValues {
    var freshTokens: Int? { get }
    var totalTokensIncludingCached: Int? { get }
}

extension UsageBucket: UsageTokenValues {}
extension PeriodBucket: UsageTokenValues {}
extension UsageTotals: UsageTokenValues {}

enum UsageTokenBasis: String, CaseIterable, Identifiable {
    case fresh
    case all

    var id: String { rawValue }
    var label: String { self == .fresh ? "Fresh tokens" : "All tokens" }
    var qualifier: String { self == .fresh ? "excludes cache · client-reported" : "includes cache · client-reported" }

    static let explanation = "Fresh tokens count non-cached input + output. All tokens also include recorded cache creation and cache reads, once each. Reasoning already included in output is not added again. Counts measure token traffic, including repeated context, not unique text. Only client-reported categories are included; unreported usage is not inferred. This choice changes the chart. Tables always show fresh tokens, cache and total side by side; cost and provider limits stay the same."

    func value(_ usage: (any UsageTokenValues)?) -> Int? {
        // Do not turn an absent all-token field into a fresh count or a zero.
        let value = self == .fresh ? usage?.freshTokens : usage?.totalTokensIncludingCached
        return value.flatMap { $0 >= 0 ? $0 : nil }
    }
}

private struct UsageTokenBasisKey: EnvironmentKey {
    static let defaultValue: UsageTokenBasis = .fresh
}

extension EnvironmentValues {
    var usageTokenBasis: UsageTokenBasis {
        get { self[UsageTokenBasisKey.self] }
        set { self[UsageTokenBasisKey.self] = newValue }
    }
}

/// Native segmented pickers do not paint reliably in ImageRenderer. These
/// buttons use the app's existing card/tint/feedback grammar in every renderer.
struct UsageTokenBasisControl: View {
    @Binding var selection: UsageTokenBasis

    var body: some View {
        HStack(spacing: 2) {
            ForEach(UsageTokenBasis.allCases) { basis in
                Button { selection = basis } label: {
                    Text(basis.label)
                        .workFont(.captionSemibold)
                        .foregroundStyle(selection == basis ? Theme.ink : Theme.muted)
                        .padding(.horizontal, Space.s)
                        .frame(minHeight: 26)
                        .background(selection == basis ? Theme.card : .clear,
                                    in: RoundedRectangle(cornerRadius: 5))
                }
                .buttonStyle(QuietButtonStyle(horizontalPadding: 0, verticalPadding: 0))
                .accessibilityAddTraits(selection == basis ? .isSelected : [])
                .accessibilityIdentifier("usage.tokens.\(basis.rawValue)")
            }
        }
        .padding(3)
        .background(Theme.tintNeutral, in: RoundedRectangle(cornerRadius: 7))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Token counting")
    }
}
