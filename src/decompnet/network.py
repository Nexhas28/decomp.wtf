"""The coordinator: who works on what, what counts as a match, and what everyone has learned.

Every public method here is one agent-facing action. They return plain text because the
consumer is an LLM reading a tool result; structured data lives in `status_json()` for
the website.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from . import policy, retrieval, scheduler, scoring, snapshots, telemetry, validity
from .compiler import compile_source
from .elfobj import FunctionObj, Reloc, extract_function
from .project import compiler_config, maspsx_variant_config, variant_compiler_config
from .store import Store

CLAIM_TTL = 30 * 60
# A function an agent just released goes to the back of the queue for this long, for every agent: the next one
# starts on something fresh instead of passing the same function back and forth. Asked for by name, it comes at once.
RELEASE_COOLDOWN = 15 * 60
MACHINE_AGENTS = ("autopilot", "permuter")  # decomp.wtf's own workers: their claims never mean "given up on"
# A function released with a toolchain-wall note is left out of strategy claims for this long (asking by name
# still works): the same signature gives the same result, and agents were burning their first claims on walls
# everyone already swept. A context or compiler-profile change clears the cooldown (see _reset_budgets_for_context).
WALL_COOLDOWN = 6 * 3600
# Wall signatures (PR-31): a release note matching one of these classifies the function as a wall and records
# the reason in function_walls so successors see WHY without re-sweeping it.
WALL_SIGNATURES = (
    ("gte-cop2", r"\b(GTE|COP2)\b|\b(lwc2|swc2|ctc2|mfc2|rtps|rtpt)\b"),
    ("jump-table", r"Illegal operands (lui|addiu)|jtbl_|jump.?table"),
    ("load-delay-nop", r"load.?delay|spurious nop|extra nop"),
    ("bios-veneer", r"\bBIOS\b|veneer|hand.?written assembly|syscall"),
    # These are matched as WORDS, not as substrings: a release note that merely names a tool directory
    # ("... left for azflash-4/gb-merge-fragments ...") used to file the row as a wall for six hours,
    # because `fragment` matched inside the path. The lookbehind requires the word not to be preceded by
    # a word character or a hyphen, so paths and slugs are ignored while ordinary prose still trips it.
    ("boundary-fragment", r"(?<![-\w])(?:boundary|fragments?|continuation)\b|(?<![-\w])(?:false|mis).?split"),
    ("toolchain-floor", r"\bfloor\b|(?<![-\w])toolchain"),
    ("regalloc-tie", r"regalloc|allocno|register allocation|register tie"),
)


def wall_signature(text: str) -> str | None:
    """The wall class a release note names, or None when the note is ordinary handoff text.

    Only prose counts: tool paths and slugs that happen to contain a wall word (e.g.
    `tools/azflash-4/gb-merge-fragments`) are not a wall claim."""
    t = text or ""
    for sig, pattern in WALL_SIGNATURES:
        if re.search(pattern, t, re.I):
            return sig
    return None


def _fragment_like(fn, arch: str = "") -> bool:
    """The boundary-fragment shape perf-test observed: the entry instruction already uses a saved
    register (live-in) with no stack-frame prologue — a function split in two by the disassembler.
    Reads the first instruction in either stored form: plain mnemonic lines (walls/releases) or the
    imported `/* rom vram word */  mnemonic operands` lines; labels and comments are not instructions.

    `arch` selects the lane. On sm83 (Game Boy/Color) the test reads the BYTES instead: a row is a
    fragment when a `jr`/`jp` inside it targets an address outside its own range. That must happen
    before the text scan below, because a row with no stored assembly (205 of them on this project)
    has no first instruction to test and would otherwise be refused without its bytes ever being
    looked at."""
    if (arch or "").lower() == "sm83":
        code, base = fn["target"] or b"", fn["vram"] or 0
        if not code:
            return False
        from .rom import sm83

        at = 0
        while at < len(code):
            op = code[at]
            target = None
            if op == 0x18 or (0x20 <= op <= 0x38 and op % 8 == 0):  # jr / jr cc, relative
                if at + 1 < len(code):
                    off = code[at + 1]
                    off -= 256 if off > 127 else 0
                    target = base + at + 2 + off
            elif op in (0xC2, 0xC3, 0xD2, 0xDA, 0xE2, 0xEA, 0xF2, 0xFA):  # jp / jp cc, absolute
                if at + 2 < len(code):
                    target = code[at + 1] | (code[at + 2] << 8)
            if target is not None and not (base <= target < base + len(code)):
                return True
            at += max(1, sm83.length(code, at))
        return False
    for line in (fn["asm"] or "").splitlines():
        text = line.strip()
        if not text or text.startswith((".", "#", "@")):
            continue
        if text.startswith("/*"):
            if "*/" not in text:
                continue
            text = text.split("*/", 1)[1].strip()
            if not text:
                continue
            break
        if re.match(r"^(glabel|endlabel|jlabel|dlabel)\b", text):
            continue  # labels aren't instructions
        break
    else:
        return False
    if re.search(r"\baddiu\s+sp,\s*sp,\s*-", text) or re.search(r"\baddi\s+sp,\s*sp,\s*-", text):
        return False  # a real prologue: the frame is set up
    return bool(re.search(r"\b(s[0-7]|ra|gp)\b", text))
# Twins first (see app/twins.py): a function whose code the game has more than once (boost: how many more times)
# and isn't too big or given up on, best value per instruction first; its twins (boost -1) last, as they match for
# free once it does.
_TWINS = ("(f.boost > 0 AND f.n_insns <= 400 AND f.abandons < 2) DESC, "
          "CASE WHEN f.boost > 0 THEN (f.boost + 1) * 64 / (f.n_insns + 16) ELSE 0 END DESC, f.boost < 0, ")
STRATEGY_INSNS_CAP = {"closest": 400}  # near-miss search has no business with giants (perf-test run 2)
STRATEGIES = {
    "easiest": _TWINS + "f.abandons > 0, f.n_insns ASC",
    # what nobody gave up on, small first; among functions of about the same size, the files closest to done (a
    # file is linked into the game once all of it matches). Leftovers agents released again and again come last,
    # or a crowd of agents spends its time passing the hardest functions around.
    "finish": _TWINS + "f.abandons ASC, (f.n_insns + 31) / 32 ASC, (SELECT COUNT(*) FROM functions g WHERE "
                       "g.project_id=f.project_id AND g.unit=f.unit AND g.status='open') ASC, f.n_insns ASC",
    # near-misses, the ones given up on least first (else every agent resumes the same stuck one)
    "closest": "f.best_score IS NULL, f.abandons ASC, CAST(f.best_score AS REAL) / (f.n_insns * 100) ASC",
    "hardest": "f.n_insns DESC",
    "random": "RANDOM()",
    # P11.2: the explainable adaptive policy; scored in Python (scheduler.choose) over a bounded window,
    # so its value here is just the admission key — never rendered into SQL.
    "priority": "",
}

# ---------------------------------------------------------------------------- A.3 escalation wiring
# Cold-path helpers for stall_note/escalate only (P8.4). The claim/attempt hot paths never import or
# touch the analysis broker (A-02: easy functions must not start providers); the lazy imports below
# keep it that way.

_ARCH_PROCESSORS = {"mips": ("mips", 32), "ppc": ("powerpc", 32), "x86": ("x86", 32),
                    "x86_64": ("x86", 64), "arm": ("arm", 32), "thumb": ("arm", 32),
                    "aarch64": ("aarch64", 64), "sh": ("sh", 32)}


def _counts_from_rows(rows) -> dict:
    """Structured mismatch counts (P3.4 schema keys) derived from the rendered diff rows — the same
    classification the agent sees ('r' register, 's' stack offset, 'i' operand, 'o' reorder,
    '<' deletion, '>' insertion). For duplicated identical instructions the reorder count may
    over-count by the multiplicity: a policy input, never a score."""
    counts = {k: 0 for k in ("register", "stack_offset", "operand", "insertion", "deletion", "reorder")}
    kinds = {"r": "register", "s": "stack_offset", "i": "operand", "o": "reorder",
             "<": "deletion", ">": "insertion"}
    for mark, _, _ in rows:
        kind = kinds.get(mark)
        if kind is not None:
            counts[kind] += 1
    return counts


def _action_advice(action, name: str, since: int, stale: str = "") -> str:
    """The policy's next action, as one appended agent-facing line."""
    lines = {
        "permutation": (f"\n🧭 Next action: PERMUTATION — the last diff is structurally close with only "
                        f"register/stack differences: keep guided permutation; analysis would not help yet."),
        "investigate_function": (f"\n🧭 Next action: TARGETED INVESTIGATION — control-flow, boundary or "
                                 f"jump-table uncertainty persists after {since} non-improving compiles: call "
                                 f"escalate_function(\"{name}\") for evidence, ranked hypotheses and "
                                 f"budget-capped candidate changes."),
        "investigate_neighbourhood": (f"\n🧭 Next action: NEIGHBOURHOOD INVESTIGATION — conflicting "
                                      f"cross-function evidence around {name}: escalate its callers/callees."),
        "release_cooldown": (f"\n🧭 Next action: RELEASE {name} with structured notes (what you tried, "
                             f"what's left) and take a cooldown — no progress and no new evidence expected."),
        "m2c_draft": (f"\n🧭 Next action: M2C DRAFT — no m2c draft has been tried for {name} yet: generate "
                      f"and compile it before any deeper analysis."),
        "context_repair": ("\n🧭 Next action: CONTEXT REPAIR — repeated compile errors from missing "
                           "declarations: fix the context/prototypes before mutating code."),
        "profile_calibration": ("\n🧭 Next action: PROFILE CALIBRATION — the compiler profile stays "
                                "doubtful: run a bounded calibration before more attempts."),
        "data_literal_check": ("\n🧭 Next action: DATA/LITERAL CHECK — the code aligns but data/literals "
                               "differ: focus on the unit's data tables, not generic code changes."),
        "twin_source": ("\n🧭 Next action: TRY THE TWIN/COMPATIBLE SOURCE first — an exact twin or a "
                        "promising compatible source exists; deeper analysis is not needed."),
    }
    text = lines.get(action.kind, "")
    return (text + stale) if text else ""


def _profile_for(proj, fn) -> dict | None:
    """The admitted raw-region profile for one function's target bytes (P8.2/P7.1): the extractor's
    coordinates are authoritative, so the function's own vram/rom_offset is the mapping origin."""
    entry = _ARCH_PROCESSORS.get((proj["arch"] or "").lower())
    code = fn["target"] if fn["target"] is not None else None
    if entry is None or not code:
        return None
    processor, bits = entry
    arch = (proj["arch"] or "").lower()
    vaddr = fn["vram"] if fn["vram"] is not None else (fn["rom_offset"] or 0)
    return {"kind": "raw_region", "processor": processor, "bits": bits,
            "endian": "big" if proj["big_endian"] else "little",
            "mode": "thumb" if arch == "thumb" else ("arm" if arch == "arm" else ""),
            "memory_map": ({"name": "fn", "file_offset": 0, "size": len(code), "vaddr": vaddr},),
            "metadata": {"function": fn["name"]}}


