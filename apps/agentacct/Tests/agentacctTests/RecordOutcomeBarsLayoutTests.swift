import AppKit
import SwiftUI
import XCTest
@testable import agentacct

/// Geometry of the record page's Steps and Checks cards, measured from a real
/// render. The cards are drawn on a background no theme colour uses, so each
/// card's top and bottom edge is where a pixel column leaves and re-enters
/// that background.
final class RecordOutcomeBarsLayoutTests: XCTestCase {
    /// A self-checked pass, a bare claim and a blocked step with a failing
    /// check. The Steps legend fits on one line, and only Checks carries the
    /// "1 failing check needs attention" note, so Checks is the taller card.
    private var steps: [V1Step] {
        [
            step("self-checked", status: "completed", grade: "self_checked", checks: [check("pass", result: "passed")]),
            step("claimed", status: "completed", grade: "claimed", checks: []),
            step("blocked", status: "blocked", grade: nil, checks: [check("fail", result: "failed")]),
        ]
    }

    @MainActor
    func testSideBySideCardsShareOneTopAndBottomEdgeWhenOnlyChecksCarriesTheNote() throws {
        let render = try OutcomeBarsRender(steps: steps, width: 900)
        XCTAssertTrue(render.cardEdges(atX: 450).isEmpty, "at 900 pt the cards sit side by side, with the gap between them at the centre")
        let stepsCard = try XCTUnwrap(render.cardEdges(atX: 225).first, "no Steps card crosses x = 225")
        let checksCard = try XCTUnwrap(render.cardEdges(atX: 675).first, "no Checks card crosses x = 675")

        XCTAssertEqual(stepsCard.top, checksCard.top, accuracy: 0.5, "side by side, both cards start on one top edge")
        XCTAssertEqual(
            stepsCard.bottom,
            checksCard.bottom,
            accuracy: 0.5,
            "the Steps card ends at \(stepsCard.bottom) pt but the Checks card at \(checksCard.bottom) pt: side by side, both cards must end on one bottom edge"
        )
    }

    @MainActor
    func testSideBySideCardsDoNotStretchIntoExtraHeightTheParentOffers() throws {
        let natural = try OutcomeBarsRender(steps: steps, width: 900)
        let offeredMore = try OutcomeBarsRender(steps: steps, width: 900, height: 400)
        let tallest = try XCTUnwrap(natural.cardEdges(atX: 675).first, "no Checks card crosses x = 675")

        for (name, x) in [("Steps", CGFloat(225)), ("Checks", 675)] {
            let card = try XCTUnwrap(offeredMore.cardEdges(atX: x).first, "no \(name) card crosses x = \(x)")
            XCTAssertLessThanOrEqual(
                card.bottom,
                tallest.bottom + 0.5,
                "offered 400 pt, the \(name) card grows to \(card.bottom) pt: the row must stay at the taller card's own height (\(tallest.bottom) pt)"
            )
        }
    }

    @MainActor
    func testStackedCardsKeepTheirOwnHeights() throws {
        let natural = try OutcomeBarsRender(steps: steps, width: 360)
        let offeredMore = try OutcomeBarsRender(steps: steps, width: 360, height: 600)
        let naturalCards = natural.cardEdges(atX: 180)
        let offeredCards = offeredMore.cardEdges(atX: 180)
        XCTAssertEqual(naturalCards.count, 2, "at 360 pt the cards stack in one column")
        guard naturalCards.count == 2, offeredCards.count == 2 else { return }

        XCTAssertLessThan(
            naturalCards[0].height + 1,
            naturalCards[1].height,
            "stacked, the Steps card (\(naturalCards[0].height) pt) keeps its own height instead of matching Checks (\(naturalCards[1].height) pt)"
        )
        for (name, natural, offered) in zip3(["Steps", "Checks"], naturalCards, offeredCards) {
            XCTAssertEqual(
                offered.height,
                natural.height,
                accuracy: 0.5,
                "offered 600 pt, the stacked \(name) card is \(offered.height) pt tall instead of its own \(natural.height) pt"
            )
        }
    }

