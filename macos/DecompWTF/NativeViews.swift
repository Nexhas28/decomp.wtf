import AppKit
import SwiftUI

/// The window once decomp.wtf runs: a native sidebar, and the page beside it. A project's overview is drawn natively;
/// every other screen is the page's own (its sidebar hidden, so there's one sidebar, this one).
struct MainSplitView: View {
    let url: URL
    let api: DecompAPI
    let onQuit: () -> Void
    @EnvironmentObject private var web: WebController
    @EnvironmentObject private var projects: Projects
    @State private var columns = NavigationSplitViewVisibility.all

    var body: some View {
        NavigationSplitView(columnVisibility: $columns) {
            Sidebar()
                .navigationSplitViewColumnWidth(min: 200, ideal: 240, max: 340)
        } detail: {
            ZStack {
                Color(nsColor: .windowBackgroundColor)  // under the page too: it draws no background of its own
                // Always there, under the overview: switching tabs doesn't reload the page.
                WebView(url: url, controller: web, onQuit: onQuit, onRoute: { projects.route = Route(hash: $0) })
                    .opacity(projects.showsNativeOverview ? 0 : 1)
                    .allowsHitTesting(!projects.showsNativeOverview)
                    .accessibilityHidden(projects.showsNativeOverview)
                if projects.showsNativeOverview {
                    OverviewView()
                        .background(Color(nsColor: .windowBackgroundColor))
                }
            }
            .navigationTitle(title)
        }
        .onAppear { projects.connect(api) }
        .onDisappear { projects.connect(nil) }
        .onChange(of: api) { _, new in projects.connect(new) }
    }

    private var title: String {
        if let slug = projects.route.slug {
            return projects.list.first { $0.slug == slug }?.name ?? projects.overview?.name ?? "decomp.wtf"
        }
        return "decomp.wtf"
    }
}

// MARK: sidebar

private enum NavItem: Hashable {
    case home, catalog, agents, tools, settings
    case project(String)
}

private struct Sidebar: View {
    @EnvironmentObject private var web: WebController
    @EnvironmentObject private var projects: Projects
    /// The project the right-click Delete is asking about, and why a delete didn't happen.
    @State private var deleting: ProjectSummary?
    @State private var deleteError: String?

    private var selection: Binding<NavItem?> {
        Binding(
            get: {
                switch projects.route {
                case .home: return .home
                case .catalog: return .catalog
                case .agents: return .agents
                case .tools: return .tools
                case .settings: return .settings
                case .project(let slug, _): return .project(slug)
                case .other: return nil
                }
            },
            set: { item in
                guard let item else { return }
                let route: Route
                switch item {
                case .home: route = .home
                case .catalog: route = .catalog
                case .agents: route = .agents
                case .tools: route = .tools
                case .settings: route = .settings
                case .project(let slug):
                    // Already in this project (on another tab): back to its overview.
                    route = .project(slug, tab: "overview")
                }
                // A page screen shows once the page says it's there (its route), so the overview never gives way
                // to whatever the page showed before; the native overview can show at once.
                if route.isNativeOverview { projects.route = route }
                web.go(route.hash)
            })
    }

