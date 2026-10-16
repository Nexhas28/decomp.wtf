"""The auto-fixer: agents leave notes when a function needs something no decompiling agent can do: it was cut in the
wrong place ("needs merge", "delay slot belongs to the next one"), it's really hand-written ("BIOS veneer", "no C
emits it"), or its C is right and only the permuter can finish it ("allocator wall, needs permuter"). Left alone,
agent after agent claims it, confirms it, and gives up.

Notes on a project's own copy (in a team room, the host's: members' notes reach it) are read as they're left; when
helpers start, the latest note on every unmatched function is read too. The flagged functions are collected for a
minute, then one helper (Claude Code; Sonnet 5.5 at medium effort unless Settings say otherwise) is started with
the list and the repairs (boundary.py: fix_boundary, set_aside_function, queue_for_permuter), which only its
session may use. It checks each one against the assembly before changing anything. One at a time per project; a
function is looked at again only after a week, or after a new note.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time

log = logging.getLogger("decomp.wtf")

SONNET = "claude-sonnet-5-5"
# the models the app offers for it (any other Claude Code model name works through the settings too)
MODELS = [{"id": SONNET, "name": "Sonnet 5.5", "note": "the default: careful and quick"},
          {"id": "claude-opus-5-5", "name": "Opus 5.5", "note": "the strongest, costs more"},
          {"id": "claude-haiku-5-5", "name": "Haiku 5.5", "note": "cheapest, for simple lists"}]
DEFAULTS = {"enabled": True, "model": SONNET, "effort": "medium"}
DEBOUNCE = 60.0  # seconds a flagged note waits for others, so one session gets them together
BATCH = 12  # functions per fixer session
PERMUTER_MAX = 400  # (permute.MAX_INSNS) bigger functions are left to agents
AGAIN_AFTER = 7 * 24 * 3600  # a function the fixer looked at waits this long before another look (or a new note)

# What a note says needs doing. Specific phrases only: "merged two temps" or "split the multiply" are C work.
FLAGS = {
    "boundary": re.compile(
        r"needs? (an? |the )?(lead[- ]side |boundary |extent )?merge|merge[- ]?(list|blocked|tool)|mis-?split|"
        r"(wrong|bad|false|truncated) (split|extent|boundary)|boundary (wall|fix|fragment|artifact|issue|block|merge)|"
        r"boundary-(blocked|truncated)|\bfragment\b|delay[- ]slot (is|belongs)|belongs to (func_|the (next|previous|prior))|"
        r"extent (repair|fix|artifact|split)|needs (host )?(boundary|extent) (fix|split|repair)|"
        r"(falls|runs) (through )?into (the )?(next|func_)|epilogue[- ]only|tail chunk|no prologue",
        re.I),
    "set_aside": re.compile(
        r"hand[- ]?written|\bBIOS\b|\bveneer\b|trampoline|syscall stub|libgte|not C[- ]expressible|"
        r"(unmatchable|impossible) (in|from|via) (pure )?C\b|no C (construct|compiler) emits|closed door|"
        r"needs (hand[- ]written )?asm\b",
        re.I),
    "permuter": re.compile(r"permuter|allocno|allocator (tie|wall|lever)|regalloc wall|coloring tie|"
                           r"pure (regalloc|register|allocator)", re.I),
}


def flags(text: str) -> set[str]:
    """What a note asks for, of the auto-fixer's kinds of repair."""
    return {k for k, rx in FLAGS.items() if rx.search(text or "")}


def wanted(fn, kinds: set[str]) -> set[str]:
    """The asks that still make sense for a function as it is now."""
    out = set()
    if fn["status"] == "matched":
        return out
    if "boundary" in kinds:
        out.add("boundary")
    if "set_aside" in kinds and fn["status"] == "open":
        out.add("set_aside")
    if "permuter" in kinds and fn["status"] == "open" and fn["best_source"] and fn["best_score"] \
            and (fn["n_insns"] or 0) <= PERMUTER_MAX:
        out.add("permuter")
    return out


