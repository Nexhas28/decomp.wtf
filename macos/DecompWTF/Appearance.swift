import AppKit

/// One light/dark choice for the whole window. The page keeps it (its Settings ▸ Appearance: System, Light or Dark)
/// and tells the app, so the native sidebar, title bar and overview match the page; it's remembered here too, so the
/// window starts in the right colours before the page has loaded.
enum Appearance {
    static let key = "decompTheme"
    static let changed = Notification.Name("decompThemeChanged")

    /// "" (follow the Mac), "light" or "dark".
    static var saved: String { UserDefaults.standard.string(forKey: key) ?? "" }

    @MainActor
    static func apply(_ theme: String) {
        let theme = ["light", "dark"].contains(theme) ? theme : ""
        if theme != saved {
            UserDefaults.standard.set(theme, forKey: key)
            NotificationCenter.default.post(name: changed, object: nil)
        }
        let wanted: NSAppearance? = theme == "dark" ? NSAppearance(named: .darkAqua)
            : theme == "light" ? NSAppearance(named: .aqua) : nil
        if NSApp.appearance != wanted { NSApp.appearance = wanted }
    }
}
