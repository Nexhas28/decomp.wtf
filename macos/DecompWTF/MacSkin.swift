import Foundation

/// The page's screens in the Mac's own look, to match the native overview: the system font, the system accent colour
/// for buttons, links, tabs and progress, borderless grey cards, Mac-sized controls. Only how things look changes; the
/// page (decomp.wtf's Python app) stays the same. Added as a constructed stylesheet: the page's CSP blocks a <style>.
enum MacSkin {
    static let css = """
    html[data-shell=mac] {
      --sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif;
      --mono: ui-monospace, "SF Mono", Menlo, monospace;
      --r: 10px;
      --mac-accent: #007aff;
      --mac-accent-ink: #ffffff;
      --mac-fill: rgba(0, 0, 0, .045);
      --mac-fill-strong: rgba(0, 0, 0, .075);
      --mac-control: rgba(0, 0, 0, .055);
      --mac-control-hover: rgba(0, 0, 0, .09);
      --mac-field: #ffffff;
      --mac-field-line: rgba(0, 0, 0, .14);
      --mac-ring: rgba(0, 122, 255, .45);
      --mac-tint: rgba(0, 122, 255, .10);
    }
    @supports (color: AccentColor) { html[data-shell=mac] { --mac-accent: AccentColor; --mac-accent-ink: AccentColorText; } }
    @media (prefers-color-scheme: dark) { html[data-shell=mac]:not([data-theme=light]) {
      --mac-fill: rgba(255, 255, 255, .055); --mac-fill-strong: rgba(255, 255, 255, .09);
      --mac-control: rgba(255, 255, 255, .10); --mac-control-hover: rgba(255, 255, 255, .15);
      --mac-field: rgba(255, 255, 255, .06); --mac-field-line: rgba(255, 255, 255, .14);
      --mac-ring: rgba(10, 132, 255, .55); --mac-tint: rgba(10, 132, 255, .16);
    } }
    html[data-shell=mac][data-theme=dark] {
      --mac-fill: rgba(255, 255, 255, .055); --mac-fill-strong: rgba(255, 255, 255, .09);
      --mac-control: rgba(255, 255, 255, .10); --mac-control-hover: rgba(255, 255, 255, .15);
      --mac-field: rgba(255, 255, 255, .06); --mac-field-line: rgba(255, 255, 255, .14);
      --mac-ring: rgba(10, 132, 255, .55); --mac-tint: rgba(10, 132, 255, .16);
    }

    /* type: the system font at Mac sizes, headings like the native overview */
    html[data-shell=mac] body { font: 400 13.5px/1.5 var(--sans); }
    html[data-shell=mac] .page-head h1, html[data-shell=mac] .hero h1 { font-weight: 600; letter-spacing: -.022em; }
    html[data-shell=mac] .ph-title h1 { font-size: 26px; font-weight: 600; letter-spacing: -.02em; }
    html[data-shell=mac] h2, html[data-shell=mac] h3, html[data-shell=mac] h4 { letter-spacing: -.01em; }
    html[data-shell=mac] .eyebrow, html[data-shell=mac] .kpi .label, html[data-shell=mac] .nav-label,
    html[data-shell=mac] .fn-table th, html[data-shell=mac] .facts dt {
      font: 600 11px/1.2 var(--sans); letter-spacing: .01em; text-transform: none; color: var(--muted);
    }
    html[data-shell=mac] .kpi .num, html[data-shell=mac] .kpi .num.mono {
      font-family: var(--sans); font-weight: 600; font-variant-numeric: tabular-nums; letter-spacing: -.02em;
    }
    html[data-shell=mac] .ph-pct b { font-family: var(--sans); font-variant-numeric: tabular-nums; }
    /* tags: small grey capsules like the native overview's chips */
    html[data-shell=mac] .plat, html[data-shell=mac] .badge, html[data-shell=mac] .chip-s {
      font: 500 11px/1 var(--sans); letter-spacing: 0; text-transform: none; border-radius: 6px;
    }
    html[data-shell=mac] .plat, html[data-shell=mac] .chip-s { border-color: transparent; background: var(--mac-control); color: var(--ink-2); }
    html[data-shell=mac] .chip-s.mono { font-family: var(--mono); }

    /* buttons: blue prominent, grey bordered, Mac corner radius */
    html[data-shell=mac] .primary, html[data-shell=mac] .nav-new, html[data-shell=mac] .chip.on {
      background: var(--mac-accent); color: var(--mac-accent-ink); border-color: transparent;
    }
    html[data-shell=mac] .primary:hover { background: var(--mac-accent); filter: brightness(1.08); }
    html[data-shell=mac] .primary, html[data-shell=mac] .ghost { border-radius: 7px; font-weight: 500; }
    html[data-shell=mac] .ghost { border-color: transparent; background: var(--mac-control); color: var(--ink); }
    html[data-shell=mac] .ghost:hover { background: var(--mac-control-hover); }
    html[data-shell=mac] .ghost.icon.sm { background: transparent; }
    html[data-shell=mac] .ghost.icon.sm:hover { background: var(--mac-control); }
    html[data-shell=mac] .chip { border-color: transparent; background: var(--mac-control); }
    html[data-shell=mac] .chip:hover { background: var(--mac-control-hover); }
    html[data-shell=mac] .chip.on span { color: var(--mac-accent-ink); opacity: .75; }
    html[data-shell=mac] .pill-btn { border-radius: 7px; }

    /* links, focus, selection, checkboxes: the accent colour */
    html[data-shell=mac] .link, html[data-shell=mac] .link-btn { color: var(--mac-accent); text-decoration: none; }
    html[data-shell=mac] .link:hover, html[data-shell=mac] .link-btn:hover { text-decoration: underline; }
    html[data-shell=mac] :focus-visible { outline: 3px solid var(--mac-ring); outline-offset: 1px; }
    html[data-shell=mac] ::selection { background: var(--mac-tint); }
    html[data-shell=mac] input[type=checkbox], html[data-shell=mac] input[type=radio],
    html[data-shell=mac] .check input { accent-color: var(--mac-accent); }

    /* cards and panels: borderless grey, like the native overview's boxes */
    html[data-shell=mac] .card, html[data-shell=mac] .kpi, html[data-shell=mac] .cat-card,
    html[data-shell=mac] .tool-card, html[data-shell=mac] .check {
      border-color: transparent; background: var(--mac-fill);
    }
    html[data-shell=mac] .card:hover, html[data-shell=mac] .cat-card:hover {
      border-color: transparent; background: var(--mac-fill-strong); transform: none;
    }
    html[data-shell=mac] .tool-card.ok { border-color: transparent; }
    html[data-shell=mac] .start-card, html[data-shell=mac] .start-card.accent {
      border-color: transparent; background: var(--mac-fill); transform: none;
    }
    html[data-shell=mac] .start-card:hover { border-color: transparent; background: var(--mac-fill-strong); transform: none; }
    html[data-shell=mac] .sc-icon { background: var(--mac-tint); color: var(--mac-accent); }
    html[data-shell=mac] .sc-icon svg { fill: var(--mac-accent); }
    html[data-shell=mac] .how li::before { font-family: var(--sans); background: var(--mac-tint); color: var(--mac-accent); }
    html[data-shell=mac] .plat-row > span { font: 500 11px/1 var(--sans); letter-spacing: 0; border-color: transparent; background: var(--mac-control); }
    html[data-shell=mac] .plat-row > span.rec { color: var(--ink); background: var(--mac-tint); }
    html[data-shell=mac] .panel { border-color: var(--line-soft); }

    /* progress: blue bars on a grey track, like the native progress views */
    html[data-shell=mac] .meter, html[data-shell=mac] .np-bar { background: var(--mac-fill-strong); }
    html[data-shell=mac] .meter i, html[data-shell=mac] .np-bar i, html[data-shell=mac] .upbar i { background: var(--mac-accent); }

    /* tabs: an accent underline; segmented controls like the Mac's */
    html[data-shell=mac] .ptabs a { font-weight: 500; }
    html[data-shell=mac] .ptabs a.on { border-bottom-color: var(--mac-accent); }
    html[data-shell=mac] .seg { background: var(--mac-control); border-color: transparent; border-radius: 8px; }
    html[data-shell=mac] .seg button { border-radius: 6px; }
    html[data-shell=mac] .seg button.on { background: var(--mac-field); box-shadow: 0 1px 2px rgba(0, 0, 0, .18), 0 0 0 .5px rgba(0, 0, 0, .06); }
    html[data-shell=mac] .fn-table th.sorted::after { color: var(--mac-accent); }

    /* text fields: white (or dark) with a thin border and the accent focus ring */
    html[data-shell=mac] .field select, html[data-shell=mac] .field input, html[data-shell=mac] .field textarea,
    html[data-shell=mac] .lz-custom, html[data-shell=mac] .pad > textarea, html[data-shell=mac] .list-tools .search,
    html[data-shell=mac] input[type=text], html[data-shell=mac] input[type=search], html[data-shell=mac] input[type=url],
    html[data-shell=mac] input[type=password], html[data-shell=mac] input[type=number] {
      background: var(--mac-field); border: 1px solid var(--mac-field-line); border-radius: 7px; font-family: var(--sans);
    }
    html[data-shell=mac] input:focus, html[data-shell=mac] select:focus, html[data-shell=mac] textarea:focus {
      outline: 3px solid var(--mac-ring); outline-offset: 0; border-color: transparent;
    }
    html[data-shell=mac] .list-tools .search, html[data-shell=mac] .field input.mono,
    html[data-shell=mac] .field input[type=password] { font-family: var(--mono); }

    /* the drop zone and popovers */
    html[data-shell=mac] .drop:hover, html[data-shell=mac] .drop.over, html[data-shell=mac] .drop:focus-visible {
      border-color: var(--mac-accent); background: var(--mac-tint);
    }
    html[data-shell=mac] .popover, html[data-shell=mac] .tip, html[data-shell=mac] .toast { border-radius: 10px; }
    """

    /// A user script that adds the skin before the page draws (no flash of the old look).
    static let script: String = {
        let literal = String(decoding: try! JSONSerialization.data(withJSONObject: [css]), as: UTF8.self)
        return """
        (() => {
          document.documentElement.dataset.shell = "mac";
          try {
            const sheet = new CSSStyleSheet();
            sheet.replaceSync(\(literal)[0]);
            document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
          } catch (e) { console.warn("decomp.wtf Mac skin:", e); }
        })();
        """
    }()
}
