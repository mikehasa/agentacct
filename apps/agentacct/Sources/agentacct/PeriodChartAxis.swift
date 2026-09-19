import SwiftUI

/// The ONE y-axis label column for period bar charts (C46), shared by the
/// Dashboard and Usage charts.
///
/// * The column is exactly `plotHeight` tall — the date band under the plot
///   sits OUTSIDE it, so the bottom label can never line up with a date tick.
/// * `labels` run top to bottom and are spaced evenly: the first label sits
///   on the top gridline (the max value), the last on the zero gridline.
///   Each label is vertically centered on its gridline.
/// * Labels never wrap (`fixedSize`), and the column width is the widest
///   label's natural width — no fixed 44pt column that forces `121.1M` onto
///   two lines. Format values with `Fmt.axisDollars` / `Fmt.axisTokens`.
struct PeriodChartAxis: View {
    let labels: [String]
    let plotHeight: CGFloat

    /// The gridline y-offset (from the plot top) for label `index`.
    static func gridlineY(index: Int, count: Int, plotHeight: CGFloat) -> CGFloat {
        guard count > 1 else { return plotHeight }
        return plotHeight * CGFloat(index) / CGFloat(count - 1)
    }

    var body: some View {
        ZStack(alignment: .topTrailing) {
            // Width probe: the widest label sets the column width.
            VStack(alignment: .trailing, spacing: 0) {
                ForEach(labels.indices, id: \.self) { index in
                    label(labels[index])
                }
            }
            .frame(height: 0, alignment: .top)
            .hidden()

            // Each label sits in a zero-height frame at its gridline, so its
            // vertical center IS the gridline. The overflow never enters the
            // stack's bounds (an alignment-guide shift did, pushing every
            // label half a line below its gridline).
            ForEach(labels.indices, id: \.self) { index in
                let y = Self.gridlineY(index: index, count: labels.count, plotHeight: plotHeight)
                label(labels[index])
                    .frame(height: 0, alignment: .center)
                    .offset(y: y)
            }
        }
        .frame(height: plotHeight, alignment: .topTrailing)
        .accessibilityHidden(true)
    }

    private func label(_ text: String) -> some View {
        Text(text)
            .workFont(.dataSmall)
            .foregroundStyle(Theme.muted)
            .lineLimit(1)
            .fixedSize()
    }
}

/// The ONE absence mark for a period bar chart (K46), shared by the Dashboard
/// and Usage charts.
///
/// The two absences differ by SHAPE and WEIGHT, never by hue: the previous
/// Usage chart separated them with `Theme.hairline` against `Theme.tintNeutral`,
/// which is ΔE 1.1 apart in dark — invisible. Shape is the same device the
/// evidence pips use, and both marks stay in neutral tokens, so no reserved
/// colour is spent on a chart.
///
/// `unpriced` is the HEAVIER mark on purpose: usage that exists but carries no
/// price is a fact a cost reviewer must see, and it must never read as lighter
/// than "nothing happened".
struct PeriodAbsenceMark: View {
    enum Kind: Equatable {
        /// The cube recorded no usage rows for the period.
        case noUsage
        /// Usage exists, but this series has no figure for it.
        case unpriced

        /// The payload's own words for the state — never a Swift synonym.
        var name: String {
            switch self {
            case .noUsage: return PayloadAbsence.noUsage
            case .unpriced: return PayloadAbsence.unpriced
            }
        }
    }

    let kind: Kind

    var body: some View {
        switch kind {
        case .noUsage:
            // A bare 1pt rule: present, measurable, and clearly not a bar.
            Rectangle()
                .fill(Theme.rule)
                .frame(height: 1)
        case .unpriced:
            // A 3pt stub with its own outline, so it reads as a body with an
            // edge rather than a fainter line.
            RoundedRectangle(cornerRadius: 1, style: .continuous)
                .fill(Theme.tintNeutral)
                .overlay(
                    RoundedRectangle(cornerRadius: 1, style: .continuous)
                        .strokeBorder(Theme.rule, lineWidth: 1)
                )
                .frame(height: 3)
        }
    }
}

