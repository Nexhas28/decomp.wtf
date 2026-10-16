"""The permuter, in the background: the decomp community's way through the last few percent.

A function at 95% usually differs in register allocation or instruction order, which comes down to how the C
is written, not what it does: the order of statements and declarations, a temporary, a cast, `x += 1` for
`x++`, `register`. decomp-permuter tries thousands of such rewrites and keeps what scores better; agents doing
it by hand spend turns (and tokens) on it, and stall. Here it runs on its own, on spare CPU, on the near misses
agents left (or autopilot's drafts reached): it holds a function for a minute at a time, so an agent that
wants it gets it next, records every improvement as the function's best attempt (agents start from it), and
a match as a match. Rewrites keep the meaning of the code (see permuter.py); what matches is still judged
byte for byte, like everything else.

The guided search (PR-18, plan P5.4-P5.6) is on by default: the structured mismatch counts of the best attempt
route the operator weights, a bounded beam of structurally different candidates replaces the single trajectory,
evaluations are reused by source and object digest, and runs checkpoint for the next turn. With
DECOMPNET_GUIDED_SEARCH=0 the permuter runs the single-trajectory policy instead (see search.policy.active_policy).

The permuter is the default finisher. When an agent's compile lands a near miss (under HANDOFF of the function's
score, the same number of instructions, only register/stack/order differences), the network hands the function
here (`enqueue`) with that attempt as the seed and tells the agent to release it and move on: hand-shuffling
registers is what the permuter is for, and agents spent thousands of compiles on it. Queued hand-offs come before
the permuter's own scan of near misses, and as many run at once as the scheduler's permuter pool allows (about a
quarter of the cores). While the permuter has a function (queued or being permuted) the network keeps it out of
agents' strategy claims and says so in its brief.

A turn that compiled nothing because the infrastructure failed (no toolchain, an evaluation that could not
run) is refunded: a missing toolchain must not consume a function's search budget. The budget row counts
turns that compiled, turns that improved, and infrastructure failures (plan P1.2).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .. import scheduler, scoring, snapshots
from ..network import NetworkError
from ..permuter import digest
from ..search import SearchResumeError, EvalOutcome, checkpoint_run, new_run, resume_run, step
from ..search.policy import active_policy

log = logging.getLogger("decomp.wtf")
NEAR = 0.06  # a near miss: at most this much of the function differs (best_score <= 6% of n_insns * 100)
HANDOFF = 0.05  # an agent's attempt under this much of the score (same insn count, r/o/s diffs only) is handed here
MAX_INSNS = 400  # the permuter has no business with giants (perf-test run 2)
SECONDS = 60  # per function per turn
MAX_TURNS = 4  # turns per function before it's left to agents
CHECKPOINT_LIMIT = 64  # bounded resume checkpoints, in memory and in the search_runs table (P5.5 step 6)
NEEDS_CONTEXT = "needs-context"  # search_budgets.reset_reason: mutation will not help, context work comes first
CALL_REFERENCE_REASON = "call_reference mismatches dominate"  # routing.route()'s reason for call/global references


def _toolchain_missing(result) -> bool:
    """A failed compile that says the toolchain itself is missing (compiler.py's 'compiler not found',
    toolchains.ToolchainError): infrastructure, not a verdict on the candidate (plan P1.2 item 3)."""
    log_text = str(getattr(result, "log", "") or "")
    return (log_text.startswith("compiler not found") or "ToolchainError" in log_text
            or "toolchain not installed" in log_text)


def needs_context(cp: dict) -> bool:
    """Whether a run checkpoint's recorded routing decision (search.routing.RoutingDecision) says the
    mismatch is call/global-reference ('prototype/context investigation first') or literal/data with no
    structured evidence: those need context work, not mutation (plan P5.4)."""
    routing = cp.get("routing") if isinstance(cp, dict) else None
    if not isinstance(routing, dict) or routing.get("routing") == "uniform":  # uniform: no routing decision
        return False
    if str(routing.get("reason", "")).startswith(CALL_REFERENCE_REASON):
        return True
    counts = routing.get("counts")
    return (routing.get("coverage") == "complete" and isinstance(counts, dict)
            and not any(isinstance(v, (int, float)) and v > 0 for v in counts.values()))


@dataclass
class State:
    state: str = "running"
    tried: int = 0  # functions permuted
    variants: int = 0
    improved: int = 0
    matched: int = 0
    current: str = ""
    error: str = ""  # why it couldn't run (state "error"), e.g. the fleet lockdown refused its identity
    refused: str = ""  # why the last match it found was not accepted
    started_at: float = field(default_factory=time.time)
    stop: threading.Event = field(default_factory=threading.Event)


@dataclass
class Handoff:
    """A near miss an agent's compile handed to the permuter: the attempt is the seed of the next run."""
    function_id: int
    project_id: int
    name: str
    source: str
    score: int
    agent: str = ""
    at: float = field(default_factory=time.time)


