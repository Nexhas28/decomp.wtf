import SwiftUI

/// One function in /api/map (rows come as arrays under `keys`).
struct MapCell: Equatable {
    let name: String
    let vram: Int?
    let unit: String?
    let insns: Int
    var status: String
    let best: Double?
    let matchedBy: String?

    /// The page's pctOf: how close the best attempt is (1 = matched).
    var closeness: Double? {
        guard let best else { return nil }
        return max(0, 1 - best / Double(max(1, insns * 100)))
    }

    static func decode(_ data: Data) -> (sig: String, cells: [MapCell]?)? {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let sig = obj["sig"] as? String else { return nil }
        if obj["same"] as? Bool == true { return (sig, nil) }
        guard let keys = obj["keys"] as? [String], let rows = obj["rows"] as? [[Any]] else { return nil }
        let at = Dictionary(uniqueKeysWithValues: keys.enumerated().map { ($1, $0) })
        func v(_ row: [Any], _ k: String) -> Any? {
            guard let i = at[k], i < row.count, !(row[i] is NSNull) else { return nil }
            return row[i]
        }
        let cells = rows.map { r in
            MapCell(name: v(r, "name") as? String ?? "", vram: (v(r, "vram") as? NSNumber)?.intValue,
                    unit: v(r, "unit") as? String, insns: (v(r, "insns") as? NSNumber)?.intValue ?? 0,
                    status: v(r, "status") as? String ?? "open", best: (v(r, "best") as? NSNumber)?.doubleValue,
                    matchedBy: v(r, "matched_by") as? String)
        }
        return (sig, cells)
    }
}

/// The page's ROM map: every function a square, in ROM order (or by source file), coloured by how far along it is.
struct FunctionMap: View {
    let cells: [MapCell]
    let working: [String: String]  // function -> agent on it
    var flashes: [String: Date] = [:]  // just matched -> when
    let isFork: Bool
    let onOpen: (String) -> Void
    @Environment(\.colorScheme) private var scheme
    @State private var hover: Int?
    @State private var width: CGFloat = 600

    private struct Geo { let size: CGFloat, gap: CGFloat, cols: Int, height: CGFloat }

