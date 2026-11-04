import AppKit

private var pendingPrompts: [(NSApplication.ModalResponse) -> Void] = []
private var updateStarts = 0, terminationRequests = 0, serverStarts = 0
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
/// Counts the start instead of spawning a server.
func updateStatusProbeServerStarted() { serverStarts += 1 }
func updateStatusProbeForbiddenUpdater(command: String, repository: URL,
    progress: @escaping (String) -> Void,
    completion: @escaping (Result<Updater.Outcome, Updater.Failure>) -> Void) {
    fatalError("Inert probe attempted a real update")
}

@main
private enum UpdateStatusProbe {
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

        finish(ready: true)
        expectUpdateBlocked()
        answer(.alertFirstButtonReturn)
        precondition(!delegate.relaunchOffered && !delegate.updating)
        precondition(delegate.relaunchAfterQuit && terminationRequests == 1)
        precondition(delegate.validateMenuItem(updateMenu), "A Relaunch whose quit is declined should allow another update")
        print("PASS: Relaunch requests termination with no update running, then admits updates again")
        print("PASS: no real updater, download, installation, server or relaunch invoked")
    }
}
