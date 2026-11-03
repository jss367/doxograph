import AppKit
import WebKit

private var replies: [Bool] = [], decisions: [Bool] = []
private var questions = 0, uploadQuestions = 0, healthRequests = 0
private var allowUpload = false, onHealth: (() -> Void)?
func consentProbeReply(_ allowed: Bool) { replies.append(allowed) }
func consentProbeWritingQuestion() -> Bool {
    questions += 1
    precondition(!decisions.isEmpty, "Unexpected repeated unsaved-writing question")
    return decisions.removeFirst()
}
func consentProbeUploadQuestion() -> Bool { uploadQuestions += 1; return allowUpload }
func consentProbeHealthReturned() { onHealth?() }

private final class ConsentHealthProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        precondition(request.url?.path == "/api/health", "Unexpected network request")
        healthRequests += 1
        let response = HTTPURLResponse(url:request.url!, statusCode:200, httpVersion:nil, headerFields:nil)!
        client?.urlProtocol(self, didReceive:response, cacheStoragePolicy:.notAllowed)
        client?.urlProtocol(self, didLoad:Data("{\"app\":\"doxograph\",\"jobs\":0,\"arriving\":0}".utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@main
private enum QuitWritingConsentProbe {
    static func pump(_ until: () -> Bool) {
        let deadline = Date().addingTimeInterval(8)
        while !until() && Date() < deadline {
            _ = RunLoop.current.run(mode:.default, before:Date().addingTimeInterval(0.01))
        }
        precondition(until(), "Inert consent probe timed out")
    }
    static func main() {
        URLProtocol.registerClass(ConsentHealthProtocol.self)
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        app.finishLaunching()
        let delegate = AppDelegate() // Never install it or start the library server.
        delegate.ready = true
        let window = delegate.window, web = window.webView
        web.loadHTMLString("""
          <!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'">
          <textarea id="draft">Synthetic draft</textarea>
          <script>
            window.doxographSaveDrafts = () => false;
            window.doxographQuitWritingSnapshot = () => document.getElementById('draft').value;
          </script>
          """, baseURL:nil)
        pump { !web.isLoading }
        func js(_ script: String) {
            var answered = false
            web.evaluateJavaScript(script + ";true") { _, error in precondition(error == nil); answered = true }
            pump { answered }
        }
        for mode in ["same-accept", "initial-cancel", "changed-cancel", "changed-accept", "unknown-snapshot", "upload-cancel", "upload-accept"] {
            questions = 0; uploadQuestions = 0; healthRequests = 0; replies = []; Uploader.inFlight = 0
            decisions = mode == "initial-cancel" ? [false] : mode == "changed-cancel" ? [true,false]
                : ["changed-accept", "unknown-snapshot"].contains(mode) ? [true,true] : [true]
            allowUpload = mode == "upload-accept"
            js("document.getElementById('draft').value = 'Synthetic draft'; window.doxographQuitWritingSnapshot = () => document.getElementById('draft').value;")
            if mode == "unknown-snapshot" { js("delete window.doxographQuitWritingSnapshot") }
            var changed = false
            onHealth = {
                precondition(window.editingFrozen)
                if mode.hasPrefix("changed-") && !changed {
                    changed = true
                    js("document.getElementById('draft').value = 'New asynchronously completed writing'")
                }
                if mode.hasPrefix("upload-") && !changed {
                    changed = true
                    // Reserve only; never read a file or start a transfer.
                    Uploader.reserveForUpload([URL(fileURLWithPath:"/synthetic-never-opened.pdf")])
                }
            }
            let reply = delegate.applicationShouldTerminate(app)
            if case .terminateLater = reply {} else { fatalError("Expected asynchronous save") }
            pump { !replies.isEmpty }
            let cancelled = ["initial-cancel", "changed-cancel", "upload-cancel"].contains(mode)
            precondition(replies == [!cancelled])
            precondition(questions == (["changed-cancel", "changed-accept", "unknown-snapshot"].contains(mode) ? 2 : 1))
            precondition(healthRequests == (mode == "initial-cancel" ? 0 : mode.hasPrefix("upload-") ? 2 : 1))
            precondition(uploadQuestions == (mode.hasPrefix("upload-") ? 1 : 0))
            if cancelled { pump { !window.editingFrozen } }
            else { precondition(window.editingFrozen); window.resumeEditingAfterCancelledQuit(); pump { !window.editingFrozen } }
            print("PASS: \(mode), actual AppDelegate save/probe/final-save, unchanged-only consent and independent upload decision")
        }
        print("PASS: no real quit, upload, server, provider, update, install or user data")
    }
}
