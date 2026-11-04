import AppKit

private var pendingPrompts: [(NSApplication.ModalResponse) -> Void] = []
private var updateStarts = 0, terminationRequests = 0
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
        func later() {
            precondition(pendingPrompts.count == 1)
            pendingPrompts.removeFirst()(.alertSecondButtonReturn)
        }

        finish(ready: true)
        later()
        precondition(window.statusBackground.isHidden, "Later should hide finished progress when ready and idle")
        print("PASS: Later hides completed progress when ready and idle")

        finish(ready: false)
        later()
        precondition(!window.statusBackground.isHidden, "Later must retain startup status when not ready")
        print("PASS: Later keeps status when server is not ready")

        finish(ready: true)
        precondition(delegate.validateMenuItem(updateMenu), "Completed update should allow another update")
        delegate.updateDoxograph(nil) // Actual admission/status logic; command dispatch intercepted.
        precondition(delegate.updating && updateStarts == 1)
        precondition(!delegate.validateMenuItem(updateMenu))
        delegate.updateDoxograph(nil)
        precondition(updateStarts == 1, "An active update must reject a duplicate start")
        later() // The older sheet is dismissed after the newer update starts.
        precondition(delegate.updating)
        precondition(!window.statusBackground.isHidden, "Old Later callback hid a newer update's progress")
        precondition(window.status.stringValue == "Checking for updates…")
        precondition(!delegate.validateMenuItem(updateMenu))
        print("PASS: old Later callback preserves newer update progress and menu guard")

        delegate.updating = false
        finish(ready: true)
        pendingPrompts.removeFirst()(.alertFirstButtonReturn)
        precondition(delegate.relaunchAfterQuit && terminationRequests == 1)
        print("PASS: Relaunch intent retained with termination intercepted")
        print("PASS: no real updater, download, installation, server or relaunch invoked")
    }
}
