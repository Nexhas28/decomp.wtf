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

    /// The page's "Join a team" dialog (its own nav button, hidden in the app), opened from the home screen so it
    /// isn't behind the native overview.
    func joinTeam() {
        run("""
        location.hash = "#/";
        setTimeout(() => document.getElementById("navJoin")?.click(), 200);
        """)
    }

    /// The page's agent launcher (its Start agents button), on the project's Agents tab.
    func startAgents(_ slug: String) {
        run("""
        location.hash = \(Self.literal("#/p/\(slug)/agents"));
        setTimeout(() => document.getElementById("phStart")?.click(), 400);
        """)
    }

    /// The page's own appearance setting (System, Light or Dark); the page tells the app back, and the window follows.
    func setTheme(_ theme: String) {
        Appearance.apply(theme)
        run("if (typeof setTheme === 'function') setTheme(\(Self.literal(theme)));")
    }

    /// One function's page (code, diff, attempts), as clicking its square on the page's map opens it.
    func openFunction(_ slug: String, _ name: String) {
        run("""
        location.hash = \(Self.literal("#/p/\(slug)/functions"));
        setTimeout(() => { if (typeof openFn === "function") openFn(\(Self.literal(name))); }, 400);
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
    var onRoute: (String) -> Void = { _ in }

    func makeCoordinator() -> Coordinator { Coordinator(onQuit: onQuit, onRoute: onRoute) }

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

    /// Tells the app where the page is (its #/ route), so the native sidebar and overview follow it.
    private static let routeHook = """
    (() => {
      const tell = () => window.webkit.messageHandlers.decompRoute.postMessage(location.hash || "#/");
      window.addEventListener("hashchange", tell);
      if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", tell); else tell();
    })();
    """

    /// The app draws its own sidebar: hide the page's (and its narrow-window menu bar), so there's only one.
    /// Through element styles: the page's CSP blocks an added <style>.
    private static let hideNav = """
    (() => {
      const hide = (el) => el && el.style.setProperty("display", "none", "important");
      hide(document.getElementById("nav"));
      hide(document.getElementById("navShade"));
      document.querySelectorAll(".mbar").forEach(hide);
      const app = document.querySelector(".app");
      if (app) app.style.setProperty("grid-template-columns", "minmax(0, 1fr)", "important");
    })();
    """

    /// Tells the app the page's appearance choice ("", "light" or "dark") now and whenever it changes.
    private static let themeHook = """
    (() => {
      const root = document.documentElement;
      const tell = () => window.webkit.messageHandlers.decompTheme.postMessage(root.dataset.theme || "");
      new MutationObserver(tell).observe(root, { attributes: true, attributeFilter: ["data-theme"] });
      tell();
    })();
    """

    /// The page keeps its Light/Dark choice per address, and decomp.wtf's port can change between launches, so the
    /// app remembers it and hands it to the page before the page reads it.
    private static func themeSeed() -> String {
        """
        (() => {
          try {
            const t = \(WebController.literal(Appearance.saved));
            if (t) localStorage.setItem("decomp-wtf-theme", t); else localStorage.removeItem("decomp-wtf-theme");
          } catch (e) {}
        })();
        """
    }

    @MainActor
    static func installScripts(_ ucc: WKUserContentController) {
        ucc.removeAllUserScripts()
        ucc.addUserScript(WKUserScript(source: themeSeed(), injectionTime: .atDocumentStart, forMainFrameOnly: true))
        ucc.addUserScript(WKUserScript(source: MacSkin.script, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        ucc.addUserScript(WKUserScript(source: quitHook, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        ucc.addUserScript(WKUserScript(source: routeHook, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        ucc.addUserScript(WKUserScript(source: hideNav, injectionTime: .atDocumentEnd, forMainFrameOnly: true))
        ucc.addUserScript(WKUserScript(source: themeHook, injectionTime: .atDocumentEnd, forMainFrameOnly: true))
    }

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        Self.installScripts(config.userContentController)
        config.userContentController.add(context.coordinator, name: "decompQuit")
        config.userContentController.add(context.coordinator, name: "decompRoute")
        config.userContentController.add(context.coordinator, name: "decompTheme")
        // A new appearance choice: the next page load starts with it too.
        context.coordinator.themeObserver = NotificationCenter.default.addObserver(
            forName: Appearance.changed, object: nil, queue: .main
        ) { [weak ucc = config.userContentController] _ in
            if let ucc { MainActor.assumeIsolated { Self.installScripts(ucc) } }
        }
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
        let onRoute: (String) -> Void
        var themeObserver: NSObjectProtocol?

        deinit { if let themeObserver { NotificationCenter.default.removeObserver(themeObserver) } }

        init(onQuit: @escaping () -> Void, onRoute: @escaping (String) -> Void) {
            self.onQuit = onQuit
            self.onRoute = onRoute
        }

        func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
            if message.name == "decompQuit" { onQuit() }
            if message.name == "decompRoute", let hash = message.body as? String { onRoute(hash) }
            if message.name == "decompTheme", let theme = message.body as? String {
                MainActor.assumeIsolated { Appearance.apply(theme) }
            }
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
