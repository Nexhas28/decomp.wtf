import Foundation

/// Where the page is, read from its location.hash (#/, #/catalog, #/p/<slug>/<tab>, ...).
enum Route: Hashable {
    case home
    case catalog
    case agents
    case tools
    case settings
    case project(String, tab: String)
    case other(String)

    init(hash: String) {
        let parts = hash.hasPrefix("#/") ? hash.dropFirst(2).split(separator: "/").map(String.init) : []
        switch parts.first {
        case nil: self = .home
        case "catalog": self = .catalog
        case "agents": self = .agents
        case "tools": self = .tools
        case "settings": self = .settings
        case "p" where parts.count >= 2: self = .project(parts[1], tab: parts.count >= 3 ? parts[2] : "overview")
        default: self = .other(hash)
        }
    }

    var hash: String {
        switch self {
        case .home: return "#/"
        case .catalog: return "#/catalog"
        case .agents: return "#/agents"
        case .tools: return "#/tools"
        case .settings: return "#/settings"
        case .project(let slug, let tab): return tab == "overview" ? "#/p/\(slug)" : "#/p/\(slug)/\(tab)"
        case .other(let h): return h
        }
    }

    var slug: String? {
        if case .project(let slug, _) = self { return slug }
        return nil
    }

    /// The project overview is drawn natively; every other screen is the page's.
    var isNativeOverview: Bool {
        if case .project(_, "overview") = self { return true }
        return false
    }
}

/// The native sidebar and overview's data: polled from the running decomp.wtf, the same calls the page makes.
@MainActor
final class Projects: ObservableObject {
    @Published private(set) var list: [ProjectSummary] = []
    @Published private(set) var listError: String?
    @Published private(set) var overview: ProjectOverview?
    @Published private(set) var events: [LiveEvent] = []
    @Published private(set) var overviewError: String?
    /// The function map (every function, for the dot grid) and who's working on which.
    @Published private(set) var map: [MapCell] = []
    @Published private(set) var working: [String: String] = [:]
    /// What the auto-fixer is repairing, and what researchers are writing notes on (by name).
    @Published private(set) var fixing: Set<String> = []
    @Published private(set) var researching: Set<String> = []
    /// Functions that just matched, and when: the map rings them for a moment, like the page.
    @Published private(set) var flashes: [String: Date] = [:]
    private var mapSig: String?
    private var mapFetchedAt = Date.distantPast
    @Published var busy = false
    @Published var route: Route = .home {
        didSet { if route.slug != oldValue.slug { selectionChanged() } }
    }

    /// The native overview covers the page, except for a public decomp still being set up: its setup steps are the page's.
    var showsNativeOverview: Bool {
        guard route.isNativeOverview else { return false }
        if let ov = overview, ov.slug == route.slug, ov.needsSetup { return false }
        return true
    }

    private var api: DecompAPI?
    private var listTask: Task<Void, Never>?
    private var liveTask: Task<Void, Never>?
    private var lastEvent = 0
    private var lastAttempt = 0

