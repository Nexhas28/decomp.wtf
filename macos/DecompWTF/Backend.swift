import AppKit
import Foundation

/// Finds decomp.wtf on this Mac, starts it (or attaches to the one already running) and stops it again on quit.
/// The Python app is the whole of decomp.wtf; this only runs it and hands its address to the window.
@MainActor
final class Backend: ObservableObject {
    enum Phase: Equatable {
        case checking
        case needsUV
        case needsCheckout
        case starting
        case running(URL)
        case failed(String)
    }

    enum DockerState: Equatable { case unknown, ready, notRunning, missing }

    @Published private(set) var phase: Phase = .checking
    @Published private(set) var docker: DockerState = .unknown
    @Published private(set) var logTail = ""
    @Published private(set) var startedAt: Date?
    /// The running server's API, for the native screens.
    @Published private(set) var api: DecompAPI?

    private var process: Process?
    private var server: (port: Int, secret: String)?
    private var startedByUs = false
    private var generation = 0

    static let checkoutKey = "decompCheckoutPath"

    // MARK: places

    /// Where decomp.wtf keeps projects: DECOMP_HOME, or ~/Decomp (the same rule as `decomp` itself).
    nonisolated static var home: URL {
        if let custom = ProcessInfo.processInfo.environment["DECOMP_HOME"], !custom.isEmpty {
            return URL(fileURLWithPath: (custom as NSString).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Decomp")
    }

    nonisolated static var stateFile: URL { home.appendingPathComponent(".decomp/server.json") }
    nonisolated static var logFile: URL { home.appendingPathComponent(".decomp/mac-app.log") }

    /// An app opened from Finder only gets /usr/bin:/bin:/usr/sbin:/sbin, which has neither uv nor docker
    /// (and decomp.wtf itself runs docker for the Linux-only compilers).
    nonisolated static let searchPath: String = {
        let user = FileManager.default.homeDirectoryForCurrentUser.path
        var dirs = ["\(user)/.local/bin", "\(user)/.cargo/bin", "/opt/homebrew/bin", "/usr/local/bin",
                    "/Applications/Docker.app/Contents/Resources/bin", "\(user)/.docker/bin"]
        dirs += (ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":").map(String.init)
        dirs += ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        var seen = Set<String>()
        return dirs.filter { !$0.isEmpty && seen.insert($0).inserted }.joined(separator: ":")
    }()

    nonisolated static func which(_ name: String) -> URL? {
        for dir in searchPath.split(separator: ":") {
            let url = URL(fileURLWithPath: String(dir)).appendingPathComponent(name)
            if FileManager.default.isExecutableFile(atPath: url.path) { return url }
        }
        return nil
    }

    /// A folder holding decomp.wtf's source (a git checkout), which `uv run decomp` runs from.
    nonisolated static func isCheckout(_ url: URL) -> Bool {
        guard let text = try? String(contentsOf: url.appendingPathComponent("pyproject.toml"), encoding: .utf8) else {
            return false
        }
        return text.contains("decompnet")
    }

    var checkout: URL? {
        if let saved = UserDefaults.standard.string(forKey: Self.checkoutKey) {
            let url = URL(fileURLWithPath: saved)
            if Self.isCheckout(url) { return url }
        }
        // Built from a checkout (macos/build/...): use the checkout it was built from.
        var dir = Bundle.main.bundleURL.deletingLastPathComponent()
        while dir.path != "/" {
            if Self.isCheckout(dir) { return dir }
            dir = dir.deletingLastPathComponent()
        }
        return nil
    }

    // MARK: start and stop

    func start() {
        generation += 1
        let gen = generation
        phase = .checking
        logTail = ""
        Task { await self.checkDocker() }
        Task {
            if let running = await Self.runningServer() {
                guard gen == self.generation else { return }
                self.server = running
                self.startedByUs = false
                self.setRunning(running)
                return
            }
            self.launch(gen)
        }
    }

    private func launch(_ gen: Int) {
        let exe: URL
        var args: [String]
        var cwd = Self.home
        if let repo = checkout {
            guard let uv = Self.which("uv") else { phase = .needsUV; return }
            exe = uv
            args = ["run", "--project", repo.path, "decomp", "--no-browser"]
            cwd = repo
        } else if let installed = Self.which("decomp") {
            exe = installed
            args = ["--no-browser"]
        } else {
            phase = .needsCheckout
            return
        }

        let fm = FileManager.default
        try? fm.createDirectory(at: Self.logFile.deletingLastPathComponent(), withIntermediateDirectories: true)
        fm.createFile(atPath: Self.logFile.path, contents: nil)
        // A file, not a Pipe: nobody would drain a pipe once the window is up, and a full pipe stalls the server.
        guard let log = try? FileHandle(forWritingTo: Self.logFile) else {
            phase = .failed("Couldn't write the log file at \(Self.logFile.path).")
            return
        }

        let proc = Process()
        proc.executableURL = exe
        proc.arguments = args
        proc.currentDirectoryURL = cwd
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = Self.searchPath
        env["PYTHONUNBUFFERED"] = "1"
        // decomp.wtf finds the reverse-engineering tools (local-tools/) itself; DECOMP_LOCAL_TOOLS, when set, passes through.
        proc.environment = env
        proc.standardOutput = log
        proc.standardError = log
        proc.standardInput = FileHandle.nullDevice
        proc.terminationHandler = { [weak self] p in
            let code = p.terminationStatus
            Task { @MainActor in self?.processEnded(p, code: code) }
        }

        let launchedAt = Date()
        do {
            try proc.run()
        } catch {
            phase = .failed("Couldn't start decomp.wtf: \(error.localizedDescription)")
            return
        }
        process = proc
        startedByUs = true
        startedAt = launchedAt
        phase = .starting

        Task {
            // The first start builds Python's environment, which can take a few minutes.
            let deadline = Date().addingTimeInterval(600)
            while Date() < deadline, gen == self.generation, self.phase == .starting {
                self.refreshLog()
                if let running = await Self.runningServer(newerThan: launchedAt) {
                    self.server = running
                    self.setRunning(running)
                    return
                }
                try? await Task.sleep(nanoseconds: 500_000_000)
            }
            if gen == self.generation, self.phase == .starting {
                self.refreshLog()
                self.phase = .failed("decomp.wtf took too long to start.")
            }
        }
    }

    private func processEnded(_ p: Process, code: Int32) {
        guard p === process else { return }
        process = nil
        refreshLog()
        switch phase {
        case .starting:
            // `decomp` exits straight away when another copy is already running (or, in some installs, after
            // starting the server in the background): attach to it. One that came up after we launched is ours.
            Task {
                if let running = await Self.runningServer() {
                    let ours = await Self.runningServer(newerThan: self.startedAt) != nil
                    self.server = running
                    self.startedByUs = ours
                    self.setRunning(running)
                } else {
                    self.phase = .failed("decomp.wtf stopped while starting (exit code \(code)).")
                }
            }
        case .running:
            guard startedByUs else { break }
            if code == 0 {
                // The page's own Quit button: the app goes with it.
                startedByUs = false
                quitApp()
            } else {
                phase = .failed("decomp.wtf stopped (exit code \(code)).")
            }
        default:
            break
        }
    }

    private func setRunning(_ s: (port: Int, secret: String)) {
        api = DecompAPI(port: s.port, secret: s.secret)
        phase = .running(Self.openURL(s))
        watch(generation, s)
    }

    /// The page's own Quit button stopped the server: the app goes with it.
    func pageQuit() {
        generation += 1
        startedByUs = false
        quitApp()
    }

    /// While the page is up, notice the server going away (stopped elsewhere, crashed) instead of leaving a dead page.
    /// Patient: decomp.wtf's own updater restarts the server in place, and the page carries on by itself after.
    private func watch(_ gen: Int, _ s: (port: Int, secret: String)) {
        Task {
            var misses = 0
            while gen == self.generation, case .running = self.phase {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                guard gen == self.generation else { return }
                var req = URLRequest(url: URL(string: "http://127.0.0.1:\(s.port)/health")!)
                req.timeoutInterval = 2
                let alive = (try? await URLSession.shared.data(for: req)) != nil
                misses = alive ? 0 : misses + 1
                if misses >= 20, gen == self.generation, case .running = self.phase {
                    self.refreshLog()
                    self.phase = .failed("decomp.wtf stopped.")
                    return
                }
            }
        }
    }

    func restart() {
        Task {
            await stop()
            start()
        }
    }

    /// Stops the server if this app started it; one that was already running is left alone.
    func stop() async {
        generation += 1
        guard startedByUs else { return }
        phase = .checking  // so the server exiting now isn't taken for a crash
        let proc = process
        if let server { await Self.quit(server) }
        if let proc {
            for _ in 0..<50 where proc.isRunning { try? await Task.sleep(nanoseconds: 100_000_000) }
            if proc.isRunning { proc.terminate() }
        }
        process = nil
        server = nil
        startedByUs = false
        phase = .checking
    }

    func useCheckout(_ url: URL) {
        UserDefaults.standard.set(url.path, forKey: Self.checkoutKey)
        start()
    }

    func refreshLog() {
        guard let data = try? Data(contentsOf: Self.logFile) else { return }
        let text = String(decoding: data.suffix(6000), as: UTF8.self)
        logTail = text.split(separator: "\n", omittingEmptySubsequences: false).suffix(30).joined(separator: "\n")
    }

    // MARK: Docker

    func checkDocker() async {
        guard let docker = Self.which("docker") else {
            self.docker = .missing
            return
        }
        let ok = await Task.detached { () -> Bool in
            let p = Process()
            p.executableURL = docker
            p.arguments = ["info", "--format", "{{.ServerVersion}}"]
            var env = ProcessInfo.processInfo.environment
            env["PATH"] = Backend.searchPath
            p.environment = env
            p.standardOutput = FileHandle.nullDevice
            p.standardError = FileHandle.nullDevice
            do { try p.run() } catch { return false }
            p.waitUntilExit()
            return p.terminationStatus == 0
        }.value
        self.docker = ok ? .ready : .notRunning
    }

    // MARK: talking to the server

    nonisolated static func openURL(_ s: (port: Int, secret: String)) -> URL {
        var c = URLComponents()
        c.scheme = "http"
        c.host = "127.0.0.1"
        c.port = s.port
        c.path = "/"
        c.queryItems = [URLQueryItem(name: "k", value: s.secret)]
        return c.url!
    }

    /// The decomp.wtf answering at the address in server.json, if any (and, with `newerThan`, only one that wrote
    /// server.json after that moment: an old file from a crashed run doesn't count).
    nonisolated static func runningServer(newerThan: Date? = nil) async -> (port: Int, secret: String)? {
        guard let data = try? Data(contentsOf: stateFile),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let port = json["port"] as? Int, let secret = json["secret"] as? String else { return nil }
        if let newerThan,
           let modified = try? stateFile.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate,
           modified < newerThan.addingTimeInterval(-1) {
            return nil
        }
        var req = URLRequest(url: URL(string: "http://127.0.0.1:\(port)/health")!)
        req.timeoutInterval = 1.5
        guard let (body, _) = try? await URLSession.shared.data(for: req),
              let health = try? JSONSerialization.jsonObject(with: body) as? [String: Any],
              health["app"] as? String == "decomp-wtf" else { return nil }
        return (port, secret)
    }

    nonisolated static func quit(_ s: (port: Int, secret: String)) async {
        var req = URLRequest(url: URL(string: "http://127.0.0.1:\(s.port)/api/quit")!)
        req.httpMethod = "POST"
        req.httpBody = Data("{}".utf8)
        req.timeoutInterval = 10
        req.httpShouldHandleCookies = false
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("1", forHTTPHeaderField: "X-Decomp")
        req.setValue("decomp_live=\(s.secret)", forHTTPHeaderField: "Cookie")
        _ = try? await URLSession.shared.data(for: req)
    }
}

/// Quits the app from code. Not NSApp.terminate straight from a Swift task: quitting waits (on the main queue) for
/// decomp.wtf to stop, while that task is still holding the main queue, so the app froze instead of quitting.
@MainActor
func quitApp() {
    NSApp.perform(#selector(NSApplication.terminate(_:)), with: nil, afterDelay: 0)
}
