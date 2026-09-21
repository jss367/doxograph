import Darwin
import Foundation

/// Owns the `doxograph serve` process behind the window.
///
/// The app does not reimplement the server: it starts the one that is already
/// installed, waits for it to answer, and points a web view at it. If a server
/// is already listening it is adopted instead, so launching the app twice, or
/// launching it over a `doxograph serve` running in a terminal, does not fight
/// over the port or kill work that is already in flight.
final class ServerController {
    enum Failure {
        case commandNotFound
        case launchFailed(String)
        case neverAnswered(String)
        case staleServer(Stale)
    }

    /// A Doxograph already on the port, running code this app should not adopt.
    ///
    /// Adoption is how an orphan becomes permanent. This app stops only the
    /// child it spawned, so a server that outlived a crash, a force quit or a
    /// bundle swapped underneath it is adopted by every launch afterwards and
    /// upgraded by none — its code stays pinned at whatever it was while the
    /// app around it moves on, silently, for as long as the process lives. What
    /// the server says about itself is the only thing that tells that apart from
    /// the `doxograph serve` someone is deliberately running in a terminal,
    /// which is why the mismatch is a question for the user rather than a
    /// decision taken here.
    struct Stale {
        /// What the server said that makes it not this app's to run on.
        ///
        /// The two are the same failure at two resolutions. A release number is
        /// coarse — `Info.plist` and `doxograph/__init__.py` move together and
        /// only on a release, so it catches a server that outlived a release and
        /// misses one that outlived a morning's commits. The digest is the
        /// finer question and the one actually worth asking: is restarting this
        /// server going to change what it runs.
        enum Reason {
            /// It answers with a release this app was not built alongside.
            case version
            /// It answers with this app's release, and the Python it loaded is
            /// no longer the Python on disk.
            case code
        }

        let port: Int
        /// What the server calls itself, or empty when it is old enough not to
        /// say. Both are equally not this app's version.
        let version: String
        let health: Health
        let reason: Reason
        /// What the server said about its own sources, or nil from one too old
        /// to have been asked — every release before this check existed.
        let code: Code?

        /// The version, as it goes in a sentence.
        var versionName: String { ServerController.versionName(version) }

        /// As much of this particular server as can be seen from outside it.
        ///
        /// Not an identifier the server issues — it has none — but enough to
        /// notice that the thing on the port is no longer the thing a question
        /// was asked about. A pid would be exact and is unusable: the alert has
        /// no deadline, and a pid outlives its process only as a number the
        /// kernel will hand to someone else. The digest is the strong half; the
        /// version is all there is from a server too old to report one.
        var identity: String { "\(version)/\(code?.running ?? "")" }

        /// What stopping this server would cost, or nothing when it is idle.
        /// The two counts are summed here because the sentence only has to be
        /// right about there being work to lose.
        var workNote: String {
            let busy = health.jobs + health.arriving
            guard busy > 0 else { return "" }
            return "\n\nIt has \(busy) \(busy == 1 ? "paper" : "papers") in flight, "
                + "which stopping it would lose."
        }
    }

    /// What a server said about the Python it is made of, which is a different
    /// question from which release it belongs to.
    ///
    /// Only `.py` files are in the digest, because only they are read once and
    /// kept. `static/` is handed out per request, so editing `app.js` under a
    /// running server reaches the next reload and leaves nothing stale behind.
    struct Code {
        /// The digest the server took while importing its own modules, which is
        /// the code it runs until it exits.
        let running: String
        /// The same digest over the files on disk, taken when it was asked.
        let onDisk: String

        /// Whether the server has outlived its own source.
        ///
        /// An empty digest on either side is a package the server could not read
        /// back. That is an unanswered question, not a matching answer, so it is
        /// not a mismatch either — this check only ever refuses on something it
        /// was actually told.
        var moved: Bool { !running.isEmpty && !onDisk.isEmpty && running != onDisk }
    }

    /// The version this app was built as. The release commit bumps the bundle
    /// and the Python package together, so this is what a server running this
    /// app's code answers `/api/health` with.
    static let appVersion = Bundle.main
        .object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""

    /// A reported version, as it goes in a sentence. A server too old to name
    /// one is not a server whose version is fine; it is one nobody can name.
    static func versionName(_ version: String) -> String {
        version.isEmpty ? "an unknown version" : "version \(version)"
    }

