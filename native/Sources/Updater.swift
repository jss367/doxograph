import AppKit
import Darwin
import Foundation

/// Brings the checkout the app runs from up to date.
///
/// The app is a launcher over the `doxograph` installed in a checkout's venv,
/// so updating it means updating the checkout: `git pull`, then `pip install
/// -e .` so a new dependency lands in the venv, then a rebuild of this bundle
/// when anything under `native/` moved. Python changes need only a server
/// restart to show up; Swift changes need the relaunch the caller does after a
/// rebuild.
enum Updater {
    static let repositoryDefaultsKey = "DoxographRepository"
    /// The commit whose dependencies and bundle were last fully installed.
    /// Written only once every step after the pull has succeeded, so a pull
    /// whose pip or build then failed is not mistaken for an update that took.
    static let updatedShaDefaultsKey = "DoxographUpdatedSha"

    struct Outcome {
        /// `git log --oneline` for what the pull brought in; empty when nothing did.
        let changes: String
        /// True when `native/` changed and the running bundle was replaced.
        let appRebuilt: Bool
    }

    enum Failure: Error {
        /// Which step failed, and what it printed.
        case step(String, String)
    }

    private static let queue = DispatchQueue(label: "com.jss367.doxograph.updater")

    // MARK: - Finding the checkout

    /// The checkout a `doxograph` command was installed from, judged by
    /// walking up from the command until a directory looks like this repo.
    /// `<repo>/.venv/bin/doxograph` is what `build.sh` records, so this is
    /// usually three steps.
    static func repository(near command: String) -> URL? {
        var directory = URL(fileURLWithPath: command).deletingLastPathComponent()
        while directory.path != "/" {
            if isRepository(directory) { return directory }
            directory.deleteLastPathComponent()
        }
        return nil
    }

    static func rememberedRepository() -> URL? {
        guard let path = UserDefaults.standard.string(forKey: repositoryDefaultsKey) else { return nil }
        let url = URL(fileURLWithPath: path)
        return isRepository(url) ? url : nil
    }

    static func remember(_ repository: URL) {
        UserDefaults.standard.set(repository.path, forKey: repositoryDefaultsKey)
    }

    static func isRepository(_ directory: URL) -> Bool {
        let files = FileManager.default
        return files.fileExists(atPath: directory.appendingPathComponent("pyproject.toml").path)
            && files.fileExists(atPath: directory.appendingPathComponent("native/build.sh").path)
            && files.fileExists(atPath: directory.appendingPathComponent(".git").path)
    }

    // MARK: - Finding the interpreter

    /// The Python a console script runs under, so `pip` installs into the
    /// environment that owns the command rather than one that happens to share
    /// its directory. pip writes the interpreter into the script's `#!` line,
    /// which is what a venv, pipx or Homebrew install all have; a wrapper
    /// without one falls back to a `python` beside the command, and a command
    /// with neither cannot be updated past a `pyproject.toml` change.
    static func interpreter(for command: String) -> String? {
        if let named = shebangInterpreter(of: command), Locate.isRunnable(named) { return named }
        let sibling = ((command as NSString).deletingLastPathComponent as NSString)
            .appendingPathComponent("python")
        return Locate.isRunnable(sibling) ? sibling : nil
    }

    /// The path on the first line of a script when it is `#!/some/python`.
    /// `#!/usr/bin/env python` names no interpreter, and the `#!/bin/sh`
    /// trampoline pip writes when the venv path has a space in it names the
    /// wrong one, so only a direct path to something called python counts.
    private static func shebangInterpreter(of command: String) -> String? {
        guard let handle = FileHandle(forReadingAtPath: command) else { return nil }
        defer { try? handle.close() }
        guard let data = try? handle.read(upToCount: 1024) else { return nil }
        let first = String(decoding: data, as: UTF8.self)
            .split(separator: "\n", maxSplits: 1, omittingEmptySubsequences: false).first ?? ""
        guard first.hasPrefix("#!") else { return nil }
        let words = first.dropFirst(2).split(separator: " ").map { $0.trimmingCharacters(in: .whitespaces) }
        guard let path = words.first, path.hasPrefix("/"),
              URL(fileURLWithPath: path).lastPathComponent.hasPrefix("python") else { return nil }
        return path
    }

