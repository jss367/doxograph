import AppKit
import WebKit

/// The window: a web view pointed at the local server, plus the small amount of
/// native behaviour a page loses when it stops being a browser tab.
final class WebWindow: NSObject, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    let window: NSWindow
    let webView: WKWebView

    /// Files dropped before the server was ready, replayed once it is.
    var onFilesDropped: (([URL]) -> Void)?

    private let status: NSTextField
    private var auxiliaryWindows: [NSWindow] = []
    private var host: String?

    override init() {
        let configuration = WKWebViewConfiguration()
        webView = WKWebView(frame: .zero, configuration: configuration)

        status = NSTextField(labelWithString: "Starting Doxograph…")
        status.font = .systemFont(ofSize: 13)
        status.textColor = .secondaryLabelColor
        status.alignment = .center

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1180, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false)
        window.title = "Doxograph"
        window.titlebarAppearsTransparent = false
        window.minSize = NSSize(width: 720, height: 480)
        window.setFrameAutosaveName("DoxographMain")

        super.init()

        // Constructed with an explicit frame so `init(frame:)`, and with it the
        // registration for dragged files, is the initializer that runs.
        let content = DropView(frame: window.contentLayoutRect)
        content.autoresizingMask = [.width, .height]
        content.onDrop = { [weak self] urls in
            self?.onFilesDropped?(urls)
            return true
        }

        webView.frame = content.bounds
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = self
        webView.uiDelegate = self
        content.addSubview(webView)

        status.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(status)
        NSLayoutConstraint.activate([
            status.centerXAnchor.constraint(equalTo: content.centerXAnchor),
            status.centerYAnchor.constraint(equalTo: content.centerYAnchor),
        ])

        window.contentView = content
        window.delegate = self
        if window.frame.origin == .zero { window.center() }
    }

    func show() {
        window.makeKeyAndOrderFront(nil)
    }

    func load(_ url: URL) {
        host = url.host
        status.isHidden = true
        webView.load(URLRequest(url: url))
    }

    func showStatus(_ text: String) {
        status.stringValue = text
        status.isHidden = false
    }

    // MARK: - Navigation

    private func isLocal(_ url: URL) -> Bool {
        guard let urlHost = url.host else { return url.scheme == "about" }
        return urlHost == host || urlHost == "127.0.0.1" || urlHost == "localhost"
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else { return decisionHandler(.allow) }
        if url.isFileURL {
            // A file dropped on a part of the page that does not handle drops
            // makes the web view try to navigate to it. Adding the paper is
            // what the drop meant, so take it as an upload instead.
            decisionHandler(.cancel)
            if Uploader.isPDF(url) { onFilesDropped?([url]) }
        } else if isLocal(url) {
            decisionHandler(.allow)
        } else {
            // arXiv and DOI links belong in the browser, not in a window with
            // no address bar and no way back.
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        showStatus("Could not load the app: \(error.localizedDescription)")
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        showStatus("Could not reach the server: \(error.localizedDescription)")
    }

    /// The page opens PDFs and BibTeX with `target="_blank"`. In a browser those
    /// become tabs; here a local one becomes its own window and anything else
    /// goes to the default browser.
    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        guard let url = navigationAction.request.url else { return nil }
        guard isLocal(url) else {
            NSWorkspace.shared.open(url)
            return nil
        }

        let child = WKWebView(frame: NSRect(x: 0, y: 0, width: 900, height: 1000),
                              configuration: configuration)
        child.navigationDelegate = self
        child.uiDelegate = self
        let auxiliary = NSWindow(contentRect: child.frame,
                                 styleMask: [.titled, .closable, .miniaturizable, .resizable],
                                 backing: .buffered,
                                 defer: false)
        auxiliary.title = url.lastPathComponent
        auxiliary.contentView = child
        auxiliary.isReleasedWhenClosed = false
        auxiliary.center()
        auxiliary.makeKeyAndOrderFront(nil)
        auxiliaryWindows.append(auxiliary)
        return child
    }

    func webViewDidClose(_ webView: WKWebView) {
        guard let index = auxiliaryWindows.firstIndex(where: { $0.contentView === webView }) else { return }
        auxiliaryWindows[index].close()
        auxiliaryWindows.remove(at: index)
    }

    // MARK: - Page dialogs
    //
    // Deleting a claim, deleting a paper and retagging everything all go
    // through `confirm()`. A web view with no UI delegate answers false without
    // asking, so without these the buttons would quietly do nothing.

    func webView(
        _ webView: WKWebView,
        runJavaScriptAlertPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping () -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        present(alert) { _ in completionHandler() }
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptConfirmPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (Bool) -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        present(alert) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptTextInputPanelWithPrompt prompt: String,
        defaultText: String?,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (String?) -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = prompt
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
        field.stringValue = defaultText ?? ""
        alert.accessoryView = field
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        present(alert) { completionHandler($0 == .alertFirstButtonReturn ? field.stringValue : nil) }
    }

    private func present(_ alert: NSAlert, then handle: @escaping (NSApplication.ModalResponse) -> Void) {
        if window.isVisible {
            alert.beginSheetModal(for: window, completionHandler: handle)
        } else {
            handle(alert.runModal())
        }
    }
}

/// Catches file drops the page itself did not take.
///
/// The web view sits on top and gets first refusal, so when the page's own drop
/// handler runs this never fires; it is the safety net for a drop that lands on
/// a part of the window the page does not cover.
final class DropView: NSView {
    var onDrop: (([URL]) -> Bool)?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        registerForDraggedTypes([.fileURL])
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("not used") }

    private func pdfs(in info: NSDraggingInfo) -> [URL] {
        let options: [NSPasteboard.ReadingOptionKey: Any] = [.urlReadingFileURLsOnly: true]
        let urls = info.draggingPasteboard.readObjects(forClasses: [NSURL.self], options: options)
        return (urls as? [URL] ?? []).filter(Uploader.isPDF)
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        pdfs(in: sender).isEmpty ? [] : .copy
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        let files = pdfs(in: sender)
        guard !files.isEmpty else { return false }
        return onDrop?(files) ?? false
    }
}
