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
  choose(/Windows/i.test(navigator.userAgent) ? "win" : /Macintosh|Mac OS X/i.test(navigator.userAgent) ? "mac" : "unix");
  document.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", () => {
    navigator.clipboard.writeText(b.dataset.copy).then(() => {
      b.textContent = "Copied";
      setTimeout(() => { b.textContent = "Copy"; }, 1600);
    });
  }));
  const nav = document.getElementById("nav");
  const onScroll = () => nav && nav.classList.toggle("scrolled", scrollY > 8);
  addEventListener("scroll", onScroll, { passive: true });
  onScroll();
})();
