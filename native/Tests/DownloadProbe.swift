import AppKit
import WebKit

/// Use the real navigation and download delegates without opening save dialogs.
private final class DownloadProbe: NSObject, NSApplicationDelegate {
    private var window: WebWindow!
    private let folder: URL
    private var picked: [String] = []
    private var failures = 0

    init(folder: URL) { self.folder = folder }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let downloads = DownloadController(chooseDestination: { filename, _, completion in
            self.picked.append(filename)
            completion(filename == "cancel.md" ? nil : self.folder.appendingPathComponent(filename))
        }, reportError: { error, _ in
            precondition(self.picked.last == "interrupted.md", "Unexpected download failure: \(error)")
            self.failures += 1
        })
        window = WebWindow(downloads: downloads)
        window.load(URL(string: CommandLine.arguments[1])!)
        waitUntil({ self.window.webView.isLoading == false }) { self.export(0) }
    }

    private let exports = [
        ("note.md", "# Research note", "text/markdown"),
        ("selection.csv", "Claim,Paper\nFinding,Study", "text/csv"),
        ("annotated.pdf", "%PDF-1.4 exported copy", "application/pdf"),
    ]

    private func export(_ index: Int) {
        if index == exports.count { return cancelExport() }
        let (name, content, type) = exports[index]
        let destination = folder.appendingPathComponent(name)
        // Cover both a new file and replacement. WebKit itself cannot write
        // to a destination that already exists.
        if index > 0 { try! Data("Previous version".utf8).write(to: destination) }
        let arguments = String(data: try! JSONSerialization.data(withJSONObject: [name, content, type]), encoding: .utf8)!
        window.webView.evaluateJavaScript("exportFile(...\(arguments))") { _, error in
            precondition(error == nil, "Could not start the export: \(String(describing: error))")
        }
        waitUntil({ (try? Data(contentsOf: destination)) == Data(content.utf8) }) {
            precondition(self.picked.last == name, "The download lost its suggested filename")
            precondition(self.failures == 0, "The download delegate reported an error")
            self.export(index + 1)
        }
    }

    private func cancelExport() {
        window.webView.evaluateJavaScript("exportFile('cancel.md', 'unsaved', 'text/markdown')")
        waitUntil({ self.picked.last == "cancel.md" }) {
            precondition(!FileManager.default.fileExists(atPath: self.folder.appendingPathComponent("cancel.md").path))
            self.interruptExport()
        }
    }

    private func interruptExport() {
        let destination = folder.appendingPathComponent("interrupted.md")
        let previous = Data("Keep the previous export".utf8)
        try! previous.write(to: destination)
        window.webView.evaluateJavaScript("const a = document.createElement('a'); a.href = '/interrupted'; a.download = 'interrupted.md'; a.click();") { _, error in
            precondition(error == nil, "Could not start the interrupted download: \(String(describing: error))")
        }
        waitUntil({ self.failures == 1 }) {
            precondition(try! Data(contentsOf: destination) == previous, "A failed download erased the previous export")
            precondition(self.window.webView.url?.absoluteString == CommandLine.arguments[1], "An export replaced the app page")
            let files = try! FileManager.default.contentsOfDirectory(atPath: self.folder.path)
            precondition(!files.contains { $0.hasPrefix(".doxograph-download-") }, "A download left a staging file")
            print("PASS: Markdown, CSV, PDF, replacement, cancellation, and interrupted downloads")
            NSApp.terminate(nil)
        }
    }

    private func waitUntil(_ ready: @escaping () -> Bool, then next: @escaping () -> Void) {
        if ready() { next(); return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { self.waitUntil(ready, then: next) }
    }
}

@main
private enum DownloadProbeTest {
    private static func testFileRecovery(in folder: URL) {
        let files = FileManager.default
        let original = folder.appendingPathComponent("relocated-original.md")
        let destination = folder.appendingPathComponent("recovered.md")
        let contents = Data("Previous export".utf8)
        let failure = NSError(domain: NSCocoaErrorDomain, code: NSFileWriteUnknownError,
                              userInfo: ["NSFileOriginalItemLocationKey": original])

        try! contents.write(to: original)
        let restored = DownloadController.restoreOriginal(after: failure, to: destination) as NSError
        precondition(restored === failure)
        precondition(try! Data(contentsOf: destination) == contents, "The previous export was not restored")
        precondition(!files.fileExists(atPath: original.path))

        // Recovery must not overwrite a file another writer put at the destination.
        try! contents.write(to: original)
        let concurrent = Data("Another writer's export".utf8)
        try! concurrent.write(to: destination)
        let occupied = DownloadController.restoreOriginal(after: failure, to: destination) as NSError
        let occupiedSuggestion = occupied.userInfo[NSLocalizedRecoverySuggestionErrorKey] as? String
        precondition(occupiedSuggestion?.contains(original.path) == true, "An occupied destination must not hide the original's location")
        precondition(try! Data(contentsOf: destination) == concurrent)
        precondition(try! Data(contentsOf: original) == contents)

        // A missing parent makes restoration fail deterministically, even as root.
        let unavailable = folder.appendingPathComponent("missing-folder/recovered.md")
        let recoveryFailure = DownloadController.restoreOriginal(after: failure, to: unavailable) as NSError
        let suggestion = recoveryFailure.userInfo[NSLocalizedRecoverySuggestionErrorKey] as? String
        precondition(suggestion?.contains(original.path) == true, "Recovery errors must identify the previous export")
        precondition(try! Data(contentsOf: original) == contents)

        let ordinaryFailure = NSError(domain: NSCocoaErrorDomain, code: NSFileWriteUnknownError)
        precondition((DownloadController.restoreOriginal(after: ordinaryFailure, to: unavailable) as NSError) === ordinaryFailure)
    }

    static func main() {
        testFileRecovery(in: URL(fileURLWithPath: CommandLine.arguments[2]))
        DispatchQueue.global().asyncAfter(deadline: .now() + 20) {
            fputs("FAIL: native download did not complete\n", stderr)
            exit(1)
        }
        let application = NSApplication.shared
        let delegate = DownloadProbe(folder: URL(fileURLWithPath: CommandLine.arguments[2]))
        application.delegate = delegate
        application.setActivationPolicy(.prohibited)
        application.run()
    }
}