    private static let preferredPort = 8765
    private static let portDefaultsKey = "DoxographPort"
    /// Every number that is a TCP port at all. Port 0 is excluded on purpose:
    /// `bind` treats it as "give me any free port", so it would read as free
    /// forever and the server would be started with `--port 0`, listening
    /// somewhere the app never looks.
    private static let portRange = 1...65535
    /// How many ports past the preferred one the walk may try.
    private static let scanWidth = 20
    /// How long the port walk may spend probing before it settles for the free
    /// port it has already found. Only unresponsive occupants can run this out.
    private static let scanBudget: TimeInterval = 5

    private let host = "127.0.0.1"
    private let queue = DispatchQueue(label: "com.jss367.doxograph.server")
    private let log = LogBuffer()
    private var port = ServerController.preferredPort

    /// Guards the two fields the startup queue and the quitting main thread
    /// both touch. Everything else here is read and written on one of them.
    private let lock = NSLock()
    private var process: Process?
    private var cancelled = false

    /// True when this app started the server, and so is responsible for it.
    private(set) var ownsServer = false

    /// The version the server this app spawned answered with, once it has
    /// answered. Nil until then, and empty from a server too old to say —
    /// which is a version that does not match, not an absence of one.
    private var startedVersion: String?

    /// The version of the server this app started, when it is not this app's
    /// own, and nil when it matches or nothing was started.
    ///
    /// Spawning proves nothing about the version. The command comes from
    /// `doxograph-path`, which is `DOXOGRAPH_CMD` at build time, or a command
    /// chosen by hand, or a checkout that has been pulled past the bundle that
    /// launched it — so the same-version guarantee the port walk gives for an
    /// adopted server has to be checked again for one this app starts itself.
    ///
    /// Unlike an adopted mismatch this is said out loud and then run on, not
    /// refused. The two cases look alike and are not: an orphan is old code
    /// pinned for as long as its process lives, while a spawned server is
    /// whatever the checkout holds right now, usually newer than the bundle and
    /// replaced on the next launch. A checkout pulled ahead of its app is an
    /// ordinary morning, and refusing to start over it would break more than it
    /// protects.
    var spawnedVersionMismatch: String? {
        guard ownsServer, let reported = startedVersion, reported != Self.appVersion
        else { return nil }
        return reported
    }

    var baseURL: URL { URL(string: "http://\(host):\(port)")! }

    // MARK: - Starting

    func start(onReady: @escaping (URL) -> Void, onFailure: @escaping (Failure) -> Void) {
        queue.async {
            self.deliver(self.bringUp(), onReady: onReady, onFailure: onFailure)
        }
    }

    private func deliver(_ result: Outcome, onReady: @escaping (URL) -> Void, onFailure: @escaping (Failure) -> Void) {
        DispatchQueue.main.async {
            switch result {
            case .success: onReady(self.baseURL)
            case .failure(let failure): onFailure(failure)
            // The app is on its way out. Nobody is left to show a window to
            // or an alert about.
            case .cancelled: break
            }
        }
    }

    private enum Outcome {
        case success
        case failure(Failure)
        case cancelled
    }

    private var isCancelled: Bool {
        lock.lock()
        defer { lock.unlock() }
        return cancelled
    }

    /// The port to start the walk from: the configured one when it is a port,
    /// and the built-in one when it is not.
    ///
    /// A preference is a number a person typed, so it can be anything. A value
    /// outside the port range is not a port the walk could fall back to, so it
    /// is ignored rather than clamped: quietly reading `DoxographPort 70000` as
    /// 65535 would put the app somewhere the user never asked for and looks
    /// like it worked. Falling back to 8765 is the same thing that happens when
    /// the key is absent or holds a string, which is the behaviour that is
    /// already documented.
    private static func startingPort() -> Int {
        guard let configured = UserDefaults.standard.object(forKey: portDefaultsKey) as? Int,
              portRange.contains(configured) else { return preferredPort }
        return configured
    }

    private func bringUp() -> Outcome {
        let preferred = Self.startingPort()

        // Both of the steps below can take seconds — the port walk against an
        // unresponsive neighbour, the command lookup against a login shell that
        // sources a slow profile — and a quit can land in either of them.
        guard !isCancelled else { return .cancelled }

        switch choosePort(from: preferred) {
        case .exhausted:
            return .failure(.launchFailed("No free port near \(preferred)."))

        case .adopt(let existing):
            port = existing
            ownsServer = false
            return .success

        case .stale(let server):
            // Pointed at it but not started on it. The app is told, and comes
            // back through `useStale` or `replaceStale` once the user has said
            // which of those they want.
            port = server.port
            ownsServer = false
            return .failure(.staleServer(server))

        case .start(let free):
            guard !isCancelled else { return .cancelled }
            guard let command = Locate.doxographCommand() else { return .failure(.commandNotFound) }
            port = free
            do {
                guard try spawn(command: command) else { return .cancelled }
            } catch {
                return .failure(.launchFailed("\(command): \(error.localizedDescription)"))
            }
            ownsServer = true
            return waitUntilAnswering()
        }
    }

