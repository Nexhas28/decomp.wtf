"use strict";
// Install commands for the visitor's system, copy buttons, and the header border once you scroll.
(() => {
  const choose = (os) => {
    document.querySelectorAll(".os button").forEach((b) => {
      b.classList.toggle("on", b.dataset.os === os);
      b.setAttribute("aria-selected", String(b.dataset.os === os));
    });
    document.querySelectorAll(".install [data-os]:not(button)").forEach((el) => { el.hidden = el.dataset.os !== os; });
  };
  document.querySelectorAll(".os button").forEach((b) => b.addEventListener("click", () => choose(b.dataset.os)));
  // A phone says "like Mac OS X" too: it gets the commands for the computer it will install on, not macOS's note
  const ua = navigator.userAgent;
  choose(/Windows/i.test(ua) ? "win" : /iPhone|iPad|iPod|Android/i.test(ua) ? "unix" : /Macintosh|Mac OS X/i.test(ua) ? "mac" : "unix");
  const said = (b, text) => {
    b.textContent = text;
    setTimeout(() => { b.textContent = "Copy"; }, 1800);
  };
  // The clipboard API is missing or refused in some browsers (older ones, some privacy settings, in-app browsers):
  // then the old way, and if that fails too, the command is selected, ready for Ctrl+C.
  const fallback = (b) => {
    const code = b.parentElement.querySelector("code");
    const range = document.createRange();
    range.selectNodeContents(code || b);
    const sel = getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    let ok = false;
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    said(b, ok ? "Copied" : "Press Ctrl+C");
  };
  document.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", () => {
    if (!navigator.clipboard || !window.isSecureContext) return fallback(b);
    navigator.clipboard.writeText(b.dataset.copy).then(() => said(b, "Copied"), () => fallback(b));
  }));
  const nav = document.getElementById("nav");
  const onScroll = () => nav && nav.classList.toggle("scrolled", scrollY > 8);
  addEventListener("scroll", onScroll, { passive: true });
  onScroll();
})();