    // MARK: - Updating

    /// Pull, reinstall, and rebuild if needed. `progress` and `completion` are
    /// called on the main thread.
    static func run(
        command: String,
        repository: URL,
        progress: @escaping (String) -> Void,
        completion: @escaping (Result<Outcome, Failure>) -> Void
    ) {
        queue.async {
            let result = update(command: command, repository: repository) { text in
                DispatchQueue.main.async { progress(text) }
            }
            DispatchQueue.main.async { completion(result) }
        }
    }

    private static func update(
        command: String, repository: URL, progress: (String) -> Void
    ) -> Result<Outcome, Failure> {
        let binDirectory = (command as NSString).deletingLastPathComponent
        let path = [
            binDirectory, "/usr/bin", "/bin", "/usr/sbin", "/sbin", "/usr/local/bin", "/opt/homebrew/bin",
        ].joined(separator: ":")
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = path
        // A Dock launch has no terminal, so a git that wants a password would
        // hang forever. Better to fail with a message.
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GIT_SSH_COMMAND"] = "ssh -oBatchMode=yes"
        // Whatever build.sh records must be the command this app already runs,
        // not whatever it would guess from PATH.
        environment["DOXOGRAPH_CMD"] = command

        func git(_ arguments: [String], timeout: TimeInterval = 120) -> Step {
            step("/usr/bin/git", ["-C", repository.path] + arguments, environment, cwd: repository, timeout: timeout)
        }

        progress("Checking for updates…")
        let before = git(["rev-parse", "HEAD"])
        guard before.succeeded else { return .failure(.step("git rev-parse", before.output)) }
        // The first update has nothing recorded yet. Take where it starts as
        // the last fully installed commit, so a pip or build failure on this
        // run is retried on the next one instead of being read as up to date.
        if UserDefaults.standard.string(forKey: updatedShaDefaultsKey) == nil {
            UserDefaults.standard.set(before.output.trimmed, forKey: updatedShaDefaultsKey)
        }

        let pull = git(["pull", "--ff-only"])
        guard pull.succeeded else { return .failure(.step("git pull", pull.output)) }

        let after = git(["rev-parse", "HEAD"])
        guard after.succeeded else { return .failure(.step("git rev-parse", after.output)) }
        let head = after.output.trimmed

        // A pull that landed but whose pip or build then failed has already
        // moved HEAD, so measuring from where this run started would call the
        // checkout up to date and never retry those steps. Measure instead
        // from the last commit that was fully installed, when it is behind
        // where this run started. When it is not (a rewound checkout, a first
        // update) the recorded commit says nothing about this history.
        var base = before.output.trimmed
        if let completed = UserDefaults.standard.string(forKey: updatedShaDefaultsKey),
           completed != base,
           git(["merge-base", "--is-ancestor", completed, base]).succeeded {
            base = completed
        }
        guard base != head else {
            UserDefaults.standard.set(head, forKey: updatedShaDefaultsKey)
            return .success(Outcome(changes: "", appRebuilt: false))
        }
        let range = "\(base)..\(head)"
        let changes = git(["log", "--oneline", "--no-decorate", range]).output.trimmed
        let changed = git(["diff", "--name-only", range]).output
            .split(separator: "\n").map(String.init)

        // Editable installs pick up code changes on their own, but not a new
        // dependency or a new console script, so reinstall when the metadata
        // moved. It is cheap, and the failure mode of skipping it is a server
        // that dies on import.
        if changed.contains("pyproject.toml") {
            progress("Installing dependencies…")
            guard let python = interpreter(for: command) else {
                return .failure(.step("pip install", """
                    Could not tell which Python runs \(command): its first line \
                    names no interpreter, and there is no python beside it.
                    """))
            }
            let pip = step(python, ["-m", "pip", "install", "-e", repository.path],
                           environment, cwd: repository, timeout: 600)
            guard pip.succeeded else { return .failure(.step("pip install", pip.output)) }
        }

        var appRebuilt = false
        if changed.contains(where: { $0.hasPrefix("native/") }) {
            progress("Rebuilding the app…")
            let build = step("/bin/bash", [repository.appendingPathComponent("native/build.sh").path],
                             environment, cwd: repository, timeout: 600)
            guard build.succeeded else { return .failure(.step("native/build.sh", build.output)) }
            do {
                try install(built: repository.appendingPathComponent("native/build/Doxograph.app"))
            } catch {
                return .failure(.step("installing the app", error.localizedDescription))
            }
            appRebuilt = true
        }

        UserDefaults.standard.set(head, forKey: updatedShaDefaultsKey)
        return .success(Outcome(changes: changes, appRebuilt: appRebuilt))
    }

