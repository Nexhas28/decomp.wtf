import AppKit
import SwiftUI

@main
struct DecompWTFApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var backend = Backend()
    @StateObject private var web = WebController()
    @StateObject private var updater = Updater()

    var body: some Scene {
        Window("decomp.wtf", id: "main") {
            ContentView()
                .environmentObject(backend)
                .environmentObject(web)
                .frame(minWidth: 900, minHeight: 600)
                .onAppear {
                    delegate.backend = backend
                    if backend.phase == .checking {
                        backend.start()
                        updater.launched()
                    }
                }
        }
        .defaultSize(width: 1280, height: 820)
        .commands { AppCommands(backend: backend, web: web, updater: updater) }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var backend: Backend?

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    /// Quitting stops the decomp.wtf this app started (agents stop with it), so nothing keeps running unseen.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let backend else { return .terminateNow }
        Task { @MainActor in
            await backend.stop()
            sender.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }
}

struct AppCommands: Commands {
    let backend: Backend
    let web: WebController
    let updater: Updater

    var body: some Commands {
        CommandGroup(after: .appInfo) {
            Button("Check for Updates…") { Task { await updater.check(userAsked: true) } }
        }
        CommandGroup(replacing: .newItem) {
            Button("New Project") { web.go("#/") }
                .keyboardShortcut("n")
            Button("Open Game…") { openGame() }
                .keyboardShortcut("o")
            Button("Open the Demo Game") { web.openDemo() }
            Divider()
            Button("Continue a Public Decomp…") { web.go("#/catalog") }
            Button("Join a Team…") { web.run("document.getElementById('navJoin')?.click();") }
        }
        CommandGroup(after: .sidebar) {
            Button("Back") { web.goBack() }
                .keyboardShortcut("[")
            Button("Forward") { web.goForward() }
                .keyboardShortcut("]")
            Button("Reload Page") { web.reload() }
                .keyboardShortcut("r")
            Divider()
        }
        CommandMenu("Tools") {
            Button("Agents & Models") { web.go("#/agents") }
            Button("Compilers & Tools") { web.go("#/tools") }
            Button("Settings") { web.go("#/settings") }
                .keyboardShortcut(",")
            Divider()
            Button("Show Projects Folder in Finder") {
                NSWorkspace.shared.activateFileViewerSelecting([Backend.home])
            }
            Button("Show Log") { NSWorkspace.shared.open(Backend.logFile) }
            Divider()
            Button("Restart decomp.wtf") { backend.restart() }
        }
    }

    private func openGame() {
        let panel = NSOpenPanel()
        panel.title = "Choose your game"
        panel.message = "Pick a ROM, disc image or program file of a game you own."
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        web.openGame(at: url)
    }
}
