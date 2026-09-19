import CoreGraphics
import XCTest
@testable import agentacct

final class WorkTimeCanvasLayoutTests: XCTestCase {
    private let full = WorkTimelineInterval(lower: 100, upper: 200)

    /// THE LANE decides the side of the axis, and packing only chooses a band
    /// within that lane. Before this, vertical position was an arbitrary
    /// packing slot: a work step and a check could swap sides between renders,
    /// so the axis split carried no fact at all.
    func testTheLaneDecidesTheSideAndPackingOnlyChoosesABandWithinIt() {
        let records = ["d", "c"].map { record($0, start: 125) }
            + ["b", "a"].map { checkRecord($0, start: 125) }
        let layout = WorkTimeCanvasLayout(records: records, window: full, width: 1000, height: 420)

        XCTAssertEqual(layout.items.count, 4)
        XCTAssertEqual(layout.bandCountPerSide, 2)
        XCTAssertEqual(Set(layout.items.filter(\.isAbove).flatMap(\.recordIDs)), ["c", "d"],
                       "the work lane is above the axis")
        XCTAssertEqual(Set(layout.items.filter { !$0.isAbove }.flatMap(\.recordIDs)), ["a", "b"],
                       "the check-evidence lane is below it")
        XCTAssertTrue(layout.items.allSatisfy { $0.anchorX == 250 && !$0.isCluster })
        assertReadable(layout, width: 1000, height: 420)
        for item in layout.items where !item.isAbove {
            XCTAssertGreaterThanOrEqual(item.frame.minY, layout.axisY + 44)
        }
    }

    /// Density inside one lane never borrows the other lane's bands, and a
    /// dense group never mixes the two: a card's side always says the same
    /// thing about every record on it.
    func testDensityNeverSpillsAcrossTheAxisOrMixesLanesInOneCard() {
        let records = (0..<6).map { record("step-\($0)", start: 125) }
            + [checkRecord("check", start: 125)]
        let layout = WorkTimeCanvasLayout(records: records, window: full, width: 1000, height: 420)

        XCTAssertEqual(Set(layout.items.flatMap(\.recordIDs)), Set(records.map(\.id)),
                       "no record is dropped when one lane is full")
        XCTAssertEqual(layout.items.filter { !$0.isAbove }.flatMap(\.recordIDs), ["check"],
                       "the lone check keeps its own lane while the work lane is crowded")
        for item in layout.items {
            let lanes = Set(item.recordIDs.map { $0.hasPrefix("check") })
            XCTAssertEqual(lanes.count, 1, "a group mixed two lanes onto one card")
        }
    }

    func testLayoutRetainsOffscreenRecordsAndCullingIsPresentationOnly() {
        let records = [
            record("far", start: 10, end: 20),
            record("before", start: 80, end: 99),
            record("crossing", start: 50, end: 250),
            record("left", start: 100),
            record("right", start: 200),
            record("after", start: 201),
            record("farAfter", start: 500),
            record("undated", start: nil),
            record("invalid", start: .nan),
        ]
        let layout = WorkTimeCanvasLayout(records: records, window: full, width: 1000, height: 420)

        // Every dated record keeps a stable placement, including records
        // outside the window: that is what lets panning translate cards
        // instead of regrouping them at the viewport edges.
        XCTAssertEqual(Set(layout.items.flatMap(\.recordIDs)),
            ["after", "before", "crossing", "far", "farAfter", "left", "right"])
        // The window's record count asks the SAME question the Activity
        // heading asks — "is this record's card drawn in this window?" (F5).
        // `crossing` starts half a window before the lower bound, so its card
        // is not in view; only its span line is, and `crossingSpans` below is
        // what accounts for it.
        XCTAssertEqual(layout.visibleRecordCount, 2)
        XCTAssertEqual(layout.visibleRecordCount,
                       records.filter { full.contains($0) && $0.start != nil && $0.start!.isFinite }.count,
                       "the canvas tally and the shared window rule are one rule")
        XCTAssertEqual(layout.undatedRecordIDs, ["invalid", "undated"])
        let crossing = layout.items.first { $0.recordIDs.contains("crossing") }
        XCTAssertEqual(crossing?.timeBounds, .init(lower: 50, upper: 250))
        // Anchors are no longer clipped to the viewport: 50 lies half a window
        // before the window's lower bound, so its position is offscreen.
        XCTAssertEqual(crossing?.anchorX, -500)
        let visible = layout.visibleCards(in: 1000)
        XCTAssertEqual(Set(visible.flatMap(\.recordIDs)), ["after", "before", "left", "right"],
            "Cards render only near the viewport: the accessibility tree never holds an invisible card")
        XCTAssertEqual(layout.crossingSpans(in: 1000).map(\.recordIDs), [["crossing"]],
            "A span crossing the window keeps its line on screen without pinning a card")
        assertReadable(layout, width: 1000, height: 420)
    }