    var body: some View {
        List(selection: selection) {
            Label("New Project", systemImage: "plus.circle")
                .tag(NavItem.home)

            Section("Projects") {
                if projects.list.isEmpty {
                    Text(projects.listError.map { "Couldn't load your projects: \($0)" } ?? "None yet: drop in a game.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(projects.list) { p in
                        ProjectRow(project: p)
                            .tag(NavItem.project(p.slug))
                            // Already selected, on another of its tabs: a click goes back to its overview (the
                            // selection itself doesn't change, so the binding isn't told).
                            .simultaneousGesture(TapGesture().onEnded {
                                if case .project(p.slug, let tab) = projects.route, tab != "overview" {
                                    projects.route = .project(p.slug, tab: "overview")
                                    web.go("#/p/\(p.slug)")
                                }
                            })
                            .contextMenu {
                                Button("Delete Project…", role: .destructive) { deleting = p }
                            }
                    }
                }
            }
            .alert(deleting.map { "Delete \u{201C}\($0.name)\u{201D}?" } ?? "",
                   isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }),
                   presenting: deleting) { p in
                Button("Delete", role: .destructive) { delete(p) }
                Button("Cancel", role: .cancel) {}
            } message: { p in
                Text("This removes \(p.name) from decomp.wtf, with all its matched functions, notes and history. "
                     + "It can't be undone.\n\nYour own game file isn't touched, and the project's folder stays on your Mac.")
            }
            .alert("Couldn't delete the project", isPresented: Binding(get: { deleteError != nil },
                                                                       set: { if !$0 { deleteError = nil } })) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(deleteError ?? "")
            }

            Section("Explore") {
                Label("Public Decomps", systemImage: "square.grid.2x2")
                    .tag(NavItem.catalog)
                Button {
                    web.joinTeam()
                } label: {
                    Label("Join a Team", systemImage: "person.2")
                }
                .buttonStyle(.plain)
            }

            Section {
                Label("Agents & Models", systemImage: "cpu")
                    .tag(NavItem.agents)
                Label("Compilers & Tools", systemImage: "wrench.and.screwdriver")
                    .tag(NavItem.tools)
                Label("Settings", systemImage: "gearshape")
                    .tag(NavItem.settings)
            }
        }
        .listStyle(.sidebar)
    }

    /// Delete it in decomp.wtf; if it was open, back to the home page. The page's own project list catches up at once.
    private func delete(_ p: ProjectSummary) {
        let wasOpen = projects.route.slug == p.slug
        Task {
            do {
                try await projects.delete(p.slug)
                if wasOpen { web.go("#/") }
                web.run("if (typeof loadNav === 'function') loadNav();")
            } catch {
                deleteError = error.localizedDescription
            }
        }
    }
}

private struct ProjectRow: View {
    let project: ProjectSummary

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(project.name)
                    .lineLimit(1)
                    .truncationMode(.tail)
                Spacer(minLength: 4)
                if (project.working ?? 0) > 0 {
                    StatusDot(color: .green)
                        .help("\(project.working ?? 0) agent\(project.working == 1 ? "" : "s") working")
                } else if project.autopilot == "running" {
                    StatusDot(color: .orange)
                        .help("autopilot running")
                }
                if let room = project.room {
                    Image(systemName: "person.2.fill")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .help(room == "host" ? "you share this project" : "in a team room")
                }
            }
            if let broken = project.broken {
                Text("can't be opened")
                    .font(.caption)
                    .foregroundStyle(.red)
                    .help(broken)
            } else {
                HStack {
                    Text(Format.platform(project.platform, project.platform_name))
                    Spacer()
                    Text(project.needsSetup ? "set up" : "\(Format.pct(project.percent))%")
                        .monospacedDigit()
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                ProgressView(value: min(project.percent, 100), total: 100)
                    .progressViewStyle(.linear)
                    .controlSize(.mini)
            }
        }
        .padding(.vertical, 3)
    }
}

private struct StatusDot: View {
    let color: Color
    var body: some View { Circle().fill(color).frame(width: 7, height: 7) }
}

// MARK: overview

private struct OverviewView: View {
    @EnvironmentObject private var web: WebController
    @EnvironmentObject private var projects: Projects

    private static let tabs: [(String, String, String)] = [
        ("functions", "Functions", "list.bullet.rectangle"), ("agents", "Agents", "cpu"),
        ("activity", "Activity", "clock"), ("team", "Team", "person.2"),
        ("source", "Source", "chevron.left.forwardslash.chevron.right"), ("settings", "Settings", "gearshape"),
    ]

