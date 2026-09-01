import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuItemValidation {
    private let server = ServerController()
    private let window = WebWindow()
    private var ready = false
    /// PDFs dropped on the Dock icon before the server answered. A drop can
    /// even be what launched the app, so this fills up before the window exists.
    private var pendingDrops: [URL] = []

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = buildMenu()
        window.onFilesDropped = { [weak self] urls in self?.take(urls) }
        window.show()
        startServer()
    }

    private func startServer() {
        window.showStatus("Starting Doxograph…")
        server.start(
            onReady: { [weak self] url in
                guard let self else { return }
                self.ready = true
                self.window.load(url)
                self.flushPendingDrops()
            },
            onFailure: { [weak self] failure in self?.report(failure) })
    }

    // MARK: - Files

    /// Dropping PDFs on the Dock icon, or opening them with Doxograph from
    /// Finder, lands here.
    func application(_ application: NSApplication, open urls: [URL]) {
        take(urls.filter(Uploader.isPDF))
    }

    private func take(_ urls: [URL]) {
        guard !urls.isEmpty else { return }
        guard ready else {
            pendingDrops.append(contentsOf: urls)
            return
        }
        NSApp.activate(ignoringOtherApps: true)
        window.show()
        Uploader.upload(urls, to: server.baseURL, extractNow: true) { [weak self] result in
            if case .failure(let error) = result { self?.warn("Upload failed", error.localizedDescription) }
        }
    }

    private func flushPendingDrops() {
        let waiting = pendingDrops
        pendingDrops = []
        take(waiting)
    }

    @objc func addPapers(_ sender: Any?) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.pdf]
        panel.prompt = "Add"
        panel.message = "Choose PDFs to add to the corpus."
        panel.beginSheetModal(for: window.window) { [weak self] response in
            guard response == .OK else { return }
            self?.take(panel.urls)
        }
    }

    // MARK: - Failure

    private func report(_ failure: ServerController.Failure) {
        switch failure {
        case .commandNotFound:
            window.showStatus("Doxograph is not installed where this app can find it.")
            let alert = NSAlert()
            alert.messageText = "Can’t find the doxograph command"
            alert.informativeText = """
                This app runs the doxograph you installed with pip; it does not \
                bundle its own copy. Point it at the command inside your virtual \
                environment, usually .venv/bin/doxograph.
                """
            alert.addButton(withTitle: "Choose…")
            alert.addButton(withTitle: "Quit")
            guard alert.runModal() == .alertFirstButtonReturn else { return NSApp.terminate(nil) }
            chooseCommand()
        case .launchFailed(let detail):
            window.showStatus("The server did not start.")
            warn("The server did not start", detail)
        case .neverAnswered(let log):
            window.showStatus("The server started but never answered.")
            warn("The server started but never answered", log)
        }
    }

    private func chooseCommand() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.message = "Choose the doxograph command."
        panel.directoryURL = FileManager.default.homeDirectoryForCurrentUser
        panel.showsHiddenFiles = true
        guard panel.runModal() == .OK, let url = panel.url else { return NSApp.terminate(nil) }
        guard Locate.isRunnable(url.path) else {
            warn("That file is not runnable", "\(url.path) is not an executable command.")
            return chooseCommand()
        }
        Locate.remember(url.path)
        startServer()
    }

    private func warn(_ message: String, _ detail: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = message
        alert.informativeText = detail
        alert.addButton(withTitle: "OK")
        if window.window.isVisible {
            alert.beginSheetModal(for: window.window, completionHandler: nil)
        } else {
            alert.runModal()
        }
    }

    // MARK: - Quitting

    /// Reading a paper takes minutes and dies with the server, so quitting in
    /// the middle of one asks first.
    ///
    /// A paper that is still being uploaded counts too, and counts even when
    /// the server was adopted rather than started here. An adopted server
    /// survives this app and keeps reading, but the upload does not: it runs in
    /// this process and dies with it, so the paper is lost either way.
    ///
    /// The server counts an upload from the moment its request arrives, which
    /// covers a paper dropped on the page as well as one dropped on the app.
    /// What it cannot see is the stretch before that — the copy into a
    /// multipart body — so the two counts overlap rather than meeting.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard ready, server.ownsServer else {
            let uploading = Uploader.uploadsInFlight
            guard uploading > 0 else { return .terminateNow }
            return confirmQuit(uploading: uploading, reading: 0) ? .terminateNow : .terminateCancel
        }
        // Count the uploads on both sides of the question, because either
        // reading alone can miss work the other sees. The server answers a POST
        // only once it has staged the file and made the job, so an upload that
        // lands while the health request is in the air leaves a count that says
        // zero about a moment when the job did not exist yet. Reading the
        // uploads first catches that one; reading them again afterwards catches
        // a paper dropped while the question was being asked.
        //
        // Both samples are carried into the decision rather than looked up
        // again inside it. Sampling twice and then asking a third time is what
        // the earlier version did, and the third reading is taken after the
        // upload has finished — so the sample that saw the work was thrown away
        // and the app quit anyway, which is the whole race this is here for.
        let uploadingBefore = Uploader.uploadsInFlight
        server.activeJobs { busy in
            let uploading = max(uploadingBefore, Uploader.uploadsInFlight)
            let reading = busy ?? 0
            guard uploading == 0, reading == 0 else {
                return NSApp.reply(
                    toApplicationShouldTerminate: self.confirmQuit(uploading: uploading, reading: reading))
            }
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    /// Asks whether to quit on top of work in progress. True means quit.
    ///
    /// Takes the counts its caller measured instead of measuring again: an
    /// upload that finished a moment ago handed its paper to the server, and
    /// reading zero here would be reading it too late to matter. The wording
    /// can therefore lag by a beat — a paper that has just become a reading job
    /// is still announced as one being added — but it is never silent about
    /// work that exists.
    ///
    /// Always offers the quit, so a count that somehow refuses to fall — a
    /// request that never returns, say — cannot trap the user in their own app.
    private func confirmQuit(uploading: Int, reading: Int) -> Bool {
        guard uploading > 0 || reading > 0 else { return true }
        let alert = NSAlert()
        switch (uploading, reading) {
        case (0, _):
            alert.messageText = reading == 1
                ? "Doxograph is still reading a paper."
                : "Doxograph is still reading \(reading) papers."
            alert.informativeText = "Quitting now stops the extraction, and the papers stay unread."
            alert.addButton(withTitle: "Quit Anyway")
            alert.addButton(withTitle: "Keep Reading")
        case (_, 0):
            alert.messageText = uploading == 1
                ? "Doxograph is still adding a paper."
                : "Doxograph is still adding \(uploading) papers."
            alert.informativeText = "Quitting now cancels the upload, and nothing is added."
            alert.addButton(withTitle: "Quit Anyway")
            alert.addButton(withTitle: "Keep Adding")
        default:
            alert.messageText = "Doxograph is still adding and reading papers."
            alert.informativeText = """
                Quitting now cancels the upload and stops the extraction, and \
                the papers stay unread.
                """
            alert.addButton(withTitle: "Quit Anyway")
            alert.addButton(withTitle: "Keep Going")
        }
        return alert.runModal() == .alertFirstButtonReturn
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        server.stop()
    }

    // MARK: - View menu

    @objc func reloadPage(_ sender: Any?) { window.webView.reload() }

    @objc func goBack(_ sender: Any?) { window.webView.goBack() }

    @objc func actualSize(_ sender: Any?) { window.webView.pageZoom = 1 }

    @objc func zoomIn(_ sender: Any?) {
        window.webView.pageZoom = min(window.webView.pageZoom * 1.1, 3)
    }

    @objc func zoomOut(_ sender: Any?) {
        window.webView.pageZoom = max(window.webView.pageZoom / 1.1, 0.5)
    }

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        switch menuItem.action {
        case #selector(goBack(_:)): return window.webView.canGoBack
        case #selector(reloadPage(_:)), #selector(addPapers(_:)): return ready
        default: return true
        }
    }
}