    /// Put the freshly built bundle where the running one is. When the app is
    /// running out of `native/build/` the build already replaced it in place.
    ///
    /// The swap goes through siblings so that a failure at any point leaves
    /// an installed app: the copy lands beside the running bundle first, the
    /// running bundle steps aside, and only then does the copy take its place.
    /// Deleting first and copying second would leave nothing to launch if the
    /// copy failed. A rename within one directory cannot half-succeed the way
    /// a copy can, and if the second rename does fail the old bundle is put
    /// back.
    ///
    /// Moving a running bundle is fine on macOS: the executable stays mapped
    /// until the process exits, and the relaunch that follows opens the new one.
    private static func install(built: URL) throws {
        let running = Bundle.main.bundleURL.resolvingSymlinksInPath()
        guard running.pathExtension == "app",
              running.path != built.resolvingSymlinksInPath().path else { return }
        let files = FileManager.default
        let staged = running.appendingPathExtension("new")
        let retired = running.appendingPathExtension("old")
        try? files.removeItem(at: staged)
        try? files.removeItem(at: retired)
        do {
            try files.copyItem(at: built, to: staged)
        } catch {
            try? files.removeItem(at: staged)
            throw error
        }
        try files.moveItem(at: running, to: retired)
        do {
            try files.moveItem(at: staged, to: running)
        } catch {
            try? files.moveItem(at: retired, to: running)
            try? files.removeItem(at: staged)
            throw error
        }
        try? files.removeItem(at: retired)
    }

    // MARK: - Running things

    private struct Step {
        let status: Int32
        let output: String
        var succeeded: Bool { status == 0 }
    }

    private static func step(
        _ executable: String, _ arguments: [String], _ environment: [String: String],
        cwd: URL, timeout: TimeInterval
    ) -> Step {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.environment = environment
        process.currentDirectoryURL = cwd
        process.standardInput = FileHandle.nullDevice
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe

        do {
            try process.run()
        } catch {
            return Step(status: -1, output: "\(executable): \(error.localizedDescription)")
        }

        // Drain the pipe while waiting, or a chatty step fills it and stalls.
        var output = Data()
        let drained = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            output = pipe.fileHandleForReading.readDataToEndOfFile()
            drained.signal()
        }
        let exited = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            process.waitUntilExit()
            exited.signal()
        }
        if exited.wait(timeout: .now() + timeout) == .timedOut {
            // Coming back with the process still alive would let a pip or a
            // build that ignored the deadline keep changing the checkout under
            // the next update. Ask it to stop, then make it, and do not return
            // until it is gone.
            if process.isRunning { process.terminate() }
            if exited.wait(timeout: .now() + 5) == .timedOut {
                kill(process.processIdentifier, SIGKILL)
                exited.wait()
            }
            // The pipe closes when its last writer does, and a grandchild the
            // signal never reached (build.sh's swiftc, say) can hold it open
            // after the child is dead. Nothing it prints is wanted now, so
            // give the drain a moment and otherwise leave it to finish alone.
            _ = drained.wait(timeout: .now() + 5)
            let name = ([executable] + arguments).joined(separator: " ")
            return Step(status: -1, output: "Gave up on `\(name)` after \(Int(timeout)) seconds.")
        }
        drained.wait()
        return Step(status: process.terminationStatus, output: String(decoding: output, as: UTF8.self))
    }
}

private extension String {
    var trimmed: String { trimmingCharacters(in: .whitespacesAndNewlines) }
}
