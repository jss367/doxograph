// Renders the app icon from native/icon.png and writes it as an .icns.
//
//   swift native/tools/make-icon.swift <output.icns> [icon.png]
//
// The artwork is a scroll of claims fed by a stack of source lines, drawn as a
// square with its own background. macOS icons sit on a transparent canvas,
// inset from the edges and clipped to the system's rounded square, so this
// places the artwork on that grid rather than filling the whole tile.

import AppKit
import Foundation

let outputPath = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "Doxograph.icns"
let scriptURL = URL(fileURLWithPath: CommandLine.arguments[0])
let artworkPath = CommandLine.arguments.count > 2
    ? CommandLine.arguments[2]
    : scriptURL.deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("icon.png").path
let side = 1024

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

guard let artwork = NSImage(contentsOfFile: artworkPath),
      let source = artwork.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    fail("could not read the artwork at \(artworkPath)")
}

guard let context = CGContext(data: nil, width: side, height: side, bitsPerComponent: 8,
                              bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
    fail("could not create a drawing context")
}

// Apple's icon grid: the tile occupies 824 of 1024 points, with corners rounded
// at roughly 22% of its width. Clipping there also trims the artwork's own
// rounded corners, so no background leaks past the shape.
let full = CGRect(x: 0, y: 0, width: side, height: side)
let tile = full.insetBy(dx: CGFloat(side) * 100 / 1024, dy: CGFloat(side) * 100 / 1024)
let radius = tile.width * 0.2237
context.addPath(CGPath(roundedRect: tile, cornerWidth: radius, cornerHeight: radius, transform: nil))
context.clip()
context.interpolationQuality = .high
context.draw(source, in: tile)

guard let image = context.makeImage() else {
    fail("could not render the icon")
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
