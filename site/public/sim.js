"use strict";
/* The homepage simulation of decomp.wtf on the bundled demo ROM. Functions, assembly and C come from
   /data/sim.json (built by site/tools/build_sim.py from the real ROM); agents, attempts and timing are simulated. */
(() => {
  const root = document.getElementById("sim");
  if (!root) return;
  const $ = (sel) => root.querySelector(sel);
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const hex = (n) => (n >>> 0).toString(16).toUpperCase().padStart(8, "0");
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const TYPES = [
    { name: "Claude Code", model: "Anthropic" },
    { name: "opencode", model: "DeepSeek" },
    { name: "Codex", model: "OpenAI" },
    { name: "Antigravity CLI", model: "Google" },
    { name: "Ollama", model: "qwen3-coder · local" },
    { name: "Claude Code", model: "Anthropic" },
  ];
  const map = $("[data-map]"), feedEl = $("[data-feed]"), agentsEl = $("[data-agents]");
  const stat = (k) => $(`[data-stat="${k}"]`), ins = (k) => $(`[data-i="${k}"]`);

  let data = null, fns = [], agents = [], compiles = 0, speed = 4, playing = !reduce, visible = true;
  let pinned = null, focus = null, last = performance.now(), doneAt = 0, seed = 7, cells = [];
  const rnd = () => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed / 0x7fffffff; };

  /* ------------------------------------------------------------ highlighting */
  const asmCache = new Map(), cCache = new Map();
  function asmHtml(f) {
    if (!asmCache.has(f.i)) {
      asmCache.set(f.i, f.asm.split("\n").map((line) => {
        if (/^\.L[0-9A-F]+:/.test(line)) return `<span class="l">${esc(line)}</span>`;
        const m = line.match(/^([0-9A-F]{8})\s+(\S+)\s*(.*)$/);
        if (!m) return esc(line);
        return `<span class="a">${m[1]}</span>  <span class="o">${esc(m[2].padEnd(7))}</span> ${esc(m[3]).replace(/(\.L[0-9A-F]{8})/g, '<span class="l">$1</span>')}`;
      }).join("\n"));
    }
    return asmCache.get(f.i);
  }
  const KW = /\b(void|int|char|short|long|float|double|unsigned|signed|struct|union|enum|typedef|static|extern|const|volatile|register|return|if|else|for|while|do|switch|case|default|break|continue|goto|sizeof|s8|u8|s16|u16|s32|u32|f32|uLong|uInt|Bytef|Byte|voidpf|charf|ush|ulg|uch|local|z_streamp)\b/g;
  function cLines(f) {
    if (!cCache.has(f.i)) {
      cCache.set(f.i, f.c.split("\n").map((line) => {
        const e = esc(line);
        if (/^\s*(\/\*|\*|\/\/)/.test(line)) return `<span class="c">${e}</span>`;
        return e.replace(KW, '<span class="k">$1</span>').replace(/\b(0x[0-9A-Fa-f]+|\d+)\b/g, '<span class="n">$1</span>');
      }));
    }
    return cCache.get(f.i);
  }

  /* ------------------------------------------------------------ the work */
  function plan(f) {
    const k = Math.max(1, Math.min(9, Math.round(1 + f.insns / 18 + rnd() * 2.2)));
    return { k, s0: Math.max(40, Math.round(f.insns * 100 * (0.18 + rnd() * 0.35))) };
  }
  function scoreAt(f, a) {
    if (a >= f.plan.k) return 0;
    const t = a / f.plan.k;
    return Math.max(1, Math.round(f.plan.s0 * Math.pow(1 - t, 1.6) * (0.85 + rnd() * 0.3)));
  }
  function pickOpen() {
    const open = fns.filter((f) => f.status === "open").sort((a, b) => a.insns - b.insns);
    if (!open.length) return null;
    return open[Math.floor(rnd() * Math.min(4, open.length))];
  }
  function log(ag, html, cls = "") {
    const li = document.createElement("li");
    if (cls) li.className = cls;
    li.innerHTML = `<i></i><span><b>${esc(ag.label)}</b> ${html}</span>`;
    feedEl.prepend(li);
    while (feedEl.children.length > 9) feedEl.lastChild.remove();
  }
  function step(ag, quiet) {
    let f = ag.fn;
    if (!f) {
      f = pickOpen();
      if (!f) { ag.wait = Infinity; ag.idle = true; if (!quiet) renderAgents(); return; }
      Object.assign(f, { status: "working", agent: ag, plan: plan(f), attempt: 0 });
      ag.fn = f;
      ag.wait = 1.4 + Math.min(4, f.insns / 25) + rnd();
      if (!quiet) log(ag, `claim_function() → ${f.auto}`);
    } else {
      f.attempt++;
      compiles++;
      const s = scoreAt(f, f.attempt);
      f.best = f.best == null ? s : Math.min(f.best, s);
      if (s === 0) {
        Object.assign(f, { status: "matched", agent: null });
        ag.fn = null;
        ag.done++;
        ag.wait = 0.8 + rnd();
        if (!quiet) {
          log(ag, `compile_and_diff(${f.auto}) → score 0`, "ok");
          log(ag, `matched · named it <b>${esc(f.name)}</b>`, "ok");
        }
      } else {
        ag.wait = 2 + rnd() * 2.4 + Math.min(3, f.insns / 60);
        if (!quiet) log(ag, `compile_and_diff(${f.auto}) → score ${s} · ${Math.max(0, 100 - s / f.insns).toFixed(0)}%`);
      }
    }
    if (quiet) return;
    renderCell(f);
    renderStats();
    renderAgents();
    if (!pinned) { focus = f; renderInspect(); } else if (pinned === f) renderInspect();
  }
  function addAgent(quiet) {
    if (agents.length >= TYPES.length) return;
    const t = TYPES[agents.length];
    const n = agents.filter((a) => a.name === t.name).length + 1;
    agents.push({ ...t, label: `${t.name.toLowerCase().replace(/ /g, "-")}-${n}`, fn: null, wait: 0.3 + agents.length * 0.6, done: 0, idle: false });
    if (!quiet) { renderAgents(); renderStats(); }
    $('[data-act="add"]').disabled = agents.length >= TYPES.length;
  }

  /* ------------------------------------------------------------ rendering */
  function buildMap() {
    map.innerHTML = "";
    cells = fns.map((f) => {
      const b = document.createElement("button");
      b.type = "button";
      b.style.setProperty("--k", f.i);
      b.addEventListener("click", () => {
        pinned = pinned === f ? null : f;
        $('[data-act="follow"]').hidden = !pinned;
        renderSel();
        if (pinned) renderInspect(); else if (focus) renderInspect();
      });
      map.append(b);
      return b;
    });
  }
  function renderCell(f) {
    const b = cells[f.i];
    if (!b) return;
    b.className = (f.status === "matched" ? "m" : f.status === "working" ? "w" : f.best != null ? "t" : "") + (pinned === f ? " sel" : "");
    if (f.best != null && f.status !== "matched") b.style.setProperty("--p", Math.max(0, 1 - f.best / (f.insns * 100)).toFixed(2));
    b.title = f.status === "matched" ? `${f.name} · matched` : `${f.auto} · ${f.insns} instructions`;
    b.setAttribute("aria-label", b.title);
  }
  function renderSel() { fns.forEach(renderCell); }
  function renderStats() {
    const total = fns.reduce((s, f) => s + f.insns, 0) || 1;
    const done = fns.filter((f) => f.status === "matched");
    const pct = (100 * done.reduce((s, f) => s + f.insns, 0)) / total;
    stat("pct").textContent = `${pct < 10 ? pct.toFixed(1) : pct.toFixed(0)}%`;
    stat("bar").style.width = `${pct}%`;
    stat("fns").textContent = `${done.length} / ${fns.length}`;
    stat("compiles").textContent = compiles;
    stat("agents").textContent = agents.length;
    if (done.length === fns.length && fns.length && !doneAt) {
      doneAt = performance.now();
      root.classList.add("done");
      log({ label: "decomp.wtf" }, `every function matches. 100%.`, "ok");
    }
  }
  const agentEls = new Map();  // one card per agent, updated in place (no re-animating)
  function renderAgents() {
    for (const [a, el] of agentEls) if (!agents.includes(a)) { el.remove(); agentEls.delete(a); }
    for (const a of agents) {
      let el = agentEls.get(a);
      if (!el) {
        el = document.createElement("div");
        el.className = "sim-agent";
        el.innerHTML = `<header><i></i><b>${esc(a.name)}</b><small>${esc(a.model)}</small></header><div class="fn"><span></span><em></em></div><div class="bar"><i></i></div>`;
        agentsEl.append(el);
        agentEls.set(a, el);
      }
      const f = a.fn;
      el.querySelector("header i").className = a.idle ? "idle" : "";
      el.querySelector(".fn span").textContent = f ? f.auto : a.idle ? "nothing left to do" : "picking a function";
      el.querySelector(".fn em").textContent = f ? (f.attempt ? `try ${f.attempt}` : "reading") : `${a.done} matched`;
      const p = f && f.best != null ? Math.max(4, 100 * (1 - f.best / (f.insns * 100))) : f ? 4 : 0;
      el.querySelector(".bar i").style.width = `${p}%`;
    }
  }
  function renderInspect() {
    const f = pinned || focus;
    if (!f) return;
    ins("name").textContent = f.status === "matched" && f.name !== f.auto ? `${f.auto} → ${f.name}` : f.auto;
    let meta = `0x${hex(f.vram)} · ${f.insns} instructions · `;
    if (f.status === "matched") meta += `<span class="ok">score 0 · byte-identical</span>`;
    else if (f.status === "working") meta += `<span class="wk">${esc(f.agent.name)} · ${f.attempt ? `try ${f.attempt}, best score ${f.best}` : "reading the assembly"}</span>`;
    else meta += f.best != null ? `best score ${f.best}` : "not started";
    ins("meta").innerHTML = meta;
    const asm = ins("asm");
    if (asm.dataset.fn !== String(f.i)) { asm.innerHTML = asmHtml(f); asm.scrollTop = 0; asm.dataset.fn = f.i; }
    const c = ins("c"), lines = cLines(f);
    if (f.status === "matched") {
      ins("clabel").textContent = "The agent's C · matches";
      c.innerHTML = lines.join("\n");
      if (c.dataset.state !== `m${f.i}`) { c.scrollTop = 0; c.dataset.state = `m${f.i}`; }
    } else if (f.status === "working") {
      ins("clabel").textContent = f.attempt ? `The agent's C · try ${f.attempt}` : "The agent's C · drafting";
      const shown = Math.max(1, Math.ceil(lines.length * Math.min(1, (f.attempt + 0.6) / f.plan.k)));
      const edit = f.attempt ? new Set([Math.floor(rnd() * shown), Math.floor(rnd() * shown)]) : new Set();
      c.innerHTML = lines.slice(0, shown).map((l, k) => (edit.has(k) ? `<span class="hl">${l}</span>` : l)).join("\n") + ' <span class="caret"></span>';
      c.dataset.state = `w${f.i}`;
      c.scrollTop = c.scrollHeight;
    } else {
      ins("clabel").textContent = "The agent's C";
      c.innerHTML = '<span class="c">/* nobody has worked on this one yet */</span>';
      c.dataset.state = "";
    }
  }

  /* ------------------------------------------------------------ control */
  function reset() {
    seed = 7; compiles = 0; pinned = null; focus = null; doneAt = 0; agents = [];
    agentEls.forEach((el) => el.remove());
    agentEls.clear();
    root.classList.remove("done");
    fns = data.functions.map((f, i) => ({ ...f, i, status: "open", best: null, attempt: 0, agent: null, plan: null }));
    feedEl.innerHTML = "";
    $('[data-act="follow"]').hidden = true;
    buildMap();
    addAgent(true);
    addAgent(true);
    let t = 0;  // start a little way in, so there is something to see at first glance
    while (t < 26) {
      t += 0.25;
      for (const ag of agents) { ag.wait -= 0.25; if (ag.wait <= 0) step(ag, true); }
    }
    fns.forEach(renderCell);
    renderStats();
    renderAgents();
    focus = agents.map((a) => a.fn).find(Boolean) || fns.find((f) => f.status === "matched") || fns[0];
    renderInspect();
    log({ label: "decomp.wtf" }, `demo ROM split into ${fns.length} functions`);
  }
  function frame(now) {
    const dt = Math.min(0.25, (now - last) / 1000);
    last = now;
    if (data && playing && visible) {
      for (const ag of agents) { ag.wait -= dt * speed; if (ag.wait <= 0) step(ag); }
      if (doneAt && now - doneAt > 9000) reset();
    }
    requestAnimationFrame(frame);
  }

  root.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.speed) {
      speed = Number(b.dataset.speed);
      root.querySelectorAll("[data-speed]").forEach((x) => x.classList.toggle("on", x === b));
    } else if (b.dataset.act === "play") {
      playing = !playing;
      b.textContent = playing ? "Pause" : "Play";
      b.setAttribute("aria-label", b.textContent);
    } else if (b.dataset.act === "add" && data) {
      addAgent();
      log(agents[agents.length - 1], "joined");
    } else if (b.dataset.act === "reset" && data) {
      reset();
    } else if (b.dataset.act === "follow") {
      pinned = null;
      b.hidden = true;
      renderSel();
      renderInspect();
    }
  });
  if (!playing) { const p = $('[data-act="play"]'); p.textContent = "Play"; p.setAttribute("aria-label", "Play"); }
  new IntersectionObserver((es) => { visible = es[0].isIntersecting && !document.hidden; }, { threshold: 0.05 }).observe(root);
  document.addEventListener("visibilitychange", () => {
    const r = root.getBoundingClientRect();
    visible = !document.hidden && r.bottom > 0 && r.top < innerHeight;
  });

  fetch("/data/sim.json").then((r) => r.json()).then((d) => {
    data = d;
    reset();
    requestAnimationFrame(frame);
  }).catch(() => { ins("name").textContent = "The simulation couldn't load."; });
})();