    func testPanningTranslatesEveryItemWithoutRegrouping() {
        let records = (0..<300).map { record("event-\($0)", start: 100 + Double(($0 * 7919) % 10000) / 100) }
        let base = WorkTimeCanvasLayout(records: records, window: full, width: 1000, height: 420)
        let shifted = WorkTimeCanvasLayout(records: records, window: .init(lower: 130, upper: 230), width: 1000, height: 420)

        // Same items, same members, same sides: a pan only translates.
        XCTAssertEqual(base.items.map(\.id), shifted.items.map(\.id))
        XCTAssertEqual(base.items.map(\.recordIDs), shifted.items.map(\.recordIDs))
        let dx = 30 / full.span * 1000
        for (baseItem, shiftedItem) in zip(base.items, shifted.items) {
            XCTAssertEqual(shiftedItem.anchorX, baseItem.anchorX - dx, accuracy: 0.000_001)
            XCTAssertEqual(shiftedItem.frame.minX, baseItem.frame.minX - dx, accuracy: 0.000_001)
            XCTAssertEqual(shiftedItem.frame.minY, baseItem.frame.minY)
            XCTAssertEqual(shiftedItem.isAbove, baseItem.isAbove)
            XCTAssertEqual(shiftedItem.timeBounds, baseItem.timeBounds)
        }
        assertReadable(shifted, width: 1000, height: 420)
    }

    func testSixThousandCoincidentRecordsRemainBoundedAndFullyInspectable() {
        // Half in each lane, so both lanes' bands are exercised: six thousand
        // coincident records still resolve to the four cards the two lanes can
        // hold, and every one of them stays reachable through a group.
        let records = (0..<6000).map { $0.isMultiple(of: 2) ? record("event-\($0)", start: 125)
                                                            : checkRecord("event-\($0)", start: 125) }
        let layout = WorkTimeCanvasLayout(records: records, window: full, width: 1000, height: 420)

        XCTAssertEqual(layout.items.count, 4)
        XCTAssertTrue(layout.items.contains(where: \.isCluster))
        XCTAssertEqual(layout.items.reduce(0) { $0 + $1.count }, records.count)
        XCTAssertEqual(Set(layout.items.flatMap(\.recordIDs)), Set(records.map(\.id)))
        XCTAssertTrue(layout.items.allSatisfy { $0.timeBounds == .init(lower: 125, upper: 125) })
        let reordered = WorkTimeCanvasLayout(records: records.reversed(), window: full, width: 1000, height: 420)
        XCTAssertEqual(layout.items, reordered.items)
        assertReadable(layout, width: 1000, height: 420)
    }

    func testSparseRecordsAndFourNarrowCoincidentRecordsDoNotOvercollapse() {
        let sparse = (0..<8).map { record("event-\($0)", start: 105 + Double($0) * 12) }
        let broad = WorkTimeCanvasLayout(records: sparse, window: full, width: 1000, height: 420)
        XCTAssertEqual(broad.items.count, sparse.count)
        XCTAssertFalse(broad.items.contains(where: \.isCluster))
        let simultaneous = (0..<2).map { record("same-\($0)", start: 150) }
            + (0..<2).map { checkRecord("same-check-\($0)", start: 150) }
        let narrow = WorkTimeCanvasLayout(records: simultaneous, window: full, width: 180, height: 420)
        XCTAssertEqual(narrow.items.count, simultaneous.count)
        XCTAssertFalse(narrow.items.contains(where: \.isCluster))
        assertReadable(narrow, width: 180, height: 420)
    }

    func testDenseUnevenHistoryPacksWithoutOverlapOrLostMembers() {
        let records = (0..<6000).map { index in
            // Distinct deterministic times include multiple bursts at bin edges.
            record("event-\(index)", start: 100 + Double((index * 7919) % 10000) / 100)
        }
        for width in [180.0, 360, 1000, 1800] {
            let layout = WorkTimeCanvasLayout(records: records, window: full, width: width, height: 420)
            let memberIDs = layout.items.flatMap(\.recordIDs)
            XCTAssertEqual(memberIDs.count, records.count, "width \(width)")
            XCTAssertEqual(Set(memberIDs), Set(records.map(\.id)), "width \(width)")
            XCTAssertEqual(Set(layout.items.map(\.id)).count, layout.items.count)
            assertReadable(layout, width: width, height: 420)
        }
    }

