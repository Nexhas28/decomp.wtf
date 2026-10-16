import AppKit
import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var backend: Backend
    @EnvironmentObject private var web: WebController
    @State private var dockerDismissed = false

    var body: some View {
        VStack(spacing: 0) {
            if case .running = backend.phase, !dockerDismissed, backend.docker == .missing || backend.docker == .notRunning {
                DockerBar(state: backend.docker, recheck: { Task { await backend.checkDocker() } },
                          dismiss: { dockerDismissed = true })
                Divider()
            }
            content
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(Color(nsColor: .windowBackgroundColor))
    }

    @ViewBuilder private var content: some View {
        switch backend.phase {
        case .checking:
            ProgressView()
        case .starting:
            StartingView()
        case .running(let url):
            if let api = backend.api {
                MainSplitView(url: url, api: api, onQuit: { backend.pageQuit() })
            } else {
                WebView(url: url, controller: web, onQuit: { backend.pageQuit() })
            }
        case .needsUV:
            SetupView(
                icon: "shippingbox",
                title: "One free helper is missing",
                message: "decomp.wtf needs a small free program called uv, which sets up the Python it runs on. "
                    + "Install it once, then come back and press Try Again.",
                steps: ["Press Open the uv Install Page.",
                        "Follow the macOS steps on that page (one line to paste into Terminal).",
                        "Come back here and press Try Again."],
                primary: ("Open the uv Install Page", {
                    NSWorkspace.shared.open(URL(string: "https://docs.astral.sh/uv/getting-started/installation/")!)
                }),
                secondary: ("Try Again", { backend.start() }))
        case .needsCheckout:
            SetupView(
                icon: "folder.badge.questionmark",
                title: "Where is decomp.wtf?",
                message: "This app runs decomp.wtf from its folder on your Mac (the one you downloaded from GitHub, "
                    + "with a file called pyproject.toml in it). Show the app where it is once and it remembers.",
                steps: [],
                primary: ("Choose the decomp.wtf Folder…", { chooseCheckout() }),
                secondary: ("Try Again", { backend.start() }))
        case .failed(let why):
            FailedView(why: why)
        }
    }

    private func chooseCheckout() {
        let panel = NSOpenPanel()
        panel.message = "Choose the decomp.wtf folder (it has pyproject.toml in it)."
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        if Backend.isCheckout(url) {
            backend.useCheckout(url)
        } else {
            let alert = NSAlert()
            alert.messageText = "That isn't the decomp.wtf folder"
            alert.informativeText = "Pick the folder that has pyproject.toml, README.md and a src folder in it."
            alert.runModal()
        }
    }
}

private struct StartingView: View {
    @EnvironmentObject private var backend: Backend

    var body: some View {
        VStack(spacing: 16) {
            AppMark()
            ProgressView()
                .controlSize(.large)
            Text("Starting decomp.wtf…")
                .font(.title2.weight(.semibold))
            Text("The very first start sets things up and can take a minute or two.")
                .foregroundStyle(.secondary)
            if !backend.logTail.isEmpty {
                LogBox(text: backend.logTail)
                    .frame(maxWidth: 640, maxHeight: 160)
            }
        }
        .padding(40)
    }
}

private struct FailedView: View {
    @EnvironmentObject private var backend: Backend
    let why: String

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 44))
                .foregroundStyle(.orange)
            Text("decomp.wtf isn't running")
                .font(.title2.weight(.semibold))
            Text(why)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            if !backend.logTail.isEmpty {
                LogBox(text: backend.logTail)
                    .frame(maxWidth: 720, maxHeight: 260)
            }
            HStack {
                Button("Try Again") { backend.start() }
                    .keyboardShortcut(.defaultAction)
                Button("Show Full Log") { NSWorkspace.shared.open(Backend.logFile) }
            }
        }
        .padding(40)
    }
}

private struct SetupView: View {
    let icon: String
    let title: String
    let message: String
    let steps: [String]
    let primary: (String, () -> Void)
    let secondary: (String, () -> Void)

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Image(systemName: icon)
                .font(.system(size: 40))
                .foregroundStyle(.tint)
            Text(title)
                .font(.title.weight(.semibold))
            Text(message)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if !steps.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(steps.enumerated()), id: \.offset) { i, step in
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Text("\(i + 1)").font(.callout.monospacedDigit().weight(.semibold))
                                .frame(width: 22, height: 22)
                                .background(Circle().fill(.quaternary))
                            Text(step)
                        }
                    }
                }
            }
            HStack {
                Button(primary.0, action: primary.1)
                    .keyboardShortcut(.defaultAction)
                Button(secondary.0, action: secondary.1)
            }
            .controlSize(.large)
        }
        .frame(maxWidth: 520)
        .padding(40)
    }
}

private struct DockerBar: View {
    let state: Backend.DockerState
    let recheck: () -> Void
    let dismiss: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "shippingbox.fill").foregroundStyle(.orange)
            Text(state == .missing
                 ? "Docker Desktop isn't installed. Most games' compilers need it on a Mac (it runs them in a small Linux)."
                 : "Docker Desktop isn't running. Most games' compilers need it on a Mac.")
                .font(.callout)
                .lineLimit(2)
            Spacer()
            if state == .missing {
                Button("Get Docker Desktop") {
                    NSWorkspace.shared.open(URL(string: "https://docs.docker.com/desktop/setup/install/mac-install/")!)
                }
            } else {
                Button("Open Docker") {
                    if let app = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.docker.docker") {
                        NSWorkspace.shared.openApplication(at: app, configuration: .init())
                    }
                }
            }
            Button("Check Again", action: recheck)
            Button(action: dismiss) { Image(systemName: "xmark") }
                .buttonStyle(.borderless)
                .help("Hide")
        }
        .controlSize(.small)
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(.orange.opacity(0.10))
    }
}

private struct LogBox: View {
    let text: String

    var body: some View {
        ScrollView {
            Text(text)
                .font(.system(.caption, design: .monospaced))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(10)
        }
        .defaultScrollAnchor(.bottom)
        .background(RoundedRectangle(cornerRadius: 8).fill(.quaternary.opacity(0.5)))
    }
}

private struct AppMark: View {
    var body: some View {
        Image(nsImage: NSApp.applicationIconImage)
            .resizable()
            .frame(width: 72, height: 72)
    }
}