    var body: some View {
        if let ov = projects.overview, ov.slug == projects.route.slug {
            ScrollView {
                // The tabs bar stays pinned at the top once it scrolls up there (a section header).
                LazyVStack(alignment: .leading, spacing: 20, pinnedViews: [.sectionHeaders]) {
                    header(ov)
                    if let err = projects.overviewError {
                        Label(err, systemImage: "exclamationmark.triangle")
                            .foregroundStyle(.orange)
                    }
                    if ov.needsAttention { attention(ov) }
                    progress(ov)
                    stats(ov)
                    if let ap = AutopilotText(ov) { autopilot(ov, ap) }
                    Section {
                        if !projects.map.isEmpty {
                            FunctionMap(cells: projects.map, working: projects.working, fixing: projects.fixing,
                                        researching: projects.researching, flashes: projects.flashes,
                                        isFork: ov.isFork) { name in
                                web.openFunction(ov.slug, name)
                            }
                        }
                        activity
                    } header: {
                        tabs(ov)
                    }
                }
                .padding(.horizontal, 28)
                .padding(.vertical, 28)
                .frame(maxWidth: 900, alignment: .leading)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        } else if let err = projects.overviewError {
            ContentUnavailableView("Couldn't open this project", systemImage: "exclamationmark.triangle",
                                   description: Text(err))
        } else {
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private var summary: ProjectSummary? { projects.list.first { $0.slug == projects.route.slug } }

    private func header(_ ov: ProjectOverview) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                ForEach(chips(ov), id: \.self) { chip in
                    Text(chip)
                        .font(.caption.weight(.medium))
                        .padding(.horizontal, 7)
                        .padding(.vertical, 2)
                        .background(Capsule().fill(.quaternary))
                }
            }
            HStack(alignment: .firstTextBaseline) {
                Text(ov.name)
                    .font(.largeTitle.weight(.semibold))
                    .textSelection(.enabled)
                Spacer()
                if let n = summary?.working, n > 0 {
                    Label("\(n) agent\(n == 1 ? "" : "s") working", systemImage: "circle.fill")
                        .labelStyle(.titleAndIcon)
                        .foregroundStyle(.green)
                        .font(.callout)
                }
                if !ov.needsSetup {
                    Button {
                        web.startAgents(ov.slug)
                    } label: {
                        Label((summary?.working ?? 0) > 0 ? "Add Agents…" : "Start Agents…", systemImage: "play.fill")
                    }
                    .glassButton(prominent: true)
                    OpusButton(slug: ov.slug)
                }
                Button { projects.openFolder() } label: { Image(systemName: "folder") }
                    .glassButton()
                    .help("Open the project folder")
                    .accessibilityLabel("Open the project folder")
            }
        }
    }

    private func chips(_ ov: ProjectOverview) -> [String] {
        var out = [ov.platform_name ?? Format.platform(ov.platform, nil)]
        if let code = ov.rom?.game_code, !code.isEmpty { out.append(code) }
        if ov.isFork {
            out.append(ov.origin == "scratch" ? "from scratch" : "public decomp")
        } else if let region = ov.rom?.region, !region.isEmpty {
            out.append(region)
        }
        if let comp = ov.isFork ? ov.compiler_name : ov.compiler?.name, !comp.isEmpty { out.append(comp) }
        return out
    }

