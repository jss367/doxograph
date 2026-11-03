import AppKit
import WebKit

private var navigationCalls = 0, timeoutQuestions = 0
private var allowTimedOutNavigation = false
func resumeProbeTimeoutChoice() -> Bool {
    timeoutQuestions += 1
    return allowTimedOutNavigation
}

final class ResumeProbeWebView: WKWebView {
    var held: (String, (@MainActor @Sendable (Any?, Error?) -> Void)?)?
    override var canGoBack: Bool { true }
    override func goBack() -> WKNavigation? { navigationCalls += 1; return nil }
    override func reload() -> WKNavigation? { navigationCalls += 1; return nil }
    override func evaluateJavaScript(_ script: String, completionHandler: (@MainActor @Sendable (Any?, Error?) -> Void)? = nil) {
        if script.contains("pageImports.resume()") { held = (script, completionHandler); return }
        super.evaluateJavaScript(script, completionHandler: completionHandler)
    }
    func restore(error: Bool = false) {
        guard let (script, done) = held else { fatalError("No pending restoration") }
        held = nil
        if error { done?(nil, NSError(domain:"inert-restoration-error", code:1)); return }
        super.evaluateJavaScript(script, completionHandler: done)
    }
}

@main
private enum EditingResumeProbe {
    static func pump(_ until: () -> Bool) {
        let deadline = Date().addingTimeInterval(8)
        while !until() && Date() < deadline {
            _ = RunLoop.current.run(mode:.default, before:Date().addingTimeInterval(0.01))
        }
        precondition(until(), "Inert resume probe timed out")
    }
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        app.finishLaunching()
        // Never register the delegate, start a server or invoke application termination.
        let delegate = AppDelegate()
        delegate.ready = true
        let window = delegate.window, web = window.webView as! ResumeProbeWebView
        web.loadHTMLString("""
          <!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'">
          <textarea id="draft">Keep this unsaved draft</textarea>
          <script>window.doxographSaveDrafts = () => false;</script>
          """, baseURL:nil)
        pump { !web.isLoading }
        func js(_ script: String) -> Any? {
            var answered = false, value: Any?
            web.evaluateJavaScript(script) { result, error in
                precondition(error == nil); value = result; answered = true
            }
            pump { answered }
            return value
        }
        func freeze() {
            var answered = false
            window.saveDrafts(freezeEditing:true) { _ in answered = true }
            pump { answered }
            precondition(window.editingFrozen)
        }
        let back = NSMenuItem(title:"Back", action:#selector(AppDelegate.goBack(_:)), keyEquivalent:"")
        let reload = NSMenuItem(title:"Reload", action:#selector(AppDelegate.reloadPage(_:)), keyEquivalent:"")
        func assertBlocked() {
            precondition(window.editingFrozen, "Native navigation thawed before DOM restoration callback")
            precondition(!delegate.validateMenuItem(back) && !delegate.validateMenuItem(reload))
            let calls = navigationCalls
            delegate.reloadPage(nil); delegate.goBack(nil)
            precondition(navigationCalls == calls)
            precondition(js("document.getElementById('draft').value") as? String == "Keep this unsaved draft")
        }
        // Stay/Don't Quit keeps the draft protected while the restoration request waits.
        freeze()
        window.resumeEditingAfterCancelledQuit()
        assertBlocked()
        precondition(js("document.body.inert") as? Bool == true)
        pump { timeoutQuestions > 0 }
        assertBlocked() // Keep Waiting cannot silently enable navigation.
        web.restore()
        pump { !window.editingFrozen }
        precondition(js("document.body.inert") as? Bool == false)
        precondition(delegate.validateMenuItem(back) && delegate.validateMenuItem(reload))
        print("PASS: delayed restoration blocks native menu/direct Back and Reload, timeout Keep Waiting, draft preserved")

        freeze()
        window.resumeEditingAfterCancelledQuit()
        assertBlocked()
        web.restore(error:true)
        pump { !window.editingFrozen }
        precondition(js("document.getElementById('draft').value") as? String == "Keep this unsaved draft")
        print("PASS: evaluated restoration error releases native lock and retains draft without navigating")

        freeze()
        let questions = timeoutQuestions
        allowTimedOutNavigation = true
        window.resumeEditingAfterCancelledQuit()
        assertBlocked()
        pump { !window.editingFrozen }
        precondition(timeoutQuestions == questions + 1)
        precondition(delegate.validateMenuItem(back) && delegate.validateMenuItem(reload))
        // A new quit can freeze again; a stale restoration callback must not thaw it.
        let late = web.held?.1
        web.restore()
        pump { js("document.body.inert") as? Bool == false }
        freeze()
        late?(nil, nil)
        assertBlocked()
        window.resumeEditingAfterCancelledQuit()
        web.restore()
        pump { !window.editingFrozen }
        print("PASS: explicit timeout Allow Navigation avoids permanent lock; late callback cannot thaw a newer quit")
        print("PASS: no real navigation, server, upload, provider, update or quit invoked")
    }
}