    func testZoomRevealsDenseMembersWithoutInventingTheirTime() {
        let records = (0..<12).map { record("event-\($0)", start: 125 + Double($0)) }
        let broad = WorkTimeCanvasLayout(records: records, window: full, width: 1600, height: 420)
        let close = WorkTimeCanvasLayout(records: records, window: .init(lower: 124, upper: 137), width: 1600, height: 420)

        XCTAssertTrue(broad.items.contains(where: \.isCluster))
        XCTAssertGreaterThan(close.items.count, broad.items.count)
        XCTAssertEqual(Set(close.items.flatMap(\.recordIDs)), Set(records.map(\.id)))
        assertReadable(close, width: 1600, height: 420)
    }

    func testLargeTextUsesFewerBandsAndPreservesReadableLabelStrip() {
        let records = (0..<30).map { record("event-\($0)", start: 100 + Double($0) * 3) }
        for scale in [1.85, 2.3] {
            let height = max(420, WorkTimeCanvasLayout.minimumHeight(textScale: scale))
            let layout = WorkTimeCanvasLayout(records: records, window: full, width: 1000, height: height, textScale: scale)
            XCTAssertEqual(layout.bandCountPerSide, 1)
            XCTAssertEqual(layout.cardHeight, 80 * scale)
            XCTAssertEqual(Set(layout.items.flatMap(\.recordIDs)), Set(records.map(\.id)))
            for item in layout.items where !item.isAbove {
                XCTAssertGreaterThanOrEqual(item.frame.minY, layout.axisY + 44 * scale)
            }
            assertReadable(layout, width: 1000, height: height)
        }
    }

    func testInvalidGeometryHasNoNonfiniteFramesOrHiddenRecordCount() {
        let records = [record("valid", start: 125)]
        for dimensions in [(0.0, 420.0), (1000.0, 0.0), (.infinity, 420.0), (1000.0, .nan), (-1.0, -1.0)] {
            let layout = WorkTimeCanvasLayout(records: records, window: full, width: dimensions.0, height: dimensions.1)
            XCTAssertTrue(layout.items.isEmpty)
            XCTAssertEqual(layout.visibleRecordCount, 1)
            XCTAssertTrue(layout.axisY.isFinite)
        }
        let invalidScale = WorkTimeCanvasLayout(records: records, window: full, width: 1000, height: 420, textScale: .nan)
        XCTAssertEqual(invalidScale.cardHeight, 80)
        assertReadable(invalidScale, width: 1000, height: 420)
    }

    func testPanPreservesSpanAndSaturatesAtDomainEdges() {
        let window = WorkTimelineInterval(lower: 120, upper: 140)
        XCTAssertEqual(WorkTimeCanvasLayout.pannedWindow(window, by: 10, within: full), .init(lower: 130, upper: 150))
        XCTAssertEqual(WorkTimeCanvasLayout.pannedWindow(window, by: .greatestFiniteMagnitude, within: full), .init(lower: 180, upper: 200))
        XCTAssertEqual(WorkTimeCanvasLayout.pannedWindow(window, by: -.greatestFiniteMagnitude, within: full), .init(lower: 100, upper: 120))
        XCTAssertEqual(WorkTimeCanvasLayout.pannedWindow(window, by: .nan, within: full), window)
    }

    func testEdgeRevealIsHalfACardAndAlwaysBounded() {
        let window = WorkTimelineInterval(lower: 100, upper: 200)
        // Half a 200pt card in a 1000pt viewport: 10% of the span.
        XCTAssertEqual(WorkTimeCanvasLayout.edgeRevealTime(window: window, width: 1000, cardWidth: 200),
                       10, accuracy: 0.000_001)
        // Large text needs a larger margin: half of a 400pt card in 880pt.
        XCTAssertEqual(WorkTimeCanvasLayout.edgeRevealTime(window: window, width: 880, cardWidth: 400),
                       400.0 / 2 / 880 * 100, accuracy: 0.000_001)
        // Unknown geometry falls back to the cap; degenerate spans stay finite.
        XCTAssertEqual(WorkTimeCanvasLayout.edgeRevealTime(window: window),
                       WorkTimeCanvasLayout.maximumEdgeRevealFraction * 100, accuracy: 0.000_001)
        XCTAssertEqual(WorkTimeCanvasLayout.edgeRevealTime(window: .init(lower: 0, upper: .nan)),
                       WorkTimeCanvasLayout.maximumEdgeRevealFraction, accuracy: 0.000_001)
    }

    func testExpandedDomainAddsTheEdgeRevealWithinBounds() {
        let full = WorkTimelineInterval(lower: 100, upper: 200)
        XCTAssertEqual(WorkTimeCanvasLayout.expandedDomain(full, by: 10), .init(lower: 90, upper: 210))
        XCTAssertEqual(WorkTimeCanvasLayout.expandedDomain(full, by: 0), full)
        XCTAssertEqual(WorkTimeCanvasLayout.expandedDomain(full, by: .nan), full)
        XCTAssertEqual(WorkTimeCanvasLayout.expandedDomain(full, by: -.infinity), full)
    }

