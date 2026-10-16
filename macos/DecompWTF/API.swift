import Foundation

/// The running decomp.wtf's own API, as the page uses it: the decomp_live cookie on every call, X-Decomp: 1 on writes.
/// Everything here reads what the Python app works out; nothing is computed on this side.
struct DecompAPI: Equatable {
    let port: Int
    let secret: String

    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    func get<T: Decodable>(_ path: String, _ query: [String: String] = [:]) async throws -> T {
        try await send(request(path, query), as: T.self)
    }

    /// Raw bytes, for answers that aren't one fixed shape (/api/map's rows).
    func data(_ path: String, _ query: [String: String] = [:]) async throws -> Data {
        let (data, response) = try await URLSession.shared.data(for: request(path, query))
        if let http = response as? HTTPURLResponse, http.statusCode >= 400 {
            throw Failure(message: "decomp.wtf answered \(http.statusCode)")
        }
        return data
    }

    func post<T: Decodable>(_ path: String, _ body: [String: Any]) async throws -> T {
        var req = request(path, [:])
        req.httpMethod = "POST"
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("1", forHTTPHeaderField: "X-Decomp")
        return try await send(req, as: T.self)
    }

    private func request(_ path: String, _ query: [String: String]) -> URLRequest {
        var c = URLComponents()
        c.scheme = "http"
        c.host = "127.0.0.1"
        c.port = port
        c.path = path
        if !query.isEmpty { c.queryItems = query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) } }
        var req = URLRequest(url: c.url!)
        req.timeoutInterval = 15
        req.httpShouldHandleCookies = false
        req.setValue("decomp_live=\(secret)", forHTTPHeaderField: "Cookie")
        return req
    }

    private func send<T: Decodable>(_ req: URLRequest, as: T.Type) async throws -> T {
        let (data, response) = try await URLSession.shared.data(for: req)
        if let http = response as? HTTPURLResponse, http.statusCode >= 400 {
            let why = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["error"] as? String
            throw Failure(message: why ?? "decomp.wtf answered \(http.statusCode)")
        }
        return try JSONDecoder().decode(T.self, from: data)
    }
}

// MARK: what the API answers (every field optional: the server sends null for what doesn't apply)

/// One row of /api/projects: the sidebar.
struct ProjectSummary: Decodable, Identifiable, Equatable {
    let slug: String
    let title: String?
    let kind: String?
    let platform: String?
    let platform_name: String?
    let ready: Bool?
    let functions: Int?
    let matched: Int?
    let bytes: Int?
    let matched_bytes: Int?
    let working: Int?
    let room: String?
    let autopilot: String?
    let broken: String?

    var id: String { slug }
    var name: String { title ?? slug }
    var percent: Double { Format.percent(matched_bytes, of: bytes) }
    var needsSetup: Bool { kind == "fork" && ready != true }
}

struct Autopilot: Decodable, Equatable {
    let state: String?
    let phase: String?
    let total: Int?
    let tried: Int?
    let matched: Int?
    let near: Int?
    let twins: Int?
    let no_drafts: String?
}

struct Twins: Decodable, Equatable {
    let state: String?
    let matched: Int?
    let asm: Int?
}

struct Permuter: Decodable, Equatable {
    let state: String?
    let tried: Int?
    let matched: Int?
    let current: String?
}

/// The rom and compiler blocks differ between kinds of project: anything unexpected reads as empty, not as an error.
struct ROMInfo: Decodable, Equatable {
    var game_code: String?
    var region: String?

    init(from decoder: Decoder) throws {
        let c = try? decoder.container(keyedBy: CodingKeys.self)
        game_code = try? c?.decodeIfPresent(String.self, forKey: .game_code)
        region = try? c?.decodeIfPresent(String.self, forKey: .region)
    }

    private enum CodingKeys: String, CodingKey { case game_code, region }
}

struct CompilerInfo: Decodable, Equatable {
    var name: String?
    var installed: Bool?

    init(from decoder: Decoder) throws {
        let c = try? decoder.container(keyedBy: CodingKeys.self)
        name = try? c?.decodeIfPresent(String.self, forKey: .name)
        installed = try? c?.decodeIfPresent(Bool.self, forKey: .installed)
    }