    private func attention(_ ov: ProjectOverview) -> some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.circle.fill").foregroundStyle(.orange)
            Text(ov.isFork
                 ? "\(Format.plural(ov.uncovered?.functions, "function")) are in code no source file covers yet, so agents can't decompile them."
                 : "This game's compiler (\(ov.compiler?.name ?? "?")) isn't installed yet, so nothing can be checked.")
                .fixedSize(horizontal: false, vertical: true)
            Spacer()
            Button(ov.isFork ? "Give Them Source Files…" : "Set Up the Compiler…") {
                // The page's own notice, above its Functions tab, has the button that does it.
                web.go("#/p/\(ov.slug)/functions")
            }
        }
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 18).fill(.orange.opacity(0.10)))
        .glassCard()
    }

    private func progress(_ ov: ProjectOverview) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("\(Format.pct(ov.percent))%")
                    .font(.system(size: 34, weight: .semibold).monospacedDigit())
                Text("of code")
                    .foregroundStyle(.secondary)
            }
            ProgressView(value: min(ov.percent, 100), total: 100)
                .progressViewStyle(.linear)
                .tint(.accentColor)
                .animation(.timingCurve(0.2, 0.8, 0.2, 1, duration: 0.8), value: ov.percent)
            Text(subline(ov))
                .foregroundStyle(.secondary)
        }
    }

    private func subline(_ ov: ProjectOverview) -> String {
        if ov.needsSetup { return "set up the project to start" }
        var s = "\(Format.count(ov.matched)) / \(Format.count(ov.functions)) functions"
        if ov.isFork, let linked = ov.measures?.complete_code_percent { s += " · \(Format.pct(linked))% linked" }
        return s + " · \(Format.plural(ov.attempts, "compile"))"
    }

    private func stats(_ ov: ProjectOverview) -> some View {
        Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 12) {
            GridRow {
                Stat(label: "Functions matched", value: "\(Format.count(ov.matched)) of \(Format.count(ov.functions))",
                     detail: "\(Format.pct(Format.percent(ov.matched, of: ov.functions)))% of functions")
                Stat(label: "Code matched", value: "\(Format.count(ov.matched_bytes)) of \(Format.count(ov.bytes))",
                     detail: "bytes")
                Stat(label: "Compiles", value: Format.count(ov.attempts),
                     detail: ov.last_activity.map { "last activity \(Format.since($0))" } ?? "nothing yet")
            }
        }
    }

    private func autopilot(_ ov: ProjectOverview, _ ap: AutopilotText) -> some View {
        VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .top) {
                    Image(systemName: ap.running ? "bolt.fill" : "bolt")
                        .foregroundStyle(ap.running ? .orange : .secondary)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(ap.title).font(.headline)
                        Text(ap.sub)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer()
                    Button(ap.button) { projects.toggleAutopilot() }
                        .glassButton()
                        .disabled(projects.busy)
                }
                if let bar = ap.bar {
                    ProgressView(value: bar, total: 1)
                }
        }
        .padding(14)
        .glassCard()
    }

    /// The project's other screens: a glass bar that floats over the content once it's pinned to the top.
    private func tabs(_ ov: ProjectOverview) -> some View {
        GlassGroup {
            HStack(spacing: 8) {
                ForEach(Self.tabs, id: \.0) { tab in
                    Button {
                        web.go("#/p/\(ov.slug)/\(tab.0)")
                    } label: {
                        Label(tab.1, systemImage: tab.2)
                    }
                    .glassButton()
                }
                Spacer(minLength: 0)
            }
        }
        .controlSize(.large)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder private var activity: some View {
        if !projects.events.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text("Recent activity").font(.headline)
                ForEach(projects.events.suffix(12).reversed()) { ev in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(ev.agent ?? "")
                            .foregroundStyle(.secondary)
                            .frame(width: 110, alignment: .leading)
                            .lineLimit(1)
                        Text(ev.kind ?? "")
                            .font(.caption.weight(.medium))
                            .padding(.horizontal, 6)
                            .background(Capsule().fill(ev.kind == "match" ? Color.green.opacity(0.2) : Color.secondary.opacity(0.15)))
                        Text(ev.detail ?? "")
                            .font(.system(.body, design: .monospaced))
                            .lineLimit(1)
                            .truncationMode(.middle)
                        Spacer()
                        Text(Format.since(ev.created_at))
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                    }
                }
            }
        }
    }
}