    private func step(_ id: String, status: String, grade: String?, checks: [V1Check]) -> V1Step {
        V1Step(
            workId: id,
            sectionId: nil,
            title: nil,
            latestStatus: status,
            kind: nil,
            phase: nil,
            startedAt: nil,
            updatedAt: nil,
            summary: nil,
            files: nil,
            blocker: nil,
            nextStep: nil,
            usage: nil,
            joinConfidence: nil,
            evidenceStatus: nil,
            evidenceGrade: grade,
            evidenceGradeReason: nil,
            models: nil,
            checks: checks
        )
    }

    private func check(_ id: String, result: String) -> V1Check {
        V1Check(
            eventId: id,
            createdAt: nil,
            evidenceType: "test",
            result: result,
            summary: nil,
            exitCode: result == "passed" ? 0 : 1,
            sourceType: "mcp_agent_reported",
            checkIdentity: id,
            supersessionState: nil,
            supersededByEventId: nil,
            resolutionScope: nil,
            resolutionSummary: nil,
            resolvesBlockedEventId: nil,
            files: nil,
            artifactRef: nil,
            artifactPath: nil,
            artifactUrl: nil,
            commandRedacted: nil,
            artifactPathRedacted: nil,
            artifactUrlRedacted: nil
        )
    }
}

private func zip3<A, B, C>(_ a: [A], _ b: [B], _ c: [C]) -> [(A, B, C)] {
    zip(a, zip(b, c)).map { ($0, $1.0, $1.1) }
}

/// One card's vertical extent down a pixel column, in points.
private struct CardEdges: Equatable {
    let top: CGFloat
    let bottom: CGFloat

    var height: CGFloat { bottom - top }
}

/// RecordOutcomeBars rendered at 2x, as the snapshot harness renders, on a
/// magenta background.
private struct OutcomeBarsRender {
    private static let scale: CGFloat = 2
    private let rgba: [UInt8]
    private let pixelsWide: Int
    private let pixelsHigh: Int

    @MainActor
    init(steps: [V1Step], width: CGFloat, height: CGFloat? = nil) throws {
        let view = RecordOutcomeBars(steps: steps)
            .frame(width: width, height: height, alignment: .top)
            .background(Color(red: 1, green: 0, blue: 1))
            .environment(\.colorScheme, .light)
        let renderer = ImageRenderer(content: view)
        renderer.scale = Self.scale
        renderer.colorMode = .nonLinear
        renderer.proposedSize = ProposedViewSize(width: width, height: height)
        let image = try XCTUnwrap(renderer.cgImage, "the outcome cards produced no image")

        let (wide, high) = (image.width, image.height)
        var rgba = [UInt8](repeating: 0, count: wide * high * 4)
        let drawn = rgba.withUnsafeMutableBytes { buffer -> Bool in
            guard let context = CGContext(
                data: buffer.baseAddress,
                width: wide,
                height: high,
                bitsPerComponent: 8,
                bytesPerRow: wide * 4,
                space: CGColorSpace(name: CGColorSpace.sRGB)!,
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
            ) else { return false }
            context.draw(image, in: CGRect(x: 0, y: 0, width: wide, height: high))
            return true
        }
        XCTAssertTrue(drawn, "the render could not be read back as RGBA pixels")
        self.rgba = rgba
        pixelsWide = wide
        pixelsHigh = high
    }

    /// Every card crossing the pixel column at `x`, top to bottom.
    func cardEdges(atX x: CGFloat) -> [CardEdges] {
        let column = min(max(Int(x * Self.scale), 0), pixelsWide - 1)
        var cards: [CardEdges] = []
        var cardTop: Int?
        for row in 0..<pixelsHigh {
            let insideCard = !isBackground(column: column, row: row)
            if insideCard, cardTop == nil { cardTop = row }
            if !insideCard, let top = cardTop {
                cards.append(CardEdges(top: CGFloat(top) / Self.scale, bottom: CGFloat(row) / Self.scale))
                cardTop = nil
            }
        }
        if let top = cardTop {
            cards.append(CardEdges(top: CGFloat(top) / Self.scale, bottom: CGFloat(pixelsHigh) / Self.scale))
        }
        return cards
    }

    /// The bitmap's first row is the image's top edge.
    private func isBackground(column: Int, row: Int) -> Bool {
        let offset = (row * pixelsWide + column) * 4
        let (red, green, blue) = (rgba[offset], rgba[offset + 1], rgba[offset + 2])
        return red > 240 && green < 16 && blue > 240
    }
}