    func connect(_ api: DecompAPI?) {
        guard api != self.api else { return }
        self.api = api
        listTask?.cancel()
        liveTask?.cancel()
        list = []
        overview = nil
        guard let api else { return }
        // Every 5 s, like the page's own sidebar.
        listTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.loadList(api)
                try? await Task.sleep(nanoseconds: 5_000_000_000)
            }
        }
        selectionChanged()
    }

    func refreshList() {
        guard let api else { return }
        Task { await loadList(api) }
    }

    private func loadList(_ api: DecompAPI) async {
        do {
            let rows: [ProjectSummary] = try await api.get("/api/projects")
            guard api == self.api else { return }
            if rows != list { list = rows }
            listError = nil
        } catch {
            if list.isEmpty { listError = error.localizedDescription }
        }
    }

    private func selectionChanged() {
        liveTask?.cancel()
        overviewError = nil
        lastEvent = 0
        lastAttempt = 0
        events = []
        map = []
        working = [:]
        fixing = []
        researching = []
        flashes = [:]
        mapSig = nil
        mapFetchedAt = .distantPast
        if overview?.slug != route.slug { overview = nil }
        guard let api, let slug = route.slug else { return }
        // /api/live while a project is open, every 2 s like the page (it answers the overview with what happened
        // since the last call; /api/project would also make this the "last project" each time).
        liveTask = Task { [weak self] in
            while !Task.isCancelled {
                // Only while the native overview shows (or before it has numbers): the page polls for its own tabs.
                if let self, self.overview == nil || self.route.isNativeOverview { await self.loadLive(api, slug) }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    private func loadLive(_ api: DecompAPI, _ slug: String) async {
        do {
            let live: LiveUpdate = try await api.get("/api/live", ["slug": slug, "e": "\(lastEvent)", "a": "\(lastAttempt)"])
            guard api == self.api, slug == route.slug else { return }
            if live.overview != overview { overview = live.overview }
            lastAttempt = max(lastAttempt, live.attempts?.map(\.id).max() ?? 0)
            var mapDirty = map.isEmpty || Date().timeIntervalSince(mapFetchedAt) > 15
            let first = lastEvent == 0
            if let new = live.events, !new.isEmpty {
                if !first {
                    let now = Date()
                    flashes = flashes.filter { now.timeIntervalSince($0.value) < 1.6 }
                    for ev in new where ev.kind == "match" { if let name = ev.detail { flashes[name] = now } }
                }
                lastEvent = max(lastEvent, new.map(\.id).max() ?? 0)
                events = Array((events + new).suffix(40))
                if new.contains(where: { ["match", "split", "rename", "rescan", "boundary", "set_aside"].contains($0.kind ?? "") }) { mapDirty = true }
            }
            var claimed: [String: String] = [:]
            for c in live.claims ?? [] { if let n = c.name { claimed[n] = c.agent ?? "an agent" } }
            if claimed != working { working = claimed }
            let fix = Set(live.fixing ?? []), research = Set(live.researching ?? [])
            if fix != fixing { fixing = fix }
            if research != researching { researching = research }
            if mapDirty { await loadMap(api, slug) }
            overviewError = nil
        } catch {
            guard slug == route.slug else { return }
            overviewError = error.localizedDescription
        }
    }

    /// /api/map, as the page asks for it: with the signature it has, so an unchanged map answers in a few bytes.
    private func loadMap(_ api: DecompAPI, _ slug: String) async {
        var q = ["slug": slug]
        if let mapSig, !map.isEmpty { q["since"] = mapSig }
        guard let data = try? await api.data("/api/map", q), let got = MapCell.decode(data),
              api == self.api, slug == route.slug else { return }
        mapFetchedAt = Date()
        mapSig = got.sig
        if let cells = got.cells, cells != map { map = cells }
    }

    /// Run or stop the autopilot: the page's own button, the same call.
    func toggleAutopilot() {
        guard let api, let ov = overview else { return }
        let running = ov.autopilot?.state == "running"
        busy = true
        Task {
            defer { busy = false }
            do {
                let fresh: ProjectOverview = try await api.post("/api/autopilot",
                                                               ["slug": ov.slug, "action": running ? "stop" : "start"])
                if fresh.slug == route.slug { overview = fresh }
                await loadList(api)
            } catch {
                overviewError = error.localizedDescription
            }
        }
    }

    /// Delete a project from decomp.wtf: its matched work, notes and history. Its folder, and the game, stay on disk.
    func delete(_ slug: String) async throws {
        guard let api else { return }
        struct Done: Decodable { let ok: Bool? }
        let _: Done = try await api.post("/api/delete", ["slug": slug])
        list.removeAll { $0.slug == slug }
        if route.slug == slug { route = .home }
        await loadList(api)
    }

    func openFolder() {
        guard let api, let slug = route.slug else { return }
        Task {
            struct Opened: Decodable { let ok: Bool?; let message: String? }
            if let r: Opened = try? await api.post("/api/open", ["slug": slug]), let msg = r.message, !msg.isEmpty {
                overviewError = msg
            }
        }
    }
}