    private enum CodingKeys: String, CodingKey { case name, installed }
}

/// A ready public decomp's functions no source file covers yet (the page offers to split them into files).
struct Uncovered: Decodable, Equatable {
    var functions: Int?

    init(from decoder: Decoder) throws {
        let c = try? decoder.container(keyedBy: CodingKeys.self)
        functions = try? c?.decodeIfPresent(Int.self, forKey: .functions)
    }

    private enum CodingKeys: String, CodingKey { case functions }
}

struct Measures: Decodable, Equatable {
    let complete_code_percent: Double?
}

/// /api/project (and the `overview` inside /api/live): one project's numbers.
struct ProjectOverview: Decodable, Equatable {
    let slug: String
    let title: String?
    let description: String?
    let kind: String?
    let origin: String?
    let ready: Bool?
    let platform: String?
    let platform_name: String?
    let rom: ROMInfo?
    let compiler: CompilerInfo?
    let compiler_name: String?
    let functions: Int?
    let matched: Int?
    let bytes: Int?
    let matched_bytes: Int?
    let attempts: Int?
    let folder: String?
    let last_activity: Double?
    let autopilot: Autopilot?
    let twins: Twins?
    let permuter: Permuter?
    let measures: Measures?
    let uncovered: Uncovered?

    var name: String { title ?? slug }
    var isFork: Bool { kind == "fork" }
    var needsSetup: Bool { isFork && ready != true }
    var percent: Double { Format.percent(matched_bytes, of: bytes) }

    /// Something the page asks the user to do above every project tab (install the compiler, give code source files).
    var needsAttention: Bool {
        (!isFork && compiler?.installed == false) || (isFork && ready == true && (uncovered?.functions ?? 0) > 0)
    }
}

struct LiveEvent: Decodable, Identifiable, Equatable {
    let id: Int
    let kind: String?
    let detail: String?
    let agent: String?
    let created_at: Double?
}

/// /api/live: the overview plus what happened since the ids passed in (the page polls this while a project is open).
struct LiveUpdate: Decodable {
    let overview: ProjectOverview
    let events: [LiveEvent]?
    let attempts: [LiveAttempt]?
    let claims: [LiveClaim]?
}

struct LiveClaim: Decodable { let name: String?; let agent: String? }

struct LiveAttempt: Decodable { let id: Int }

// MARK: the page's own wording

enum Format {
    static func percent(_ part: Int?, of whole: Int?) -> Double {
        guard let whole, whole > 0 else { return 0 }
        return 100 * Double(part ?? 0) / Double(whole)
    }

    /// The page's pctNum: two decimals under 10%, one above.
    static func pct(_ p: Double) -> String { String(format: p < 10 ? "%.2f" : "%.1f", p) }

    static func count(_ n: Int?) -> String { (n ?? 0).formatted() }

    static func plural(_ n: Int?, _ word: String) -> String { "\(count(n)) \(word)\((n ?? 0) == 1 ? "" : "s")" }

    static func since(_ t: Double?) -> String {
        guard let t, t > 0 else { return "" }
        let s = Date().timeIntervalSince1970 - t
        if s < 60 { return "just now" }
        if s < 3600 { return "\(Int((s / 60).rounded())) min ago" }
        if s < 86400 { return "\(Int((s / 3600).rounded())) h ago" }
        if s < 172800 { return "yesterday" }
        return "\(Int((s / 86400).rounded())) days ago"
    }

    static let platformShort = ["gc": "GameCube", "wii": "Wii", "ps": "PS1", "psx": "PS1", "ps2": "PS2", "n64": "N64",
                                "gba": "GBA", "nds": "DS", "switch": "Switch", "win32": "PC", "xbox360": "Xbox 360",
                                "3ds": "3DS", "xbox": "Xbox", "psp": "PSP"]

    static func platform(_ key: String?, _ name: String?) -> String {
        if let key, let short = platformShort[key] { return short }
        if let name, !name.isEmpty { return name.replacingOccurrences(of: "Nintendo ", with: "") }
        return "?"
    }
}
