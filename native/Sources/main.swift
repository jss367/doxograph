import AppKit

// Doxograph's window is the web app it already had; this wrapper starts the
// server, points a window at it, and makes the Dock icon a place to drop
// papers.
let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