private struct Stat: View {
    let label: String
    let value: String
    let detail: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.title3.weight(.semibold).monospacedDigit())
            Text(detail).font(.caption).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .glassCard()
    }
}

/// The autopilot box's wording, as the page's renderAutopilot writes it.
private struct AutopilotText {
    let title: String
    let sub: String
    let button: String
    let running: Bool
    let bar: Double?

    init?(_ ov: ProjectOverview) {
        let ap = ov.autopilot, tw = ov.twins, pm = ov.permuter
        let canRun = (!ov.isFork || ov.ready == true) && (ov.functions ?? 0) - (ov.matched ?? 0) > 0
        if ap == nil && !canRun && tw == nil && pm == nil { return nil }
        let running = ap?.state == "running"
        let twinsRunning = tw?.state == "running"
        var perm = ""
        if let pm, (pm.tried ?? 0) > 0 || pm.state == "running" {
            let doing = pm.state == "running"
                ? "is rewriting near misses\(pm.current.map { " (now \($0))" } ?? "")" : "rewrote near misses"
            perm = " The permuter \(doing): \(Format.plural(pm.tried, "function")), \(pm.matched ?? 0) matched."
        }
        let twinsDone = (ap?.twins ?? 0) > 0 ? ap!.twins! : (tw?.matched ?? 0)
        self.running = running
        if running, let ap, ap.phase == "twins" {
            title = "Autopilot: matching code the game has more than once (twins of matched functions)"
            sub = "Each match's code is tried on its identical copies, names swapped; the judge decides.\(perm)"
            button = "Stop"
            bar = nil
        } else if running, let ap {
            title = "Autopilot: trying each function's machine draft · \(Format.count(ap.matched)) matched for free"
            var s = "\(Format.count(ap.tried)) of \(Format.count(ap.total)) tried"
            if let near = ap.near, near > 0 { s += " · \(near) more are 90%+ there" }
            if let t = ap.twins, t > 0 { s += " · \(Format.count(t)) from identical code" }
            sub = s + ". Agents work alongside it.\(perm)"
            button = "Stop"
            bar = Double(ap.tried ?? 0) / Double(max(1, ap.total ?? 0))
        } else if ap != nil || tw != nil {
            let total = ap != nil ? (ap?.matched ?? 0) : twinsDone
            if twinsRunning {
                title = "Matching identical code: \(Format.count(tw?.matched)) so far"
            } else if total > 0 {
                title = "Autopilot matched \(Format.count(total)) function\(total == 1 ? "" : "s") for free"
            } else {
                title = "Autopilot found no free matches this time"
            }
            var parts: [String] = []
            if twinsDone > 0 { parts.append("\(Format.count(twinsDone)) from identical code") }
            if let a = tw?.asm, a > 0 { parts.append("\(Format.count(a)) copies of assembly set aside (not for agents)") }
            if let ap, let tried = ap.tried, tried > 0 {
                parts.append("\(Format.count(tried)) machine drafts tried\((ap.near ?? 0) > 0 ? ", \(ap.near!) more 90%+ there" : "")")
            }
            let again = (ap?.no_drafts ?? "").isEmpty ? "Run it again after agents add types and names: drafts get better." : ""
            sub = (parts.isEmpty ? "" : parts.joined(separator: " · ") + ". ") + again + perm
            button = "Run Again"
            bar = nil
        } else {
            title = "Free matches first"
            sub = (ov.isFork && ov.origin != "scratch"
                ? "Autopilot matches the code this game has more than once from its matched copies (names swapped, judged like any attempt)"
                : "Autopilot matches identical copies of matched code and tries every function's machine draft (m2c) through the judge")
                + "; whatever matches costs no agent time.\(perm)"
            button = "Run Autopilot"
            bar = nil
        }
    }
}


// MARK: Claude Opus on API credits