/// The ONE hover/focus readout for a period bar chart (K79), shared by the
/// Dashboard and Usage charts.
///
/// It sits in a reserved band ABOVE the plot, anchored at the focused bar's x.
/// The Usage chart used to pin its readout to the plot's top-LEFT corner, up to
/// ~580px from the bar it described, while the Dashboard drew a second,
/// differently-worded readout over its bars.
struct PeriodChartReadout: View {
    /// `<period label> · <value> · <qualifier>` — every part is payload text.
    let text: String

    /// The band a chart reserves above its plot for this readout.
    static let bandHeight: CGFloat = 20

    /// The readout's line: `Sep 13 · ~$69.97 · Partial subtotal · 3 of 50
    /// usage records unpriced`. The qualifier words are the reducer's; Swift
    /// never invents a bare "partial".
    static func text(period: String, value: String) -> String {
        "\(period) · \(value)"
    }

    /// The readout's LEADING edge: centered on the focused bar, then pulled
    /// back so the whole label stays inside the plot instead of hanging off
    /// an edge. A label wider than the plot starts at the plot's left edge.
    static func leadingX(
        columnCenter: CGFloat,
        readoutWidth: CGFloat,
        plotWidth: CGFloat
    ) -> CGFloat {
        let ideal = columnCenter - readoutWidth / 2
        let rightmost = max(0, plotWidth - readoutWidth)
        return min(max(0, ideal), rightmost)
    }

    var body: some View {
        Text(text)
            .workFont(.dataSmallSemibold)
            .foregroundStyle(Theme.ink)
            .lineLimit(1)
            .fixedSize()
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                    .strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW)
            )
            .allowsHitTesting(false)
            .accessibilityHidden(true)
    }
}

/// Date-band density for a period chart: one rule for both charts instead of
/// "first/middle/last" on one and "every bucket" on the other (K79).
enum PeriodChartDateBand {
    /// Label every `stride`-th bucket — the smallest stride whose labels fit
    /// side by side: `ceil(labelWidth * count / plotWidth)`, never below 1.
    static func stride(labelWidth: CGFloat, count: Int, plotWidth: CGFloat) -> Int {
        guard count > 0, plotWidth > 0, labelWidth > 0 else { return 1 }
        let needed = labelWidth * CGFloat(count) / plotWidth
        return max(1, Int(needed.rounded(.up)))
    }

    /// The indices a band labels at that stride. The LAST bucket is always
    /// labelled, so the range's end is never left unnamed — and when it lands
    /// closer than one stride to the previous label it REPLACES that one
    /// rather than sitting on top of it.
    static func labelledIndices(count: Int, stride: Int) -> Set<Int> {
        guard count > 0 else { return [] }
        let step = max(1, stride)
        var indices = Set(Swift.stride(from: 0, to: count, by: step))
        let last = count - 1
        if let nearest = indices.max(), nearest != last, last - nearest < step {
            indices.remove(nearest)
        }
        indices.insert(last)
        return indices
    }
}

private struct PeriodChartReadoutWidthKey: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}

/// Places a `PeriodChartReadout` over the focused bar inside a plot-wide,
/// top-leading band. The readout measures itself, so the clamp uses the label
/// actually drawn rather than a guessed width.
struct PeriodChartReadoutAnchor: ViewModifier {
    let columnCenter: CGFloat
    let plotWidth: CGFloat
    @State private var measuredWidth: CGFloat = 0

    func body(content: Content) -> some View {
        content
            .background(
                GeometryReader { proxy in
                    Color.clear.preference(key: PeriodChartReadoutWidthKey.self, value: proxy.size.width)
                }
            )
            .onPreferenceChange(PeriodChartReadoutWidthKey.self) { width in
                measuredWidth = width
            }
            .offset(
                x: PeriodChartReadout.leadingX(
                    columnCenter: columnCenter,
                    readoutWidth: measuredWidth,
                    plotWidth: plotWidth
                )
            )
    }
}
