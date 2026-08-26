#!/usr/bin/env swift
// Generate Resources/AppIcon.icns from the Stamped Tile brand geometry.
//
// Deterministic vector redraw of the brand masters (no bitmap inputs):
// * 128px and up use the 64-unit lockup cut — tile (2,2,60,60) r14, ring
//   c(27,27) r10 stroke 6.5, stem (35,12,6.5,26), rules (12,46,40,4)+(12,53,40,4);
// * 16/32px use the simplified 16-unit cut from mono-16 so counters stay open
//   at favicon scale.
// Colors are the light brand pair (cobalt #245BDB tile, cream #F4F1E9
// knockouts) — app icons do not theme-switch.
//
// Usage:  cd apps/agentacct && swift Scripts/generate-app-icon.swift
// Writes: Resources/AppIcon.icns (via a temporary .iconset + iconutil)

import AppKit
import Foundation

let cobalt = CGColor(srgbRed: 0x24 / 255.0, green: 0x5B / 255.0, blue: 0xDB / 255.0, alpha: 1)
let cream = CGColor(srgbRed: 0xF4 / 255.0, green: 0xF1 / 255.0, blue: 0xE9 / 255.0, alpha: 1)

/// macOS icons leave a margin around the glyph: the system grid puts a
/// 1024-canvas icon's rounded square at 100..924. The tile IS the icon shape,
/// so scale the 60-unit tile to 824/1024 of the canvas and center it.
func drawLargeCut(_ cg: CGContext, canvas: CGFloat) {
    let tileSide = canvas * 824.0 / 1024.0
    let s = tileSide / 60.0  // one design unit
    let origin = (canvas - tileSide) / 2

    func rect(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat) -> CGRect {
        // Design coordinates are top-left origin (SVG); CG is bottom-left.
        CGRect(x: origin + (x - 2) * s, y: canvas - origin - (y - 2 + h) * s, width: w * s, height: h * s)
    }

    let tile = CGPath(
        roundedRect: rect(2, 2, 60, 60),
        cornerWidth: 14 * s,
        cornerHeight: 14 * s,
        transform: nil
    )
    cg.setFillColor(cobalt)
    cg.addPath(tile)
    cg.fillPath()

    cg.setStrokeColor(cream)
    cg.setLineWidth(6.5 * s)
    cg.strokeEllipse(in: rect(27 - 10, 27 - 10, 20, 20))
    cg.setFillColor(cream)
    cg.fill(rect(35, 12, 6.5, 26))
    cg.fill(rect(12, 46, 40, 4))
    cg.fill(rect(12, 53, 40, 4))
}

/// The mono-16 simplified cut, same margin treatment.
func drawSmallCut(_ cg: CGContext, canvas: CGFloat) {
    let tileSide = canvas * 824.0 / 1024.0
    let s = tileSide / 14.0
    let origin = (canvas - tileSide) / 2

    func rect(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat) -> CGRect {
        CGRect(x: origin + (x - 1) * s, y: canvas - origin - (y - 1 + h) * s, width: w * s, height: h * s)
    }

    let tile = CGPath(
        roundedRect: rect(1, 1, 14, 14),
        cornerWidth: 3.5 * s,
        cornerHeight: 3.5 * s,
        transform: nil
    )
    cg.setFillColor(cobalt)
    cg.addPath(tile)
    cg.fillPath()

    cg.setStrokeColor(cream)
    cg.setLineWidth(1.4 * s)
    cg.strokeEllipse(in: rect(6.6 - 2.2, 6.4 - 2.2, 4.4, 4.4))
    cg.setFillColor(cream)
    cg.fill(rect(8.6, 3, 1.6, 6))
    cg.fill(rect(3, 10.8, 10, 1.2))
    cg.fill(rect(3, 12.8, 10, 1.2))
}

func renderPNG(pixels: Int, to url: URL) throws {
    let space = CGColorSpace(name: CGColorSpace.sRGB)!
    guard let cg = CGContext(
        data: nil,
        width: pixels,
        height: pixels,
        bitsPerComponent: 8,
        bytesPerRow: 0,
        space: space,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else { throw NSError(domain: "icon", code: 1) }

    if pixels <= 32 {
        drawSmallCut(cg, canvas: CGFloat(pixels))
    } else {
        drawLargeCut(cg, canvas: CGFloat(pixels))
    }

    guard let image = cg.makeImage() else { throw NSError(domain: "icon", code: 2) }
    let rep = NSBitmapImageRep(cgImage: image)
    guard let png = rep.representation(using: .png, properties: [:]) else {
        throw NSError(domain: "icon", code: 3)
    }
    try png.write(to: url)
}

let fm = FileManager.default
let scriptDir = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent()
let appDir = scriptDir.deletingLastPathComponent()
let resources = appDir.appendingPathComponent("Resources")
let iconset = fm.temporaryDirectory.appendingPathComponent("AppIcon-\(UUID().uuidString).iconset")
try fm.createDirectory(at: iconset, withIntermediateDirectories: true)
try fm.createDirectory(at: resources, withIntermediateDirectories: true)

// (filename, pixel size)
let variants: [(String, Int)] = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]
for (name, pixels) in variants {
    try renderPNG(pixels: pixels, to: iconset.appendingPathComponent(name))
}

let icns = resources.appendingPathComponent("AppIcon.icns")
let iconutil = Process()
iconutil.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
iconutil.arguments = ["-c", "icns", iconset.path, "-o", icns.path]
try iconutil.run()
iconutil.waitUntilExit()
guard iconutil.terminationStatus == 0 else {
    fatalError("iconutil failed with status \(iconutil.terminationStatus)")
}
try? fm.removeItem(at: iconset)
print("wrote \(icns.path)")
