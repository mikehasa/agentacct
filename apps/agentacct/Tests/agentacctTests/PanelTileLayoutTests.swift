import SwiftUI
import XCTest

@testable import agentacct

final class PanelTileLayoutTests: XCTestCase {
    @MainActor
    func testPanelTileHeightDoesNotDependOnDetailPresence() throws {
        for width in [226.0, 243.0, 266.0] {
            let withoutDetail = try renderedHeight(detail: nil, width: width)
            for detail in ["reference", "provider-reported account-wide usage"] {
                let withDetail = try renderedHeight(detail: detail, width: width)

                XCTAssertEqual(
                    withoutDetail,
                    withDetail,
                    "PanelTile height changed for detail at width \(width)"
                )
            }
        }
    }

    @MainActor
    func testPanelTileCanOmitReservedDetailSpaceForCompactRows() throws {
        let standardHeight = try renderedHeight(detail: nil, width: 243)
        let compactHeight = try renderedHeight(
            detail: nil,
            width: 243,
            reservesDetailSpace: false
        )

        XCTAssertLessThan(compactHeight, standardHeight)
    }

    @MainActor
    private func renderedHeight(
        detail: String?,
        width: CGFloat,
        reservesDetailSpace: Bool = true
    ) throws -> Int {
        let tile = PanelTile(
            label: "today · fresh tokens",
            value: "297.4k",
            detail: detail,
            reservesDetailSpace: reservesDetailSpace
        )
        .frame(width: width)
        .fixedSize(horizontal: false, vertical: true)
        .environment(\.colorScheme, .light)

        let renderer = ImageRenderer(content: tile)
        renderer.scale = 1
        return try XCTUnwrap(renderer.cgImage).height
    }
}
