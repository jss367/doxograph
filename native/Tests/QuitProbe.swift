import AppKit

/// Exercise the real probe delivery without touching a running library server.
private final class HealthProtocol: URLProtocol {
    private static var requests = 0

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.requests += 1
        // The initial probe is idle, the first quit is canceled over work,
        // and the next quit succeeds once that work is done.
        let jobs = Self.requests == 2 ? 1 : 0
        let data = Data("{\"app\":\"doxograph\",\"jobs\":\(jobs),\"arriving\":0}".utf8)
        let response = HTTPURLResponse(url: request.url!, statusCode: 200,
                                       httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

private final class QuitDelegate: NSObject, NSApplicationDelegate {
    private let server = ServerController()
    private var attempts = 0

    func applicationDidFinishLaunching(_ notification: Notification) {
        server.probe { probe in
            precondition(Thread.isMainThread)
            guard case .answered(let health) = probe, health.jobs == 0 else {
                fatalError("The ordinary health probe did not return its response")
            }
            // The update completion calls terminate while servicing the main
            // queue. That queue cannot deliver another block until this ends.
            DispatchQueue.main.async {
                NSApp.terminate(nil)
                precondition(self.attempts == 1, "The first quit should be canceled")
                NSApp.terminate(nil)
                fatalError("The second quit should exit")
            }
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        attempts += 1
        server.probe { probe in
            precondition(Thread.isMainThread)
            guard case .answered(let health) = probe else {
                fatalError("The quit health probe did not return its response")
            }
            precondition(health.jobs == (self.attempts == 1 ? 1 : 0))
            sender.reply(toApplicationShouldTerminate: health.jobs == 0)
        }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        precondition(attempts == 2)
        print("PASS: ordinary probe, canceled quit, and successful quit from the main queue")
    }
}

@main
private enum QuitProbeTest {
    static func main() {
        URLProtocol.registerClass(HealthProtocol.self)
        // A regression must fail instead of leaving another stuck app behind.
        DispatchQueue.global().asyncAfter(deadline: .now() + 10) {
            fputs("FAIL: quit probe never completed\n", stderr)
            exit(1)
        }
        let application = NSApplication.shared
        let delegate = QuitDelegate()
        application.delegate = delegate
        application.setActivationPolicy(.prohibited)
        application.run()
    }
}