@dataclass
class TurnStats:
    """What one search turn did, for the budget row: whether any candidate compiled, whether any improved
    on the best, and how many infrastructure failures (evaluation could not run, exceptions) it hit."""
    compiled: bool = False
    improved: bool = False
    infra: int = 0
    refused: str = ""  # a match was found but _record refused it: the turn ends, the function stays open


class Permuter:
    def __init__(self, net):
        self.net = net
        self.jobs: dict[str, State] = {}
        self.checkpoints: OrderedDict[int, dict] = OrderedDict()  # function id -> run checkpoint (P5.5)
        self.queue: OrderedDict[int, Handoff] = OrderedDict()  # function id -> near miss handed over, oldest first
        self.active: dict[int, tuple[str, int]] = {}  # function id -> (name, project id) while a worker permutes it
        self._lock = threading.Lock()  # queue, active and the in-memory checkpoints (workers run in parallel)
        self._wake = threading.Event()  # a hand-off wakes the loop out of its idle wait

    def status(self, slug: str) -> dict | None:
        st = self.jobs.get(slug)
        if st is None:
            return None
        out = {k: v for k, v in st.__dict__.items() if k != "stop"}
        with self._lock:
            out["queued"] = len(self.queue)
            out["workers"] = self.workers()
        return out

    # ------------------------------------------------------------ hand-offs

    @staticmethod
    def workers() -> int:
        """How many functions permute at once: the scheduler's permuter pool (about a quarter of the cores)."""
        return max(1, scheduler.BUDGET.limits.get("permuter", 1))

    def enqueue(self, fn, source: str, score: int, agent: str = "") -> Handoff:
        """Hand a near miss over: the next run on the function starts from `source` (when it is at least as good
        as the recorded best). A function already queued keeps its place with the newer seed. The loop wakes."""
        hand = Handoff(fn["id"], fn["project_id"], fn["name"], source, score, agent)
        with self._lock:
            self.queue[fn["id"]] = hand  # a function already in line keeps its place, with the newest seed
        self._wake.set()
        return hand

    def dequeue(self, function_id: int) -> None:
        with self._lock:
            self.queue.pop(function_id, None)

    def holds(self, function_id: int) -> str:
        """'permuting' while a worker has the function, 'queued' while it waits its turn, '' otherwise."""
        with self._lock:
            if function_id in self.active:
                return "permuting"
            return "queued" if function_id in self.queue else ""

    def held(self, project_id: int) -> set[int]:
        """The project's functions the permuter has (queued or being permuted): the network keeps these out
        of agents' strategy claims while a permuter is running."""
        with self._lock:
            return ({h.function_id for h in self.queue.values() if h.project_id == project_id}
                    | {fid for fid, (_, pid) in self.active.items() if pid == project_id})

    def running(self, slug: str) -> bool:
        st = self.jobs.get(slug)
        return st is not None and st.state == "running"

    def queued(self, proj) -> list:
        """The queued hand-offs for a project that are ready to permute: still open and not claimed by anyone
        (an agent that has not released yet keeps its place in line). A function that matched meanwhile, or
        whose search budget is spent, leaves the queue."""
        with self._lock:
            ids = [fid for fid, h in self.queue.items() if h.project_id == proj["id"]]
        if not ids:
            return []
        out, gone = [], []
        now = time.time()
        policy = active_policy()
        for fid in ids:
            fn = self.net.store.one("SELECT * FROM functions WHERE id=?", fid)
            if fn is None or fn["status"] != "open" or not fn["n_insns"] or fn["n_insns"] > MAX_INSNS:
                gone.append(fid)
                continue
            budget = self.net.store.one("SELECT turns_used, max_turns, reset_reason, policy_version FROM search_budgets "
                                        "WHERE function_id=?", fid)
            if budget is not None and budget["policy_version"] == policy.version and (
                    budget["turns_used"] >= budget["max_turns"] or budget["reset_reason"] == NEEDS_CONTEXT):
                gone.append(fid)
                continue
            if self.net.store.one("SELECT 1 FROM claims WHERE function_id=? AND expires_at > ?", fid, now):
                continue  # the agent still holds it: it stays in line until released
            out.append(fn)
        for fid in gone:
            self.dequeue(fid)
        return out

    def start(self, slug: str) -> State | None:
        """Run on the project's near misses until there are none (looking again every few minutes while the
        app runs: agents make new ones)."""
        if (st := self.jobs.get(slug)) is not None and st.state == "running":
            return st
        # A room member's copy permutes only what its own agents hand over (see round): the host's copy does the
        # project-wide scan, and a member's match is sent to the host to confirm, like any of its agents' matches.
        st = self.jobs[slug] = State()
        threading.Thread(target=self._loop, args=(slug, st), daemon=True, name=f"permuter-{slug}").start()
        return st

    def stop(self, slug: str) -> None:
        if (st := self.jobs.get(slug)) is not None:
            st.stop.set()

    def candidates(self, proj, limit: int = 20) -> list:
        """The near-misses with search budget left, closest first. Every material eligibility condition
        (open, unclaimed, has a scored attempt, not too big, budget not exhausted *under the current
        search policy*) is evaluated in SQL before LIMIT: functions whose budget is spent cannot hide
        eligible work behind them the way the reviewed LIMIT-then-Python-filter query did. A budget
        row stamped with an older policy version no longer blocks: a new search policy is a new epoch
        and gets a fresh bounded budget (PR-18). The ordering (score ratio, then id) is stable, so
        repeated calls advance through the same window instead of re-reading its first page."""
        return self.net.store.all(
            "SELECT f.* FROM functions f WHERE f.project_id=? AND f.status='open' "
            "AND f.best_source IS NOT NULL AND f.best_score > 0 AND f.best_score <= f.n_insns * 100 * ? "
            "AND f.n_insns > 0 AND f.n_insns <= 400 "
            "AND f.id NOT IN (SELECT function_id FROM claims WHERE expires_at > ?) "
            "AND NOT EXISTS (SELECT 1 FROM search_budgets b WHERE b.function_id=f.id "
            "AND b.turns_used >= b.max_turns AND b.policy_version = ?) "
            "AND NOT EXISTS (SELECT 1 FROM search_budgets b WHERE b.function_id=f.id "
            "AND b.reset_reason = ? AND b.policy_version = ?) "
            "ORDER BY CAST(f.best_score AS REAL) / (f.n_insns * 100) ASC, f.id ASC LIMIT ?",
            proj["id"], NEAR, time.time(), active_policy().version, NEEDS_CONTEXT, active_policy().version, limit)

    def _turns_used(self, fn) -> int:
        """The function's persisted permuter search-budget state (PR-04 adds reset policy and
        charge-after-claim semantics on the same table)."""
        row = self.net.store.one("SELECT turns_used FROM search_budgets WHERE function_id=?", fn["id"])
        return row["turns_used"] if row else 0

    def _charge_turn(self, fn) -> int:
        """Record one search turn against the function's persisted budget; returns the new count. The
        row is stamped with the current search-policy epoch and version (PR-18 / P5.8: a policy change
        is a new search epoch)."""
        now = time.time()
        policy = active_policy()
        with self.net.store.tx() as db:
            db.execute(
                "INSERT INTO search_budgets(function_id, turns_used, max_turns, last_at, epoch, policy_version) "
                "VALUES(?,1,?,?,?,?) ON CONFLICT(function_id) DO UPDATE SET turns_used=search_budgets.turns_used+1, "
                "last_at=excluded.last_at, epoch=excluded.epoch, policy_version=excluded.policy_version",
                (fn["id"], MAX_TURNS, now, policy.epoch, policy.version))
            return db.execute("SELECT turns_used FROM search_budgets WHERE function_id=?", (fn["id"],)).fetchone()[0]

    def _finish_turn(self, fn, stats: TurnStats) -> None:
        """Settle the turn's budget row in one transaction: count turns that compiled and improved; a turn
        that compiled nothing while the infrastructure failed is refunded and counted as an infra failure
        (a missing toolchain must not consume search budget)."""
        refund = not stats.compiled and stats.infra > 0
        with self.net.store.tx() as db:
            db.execute("UPDATE search_budgets SET compiled_turns=compiled_turns+?, improved_turns=improved_turns+? "
                       "WHERE function_id=?", (int(stats.compiled), int(stats.improved), fn["id"]))
            if refund:
                db.execute("UPDATE search_budgets SET turns_used=MAX(turns_used-1,0), "
                           "infra_failures=infra_failures+1 WHERE function_id=?", (fn["id"],))

    def _policy_reset(self, fn) -> None:
        """A budget row stamped with an older search-policy version gets one fresh budget under the
        current policy (epoch = search-policy version). Rows at the current version are untouched, so
        reset reasons recorded by the network ('better-source', 'context-changed') stay authoritative."""
        now = time.time()
        policy = active_policy()
        with self.net.store.tx() as db:
            db.execute("UPDATE search_budgets SET turns_used=0, epoch=?, policy_version=?, "
                       "reset_reason='policy-changed', last_at=? WHERE function_id=? AND policy_version < ?",
                       (policy.epoch, policy.version, now, fn["id"], policy.version))

    def _loop(self, slug: str, st: State) -> None:
        try:
            agent, _ = self.net.local_agent("permuter")
        except NetworkError as e:  # the fleet lockdown refuses the identity: end with the reason, not silently
            st.state, st.error, st.current = "error", str(e), ""
            log.warning("permuter on %s: %s", slug, e)
            return
        idle = 0
        try:
            while not st.stop.is_set():
                try:
                    proj = self.net.project_row(slug)
                except NetworkError:
                    return  # the project is gone
                self._wake.clear()
                todo = self.round(proj)
                if not todo:
                    idle += 1
                    if idle > 20:  # nothing for an hour: done until agents start again
                        return
                    self._wait(st, 180)
                    continue
                idle = 0
                self._run_round(agent, proj, todo, st)
        finally:
            st.state = "stopped" if st.stop.is_set() else "done"
            st.current = ""

    def round(self, proj) -> list:
        """What this round permutes: queued hand-offs first (oldest first), then the permuter's own scan of the
        project's near misses, without repeats."""
        todo = self.queued(proj)
        if self.member(proj["slug"]):
            return todo  # a room member leaves the scan of the whole project to the host's copy
        seen = {fn["id"] for fn in todo}
        todo += [fn for fn in self.candidates(proj) if fn["id"] not in seen]
        return todo

    def member(self, slug: str) -> bool:
        """Whether this copy joined the project from a friend's room (the host's copy is the authority)."""
        return slug in (getattr(self.net, "links", None) or {})

    def _share(self, agent, proj, fn, source: str, score: int, matched: bool) -> None:
        """A room member's result goes to the host like an agent's does: a match for the host to confirm on its
        copy (the host records it; it comes back through the room), a better attempt as a hint."""
        link = (getattr(self.net, "links", None) or {}).get(proj["slug"])
        if link is None:
            return
        try:
            if matched:
                verdict = link.attempt(agent, fn["name"], source)
                if not verdict.get("matched"):
                    log.info("permuter: the room's host did not confirm %s (score %s)", fn["name"], verdict.get("score"))
            elif (report := getattr(self.net, "_report", None)) is not None:
                report(link, agent, fn["name"], source, score)
        except Exception as e:  # sharing is best-effort: the local record stands either way
            log.info("permuter: sharing %s with the room failed: %s", fn["name"], e)

    def _wait(self, st: State, seconds: float) -> None:
        """Idle until something changes: a stop, a hand-off, or the time."""
        deadline = time.monotonic() + seconds
        while not st.stop.is_set() and not self._wake.is_set() and time.monotonic() < deadline:
            st.stop.wait(min(1.0, max(0.0, deadline - time.monotonic())))

    def _run_round(self, agent, proj, todo: list, st: State) -> None:
        """Permute the round's functions, as many at once as the permuter pool allows. Each turn holds one unit
        of the pool; the lease (_hold) is what keeps two workers off one function."""
        workers = self.workers()

        def one(fn):
            if st.stop.is_set():
                return
            try:
                with scheduler.BUDGET.slot("permuter"):
                    self._permute(agent, proj, fn, st)
            except Exception as e:  # one odd function must not stop the rest
                log.info("permuter: %s: %s", fn["name"], e)

        if workers <= 1:
            for fn in todo:
                if st.stop.is_set():
                    break
                one(fn)
            return
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="permuter-worker") as pool:
            for fn in todo:
                if st.stop.is_set():
                    break
                pool.submit(one, fn)

    def _hold(self, agent, fn) -> bool:
        now = time.time()
        with self.net.store.tx() as db:
            self.net._expire_claims(db)
            # INSERT OR IGNORE: the lease is granted atomically — of two workers racing on one
            # function, exactly one wins, even across processes sharing the database file.
            cur = db.execute("INSERT OR IGNORE INTO claims VALUES(?,?,?,?)",
                             (fn["id"], agent.id, now, now + SECONDS + 120))
            return cur.rowcount == 1

    def _let_go(self, agent, fn) -> None:
        with self.net.store.tx() as db:
            db.execute("DELETE FROM claims WHERE function_id=? AND agent_id=?", (fn["id"], agent.id))

    def _snapshot_key(self, proj, fn, fork: str | None) -> str:
        """The identity the evaluation caches are valid under: context, target, compiler and scorer.
        Fork rooms score remotely, so their key is the fork identity (results are coordinator-judged)."""
        if fork:
            return f"fork:{proj['id']}:{fork}"
        ctx = self.net._context(proj["id"], fn["unit"])
        if ctx is None:
            return f"local:{proj['id']}:{fn['id']}:nocontext"
        return (f"local:{snapshots.context_digest(ctx)}:{snapshots.target_digest(fn)}:"
                f"{snapshots.compiler_digest(proj)}:scorer{snapshots.SCORER_VERSION}")

    def _probe(self, proj, fn, fork: str | None) -> tuple[dict | None, str, EvalOutcome | None]:
        """One compile of the best attempt, for structured mismatch counts (P5.4 routing evidence) and
        a warm dedup entry. Never records an attempt: it is routing input, not a candidate."""
        if fork:
            return None, "unknown", None
        ctx = self.net._context(proj["id"], fn["unit"])
        result = self.net._compile(proj, ctx["header"], fn["best_source"], fn)
        if not result.ok:
            return None, "unknown", EvalOutcome(score=None, kind="compile_error")
        cand, verdict, _ = self.net._evaluate(fn, proj, result.obj)
        if cand is None:
            return None, "unknown", EvalOutcome(score=None, kind="evaluate_failure")
        comp = scoring.score_only(self.net._target_obj(fn, proj), cand)
        score = 0 if comp.bytes_match else max(1, comp.score)
        if verdict is not None:
            score = 0 if verdict else max(1, score)
        return (dict(comp.counts), "complete",
                EvalOutcome(score=score, bytes_match=comp.bytes_match, counts=dict(comp.counts),
                            object_digest=comp.candidate.normalised_fingerprint, kind="scored"))

    def _make_evaluator(self, agent, proj, fn, fork: str | None, run, stats: TurnStats | None = None):
        """Evaluate one candidate under the current snapshot; records a genuine improvement exactly
        as the pre-PR-18 permuter did (via='permuter', never on cache hits), with the snapshot captured
        before compilation so the acceptance path can re-check it. `stats` collects what the turn did."""
        stats = TurnStats() if stats is None else stats

        def evaluate(source: str) -> EvalOutcome:
            if fork:
                res = self.net.forks.attempt(agent, fork, fn["name"], source, keep=False,
                                             quiet=run.portfolio.best.score)
                score = res.get("score")
                if score is None:
                    stats.infra += 1
                    return EvalOutcome(score=None, kind="evaluate_failure")
                stats.compiled = True
                if score == 0 and not res.get("matched"):  # identical, but the match was refused
                    stats.refused = res.get("text") or "the match was refused"
                    return EvalOutcome(score=None, kind="evaluate_failure")
                if score < run.portfolio.best.score:
                    stats.improved = True
                return EvalOutcome(score=score, kind="fork")
            ctx = self.net._context(proj["id"], fn["unit"])
            snap = snapshots.capture(fn, proj, ctx)
            result = self.net._compile(proj, ctx["header"], source, fn)
            if not result.ok:
                if _toolchain_missing(result):  # not a verdict on the candidate: the turn is refunded
                    stats.infra += 1
                    return EvalOutcome(score=None, kind="evaluate_failure")
                return EvalOutcome(score=None, kind="compile_error")
            stats.compiled = True
            cand, verdict, _ = self.net._evaluate(fn, proj, result.obj)
            if cand is None:
                stats.infra += 1
                return EvalOutcome(score=None, kind="evaluate_failure")
            comp = scoring.score_only(self.net._target_obj(fn, proj), cand)
            score = 0 if comp.bytes_match else max(1, comp.score)
            if verdict is not None:
                score = 0 if verdict else max(1, score)
            if score < run.portfolio.best.score:
                refused = self.net._record(fn, agent, score, source, ctx["version"], matched=comp.bytes_match,
                                           via="permuter", snapshot=snap)
                if comp.bytes_match and refused:  # not a match: never reported or counted as one
                    stats.refused = refused
                    return EvalOutcome(score=None, kind="evaluate_failure")
                stats.improved = True
                if self.member(proj["slug"]):
                    self._share(agent, proj, fn, source, score, comp.bytes_match)
            return EvalOutcome(score=score, bytes_match=comp.bytes_match, counts=dict(comp.counts),
                               object_digest=comp.candidate.normalised_fingerprint, kind="scored")
        return evaluate

    def _seed_source(self, fn) -> tuple[str, int]:
        """What a fresh run starts from: the handed-over attempt when the function was handed to the permuter
        and that attempt is at least as good as the recorded best (it usually is the best: _record keeps the
        better score), otherwise the function's best attempt."""
        with self._lock:
            hand = self.queue.get(fn["id"])
        if hand is not None and hand.source and (fn["best_score"] is None or hand.score <= fn["best_score"]):
            return hand.source, hand.score
        return fn["best_source"], fn["best_score"]

    def _resume_or_start(self, proj, fn, fork, policy, seed, snapshot_key):
        """Resume the previous turn's checkpoint when the snapshot and policy still match; otherwise
        start a fresh deterministic run routed by the best attempt's structured mismatches (the seed is
        the handed-over attempt when there is one, see _seed_source)."""
        cp = self._load_checkpoint(fn["id"])
        if cp is not None:
            try:
                return resume_run(policy, snapshot_key, cp)
            except SearchResumeError:
                self._drop_checkpoint(fn["id"])
        source, score = self._seed_source(fn)
        fn = {**fn, "best_source": source, "best_score": score} if source != fn["best_source"] else fn
        counts, coverage, probe = self._probe(proj, fn, fork)
        run = new_run(policy, seed, snapshot_key, source, score, counts=counts, coverage=coverage)
        if probe is not None:
            run.evals.store(digest(source), probe)
        return run

    def _load_checkpoint(self, fn_id: int) -> dict | None:
        """The function's resumable checkpoint: from memory, else from the search_runs table (a restart
        keeps it). An unreadable row is dropped. Compatibility is judged by resume_run."""
        with self._lock:
            cp = self.checkpoints.get(fn_id)
        if cp is not None:
            return cp
        row = self.net.store.one("SELECT checkpoint FROM search_runs WHERE function_id=?", fn_id)
        if row is None:
            return None
        try:
            cp = json.loads(row["checkpoint"])
        except ValueError:
            cp = None
        if not isinstance(cp, dict):
            self._drop_checkpoint(fn_id)
            return None
        return cp

    def _save_checkpoint(self, fn_id: int, cp: dict) -> None:
        with self._lock:
            if len(self.checkpoints) >= CHECKPOINT_LIMIT and fn_id not in self.checkpoints:
                self.checkpoints.popitem(last=False)
            self.checkpoints[fn_id] = cp
            self.checkpoints.move_to_end(fn_id)
        with self.net.store.tx() as db:
            db.execute(
                "INSERT INTO search_runs(function_id, snapshot_key, policy_epoch, checkpoint, updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(function_id) DO UPDATE SET snapshot_key=excluded.snapshot_key, "
                "policy_epoch=excluded.policy_epoch, checkpoint=excluded.checkpoint, updated_at=excluded.updated_at",
                (fn_id, cp.get("snapshot_key", ""), active_policy().epoch, json.dumps(cp), time.time()))
            db.execute(  # bounded: the newest CHECKPOINT_LIMIT rows stay, oldest updated_at first out
                "DELETE FROM search_runs WHERE function_id NOT IN "
                "(SELECT function_id FROM search_runs ORDER BY updated_at DESC, function_id DESC LIMIT ?)",
                (CHECKPOINT_LIMIT,))
            if needs_context(cp):  # P5.4: context work, not mutation: out of the queue, and the status view says why
                policy = active_policy()
                db.execute(
                    "INSERT INTO search_budgets(function_id, turns_used, max_turns, last_at, epoch, policy_version, "
                    "reset_reason) VALUES(?,0,?,?,?,?,?) ON CONFLICT(function_id) DO UPDATE SET "
                    "reset_reason=excluded.reset_reason WHERE search_budgets.reset_reason IS NULL "
                    "OR search_budgets.reset_reason NOT LIKE 'wall:%'",
                    (fn_id, MAX_TURNS, time.time(), policy.epoch, policy.version, NEEDS_CONTEXT))

    def _drop_checkpoint(self, fn_id: int) -> None:
        """Forget the function's checkpoint (matched, budget spent, or incompatible with the current snapshot)."""
        with self._lock:
            self.checkpoints.pop(fn_id, None)
        with self.net.store.tx() as db:
            db.execute("DELETE FROM search_runs WHERE function_id=?", (fn_id,))

    def _set_current(self, st: State) -> None:
        st.current = ", ".join(sorted(name for name, _ in self.active.values()))  # under self._lock

    def _permute(self, agent, proj, fn, st: State) -> None:
        if not self._hold(agent, fn):
            return
        self._policy_reset(fn)  # a new search-policy epoch gets one fresh bounded budget
        turn = self._charge_turn(fn)  # a failed claim costs nothing: only real search work is charged
        with self._lock:
            self.active[fn["id"]] = (fn["name"], fn["project_id"])
            self._set_current(st)
            st.tried += 1
        fork = self.net.fork_slug(proj["slug"])
        policy = active_policy()
        seed = f"{fn['id']}-{turn}-{policy.epoch}"  # deterministic per (function, turn, policy epoch)
        stats = TurnStats()
        try:
            snapshot_key = self._snapshot_key(proj, fn, fork)
            run = self._resume_or_start(proj, fn, fork, policy, seed, snapshot_key)
            evaluator = self._make_evaluator(agent, proj, fn, fork, run, stats)
            deadline = time.monotonic() + SECONDS
            while (time.monotonic() < deadline and not st.stop.is_set()
                   and run.portfolio.best.score > 0 and run.steps < policy.max_steps):
                res = step(run, evaluator)
                if res is None:
                    break  # step budget spent or already matched
                if stats.refused:  # mutating further only finds the same refused match again
                    st.refused = f"{fn['name']}: {stats.refused}"
                    log.info("permuter: %s match refused: %s", fn["name"], stats.refused)
                    break
                if res.operator is not None:
                    st.variants += 1
                if res.kind == "improving":
                    st.improved += 1
                elif res.kind == "matched":
                    st.improved += 1
                    st.matched += 1
                    log.info("permuter: %s matched", fn["name"])
                    break
            if run.portfolio.best.score > 0 and not stats.refused and self._turns_used(fn) < MAX_TURNS:
                self._save_checkpoint(fn["id"], checkpoint_run(run))  # resume next turn (P5.5 step 7)
            else:
                self._drop_checkpoint(fn["id"])
                self.dequeue(fn["id"])  # matched, refused or out of budget: the hand-off is done with
        except Exception:  # a toolchain error or any other exception is an infrastructure failure
            stats.infra += 1
            raise
        finally:
            try:
                self._finish_turn(fn, stats)
            finally:
                self._let_go(agent, fn)
                with self._lock:
                    self.active.pop(fn["id"], None)
                    self._set_current(st)