    func testExpandedDomainFallsBackWhenEndpointsOrSpanOverflow() {
        let belowZero = WorkTimelineInterval(lower: -.greatestFiniteMagnitude, upper: -.greatestFiniteMagnitude / 2)
        XCTAssertEqual(WorkTimeCanvasLayout.expandedDomain(belowZero, by: 1), belowZero)
        // Finite endpoints whose span overflows also fall back instead of
        // defeating the zoom-out bound.
        let wide = WorkTimelineInterval(lower: -8e307, upper: 8e307)
        XCTAssertEqual(WorkTimeCanvasLayout.expandedDomain(wide, by: 2.4e307), wide)
    }

    func testPanningReachesTheEdgeRevealSoTheFirstCardFitsInFull() throws {
        let full = WorkTimelineInterval(lower: 100, upper: 200)
        let window = WorkTimelineInterval(lower: 100, upper: 130)
        let reveal = WorkTimeCanvasLayout.edgeRevealTime(window: window, width: 1000, cardWidth: 200)
        let domain = WorkTimeCanvasLayout.expandedDomain(full, by: reveal)
        let earliest = WorkTimeCanvasLayout.pannedWindow(window, by: -1_000, within: domain)

        XCTAssertEqual(earliest.lower, full.lower - reveal, accuracy: 0.000_001)
        // A record at the recorded domain's lower bound lands at least half a
        // 200pt card inside a 1000pt viewport at the extreme position.
        let layout = WorkTimeCanvasLayout(records: [record("first", start: 100)],
            window: earliest, width: 1000, height: 420)
        let item = try XCTUnwrap(layout.visibleCards(in: 1000).first)
        XCTAssertGreaterThanOrEqual(item.frame.minX, 0, "the first card is fully inside at the extreme")
        XCTAssertEqual(item.frame.minX, 0, accuracy: 0.000_001)
    }

    func testZoomKeepsTheDataSpanBoundAndPreservesAnOverscrolledPosition() {
        let full = WorkTimelineInterval(lower: 100, upper: 200)
        let position = WorkTimeCanvasLayout.expandedDomain(full, by: 20)
        let window = WorkTimelineInterval(lower: 80, upper: 100)

        // Zooming out stops at the recorded span but keeps the margin position.
        let zoomedOut = WorkTimeCanvasLayout.zoomedWindow(window, factor: 0.001,
            anchorFraction: 1, within: full, positionDomain: position)
        XCTAssertEqual(zoomedOut.upper - zoomedOut.lower, full.span, accuracy: 0.000_001)
        XCTAssertLessThan(zoomedOut.lower, full.lower, "the overscrolled position survives zooming")

        // Zooming in at the margin edge does not snap back into the range.
        let zoomedIn = WorkTimeCanvasLayout.zoomedWindow(window, factor: 2,
            anchorFraction: 0, within: full, positionDomain: position)
        XCTAssertEqual(zoomedIn.lower, 80, accuracy: 0.000_001)
        XCTAssertEqual(zoomedIn.upper - zoomedIn.lower, 10, accuracy: 0.000_001)
    }

    func testZoomKeepsPointerTimeAnchoredUntilDomainEdgeRequiresClamping() {
        let window = WorkTimelineInterval(lower: 120, upper: 180)
        let zoomed = WorkTimeCanvasLayout.zoomedWindow(window, factor: 2, anchorFraction: 0.25, within: full)
        XCTAssertEqual(zoomed.upper - zoomed.lower, 30, accuracy: 0.000001)
        XCTAssertEqual(zoomed.lower + (zoomed.upper - zoomed.lower) * 0.25, 135, accuracy: 0.000001)
        XCTAssertEqual(WorkTimeCanvasLayout.zoomedWindow(window, factor: 0.001, anchorFraction: 0, within: full), full)
        let smallest = WorkTimeCanvasLayout.zoomedWindow(window, factor: .greatestFiniteMagnitude, anchorFraction: 0.25, within: full)
        XCTAssertEqual(smallest.upper - smallest.lower, WorkTimeCanvasLayout.minimumVisibleSpan, accuracy: 0.000001)
        XCTAssertEqual(smallest.lower + (smallest.upper - smallest.lower) * 0.25, 135, accuracy: 0.000001)
        XCTAssertEqual(WorkTimeCanvasLayout.zoomedWindow(window, factor: .nan, anchorFraction: 0, within: full), window)
    }