FIXER_PROMPT = """\
You are decomp.wtf's auto-fixer for {title} ({platform}). Decompiling agents left notes saying the functions below
need a repair they can't make themselves. Your job is only those repairs: you write no C.

{items}

For each function, in order:
1. get_function on it, and on its neighbours where the note is about a boundary (the functions just before and after
   it: their addresses are in the assembly). Check the note against the assembly yourself; notes can be wrong.
2. Repair it with one of these, only when the assembly shows it:
   - fix_boundary(first, second, at, reason): two neighbouring functions meet in the wrong place. `at` is where
     `second` should start; `second`'s end address joins them into one. Typical cases:
       * `first` ends on jr/j/branch and its delay slot is `second`'s first instruction: at = second's address + 4
       * `first` ends with `second`'s prologue (addiu sp,sp,-N) or first instruction: at = that instruction's address
       * `second` has no prologue, uses saved registers / sp offsets `first` set up, or is only `first`'s epilogue,
         and `first` runs on into it (no return of its own): join them (at = second's end)
     Every byte stays where it is; the tool refuses cuts that would split a jump from its delay slot. The saved best
     attempts are judged again and the result says if one matched.
   - set_aside_function(function, reason): no C compiles to it: a BIOS/kernel call stub, coprocessor code, $ra kept
     in a global, frameless code that reads registers no caller set. Never for ordinary C that is just hard.
   - queue_for_permuter(function, reason): its best attempt has the right instruction count and structure, and only
     registers or instruction order differ (what the notes call an allocator or regalloc wall).
3. Then add_note(function=..., text="auto-fixer: <what you did, or why you changed nothing>"), one line.

Don't claim, compile or release functions. If a repair is refused, read why: fix your call or leave it with a note.
Be brief: work, don't narrate. Stop when the list is done.
"""


def describe(items: list[dict]) -> str:
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. {it['function']} (flagged: {', '.join(sorted(it['kinds']))}). The note: "
                     f"\"{it['note'][:500]}\"")
    return "\n".join(lines)


