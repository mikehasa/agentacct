import SwiftUI
import XCTest
@testable import agentacct

/// The ONE page-container rule (K112): `pageFrame()` owns the width cap AND
/// the leading alignment, so no pane can apply the cap without the alignment
/// and slide sideways relative to its neighbours on a wide window.
final class PageFrameRuleTests: XCTestCase {
    private var sourcesDirectory: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Sources/agentacct", isDirectory: true)
    }

    func testPageFrameCarriesBothTheCapAndTheAlignment() throws {
        let theme = try String(
            contentsOf: sourcesDirectory.appendingPathComponent("Theme.swift"),
            encoding: .utf8
        )
        let start = try XCTUnwrap(theme.range(of: "func pageFrame() -> some View {"))
        let end = try XCTUnwrap(theme.range(of: "\n    }", range: start.upperBound..<theme.endIndex))
        let body = theme[start.upperBound..<end.lowerBound]
        XCTAssertTrue(body.contains("maxWidth: Metrics.pageMaxWidth, alignment: .leading"))
        XCTAssertTrue(body.contains("maxWidth: .infinity, alignment: .leading"))
    }

    func testNoPaneRepeatsTheOuterLeadingFrameAfterPageFrame() throws {
        for pane in ["DashboardPane", "UsagePane", "WorkPane", "SourcesPane"] {
            let source = try String(
                contentsOf: sourcesDirectory.appendingPathComponent("\(pane).swift"),
                encoding: .utf8
            )
            var searchStart = source.startIndex
            while let call = source.range(of: ".pageFrame()", range: searchStart..<source.endIndex) {
                let tailEnd = source.index(call.upperBound, offsetBy: 80, limitedBy: source.endIndex) ?? source.endIndex
                let tail = source[call.upperBound..<tailEnd]
                XCTAssertFalse(
                    tail.hasPrefix("\n                .frame(maxWidth: .infinity, alignment: .leading)")
                        || tail.hasPrefix("\n            .frame(maxWidth: .infinity, alignment: .leading)"),
                    "\(pane) repeats the leading frame that pageFrame() now owns"
                )
                searchStart = call.upperBound
            }
        }
    }
}

/// The shared period-chart pieces (K46, K79).
final class PeriodChartSharedPartsTests: XCTestCase {
    func testAbsenceMarksTakeTheirNamesFromThePayloadVocabulary() {
        XCTAssertEqual(PeriodAbsenceMark.Kind.noUsage.name, PayloadAbsence.noUsage)
        XCTAssertEqual(PeriodAbsenceMark.Kind.unpriced.name, PayloadAbsence.unpriced)
        XCTAssertNotEqual(PeriodAbsenceMark.Kind.noUsage.name, PeriodAbsenceMark.Kind.unpriced.name)
    }

    func testTheReadoutIsAnchoredOverItsBarAndClampedIntoThePlot() {
        // Centered on the bar when there is room on both sides.
        XCTAssertEqual(
            PeriodChartReadout.leadingX(columnCenter: 300, readoutWidth: 100, plotWidth: 600),
            250
        )
        // Never hangs off the left edge.
        XCTAssertEqual(
            PeriodChartReadout.leadingX(columnCenter: 10, readoutWidth: 100, plotWidth: 600),
            0
        )
        // Never hangs off the right edge.
        XCTAssertEqual(
            PeriodChartReadout.leadingX(columnCenter: 590, readoutWidth: 100, plotWidth: 600),
            500
        )
        // A label wider than the plot starts at the plot's left edge.
        XCTAssertEqual(
            PeriodChartReadout.leadingX(columnCenter: 300, readoutWidth: 800, plotWidth: 600),
            0
        )
    }

    func testReadoutTextJoinsThePeriodNameAndThePayloadValue() {
        XCTAssertEqual(
            PeriodChartReadout.text(period: "week of Jul 20", value: "~$69.97 · Partial subtotal"),
            "week of Jul 20 · ~$69.97 · Partial subtotal"
        )
    }

    func testDateBandStrideThinsLabelsOnlyAsFarAsItMust() {
        // 7 short labels in a wide plot: label every bucket.
        XCTAssertEqual(PeriodChartDateBand.stride(labelWidth: 50, count: 7, plotWidth: 700), 1)
        // 90 labels in the same plot: one in every 7.
        XCTAssertEqual(PeriodChartDateBand.stride(labelWidth: 50, count: 90, plotWidth: 700), 7)
        // Degenerate inputs never produce a zero or negative stride.
        XCTAssertEqual(PeriodChartDateBand.stride(labelWidth: 0, count: 10, plotWidth: 0), 1)
    }