    /// The three measured task shapes, and what the window floor does to each.
    ///
    /// A task at or under the floor has NO narrower window, so every zoom, pan
    /// and handle drag resolves back to the domain it was given: the control is
    /// not slow or fiddly, it is absent, and `canNarrow` is what the surfaces
    /// ask before drawing one. Above the floor the range is real and stated in
    /// numbers here, so a later change to the floor cannot quietly make the
    /// canvas unzoomable without this failing.
    func testCanNarrowIsFalseExactlyWhenNoZoomCanChangeTheWindow() {
        // task_7bb028c1 — 3 events, 0.30s: shorter than the floor.
        let brief = WorkTimelineInterval(lower: 1_789_483_454, upper: 1_789_483_454.30)
        XCTAssertFalse(WorkTimeCanvasLayout.canNarrow(brief))
        let briefWindow = WorkTimeCanvasLayout.clampedWindow(brief, to: brief)
        for factor in [1.25, 2.0, 1_000_000.0, 0.8, 0.001] {
            let zoomed = WorkTimeCanvasLayout.zoomedWindow(briefWindow, factor: factor,
                anchorFraction: 0.5, within: brief)
            XCTAssertTrue(WorkTimeCanvasLayout.sameWindow(zoomed, briefWindow),
                          "factor \(factor) moved a window that has nowhere to go")
        }
        for delta in [-1_000.0, -0.05, 0.05, 1_000] {
            XCTAssertTrue(WorkTimeCanvasLayout.sameWindow(
                WorkTimeCanvasLayout.pannedWindow(briefWindow, by: delta, within: brief), briefWindow),
                "a window covering its whole domain cannot pan by \(delta)")
        }
        // A task exactly AT the floor is still not narrowable: the clamp floors
        // the span at the same 5 seconds it already shows.
        let atFloor = WorkTimelineInterval(lower: 0, upper: WorkTimeCanvasLayout.minimumVisibleSpan)
        XCTAssertFalse(WorkTimeCanvasLayout.canNarrow(atFloor))

        // task_5f7dbea9 — 9 events, 138.61s — and task_ef6818aa — 65 events,
        // 24,954.83s: both narrowable, to the same 5-second floor.
        for span in [138.61, 24_954.83] {
            let recorded = WorkTimelineInterval(lower: 1_789_483_454, upper: 1_789_483_454 + span)
            XCTAssertTrue(WorkTimeCanvasLayout.canNarrow(recorded), "span \(span)")
            let narrowest = WorkTimeCanvasLayout.zoomedWindow(recorded, factor: .greatestFiniteMagnitude,
                anchorFraction: 0.5, within: recorded)
            XCTAssertEqual(narrowest.span, WorkTimeCanvasLayout.minimumVisibleSpan, accuracy: 0.000_001)
            XCTAssertFalse(WorkTimeCanvasLayout.sameWindow(narrowest, recorded))
        }

        // Degenerate domains are never narrowable, and never crash the test.
        for broken in [WorkTimelineInterval(lower: 5, upper: 5),
                       .init(lower: .nan, upper: 1),
                       .init(lower: -.greatestFiniteMagnitude, upper: .greatestFiniteMagnitude)] {
            XCTAssertFalse(WorkTimeCanvasLayout.canNarrow(broken))
        }
    }

    /// The floor is the AXIS's resolution, not a taste — so pin it to the axis.
    /// At the floor the canvas still draws several one-second ticks; below it,
    /// the axis cannot divide further, which is the whole reason the floor is
    /// absolute rather than a fraction of the task (see `minimumVisibleSpan`).
    func testTheWindowFloorIsTheSmallestSpanTheAxisCanStillLabel() {
        let spacing = 140.0  // the canvas's own minimumSpacing at text scale 1
        for width in [880.0, 1_000, 1_400] {
            let floorWindow = WorkTimelineInterval(lower: 0, upper: WorkTimeCanvasLayout.minimumVisibleSpan)
            let ticks = WorkTimelineTimeAxis.ticks(in: floorWindow, width: width, minimumSpacing: spacing)
            XCTAssertEqual(ticks.step, 1, "the floor sits on the axis's finest step at width \(width)")
            XCTAssertGreaterThanOrEqual(ticks.times.count, 3,
                "an axis needs several labelled ticks to be a scale (width \(width))")
            // A window a fifth of the 0.30s task — what a relative floor would
            // allow — cannot be labelled at all: the axis has no finer step.
            let proportional = WorkTimelineTimeAxis.ticks(in: .init(lower: 0, upper: 0.30 / 5),
                width: width, minimumSpacing: spacing)
            XCTAssertEqual(proportional.step, 1)
            XCTAssertLessThanOrEqual(proportional.times.count, 1,
                "a proportional floor would zoom into an axis with nothing on it")
        }
    }

