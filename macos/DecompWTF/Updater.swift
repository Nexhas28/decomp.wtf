import AppKit
import Foundation

/// Updates the Mac app itself from GitHub Releases. (decomp.wtf's own updater, in the page, updates the Python app;
/// this one only replaces the .app, so the two never touch the same thing.)
///
/// A Mac release is a GitHub release tagged `mac-v<version>` with a `decomp.wtf-mac-<version>.zip` asset holding
/// decomp.wtf.app (macos/release.sh and .github/workflows/mac-release.yml make them).
@MainActor
final class Updater: ObservableObject {
    nonisolated static let repo = "Nexhas28/decomp.wtf"
    nonisolated static let assetPrefix = "decomp.wtf-mac-"

    struct Release {
        let version: String
        let zip: URL
        let notes: String
    }

    @Published private(set) var busy = false

    private let defaults = UserDefaults.standard
    private static let lastCheckKey = "updateLastCheck"
    private static let skippedKey = "updateSkippedVersion"
    private static let pendingKey = "updatePendingVersion"
    private static let fromKey = "updatePendingFrom"
    private static let recheck: TimeInterval = 6 * 3600  // GitHub allows 60 unauthenticated API calls an hour

    nonisolated static var current: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0"
    }

    /// The releases to look at. DECOMP_MAC_UPDATE_FEED points it somewhere else (a test feed).
    nonisolated static var feed: URL {
        if let custom = ProcessInfo.processInfo.environment["DECOMP_MAC_UPDATE_FEED"], let url = URL(string: custom) {
            return url
        }
        return URL(string: "https://api.github.com/repos/\(repo)/releases?per_page=30")!
    }

    nonisolated static func key(_ v: String) -> [Int] {
        let parts = v.split(whereSeparator: { !$0.isNumber }).prefix(3).compactMap { Int($0) }
        return parts.isEmpty ? [0] : parts
    }

    nonisolated static func newer(_ a: String, than b: String) -> Bool {
        let x = key(a), y = key(b)
        for i in 0..<max(x.count, y.count) {
            let l = i < x.count ? x[i] : 0, r = i < y.count ? y[i] : 0
            if l != r { return l > r }
        }
        return false
    }

    private static var updateLog: URL { Backend.home.appendingPathComponent(".decomp/mac-update.log") }

    // MARK: checking

    /// On launch: say how the last update went, then look for a new one (at most every few hours).
    func launched() {
        reportFinishedUpdate()
        let last = defaults.double(forKey: Self.lastCheckKey)
        let forced = ProcessInfo.processInfo.environment["DECOMP_MAC_UPDATE_FEED"] != nil
        guard forced || Date().timeIntervalSince1970 - last > Self.recheck else { return }
        Task {
            try? await Task.sleep(nanoseconds: 3_000_000_000)  // let the window come up first
            await check(userAsked: false)
        }
    }

    func check(userAsked: Bool) async {
        guard !busy else { return }
        let found: Release?
        do {
            found = try await Self.latest()
            defaults.set(Date().timeIntervalSince1970, forKey: Self.lastCheckKey)
        } catch {
            if userAsked {
                await say("Couldn't check for updates",
                    "decomp.wtf couldn't reach GitHub to look for a new version. Check your internet connection "
                    + "and try again.\n\nWhat went wrong: \(error.localizedDescription)")
            }
            return
        }
        guard let release = found, Self.newer(release.version, than: Self.current) else {
            if userAsked {
                await say("You're up to date", "decomp.wtf for Mac \(Self.current) is the newest version.")
            }
            return
        }
        if !userAsked, defaults.string(forKey: Self.skippedKey) == release.version { return }
        await offer(release)
    }

    /// The newest published Mac release: not a draft or pre-release, with the app's zip attached.
    nonisolated static func latest() async throws -> Release? {
        var req = URLRequest(url: feed)
        req.timeoutInterval = 20
        req.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        req.setValue("decomp.wtf-mac/\(current) (update check)", forHTTPHeaderField: "User-Agent")
        let (data, response) = try await URLSession.shared.data(for: req)
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            throw UpdateError("GitHub answered with error \(http.statusCode)")
        }
        let json = try JSONSerialization.jsonObject(with: data)
        let list = (json as? [[String: Any]]) ?? [json as? [String: Any]].compactMap { $0 }
        var best: Release?
        for rel in list where rel["draft"] as? Bool != true && rel["prerelease"] as? Bool != true {
            let assets = rel["assets"] as? [[String: Any]] ?? []
            guard let asset = assets.first(where: {
                let name = $0["name"] as? String ?? ""
                return name.hasPrefix(assetPrefix) && name.hasSuffix(".zip")
            }), let link = asset["browser_download_url"] as? String, let url = URL(string: link) else { continue }
            let tag = rel["tag_name"] as? String ?? ""
            let version = tag.replacingOccurrences(of: "mac-v", with: "").replacingOccurrences(of: "v", with: "")
            if best == nil || newer(version, than: best!.version) {
                best = Release(version: version, zip: url, notes: rel["body"] as? String ?? "")
            }
        }
        return best
    }

    private func offer(_ release: Release) async {
        let alert = NSAlert()
        alert.messageText = "A new version of decomp.wtf for Mac is ready"
        var text = "Version \(release.version) is out (you have \(Self.current)).\n\n"
            + "Install it now? The app closes, updates itself and opens again in a few seconds. "
            + "Your projects aren't touched, but running agents stop."
        let notes = release.notes.trimmingCharacters(in: .whitespacesAndNewlines)
        if !notes.isEmpty { text += "\n\nWhat's new:\n" + String(notes.prefix(800)) }
        alert.informativeText = text
        alert.addButton(withTitle: "Install Now")
        alert.addButton(withTitle: "Later")
        alert.addButton(withTitle: "Skip This Version")
        switch await present(alert) {
        case .alertFirstButtonReturn:
            Task { await install(release) }
        case .alertThirdButtonReturn:
            defaults.set(release.version, forKey: Self.skippedKey)
        default:
            break
        }
    }

    // MARK: installing

    func install(_ release: Release) async {
        busy = true
        defer { busy = false }
        do {
            let app = Bundle.main.bundleURL
            let folder = app.deletingLastPathComponent()
            guard FileManager.default.isWritableFile(atPath: folder.path) else {
                throw UpdateError("the app's folder (\(folder.path)) can't be changed from here. Move "
                                  + "decomp.wtf for Mac to your Applications folder and try again")
            }
            let fresh = try await Self.download(release)
            try startSwap(old: app, new: fresh, version: release.version)
        } catch {
            await say("The update couldn't be installed",
                "decomp.wtf for Mac keeps working as it is.\n\nWhat went wrong: \(error.localizedDescription)")
            return
        }
        quitApp()  // stops decomp.wtf; the helper swaps the app once this one has quit
    }

    /// Downloads and unpacks the release, and checks it's really this app at that version.
    nonisolated static func download(_ release: Release) async throws -> URL {
        let work = FileManager.default.temporaryDirectory.appendingPathComponent("decomp-mac-update-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: work, withIntermediateDirectories: true)
        let (file, response) = try await URLSession.shared.download(from: release.zip)
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            throw UpdateError("the download failed (error \(http.statusCode))")
        }
        let zip = work.appendingPathComponent("update.zip")
        try FileManager.default.moveItem(at: file, to: zip)
        try run("/usr/bin/ditto", ["-x", "-k", zip.path, work.appendingPathComponent("unpacked").path])
        let unpacked = work.appendingPathComponent("unpacked")
        guard let name = try FileManager.default.contentsOfDirectory(atPath: unpacked.path).first(where: { $0.hasSuffix(".app") })
        else { throw UpdateError("the download didn't contain the app") }
        let app = unpacked.appendingPathComponent(name)
        guard let bundle = Bundle(url: app), bundle.bundleIdentifier == Bundle.main.bundleIdentifier else {
            throw UpdateError("the download isn't decomp.wtf for Mac")
        }
        let got = bundle.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?"
        guard key(got) == key(release.version) else {
            throw UpdateError("the download is version \(got), not \(release.version)")
        }
        return app
    }

    /// A small script outside the app waits for it to quit, puts the new version in its place (the old one comes
    /// back if that fails) and opens it again.
    private func startSwap(old: URL, new: URL, version: String) throws {
        let script = """
        pid="$1"; old="$2"; new="$3"; shift 3
        echo "$(date): updating $old"
        for i in $(seq 1 600); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
        if kill -0 "$pid" 2>/dev/null; then echo "the app didn't quit; not updating"; exit 1; fi
        backup="$old.previous"
        rm -rf "$backup"
        if ! mv "$old" "$backup"; then echo "couldn't move the old app aside"; open "$old"; exit 1; fi
        if ditto "$new" "$old"; then
          xattr -dr com.apple.quarantine "$old" 2>/dev/null
          rm -rf "$backup"
          echo "installed"
        else
          echo "copying the new app failed; putting the old one back"
          rm -rf "$old"; mv "$backup" "$old"
        fi
        rm -rf "$(dirname "$(dirname "$new")")"
        open -n "$@" "$old"
        """
        let file = FileManager.default.temporaryDirectory.appendingPathComponent("decomp-mac-update.sh")
        try script.write(to: file, atomically: true, encoding: .utf8)

        // Keep the settings this copy was started with (where projects live, a test feed).
        var envArgs: [String] = []
        for name in ["DECOMP_HOME", "DECOMP_MAC_UPDATE_FEED", "DECOMP_PORT"] {
            if let v = ProcessInfo.processInfo.environment[name] { envArgs += ["--env", "\(name)=\(v)"] }
        }
        try? FileManager.default.createDirectory(at: Self.updateLog.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: Self.updateLog.path) {
            FileManager.default.createFile(atPath: Self.updateLog.path, contents: nil)
        }
        let log = try FileHandle(forWritingTo: Self.updateLog)
        log.seekToEndOfFile()

        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = [file.path, String(ProcessInfo.processInfo.processIdentifier), old.path, new.path] + envArgs
        p.standardOutput = log
        p.standardError = log
        p.standardInput = FileHandle.nullDevice
        try p.run()
        defaults.set(version, forKey: Self.pendingKey)
        defaults.set(Self.current, forKey: Self.fromKey)
    }

    /// After an update: say it worked, or that it didn't and this is still the old version.
    private func reportFinishedUpdate() {
        guard let wanted = defaults.string(forKey: Self.pendingKey) else { return }
        let from = defaults.string(forKey: Self.fromKey) ?? "?"
        defaults.removeObject(forKey: Self.pendingKey)
        defaults.removeObject(forKey: Self.fromKey)
        Task {
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            if Self.key(Self.current) == Self.key(wanted) {
                await say("decomp.wtf for Mac is up to date", "It was updated from version \(from) to \(wanted).")
            } else {
                await say("The update didn't finish",
                    "decomp.wtf for Mac is still version \(Self.current); version \(wanted) couldn't be put in "
                    + "place. Choose decomp.wtf ▸ Check for Updates… to try again.\n\nDetails are in "
                    + Self.updateLog.path)
            }
        }
    }

    private func say(_ title: String, _ text: String) async {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = text
        alert.addButton(withTitle: "OK")
        _ = await present(alert)
    }

    /// As a sheet on the window, waited for without blocking: runModal inside a task would hold up everything else
    /// the app does meanwhile (the window would sit on "Starting…" behind the message).
    private func present(_ alert: NSAlert) async -> NSApplication.ModalResponse {
        guard let window = NSApp.windows.first(where: { $0.isVisible && $0.canBecomeMain }) else {
            return alert.runModal()
        }
        return await withCheckedContinuation { done in
            alert.beginSheetModal(for: window) { done.resume(returning: $0) }
        }
    }

    nonisolated private static func run(_ exe: String, _ args: [String]) throws {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = args
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        try p.run()
        p.waitUntilExit()
        if p.terminationStatus != 0 { throw UpdateError("unpacking the download failed") }
    }
}

struct UpdateError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}
