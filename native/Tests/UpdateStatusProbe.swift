import AppKit

private var pendingPrompts: [(NSApplication.ModalResponse) -> Void] = []
private var updateStarts = 0, terminationRequests = 0, serverStarts = 0
private var terminationReplies: [Bool] = []
/// Record the actual presentation calls without opening a dialog.
final class UpdateStatusProbeAlert: NSAlert {
    var sheetParent: NSWindow?
    var modalCalls = 0

    override func beginSheetModal(for sheetWindow: NSWindow,
        completionHandler handler: ((NSApplication.ModalResponse) -> Void)? = nil) {
        sheetParent = sheetWindow
        if let handler { pendingPrompts.append(handler) }
    }

    override func runModal() -> NSApplication.ModalResponse {
        modalCalls += 1
        return .alertSecondButtonReturn
    }
}
func updateStatusProbeStarted() { updateStarts += 1 }
func updateStatusProbeTerminate() { terminationRequests += 1 }
/// The answer to a terminateLater, recorded instead of ending a quit nobody began.
func updateStatusProbeReply(_ allowed: Bool) { terminationReplies.append(allowed) }
/// Counts the start instead of spawning a server.
func updateStatusProbeServerStarted() { serverStarts += 1 }
func updateStatusProbeForbiddenUpdater(command: String, repository: URL,
    progress: @escaping (String) -> Void,
    completion: @escaping (Result<Updater.Outcome, Updater.Failure>) -> Void) {
    fatalError("Inert probe attempted a real update")
}

@main
private enum UpdateStatusProbe {
    static func pump(_ until: () -> Bool) {
        let deadline = Date().addingTimeInterval(8)
        while !until() && Date() < deadline {
            _ = RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.01))
        }
        precondition(until(), "Inert update-status probe timed out")
    }

    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        app.finishLaunching()
        // No delegate registration, application launch, updater, server or real sheet.
        let delegate = AppDelegate(), window = delegate.window
        let hiddenAlert = UpdateStatusProbeAlert()
        var hiddenResponses: [NSApplication.ModalResponse] = []
        precondition(!window.window.isVisible)
        window.present(hiddenAlert) { hiddenResponses.append($0) }
        precondition(hiddenAlert.modalCalls == 1 && hiddenAlert.sheetParent == nil)
        precondition(hiddenResponses == [.alertSecondButtonReturn] && pendingPrompts.isEmpty)
        print("PASS: hidden window uses runModal and delivers its response")

        window.window.orderFront(nil)
        defer { window.window.orderOut(nil) }
        precondition(window.window.isVisible)
        let visibleAlert = UpdateStatusProbeAlert()
        var visibleResponses: [NSApplication.ModalResponse] = []
        window.present(visibleAlert) { visibleResponses.append($0) }
        precondition(visibleAlert.sheetParent === window.window && visibleAlert.modalCalls == 0)
        precondition(visibleResponses.isEmpty && pendingPrompts.count == 1)
        pendingPrompts.removeFirst()(.alertFirstButtonReturn)
        precondition(visibleResponses == [.alertFirstButtonReturn])
        print("PASS: visible window uses beginSheetModal and defers its response")

        let updateMenu = NSMenuItem(title: "Update Doxograph", action: #selector(AppDelegate.updateDoxograph(_:)), keyEquivalent: "")
        func finish(ready: Bool) {
            delegate.ready = ready
            delegate.updating = true
            window.showStatus("Previous update finished")
            delegate.finishUpdate(.success(Updater.Outcome(changes: "Synthetic native change", appRebuilt: true)), ownsServer: true)
            precondition(!delegate.updating && pendingPrompts.count == 1)
        }
        func answer(_ response: NSApplication.ModalResponse) {
            precondition(pendingPrompts.count == 1)
            pendingPrompts.removeFirst()(response)
        }
        /// The open prompt admits no update, by menu or by direct call.
        func expectUpdateBlocked() {
            let starts = updateStarts
            precondition(delegate.relaunchOffered)
            precondition(!delegate.validateMenuItem(updateMenu), "Update must be disabled under a relaunch prompt")
            delegate.updateDoxograph(nil)
            precondition(updateStarts == starts && !delegate.updating, "An update started under a relaunch prompt")
            precondition(window.status.stringValue == "Previous update finished")
        }

        finish(ready: true)
        expectUpdateBlocked()
        answer(.alertSecondButtonReturn)
        precondition(!delegate.relaunchOffered)
        precondition(window.statusBackground.isHidden, "Later should hide finished progress when ready")
        precondition(delegate.validateMenuItem(updateMenu), "Later should allow another update")
        delegate.updateDoxograph(nil) // Actual admission/status logic; command dispatch intercepted.
        precondition(delegate.updating && updateStarts == 1)
        precondition(window.status.stringValue == "Checking for updates…")
        delegate.updating = false
        print("PASS: Update is blocked under a relaunch prompt and admitted after Later")

        finish(ready: false)
        expectUpdateBlocked()
        answer(.alertSecondButtonReturn)
        precondition(!window.statusBackground.isHidden, "Later must retain startup status when not ready")
        print("PASS: Later keeps status when server is not ready")

        precondition(serverStarts == 0, "No server was failed, so nothing should have started one")
        delegate.serverFailed = true
        finish(ready: false)
        expectUpdateBlocked()
        precondition(serverStarts == 0, "The retry waits for the prompt's answer")
        answer(.alertSecondButtonReturn)
        precondition(serverStarts == 1, "Later must retry a failed server start at once")
        precondition(delegate.validateMenuItem(updateMenu), "A failed start leaves Update as the way out")
        delegate.serverFailed = false
        print("PASS: Later retries a failed server start immediately")

        // A page whose writing will not save, so the quit's first save asks,
        // and the probe's alert answers Don't Quit: a real decline, no server.
        let web = window.webView
        web.loadHTMLString("""
          <!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'">
          <script>window.doxographSaveDrafts = () => false;</script>
          """, baseURL: nil)
        pump { !web.isLoading }

        finish(ready: true)
        expectUpdateBlocked()
        answer(.alertFirstButtonReturn)
        precondition(!delegate.relaunchOffered && !delegate.updating)
        precondition(delegate.relaunchAfterQuit && terminationRequests == 1)
        // What NSApp.terminate would do next: the delegate's own answer.
        let reply = delegate.applicationShouldTerminate(app)
        guard case .terminateLater = reply else { fatalError("Expected the quit to wait on its save") }
        let starts = updateStarts
        precondition(!delegate.validateMenuItem(updateMenu), "Update must be disabled while the Relaunch quit is pending")
        delegate.updateDoxograph(nil)
        precondition(updateStarts == starts && !delegate.updating, "An update started under a pending Relaunch quit")
        precondition(window.status.stringValue == "Previous update finished")
        print("PASS: Update is blocked, by menu and by direct call, while the Relaunch quit is pending")

        pump { !terminationReplies.isEmpty }
        precondition(terminationReplies == [false], "The unsaved-writing question should have declined the quit")
        precondition(!delegate.relaunchAfterQuit, "A declined quit must call the relaunch off")
        pump { !window.editingFrozen }
        precondition(delegate.validateMenuItem(updateMenu), "A declined Relaunch quit should allow another update")
        delegate.updateDoxograph(nil)
        precondition(delegate.updating && updateStarts == starts + 1)
        delegate.updating = false
        print("PASS: a declined Relaunch quit admits updates again")
        print("PASS: no real updater, download, installation, server, quit or relaunch invoked")
    }
}
