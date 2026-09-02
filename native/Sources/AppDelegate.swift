import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuItemValidation {
    private let server = ServerController()
    private let window = WebWindow()
    private var ready = false
    /// The server was asked to start and did not. Updating is still allowed,
    /// since a pull may be exactly what fixes it.
    private var serverFailed = false
    /// PDFs dropped on the Dock icon before the server answered. A drop can
    /// even be what launched the app, so this fills up before the window exists.
    private var pendingDrops: [URL] = []
    /// An update is in progress; a second one must wait for it.
    private var updating = false
    /// The bundle was replaced by an update, and the quit under way should be
    /// followed by a launch of the new one.
    private var relaunchAfterQuit = false

    /// Appended to an update's report when the server is not this app's to
    /// restart, so the new Python is not mistaken for live.
    private static let adoptedServerNote = """


        This app is using a server started elsewhere, so the changes \
        take effect when that `doxograph serve` is restarted.
        """

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = buildMenu()
        window.onFilesDropped = { [weak self] urls in self?.take(urls) }
        window.show()
        startServer()
    }

    private func startServer() {
        serverFailed = false
        window.showStatus("Starting Doxograph…")
        server.start(onReady: serverReady, onFailure: { [weak self] failure in self?.report(failure) })
    }

    private func serverReady(_ url: URL) {
        ready = true
        window.load(url)
        flushPendingDrops()
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
        window.currentWorkspace { [weak self] workspace in
            guard let self else { return }
            Uploader.upload(urls, to: self.server.baseURL, extractNow: true, workspace: workspace) {
                [weak self] result in
                if case .failure(let error) = result {
                    self?.warn("Upload failed", error.localizedDescription)
                }
            }
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

    // MARK: - Updating

    /// Pull the checkout the app runs from, and restart on top of it.
    ///
    /// The app is a launcher over a checkout's venv, so an update is a `git
    /// pull` there. Python changes are live once the server restarts; a change
    /// under `native/` means this bundle is stale too, so it is rebuilt and the
    /// app relaunched. An adopted server is not this app's to restart: the pull
    /// still happens, and the alert says the terminal has to do the rest.
    @objc func updateDoxograph(_ sender: Any?) {
        guard !updating else { return }
        updating = true
        window.showStatus("Checking for updates…")
        // Finding the command can fall back to a login shell, which is slow.
        DispatchQueue.global().async { [weak self] in
            let command = Locate.doxographCommand()
            DispatchQueue.main.async { self?.update(with: command) }
        }
    }

    private func update(with command: String?) {
        guard let command else {
            updating = false
            return report(.commandNotFound)
        }
        guard let repository = Updater.repository(near: command)
            ?? Updater.rememberedRepository()
            ?? chooseRepository() else {
            updating = false
            if ready { window.hideStatus() }
            return
        }
        Updater.remember(repository)
        let ownsServer = server.ownsServer
        Updater.run(command: command, repository: repository,
                    progress: { [weak self] text in self?.window.showStatus(text) },
                    completion: { [weak self] result in self?.finishUpdate(result, ownsServer: ownsServer) })
    }

    private func finishUpdate(_ result: Result<Updater.Outcome, Updater.Failure>, ownsServer: Bool) {
        updating = false
        switch result {
        case .failure(.step(let name, let output)):
            if ready { window.hideStatus() }
            warn("Update failed at \(name)", output.isEmpty ? "It printed nothing." : output)

        case .success(let outcome) where outcome.changes.isEmpty:
            if ready { window.hideStatus() }
            inform("Doxograph is up to date.", "")
            // Nothing new to run, but a start that failed may have failed for
            // a passing reason, and this is the one button left to press.
            if serverFailed { startServer() }

        case .success(let outcome) where outcome.appRebuilt:
            // The new bundle is in place; only a fresh process can run it. The
            // quit below goes through the usual questions about work in flight,
            // and the relaunch is spawned only once the quit is actually happening.
            // Relaunching over an adopted server adopts it again, old Python and
            // all, so that case carries the same note as a Python-only update.
            let alert = NSAlert()
            alert.messageText = "Updated Doxograph. Relaunch to run the new version."
            alert.informativeText = ownsServer ? outcome.changes : outcome.changes + Self.adoptedServerNote
            alert.addButton(withTitle: "Relaunch")
            alert.addButton(withTitle: "Later")
            guard alert.runModal() == .alertFirstButtonReturn else {
                if ready { window.hideStatus() }
                // The old bundle keeps running, but the Python is new, and a
                // server that failed to start may start now.
                if serverFailed { startServer() }
                return
            }
            relaunchAfterQuit = true
            NSApp.terminate(nil)

        case .success(let outcome):
            if serverFailed {
                // Nothing is running that the restart could interrupt, and the
                // pull may be what the failed start was missing. Try again.
                inform("Updated Doxograph.", outcome.changes)
                startServer()
                return
            }
            guard ownsServer else {
                if ready { window.hideStatus() }
                inform("Updated Doxograph.", outcome.changes + Self.adoptedServerNote)
                return
            }
            // Restarting the server ends whatever it is doing, exactly as
            // quitting would, so it gets the same question first, with the same
            // two readings of the upload count for the same reasons.
            let uploadingBefore = Uploader.uploadsInFlight
            server.probe { [weak self] probe in
                guard let self else { return }
                guard self.mayInterrupt(.restart, probe, ownsServer: true, uploadingBefore: uploadingBefore) else {
                    if self.ready { self.window.hideStatus() }
                    self.inform("Updated Doxograph.", outcome.changes + """


                        The server is still running the old code. Quit and reopen \
                        Doxograph to run the new version.
                        """)
                    return
                }
                self.ready = false
                self.window.showStatus("Restarting Doxograph…")
                self.server.restart(
                    onReady: { [weak self] url in
                        self?.serverReady(url)
                        self?.inform("Updated Doxograph.", outcome.changes)
                    },
                    onFailure: { [weak self] failure in self?.report(failure) })
            }
        }
    }

    /// Ask where the checkout is, when the command was not installed from one
    /// the app can recognise.
    private func chooseRepository() -> URL? {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.message = "Choose the doxograph checkout to update (the folder with pyproject.toml)."
        panel.prompt = "Update"
        panel.directoryURL = FileManager.default.homeDirectoryForCurrentUser
        while panel.runModal() == .OK, let url = panel.url {
            if Updater.isRepository(url) { return url }
            warn("That is not a doxograph checkout",
                 "\(url.path) has no pyproject.toml, native/build.sh, or .git.")
        }
        return nil
    }

    private func inform(_ message: String, _ detail: String) {
        let alert = NSAlert()
        alert.messageText = message
        alert.informativeText = detail
        alert.addButton(withTitle: "OK")
        alert.beginSheetModal(for: window.window, completionHandler: nil)
    }

    // MARK: - Failure

    private func report(_ failure: ServerController.Failure) {
        serverFailed = true
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
    /// The two kinds of work in flight are not lost by the same event, which is
    /// why an adopted server is not simply exempt from the question:
    ///
    /// - A paper being read dies with the server. When the server was adopted
    ///   it outlives this app and keeps reading, so quitting costs nothing and
    ///   the reading count is ignored.
    /// - A paper being uploaded dies with whoever is sending it, and that is
    ///   this app either way: the native uploader runs in this process, and a
    ///   paper dropped on the page is posted by this app's web view. Quitting
    ///   tears that request down even though the adopted server survives — the
    ///   server living on does not help when the bytes stop arriving. So an
    ///   upload is asked about whether the server is owned or adopted.
    ///
    /// Uploads are counted on both sides, because neither side sees them all.
    /// This app knows only about the papers it sends itself; a paper dropped on
    /// the page never touches its counter, and only the server sees that one.
    /// The server counts from the moment the request arrives and this app
    /// counts from before it is sent, so the two overlap rather than meeting,
    /// and the larger is taken rather than the sum — one native upload is
    /// usually in both at once, and the number only has to be right about being
    /// nonzero.
    ///
    /// One honest false positive: on an adopted server the arriving upload may
    /// belong to someone else — a browser tab, a curl in a terminal — that
    /// quitting this app would not disturb, and the question gets asked anyway.
    /// The server cannot say whose request it is, and guessing wrong the other
    /// way loses a paper, so it errs toward asking.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        // An update is a pull, a pip install and a build, and none of them
        // likes being abandoned half-way. The children are not stopped here:
        // the next update measures from the last commit that fully installed,
        // so whatever this one leaves behind is done over. The question is so
        // the user knows that is what they are choosing.
        if updating, !confirmQuitWhileUpdating() {
            relaunchAfterQuit = false
            return .terminateCancel
        }
        guard ready else { return .terminateNow }
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
        let ownsServer = server.ownsServer
        server.probe { probe in
            let quitting = self.mayInterrupt(
                .quit, probe, ownsServer: ownsServer, uploadingBefore: uploadingBefore)
            // A relaunch was only ever meant to follow this quit. If the user
            // keeps the app open, the next quit is an ordinary one and must not
            // bring the app back.
            if !quitting { self.relaunchAfterQuit = false }
            NSApp.reply(toApplicationShouldTerminate: quitting)
        }
        return .terminateLater
    }

    /// What is about to cut the server's work short. Quitting and restarting
    /// the server after an update lose the same work, so they ask the same
    /// question; this only changes the words on it.
    private enum Interruption {
        case quit, restart

        var gerund: String { self == .quit ? "Quitting" : "Restarting" }
        var anyway: String { self == .quit ? "Quit Anyway" : "Restart Anyway" }
        /// The button that backs out: "Keep Reading" makes sense of a quit,
        /// while a restart deferred is simply done later.
        func keep(_ doing: String) -> String { self == .quit ? "Keep \(doing)" : "Later" }
    }

    /// Whether to go ahead, given what the server said — or did not say.
    ///
    /// A probe that fails is the case this used to get backwards. Reading a
    /// missing answer as a job count of zero turns "I could not find out" into
    /// "nothing is happening", which is the one direction this code must never
    /// err in: the server keeps extracting, the app approves the quit, stops it,
    /// and the paper is lost without the warning it promises.
    ///
    /// Not every failure is the same, though, and prompting on all of them would
    /// nag on every quit after a server crash — the server is gone, so there is
    /// nothing left to lose by going. The two are distinguishable: a refused
    /// connection means nothing is listening, while a timeout means something is
    /// listening and not answering, which is exactly what a server in the middle
    /// of a paper can look like. So a refusal quits, and a silence asks.
    private func mayInterrupt(
        _ action: Interruption, _ probe: ServerController.Probe, ownsServer: Bool, uploadingBefore: Int
    ) -> Bool {
        let uploadingHere = max(uploadingBefore, Uploader.uploadsInFlight)
        switch probe {
        case .answered(let health):
            let uploading = max(uploadingHere, health.arriving)
            // A paper being read dies with the server, and an adopted server
            // does not die with this app.
            let reading = ownsServer ? health.jobs : 0
            guard uploading > 0 || reading > 0 else { return true }
            return confirmInterruption(action, uploading: uploading, reading: reading)
        case .unreachable:
            // Nothing is listening, so the server has no work to lose. This
            // app's own upload still might be in flight, and it is about to
            // fail, but it is the one thing here that is known rather than
            // guessed — so it is still worth saying.
            guard uploadingHere > 0 else { return true }
            return confirmInterruption(action, uploading: uploadingHere, reading: 0)
        case .unresponsive:
            // Something is on the port and would not say what it is doing. Any
            // count in hand is a floor on work that cannot be seen the rest of,
            // so the honest thing to say is that it is not known — not a number.
            return confirmInterruptionNotKnowing(action)
        }
    }

    /// Asks whether to go ahead on top of work in progress. True means go.
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
    private func confirmInterruption(_ action: Interruption, uploading: Int, reading: Int) -> Bool {
        guard uploading > 0 || reading > 0 else { return true }
        let alert = NSAlert()
        switch (uploading, reading) {
        case (0, _):
            alert.messageText = reading == 1
                ? "Doxograph is still reading a paper."
                : "Doxograph is still reading \(reading) papers."
            alert.informativeText = "\(action.gerund) now stops the extraction, and the papers stay unread."
            alert.addButton(withTitle: action.anyway)
            alert.addButton(withTitle: action.keep("Reading"))
        case (_, 0):
            alert.messageText = uploading == 1
                ? "Doxograph is still adding a paper."
                : "Doxograph is still adding \(uploading) papers."
            alert.informativeText = "\(action.gerund) now cancels the upload, and nothing is added."
            alert.addButton(withTitle: action.anyway)
            alert.addButton(withTitle: action.keep("Adding"))
        default:
            alert.messageText = "Doxograph is still adding and reading papers."
            alert.informativeText = """
                \(action.gerund) now cancels the upload and stops the extraction, and \
                the papers stay unread.
                """
            alert.addButton(withTitle: action.anyway)
            alert.addButton(withTitle: action.keep("Going"))
        }
        return alert.runModal() == .alertFirstButtonReturn
    }

    /// Asks whether to go ahead when the server would not say what it is doing.
    ///
    /// Its own question, not a count of zero or a guess dressed up as one. The
    /// other prompt names what is at stake — a paper being added, two being read
    /// — and there is no honest way to fill that in here. What can be said is
    /// why the question is being asked at all, so the user can decide with the
    /// same information the app has.
    ///
    /// Going ahead is offered here too, and it is the default button: a server
    /// that has wedged must not be able to trap someone in the app.
    private func confirmInterruptionNotKnowing(_ action: Interruption) -> Bool {
        let alert = NSAlert()
        alert.messageText = "Doxograph can’t tell whether it is still working."
        alert.informativeText = """
            The server did not answer. It may be part-way through reading a \
            paper, and \(action.gerund.lowercased()) now would stop it and leave \
            the paper unread.
            """
        alert.addButton(withTitle: action.anyway)
        alert.addButton(withTitle: action == .quit ? "Don’t Quit" : "Later")
        return alert.runModal() == .alertFirstButtonReturn
    }

    /// Asks whether to quit out from under a running update. True means quit.
    ///
    /// Nothing is lost for good: the update is measured from the last commit
    /// that fully installed, so the next one does over whatever this one was
    /// in the middle of. It is still a pull, a pip install or a build left
    /// running with no app attached, which is worth a question.
    private func confirmQuitWhileUpdating() -> Bool {
        let alert = NSAlert()
        alert.messageText = "Doxograph is updating."
        alert.informativeText = """
            Quitting now leaves the update unfinished. The next Update \
            Doxograph… does it over.
            """
        alert.addButton(withTitle: "Quit Anyway")
        alert.addButton(withTitle: "Keep Updating")
        return alert.runModal() == .alertFirstButtonReturn
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        server.stop()
        if relaunchAfterQuit { relaunch() }
    }

    /// Open this bundle again once this process is gone. `open` on a bundle
    /// that is still running only brings it forward, so a shell waits for the
    /// pid to disappear first.
    private func relaunch() {
        let helper = Process()
        helper.executableURL = URL(fileURLWithPath: "/bin/sh")
        helper.arguments = [
            "-c", "while kill -0 \"$1\" 2>/dev/null; do sleep 0.2; done; open \"$2\"", "relaunch",
            String(ProcessInfo.processInfo.processIdentifier), Bundle.main.bundlePath,
        ]
        try? helper.run()
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
        // Not while the server is still coming up, since the restart after
        // the pull needs to know whether it is owned. A start that failed is
        // settled, and updating is then the way out.
        case #selector(updateDoxograph(_:)): return (ready || serverFailed) && !updating
        default: return true
        }
    }
}