def _escalation_advice(net, function_id: int, agent_id: int, since: int) -> str:
    """A.3: consult the escalation policy with the last attempt's structured mismatch evidence and
    append a next-action recommendation (permutation vs investigation vs release). Cold path only —
    runs once an agent is actually stalled. When the attempt's snapshot is still current the last
    source is recompiled and re-diffed for a real summary; otherwise the advice is marked historical."""
    from .analysis.escalation import EscalationInputs, EscalationPolicy

    attempt = net.store.one(
        "SELECT id, score, source FROM attempts WHERE function_id=? AND agent_id=? AND compiled=1 "
        "ORDER BY id DESC LIMIT 1", function_id, agent_id)
    if attempt is None or not (attempt["source"] or "").strip():
        return ""
    fn = net.store.one("SELECT * FROM functions WHERE id=?", function_id)
    proj = net.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
    snap_row = net.store.one("SELECT * FROM attempt_snapshots WHERE attempt_id=?", attempt["id"])
    mismatch, score_percent, stale = None, None, ""
    if snap_row is not None:
        snap = {"schema": snapshots.SNAPSHOT_SCHEMA,
                "function_revision": snap_row["function_revision"],
                "context_version": snap_row["context_version"],
                "context_digest": snap_row["context_digest"],
                "target_digest": snap_row["target_digest"],
                "compiler_digest": snap_row["compiler_digest"],
                "scorer_version": snap_row["scorer_version"],
                "policy_version": snap_row["policy_version"]}
        ok, why = snapshots.check_current(net, proj, fn, snap)
        if not ok:
            stale = f" (historical attempt — {why}: revalidate before acting on this advice)"
        else:
            try:
                _, _, _, _, d = net._compile_and_evaluate(fn, proj, attempt["source"], net._context(proj["id"], fn["unit"]))
            except Exception:
                d = None
            if d is not None:
                mismatch = scoring.mismatch_summary(_counts_from_rows(d.rows), bytes_match=d.bytes_match,
                                                    score=d.score)
                score_percent = (1 - d.score / max(1, d.max_score)) * 100
    policy = getattr(net, "_escalation_policy", None) or EscalationPolicy()
    action = policy.decide(EscalationInputs(
        function_key=f"{proj['slug']}::{fn['name']}", snapshot=f"rev-{fn['revision']}",
        mismatch=mismatch, score_percent=score_percent, attempts=since,
        m2c_tried=bool(fn["seed"]), recent_progress=False, new_evidence_expected=False))
    return _action_advice(action, fn["name"], since, stale)


def _render_investigation(investigation, budget) -> str:
    """The P8.5 investigation contract as agent-facing text (ranked hypotheses and minimal candidate
    changes only — never raw provider output)."""
    lines = [f"🧪 Investigation: {investigation.question}",
             f"snapshot {investigation.snapshot} | provider {investigation.provider or 'n/a'} | "
             f"status {investigation.status} | budget {budget.wall_seconds:g}s wall, "
             f"{budget.worker_seconds:g}s worker"]
    if investigation.facts:
        lines.append(f"observed facts: {len(investigation.facts)} (each with an evidence id)")
    hypotheses = investigation.ranked_hypotheses()[:3]
    if hypotheses:
        lines.append("ranked hypotheses:")
        lines += [f"- {h.statement}" for h in hypotheses]
    if investigation.unresolved:
        lines.append("unresolved:")
        lines += [f"- {u}" for u in investigation.unresolved[:3]]
    if investigation.candidate_changes:
        lines.append("minimal candidate changes (apply only through the protected edit path):")
        lines += [f"- {c.kind} {c.target!r}: expected {c.expected_observation}"
                  for c in investigation.candidate_changes]
    lines.append(f"stopping condition: {investigation.stopping_condition}")
    return "\n".join(lines)


class NetworkError(Exception):
    """An error meant to be shown to the agent as-is."""


# The optional retrieval block of a project's config JSON (plan Section 18.9): every flag defaults
# off, so projects without the block behave exactly as before.
RETRIEVAL_KEYS = ("function_id_retrieval", "bsim_retrieval", "external_queries", "route_order",
                  "bsim_max_cold_cost")


def config_from(proj) -> retrieval.RetrievalConfig:
    """`retrieval.config_from(proj)`: parse the optional ``retrieval`` block of a project's config JSON
    into a :class:`decompnet.retrieval.RetrievalConfig`. Unknown keys are rejected loudly — a typo must
    not silently disable (or enable) a route — and every flag defaults off (P10.5)."""
    cfg = json.loads(proj["config"]) if isinstance(proj["config"], str) else (proj["config"] or {})
    block = cfg.get("retrieval", {}) or {}
    if not isinstance(block, dict):
        raise NetworkError("project config `retrieval` must be a table of retrieval flags")
    unknown = sorted(set(block) - set(RETRIEVAL_KEYS))
    if unknown:
        raise NetworkError(f"unknown retrieval config key(s) {', '.join(unknown)}; "
                           f"known keys: {', '.join(RETRIEVAL_KEYS)}")
    try:
        return retrieval.RetrievalConfig.from_dict(block)
    except ValueError as e:
        raise NetworkError(f"bad retrieval config: {e}") from e


@dataclass
class Agent:
    id: int
    name: str
    owner: str | None
    trusted: bool = False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 120 else f"{s // 60}m ago" if s < 7200 else f"{s // 3600}h ago"


