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

    private static func loginShellLookup() -> String? {
        let shell = ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh"
        let process = Process()
        process.executableURL = URL(fileURLWithPath: shell)
        process.arguments = ["-lc", "command -v doxograph"]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        guard (try? process.run()) != nil else { return nil }

        // A login shell runs the user's whole profile, which can hang on
        // anything. Give it a few seconds and then stop waiting.
        let finished = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            process.waitUntilExit()
            finished.signal()
        }
        if finished.wait(timeout: .now() + 5) == .timedOut {
            process.terminate()
            return nil
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let path = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        return path.isEmpty ? nil : path.components(separatedBy: "\n").first
    }
}
