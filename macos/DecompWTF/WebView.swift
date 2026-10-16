import AppKit
import SwiftUI
import WebKit

/// Holds the page so menu commands can reach it.
@MainActor
final class WebController: ObservableObject {
    weak var webView: WKWebView?

    func reload() { webView?.reload() }
    func goBack() { webView?.goBack() }
    func goForward() { webView?.goForward() }

    func run(_ js: String) {
        webView?.evaluateJavaScript(js, completionHandler: nil)
    }

    func go(_ hash: String) {
        run("location.hash = \(Self.literal(hash));")
    }

    /// File > Open Game…: pick it with the Mac's own file window, then hand its path to the page's "use a file on
    /// this computer" form, so the game is read where it is (no copy through the page) and the user still confirms
    /// it's their own copy there.
    func openGame(at url: URL) {
        run("""
        location.hash = "#/";
        setTimeout(() => {
          const b = document.getElementById("pathPick");
          if (!b) return;
          b.click();
          setTimeout(() => { const f = document.getElementById("ppPath"); if (f) f.value = \(Self.literal(url.path)); }, 50);
        }, 200);
        """)
    }

    func openDemo() {
        run("""
        location.hash = "#/";
        setTimeout(() => document.getElementById("demoBtn")?.click(), 200);
        """)
    }

    static func literal(_ s: String) -> String {
        let data = try! JSONSerialization.data(withJSONObject: [s])
        return String(decoding: data.dropFirst().dropLast(), as: UTF8.self)
    }
}

struct WebView: NSViewRepresentable {
    let url: URL
    let controller: WebController
    var onQuit: () -> Void = {}

    func makeCoordinator() -> Coordinator { Coordinator(onQuit: onQuit) }

    /// Tells the app when the page's own Quit button has stopped decomp.wtf, so the window closes with it.
    private static let quitHook = """
    (() => {
      const real = window.fetch;
      window.fetch = function (input, init) {
        const url = typeof input === "string" ? input : (input && input.url) || "";
        const res = real.apply(this, arguments);
        if (url.includes("/api/quit")) res.finally(() => window.webkit.messageHandlers.decompQuit.postMessage(1));
        return res;
      };
    })();
    """

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.userContentController.addUserScript(
            WKUserScript(source: Self.quitHook, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        config.userContentController.add(context.coordinator, name: "decompQuit")
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        let view = WKWebView(frame: .zero, configuration: config)
        view.uiDelegate = context.coordinator
        view.navigationDelegate = context.coordinator
        view.allowsBackForwardNavigationGestures = true
        view.isInspectable = true
        view.setValue(false, forKey: "drawsBackground")
        controller.webView = view
        context.coordinator.loaded = url
        view.load(URLRequest(url: url))
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {
        controller.webView = view
        if context.coordinator.loaded != url {
            context.coordinator.loaded = url
            view.load(URLRequest(url: url))
        }
    }

    final class Coordinator: NSObject, WKUIDelegate, WKNavigationDelegate, WKDownloadDelegate, WKScriptMessageHandler {
        var loaded: URL?
        let onQuit: () -> Void

        init(onQuit: @escaping () -> Void) { self.onQuit = onQuit }

        func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
            if message.name == "decompQuit" { onQuit() }
        }

        private static func isLocal(_ url: URL?) -> Bool {
            guard let url else { return true }
            if ["about", "blob", "data", "javascript"].contains(url.scheme ?? "") { return true }
            return url.host == "127.0.0.1" || url.host == "localhost"
        }

        // Links to the web (GitHub, docs, sign-in pages) open in the user's browser, not inside the app.
        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void) {
            if action.targetFrame?.isMainFrame != false, !Self.isLocal(action.request.url), let url = action.request.url {
                NSWorkspace.shared.open(url)
                decisionHandler(.cancel)
                return
            }
            decisionHandler(action.shouldPerformDownload ? .download : .allow)
        }

        func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                     decisionHandler: @escaping @MainActor (WKNavigationResponsePolicy) -> Void) {
            decisionHandler(response.canShowMIMEType ? .allow : .download)
        }

        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let url = action.request.url {
                if Self.isLocal(url) { webView.load(action.request) } else { NSWorkspace.shared.open(url) }
            }
            return nil
        }

        // <input type="file">: without this the page's "choose a file" buttons do nothing.
        func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                     initiatedByFrame frame: WKFrameInfo,
                     completionHandler: @escaping @MainActor ([URL]?) -> Void) {
            let panel = NSOpenPanel()
            panel.canChooseFiles = true
            panel.canChooseDirectories = parameters.allowsDirectories
            panel.allowsMultipleSelection = parameters.allowsMultipleSelection
            if let window = webView.window {
                panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.urls : nil) }
            } else {
                completionHandler(panel.runModal() == .OK ? panel.urls : nil)
            }
        }

        // alert / confirm / prompt: without these, confirm() quietly returns false and deleting does nothing.
        func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor () -> Void) {
            let alert = NSAlert()
            alert.messageText = message
            alert.addButton(withTitle: "OK")
            present(alert, in: webView) { _ in completionHandler() }
        }

        func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor (Bool) -> Void) {
            let alert = NSAlert()
            alert.messageText = message
            alert.addButton(withTitle: "OK")
            alert.addButton(withTitle: "Cancel")
            present(alert, in: webView) { completionHandler($0 == .alertFirstButtonReturn) }
        }

        func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor (String?) -> Void) {
            let alert = NSAlert()
            alert.messageText = prompt
            let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
            field.stringValue = defaultText ?? ""
            alert.accessoryView = field
            alert.addButton(withTitle: "OK")
            alert.addButton(withTitle: "Cancel")
            present(alert, in: webView) { completionHandler($0 == .alertFirstButtonReturn ? field.stringValue : nil) }
        }

        private func present(_ alert: NSAlert, in webView: WKWebView, done: @escaping (NSApplication.ModalResponse) -> Void) {
            if let window = webView.window {
                alert.beginSheetModal(for: window, completionHandler: done)
            } else {
                done(alert.runModal())
            }
        }

        // Downloads go to ~/Downloads, like a browser.
        func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
            download.delegate = self
        }

        func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
            download.delegate = self
        }

        func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
                      completionHandler: @escaping @MainActor (URL?) -> Void) {
            let folder = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
            var dest = folder.appendingPathComponent(suggestedFilename)
            let base = dest.deletingPathExtension().lastPathComponent, ext = dest.pathExtension
            var n = 2
            while FileManager.default.fileExists(atPath: dest.path) {
                dest = folder.appendingPathComponent(ext.isEmpty ? "\(base) \(n)" : "\(base) \(n).\(ext)")
                n += 1
            }
            completionHandler(dest)
        }

        func downloadDidFinish(_ download: WKDownload) {
            NSSound(named: "Glass")?.play()
        }
    }
}