    func testWindowSpanNeverShrinksBelowTheReadableMinimum() {
        let window = WorkTimelineInterval(lower: 120, upper: 140)
        let zoomed = WorkTimeCanvasLayout.zoomedWindow(window, factor: 1_000_000, anchorFraction: 0.5, within: full)
        XCTAssertEqual(zoomed.upper - zoomed.lower, WorkTimeCanvasLayout.minimumVisibleSpan, accuracy: 0.000001)
        // Unless the whole recorded domain is smaller than the minimum.
        let tiny = WorkTimelineInterval(lower: 42, upper: 43)
        XCTAssertEqual(WorkTimeCanvasLayout.clampedWindow(tiny, to: tiny), tiny)
    }

    func testZoomByAbsoluteAnchorTimeMatchesFractionAndClampsOutsideAnchors() {
        let window = WorkTimelineInterval(lower: 120, upper: 180)
        XCTAssertEqual(WorkTimeCanvasLayout.zoomedWindow(window, factor: 2, anchorTime: 135, within: full),
                       WorkTimeCanvasLayout.zoomedWindow(window, factor: 2, anchorFraction: 0.25, within: full))
        // An anchor outside the window (the overview's domain-wide pointer)
        // clamps to the nearest window edge instead of skipping clamping.
        XCTAssertEqual(WorkTimeCanvasLayout.zoomedWindow(window, factor: 2, anchorTime: 500, within: full),
                       WorkTimeCanvasLayout.zoomedWindow(window, factor: 2, anchorFraction: 1, within: full))
        XCTAssertEqual(WorkTimeCanvasLayout.zoomedWindow(window, factor: 2, anchorTime: .nan, within: full),
                       WorkTimeCanvasLayout.zoomedWindow(window, factor: 2, anchorFraction: 0.5, within: full))
    }

    func testClampNormalizesReversedDegenerateAndNonfiniteWindows() {
        XCTAssertEqual(WorkTimeCanvasLayout.clampedWindow(.init(lower: 160, upper: 140), to: full), .init(lower: 140, upper: 160))
        XCTAssertEqual(WorkTimeCanvasLayout.clampedWindow(.init(lower: 200, upper: 200), to: full), .init(lower: 195, upper: 200),
            "A degenerate point window expands to the readable minimum span")
        XCTAssertEqual(WorkTimeCanvasLayout.clampedWindow(.init(lower: .nan, upper: 150), to: full), full)
        XCTAssertEqual(WorkTimeCanvasLayout.clampedWindow(full, to: .init(lower: 42, upper: 42)), .init(lower: 42, upper: 43))
        XCTAssertEqual(WorkTimeCanvasLayout.clampedWindow(full, to: .init(lower: -.greatestFiniteMagnitude, upper: .greatestFiniteMagnitude)), .init(lower: 0, upper: 1))
        XCTAssertEqual(WorkTimeCanvasLayout.clampedWindow(full, to: full, minimumSpan: .infinity), full)
    }

    func testLatestWindowKeepsNewestRecordVisibleWhenSevenDayDomainHasPadding() {
        let latest = 100.0 + 7 * 24 * 60 * 60
        let padding = 7.0 * 24 * 60 * 60 * 0.03
        let domain = WorkTimelineInterval(lower: 100 - padding, upper: latest + padding)
        let window = WorkTimeCanvasLayout.latestWindow(within: domain, latest: latest)

        XCTAssertLessThan(window.lower, latest)
        XCTAssertGreaterThan(window.upper, latest)
        XCTAssertEqual(window.upper, latest + 60, accuracy: 0.000001)
        XCTAssertEqual(window.upper - window.lower, 1800, accuracy: 0.000001)
        let invalidLatest: [Double?] = [nil, .nan, .infinity, -1, domain.upper + 1]
        for invalid in invalidLatest {
            XCTAssertEqual(WorkTimeCanvasLayout.latestWindow(within: domain, latest: invalid).upper, domain.upper)
        }
    }

