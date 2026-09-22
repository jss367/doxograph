import AppKit
import Foundation

/// Finds the `doxograph` command the app should run.
///
/// A GUI app launched from the Dock inherits none of the shell's environment,
/// so the venv on `PATH` in Terminal is invisible here. The build bakes the
/// path it installed against into the bundle, and everything else is a fallback
/// for the day that path stops being true.
enum Locate {
    static let commandDefaultsKey = "DoxographCommand"

    static func doxographCommand() -> String? {
        var seen: [String] = []
        if let env = ProcessInfo.processInfo.environment["DOXOGRAPH_CMD"] { seen.append(env) }
        if let chosen = UserDefaults.standard.string(forKey: commandDefaultsKey) { seen.append(chosen) }
        if let baked = bakedPath() { seen.append(baked) }
        seen.append(contentsOf: knownPaths())
        if let found = seen.first(where: isRunnable) { return found }
        // Last resort, and the slow one: ask a login shell, which is the only
        // thing that knows what the user's own PATH looks like.
        if let onPath = loginShellLookup(), isRunnable(onPath) { return onPath }
        return nil
    }

    /// Remember a command the user picked by hand, so the next launch is quiet.
    static func remember(_ path: String) {
        UserDefaults.standard.set(path, forKey: commandDefaultsKey)
    }

    static func isRunnable(_ path: String) -> Bool {
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory),
              !isDirectory.boolValue else { return false }
        return FileManager.default.isExecutableFile(atPath: path)
    }

    private static func bakedPath() -> String? {
        guard let url = Bundle.main.url(forResource: "doxograph-path", withExtension: nil),
              let text = try? String(contentsOf: url, encoding: .utf8) else { return nil }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private static func knownPaths() -> [String] {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        return [
            "\(home)/git/doxograph/.venv/bin/doxograph",
            "\(home)/doxograph/.venv/bin/doxograph",
            "\(home)/.local/bin/doxograph",
            "/opt/homebrew/bin/doxograph",
            "/usr/local/bin/doxograph",
        ]
    }

    /// What the user's login shell says `doxograph` is.
    ///
    /// The profile the shell runs first can print anything — a banner, a
    /// fortune, an `echo` left in from debugging — and a login zsh runs
    /// `.zlogout` after the command, so neither the first line nor the last
    /// is reliably the answer. The answer is fenced between two markers
    /// instead, and only what is between them is read.
    ///
    /// Waiting is for the closing marker, not for the shell to exit or the
    /// pipe to close. A profile that starts something in the background hands
    /// it the pipe, and the pipe then stays open for as long as that process
    /// lives: waiting for the end of the output waited forever. Five seconds
    /// is the limit either way, after which the shell is stopped and the
    /// lookup gives up. That blocks the caller, so this is only ever called off
    /// the main thread.
    private static func loginShellLookup() -> String? {
        let shell = ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh"
        let start = "doxograph-command-start", end = "doxograph-command-end"
        let process = Process()
        process.executableURL = URL(fileURLWithPath: shell)
        process.arguments = ["-lc", "echo \(start); command -v doxograph; echo \(end)"]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        let reader = pipe.fileHandleForReading

        let output = Guarded(Data())
        let answered = DispatchSemaphore(value: 0)
        reader.readabilityHandler = { handle in
            let chunk = handle.availableData
            output.value.append(chunk)
            // An empty read is the pipe closing, which settles it too.
            if chunk.isEmpty || String(decoding: output.value, as: UTF8.self).contains("\n\(end)\n") {
                handle.readabilityHandler = nil
                answered.signal()
            }
        }
        guard (try? process.run()) != nil else {
            reader.readabilityHandler = nil
            return nil
        }
        let timedOut = answered.wait(timeout: .now() + 5) == .timedOut
        reader.readabilityHandler = nil
        if process.isRunning { process.terminate() }
        guard !timedOut else { return nil }

        let lines = String(decoding: output.value, as: UTF8.self).components(separatedBy: "\n")
        guard let opening = lines.lastIndex(of: start),
              let closing = lines[opening...].firstIndex(of: end) else { return nil }
        let path = lines[(opening + 1)..<closing]
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        return path.count == 1 ? path[0] : nil
    }
}