    /// Starts the server, and returns false if a quit got there first — in
    /// which case whatever was started has already been shut down again.
    @discardableResult
    private func spawn(command: String) throws -> Bool {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: command)
        process.arguments = ["serve", "--host", host, "--port", String(port)]

        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        // A Dock launch starts with a minimal PATH. Nothing in the server shells
        // out today, but its own directory costs nothing to put back.
        let binDirectory = (command as NSString).deletingLastPathComponent
        environment["PATH"] = [binDirectory, environment["PATH"] ?? "/usr/bin:/bin"].joined(separator: ":")
        process.environment = environment

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        let log = self.log
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty else {
                // End of the pipe: the server has gone. A handler left
                // installed keeps being called on a descriptor that will never
                // have anything on it again, which spins for the rest of the
                // session after a restart has left the old server's pipe here.
                handle.readabilityHandler = nil
                return
            }
            log.append(String(decoding: data, as: UTF8.self))
        }

        // The handoff, and the reason the spawn and the store are inside one
        // critical section rather than two: a quit that lands between them would
        // find nothing to stop and leave the child behind. `stop()` takes the
        // same lock to set the flag and read `process`, so it is always on one
        // side or the other — it either misses the child entirely, because the
        // flag was set before it was started, or it finds it stored. Holding the
        // lock over `run()` costs the quitting thread a `posix_spawn`, which is
        // not a wait for Python to load.
        lock.lock()
        defer { lock.unlock() }
        guard !cancelled else { return false }
        try process.run()
        self.process = process
        return true
    }

    /// Poll until uvicorn binds. Imports alone take a couple of seconds, and a
    /// cold start on a slow disk takes longer, so the ceiling is generous.
    private func waitUntilAnswering() -> Outcome {
        let deadline = Date().addingTimeInterval(60)
        while Date() < deadline {
            // A quit during the wait has already terminated the child, so the
            // exit below would otherwise be reported as a server that died on
            // its own, to an app that is closing.
            if isCancelled { return .cancelled }
            if let answered = health(on: port, timeout: 1) {
                startedVersion = answered.version
                return .success
            }
            if let process = startedProcess, !process.isRunning {
                return .failure(.neverAnswered(log.tail(lines: 25).isEmpty
                    ? "The server exited immediately."
                    : log.tail(lines: 25)))
            }
            Thread.sleep(forTimeInterval: 0.25)
        }
        // Read the log before stopping, so the reason shown is the silence that
        // ran out the clock and not the shutdown that followed it. The child is
        // still running: without this it would hold the port for the rest of the
        // session, behind a window saying the server never started.
        let detail = log.tail(lines: 25).isEmpty
            ? "The server did not answer within a minute."
            : log.tail(lines: 25)
        if let process = startedProcess { shutDown(process) }
        return .failure(.neverAnswered(detail))
    }

    // MARK: - Stopping

    /// The server this app spawned, if it got that far. An adopted server never
    /// appears here: this app did not start it and does not stop it.
    private var startedProcess: Process? {
        lock.lock()
        defer { lock.unlock() }
        return process
    }

    /// Ask the server to shut down, and give uvicorn a moment to do it cleanly.
    ///
    /// This also cancels a start that is still in flight, which is the only
    /// reason the flag exists. Quitting while the port walk or the command
    /// lookup was still running used to find no process to stop and return
    /// immediately; the background queue would then spawn the server a moment
    /// later, after the last chance to stop it had passed, leaving a
    /// `doxograph serve` holding the port with no app attached to it. The next
    /// launch adopts that orphan, so nothing breaks, but a server the user did
    /// not ask for should not outlive the app they just quit.
    ///
    /// The cancel is one-way, which is what the one caller wants: the app calls
    /// this from `applicationWillTerminate` and never starts a server again.
    /// `restart()` is the way to stop a server and start another.
    func stop() {
        lock.lock()
        cancelled = true
        let started = process
        lock.unlock()
        guard let started else { return }
        shutDown(started)
    }

    /// Stop the server this app started so a fresh one can take its place, as
    /// after an update has changed the code under it. An adopted server is left
    /// alone, since this app did not start it. Unlike `stop()` this leaves the
    /// controller usable: the next `start()` walks the ports and spawns again.
    func restart(onReady: @escaping (URL) -> Void, onFailure: @escaping (Failure) -> Void) {
        lock.lock()
        let started = process
        process = nil
        lock.unlock()
        // The shutdown can wait up to ten seconds on a server that is slow to
        // die, which is too long to hold the main thread. It happens on the
        // startup queue, in front of the start it makes room for.
        queue.async {
            if let started { self.shutDown(started) }
            self.deliver(self.bringUp(), onReady: onReady, onFailure: onFailure)
        }
    }

    private func shutDown(_ process: Process) {
        guard process.isRunning else { return }
        process.terminate()
        let exited = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            process.waitUntilExit()
            exited.signal()
        }
        if exited.wait(timeout: .now() + 5) == .timedOut {
            kill(process.processIdentifier, SIGKILL)
            // A restart starts its port walk the moment this returns. Until the
            // old process is gone the walk can still find it answering, adopt
            // it, and hand the window a server that dies a beat later.
            _ = exited.wait(timeout: .now() + 5)
        }
    }

    // MARK: - A server from another version

    /// Run on the stale server after all, because the user said to.
    ///
    /// Nothing is started and nothing is stopped: the server stays unowned,
    /// this app quits without taking it down, and the next launch asks the same
    /// question again. That is the intended shape — a deliberate `doxograph
    /// serve` from another checkout is a thing to leave alone, not a thing to
    /// remember.
    ///
    /// The port is looked at again first, all the same. The alert it was
    /// answered from has no deadline, and a port is not a promise: the server
    /// the question described can exit while the question stands, leaving the
    /// window to load a refused connection, or something else entirely to
    /// answer on the port it left. So "use it" means the server that is there
    /// now, and when nothing is, discovery starts over rather than pointing the
    /// window at an address on the strength of what used to be at it.
    func useStale(_ server: Stale, onReady: @escaping (URL) -> Void,
                  onFailure: @escaping (Failure) -> Void) {
        queue.async {
            switch self.probe(on: server.port, timeout: 2) {
            case .unreachable:
                return self.deliver(self.bringUp(), onReady: onReady, onFailure: onFailure)
            case .unresponsive:
                return self.deliver(.failure(.launchFailed(Self.unidentified(server.port))),
                                    onReady: onReady, onFailure: onFailure)
            case .answered:
                // Only an answer identifies a Doxograph. The walk holds itself
                // to that for the same reason — adopting a port on the strength
                // of something listening there puts a stranger's web app in this
                // window — and the alert changes nothing about it.
                self.port = server.port
                self.ownsServer = false
                self.deliver(.success, onReady: onReady, onFailure: onFailure)
            }
        }
    }

    /// Why a port that is occupied but silent is not acted on either way.
    ///
    /// The two things it could be want opposite treatment and cannot be told
    /// apart from outside: a Doxograph too busy to answer, which the user asked
    /// to have stopped, and something else that took the port when the stale
    /// server exited, which they did not. Stopping it risks signalling a
    /// stranger; running on it risks putting a stranger's page in this window;
    /// starting alongside it risks a second Doxograph over one corpus. So
    /// nothing happens, and the person who can see what else is on their machine
    /// is told what was found.
    ///
    /// A live Doxograph is rarely this. `/api/health` is deliberately cheap —
    /// the startup poll hits it four times a second — so a server that is merely
    /// reading a paper still answers.
    private static func unidentified(_ port: Int) -> String {
        """
        Something is listening on port \(port) and did not answer as Doxograph \
        within two seconds, so there is no telling whether it is the server this \
        app asked about or something that took the port after it exited. Nothing \
        was stopped and nothing was started. Try again, or stop the server yourself.
        """
    }

    /// Stop a stale server and start one on this app's code in its place.
    ///
    /// `SIGTERM` is what `shutDown` sends its own child, but this process is
    /// not this app's child, so there is no exit status to wait on. The port
    /// going quiet is the only evidence available, and it has to be waited for:
    /// the walk inside `bringUp` starts the moment this returns, and a server
    /// still holding the port at that point is adopted again — or, worse, read
    /// as a stranger and left in place while the app reports itself stuck.
    ///
    /// A server that will not let go is reported rather than killed harder. It
    /// is not this app's process, ten seconds of `SIGTERM` unanswered means
    /// something is wrong that `SIGKILL` would only hide, and the user is the
    /// one who knows what else is running on their machine.
    func replaceStale(_ server: Stale, onReady: @escaping (URL) -> Void,
                      onFailure: @escaping (Failure) -> Void) {
        queue.async {
            // Everything is established again here, and nothing is carried over
            // from the walk that found it. The alert in between has no deadline:
            // it can stand open for days while the server it describes exits on
            // its own and the kernel gives its pid to something unrelated, and a
            // `kill` on a number that old is a signal to a stranger. A pid is
            // only safe to use in the same breath as the lookup that produced
            // it, which is why one is no longer kept in `Stale` at all.
            switch self.inspect(server.port, timeout: 2) {
            case .gone:
                // Gone while the question was up, and gone is the one answer
                // that says so: the connection was refused. There is nothing to
                // stop, and the walk below finds the port free and starts there.
                return self.deliver(self.bringUp(), onReady: onReady, onFailure: onFailure)

            case .unidentified:
                // Not an empty port — reading it as one would send the walk off
                // to start a second Doxograph on the next port up while this one
                // carries on holding this one. But not a licence to signal it
                // either: what the user authorised was stopping the server the
                // alert described, and nothing here can show that this is still
                // that server rather than whatever took the port after it left.
                return self.deliver(.failure(.launchFailed(Self.unidentified(server.port))),
                                    onReady: onReady, onFailure: onFailure)

            case .adoptable:
                // Replaced, while the question was up, by a server running this
                // app's own code — the outcome the user asked for, arrived at
                // without this app doing anything. Adopt it rather than
                // restarting a server that is already right.
                self.port = server.port
                self.ownsServer = false
                return self.deliver(.success, onReady: onReady, onFailure: onFailure)

            case .stale(let live):
                // What was agreed to was the stopping of the server the
                // question described, and two things can make what is on the
                // port no longer that server.
                //
                // Its identity can have changed — its release, or the digest of
                // the code it loaded, which is as much of one as this app can
                // see. The 0.4.1 someone agreed to stop is not the 0.5.0 that
                // took the port after it exited, and neither the agreement nor
                // the work counted in it carries over.
                //
                // Or work can have started. The counts in the question came from
                // the walk, so an upload that began while the alert stood open
                // was never in it and nobody has agreed to lose it.
                //
                // Either way the question is asked again, about what is there
                // now. Neither can cycle: the second question describes the
                // server it is about, so answering Restart to that one is the
                // agreement the first could not give.
                let anotherServer = live.identity != server.identity
                let unagreedWork = live.health.jobs + live.health.arriving > 0
                    && server.health.jobs + server.health.arriving == 0
                if anotherServer || unagreedWork {
                    return self.deliver(.failure(.staleServer(live)),
                                        onReady: onReady, onFailure: onFailure)
                }
            }
            guard let pid = Self.listener(on: server.port) else {
                return self.deliver(.failure(.launchFailed(
                    "Nothing on port \(server.port) could be traced back to a process to stop.")),
                    onReady: onReady, onFailure: onFailure)
            }
            kill(pid, SIGTERM)
            guard self.waitUntilPortIsFree(server.port) else {
                return self.deliver(.failure(.launchFailed(
                    "The server on port \(server.port) (pid \(pid)) did not stop.")),
                    onReady: onReady, onFailure: onFailure)
            }
            self.deliver(self.bringUp(), onReady: onReady, onFailure: onFailure)
        }
    }

    /// Poll until nothing is listening on a port. Ten seconds is the same
    /// patience `shutDown` gives a server of this app's own before it gives up.
    private func waitUntilPortIsFree(_ port: Int) -> Bool {
        let deadline = Date().addingTimeInterval(10)
        while Date() < deadline {
            if portIsFree(port) { return true }
            Thread.sleep(forTimeInterval: 0.25)
        }
        return false
    }

    /// The process listening on a loopback port, according to `lsof`.
    ///
    /// A server this app did not spawn has no `Process` to ask, and an orphan
    /// is precisely that case — which is also why the pid cannot come from the
    /// server itself. Teaching `/api/health` to report its own pid would help
    /// every version except the ones already out there, and those are the only
    /// ones this is ever asked about.
    ///
    /// `-t` prints bare pids and nothing else. The first is taken: a listening
    /// socket has one owner unless it was inherited across a fork, and then the
    /// parent is both what `lsof` prints first and what there is any point in
    /// signalling. A missing or unhelpful `lsof` reads as "no pid", which the
    /// caller reports rather than acting on.
    private static func listener(on port: Int) -> pid_t? {
        let lsof = Process()
        lsof.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        lsof.arguments = ["-nP", "-t", "-sTCP:LISTEN", "-iTCP@127.0.0.1:\(port)"]
        let pipe = Pipe()
        lsof.standardOutput = pipe
        lsof.standardError = FileHandle.nullDevice
        do { try lsof.run() } catch { return nil }
        // Read before waiting. A pid per line is nowhere near a pipe buffer
        // today, but waiting on a process whose output nobody is draining is
        // the deadlock that shape of code eventually finds.
        let printed = pipe.fileHandleForReading.readDataToEndOfFile()
        lsof.waitUntilExit()
        return String(decoding: printed, as: UTF8.self)
            .split(whereSeparator: \.isNewline)
            .compactMap { pid_t($0.trimmingCharacters(in: .whitespaces)) }
            .first
    }

    // MARK: - Health

    /// What the server has in flight, split by what stopping would cost.
    ///
    /// The split is the point: the two numbers are lost by different events, so
    /// an app deciding whether it may quit cannot use their sum.
    struct Health {
        /// Papers being fetched or read. These die with the server, so an
        /// adopted one carries them on after this app is gone.
        let jobs: Int
        /// Requests still on the wire — an upload's body arriving, a link being
        /// posted. These die with whoever is sending them, including this app,
        /// whose web view posts a paper dropped on the page straight to the
        /// server.
        let arriving: Int
        /// The version the server reports, or empty from one too old to report
        /// one. Only the port walk reads it, to decide whether this server is
        /// running the same code as the app that found it.
        let version: String
    }

    /// How a health probe turned out. The two ways of not getting an answer are
    /// kept apart because they mean opposite things to someone deciding whether
    /// it is safe to quit.
    enum Probe {
        /// The server answered, and this is what it said.
        case answered(Health)
        /// Nothing is listening: the connection was refused. Whatever the
        /// server was doing, it is not doing it any more, so there is nothing
        /// left to lose — and nothing to ask about.
        case unreachable
        /// Something is there and did not answer — a timeout, an error, a reply
        /// that was not Doxograph's. This is the state a busy server can be in,
        /// so what it is working on is unknown rather than nothing.
        case unresponsive
    }

    /// What the server is in the middle of, or why that could not be found out.
    /// Used to warn before quitting on top of work.
    func probe(completion: @escaping (Probe) -> Void) {
        queue.async {
            let probe = self.probe(on: self.port, timeout: 2)
            DispatchQueue.main.async { completion(probe) }
        }
    }

    /// What the server is working on, or nil when nothing on the port answered
    /// as Doxograph. The wait for a server this app just spawned wants only
    /// that: an unresponsive stranger and a silent port are both "not up yet".
    /// Checking the name matters, since a stranger's port would otherwise count
    /// as this app's server having started.
    private func health(on port: Int, timeout: TimeInterval) -> Health? {
        guard case .answered(let health) = probe(on: port, timeout: timeout) else { return nil }
        return health
    }

    /// Asks the server what it is doing, and says which kind of silence it got
    /// when it does not find out.
    ///
    /// Everything that is not a refused connection is reported as unresponsive,
    /// including an HTTP reply that is not Doxograph's health. A 500 from a
    /// server under load, a proxy in the way, a half-written body: all of them
    /// mean something is listening, and something listening may be mid-paper. A
    /// refusal is the only answer that positively rules that out.
    private func probe(on port: Int, timeout: TimeInterval) -> Probe {
        guard let url = URL(string: "http://\(host):\(port)/api/health") else { return .unreachable }
        var request = URLRequest(url: url)
        request.timeoutInterval = timeout
        request.cachePolicy = .reloadIgnoringLocalCacheData

        // Boxed rather than captured directly: the wait below can give up while
        // the request is still in flight, and then the completion's write and
        // this thread's read would land on the same variable at once.
        let outcome = Guarded(Probe.unresponsive)
        let finished = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { data, response, error in
            defer { finished.signal() }
            if let error = error as? URLError {
                outcome.value = Self.meansNothingIsListening(error) ? .unreachable : .unresponsive
                return
            }
            guard let http = response as? HTTPURLResponse, http.statusCode == 200, let data,
                  let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  body["app"] as? String == "doxograph" else { return }
            // A server too old to split the count answers `busy` alone. Read it
            // as jobs, which is how it was read before the split. Adoption makes
            // that reachable: the server on the port can be an older install
            // than the app that found it.
            let busy = body["busy"] as? Int ?? 0
            outcome.value = .answered(Health(jobs: body["jobs"] as? Int ?? busy,
                                             arriving: body["arriving"] as? Int ?? 0,
                                             version: body["version"] as? String ?? ""))
        }.resume()
        // Giving up on the wait is itself a server that did not answer in time.
        guard finished.wait(timeout: .now() + timeout + 2) == .success else { return .unresponsive }
        return outcome.value
    }

    /// Whether a failed request means the port is empty rather than slow.
    ///
    /// A refused connection is the one failure that says the server is gone:
    /// something has to be listening to be slow. `cannotConnectToHost` is what
    /// a refusal on 127.0.0.1 arrives as; the rest of the family is there for
    /// the same reason and costs nothing. A timeout is deliberately not in the
    /// list — that is exactly the shape of a server too busy to answer.
    private static func meansNothingIsListening(_ error: URLError) -> Bool {
        switch error.code {
        case .cannotConnectToHost, .cannotFindHost, .dnsLookupFailed:
            return true
        default:
            return false
        }
    }

    /// What the server says about its own sources, or nil when it did not say.
    ///
    /// Nil covers a release that predates `/api/code` as well as a request that
    /// went wrong, and both mean the same thing to every caller: the question
    /// was not answered, so nothing is concluded from it. Only a server that
    /// answers, and answers with both digests, can be refused on this.
    ///
    /// The timeout is fixed and generous rather than inherited from the walk.
    /// This runs at most once per launch — on the one port that answered as
    /// Doxograph, after which the walk returns either way — so patience here
    /// cannot add up, and unlike `/api/health` the endpoint reads every source
    /// file in the package before it replies.
    private func code(on port: Int) -> Code? {
        guard let body = fetchJSON("/api/code", on: port, timeout: 3),
              body["app"] as? String == "doxograph",
              let running = body["running"] as? String,
              let onDisk = body["onDisk"] as? String
        else { return nil }
        return Code(running: running, onDisk: onDisk)
    }

    /// A GET on a loopback endpoint, decoded, or nil for anything that is not a
    /// 200 with a JSON object in it.
    ///
    /// The distinction `probe` draws between a refused connection and a silent
    /// one is deliberately not drawn here. This is only ever asked of a port
    /// that has just answered as Doxograph, and every way of not getting an
    /// answer out of it means the same thing to the caller: ask nothing of it.
    private func fetchJSON(_ path: String, on port: Int, timeout: TimeInterval) -> [String: Any]? {
        guard let url = URL(string: "http://\(host):\(port)\(path)") else { return nil }
        var request = URLRequest(url: url)
        request.timeoutInterval = timeout
        request.cachePolicy = .reloadIgnoringLocalCacheData

        // Boxed for the same reason `probe` boxes its outcome: the wait below
        // can give up while the request is still in flight, and then the
        // completion's write races this thread's read.
        let body = Guarded<[String: Any]?>(nil)
        let finished = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { data, response, _ in
            defer { finished.signal() }
            guard let http = response as? HTTPURLResponse, http.statusCode == 200, let data,
                  let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            else { return }
            body.value = object
        }.resume()
        guard finished.wait(timeout: .now() + timeout + 2) == .success else { return nil }
        return body.value
    }

    /// What is on a port, and whether this app may run on it.
    ///
    /// One place, because two callers have to agree. The port walk asks it of
    /// every occupied candidate, and `replaceStale` asks it again of the one
    /// port an alert was answered about — and if the second were any more
    /// permissive than the first, a server the walk had just refused could be
    /// adopted a moment later by the code that was supposed to replace it.
    private func inspect(_ port: Int, timeout: TimeInterval) -> Verdict {
        switch probe(on: port, timeout: timeout) {
        case .unreachable:
            return .gone
        case .unresponsive:
            return .unidentified
        case .answered(let health):
            // Asked even when the version already settles it, because the
            // digest is also the better half of `Stale.identity` — and the
            // thing most worth being able to recognise again is exactly the
            // server an alert is about to stand open over.
            let code = self.code(on: port)
            if health.version != Self.appVersion {
                return .stale(Stale(port: port, version: health.version, health: health,
                                    reason: .version, code: code))
            }
            if code?.moved == true {
                return .stale(Stale(port: port, version: health.version, health: health,
                                    reason: .code, code: code))
            }
            return .adoptable(health)
        }
    }

    /// How `inspect` came out. `gone` and `unidentified` keep the two kinds of
    /// silence apart for the same reason `Probe` does: one says the port is
    /// empty, the other only that nothing identified itself.
    private enum Verdict {
        case adoptable(Health)
        case stale(Stale)
        case unidentified
        case gone
    }

    // MARK: - Ports

    private enum PortChoice {
        case adopt(Int)
        case start(Int)
        case stale(Stale)
        case exhausted
    }

    /// Which port this app should use: one pass over the whole range, adopting
    /// the first Doxograph that answers and otherwise starting on the first port
    /// nothing was using.
    ///
    /// It has to be one pass. Asking only about the preferred port and then
    /// looking separately for somewhere free means a first instance pushed onto
    /// 8766 by a stranger on 8765 is invisible to the second: 8766 reads as
    /// merely occupied, and the second instance starts a third server on 8767
    /// over the same corpus. The store's file locking keeps that from corrupting
    /// anything, but two servers over one corpus is not what the app promises.
    ///
    /// The pass does not stop at the first free port either, and that is the
    /// same bug one step along. A stranger holding 8765 pushes the first
    /// instance onto 8766; the stranger goes away; the next launch finds 8765
    /// free, and stopping there would start a second server beside the one still
    /// answering on 8766. A free port says nothing about the ports above it, so
    /// the free one is only remembered, and used at the end if the walk turned
    /// up no Doxograph at all.
    ///
    /// Walking the whole range is close to free, because only an occupied port
    /// costs anything. A free one is settled by the bind in `portIsFree`, which
    /// is a syscall, so the usual case — nothing else listening anywhere near
    /// 8765 — is twenty-one binds and no network at all. Probes are paid for
    /// only where something is actually listening, and they use a short timeout
    /// off the preferred port, where a stranger is the likely occupant and a
    /// local Doxograph answers `/api/health` without touching the corpus.
    ///
    /// The budget is for the pathological range: twenty services that accept a
    /// connection and then say nothing would otherwise hold the launch for the
    /// sum of their timeouts. Once it is spent the walk stops probing and takes
    /// the free port it has — but only if it has one, since with nowhere to
    /// start, finishing the walk is the only remaining hope of adopting.
    ///
    /// The walk stops at the top of the port space rather than a fixed twenty
    /// past the start. 65535 is a port a person may reasonably configure, and
    /// the ports above it do not exist: 65536 is not a slower candidate but a
    /// number `portIsFree` cannot turn into a `UInt16`, which used to trap and
    /// take the app down during launch. Clamping is also what keeps the range
    /// from inverting — `start` is a port, so the `min` never lands below it,
    /// and the guard says so for anyone who calls this with something else.
    private func choosePort(from start: Int) -> PortChoice {
        guard Self.portRange.contains(start) else { return .exhausted }
        var firstFree: Int?
        let deadline = Date().addingTimeInterval(Self.scanBudget)
        for candidate in start...min(start + Self.scanWidth, Self.portRange.upperBound) {
            if portIsFree(candidate) {
                if firstFree == nil { firstFree = candidate }
                continue
            }
            if let free = firstFree, Date() >= deadline { return .start(free) }
            // Code that does not match ends the walk exactly as a match does.
            // The server is still the one server for this corpus, so there is
            // nowhere else to go: carrying on would find a free port and start a
            // second one beside it, which is the thing the walk is built to
            // prevent. Only what happens next differs.
            switch inspect(candidate, timeout: candidate == start ? 1.5 : 0.5) {
            case .gone, .unidentified: continue
            case .stale(let server): return .stale(server)
            case .adoptable: return .adopt(candidate)
            }
        }
        guard let free = firstFree else { return .exhausted }
        return .start(free)
    }

    /// Whether nothing is listening on a port. A number that is not a port is
    /// not free — the caller has nowhere to put it, and answering the question
    /// at all is better than trapping on the conversion the way this used to.
    private func portIsFree(_ port: Int) -> Bool {
        guard let number = UInt16(exactly: port) else { return false }
        let descriptor = socket(AF_INET, SOCK_STREAM, 0)
        guard descriptor >= 0 else { return false }
        defer { close(descriptor) }
        var reuse: Int32 = 1
        setsockopt(descriptor, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout<Int32>.size))

        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = number.bigEndian
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        let bound = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return bound == 0
    }
}

/// One value two threads may touch, behind a lock. Small enough to be worth
/// having: without it a request that outlives the wait for it races the reader.
final class Guarded<Value> {
    private let lock = NSLock()
    private var stored: Value

    init(_ value: Value) { stored = value }

    var value: Value {
        get { lock.lock(); defer { lock.unlock() }; return stored }
        set { lock.lock(); defer { lock.unlock() }; stored = newValue }
    }
}

/// The server's output, kept so a failed start can say why instead of showing
/// an empty window.
final class LogBuffer {
    private var lines: [String] = []
    private let lock = NSLock()

    func append(_ text: String) {
        lock.lock()
        defer { lock.unlock() }
        lines.append(contentsOf: text.components(separatedBy: "\n"))
        if lines.count > 200 { lines.removeFirst(lines.count - 200) }
    }

    func tail(lines count: Int) -> String {
        lock.lock()
        defer { lock.unlock() }
        return lines.filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
            .suffix(count)
            .joined(separator: "\n")
    }
}
