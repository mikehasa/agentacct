import AppKit
import SwiftUI

// The Stamped Tile mark — the selected agentacct brand mark, drawn from the
// master geometry rather than shipped as a bitmap so it stays crisp at every
// scale and picks up the theme's accent/canvas pair (light: cobalt tile with
// cream knockouts; dark: the lighter cobalt with near-black knockouts, exactly
// the lockup-dark master).
//
// Two cuts exist, mirroring the brand masters:
// * the 64-unit lockup cut (ring a + stem + double baseline rules), for
//   16pt-and-up in-app use;
// * the 16-unit simplified cut for tiny raster contexts (menu bar template).
//
// Brand rules honored here: flat color only (no gradients), the mark never
// wears green, the hollow ring is never filled.

/// The tile mark, scalable. Geometry is the 64-unit master:
/// tile (2,2,60,60) r14 · ring c(27,27) r10 stroke 6.5 · stem (35,12,6.5,26)
/// · rules (12,46,40,4) + (12,53,40,4) — the double baseline runs to the
/// tile's inset, "books closed".
struct StampedTileMark: View {
    var tile: Color = Theme.accent
    var knockout: Color = Theme.canvas

    var body: some View {
        Canvas { context, size in
            let s = min(size.width, size.height) / 64

            let tilePath = Path(
                roundedRect: CGRect(x: 2 * s, y: 2 * s, width: 60 * s, height: 60 * s),
                cornerRadius: 14 * s
            )
            context.fill(tilePath, with: .color(tile))

            let ring = Path(
                ellipseIn: CGRect(x: (27 - 10) * s, y: (27 - 10) * s, width: 20 * s, height: 20 * s)
            )
            context.stroke(ring, with: .color(knockout), lineWidth: 6.5 * s)

            context.fill(
                Path(CGRect(x: 35 * s, y: 12 * s, width: 6.5 * s, height: 26 * s)),
                with: .color(knockout)
            )
            context.fill(
                Path(CGRect(x: 12 * s, y: 46 * s, width: 40 * s, height: 4 * s)),
                with: .color(knockout)
            )
            context.fill(
                Path(CGRect(x: 12 * s, y: 53 * s, width: 40 * s, height: 4 * s)),
                with: .color(knockout)
            )
        }
        .accessibilityHidden(true)  // decorative beside the wordmark
    }
}

/// The brand lockup for the window top bar: mark + lowercase wordmark
/// (650 weight, −0.8 tracking, per the lockup master).
struct BrandLockup: View {
    var markSize: CGFloat = 20

    var body: some View {
        HStack(spacing: 7) {
            StampedTileMark()
                .frame(width: markSize, height: markSize)
            Text("agentacct")
                .font(Face.sansFont(14, .semibold))
                .tracking(-0.4)
                .foregroundStyle(Theme.ink)
        }
    }
}

/// Menu-bar mark: the 16-unit simplified cut rendered as a template image so
/// the system colors it for the bar (dark/light/tinted menu bars all work).
/// Geometry from mono-16: tile (1,1,14,14) r3.5 · ring c(6.6,6.4) r2.2
/// stroke 1.4 · stem (8.6,3,1.6,6) · rules (3,10.8,10,1.2) + (3,12.8,10,1.2).
enum MenuBarMark {
    static func templateImage(pointSize: CGFloat = 16) -> NSImage {
        let image = NSImage(size: NSSize(width: pointSize, height: pointSize), flipped: true) { rect in
            guard let cg = NSGraphicsContext.current?.cgContext else { return false }
            let s = rect.width / 16

            let tile = CGPath(
                roundedRect: CGRect(x: 1 * s, y: 1 * s, width: 14 * s, height: 14 * s),
                cornerWidth: 3.5 * s,
                cornerHeight: 3.5 * s,
                transform: nil
            )
            cg.setFillColor(.black)
            cg.addPath(tile)
            cg.fillPath()

            // Knockouts punch through the tile so the template alpha carries
            // the mark's counters.
            cg.setBlendMode(.clear)
            cg.setLineWidth(1.4 * s)
            cg.strokeEllipse(in: CGRect(x: (6.6 - 2.2) * s, y: (6.4 - 2.2) * s, width: 4.4 * s, height: 4.4 * s))
            cg.fill(CGRect(x: 8.6 * s, y: 3 * s, width: 1.6 * s, height: 6 * s))
            cg.fill(CGRect(x: 3 * s, y: 10.8 * s, width: 10 * s, height: 1.2 * s))
            cg.fill(CGRect(x: 3 * s, y: 12.8 * s, width: 10 * s, height: 1.2 * s))
            cg.setBlendMode(.normal)
            return true
        }
        image.isTemplate = true
        return image
    }
}