/// Starts a Claude Code helper running Opus 5.5 (medium effort) billed to the user's Anthropic API key: their API
/// credits, not their Claude plan. Asks for the key the first time.
private struct OpusButton: View {
    let slug: String
    @EnvironmentObject private var backend: Backend
    @EnvironmentObject private var projects: Projects
    @EnvironmentObject private var web: WebController
    @State private var askKey = false
    @State private var pending = false
    @State private var message: String?

    private struct Settings: Decodable { let anthropic_key: String? }
    private struct State: Decodable { let settings: Settings }
    private struct Started: Decodable { let started: [String]? }

    var body: some View {
        Button { launch() } label: {
            Label("Opus 5.5 on Credits", systemImage: "sparkles")
        }
        .glassButton()
        .help("Claude Opus 5.5 at medium effort, billed to your Anthropic API key (API credits)")
        .fixedSize()
        .sheet(isPresented: $askKey) {
            KeySheet { key in
                Task { await save(key) }
            }
        }
        .alert(message ?? "", isPresented: Binding(get: { message != nil }, set: { if !$0 { message = nil } })) {
            Button("OK", role: .cancel) {}
        }
    }

    private func launch() {
        guard let api = backend.api else { return }
        Task {
            let state: State? = try? await api.get("/api/state")
            if (state?.settings.anthropic_key ?? "").isEmpty {
                pending = true
                askKey = true
                return
            }
            await start(api)
        }
    }

    private func start(_ api: DecompAPI) async {
        let body: [String: Any] = ["kind": "claude", "slug": slug, "count": 1,
                                   "options": ["model": "claude-opus-5-5", "effort": "medium", "billing": "api"]]
        do {
            let _: Started = try await api.post("/api/agents/start", body)
            web.go("#/p/\(slug)/agents")
            projects.refreshList()
            message = "Claude Code (Opus 5.5) started on your API credits. Its progress is on the Agents tab."
        } catch {
            message = error.localizedDescription
        }
    }

    private func save(_ key: String) async {
        guard let api = backend.api else { return }
        struct OK: Decodable {}
        do {
            let _: [String: AnyDecodable] = try await api.post("/api/settings", ["anthropic_api_key": key])
            if pending { pending = false; await start(api) }
        } catch {
            message = error.localizedDescription
        }
    }
}

/// Decodes and ignores any JSON value (for answers whose content isn't needed).
private struct AnyDecodable: Decodable {
    init(from decoder: Decoder) throws {}
}

private struct KeySheet: View {
    let save: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var key = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Your Anthropic API key").font(.title2.weight(.semibold))
            Text("Opus 5.5 helpers are billed to this key, so they use your API credits instead of your Claude plan. "
                 + "Get one at platform.claude.com > API keys (it starts with sk-ant-api). It's kept on this Mac, in "
                 + "decomp.wtf's settings.")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                SecureField("Paste your key here (sk-ant-api…)", text: $key)
                    .textFieldStyle(.roundedBorder)
                Button("Paste") {
                    key = (NSPasteboard.general.string(forType: .string) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                }
                .help("Paste the key you copied")
            }
            if !key.isEmpty && !key.trimmingCharacters(in: .whitespaces).hasPrefix("sk-ant-api") {
                Text(key.hasPrefix("sk-ant-")
                     ? "That key is tied to your Claude plan, so it would use the plan's limits, not API credits. Use an API key (sk-ant-api…)."
                     : "That doesn't look like an Anthropic API key: they start with sk-ant-api.")
                    .font(.caption).foregroundStyle(.orange)
            }
            HStack {
                Button("Open platform.claude.com") {
                    NSWorkspace.shared.open(URL(string: "https://platform.claude.com/settings/keys")!)
                }
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                Button("Save") {
                    save(key.trimmingCharacters(in: .whitespacesAndNewlines))
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!key.trimmingCharacters(in: .whitespaces).hasPrefix("sk-ant-api"))
            }
        }
        .padding(24)
        .frame(width: 480)
    }
}