    func testEveryComputedWindowShowsItsFirstAndLastCardInFull() throws {
        // A task shorter than the 30-minute default window opens on exactly
        // [first record, last record]: without the edge reveal the two cards a
        // reviewer most needs — the section and the terminal failed check —
        // open half off-canvas (K18).
        let first = 1_789_483_454.0
        let last = first + 36  // a 36-second task
        let recorded = WorkTimelineInterval(lower: first, upper: last)
        let records = [record("section", start: first, end: last), record("failed-check", start: last)]

        for width in [880.0, 1000, 1400, 2000] {
            let cardWidth = min(200, width)
            for requested in [
                WorkTimeCanvasLayout.latestWindow(within: recorded, latest: last, span: 1800),  // initial
                WorkTimeCanvasLayout.latestWindow(within: recorded, latest: last, span: 60),    // follow
                WorkTimeCanvasLayout.zoomedWindow(.init(lower: first + 10, upper: first + 20),
                    factor: 0.001, anchorFraction: 0.5, within: recorded),                      // zoom-out
                WorkTimeCanvasLayout.clampedWindow(
                    try XCTUnwrap(WorkTimelineRangeNavigation.focused(on: records[1])),
                    to: recorded),                                                              // focus
            ] {
                let window = WorkTimeCanvasLayout.edgeRevealedWindow(requested, width: width, cardWidth: cardWidth)
                let layout = WorkTimeCanvasLayout(records: records, window: window,
                    width: width, height: 420)
                for item in layout.items where item.timeBounds.lower >= requested.lower
                    && item.timeBounds.lower <= requested.upper {
                    XCTAssertGreaterThanOrEqual(item.frame.minX, -0.000_001,
                        "\(item.id) is cut at the left edge at width \(width)")
                    XCTAssertLessThanOrEqual(item.frame.maxX, width + 0.000_001,
                        "\(item.id) is cut at the right edge at width \(width)")
                }
                XCTAssertLessThanOrEqual(window.span, requested.span * 2 + 0.000_001,
                    "the reveal stays inside the capped fraction")
            }
        }
    }

    func testEdgeRevealedWindowIsBoundedForDegenerateGeometry() {
        let window = WorkTimelineInterval(lower: 100, upper: 200)
        // Unknown geometry falls back to the cap: half the span on each side.
        XCTAssertEqual(WorkTimeCanvasLayout.edgeRevealedWindow(window), .init(lower: 50, upper: 250))
        // A card at least as wide as the plot cannot be solved for; it caps.
        XCTAssertEqual(WorkTimeCanvasLayout.edgeRevealedWindow(window, width: 100, cardWidth: 200),
                       .init(lower: 50, upper: 250))
        // Half a 200pt card in a 1000pt plot: span / (1 - 0.2) = 125.
        let solved = WorkTimeCanvasLayout.edgeRevealedWindow(window, width: 1000, cardWidth: 200)
        XCTAssertEqual(solved.span, 125, accuracy: 0.000_001)
        XCTAssertEqual(solved.lower, 87.5, accuracy: 0.000_001)
        for broken in [WorkTimelineInterval(lower: .nan, upper: 1), .init(lower: 5, upper: 5)] {
            XCTAssertTrue(WorkTimeCanvasLayout.edgeRevealedWindow(broken, width: 1000, cardWidth: 200).span.isFinite)
        }
    }

    func testAFartherBandNeverHidesANearerCardsStem() {
        // Three records close enough that their cards overlap horizontally.
        // The third lands in a second band whose stem column is covered by a
        // card in the first — a reader could not match that card to its dot
        // (K18). Each record still keeps its OWN named card; the covered stem
        // is ROUTED around the occluder instead, so nothing is hidden and
        // nothing is merged into an unnamed group.
        // Two work-lane records whose cards overlap horizontally: the second
        // lands in the lane's farther band, whose stem column the nearer card
        // covers. (Three would no longer fit: a lane has two bands, and
        // packing may not borrow the other lane's — that is the lane rule.)
        let records = (0..<2).map { record("event-\($0)", start: 120 + Double($0)) }
        let layout = WorkTimeCanvasLayout(records: records, window: full, width: 1000, height: 420)

        XCTAssertEqual(Set(layout.items.flatMap(\.recordIDs)), Set(records.map(\.id)),
                       "no record is dropped by the stem rule")
        XCTAssertEqual(layout.items.count, 2, "two separable records keep two named cards")
        XCTAssertFalse(layout.items.contains(where: \.isCluster))
        for item in layout.items {
            let covering = layout.items.filter { other in
                guard other.id != item.id, other.isAbove == item.isAbove,
                      other.anchorX != item.anchorX,
                      abs(other.frame.midY - layout.axisY) < abs(item.frame.midY - layout.axisY)
                else { return false }
                return other.frame.minX <= item.anchorX && item.anchorX <= other.frame.maxX
            }
            guard !covering.isEmpty else {
                XCTAssertNil(item.stemDetourX, "\(item.id) has a clear column and needs no detour")
                continue
            }
            guard let lane = item.stemDetourX else {
                XCTFail("\(item.id)'s covered stem is not routed")
                continue
            }
            for other in covering {
                XCTAssertFalse(other.frame.minX <= lane && lane <= other.frame.maxX,
                    "\(item.id)'s routed stem still runs behind \(other.id)")
            }
            // The whole leader stays on screen: the dot, the routed column and
            // the return to the card edge.
            let route = layout.stemPoints(for: item)
            XCTAssertGreaterThan(route.count, 2, "\(item.id) draws a routed leader")
            XCTAssertEqual(Double(route[0].y), layout.axisY, "the leader starts on the axis dot")
            XCTAssertEqual(Double(route[0].x), item.anchorX, "the dot stays at the record's own time")
            XCTAssertEqual(Double(route[route.count - 1].x), item.anchorX,
                           "the leader meets the card at that time")
            XCTAssertEqual(route[route.count - 1].y, item.isAbove ? item.frame.maxY : item.frame.minY)
        }
        // Coincident records still stack: their stems coincide exactly, so a
        // farther card cannot make its neighbour's dot ambiguous.
        let coincident = (0..<2).map { record("same-\($0)", start: 125) }
            + (0..<2).map { checkRecord("same-check-\($0)", start: 125) }
        let stacked = WorkTimeCanvasLayout(records: coincident, window: full, width: 1000, height: 420)
        XCTAssertEqual(stacked.items.count, 4)
        XCTAssertFalse(stacked.items.contains(where: \.isCluster))
        XCTAssertTrue(stacked.items.allSatisfy { $0.stemDetourX == nil },
                      "a shared anchor needs no detour")
        // Only genuine density groups: more records at one instant than the
        // bands can hold still merge, and the group keeps every member.
        let dense = (0..<9).map { record("dense-\($0)", start: 130 + Double($0) * 0.01) }
        let packed = WorkTimeCanvasLayout(records: dense, window: full, width: 1000, height: 420)
        XCTAssertTrue(packed.items.contains(where: \.isCluster), "a genuinely dense burst still groups")
        XCTAssertEqual(Set(packed.items.flatMap(\.recordIDs)), Set(dense.map(\.id)))
    }