    /// The page's layoutMap: squares sized so the map is about 300 px tall, between 5 and 26 px.
    private func geo(_ w: CGFloat) -> Geo {
        let n = max(1, cells.count)
        let size = max(5, min(26, floor(sqrt(w * 300 / CGFloat(n)))))
        let gap: CGFloat = size >= 12 ? 3 : 2
        let cols = max(1, Int((w + gap) / (size + gap)))
        let rows = (n + cols - 1) / cols
        return Geo(size: size, gap: gap, cols: cols, height: CGFloat(rows) * (size + gap) - gap)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(isFork ? "Function map" : "ROM map").font(.headline)
                Spacer()
                legend
            }
            Text(isFork ? "Every square is a function, grouped by source file. Click one to see it."
                        : "Every square is a function, in ROM order. Click one to see it.")
                .font(.caption)
                .foregroundStyle(.secondary)
            let g = geo(width)
            grid(g)
                .frame(height: max(g.height, 1))
                .overlay(alignment: .topLeading) { tooltip(g) }
        }
        .padding(16)
        .glassCard()
    }

    private var legend: some View {
        HStack(spacing: 12) {
            ForEach([("to do", MapPalette.open(scheme)), ("tried", MapPalette.tried(scheme)),
                     ("working", MapPalette.work(scheme)), ("matched", MapPalette.match(scheme))], id: \.0) { item in
                HStack(spacing: 5) {
                    RoundedRectangle(cornerRadius: 2).fill(item.1).frame(width: 9, height: 9)
                    Text(item.0)
                }
            }
        }
        .font(.caption)
        .foregroundStyle(.secondary)
    }

    @ViewBuilder private func grid(_ g: Geo) -> some View {
        let flashing = flashes.values.contains { Date().timeIntervalSince($0) < 1.6 }
        // About 30 frames a second while something moves (a pulse, a ring), like the page; still otherwise.
        TimelineView(.animation(minimumInterval: 1 / 30, paused: working.isEmpty && !flashing)) { tl in
            Canvas { ctx, _ in
                let t = tl.date.timeIntervalSinceReferenceDate * 1000
                let r: CGFloat = g.size >= 10 ? 2.5 : g.size >= 7 ? 1.5 : 1
                for (i, f) in cells.enumerated() {
                    let rect = CGRect(x: CGFloat(i % g.cols) * (g.size + g.gap), y: CGFloat(i / g.cols) * (g.size + g.gap),
                                      width: g.size, height: g.size)
                    var color = cellColor(f)
                    if f.status != "matched", f.status != "asm", working[f.name] != nil {
                        color = MapPalette.work(scheme).opacity(0.55 + 0.45 * sin(t / 260 + Double(i)))
                    }
                    ctx.fill(Path(roundedRect: rect, cornerRadius: r), with: .color(color))
                }
                // A function that just matched: a green ring grows out of its square and fades (1.6 s).
                let index = Dictionary(cells.enumerated().map { ($1.name, $0) }, uniquingKeysWith: { a, _ in a })
                for (name, at) in flashes {
                    let p = tl.date.timeIntervalSince(at) / 1.6
                    guard p >= 0, p < 1, let i = index[name] else { continue }
                    let grow = g.size * (0.6 + 1.8 * p)
                    let rect = CGRect(x: CGFloat(i % g.cols) * (g.size + g.gap) - grow / 2,
                                      y: CGFloat(i / g.cols) * (g.size + g.gap) - grow / 2,
                                      width: g.size + grow, height: g.size + grow)
                    ctx.stroke(Path(roundedRect: rect, cornerRadius: r + 2),
                               with: .color(MapPalette.match(scheme).opacity(1 - p)), lineWidth: 2)
                }
                if let h = hover, h < cells.count {
                    let rect = CGRect(x: CGFloat(h % g.cols) * (g.size + g.gap) - 1.5,
                                      y: CGFloat(h / g.cols) * (g.size + g.gap) - 1.5, width: g.size + 3, height: g.size + 3)
                    ctx.stroke(Path(rect), with: .color(.primary.opacity(0.9)), lineWidth: 1.5)
                }
            }
        }
        .background(GeometryReader { p in
            Color.clear.onAppear { width = p.size.width }.onChange(of: p.size.width) { _, w in width = w }
        })
        .onContinuousHover { phase in
            if case .active(let pt) = phase { hover = index(at: pt, g) } else { hover = nil }
        }
        .onTapGesture { pt in
            if let i = index(at: pt, g) { onOpen(cells[i].name) }
        }
    }

    private func index(at pt: CGPoint, _ g: Geo) -> Int? {
        let step = g.size + g.gap
        let col = Int(pt.x / step), row = Int(pt.y / step)
        guard pt.x >= 0, pt.y >= 0, col < g.cols,
              pt.x.truncatingRemainder(dividingBy: step) <= g.size,
              pt.y.truncatingRemainder(dividingBy: step) <= g.size else { return nil }
        let i = row * g.cols + col
        return i < cells.count ? i : nil
    }

    private func cellColor(_ f: MapCell) -> Color {
        if f.status == "matched" { return MapPalette.match(scheme) }
        if f.status == "asm" { return MapPalette.tried(scheme).opacity(0.35) }
        if let p = f.closeness { return MapPalette.mix(MapPalette.triedRGB(scheme), MapPalette.hiRGB(scheme), pow(p, 3)) }
        return MapPalette.open(scheme)
    }

    @ViewBuilder private func tooltip(_ g: Geo) -> some View {
        if let h = hover, h < cells.count {
            let f = cells[h]
            let status = f.status == "matched" ? "matched\(f.matchedBy.map { " by \($0)" } ?? "")"
                : working[f.name].map { "\($0) is on it" }
                ?? (f.closeness.map { "best \(Self.pctText($0))" } ?? "not started")
            let place = isFork ? (f.unit ?? "") : String(format: "%08X", f.vram ?? 0)
            VStack(alignment: .leading, spacing: 2) {
                Text(f.name).font(.caption.weight(.semibold))
                Text("\(place) · \(f.insns) instructions").font(.caption)
                Text(status).font(.caption)
            }
            .padding(8)
            .background(RoundedRectangle(cornerRadius: 8).fill(.regularMaterial))
            .shadow(radius: 4)
            .fixedSize()
            .offset(x: min(CGFloat(h % g.cols) * (g.size + g.gap) + g.size + 8, max(0, width - 240)),
                    y: CGFloat(h / g.cols) * (g.size + g.gap) + g.size + 8)
            .allowsHitTesting(false)
        }
    }

    /// The page's pctText: never reads 100% before the function really matches.
    static func pctText(_ p: Double) -> String {
        p >= 0.99 ? String(format: "%.1f%%", floor(p * 1000) / 10) : "\(Int(floor(p * 100)))%"
    }
}

/// The page's map colours (app.css --open, --tried, --work, --match, --match-hi), light and dark.
enum MapPalette {
    typealias RGB = (Double, Double, Double)
    private static func c(_ hex: Int) -> RGB {
        (Double((hex >> 16) & 0xff) / 255, Double((hex >> 8) & 0xff) / 255, Double(hex & 0xff) / 255)
    }
    private static func color(_ v: RGB) -> Color { Color(.sRGB, red: v.0, green: v.1, blue: v.2) }
    private static func pick(_ s: ColorScheme, _ light: Int, _ dark: Int) -> RGB { c(s == .dark ? dark : light) }

    static func openRGB(_ s: ColorScheme) -> RGB { pick(s, 0xd9d5c9, 0x272622) }
    static func triedRGB(_ s: ColorScheme) -> RGB { pick(s, 0xc9c1a6, 0x4a4433) }
    static func hiRGB(_ s: ColorScheme) -> RGB { pick(s, 0x8fd3a8, 0x23774a) }
    static func open(_ s: ColorScheme) -> Color { color(openRGB(s)) }
    static func tried(_ s: ColorScheme) -> Color { color(triedRGB(s)) }
    static func work(_ s: ColorScheme) -> Color { color(pick(s, 0xa8670d, 0xe5b04b)) }
    static func match(_ s: ColorScheme) -> Color { color(pick(s, 0x157a49, 0x4fcf8c)) }
    static func panel(_ s: ColorScheme) -> Color { color(pick(s, 0xfbfaf6, 0x131310)) }
    static func line(_ s: ColorScheme) -> Color { color(pick(s, 0xd8d4c8, 0x2a2924)) }

    static func mix(_ a: RGB, _ b: RGB, _ t: Double) -> Color {
        color((a.0 + (b.0 - a.0) * t, a.1 + (b.1 - a.1) * t, a.2 + (b.2 - a.2) * t))
    }
}