class Network:
    def __init__(self, db_path: str | Path, claim_ttl: int = CLAIM_TTL):
        self.store = Store(db_path)
        self.claim_ttl = claim_ttl
        self._pool = ThreadPoolExecutor(max_workers=8)
        self._paused_acceptance: dict[str, tuple[str, float]] = {}  # slug -> (reason, paused_at)
        self._pause_lock = threading.Lock()

    # ------------------------------------------------------------------ agents

    def register_agent(self, name: str, owner: str | None = None, *, trusted: bool = True, origin: str = "cli",
                       terms_accepted: bool = False) -> str:
        """Mint an agent token. Operator-created (CLI) agents are trusted; self-registered (web) agents
        start at reputation level 'new' and earn the rest through verified matches."""
        name = name.strip()
        if not name or len(name) > 40 or not all(c.isalnum() or c in "-_." for c in name):
            raise NetworkError("agent name must be 1-40 chars of [A-Za-z0-9-_.]")
        owner = (owner or "").strip()[:60] or None
        token = "dn_" + secrets.token_urlsafe(24)
        now = time.time()
        with self.store.tx() as db:
            if db.execute("SELECT 1 FROM agents WHERE lower(name)=lower(?)", (name,)).fetchone():
                raise NetworkError(f"agent name {name!r} is taken")
            db.execute(
                "INSERT INTO agents(name,owner,token_hash,created_at,trusted,origin,terms_accepted_at) VALUES(?,?,?,?,?,?,?)",
                (name, owner, _hash_token(token), now, int(trusted), origin, now if terms_accepted else None),
            )
        return token

    def authenticate(self, token: str | None) -> Agent:
        if not token:
            raise NetworkError("missing API token: add `--header \"Authorization: Bearer dn_...\"` to your MCP config")
        row = self.store.one("SELECT id,name,owner,trusted FROM agents WHERE token_hash=?", _hash_token(token))
        if row is None:
            raise NetworkError("invalid API token")
        with self.store.tx() as db:
            db.execute("UPDATE agents SET last_seen=? WHERE id=?", (time.time(), row["id"]))
        return Agent(row["id"], row["name"], row["owner"], bool(row["trusted"]))

    def _require_budget(self, agent: Agent) -> None:
        ok, why = policy.compile_budget(self.store, agent.id)
        if not ok:
            raise NetworkError(why)

    def _require_level(self, agent: Agent, what: str) -> None:
        rep = policy.reputation(self.store, agent.id)
        if not rep["level"].can_edit_context:
            raise NetworkError(f"{what} needs reputation level 'member' (one verified match). You are '{rep['level'].name}': "
                               f"match a function first; add_note is open to everyone.")

    # ---------------------------------------------------------------- helpers

    def _project(self, slug: str | None):
        if slug:
            row = self.store.one("SELECT * FROM projects WHERE slug=?", slug)
            if row is None:
                raise NetworkError(f"no project {slug!r}; call list_projects")
            return row
        rows = self.store.all("SELECT * FROM projects")
        if len(rows) == 1:
            return rows[0]
        raise NetworkError("several projects exist; pass `project` (see list_projects)")

    def _function(self, name: str, project: str | None):
        if project is None and "/" in name and "@" not in name.split("/", 1)[0]:  # project/name (fork names hold units)
            project, name = name.split("/", 1)
        if project is None:
            rows = self.store.all("SELECT * FROM functions WHERE name=?", name)
            if len(rows) > 1:
                raise NetworkError(f"{name!r} exists in several projects; pass `project`")
            row = rows[0] if rows else None
        else:
            row = self.store.one(
                "SELECT f.* FROM functions f JOIN projects p ON p.id=f.project_id WHERE p.slug=? AND f.name=?",
                project, name,
            )
        if row is None:
            raise NetworkError(f"no function {name!r}" + (f" in {project!r}" if project else ""))
        return row

    def _context(self, project_id: int, unit: str | None = ""):
        """Latest header for a translation unit, falling back to the project-wide one. Real codebases have
        one header set per source file; small projects just have the project-wide header ('' unit)."""
        unit = unit or ""
        return self.store.one(
            "SELECT * FROM context_versions WHERE project_id=? AND unit IN (?, '') "
            "ORDER BY unit=? DESC, version DESC LIMIT 1", project_id, unit, unit)

    def _unit_contexts(self, project_id: int) -> set[str]:
        return {r["unit"] for r in self.store.all(
            "SELECT DISTINCT unit FROM context_versions WHERE project_id=? AND unit != ''", project_id)}

    def _effective_unit(self, fn, units: set[str]) -> str:
        return fn["unit"] if fn["unit"] in units else ""

    @staticmethod
    def _hash_only(fn) -> bool:
        return fn["target"] is None

    def _target_obj(self, fn, proj) -> FunctionObj:
        return FunctionObj(
            name=fn["name"], arch=proj["arch"], big_endian=bool(proj["big_endian"]), code=fn["target"],
            relocs=[Reloc.from_json(r) for r in json.loads(fn["relocs"])],
        )

    def _expire_claims(self, db) -> None:
        now = time.time()
        own = {r["id"] for r in db.execute("SELECT id FROM agents WHERE name IN (?, ?)", MACHINE_AGENTS)}
        for row in db.execute("SELECT c.function_id, c.agent_id, f.project_id, f.name FROM claims c "
                              "JOIN functions f ON f.id=c.function_id WHERE c.expires_at < ?", (now,)).fetchall():
            if row["agent_id"] in own:  # decomp.wtf's own tries (cut short by a restart): nobody gave up on it
                continue
            db.execute("UPDATE functions SET abandons=abandons+1 WHERE id=?", (row["function_id"],))
            self._event(db, row["project_id"], row["agent_id"], "expired", row["name"])
        db.execute("DELETE FROM claims WHERE expires_at < ?", (now,))

    def _event(self, db, project_id, agent_id, kind, detail) -> None:
        db.execute("INSERT INTO events(project_id,agent_id,kind,detail,created_at) VALUES(?,?,?,?,?)",
                   (project_id, agent_id, kind, detail, time.time()))

    def _agent_name(self, agent_id) -> str:
        row = self.store.one("SELECT name FROM agents WHERE id=?", agent_id) if agent_id else None
        return row["name"] if row else "?"

    def _compile(self, proj, header: str, source: str):
        """Arch-specific lanes may override this (Game Boy: see app.engine.RomNetwork._compile)."""
        root = Path(proj["root"])
        return compile_source(compiler_config(root, json.loads(proj["config"])), header, source)

    def _evaluate(self, fn, proj, obj: bytes) -> tuple[FunctionObj | None, bool | None, list[str]]:
        """(candidate, verdict, problems) for a compiled object. verdict None means: decide by comparing
        fingerprints. Projects whose targets come from a ROM link the candidate instead (see app.engine)."""
        return extract_function(obj, fn["name"]), None, []

    def _compile_and_evaluate(self, fn, proj, source: str, ctx):
        """Compile one exact translation-unit input and evaluate it against `fn`'s own target (plan P2.1):
        the single compile + evaluate + score step shared by attempt() and by remote-result re-verification.
        Returns (result, candidate, verdict, problems, diff); diff is None for hash-only projects and when
        no candidate came out of the object. The caller is responsible for snapshot/acceptance checks."""
        result = self._compile(proj, ctx["header"], source)
        if not result.ok:
            alt = self._variant_attempt(fn, proj, source, ctx, None)
            if alt is None:
                return result, None, None, [], None
            result = alt[0]
        cand, verdict, problems = self._evaluate(fn, proj, result.obj)
        if cand is None or self._hash_only(fn):
            return result, cand, verdict, problems, None
        d = scoring.diff(self._target_obj(fn, proj), cand)
        if verdict is not None:
            d = scoring.DiffResult(0 if verdict else max(1, d.score), d.max_score, d.rows, verdict)
        if self._worth_variant(verdict, d):
            # near miss: try the attempt-scoped assembler variant and keep it only when it is better
            alt = self._variant_attempt(fn, proj, source, ctx, d)
            if alt is not None:
                return alt
        return result, cand, verdict, problems, d

    #: Only near misses are worth a second compile. The variant is never applied project-wide: a global
    #: assembler swap measurably regresses already-matched functions (the pinned GNU as 2.6 keeps a
    #: blocking load-delay nop some ROM functions really have, while modern binutils drops it), so it is
    #: tried per attempt and kept only when it scores strictly better.
    _VARIANT_MAX_SCORE = 300
    #: A diff with this many extra instructions (ours and not the target's) is the assembler's blocking
    #: load-delay nops: worth the variant compile even when the score is far off (it turned a 5320 into
    #: a reproducible 410 on slus-01363 func_800A51DC, and 110 into 10 on func_800B6344). Measured on
    #: func_800AEA14 the variant is the difference between 230 and the reproducible 130, so the near-miss
    #: limit is 300 and two extra rows are enough to try it - only non-matching attempts pay the second
    #: compile, and only when there is a structural signature to explain.
    _VARIANT_FAR_EXTRAS = 5

    def _worth_variant(self, verdict, d) -> bool:
        """A near miss is always worth one variant compile; far off, only a strong structural signature
        (>= _VARIANT_FAR_EXTRAS extra instructions) justifies it, so walls are not charged twice."""
        if verdict is True or d is None or d.score <= 0:
            return False
        if d.score <= self._VARIANT_MAX_SCORE:
            return True
        extras = sum(1 for marker, _t, _m in d.rows if marker.strip() == ">")
        return extras >= self._VARIANT_FAR_EXTRAS

    def _variant_configs(self, proj):
        """The assembler/assembly variants to try for one attempt, in order (never project-wide).

        Both exist because the pinned pipeline is missing two stages the original PSY-Q build had: the
        PSY-Q `move` expansion with modern binutils, and the maspsx assembly preprocessing that encodes
        gcc's jump tables. Measured: swapping either in project-wide regresses already-matched functions,
        so they are attempt-scoped and kept only when strictly better."""
        try:
            cfg = json.loads(proj["config"])
            root = Path(proj["root"])
        except (ValueError, OSError, TypeError):
            return []
        out = []
        for build in (variant_compiler_config, maspsx_variant_config):
            try:
                v = build(root, cfg)
            except Exception:  # a broken toolchain must never break a normal attempt
                v = None
            if v is not None:
                out.append(v)
        return out

    def _variant_attempt(self, fn, proj, source: str, ctx, base_diff):
        """Compile `source` with each variant; return the best strictly-better (result, cand, verdict,
        problems, diff), or None. `base_diff` None means the base compile itself failed."""
        best = None
        for vcfg in self._variant_configs(proj):
            vres = compile_source(vcfg, ctx["header"], source)
            if not vres.ok or not vres.obj:
                continue
            vcand, vverdict, vproblems = self._evaluate(fn, proj, vres.obj)
            if vcand is None:
                continue
            try:
                vd = scoring.diff(self._target_obj(fn, proj), vcand)
            except Exception:
                continue
            if vverdict is not None:
                vd = scoring.DiffResult(0 if vverdict else max(1, vd.score), vd.max_score, vd.rows, vverdict)
            if best is None or vd.score < best[4].score:
                which = "maspsx" if vcfg.asm_pp_cmd else f"asm pass {vcfg.asm_pass}"
                note = (f"variant ({which}): score "
                        + ("compile failed" if base_diff is None else str(base_diff.score))
                        + f" -> {vd.score}")
                best = (replace(vres, log=(vres.log or "") + "\n" + note), vcand, vverdict, vproblems, vd)
        if best is None:
            return None
        if base_diff is not None and best[4].score >= base_diff.score:
            return None
        return best
    def _stale_text(self, proj, fn, snap: dict, why: str) -> str:
        cur = self._context(proj["id"], fn["unit"])
        versions = f" (v{snap['context_version']} -> v{cur['version']})" if why == "context changed" and cur else ""
        return (f"⚠ the {why} during your compile{versions}: recorded as historical, not counted; re-read and retry")

    def _asm_text(self, fn, proj) -> str:
        return fn["asm"]

    def _extra_brief(self, fn, proj) -> list[str]:
        return []

    # ---------------------------------------------------------------- projects

    def list_projects(self) -> str:
        rows = self.store.all(
            """SELECT p.slug, p.name, p.arch, p.description, p.config, COUNT(f.id) AS total,
                      SUM(f.status='matched') AS matched, SUM(f.size) AS bytes,
                      SUM(CASE WHEN f.status='matched' THEN f.size ELSE 0 END) AS matched_bytes
               FROM projects p LEFT JOIN functions f ON f.project_id=p.id GROUP BY p.id ORDER BY p.slug"""
        )
        if not rows:
            return "No projects yet."
        out = []
        for r in rows:
            pct = 100 * (r["matched_bytes"] or 0) / (r["bytes"] or 1)
            legal = json.loads(r["config"]).get("legal", {})
            out.append(f"- {r['slug']}: {r['name']} [{r['arch']}] — {r['matched'] or 0}/{r['total']} functions "
                       f"matched ({pct:.1f}% of code); legal basis: {legal.get('basis')}, contributions licensed "
                       f"{legal.get('license')}\n  {r['description']}".rstrip())
        return "\n".join(out)

    def get_context(self, project: str | None, function: str | None = None) -> str:
        if function:
            fn = self._function(function, project)
            proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
            ctx = self._context(proj["id"], fn["unit"])
        else:
            proj = self._project(project)
            if self._unit_contexts(proj["id"]):
                raise NetworkError(f"{proj['slug']} has one header per source file: pass `function` to get the "
                                   f"header that function compiles against")
            ctx = self._context(proj["id"])
        return (f"// context.h for {proj['slug']} — version {ctx['version']} "
                f"(prepended to every compile as #include \"context.h\")\n{ctx['header']}")

    # ------------------------------------------------------------------ claims

    def claim(self, agent: Agent, project: str | None = None, function: str | None = None,
              strategy: str = "easiest") -> str:
        fn = self.claim_row(agent, project, function, strategy)
        if fn is None:
            proj = self._project(project)
            return f"Nothing left to claim in {proj['slug']} — every open function is matched or claimed. 🎉"
        return self._brief(fn, agent, header="CLAIMED")

    def claim_row(self, agent: Agent, project: str | None = None, function: str | None = None,
                  strategy: str = "easiest"):
        """Claim (or renew) a function and return its row, or None when nothing is left.

        A-02 invariant: this is the hottest path in the coordinator and it NEVER consults the analysis
        broker — ordering is plain SQL over functions/search_budgets/events, and 'closest' results
        always come from those tables. The brief for a walled function already shows its
        function_walls rows (see _brief), so nothing here needs analysis either."""
        if strategy not in STRATEGIES:
            raise NetworkError(f"strategy must be one of {', '.join(STRATEGIES)}")
        proj = self._project(project) if (project or function is None) else None
        cap = STRATEGY_INSNS_CAP.get(strategy, -1)
        now = time.time()
        with self.store.tx() as db:
            self._expire_claims(db)
            held = db.execute("SELECT f.* FROM claims c JOIN functions f ON f.id=c.function_id WHERE c.agent_id=?",
                              (agent.id,)).fetchone()
            if held is not None:
                if function and held["id"] != self._function(function, proj["slug"] if proj else None)["id"]:
                    raise NetworkError(f"you already hold {held['name']}; match it or release_function it first")
                fn = held
                db.execute("UPDATE claims SET expires_at=? WHERE function_id=?", (now + self.claim_ttl, fn["id"]))
            elif function:
                fn = self._function(function, proj["slug"] if proj else None)
                if fn["status"] == "matched":
                    raise NetworkError(f"{fn['name']} is already matched; use get_function to read its source")
                if fn["status"] == "asm":
                    raise NetworkError(f"{fn['name']} is hand-written assembly: no C compiles to it, so it's left out")
                other = db.execute("SELECT agent_id, expires_at FROM claims WHERE function_id=?", (fn["id"],)).fetchone()
                if other is not None:
                    raise NetworkError(f"{fn['name']} is claimed by {self._agent_name(other['agent_id'])} "
                                       f"for another {int(other['expires_at'] - now) // 60} min")
                db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fn["id"], agent.id, now, now + self.claim_ttl))
                self._event(db, fn["project_id"], agent.id, "claim", fn["name"])
            else:
                if strategy == "priority":  # P11.2: the explainable adaptive policy picks from a bounded window
                    rows = db.execute(
                        """SELECT f.* FROM functions f WHERE f.project_id=? AND f.status='open'
                           AND f.id NOT IN (SELECT function_id FROM claims)
                           AND f.id NOT IN (SELECT function_id FROM search_budgets WHERE cooldown_until > ?)
                           AND (? = -1 OR f.n_insns <= ?)
                           AND f.name NOT IN (SELECT detail FROM events WHERE project_id=? AND agent_id=? AND kind='release')
                           ORDER BY f.name IN (SELECT detail FROM events WHERE project_id=? AND kind='release'
                                               AND created_at > ?), f.id LIMIT 200""",
                        (proj["id"], now, cap, cap, proj["id"], agent.id, proj["id"], now - RELEASE_COOLDOWN),
                    ).fetchall()
                    if not rows:
                        return None
                    fn = scheduler.choose(self, proj, list(rows), 1)[0]
                else:
                    fn = db.execute(
                        f"""SELECT f.* FROM functions f WHERE f.project_id=? AND f.status='open'
                            AND f.id NOT IN (SELECT function_id FROM claims)
                            AND f.id NOT IN (SELECT function_id FROM search_budgets WHERE cooldown_until > ?)
                            AND (? = -1 OR f.n_insns <= ?)
                            AND f.name NOT IN (SELECT detail FROM events WHERE project_id=? AND agent_id=? AND kind='release')
                            ORDER BY f.name IN (SELECT detail FROM events WHERE project_id=? AND kind='release'
                                                AND created_at > ?), {STRATEGIES[strategy]} LIMIT 1""",
                        (proj["id"], now, cap, cap, proj["id"], agent.id, proj["id"], now - RELEASE_COOLDOWN),
                    ).fetchone()
                    if fn is None:
                        return None
                db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fn["id"], agent.id, now, now + self.claim_ttl))
                self._event(db, fn["project_id"], agent.id, "claim", fn["name"])
        return fn

    def release(self, agent: Agent, function: str, notes: str, project: str | None = None) -> str:
        fn = self._function(function, project)
        with self.store.tx() as db:
            row = db.execute("SELECT agent_id FROM claims WHERE function_id=?", (fn["id"],)).fetchone()
            if row is None or row["agent_id"] != agent.id:
                raise NetworkError(f"you don't hold a claim on {fn['name']}")
            db.execute("DELETE FROM claims WHERE function_id=?", (fn["id"],))
            db.execute("UPDATE functions SET abandons=abandons+1 WHERE id=?", (fn["id"],))
            note = notes.strip()
            if note:
                db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(?,?,?,?,?)",
                           (fn["project_id"], fn["id"], agent.id, note, time.time()))
                sig = wall_signature(note)
                if sig:  # PR-31: a wall release is recorded and scheduled out until something material changes
                    db.execute("INSERT OR REPLACE INTO function_walls(function_id, signature, reason, at) "
                               "VALUES(?,?,?,?)", (fn["id"], sig, note[:400], time.time()))
                    db.execute(
                        "INSERT INTO search_budgets(function_id, epoch, policy_version, turns_used, max_turns, "
                        "cooldown_until, reset_reason, last_at) VALUES(?,'wall',1,4,4,?,'wall:'||?,?) "
                        "ON CONFLICT(function_id) DO UPDATE SET cooldown_until=excluded.cooldown_until, "
                        "reset_reason='wall:'||excluded.reset_reason, epoch='wall', turns_used=4, last_at=excluded.last_at",
                        (fn["id"], time.time() + WALL_COOLDOWN, sig, time.time()))
            self._event(db, fn["project_id"], agent.id, "release", fn["name"])
        out = (f"Released {fn['name']}. Your best attempt and notes are saved so the next agent starts "
               f"where you left off. Call claim_function for a new one.")
        if wall_signature(note or ""):
            out += ("\nWall recorded (%s): strategy claims skip %s for %dh or until its context/compiler "
                    "changes." % (wall_signature(note), fn["name"], WALL_COOLDOWN // 3600))
        return out

    def get_function(self, function: str, project: str | None = None, agent: Agent | None = None) -> str:
        return self._brief(self._function(function, project), agent, header="VIEW")

    def _brief(self, fn, agent: Agent | None, header: str) -> str:
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        ctx = self._context(proj["id"], fn["unit"])
        claim = self.store.one("SELECT * FROM claims WHERE function_id=?", fn["id"])
        lines = [f"## {header}: {proj['slug']}/{fn['name']}",
                 f"size {fn['size']} bytes, {fn['n_insns']} instructions, arch {proj['arch']}, "
                 f"context.h v{ctx['version']} ({len(ctx['header'].splitlines())} lines)"]
        if claim:
            who = "you" if agent and claim["agent_id"] == agent.id else self._agent_name(claim["agent_id"])
            lines.append(f"claimed by {who}, lease expires in {int(claim['expires_at'] - time.time()) // 60} min "
                         f"(each compile_and_diff renews it)")
        if fn["status"] == "matched":
            lines += [f"MATCHED by {self._agent_name(fn['matched_agent_id'])} {_ago(fn['matched_at'])}:",
                      "```c", fn["matched_source"].strip(), "```"]
        if self._hash_only(fn):
            lines += ["", "### Target assembly", "This is a hash-only project: the coordinator keeps a one-way fingerprint of "
                      "each function, never its code. Disassemble your own lawful copy with "
                      f"`decompnet local asm <your-object.o> {fn['name']}` and diff locally with `decompnet local diff`."]
        else:
            lines += ["", "### Target assembly", "```asm", scoring.compact_asm(self._asm_text(fn, proj)), "```"]
        lines += self._extra_brief(fn, proj)
        if fn["status"] != "matched":
            if fn["best_source"]:
                lines += ["", f"### Best attempt so far: score {fn['best_score']} by "
                              f"{self._agent_name(fn['best_agent_id'])} ({fn['attempts']} attempts total) — "
                              f"start from this"
                          + ("\n⚠ LEGACY BEST (unverified): this score predates snapshot verification "
                             "and may not reproduce under the current compiler/context — revalidate "
                             "before trusting it" if "best_verified" in fn.keys() and not fn["best_verified"]
                             else ""), "```c", fn["best_source"].strip(), "```"]
            elif fn["seed"]:
                lines += ["", "### Machine-generated starting point (m2c: expect wrong types and names; "
                              "the context header helps)", "```c",
                          fn["seed"].strip(), "```"]
        walls = self.store.all(
            "SELECT signature, reason, at FROM function_walls WHERE function_id=? ORDER BY at DESC LIMIT 3",
            fn["id"])
        if walls:
            lines += ["", "### Known walls (don't re-sweep these without new information)"]
            lines += [f"- {w['signature']} ({_ago(w['at'])}): {w['reason']}" for w in walls]
        merges = self.store.all(
            "SELECT m.id, m.at, c.name AS cont FROM merge_proposals m JOIN functions c ON c.id=m.continues_id "
            "WHERE m.function_id=? ORDER BY m.id DESC LIMIT 2", fn["id"])
        if merges:
            lines += ["", "### Boundary merge proposals (review; never applied automatically)"]
            lines += [f"- {fn['name']} continues {m['cont']} ({_ago(m['at'])})" for m in merges]
        notes = self.store.all(
            "SELECT n.text, n.created_at, a.name FROM notes n LEFT JOIN agents a ON a.id=n.agent_id "
            "WHERE n.function_id=? ORDER BY n.id DESC LIMIT 8", fn["id"])
        if notes:
            lines += ["", "### Notes from previous agents on this function"]
            lines += [f"- ({n['name']}, {_ago(n['created_at'])}) {n['text']}" for n in notes]
        pnotes = self.store.all(
            "SELECT n.text, a.name FROM notes n LEFT JOIN agents a ON a.id=n.agent_id "
            "WHERE n.project_id=? AND n.function_id IS NULL ORDER BY n.id DESC LIMIT 5", proj["id"])
        if pnotes:
            lines += ["", "### Project-wide notes (latest)"] + [f"- ({n['name']}) {n['text']}" for n in pnotes]
        if fn["unit"] and fn["unit"] in self._unit_contexts(proj["id"]):
            lines.insert(2, f"source unit {fn['unit']}: call get_context with function=\"{fn['name']}\" for its header")
        if header == "CLAIMED":
            lines += ["", "Next: read context.h with get_context, write C for this function, and iterate with "
                          "compile_and_diff until score is 0. Submit only the function body (plus any helper "
                          "declarations); context.h is included automatically."]
        return "\n".join(lines)

    # --------------------------------------------------------- compile & diff

    def compile_and_diff(self, agent: Agent, function: str, source: str, project: str | None = None) -> str:
        return self.attempt(agent, function, source, project)["text"]

    def attempt(self, agent: Agent, function: str, source: str, project: str | None = None) -> dict:
        """Compile, score and record one attempt. Returns {"text", "score", "matched"} (score None: didn't compile)."""
        self._require_budget(agent)
        fn = self._function(function, project)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        now = time.time()

        # Claim bookkeeping first: you can't score (or get credit) on someone else's function.
        with telemetry.span("attempt.claim"):
            with self.store.tx() as db:
                self._expire_claims(db)
                claim = db.execute("SELECT agent_id FROM claims WHERE function_id=?", (fn["id"],)).fetchone()
                scratch = fn["status"] == "matched"
                if not scratch:
                    if claim is None:
                        held = db.execute("SELECT 1 FROM claims WHERE agent_id=?", (agent.id,)).fetchone()
                        if held:
                            raise NetworkError("you hold another claim; release it before working on a different function")
                        db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fn["id"], agent.id, now, now + self.claim_ttl))
                        self._event(db, fn["project_id"], agent.id, "claim", fn["name"])
                    elif claim["agent_id"] != agent.id:
                        raise NetworkError(f"{fn['name']} is claimed by {self._agent_name(claim['agent_id'])}")
                    else:
                        db.execute("UPDATE claims SET expires_at=? WHERE function_id=?", (now + self.claim_ttl, fn["id"]))

        ctx = self._context(proj["id"], fn["unit"])
        snap = snapshots.capture(fn, proj, ctx)
        with telemetry.span("attempt.compile", origin=agent.name):
            result = self._compile(proj, ctx["header"], source)

        if not result.ok:
            # A jump-table (or other assembly-level) rejection: the PSY-Q pipeline could assemble it, so
            # try the variants before reporting a compile error.
            alt = self._variant_attempt(fn, proj, source, ctx, None)
            if alt is None:
                self._record(fn, agent, None, source, ctx["version"], snapshot=snap)
                return {"text": f"❌ COMPILE ERROR (context v{ctx['version']})\n{result.log.strip()}",
                        "score": None, "matched": False}
            return self._judge(agent, fn, proj, ctx, snap, source, alt[0], scratch, variants=False)

        return self._judge(agent, fn, proj, ctx, snap, source, result, scratch)

    def _judge(self, agent: Agent, fn, proj, ctx, snap: dict, source: str, result, scratch: bool,
               variants: bool = True) -> dict:
        """Evaluate one compiled object against fn's target and record the result: the single evaluator +
        acceptance path shared by attempt() (one compile, one function) and attempt_many() (one compile,
        several functions) — plan P2.1/P2.2: the batch must not grow a second acceptance implementation."""
        variant_note = False
        with telemetry.span("attempt.evaluate"):
            cand, verdict, problems = self._evaluate(fn, proj, result.obj)
        if cand is None:
            self._record(fn, agent, None, source, ctx["version"], snapshot=snap)
            return {"text": f"❌ Compiled, but the object has no function named `{fn['name']}`. "
                            f"Define it with exactly that name (non-static).", "score": None, "matched": False}
        if self._hash_only(fn):
            matched_now = cand.fingerprint() == fn["fingerprint"] and not scratch
            score = 0 if matched_now else None
        else:
            with telemetry.span("attempt.score"):
                d = scoring.diff(self._target_obj(fn, proj), cand)
            if verdict is not None:
                d = scoring.DiffResult(0 if verdict else max(1, d.score), d.max_score, d.rows, verdict)
            if self._worth_variant(verdict, d):
                # near miss: one attempt-scoped assembler-variant compile, kept only when strictly better
                alt = self._variant_attempt(fn, proj, source, ctx, d)
                if alt is not None:
                    result, cand, verdict, problems, d = alt
                    variant_note = True
            matched_now = d.bytes_match and not scratch
            score = d.score

        ok, why = snapshots.check_current(self, proj, fn, snap)
        if not ok:
            # The context/target/compiler moved while this compiled: the result is historical search
            # evidence only — never a current best or a match (plan P1.4).
            self._record(fn, agent, score, source, ctx["version"], matched=False, compiled=True,
                         snapshot=snap, stale_reason=why)
            return {"text": self._stale_text(proj, fn, snap, why), "score": score, "matched": False}

        if matched_now and self.acceptance_paused(proj["slug"]):
            # Runbook 24.1 step 1: exact, but new acceptance is paused for this project. The result
            # is retained as search evidence (attempt, snapshot, candidate) and the claim is kept so
            # the holder can re-run after resume_acceptance — never accepted while paused.
            self._record(fn, agent, score, source, ctx["version"], matched=False, compiled=True,
                         snapshot=snap)
            reason = self._pause_reason(proj["slug"])
            return {"text": (f"⏸ ACCEPTANCE PAUSED for {proj['slug']}"
                             + (f" ({reason})" if reason else "")
                             + ": this attempt matches byte-for-byte, but new acceptance is paused "
                               "per runbook 24.1. The score, source and snapshot are retained as "
                               "evidence; the function stays open and your claim is kept. Revalidate "
                               f"and call resume_acceptance('{proj['slug']}'), then re-run."),
                    "score": score, "matched": False}

        if self._hash_only(fn):
            self._record(fn, agent, 0 if matched_now else None, source, ctx["version"], matched=matched_now,
                         compiled=True, snapshot=snap)
            if matched_now:
                return {"text": f"✅ MATCH! {fn['name']} matches the target fingerprint. Credited to {agent.name}; "
                                f"your claim is released.", "score": 0, "matched": True}
            return {"text": "❌ No match. This is a hash-only project, so the coordinator can only say match / no match. "
                            f"For a scored diff, run `decompnet local diff <your-object.o> {fn['name']} <your.c>` "
                            "against your own lawful copy.", "score": None, "matched": False}
        self._record(fn, agent, d.score, source, ctx["version"], matched=matched_now, snapshot=snap)

        if scratch:
            head = f"(scratch — {fn['name']} was already matched) score {d.score}"
        elif d.bytes_match:
            return {"text": f"✅ MATCH! {fn['name']} is byte-identical to the target ({fn['n_insns']} instructions). "
                            f"Credited to {agent.name}; your claim is released. Call claim_function for the next one.",
                    "score": 0, "matched": True}
        else:
            best = self.store.one("SELECT best_score FROM functions WHERE id=?", fn["id"])["best_score"]
            head = (f"score {d.score} ({scoring.percent_text(d.percent)} match) — best so far {best}. "
                    f"Target {len([r for r in d.rows if r[1]])} insns, yours {len([r for r in d.rows if r[2]])}.")
            if d.rows and all(m == " " for m, _, _ in d.rows):
                head += ("\nNOT A MATCH YET: every instruction reads the same, but the bytes differ"
                         + (" because of the linking notes at the end: fix those." if problems else
                            ": an encoding the listing doesn't show (branch form, an immediate, a relocation)."))
        warn = ""
        if variant_note:
            tail = result.log.strip().splitlines()[-1] if result.log.strip() else "used"
            warn = f"\nassembler variant: {tail}"
        elif result.log.strip():
            warn = f"\ncompiler output:\n{result.log.strip()}"
        if problems:
            warn += "\nwhen linking your code at the function's address:\n" + "\n".join(f"- {p}" for p in problems)
        return {"text": f"{head}\n{d.render()}{warn}{self.stall_note(fn['id'], agent.id)}", "score": d.score, "matched": False}

    def stall_note(self, function_id: int, agent_id: int, after: int | None = None) -> str:
        """A nudge once an agent's last `after` compiles of a function didn't beat its best: functions that match
        mostly do in the first few tries, and the agent's time is better spent on the next one. Big functions get
        longer (a quarter of Mario Party 6's agent matches over 64 instructions took more than 4 compiles, and
        those functions hold most of the code left)."""
        if after is None:
            row = self.store.one("SELECT n_insns FROM functions WHERE id=?", function_id)
            n = (row["n_insns"] if row else 0) or 0
            after = 4 if n < 64 else 6 if n < 128 else 8 if n < 256 else 10
        rows = self.store.all("SELECT score FROM attempts WHERE function_id=? AND agent_id=? ORDER BY id DESC LIMIT 16",
                              function_id, agent_id)
        best, since = None, 0
        for r in reversed(rows):
            if r["score"] is not None and (best is None or r["score"] < best):
                best, since = r["score"], 0
            else:
                since += 1
        if since < after:
            return ""
        nudge = (f"\n\n⏭ No better in your last {since} compiles. release_function with notes (what you tried, what's "
                 "left) and claim the next one: functions that match mostly do in the first few tries, and this one "
                 "keeps your best attempt for whoever comes next.")
        return nudge + _escalation_advice(self, function_id, agent_id, since)

    def analysis_broker(self):
        """The coordinator's analysis broker (P8). Built lazily with NO providers attached: the app
        layer (the chat.py wiring) registers DirectGhidraProvider/ReaProvider on it. The lazy import
        keeps the claim/attempt hot paths free of the broker (A-02)."""
        from .analysis import Broker

        broker = getattr(self, "_analysis_broker", None)
        if broker is None:
            broker = Broker()
            self._analysis_broker = broker
        return broker

    def escalate(self, function: str, *, question: str | None = None, provider: str | None = None,
                 budget: dict | None = None, project: str | None = None) -> str:
        """A.3: a stalled agent asks the broker for a focused, budget-capped investigation of one
        function (P8.5). Provider-optional: with no registered provider the agent gets explicit
        guidance, never a silent guess; admission failures are reported verbatim. Candidates produced
        here are bound to the function revision (A-06): a later context/target change demotes them to
        historical evidence instead of letting them be applied as current."""
        from .analysis.contracts import Budget, CapabilityError, TargetError

        fn = self._function(function, project)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        broker = self.analysis_broker()
        profile = _profile_for(proj, fn)
        if profile is None:
            return (f"escalate_function: no admitted Ghidra profile for arch {proj['arch']!r} "
                    f"({'big' if proj['big_endian'] else 'little'}-endian), or {fn['name']} has no "
                    f"target bytes in the coordinator — escalation is unavailable for this project.")
        wall = float((budget or {}).get("wall_seconds", 60.0))
        cap = Budget(wall_seconds=wall, worker_seconds=wall,
                     token_limit=int((budget or {}).get("token_limit", 8000)))
        module = proj["slug"] or f"project-{proj['id']}"
        key = f"{module}::{fn['name']}"
        import shutil
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="decompnet-escalate-"))
        try:
            artifact = tmp / f"{fn['name']}.bin"
            artifact.write_bytes(fn["target"])
            broker.register_target(module, artifact, profile, provider=provider,
                                   mapping={"function": fn["name"], "vram": fn["vram"] or 0,
                                            "rom_offset": fn["rom_offset"]})
            investigation = broker.propose_investigation(
                key, question or f"How should {fn['name']} be changed to reach an exact match?",
                None, cap, snapshot=f"rev-{fn['revision']}", requester="escalate_function")
            return _render_investigation(investigation, cap)
        except (CapabilityError, TargetError) as e:
            return f"escalate_function: {e}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _validity(self, fn, source: str) -> validity.Validity:
        """Run the undefined-behaviour guard on one candidate source.

        The judge is byte-exact and therefore blind to undefined behaviour: a source
        that reads through an uninitialised pointer can match the target's instruction
        shapes (see decompnet.validity for the measured case). Returns an always-ok
        verdict when the project context cannot be resolved, so the guard can never
        turn into an outage.
        """
        try:
            proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
            ctx = self._context(fn["project_id"], fn["unit"])
            raw = json.loads(proj["config"])
            if str(raw.get("arch") or "").lower() == "sm83":
                # The probe is a GCC `-Wuninitialized` check: SDCC/GBDK has no equivalent flag, so on the
                # Game Boy lane it is skipped rather than run with flags the compiler would reject.
                return validity.Validity(ok=True, detail="guard not applicable to the Game Boy lane (SDCC)")
            cfg = compiler_config(Path(proj["root"]), raw)
        except Exception:  # pragma: no cover - defensive: never block on guard setup
            return validity.Validity(ok=True, detail="guard skipped: context unavailable")
        return validity.check(cfg, ctx["header"], source, compile_source)

    def _flag_validity(self, db, fn, source: str, why: str, verdict) -> None:
        """Record a validity verdict: a meta flag on the function plus an operator note."""
        meta = fn["meta"]
        meta = json.loads(meta) if meta else {}
        meta["validity"] = {"why": why, **verdict.as_dict()}
        db.execute("UPDATE functions SET meta=? WHERE id=?", (json.dumps(meta), fn["id"]))
        db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(?,?,?,?,?)",
                   (fn["project_id"], fn["id"], None,
                    f"⚠ validity guard ({why}): {validity.describe(verdict)}", time.time()))

    def _record(self, fn, agent: Agent, score: int | None, source: str, ctx_version: int, matched: bool = False,
                compiled: bool | None = None, via: str = "agent", snapshot: dict | None = None,
                stale_reason: str | None = None):
        """Record one attempt. `stale_reason` marks the result historical: it is stored (with its snapshot)
        as search evidence but never updates best_score/status, never accepts a match and never writes a
        receipt (plan P1.4: a stale compilation cannot become a current match)."""
        telemetry.count("attempt.record")
        now = time.time()
        compiled = score is not None if compiled is None else compiled
        if matched and self.acceptance_paused(
                self.store.one("SELECT slug FROM projects WHERE id=?", fn["project_id"])["slug"]):
            # Runbook 24.1 step 1, enforced for every ingress path (agents, permuter, jobs, twins):
            # retained as search evidence, never accepted while the project's acceptance is paused.
            matched = False
        with self.store.tx() as db:
            cur = db.execute("INSERT INTO attempts(function_id,agent_id,score,compiled,context_version,source,created_at,via) "
                             "VALUES(?,?,?,?,?,?,?,?)", (fn["id"], agent.id, score, compiled, ctx_version, source, now, via))
            attempt_id = cur.lastrowid
            if snapshot is not None:
                db.execute(
                    "INSERT INTO attempt_snapshots(attempt_id, project_id, function_id, function_revision, "
                    "context_version, context_digest, target_digest, compiler_digest, scorer_version, policy_version, "
                    "stale_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (attempt_id, snapshot["project_id"], snapshot["function_id"], snapshot["function_revision"],
                     snapshot["context_version"], snapshot["context_digest"], snapshot["target_digest"],
                     snapshot["compiler_digest"], snapshot["scorer_version"], snapshot["policy_version"], stale_reason))
            db.execute("UPDATE functions SET attempts=attempts+1 WHERE id=?", (fn["id"],))
            if stale_reason is not None:
                return  # historical only: no best/status/claim/receipt/epoch changes
            # P4.6: retain every distinct source as a provenance-tagged candidate instead of letting the
            # latest draft overwrite the only copy; alternatives are never current-best unless rescored.
            self._note_candidate(db, fn, via, source, snapshot)
            if score is not None:
                cur = db.execute("UPDATE functions SET best_score=?, best_source=?, best_agent_id=?, best_verified=1 "
                                 "WHERE id=? AND status='open' AND (best_score IS NULL OR best_score > ?)",
                                 (score, source, agent.id, fn["id"], score))
                # A materially better source from an agent is new search input: give the permuter a fresh
                # budget (bounded, reset reason recorded). The permuter's own improvements don't reset its
                # own budget — that would make the budget infinite. Equal scores (cosmetic changes) don't
                # either: the strict best_score > ? comparison is what gates this.
                if cur.rowcount and via != "permuter":
                    db.execute("UPDATE search_budgets SET turns_used=0, epoch=?, reset_reason='better-source', "
                               "last_at=? WHERE function_id=? AND turns_used > 0",
                               (f"best-{score}", now, fn["id"]))
                # A near-miss improvement is exactly where undefined behaviour hides (an
                # uninitialised pointer that lands in a plausible register scores well by
                # shape). Flag it without blocking: the match guard below is the hard gate.
                if cur.rowcount and score <= 500:
                    verdict = self._validity(fn, source)
                    if not verdict.ok:
                        self._flag_validity(db, fn, source, f"score {score}", verdict)
            if matched:
                verdict = self._validity(fn, source)
                if verdict.ok and verdict.detail:
                    # The guard could not run (context/toolchain unavailable). Accepting the
                    # match is the only non-blocking option, but never silently.
                    self._flag_validity(db, fn, source, "match accepted without guard", verdict)
                if not verdict.ok:
                    # Refuse the match: a byte-identical build from a source with an
                    # uninitialised use is not finished work. Stay open and say why.
                    matched = False
                    self._flag_validity(db, fn, source, "match refused", verdict)
            if matched:
                db.execute("UPDATE functions SET status='matched', matched_source=?, matched_agent_id=?, "
                           "matched_at=?, best_score=0, best_source=? WHERE id=?",
                           (source, agent.id, now, source, fn["id"]))
                db.execute("DELETE FROM claims WHERE function_id=?", (fn["id"],))
                db.execute("DELETE FROM search_budgets WHERE function_id=?", (fn["id"],))
                snap = snapshot if snapshot is not None else snapshots.capture(
                    fn, db.execute("SELECT * FROM projects WHERE id=?", (fn["project_id"],)).fetchone(),
                    self._context(fn["project_id"], fn["unit"]))
                db.execute(
                    "INSERT OR REPLACE INTO verification_receipts(function_id, attempt_id, snapshot_id, source_digest, "
                    "candidate_object_digest, origin, object_match, function_link_match, unit_code_data_match, "
                    "whole_image_checksum, acceptance_policy, verified_at_utc) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (fn["id"], attempt_id, snapshots.snapshot_id(snap), snapshots.sha(source.encode()), None,
                     via, "fingerprint" if fn["target"] is None else "exact",
                     "exact", "unverified", "not_run", f"rom-function-policy-v{snapshots.POLICY_VERSION}",
                     snapshots.now_utc()))
                db.execute("UPDATE projects SET matched_epoch=matched_epoch+1 WHERE id=?", (fn["project_id"],))
                self._event(db, fn["project_id"], agent.id, "match", fn["name"])
        if matched:
            self.on_match(self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"]), fn["id"], agent)

    def _note_candidate(self, db, fn, origin: str, source: str, snapshot: dict | None) -> None:
        """P4.6: keep distinct sources as provenance-tagged alternatives (bounded per function). The
        current best/matched source remains authoritative; candidates are only ever current-best after
        being rescored under the current snapshot."""
        if not source:
            return
        digest = snapshots.sha(source.encode())
        if db.execute("SELECT 1 FROM candidates WHERE function_id=? AND source_digest=?", (fn["id"], digest)).fetchone():
            return
        db.execute(
            "INSERT INTO candidates(function_id, origin, source, source_digest, evidence, score_version, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (fn["id"], origin, source, digest,
             snapshots.snapshot_id(snapshot) if snapshot else None,
             snapshots.SCORER_VERSION, time.time()))
        db.execute("DELETE FROM candidates WHERE function_id=? AND id NOT IN "
                   "(SELECT id FROM candidates WHERE function_id=? ORDER BY id DESC LIMIT 50)", (fn["id"], fn["id"]))

    def alternatives(self, function: str, project: str | None = None, limit: int = 10) -> str:
        """The retained candidate sources for a function, newest first, with origin and whether each was
        recorded under the current dependency snapshot (P4.6: alternatives are rescored before reuse)."""
        fn = self._function(function, project)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        ctx = self._context(proj["id"], fn["unit"])
        rows = self.store.all(
            "SELECT * FROM candidates WHERE function_id=? ORDER BY id DESC LIMIT ?", fn["id"], limit)
        if not rows:
            return f"No retained candidates for {fn['name']} yet."
        out = [f"### {fn['name']}: {len(rows)} retained candidate(s)"]
        for r in rows:
            fresh = r["evidence"] is None or (
                r["score_version"] == snapshots.SCORER_VERSION and ctx is not None
                and r["evidence"] == snapshots.snapshot_id(snapshots.capture(fn, proj, ctx)))
            out.append(f"- {r['origin']} ({'current-snapshot' if fresh else 'historical'}): "
                       f"{r['source'].strip().splitlines()[0] if r['source'].strip() else '(empty)'}")
        return "\n".join(out)

    # --------------------------------------- acceptance pause (runbook 24.1)

    def pause_acceptance(self, slug: str, reason: str = "") -> str:
        """Runbook 24.1 step 1 (benchmarks/RUNBOOKS.md): pause NEW acceptance for one project while
        read-only inspection stays up. The runbook this helper belongs to:

          1. Pause new acceptance for the affected project/scope; read-only inspection stays up.
          2. Retain the source, object, target, receipt (`verification_receipts`), snapshot
             (`attempt_snapshots`) and relevant logs.
          3. Compare dependency identities with the current project: context version/digest,
             target digest, compiler profile identity (`toolchains.profile_identity`), matched epoch.
          4. Classify: stale context / cache corruption / changed toolchain / changed target-layout /
             policy drift.
          5. Disable the affected optimisation (benchmarks/FEATURE-FLAGS.md), never the verification gate.
          6. Re-evaluate affected accepted sources under a coherent current snapshot
             (`Network.regression_test`).
          7. Restore only validated current state; keep historical source (`candidates` table).
          8. Add a deterministic regression fixture before re-enabling.

        While paused, a byte-exact attempt is recorded as search evidence (score, source, snapshot,
        candidate row) but NOT accepted: no status change, no receipt, no matched-epoch bump, and the
        claim is kept so the holder can re-run after `resume_acceptance`. The pause is operator state
        held in memory: a coordinator restart clears it — pause again if the condition persists."""
        proj = self._project(slug)
        reason = (reason or "").strip()[:200]
        with self._pause_lock:
            self._paused_acceptance[proj["slug"]] = (reason, time.time())
        return (f"⏸ Acceptance paused for {proj['slug']}"
                + (f": {reason}" if reason else "")
                + ". Read-only inspection stays up; exact matches are retained as evidence, not accepted "
                  "(runbook 24.1). Validate, then call resume_acceptance to lift the pause.")

    def resume_acceptance(self, slug: str) -> str:
        """Runbook 24.1 steps 7-8: lift the acceptance pause once the affected state was revalidated
        under a coherent snapshot and a regression fixture exists. Re-running the retained attempt
        (the claim was kept) accepts it through the ordinary verified path."""
        proj = self._project(slug)
        with self._pause_lock:
            was = self._paused_acceptance.pop(proj["slug"], None)
        if was is None:
            return f"Acceptance for {proj['slug']} was not paused."
        return (f"▶ Acceptance resumed for {proj['slug']}. Re-run the retained attempt "
                f"(the claim was kept) to accept it through the ordinary verified path.")

    def acceptance_paused(self, slug: str) -> bool:
        with self._pause_lock:
            return slug in self._paused_acceptance

    def _pause_reason(self, slug: str) -> str:
        with self._pause_lock:
            return self._paused_acceptance.get(slug, ("", 0.0))[0]

    # ------------------------------------------------- legacy revalidation (PR-31)

    def mark_legacy_unverified(self, project: str | None = None) -> int:
        """One-time provenance audit: any open function whose current best attempt predates the
        snapshot/receipt machinery (its attempts have no attempt_snapshots rows) gets
        best_verified=0 — the best score is retained but flagged until reproduced under the current
        snapshot. This is what perf-test found on func_8004B738: a stored best no longer reachable."""
        proj = self._project(project)
        n = 0
        with self.store.tx() as db:
            for fn in db.execute("SELECT * FROM functions WHERE project_id=? AND status='open' "
                                 "AND best_source IS NOT NULL", (proj["id"],)).fetchall():
                latest = db.execute(
                    "SELECT a.id FROM attempts a WHERE a.function_id=? AND a.score IS NOT NULL "
                    "AND a.score=(SELECT best_score FROM functions WHERE id=?) ORDER BY a.id DESC LIMIT 1",
                    (fn["id"], fn["id"])).fetchone()
                if latest is None:
                    continue
                has_snap = db.execute("SELECT 1 FROM attempt_snapshots WHERE attempt_id=?",
                                      (latest["id"],)).fetchone()
                if has_snap is None:
                    db.execute("UPDATE functions SET best_verified=0 WHERE id=?", (fn["id"],))
                    n += 1
        return n

    def revalidate_bests(self, project: str | None = None, limit: int = 10) -> str:
        """Recompile each unverified legacy best under the CURRENT snapshot (bounded): a reproduced
        score flips it back to verified; anything else stays flagged with a note. Never clears the
        best source itself — historical evidence is preserved, only the trust flag changes."""
        proj = self._project(project)
        todo = self.store.all("SELECT * FROM functions WHERE project_id=? AND status='open' "
                              "AND best_verified=0 AND best_source IS NOT NULL LIMIT ?", proj["id"], limit)
        if not todo:
            return "No unverified legacy best attempts to revalidate."
        out = []
        for fn in todo:
            ctx = self._context(proj["id"], fn["unit"])
            result = self._compile(proj, ctx["header"], fn["best_source"])
            if not result.ok:
                out.append(f"❌ {fn['name']}: best no longer compiles under v{ctx['version']} — "
                           f"stays unverified (historical only)")
                continue
            cand, verdict, _ = self._evaluate(fn, proj, result.obj)
            if cand is None:
                out.append(f"❌ {fn['name']}: best compiles but no longer contains the function — "
                           f"stays unverified")
                continue
            if self._hash_only(fn):
                ok = cand.fingerprint() == fn["fingerprint"]
                score = 0 if ok else None
            else:
                d = scoring.diff(self._target_obj(fn, proj), cand)
                if verdict is not None:
                    d = scoring.DiffResult(0 if verdict else max(1, d.score), d.max_score, d.rows, verdict)
                score, ok = d.score, d.score == fn["best_score"]
            with self.store.tx() as db:
                db.execute("UPDATE functions SET best_verified=1 WHERE id=?", (fn["id"],))
                db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) "
                           "VALUES(?,?,NULL,?,?)", (proj["id"], fn["id"],
                                                    f"revalidated legacy best: reproduces (score {score})"
                                                    if ok else f"revalidated legacy best: best {fn['best_score']} "
                                                               f"not reproduced (got {score})", time.time()))
            out.append(f"{'✅' if ok else '⚠'} {fn['name']}: legacy best "
                       f"{'reproduced' if ok else 'NOT reproduced'} under v{ctx['version']} "
                       f"(stored {fn['best_score']}, got {score})")
        return "\n".join(out)

    def propose_merge(self, function: str, continues: str, project: str | None = None,
                      evidence: str = "") -> str:
        """PR-31: record a boundary-merge proposal — `function` looks like the continuation of
        `continues` (adjacent bytes, entry uses a live-in saved register with no prologue). Merges are
        NEVER applied automatically: the proposal is reviewable evidence until an operator merges.
        Forks edit their own source files; this is ROM-boundary territory only."""
        fn = self._function(function, project)
        cont = self._function(continues, project)
        if fn["project_id"] != cont["project_id"]:
            raise NetworkError("both functions must be in the same project")
        if fn["status"] == "matched" or cont["status"] == "matched":
            raise NetworkError("matched functions can't be merged")
        a = (fn["vram"], fn["rom_offset"], fn["size"])
        b = (cont["vram"], cont["rom_offset"], cont["size"])
        # `function` is the tail: it starts where `continues` ends (same direction as merge_function,
        # and the reverse of split_function, which cuts the tail off the head).
        adjacent = (b[0] is not None and a[0] is not None and a[0] == b[0] + (b[2] or 0)) or \
                   (b[1] is not None and a[1] is not None and a[1] == b[1] + (b[2] or 0))
        if not adjacent:
            raise NetworkError(f"{fn['name']} and {cont['name']} are not adjacent: no boundary to merge")
        proj_row = self.store.one("SELECT config FROM projects WHERE id=?", fn["project_id"])
        arch = (json.loads(proj_row["config"] or "{}").get("arch") or "") if proj_row else ""
        if not _fragment_like(fn, arch):
            raise NetworkError(f"{fn['name']} doesn't look like a fragment (no live-in saved register at entry)")
        with self.store.tx() as db:
            db.execute("INSERT INTO merge_proposals(function_id, continues_id, at, evidence) VALUES(?,?,?,?)",
                       (fn["id"], cont["id"], time.time(), evidence.strip()[:400]))
        return (f"Merge proposed: {fn['name']} continues {cont['name']} (adjacent at "
                f"{hex(fn['vram'] or 0)} / {hex(cont['vram'] or 0)}). Recorded for review; nothing was "
                f"merged automatically.")

    def _merge_proposal(self, fragment_id: int, continues_id: int):
        """The latest reviewed proposal authorizing a merge of `fragment` into `continues` (C.4:
        merges are executed only on this review evidence — never applied automatically)."""
        return self.store.one(
            "SELECT * FROM merge_proposals WHERE function_id=? AND continues_id=? ORDER BY id DESC LIMIT 1",
            fragment_id, continues_id)

    def _consume_merge_proposals(self, db, function_id: int) -> None:
        """A merge consumed the reviewed evidence: every proposal touching the merged (deleted)
        function is void once its boundary no longer exists."""
        db.execute("DELETE FROM merge_proposals WHERE function_id=? OR continues_id=?",
                   (function_id, function_id))

    def _delete_function_rows(self, db, function_id: int) -> None:
        """Remove one function's dependent rows (the function row itself the caller deletes): a
        removed function must not leave dangling claims, attempts, budgets, walls or proposals."""
        for table in ("claims", "attempts", "attempt_snapshots", "candidates", "notes", "function_walls",
                      "twin_misses", "verification_receipts"):
            db.execute(f"DELETE FROM {table} WHERE function_id=?", (function_id,))
        db.execute("DELETE FROM search_budgets WHERE function_id=?", (function_id,))

    def on_match(self, proj, function_id: int, agent: Agent | None = None) -> None:
        """A function just matched (subclasses try its code on its twins)."""

    # -------------------------------------------------------- shared context

    def edit_context(self, agent: Agent, project: str | None, old: str, new: str, message: str,
                     function: str | None = None) -> str:
        """Exact-string edit of a header. Accepted only if every already-matched function compiled against
        that header still matches with the new one — that's the project's test suite."""
        self._require_level(agent, "Editing the shared header")
        if function:
            fn = self._function(function, project)
            proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
            unit = self._effective_unit(fn, self._unit_contexts(proj["id"]))
        else:
            proj = self._project(project)
            if self._unit_contexts(proj["id"]):
                raise NetworkError(f"{proj['slug']} has one header per source file: pass `function` to edit the "
                                   f"header that function compiles against")
            unit = ""
        if not message.strip():
            raise NetworkError("give a short commit message explaining the change")
        for _ in range(3):
            ctx = self._context(proj["id"], unit)
            header = ctx["header"]
            if old:
                from .textedit import replace_once

                new_header = replace_once(header, old, new, "the header")
            else:
                new_header = header.rstrip("\n") + "\n\n" + new.strip("\n") + "\n"
            members = self._matched_using(proj, unit)
            epoch = self.store.one("SELECT matched_epoch FROM projects WHERE id=?", proj["id"])["matched_epoch"]
            broken = self.regression_test(proj, new_header, only=members)
            if broken:
                listing = "\n".join(f"- {name}: {why}" for name, why in broken[:10])
                return (f"❌ REJECTED: the edit breaks {len(broken)} already-matched function(s), so the header "
                        f"stays at v{ctx['version']}.\n{listing}")
            with self.store.tx() as db:
                if self._latest_version(db, proj["id"], unit) != ctx["version"]:
                    continue  # someone else committed meanwhile: re-apply on top and re-test
                if self._acceptance_moved(db, proj, unit, epoch, members):
                    continue  # a match (or reset) changed the protected set while we validated: re-test
                version = self._next_version(db, proj["id"])
                db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                           "VALUES(?,?,?,?,?,?,?)", (proj["id"], version, new_header, agent.id, message.strip(), time.time(), unit))
                self._reset_budgets_for_context(db, proj["id"], unit, version)
                self._event(db, proj["id"], agent.id, "context", f"v{version}{f' ({unit})' if unit else ''}: {message.strip()}")
            return f"✅ header is now v{version} (all {len(members)} matched functions that use it still match)."
        raise NetworkError("the header is being edited heavily right now; re-read it and retry")

    def _acceptance_moved(self, db, proj, unit: str, epoch: int, members: list) -> bool:
        """Did acceptance state change within the edited scope since validation started (plan P1.4 step 6)?
        The project-wide header protects the whole project, so the project matched_epoch is authoritative.
        A per-unit header protects only that unit: its matched set is the scoped epoch, so a match in an
        unrelated unit never voids valid work (plan V-08)."""
        if unit == "":
            return db.execute("SELECT matched_epoch FROM projects WHERE id=?", (proj["id"],)).fetchone()["matched_epoch"] != epoch
        return {m["id"] for m in self._matched_using(proj, unit)} != {m["id"] for m in members}

    def rename_symbol(self, agent: Agent, project: str | None, old: str, new: str, message: str = "") -> str:
        """Project-wide refactor: rename an identifier (field, type, global, function param...) in every
        header *and* in every matched source / best attempt, then regression-test the result.
        The rename is textual (whole words), so it is meant for unique names like `unk_1C` or `func_80012345`."""
        self._require_level(agent, "Renaming across the project")
        proj = self._project(project)
        ident = re.compile(r"^[A-Za-z_]\w*$")
        if not (ident.match(old) and ident.match(new)):
            raise NetworkError("old and new must be C identifiers")
        word = re.compile(rf"\b{re.escape(old)}\b")
        for _ in range(3):
            units = self._unit_contexts(proj["id"])
            current = {u: self._context(proj["id"], u) for u in ({""} | units)}
            changed = {u: word.sub(new, c["header"]) for u, c in current.items() if word.search(c["header"])}
            if not changed:
                raise NetworkError(f"`{old}` does not occur in any header")
            matched = self.store.all("SELECT * FROM functions WHERE project_id=? AND status='matched'", proj["id"])
            epoch = self.store.one("SELECT matched_epoch FROM projects WHERE id=?", proj["id"])["matched_epoch"]
            rewritten = {r["id"]: word.sub(new, r["matched_source"]) for r in matched}
            header_for = lambda fn: changed.get(self._effective_unit(fn, units), current[self._effective_unit(fn, units)]["header"])
            broken = self.regression_test(proj, None, sources=rewritten, only=matched, header_for=header_for)
            if broken:
                listing = "\n".join(f"- {name}: {why}" for name, why in broken[:10])
                return f"❌ REJECTED: renaming breaks {len(broken)} matched function(s).\n{listing}"
            msg = message.strip() or f"rename {old} -> {new}"
            with self.store.tx() as db:
                if db.execute("SELECT matched_epoch FROM projects WHERE id=?", (proj["id"],)).fetchone()["matched_epoch"] != epoch \
                        or any(self._latest_version(db, proj["id"], u) != current[u]["version"] for u in changed):
                    continue  # a match (or another edit) landed while we validated: re-read and re-test
                for u, header in changed.items():
                    version = self._next_version(db, proj["id"])
                    db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                               "VALUES(?,?,?,?,?,?,?)", (proj["id"], version, header, agent.id, msg, time.time(), u))
                    self._reset_budgets_for_context(db, proj["id"], u, version)
                for fid, src in rewritten.items():
                    db.execute("UPDATE functions SET matched_source=?, best_source=? WHERE id=?", (src, src, fid))
                for r in db.execute("SELECT id, best_source FROM functions WHERE project_id=? AND status='open' "
                                    "AND best_source IS NOT NULL", (proj["id"],)).fetchall():
                    db.execute("UPDATE functions SET best_source=? WHERE id=?", (word.sub(new, r["best_source"]), r["id"]))
                self._event(db, proj["id"], agent.id, "context", f"{msg} ({len(changed)} header(s))")
            return (f"✅ renamed `{old}` -> `{new}` in {len(changed)} header(s) and in {len(rewritten)} matched "
                    f"sources; all still match.")
        raise NetworkError("headers are being edited heavily right now; retry")

    def _latest_version(self, db, project_id: int, unit: str) -> int | None:
        row = db.execute("SELECT MAX(version) AS v FROM context_versions WHERE project_id=? AND unit=?",
                         (project_id, unit)).fetchone()
        return row["v"]

    def _next_version(self, db, project_id: int) -> int:
        return (db.execute("SELECT MAX(version) AS v FROM context_versions WHERE project_id=?",
                           (project_id,)).fetchone()["v"] or 0) + 1

    def _matched_using(self, proj, unit: str) -> list:
        units = self._unit_contexts(proj["id"])
        return [f for f in self.store.all("SELECT * FROM functions WHERE project_id=? AND status='matched'", proj["id"])
                if self._effective_unit(f, units) == unit]

    def _reset_budgets_for_context(self, db, project_id: int, unit: str, version: int) -> None:
        """A committed header change is new search input for every function that compiles against that
        header: their permuter budgets reset (bounded; reset reason recorded). Effective-unit logic
        matches _effective_unit()."""
        db.execute(
            "UPDATE search_budgets SET turns_used=0, cooldown_until=0, epoch=?, reset_reason='context-changed', "
            "last_at=? WHERE (turns_used > 0 OR cooldown_until > 0) AND function_id IN ("
            "SELECT f.id FROM functions f WHERE f.project_id=? AND (CASE WHEN f.unit IN "
            "(SELECT unit FROM context_versions WHERE project_id=? AND unit != '') THEN f.unit ELSE '' END)=?)",
            (f"ctx-v{version}", time.time(), project_id, project_id, unit))

    def regression_test(self, proj, header: str | None, sources: dict[int, str] | None = None,
                        only: list | None = None, header_for=None) -> list[tuple[str, str]]:
        matched = only if only is not None else self.store.all(
            "SELECT * FROM functions WHERE project_id=? AND status='matched'", proj["id"])

        def check(fn) -> tuple[str, str] | None:
            res = self._compile(proj, header_for(fn) if header_for else header,
                                (sources or {}).get(fn["id"], fn["matched_source"]))
            if not res.ok:
                return fn["name"], "no longer compiles: " + res.log.strip().splitlines()[0][:200] if res.log.strip() else "compile failed"
            cand, verdict, _ = self._evaluate(fn, proj, res.obj)
            if verdict is None and cand is not None:
                want = fn["fingerprint"] if self._hash_only(fn) else self._target_obj(fn, proj).fingerprint()
                verdict = cand.fingerprint() == want
            if cand is None or not verdict:
                return fn["name"], "compiles but no longer matches"
            return None

        if not matched:  # nothing to protect, but the header must at least parse
            if header is None:
                return []
            res = self._compile(proj, header, "")
            return [] if res.ok else [("<header>", res.log.strip()[:300])]
        with telemetry.span("context.regression", n=len(matched)):
            return [b for b in self._pool.map(check, matched) if b]

    # ------------------------------------------------------------------ notes

    def add_note(self, agent: Agent, project: str | None, text: str, function: str | None = None) -> str:
        proj = self._project(project)
        fn = self._function(function, proj["slug"]) if function else None
        if not text.strip():
            raise NetworkError("empty note")
        with self.store.tx() as db:
            db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(?,?,?,?,?)",
                       (proj["id"], fn["id"] if fn else None, agent.id, text.strip()[:4000], time.time()))
        return "Noted. Other agents will see it" + (f" when they open {fn['name']}." if fn else " in their briefings.")

    def search_notes(self, project: str | None, query: str) -> str:
        proj = self._project(project)
        rows = self.store.all(
            "SELECT n.text, n.created_at, a.name AS agent, f.name AS fn FROM notes n "
            "LEFT JOIN agents a ON a.id=n.agent_id LEFT JOIN functions f ON f.id=n.function_id "
            "WHERE n.project_id=? AND n.text LIKE ? ORDER BY n.id DESC LIMIT 20", proj["id"], f"%{query}%")
        if not rows:
            return f"No notes matching {query!r}."
        return "\n".join(f"- [{r['fn'] or 'project'}] ({r['agent']}, {_ago(r['created_at'])}) {r['text']}" for r in rows)

    # ----------------------------------------------------------------- status

    def status_json(self) -> dict:
        with self.store.tx() as db:
            self._expire_claims(db)
        projects = []
        for p in self.store.all("SELECT * FROM projects ORDER BY slug"):
            agg = self.store.one(
                "SELECT COUNT(*) AS total, SUM(status='matched') AS matched, SUM(size) AS bytes, "
                "SUM(CASE WHEN status='matched' THEN size ELSE 0 END) AS matched_bytes, SUM(attempts) AS attempts "
                "FROM functions WHERE project_id=?", p["id"])
            claims = self.store.all(
                "SELECT f.name, a.name AS agent, c.claimed_at, f.best_score, f.n_insns FROM claims c "
                "JOIN functions f ON f.id=c.function_id JOIN agents a ON a.id=c.agent_id "
                "WHERE f.project_id=? ORDER BY c.claimed_at", p["id"])
            ctx = self._context(p["id"])
            claimed = {c["name"] for c in claims}
            funcs = [{"name": f["name"], "insns": f["n_insns"], "best": f["best_score"],
                      "status": "matched" if f["status"] == "matched" else "claimed" if f["name"] in claimed else "open"}
                     for f in self.store.all("SELECT name, n_insns, status, best_score FROM functions "
                                             "WHERE project_id=? ORDER BY name", p["id"])]
            legal = json.loads(p["config"]).get("legal", {})
            # P11.7: truthful verification scope and origins, not just green percentages
            receipts = self.store.one(
                "SELECT COUNT(*) AS n FROM verification_receipts r JOIN functions f ON f.id=r.function_id "
                "WHERE f.project_id=?", p["id"])["n"] or 0
            origins = {r["origin"]: r["n"] for r in self.store.all(
                "SELECT origin, COUNT(*) AS n FROM candidates c JOIN functions f ON f.id=c.function_id "
                "WHERE f.project_id=? GROUP BY origin", p["id"])}
            stalls = {r["reset_reason"] or "none": r["n"] for r in self.store.all(
                "SELECT reset_reason, COUNT(*) AS n FROM search_budgets b JOIN functions f ON f.id=b.function_id "
                "WHERE f.project_id=? GROUP BY reset_reason", p["id"])}
            projects.append({
                "legal": {"basis": legal.get("basis"), "license": legal.get("license")},
                "funcs": funcs,
                "slug": p["slug"], "name": p["name"], "arch": p["arch"], "description": p["description"],
                "functions": agg["total"], "matched": agg["matched"] or 0, "bytes": agg["bytes"] or 0,
                "matched_bytes": agg["matched_bytes"] or 0, "attempts": agg["attempts"] or 0,
                "context_version": ctx["version"] if ctx else 0,
                "verification": {"receipts": receipts, "matched_epoch": p["matched_epoch"]},
                "origins": origins, "stalls": stalls,
                "claims": [{"function": c["name"], "agent": c["agent"], "since": c["claimed_at"],
                            "best_score": c["best_score"], "insns": c["n_insns"]} for c in claims],
            })
        leaders = self.store.all(
            "SELECT a.name, a.owner, COUNT(f.id) AS matches, COALESCE(SUM(f.n_insns),0) AS insns, a.last_seen "
            "FROM agents a LEFT JOIN functions f ON f.matched_agent_id=a.id "
            "GROUP BY a.id ORDER BY insns DESC, matches DESC LIMIT 25")
        events = self.store.all(
            "SELECT e.kind, e.detail, e.created_at, a.name AS agent, p.slug FROM events e "
            "LEFT JOIN agents a ON a.id=e.agent_id LEFT JOIN projects p ON p.id=e.project_id "
            "ORDER BY e.id DESC LIMIT 30")
        return {
            "projects": projects,
            "leaderboard": [dict(r) for r in leaders],
            "events": [dict(r) for r in events],
        }

    def status(self, agent: Agent | None = None) -> str:
        s = self.status_json()
        out = []
        for p in s["projects"]:
            pct = 100 * p["matched_bytes"] / (p["bytes"] or 1)
            out.append(f"{p['slug']}: {p['matched']}/{p['functions']} functions, {pct:.1f}% of code matched, "
                       f"context.h v{p['context_version']}, {len(p['claims'])} agents working")
            out += [f"  - {c['agent']} on {c['function']} ({c['insns']} insns, best {c['best_score']})"
                    for c in p["claims"]]
        out.append("Leaderboard (instructions matched):")
        out += [f"  {i + 1}. {r['name']}{' (you)' if agent and r['name'] == agent.name else ''}: "
                f"{r['matches']} functions / {r['insns']} insns" for i, r in enumerate(s["leaderboard"][:10])]
        return "\n".join(out)

    # ------------------------------------------------------- reference sources (P10.1, 18.6)
    # The versioned retrieval registry, persisted as reference_sources rows: each project admits its
    # own reference corpus (project_id), and project_id NULL holds the operator's shared corpus.

    def save_reference_sources(self, registry: retrieval.Registry, project_id: int | None = None) -> None:
        """Persist a retrieval.Registry as reference_sources rows (plan Section 18.6): the reference
        corpus every retrieval index is admitted against. Saving replaces the rows of that scope
        (project or shared corpus) wholesale; the registry identity (name/schema/revision) is kept in
        settings. Additive schema only — existing databases migrate in place."""
        with self.store.tx() as db:
            db.execute("DELETE FROM reference_sources WHERE project_id IS ?", (project_id,))
            now = time.time()
            for entry in registry:
                db.execute(
                    "INSERT INTO reference_sources(entry_id, project_id, repository, revision, source_location, "
                    "toolchain, profile, target_digest, processor, mode, provenance, usage_restrictions, "
                    "source_available, source_digest, holdout, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (entry.entry_id, project_id, entry.repository, entry.version, entry.source_location,
                     entry.toolchain, entry.profile, entry.target_identity, entry.processor, entry.mode,
                     entry.provenance, json.dumps(list(entry.usage_restrictions)),
                     int(entry.source_available), entry.source_digest, int(entry.holdout), now))
            db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                       ("retrieval_registry_meta" if project_id is None else
                        f"retrieval_registry_meta:{project_id}",
                        json.dumps({"name": registry.name, "schema": registry.schema,
                                    "revision": registry.revision})))

    def load_reference_sources(self, project_id: int | None = None) -> retrieval.Registry:
        """Rebuild the retrieval.Registry from reference_sources rows. With a project id, that project's
        admitted corpus; with None, every row (shared and per-project) for operator inspection."""
        if project_id is None:
            rows = self.store.all("SELECT * FROM reference_sources ORDER BY project_id IS NOT NULL, entry_id")
        else:
            rows = self.store.all("SELECT * FROM reference_sources WHERE project_id IS ? ORDER BY entry_id",
                                  project_id)
        meta = self.store.one("SELECT value FROM settings WHERE key=?",
                              "retrieval_registry_meta" if project_id is None else
                              f"retrieval_registry_meta:{project_id}")
        meta = json.loads(meta["value"]) if meta else {}
        entries = [retrieval.ReferenceEntry(
            entry_id=r["entry_id"], repository=r["repository"], version=r["revision"],
            source_location=r["source_location"], toolchain=r["toolchain"], profile=r["profile"],
            target_identity=r["target_digest"], processor=r["processor"], mode=r["mode"],
            provenance=r["provenance"], usage_restrictions=tuple(json.loads(r["usage_restrictions"] or "[]")),
            source_available=bool(r["source_available"]), source_digest=r["source_digest"],
            holdout=bool(r["holdout"])) for r in rows]
        registry = retrieval.Registry.from_json(json.dumps({
            "schema": int(meta.get("schema", retrieval.REGISTRY_SCHEMA)),
            "name": str(meta.get("name", "")), "revision": int(meta.get("revision", 0)),
            "entries": [e.to_dict() for e in entries]}))
        return registry

    # ------------------------------------------------------------- moderation
    # Operator-only actions (CLI), for takedown requests and GDPR erasure requests.

    def remove_project(self, slug: str) -> int:
        proj = self._project(slug)
        with self.store.tx() as db:
            ids = [r["id"] for r in db.execute("SELECT id FROM functions WHERE project_id=?", (proj["id"],))]
            for table in ("claims", "attempts", "attempt_snapshots", "verification_receipts", "candidates",
                        "function_walls", "merge_proposals"):
                db.executemany(f"DELETE FROM {table} WHERE function_id=?", [(i,) for i in ids])
            for table in ("notes", "events", "context_versions", "functions", "symbols", "twin_misses"):
                db.execute(f"DELETE FROM {table} WHERE project_id=?", (proj["id"],))
            db.execute("DELETE FROM reference_sources WHERE project_id=?", (proj["id"],))
            db.execute("DELETE FROM search_budgets WHERE function_id NOT IN (SELECT id FROM functions)")
            db.execute("DELETE FROM projects WHERE id=?", (proj["id"],))
        return len(ids)

    def reset_function(self, slug: str, name: str) -> None:
        """Remove every submission for one function (matched source, attempts, notes) and reopen it."""
        fn = self._function(name, slug)
        with self.store.tx() as db:
            for table in ("claims", "attempts", "notes"):
                db.execute(f"DELETE FROM {table} WHERE function_id=?", (fn["id"],))
            for table in ("attempt_snapshots", "verification_receipts", "candidates", "function_walls", "merge_proposals"):
                db.execute(f"DELETE FROM {table} WHERE function_id=?", (fn["id"],))
            db.execute("DELETE FROM search_budgets WHERE function_id=?", (fn["id"],))
            db.execute("UPDATE functions SET status='open', best_score=NULL, best_source=NULL, best_agent_id=NULL, "
                       "matched_source=NULL, matched_agent_id=NULL, matched_at=NULL, attempts=0, abandons=0, "
                       "revision=revision+1 WHERE id=?", (fn["id"],))

    def revoke_agent(self, name: str, erase: bool = False) -> None:
        """Disable an agent's token. With erase=True also strip its identity (GDPR erasure):
        the agent is renamed to an anonymous id and the operator name is removed."""
        row = self.store.one("SELECT id FROM agents WHERE name=?", name)
        if row is None:
            raise NetworkError(f"no agent named {name!r}")
        with self.store.tx() as db:
            db.execute("DELETE FROM claims WHERE agent_id=?", (row["id"],))
            db.execute("UPDATE agents SET token_hash=? WHERE id=?", ("revoked:" + secrets.token_hex(16), row["id"]))
            if erase:
                db.execute("UPDATE agents SET name=?, owner=NULL WHERE id=?", (f"erased-{row['id']}", row["id"]))

    def list_notes(self, slug: str) -> list[dict]:
        proj = self._project(slug)
        return [dict(r) for r in self.store.all(
            "SELECT n.id, f.name AS function, a.name AS agent, n.text FROM notes n LEFT JOIN functions f "
            "ON f.id=n.function_id LEFT JOIN agents a ON a.id=n.agent_id WHERE n.project_id=? ORDER BY n.id",
            proj["id"])]

    def learnings(self, slug: str, budget: int = 2500) -> str:
        """The project-wide notes agents left (newest first, within `budget` characters), for every new session:
        what one agent works out about the compiler or the project, the next one starts with."""
        proj = self._project(slug)
        out, used = [], 0
        for r in self.store.all("SELECT text FROM notes WHERE project_id=? AND function_id IS NULL ORDER BY id DESC LIMIT 40",
                                proj["id"]):
            text = " ".join(r["text"].split())[:600]
            if used + len(text) > budget:
                break
            out.append(f"- {text}")
            used += len(text)
        return "\n".join(out)

    def delete_note(self, note_id: int) -> None:
        with self.store.tx() as db:
            if db.execute("DELETE FROM notes WHERE id=?", (note_id,)).rowcount == 0:
                raise NetworkError(f"no note with id {note_id}")

    # ----------------------------------------------------------------- export

    def export(self, project: str, out_dir: Path) -> int:
        """Write matched sources + the current context as a tree you can PR upstream."""
        proj = self._project(project)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "context.h").write_text(self._context(proj["id"])["header"], encoding="utf-8")
        for unit in sorted(self._unit_contexts(proj["id"])):
            (out_dir / "contexts").mkdir(exist_ok=True)
            (out_dir / "contexts" / f"{unit}.h").write_text(self._context(proj["id"], unit)["header"], encoding="utf-8")
        rows = self.store.all(
            "SELECT f.name, f.matched_source, a.name AS agent, a.owner FROM functions f "
            "LEFT JOIN agents a ON a.id=f.matched_agent_id WHERE f.project_id=? AND f.status='matched' "
            "ORDER BY f.name", proj["id"])
        (out_dir / "src").mkdir(exist_ok=True)
        credits = []
        for r in rows:
            (out_dir / "src" / f"{r['name']}.c").write_text(
                f'#include "context.h"\n\n// matched by {r["agent"]}' + (f" (operator: {r['owner']})" if r["owner"] else "")
                + f"\n{r['matched_source'].strip()}\n", encoding="utf-8")
            credits.append(f"{r['name']}\t{r['agent']}\t{r['owner'] or ''}")
        (out_dir / "CREDITS.tsv").write_text("function\tagent\toperator\n" + "\n".join(credits) + "\n",
                                             encoding="utf-8")
        return len(rows)