class Fixer:
    """Collects flagged functions per project and starts one auto-fixer session for them at a time."""

    def __init__(self, net, manager, config=lambda: {}):
        self.net, self.manager = net, manager
        self.config = config  # Settings' "auto_fixer" (enabled, model, effort)
        self.pending: dict[str, dict[str, dict]] = {}  # slug -> function -> item
        self.sessions: dict[str, str] = {}  # slug -> the running fixer session's id
        self._timers: dict[str, threading.Timer] = {}
        self.batches: dict[str, list[dict]] = {}  # slug -> the running session's list
        self.last_error: dict[str, str] = {}  # slug -> why the last start failed
        self._lock = threading.Lock()

    def mips(self, slug: str) -> bool:
        """The repairs are for MIPS ROM projects (N64, PlayStation, PlayStation 2) on this copy's own project."""
        try:
            proj = self.net.project_row(slug)
            cfg = self.net.cfg(proj)
            return (self.net.is_rom(proj) and not self.net.fork_slug(slug) and slug not in getattr(self.net, "links", {})
                    and (cfg.get("arch") or proj["arch"] or "mips").lower() == "mips")
        except Exception:
            return False

    def settings(self) -> dict:
        cfg = self.config() or {}
        return DEFAULTS | {k: v for k, v in cfg.items() if k in DEFAULTS and v not in (None, "")}

    # -- what needs doing

    def observe(self, slug: str, function: str, text: str, agent=None) -> None:
        """A note was left (on this copy's own project). The fixer's own notes start nothing."""
        if agent is not None and getattr(self.net, "is_fixer", lambda a: False)(agent):
            return
        kinds = flags(text)
        if not kinds or not self.settings()["enabled"] or not self.mips(slug):
            return
        try:
            fn = self.net._function(function, slug)
        except Exception:
            return
        if (kinds := wanted(fn, kinds)):
            self._add(slug, fn, kinds, text, fresh=True)

    def why_not(self, slug: str) -> str:
        """Why the auto-fixer can't work on this project, or ""."""
        if slug in getattr(self.net, "links", {}):
            return "In a team room only the host's app runs the auto-fixer: its repairs change the room's copy."
        if not self.mips(slug):
            return "The auto-fixer works on N64, PlayStation and PlayStation 2 projects."
        return ""

    def scan(self, slug: str, manual: bool = False) -> int:
        """The latest note on every unmatched function (helpers are starting, or `manual`: a person asked; then
        it runs even when switched off, and functions looked at more than ten minutes ago count again).
        Returns how many were flagged."""
        if not (manual or self.settings()["enabled"]) or not self.mips(slug):
            return 0
        proj = self.net.project_row(slug)
        fixers = {r["id"] for r in self.net.store.all("SELECT id FROM agents WHERE name LIKE 'fixer-%'")}
        rows = self.net.store.all(
            "SELECT f.*, n.text AS note_text, n.agent_id AS note_agent FROM notes n JOIN functions f ON f.id=n.function_id "
            "WHERE n.project_id=? AND f.status != 'matched' AND n.id=(SELECT MAX(id) FROM notes m "
            "WHERE m.function_id=n.function_id)", proj["id"])
        n = 0
        for r in rows:
            if r["note_agent"] in fixers:
                continue
            if (kinds := wanted(r, flags(r["note_text"]))) and self._add(slug, r, kinds, r["note_text"], fresh=manual):
                n += 1
        return n

    def run_now(self, slug: str) -> dict:
        """A person's "Run it now": flag what the notes ask for and start the session straight away."""
        if (why := self.why_not(slug)):
            raise ValueError(why)
        flagged = self.scan(slug, manual=True)
        with self._lock:
            if (t := self._timers.pop(slug, None)) is not None:
                t.cancel()
            running = slug in self.sessions
        if not running and self.pending.get(slug):
            self._start(slug)
            with self._lock:
                if slug not in self.sessions and self.pending.get(slug):
                    raise ValueError(self.last_error.get(slug) or "the auto-fixer didn't start")
        return self.status(slug) | {"flagged": flagged}

    def _add(self, slug: str, fn, kinds: set[str], note: str, fresh: bool) -> bool:
        meta = json.loads(fn["meta"] or "{}") if fn["meta"] else {}
        seen = meta.get("fixer_at") or 0
        if not fresh and time.time() - seen < AGAIN_AFTER:
            return False
        if fresh and time.time() - seen < 600:  # it was just looked at (its own repair's notes, say)
            return False
        with self._lock:
            self.pending.setdefault(slug, {})[fn["name"]] = {"function": fn["name"], "id": fn["id"],
                                                             "kinds": set(kinds), "note": note.strip()}
            if slug not in self._timers and slug not in self.sessions:
                t = self._timers[slug] = threading.Timer(DEBOUNCE, self._start, args=(slug,))
                t.daemon = True
                t.start()
        return True

    # -- the fixer session

    def _start(self, slug: str) -> None:
        with self._lock:
            self._timers.pop(slug, None)
            if slug in self.sessions:
                return
            todo = list(self.pending.get(slug, {}).values())[:BATCH]
            if not todo:
                return
            for it in todo:
                self.pending[slug].pop(it["function"], None)
        st = self.settings()
        try:
            sid = self.manager.start_fixer(slug, todo, st["model"], st["effort"], on_done=self._done)
        except Exception as e:  # (Claude Code missing, a rescan...) put them back for the next note or start
            log.info("auto-fixer on %s didn't start: %s", slug, e)
            self.last_error[slug] = f"the auto-fixer didn't start: {e}"
            with self._lock:
                for it in todo:
                    self.pending.setdefault(slug, {}).setdefault(it["function"], it)
            return
        with self._lock:
            self.sessions[slug] = sid
            self.batches[slug] = todo

    def _mark(self, items: list[dict]) -> None:
        now = time.time()
        with self.net.store.tx() as db:
            for it in items:
                row = db.execute("SELECT meta FROM functions WHERE id=?", (it["id"],)).fetchone()
                if row is None:  # joined into its neighbour meanwhile
                    continue
                meta = json.loads(row["meta"] or "{}") if row["meta"] else {}
                meta["fixer_at"] = now
                db.execute("UPDATE functions SET meta=? WHERE id=?", (json.dumps(meta), it["id"]))

    def _done(self, slug: str, ok: bool = True) -> None:
        """The session stopped: its functions are marked looked-at only if it got through its list (one that
        failed, a sign-in or the provider's limit, leaves them for the next start)."""
        with self._lock:
            self.sessions.pop(slug, None)
            todo = self.batches.pop(slug, [])
        if ok:
            self._mark(todo)
        with self._lock:
            more = bool(self.pending.get(slug))
            if more and slug not in self._timers:
                t = self._timers[slug] = threading.Timer(5.0, self._start, args=(slug,))
                t.daemon = True
                t.start()

    def status(self, slug: str) -> dict:
        with self._lock:
            return {"running": self.sessions.get(slug, ""), "waiting": sorted(self.pending.get(slug, {})),
                    "working_on": [it["function"] for it in self.batches.get(slug, [])]}
