import AppKit
import WebKit

/// Save WebKit downloads, including exports generated as blob URLs in the page.
final class DownloadController: NSObject, WKDownloadDelegate {
    typealias DestinationPicker = (String, NSWindow?, @escaping (URL?) -> Void) -> Void
    typealias ErrorHandler = (Error, NSWindow?) -> Void

    private struct Transfer {
        let download: WKDownload
        var staged: URL?
        var destination: URL?
    }

    private var transfers: [ObjectIdentifier: Transfer] = [:]
    private let chooseDestination: DestinationPicker
    private let reportError: ErrorHandler

    init(chooseDestination: DestinationPicker? = nil, reportError: ErrorHandler? = nil) {
        self.chooseDestination = chooseDestination ?? Self.pickDestination
        self.reportError = reportError ?? Self.showError
        super.init()
    }

    func receive(_ download: WKDownload) {
        transfers[ObjectIdentifier(download)] = Transfer(download: download)
        download.delegate = self
    }

    func download(
        _ download: WKDownload, decideDestinationUsing response: URLResponse,
        suggestedFilename: String, completionHandler: @escaping (URL?) -> Void
    ) {
        let id = ObjectIdentifier(download)
        chooseDestination(suggestedFilename, download.webView?.window) { destination in
            guard let destination else {
                self.transfers.removeValue(forKey: id)
                completionHandler(nil)
                return
            }
            // WebKit requires a file that does not exist. Stage beside the
            // chosen file so an interrupted export cannot erase an older copy.
            let staged = destination.deletingLastPathComponent()
                .appendingPathComponent(".doxograph-download-\(UUID().uuidString)")
            self.transfers[id]?.staged = staged
            self.transfers[id]?.destination = destination
            completionHandler(staged)
        }
    }

    func downloadDidFinish(_ download: WKDownload) {
        guard let transfer = transfers.removeValue(forKey: ObjectIdentifier(download)),
              let staged = transfer.staged, let destination = transfer.destination else { return }
        do {
            let files = FileManager.default
            if files.fileExists(atPath: destination.path) {
                _ = try files.replaceItemAt(destination, withItemAt: staged)
            } else {
                try files.moveItem(at: staged, to: destination)
            }
        } catch {
            let failure = Self.restoreOriginal(after: error, to: destination)
            let original = (error as NSError).userInfo["NSFileOriginalItemLocationKey"] as? URL
            // A failed replacement can relocate the previous export, even to
            // the staging path. Preserve it there if recovery could not finish.
            if original?.standardizedFileURL != staged.standardizedFileURL {
                try? FileManager.default.removeItem(at: staged)
            }
            reportError(failure, download.webView?.window)
        }
    }

    static func restoreOriginal(after error: Error, to destination: URL) -> Error {
        let failure = error as NSError
        guard let original = failure.userInfo["NSFileOriginalItemLocationKey"] as? URL,
              !FileManager.default.fileExists(atPath: destination.path) else { return error }
        do {
            try FileManager.default.moveItem(at: original, to: destination)
            return error
        } catch let restorationError {
            var info = failure.userInfo
            info[NSLocalizedRecoverySuggestionErrorKey] =
                "The previous export was moved to \(original.path). Restore it to \(destination.path). " +
                "Automatic recovery failed: \(restorationError.localizedDescription)"
            return NSError(domain: failure.domain, code: failure.code, userInfo: info)
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        let transfer = transfers.removeValue(forKey: ObjectIdentifier(download))
        if let staged = transfer?.staged { try? FileManager.default.removeItem(at: staged) }
        let failure = error as NSError
        if failure.domain != NSURLErrorDomain || failure.code != NSURLErrorCancelled {
            reportError(error, download.webView?.window)
        }
    }

    private static func pickDestination(_ filename: String, window: NSWindow?, completion: @escaping (URL?) -> Void) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = filename
        panel.canCreateDirectories = true
        let selected: (NSApplication.ModalResponse) -> Void = { result in
            completion(result == .OK ? panel.url : nil)
        }
        if let window, window.isVisible { panel.beginSheetModal(for: window, completionHandler: selected) }
        else { panel.begin(completionHandler: selected) }
    }

    private static func showError(_ error: Error, window: NSWindow?) {
        let alert = NSAlert(error: error)
        alert.messageText = "Could not save the exported file"
        if let window, window.isVisible { alert.beginSheetModal(for: window) }
        else { alert.runModal() }
    }
}