    func testCrossingClusterSpansAreRetainedForDrawing() {
        // Five records far before the window merge into a cluster whose
        // recorded extent crosses it; its members still need drawn spans even
        // though its card is offscreen.
        var records = (0..<5).map { record("old-\($0)", start: 50, end: 250) }
        records.append(record("inside", start: 190))
        let layout = WorkTimeCanvasLayout(records: records, window: full, width: 1000, height: 420)
        let crossing = layout.crossingSpans(in: 1000)
        XCTAssertTrue(crossing.contains {
            $0.isCluster && $0.timeBounds.upper >= full.lower && $0.timeBounds.lower <= full.upper
        }, "a crossing cluster stays available for span drawing")
        let crossingIDs = crossing.flatMap(\.recordIDs)
        XCTAssertEqual(Set(crossingIDs).count, crossingIDs.count, "no member is drawn twice")
    }

    private func assertReadable(
        _ layout: WorkTimeCanvasLayout, width: Double, height: Double,
        file: StaticString = #filePath, line: UInt = #line
    ) {
        for (index, item) in layout.items.enumerated() {
            XCTAssertTrue(item.frame.origin.x.isFinite && item.frame.origin.y.isFinite, file: file, line: line)
            XCTAssertGreaterThan(item.frame.width, 0, file: file, line: line)
            XCTAssertGreaterThan(item.frame.height, 0, file: file, line: line)
            // Horizontal positions are time-true: cards may sit partly or
            // fully outside the viewport. Vertical placement stays inside.
            XCTAssertGreaterThanOrEqual(item.frame.minY, -0.000001, file: file, line: line)
            XCTAssertLessThanOrEqual(item.frame.maxY, height + 0.000001, file: file, line: line)
            XCTAssertTrue(item.anchorX.isFinite, file: file, line: line)
            for other in layout.items.dropFirst(index + 1) {
                XCTAssertFalse(item.frame.intersects(other.frame), "\(item.id) overlaps \(other.id)", file: file, line: line)
            }
        }
        // Every record whose time intersects the window stays represented:
        // either as a visible card or as a drawn span crossing an edge.
        // Culling never feeds back into placement.
        let visible = layout.visibleCards(in: width)
        let crossing = layout.crossingSpans(in: width)
        for item in layout.items
        where item.timeBounds.upper >= layout.window.lower && item.timeBounds.lower <= layout.window.upper {
            XCTAssertTrue(visible.contains(item) || crossing.contains(item),
                "\(item.id) intersects the window but is neither rendered nor drawn", file: file, line: line)
        }
    }

    private func record(_ id: String, start: Double?, end: Double? = nil) -> WorkTimelineRecord {
        .init(id: id, laneID: "session", laneTitle: "Session", lineage: "Recorded session", kind: .step, title: id, start: start, end: end)
    }

    /// A record in the reducer's CHECK-EVIDENCE lane. The canvas's vertical
    /// axis states that lane, so the two builders place cards on opposite
    /// sides of the axis no matter how packing resolves collisions.
    private func checkRecord(_ id: String, start: Double?, end: Double? = nil) -> WorkTimelineRecord {
        .init(id: id, laneID: "session", laneTitle: "Session", lineage: "Recorded session",
              kind: .check, title: id, start: start, end: end, lane: "evidence", laneLabel: "Check evidence")
    }
}
