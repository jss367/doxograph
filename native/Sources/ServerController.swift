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
    }

    private static let preferredPort = 8765
    private static let portDefaultsKey = "DoxographPort"
    /// How long the port walk may spend probing before it settles for the free
    /// port it has already found. Only unresponsive occupants can run this out.
    private static let scanBudget: TimeInterval = 5

    private let host = "127.0.0.1"
    private let queue = DispatchQueue(label: "com.jss367.doxograph.server")
    private let log = LogBuffer()
    private var process: Process?
    private var port = ServerController.preferredPort

    /// True when this app started the server, and so is responsible for it.
    private(set) var ownsServer = false

    var baseURL: URL { URL(string: "http://\(host):\(port)")! }

    // MARK: - Starting

    func start(onReady: @escaping (URL) -> Void, onFailure: @escaping (Failure) -> Void) {
        queue.async {
            let result = self.bringUp()
            DispatchQueue.main.async {
                switch result {
                case .success: onReady(self.baseURL)
                case .failure(let failure): onFailure(failure)
                }
            }
        }
    }

    private enum Outcome {
        case success
        case failure(Failure)
    }

    private func bringUp() -> Outcome {
        let preferred = UserDefaults.standard.object(forKey: Self.portDefaultsKey) as? Int
            ?? Self.preferredPort

        switch choosePort(from: preferred) {
        case .exhausted:
            return .failure(.launchFailed("No free port near \(preferred)."))

        case .adopt(let existing):
            port = existing
            ownsServer = false
            return .success

        case .start(let free):
            guard let command = Locate.doxographCommand() else { return .failure(.commandNotFound) }
            port = free
            do {
                try spawn(command: command)
            } catch {
                return .failure(.launchFailed("\(command): \(error.localizedDescription)"))
            }
            ownsServer = true
            return waitUntilAnswering()
        }
    }

    private func spawn(command: String) throws {
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
            guard !data.isEmpty else { return }
            log.append(String(decoding: data, as: UTF8.self))
        }

        try process.run()
        self.process = process
    }

    /// Poll until uvicorn binds. Imports alone take a couple of seconds, and a
    /// cold start on a slow disk takes longer, so the ceiling is generous.
    private func waitUntilAnswering() -> Outcome {
        let deadline = Date().addingTimeInterval(60)
        while Date() < deadline {
            if health(on: port, timeout: 1) != nil { return .success }
            if let process, !process.isRunning {
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
        stop()
        return .failure(.neverAnswered(detail))
    }

    // MARK: - Stopping

    /// Ask the server to shut down, and give uvicorn a moment to do it cleanly.
    /// An adopted server is left alone: this app did not start it.
    func stop() {
        guard ownsServer, let process, process.isRunning else { return }
        process.terminate()
        let exited = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            process.waitUntilExit()
            exited.signal()
        }
        if exited.wait(timeout: .now() + 5) == .timedOut {
            kill(process.processIdentifier, SIGKILL)
        }
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
        /// Upload requests whose bodies are still arriving. These die with
        /// whoever is sending them — including this app, whose web view posts a
        /// paper dropped on the page straight to the server.
        let arriving: Int
    }

    /// What the server is in the middle of, or nil if it is not answering.
    /// Used to warn before quitting on top of work.
    func health(completion: @escaping (Health?) -> Void) {
        queue.async {
            let health = self.health(on: self.port, timeout: 2)
            DispatchQueue.main.async { completion(health) }
        }
    }

    /// Returns what the server is working on, or nil when whatever is on the
    /// port is not doxograph. Checking the name matters: adopting a stranger's
    /// port would show the user someone else's web app.
    private func health(on port: Int, timeout: TimeInterval) -> Health? {
        guard let url = URL(string: "http://\(host):\(port)/api/health") else { return nil }
        var request = URLRequest(url: url)
        request.timeoutInterval = timeout
        request.cachePolicy = .reloadIgnoringLocalCacheData

        var health: Health?
        let finished = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { data, response, _ in
            defer { finished.signal() }
            guard let http = response as? HTTPURLResponse, http.statusCode == 200, let data,
                  let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  body["app"] as? String == "doxograph" else { return }
            // A server too old to split the count answers `busy` alone. Read it
            // as jobs, which is how it was read before the split. Adoption makes
            // that reachable: the server on the port can be an older install
            // than the app that found it.
            let busy = body["busy"] as? Int ?? 0
            health = Health(jobs: body["jobs"] as? Int ?? busy,
                            arriving: body["arriving"] as? Int ?? 0)
        }.resume()
        _ = finished.wait(timeout: .now() + timeout + 2)
        return health
    }

    // MARK: - Ports

    private enum PortChoice {
        case adopt(Int)
        case start(Int)
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
    private func choosePort(from start: Int) -> PortChoice {
        var firstFree: Int?
        let deadline = Date().addingTimeInterval(Self.scanBudget)
        for candidate in start...(start + 20) {
            if portIsFree(candidate) {
                if firstFree == nil { firstFree = candidate }
                continue
            }
            if let free = firstFree, Date() >= deadline { return .start(free) }
            if health(on: candidate, timeout: candidate == start ? 1.5 : 0.5) != nil {
                return .adopt(candidate)
            }
        }
        guard let free = firstFree else { return .exhausted }
        return .start(free)
    }

    private func portIsFree(_ port: Int) -> Bool {
        let descriptor = socket(AF_INET, SOCK_STREAM, 0)
        guard descriptor >= 0 else { return false }
        defer { close(descriptor) }
        var reuse: Int32 = 1
        setsockopt(descriptor, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout<Int32>.size))

        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = UInt16(port).bigEndian
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        let bound = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return bound == 0
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
