// Draws the app icon and writes it as an .icns.
//
//   swift native/tools/make-icon.swift <output.icns>
//
// The mark is a page of claims: a stack of rules with one of them pulled out
// and marked, which is what the app does to a paper.

import AppKit
import Foundation

let outputPath = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "Doxograph.icns"
let side = 1024

func draw(into context: CGContext) {
    let full = CGRect(x: 0, y: 0, width: side, height: side)
    context.saveGState()

    // Rounded-rect plate with a warm-to-cool gradient behind it.
    let inset = full.insetBy(dx: CGFloat(side) * 0.09, dy: CGFloat(side) * 0.09)
    let plate = CGPath(roundedRect: inset,
                       cornerWidth: CGFloat(side) * 0.20,
                       cornerHeight: CGFloat(side) * 0.20,
                       transform: nil)
    context.addPath(plate)
    context.clip()

    let space = CGColorSpaceCreateDeviceRGB()
    let colors = [
        CGColor(red: 0.13, green: 0.16, blue: 0.24, alpha: 1),
        CGColor(red: 0.09, green: 0.11, blue: 0.16, alpha: 1),
    ] as CFArray
    if let gradient = CGGradient(colorsSpace: space, colors: colors, locations: [0, 1]) {
        context.drawLinearGradient(gradient,
                                   start: CGPoint(x: inset.minX, y: inset.maxY),
                                   end: CGPoint(x: inset.maxX, y: inset.minY),
                                   options: [])
    }

    // The stack of claims, with one pulled out and its quote beneath it.
    let left = inset.minX + inset.width * 0.18
    let width = inset.width * 0.64
    let height = inset.height * 0.052
    let gap = inset.height * 0.105
    let indent = inset.width * 0.06

    let quiet = CGColor(red: 0.62, green: 0.66, blue: 0.74, alpha: 1)
    let accent = CGColor(red: 0.98, green: 0.71, blue: 0.31, alpha: 1)
    let quoted = CGColor(red: 0.98, green: 0.71, blue: 0.31, alpha: 0.42)
    let widths: [CGFloat] = [1.0, 0.72, 0.88, 0.55]
    let highlight = 2

    func bar(_ rect: CGRect, _ color: CGColor) {
        context.setFillColor(color)
        context.addPath(CGPath(roundedRect: rect,
                               cornerWidth: rect.height / 2,
                               cornerHeight: rect.height / 2,
                               transform: nil))
        context.fillPath()
    }

    var y = inset.maxY - inset.height * 0.30
    for (index, scale) in widths.enumerated() {
        let pulled = index == highlight
        bar(CGRect(x: pulled ? left + indent : left, y: y, width: width * scale, height: height),
            pulled ? accent : quiet)
        if pulled {
            let quoteHeight = height * 0.6
            bar(CGRect(x: left + indent, y: y - gap * 0.64, width: width * 0.44, height: quoteHeight),
                quoted)
            y -= gap * 0.64
        }
        y -= gap
    }

    context.restoreGState()
}

guard let context = CGContext(data: nil, width: side, height: side, bitsPerComponent: 8,
                              bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
    FileHandle.standardError.write(Data("could not create a drawing context\n".utf8))
    exit(1)
}
draw(into: context)

guard let image = context.makeImage() else {
    FileHandle.standardError.write(Data("could not render the icon\n".utf8))
    exit(1)
}

let workDirectory = URL(fileURLWithPath: NSTemporaryDirectory())
    .appendingPathComponent("doxograph-icon-\(UUID().uuidString)")
let iconset = workDirectory.appendingPathComponent("Doxograph.iconset")
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)

let master = workDirectory.appendingPathComponent("icon.png")
let bitmap = NSBitmapImageRep(cgImage: image)
try bitmap.representation(using: .png, properties: [:])!.write(to: master)

// The sizes `iconutil` expects, each as a plain resize of the master.
let sizes: [(Int, String)] = [
    (16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png"),
]
for (pixels, name) in sizes {
    let sips = Process()
    sips.executableURL = URL(fileURLWithPath: "/usr/bin/sips")
    sips.arguments = ["-z", String(pixels), String(pixels), master.path,
                      "--out", iconset.appendingPathComponent(name).path]
    sips.standardOutput = FileHandle.nullDevice
    sips.standardError = FileHandle.nullDevice
    try sips.run()
    sips.waitUntilExit()
}

let iconutil = Process()
iconutil.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
iconutil.arguments = ["-c", "icns", iconset.path, "-o", outputPath]
try iconutil.run()
iconutil.waitUntilExit()
try? FileManager.default.removeItem(at: workDirectory)
exit(iconutil.terminationStatus)