    func testTheLastBucketIsAlwaysLabelled() {
        let indices = PeriodChartDateBand.labelledIndices(count: 10, stride: 4)
        XCTAssertTrue(indices.contains(0))
        XCTAssertTrue(indices.contains(9))
        XCTAssertEqual(PeriodChartDateBand.labelledIndices(count: 0, stride: 3), [])
    }

    func testTheLastLabelReplacesAnAdjacentOneRatherThanCollidingWithIt() {
        // 14 weekly buckets at stride 2: …, 10, 12, then 13. 12 and 13 are
        // adjacent columns, so two long "week of …" labels would overlap.
        let indices = PeriodChartDateBand.labelledIndices(count: 14, stride: 2)
        XCTAssertTrue(indices.contains(13))
        XCTAssertFalse(indices.contains(12))
        XCTAssertTrue(indices.contains(10))
        // Every labelled pair stays at least one stride apart.
        let sorted = indices.sorted()
        for (left, right) in zip(sorted, sorted.dropFirst()) {
            XCTAssertGreaterThanOrEqual(right - left, 2, "labels \(left) and \(right) would collide")
        }
    }
}

/// Share bars stay proportional; the only floor is one device pixel (K111).
final class MeterBarProportionTests: XCTestCase {
    func testASmallShareIsShorterThanALargerOneAtTheSameScale() {
        let track: CGFloat = 80
        let tiny = MeterBar.fillWidth(fraction: 0.0027, trackWidth: track, displayScale: 2)
        let small = MeterBar.fillWidth(fraction: 0.09, trackWidth: track, displayScale: 2)
        let large = MeterBar.fillWidth(fraction: 0.69, trackWidth: track, displayScale: 2)
        XCTAssertLessThan(tiny, small)
        XCTAssertLessThan(small, large)
        // 9% of an 80pt track is 7.2pt, not the old 6pt height-sized stub that
        // also swallowed every share below 7.5%.
        XCTAssertEqual(small, 7.2, accuracy: 0.001)
    }

    func testANonZeroShareIsNeverDrawnAsNothingAndNeverOverflows() {
        XCTAssertEqual(MeterBar.fillWidth(fraction: 0.000001, trackWidth: 80, displayScale: 2), 0.5)
        XCTAssertEqual(MeterBar.fillWidth(fraction: 0, trackWidth: 80, displayScale: 2), 0)
        XCTAssertEqual(MeterBar.fillWidth(fraction: 2, trackWidth: 80, displayScale: 2), 80)
    }
}

/// Rail details keep their named absences at any width (K69).
final class DashboardSignalDetailTests: XCTestCase {
    func testTheCompactFormLeadsWithAbsencesAndNeverDropsThem() {
        let detail = DashboardSignalDetail(
            absences: ["plan share unavailable", "won't calibrate at current ratio"],
            context: ["7-day limit", "provider reported", "resets in 3d 14h", "as of 2h ago"]
        )
        XCTAssertTrue(detail.full.hasPrefix("plan share unavailable"))
        XCTAssertTrue(detail.compact.contains("plan share unavailable"))
        XCTAssertTrue(detail.compact.contains("won't calibrate at current ratio"))
        XCTAssertFalse(detail.compact.hasSuffix("…"))
        XCTAssertLessThan(detail.compact.count, detail.full.count)
    }

    func testAPlainSentenceIsUnchangedByTheCompactForm() {
        let detail = DashboardSignalDetail.sentence("Waiting for the local glance projection.")
        XCTAssertEqual(detail.full, "Waiting for the local glance projection.")
        XCTAssertEqual(detail.compact, detail.full)
    }

    func testContextFitsTheBudgetWhenThereIsNoAbsence() {
        let detail = DashboardSignalDetail(
            absences: [],
            context: ["7-day limit", "provider reported", "resets in 3d 14h", "as of 2h ago"]
        )
        XCTAssertEqual(detail.compact.count <= DashboardSignalDetail.compactBudget, true)
        XCTAssertTrue(detail.compact.hasPrefix("7-day limit"))
    }
}
