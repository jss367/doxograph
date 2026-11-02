import AppKit
import WebKit

/// Exercise the same bridge the Edit menu invokes, plus AppKit text fallback.
private final class UndoProbe: NSObject, NSApplicationDelegate {
    private var window: WebWindow!

    func applicationDidFinishLaunching(_ notification: Notification) {
        window = WebWindow()
        window.show()
        NSApp.activate(ignoringOtherApps: true)
        window.webView.loadHTMLString("""
          <html><body><button id="button">Color</button><textarea id="note"></textarea>
          <script>
          window.actions = [];
          window.doxographUndo = redo => {
            if (document.activeElement.id === 'note') return false;
            actions.push(redo ? 'redo' : 'undo'); return true;
          };
          </script></body></html>
          """, baseURL: nil)
        waitUntil({ !self.window.webView.isLoading && self.window.window.isKeyWindow }) {
          self.waitForJS("typeof window.doxographUndo === 'function'") {
            self.window.window.makeFirstResponder(self.window.webView)
            self.js("document.getElementById('button').focus()") { _ in
                self.window.performUndo(redo: false)
                self.waitForJS("actions.join(',') === 'undo'") {
                    self.window.performUndo(redo: true)
                    self.waitForJS("actions.join(',') === 'undo,redo'") {
                        self.testTextUndo()
                    }
                }
            }
          }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 15) { fatalError("Undo probe timed out") }
    }

    private func testTextUndo() {
        js("document.getElementById('note').focus(); document.execCommand('insertText', false, 'Typed note')") { _ in
            self.window.performUndo(redo: false)
            self.waitForJS("document.getElementById('note').value === ''") {
                self.window.performUndo(redo: true)
                self.waitForJS("document.getElementById('note').value === 'Typed note'") {
                    self.js("actions.join(',')") { value in
                        precondition(value as? String == "undo,redo", "Text undo reached application history")
                        print("PASS: native app undo/redo and text undo/redo")
                        NSApp.terminate(nil)
                    }
                }
            }
        }
    }
    private func js(_ script: String, then next: @escaping (Any?) -> Void) {
        window.webView.evaluateJavaScript(script) { value, error in
            precondition(error == nil, "JavaScript failed: \(String(describing: error))")
            next(value)
        }
    }
    private func waitForJS(_ condition: String, then next: @escaping () -> Void) {
        js(condition) { value in
            if value as? Bool == true { next() }
            else { DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { self.waitForJS(condition, then: next) } }
        }
    }
    private func waitUntil(_ ready: @escaping () -> Bool, then next: @escaping () -> Void) {
        if ready() { next() }
        else { DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { self.waitUntil(ready, then: next) } }
    }
}

@main private enum UndoProbeTest {
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let delegate = UndoProbe()
        app.delegate = delegate
        app.run()
    }
}
