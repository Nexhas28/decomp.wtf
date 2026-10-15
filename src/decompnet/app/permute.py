"""The permuter, in the background: the decomp community's way through the last few percent.

A function at 95% usually differs in register allocation or instruction order, which comes down to how the C
is written, not what it does: the order of statements and declarations, a temporary, a cast, `x += 1` for
`x++`, `register`. decomp-permuter tries thousands of such rewrites and keeps what scores better; agents doing
it by hand spend turns (and tokens) on it, and stall. Here it runs on its own, on spare CPU, on the near misses
agents left (or autopilot's drafts reached): it holds a function for a minute at a time, so an agent that
wants it gets it next, records every improvement as the function's best attempt (agents start from it), and
a match as a match. Rewrites keep the meaning of the code (see permuter.py); what matches is still judged
byte for byte, like everything else.

The search itself is guided (PR-18, plan P5.4-P5.6): the structured mismatch counts of the best attempt
route the operator weights, a bounded beam of structurally different candidates replaces the single
trajectory, evaluations are reused by source and object digest, and runs checkpoint for the next turn.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from .. import scoring, snapshots
from ..network import NetworkError
from ..permuter import digest
from ..search import (SEARCH_POLICY, SearchResumeError, EvalOutcome, checkpoint_run, new_run,
                      resume_run, step)

log = logging.getLogger("decomp.wtf")
NEAR = 0.06  # a near miss: at most this much of the function differs (best_score <= 6% of n_insns * 100)
SECONDS = 60  # per function per turn
MAX_TURNS = 4  # turns per function before it's left to agents
CHECKPOINT_LIMIT = 64  # bounded per-process resume checkpoints (P5.5 step 6)


@dataclass
class State:
    state: str = "running"
    tried: int = 0  # functions permuted
    variants: int = 0
    improved: int = 0
    matched: int = 0
    current: str = ""
    started_at: float = field(default_factory=time.time)
    stop: threading.Event = field(default_factory=threading.Event)


class Permuter:
    def __init__(self, net):
        self.net = net
        self.jobs: dict[str, State] = {}
        self.checkpoints: OrderedDict[int, dict] = OrderedDict()  # function id -> run checkpoint (P5.5)

    def status(self, slug: str) -> dict | None:
        st = self.jobs.get(slug)
        return None if st is None else {k: v for k, v in st.__dict__.items() if k != "stop"}

    def start(self, slug: str) -> State | None:
        """Run on the project's near misses until there are none (looking again every few minutes while the
        app runs: agents make new ones)."""
        if (st := self.jobs.get(slug)) is not None and st.state == "running":
            return st
        if slug in self.net.links:  # a room member: the host's copy permutes, and its matches come back here
            return None
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
            "ORDER BY CAST(f.best_score AS REAL) / (f.n_insns * 100) ASC, f.id ASC LIMIT ?",
            proj["id"], NEAR, time.time(), SEARCH_POLICY.version, limit)

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
        with self.net.store.tx() as db:
            db.execute(
                "INSERT INTO search_budgets(function_id, turns_used, max_turns, last_at, epoch, policy_version) "
                "VALUES(?,1,?,?,?,?) ON CONFLICT(function_id) DO UPDATE SET turns_used=search_budgets.turns_used+1, "
                "last_at=excluded.last_at, epoch=excluded.epoch, policy_version=excluded.policy_version",
                (fn["id"], MAX_TURNS, now, SEARCH_POLICY.epoch, SEARCH_POLICY.version))
            return db.execute("SELECT turns_used FROM search_budgets WHERE function_id=?", (fn["id"],)).fetchone()[0]

    def _policy_reset(self, fn) -> None:
        """A budget row stamped with an older search-policy version gets one fresh budget under the
        current policy (epoch = search-policy version). Rows at the current version are untouched, so
        reset reasons recorded by the network ('better-source', 'context-changed') stay authoritative."""
        now = time.time()
        with self.net.store.tx() as db:
            db.execute("UPDATE search_budgets SET turns_used=0, epoch=?, policy_version=?, "
                       "reset_reason='policy-changed', last_at=? WHERE function_id=? AND policy_version < ?",
                       (SEARCH_POLICY.epoch, SEARCH_POLICY.version, now, fn["id"], SEARCH_POLICY.version))

    def _loop(self, slug: str, st: State) -> None:
        agent, _ = self.net.local_agent("permuter")
        idle = 0
        try:
            while not st.stop.is_set():
                try:
                    proj = self.net.project_row(slug)
                except NetworkError:
                    return  # the project is gone
                todo = self.candidates(proj)
                if not todo:
                    idle += 1
                    if idle > 20:  # nothing for an hour: done until agents start again
                        return
                    st.stop.wait(180)
                    continue
                idle = 0
                for fn in todo:
                    if st.stop.is_set():
                        break
                    try:
                        self._permute(agent, proj, fn, st)
                    except Exception as e:  # one odd function must not stop the rest
                        log.info("permuter: %s: %s", fn["name"], e)
        finally:
            st.state = "stopped" if st.stop.is_set() else "done"
            st.current = ""

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
        result = self.net._compile(proj, ctx["header"], fn["best_source"])
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

    def _make_evaluator(self, agent, proj, fn, fork: str | None, run):
        """Evaluate one candidate under the current snapshot; records a genuine improvement exactly
        as the pre-PR-18 permuter did (via='permuter', never on cache hits)."""
        def evaluate(source: str) -> EvalOutcome:
            if fork:
                res = self.net.forks.attempt(agent, fork, fn["name"], source, keep=False,
                                             quiet=run.portfolio.best.score)
                score = res.get("score")
                return (EvalOutcome(score=score, kind="fork") if score is not None
                        else EvalOutcome(score=None, kind="evaluate_failure"))
            ctx = self.net._context(proj["id"], fn["unit"])
            result = self.net._compile(proj, ctx["header"], source)
            if not result.ok:
                return EvalOutcome(score=None, kind="compile_error")
            cand, verdict, _ = self.net._evaluate(fn, proj, result.obj)
            if cand is None:
                return EvalOutcome(score=None, kind="evaluate_failure")
            comp = scoring.score_only(self.net._target_obj(fn, proj), cand)
            score = 0 if comp.bytes_match else max(1, comp.score)
            if verdict is not None:
                score = 0 if verdict else max(1, score)
            if score < run.portfolio.best.score:
                self.net._record(fn, agent, score, source, ctx["version"], matched=comp.bytes_match,
                                 via="permuter")
            return EvalOutcome(score=score, bytes_match=comp.bytes_match, counts=dict(comp.counts),
                               object_digest=comp.candidate.normalised_fingerprint, kind="scored")
        return evaluate

    def _resume_or_start(self, proj, fn, fork, policy, seed, snapshot_key):
        """Resume the previous turn's checkpoint when the snapshot and policy still match; otherwise
        start a fresh deterministic run routed by the best attempt's structured mismatches."""
        cp = self.checkpoints.get(fn["id"])
        if cp is not None:
            try:
                return resume_run(policy, snapshot_key, cp)
            except SearchResumeError:
                self.checkpoints.pop(fn["id"], None)
        counts, coverage, probe = self._probe(proj, fn, fork)
        run = new_run(policy, seed, snapshot_key, fn["best_source"], fn["best_score"],
                      counts=counts, coverage=coverage)
        if probe is not None:
            run.evals.store(digest(fn["best_source"]), probe)
        return run

    def _save_checkpoint(self, fn_id: int, cp: dict) -> None:
        if len(self.checkpoints) >= CHECKPOINT_LIMIT:
            self.checkpoints.popitem(last=False)
        self.checkpoints[fn_id] = cp
        self.checkpoints.move_to_end(fn_id)

    def _permute(self, agent, proj, fn, st: State) -> None:
        if not self._hold(agent, fn):
            return
        self._policy_reset(fn)  # a new search-policy epoch gets one fresh bounded budget
        turn = self._charge_turn(fn)  # a failed claim costs nothing: only real search work is charged
        st.current, st.tried = fn["name"], st.tried + 1
        fork = self.net.fork_slug(proj["slug"])
        policy = SEARCH_POLICY
        seed = f"{fn['id']}-{turn}-{policy.epoch}"  # deterministic per (function, turn, policy epoch)
        snapshot_key = self._snapshot_key(proj, fn, fork)
        try:
            run = self._resume_or_start(proj, fn, fork, policy, seed, snapshot_key)
            evaluator = self._make_evaluator(agent, proj, fn, fork, run)
            deadline = time.monotonic() + SECONDS
            while (time.monotonic() < deadline and not st.stop.is_set()
                   and run.portfolio.best.score > 0 and run.steps < policy.max_steps):
                res = step(run, evaluator)
                if res is None:
                    break  # step budget spent or already matched
                if res.operator is not None:
                    st.variants += 1
                if res.kind == "improving":
                    st.improved += 1
                elif res.kind == "matched":
                    st.improved += 1
                    st.matched += 1
                    log.info("permuter: %s matched", fn["name"])
                    break
            if run.portfolio.best.score > 0 and self._turns_used(fn) < MAX_TURNS:
                self._save_checkpoint(fn["id"], checkpoint_run(run))  # resume next turn (P5.5 step 7)
            else:
                self.checkpoints.pop(fn["id"], None)
        finally:
            self._let_go(agent, fn)
