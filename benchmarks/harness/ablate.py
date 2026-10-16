#!/usr/bin/env python3
"""PSX real-toolchain solve-rate ablation (plan Section 21).

Guided permuter search (guided-v1, beam 4) vs a baseline single-trajectory policy
(beam=1), under EQUAL step budgets per function, with the compile cache paired
cold/warm, on a COPY of the production slus-01363-4 database. Real compiles go
through the project's pinned gcc-2.8.1-psx profile; every candidate is evaluated
by the coordinator's own scoring (`scoring.score_only`) against the target stored
in the copied DB. Artifacts follow the plan's Section 21.6 layout and land under
benchmark-results/<run-id>/.

This harness is designed to run INSIDE a Linux container: the PSX toolchains are
Linux i386 ELFs that macOS cannot execute, and the coordinator itself is a Linux
program. Example (run from the repo root):

    docker run --rm --platform linux/arm64 \
      -v "$PWD":/w:ro \
      -v $HOME/Decomp/slus-01363-4:/proj:ro \
      -v $HOME/Decomp/benchmarks:/bench \
      -v "$HOME/.local/share/decomp-wtf/backup/decomp-wtf-home-volume/.local/share/decompnet/toolchains":/tc:ro \
      -e DECOMPNET_TOOLCHAINS=/tc -e PYTHONPATH=/w/src \
      python:3.12-bookworm bash -c "pip install -q capstone pyelftools && \
        python3 /w/benchmarks/harness/ablate.py --db /bench/results/psx-ablation/app.db \
          --project-root /proj --run-id psx-ablation/run-001 \
          --results-root /bench/results --no-resource-limits"

Safety and honesty rules this file lives by:

* It never opens the live database or talks to the production app; it only reads
  the SQLite backup copy made with the backup API (RUNBOOKS 24.0).
* It makes NO database writes for candidate evaluations: each policy/cache variant
  starts from the SAME recorded initial state, so one policy's improvement cannot
  change another policy's starting point. ("matched" here means the search found a
  byte-identical candidate, judged by the same scoring core, on the copy.)
* The coordinator's per-compile resource limits (`prlimit`) stay ON by default. They are
  disabled inside this harness process only when `--no-resource-limits` is passed: the caps
  (`prlimit --as=2147483648`) make qemu-emulated i386 toolchains fail with "Unable to find
  a guest_base" on arm64 Docker hosts, and the harness only compiles project-recorded
  sources on a disposable copy. The choice is recorded in manifest.json, summary.json and
  effective-config.json (`prlimit_disabled`); production compiles keep their caps.
* Deterministic seeds; per-function variant order is shuffled by a seeded RNG to
  reduce warm-host bias; a global timebox marks unfinished work as censored
  instead of omitting it (plan 21.2/21.3).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO / "src"))

from decompnet import cache as ccache  # noqa: E402
from decompnet import compiler  # noqa: E402
from decompnet import scoring, snapshots  # noqa: E402
from decompnet.app.engine import RomNetwork  # noqa: E402
from decompnet.permuter import digest  # noqa: E402
from decompnet.project import compiler_config  # noqa: E402
from decompnet.search import SEARCH_POLICY, EvalOutcome, new_run, step  # noqa: E402
from decompnet.search.policy import SearchPolicy  # noqa: E402

# near-miss bound, as in the coordinator's permuter (app/permute.py)
NEAR = 0.06

ASM_MARKERS = ("GLOBAL_ASM", "INCLUDE_ASM", "INCLUDE_RODATA", "NON_MATCHING", "__asm", "__asm__")

RETRYABLE_INFRA = ("guest_base", "Resource temporarily unavailable", "Cannot allocate memory")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _git_commit() -> str:
    try:
        import subprocess
        return subprocess.run(["git", "-C", str(_REPO), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


_SECRET_HINTS = ("TOKEN", "SECRET", "PASSWORD", "KEY")


def _decompnet_env() -> dict:
    """The DECOMPNET_* environment this run saw; secret-looking values are redacted."""
    return {k: ("<redacted>" if any(h in k for h in _SECRET_HINTS) else v)
            for k, v in sorted(os.environ.items()) if k.startswith("DECOMPNET_")}


def _feature_flags() -> dict:
    """Optional features as the coordinator reads them from the environment (all off unless set)."""
    return {"guided_search": os.environ.get("DECOMPNET_GUIDED_SEARCH", "0") == "1",
            "rea_provider": os.environ.get("DECOMPNET_REA_PROVIDER") == "1",
            "structural_rerank": os.environ.get("DECOMPNET_STRUCTURAL_RERANK") == "1",
            "compile_cache": os.environ.get("DECOMPNET_COMPILE_CACHE") != "0"}


def _provider_status() -> list[dict]:
    """One row per optional provider: available true/false and why. Probes only; never starts one."""
    rows = []

    def add(provider: str, available: bool, reason: str) -> None:
        rows.append({"provider": provider, "available": bool(available), "reason": reason})

    try:
        from decompnet.forks import ghidra
        ok = ghidra.available()
        add("ghidra", ok, "analyzeHeadless and a Java runtime found" if ok
            else "analyzeHeadless and/or a Java runtime not found on this host")
    except Exception as e:
        add("ghidra", False, f"probe failed: {e}")
    try:
        from decompnet.analysis import rea
        ok = rea.available()
        why = "pinned REA install and node found" if ok else "pinned REA install or node not found"
        if os.environ.get("DECOMPNET_REA_PROVIDER") != "1":
            why += "; not enabled (DECOMPNET_REA_PROVIDER is not 1)"
        add("rea", ok, why)
    except Exception as e:
        add("rea", False, f"probe failed: {e}")
    try:
        from decompnet.retrieval.bsim import toolchain_available
        ok, why = toolchain_available()
    except Exception as e:
        ok, why = False, f"probe failed: {e}"
    add("fid", ok, why)
    add("bsim", ok, why)
    return rows


def _observed_receipts(net, since_attempt_id: int) -> list[dict]:
    """Verification receipts written for attempts recorded after `since_attempt_id` (this run's)."""
    rows = net.store.all(
        "SELECT r.*, f.name AS function FROM verification_receipts r "
        "JOIN functions f ON f.id=r.function_id WHERE r.attempt_id > ? ORDER BY r.function_id",
        since_attempt_id)
    return [dict(r) for r in rows]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")


@dataclass
class _Counts:
    compile_calls: int = 0          # compile_source invocations (cache hits included)
    real_compiles: int = 0          # actual compiler runs (cache misses / cache-off calls)
    cache_hits: int = 0
    infra_retries: int = 0          # qemu/binfmt transient failures retried
    compile_ms: float = 0.0
    score_ms: float = 0.0
    score_calls: int = 0


@dataclass
class _Variant:
    policy: SearchPolicy
    cache: str  # "cold" | "warm"


@dataclass
class _FnResult:
    fn_id: int
    name: str
    unit: str
    n_insns: int
    start_score: int
    classification: str = "ran"     # ran | toolchain-wall | censored | error
    reason: str = ""
    variants: list[dict] = field(default_factory=list)


class _Eval:
    """Compile + extract + score with counters and bounded infra retries. Never writes to the DB."""

    def __init__(self, net: RomNetwork, fn, proj, ctx, cc, cc_nocache, counts: _Counts):
        self.net = net
        self.fn = fn
        self.proj = proj
        self.ctx = ctx
        self.cc = cc                # per-variant config (cache on/off)
        self.cc_nocache = cc_nocache  # same profile, cache disabled (for infra retries)
        self.counts = counts

    def __call__(self, source: str) -> EvalOutcome:
        t0 = time.monotonic()
        result = compiler.compile_source(self.cc, self.ctx["header"], source)
        self.counts.compile_calls += 1
        self.counts.compile_ms += (time.monotonic() - t0) * 1000
        if not result.ok and any(marker in result.log for marker in RETRYABLE_INFRA):
            # binfmt/qemu hiccup: bounded retry, bypassing the cache so a spuriously
            # cached negative cannot poison the measurement
            for _ in range(2):
                self.counts.infra_retries += 1
                t0 = time.monotonic()
                result = compiler.compile_source(self.cc_nocache, self.ctx["header"], source)
                self.counts.compile_calls += 1
                self.counts.compile_ms += (time.monotonic() - t0) * 1000
                if result.ok or not any(marker in result.log for marker in RETRYABLE_INFRA):
                    break
        if not result.ok:
            return EvalOutcome(score=None, kind="compile_error")
        t0 = time.monotonic()
        cand, verdict, _ = self.net._evaluate(self.fn, self.proj, result.obj)
        if cand is None:
            return EvalOutcome(score=None, kind="evaluate_failure")
        comp = scoring.score_only(self.net._target_obj(self.fn, self.proj), cand)
        self.counts.score_ms += (time.monotonic() - t0) * 1000
        self.counts.score_calls += 1
        score = 0 if comp.bytes_match else max(1, comp.score)
        if verdict is not None:
            score = 0 if verdict else max(1, score)
        return EvalOutcome(score=score, bytes_match=comp.bytes_match, counts=dict(comp.counts),
                           object_digest=comp.candidate.normalised_fingerprint, kind="scored")


def _probe(net, fn, proj, ctx, cc, cc_nocache, counts: _Counts) -> tuple[dict | None, str, EvalOutcome | None]:
    """One compile+score of the recorded best attempt: routing evidence + a warm dedup entry,
    mirroring the coordinator's Permuter._probe."""
    ev = _Eval(net, fn, proj, ctx, cc, cc_nocache, counts)
    out = ev(fn["best_source"])
    if out.kind in ("compile_error", "evaluate_failure"):
        return None, "unknown", out
    return out.counts, "complete", out


def _snapshot_key(net, proj, fn) -> str:
    ctx = net._context(proj["id"], fn["unit"])
    if ctx is None:
        return f"local:{proj['id']}:{fn['id']}:nocontext"
    return (f"local:{snapshots.context_digest(ctx)}:{snapshots.target_digest(fn)}:"
            f"{snapshots.compiler_digest(proj)}:scorer{snapshots.SCORER_VERSION}")


def sample_functions(net: RomNetwork, proj, limit: int) -> tuple[list, list[dict]]:
    """Deterministic near-miss sample: the permuter's own priority order (score ratio, then id).
    Returns (sample, skipped) where skipped entries carry the exclusion reason."""
    rows = net.store.all(
        "SELECT f.* FROM functions f WHERE f.project_id=? AND f.status='open' "
        "AND f.best_source IS NOT NULL AND f.best_score > 0 AND f.n_insns > 0 AND f.n_insns <= 400 "
        "ORDER BY CAST(f.best_score AS REAL) / (f.n_insns * 100) ASC, f.id ASC",
        proj["id"])
    sample, skipped = [], []
    for fn in rows:
        if any(marker in fn["best_source"] for marker in ASM_MARKERS):
            skipped.append({"function": fn["name"], "id": fn["id"], "reason": "asm-marker-in-best-source"})
            continue
        if fn["best_score"] > fn["n_insns"] * 100 * NEAR:
            skipped.append({"function": fn["name"], "id": fn["id"], "reason": "not-a-near-miss"})
            continue
        if len(sample) < limit:
            sample.append(fn)
    return sample, skipped


def run_variant(net, proj, fn, ctx, variant: _Variant, seed: str, warm_cache_dir: Path,
                max_steps: int, per_run_wall: float,
                timings_fh, attempts_fh, run_meta: dict) -> dict:
    """One policy x cache-mode search over one function. Returns the variant record."""
    t_start = time.monotonic()
    policy = variant.policy
    cc = compiler_config(Path(run_meta["project_root"]), json.loads(proj["config"]))
    cc_nocache = compiler_config(Path(run_meta["project_root"]), json.loads(proj["config"]))
    if variant.cache == "warm":
        cc.cache_dir = str(warm_cache_dir)
        cache = ccache.cache_for_dir(warm_cache_dir)
        stats0 = dict(cache.stats)
    else:
        cc.cache = False
        cc_nocache.cache = False
        cache, stats0 = None, None
    cc_nocache.cache = False  # infra retries always bypass the cache

    snapshot_key = _snapshot_key(net, proj, fn)
    record = {"function": fn["name"], "id": fn["id"], "policy": policy.id, "policy_version": policy.version,
              "beam": policy.beam, "cache": variant.cache, "seed": seed,
              "max_steps": max_steps, "status": "ran", "start_score": fn["best_score"]}
    counts = _Counts()

    # probe: routing evidence from the recorded best attempt (same evidence both policies get)
    probe_counts, probe_coverage, probe_out = _probe(net, fn, proj, ctx, cc, cc_nocache, counts)
    if probe_out is not None and probe_out.kind in ("compile_error", "evaluate_failure"):
        record["status"] = "toolchain-wall"
        record["reason"] = f"best attempt does not compile: {probe_out.kind}"
        record["wall"] = time.monotonic() - t_start
        if variant.cache == "cold":
            counts.real_compiles = counts.compile_calls
        record["counts"] = counts.__dict__.copy()
        _emit(timings_fh, "variant", t_start, fn, variant, None, "toolchain-wall", None, None, None,
              counts, run_meta)
        _emit(attempts_fh, "step", t_start, fn, variant, 0, "probe", probe_out.kind, None, None,
              counts, run_meta)
        return record

    run = new_run(policy, seed, snapshot_key, fn["best_source"], fn["best_score"],
                  counts=probe_counts, coverage=probe_coverage)
    if probe_out is not None:
        run.evals.store(digest(fn["best_source"]), probe_out)

    evaluator = _Eval(net, fn, proj, ctx, cc, cc_nocache, counts)
    censored = None
    while run.steps < max_steps and run.portfolio.best.score > 0 and run.portfolio.members:
        if time.monotonic() - t_start > per_run_wall:
            censored = "per-run-wall"
            break
        res = step(run, evaluator)
        if res is None:
            break
        _emit(attempts_fh, "step", t_start, fn, variant, res.step, res.kind, res.operator,
              res.score, res.score_delta, counts, run_meta)
        if res.kind == "matched":
            break
    if censored is None and run.steps >= max_steps and run.portfolio.best.score > 0:
        censored = "budget-exhausted"

    record.update({
        "status": censored or ("matched" if run.portfolio.best.score == 0 else "finished"),
        "steps": run.steps,
        "final_score": run.portfolio.best.score,
        "best_source_digest": sha256(run.portfolio.best.source.encode("utf-8")),
        "matched": run.portfolio.best.score == 0,
        "improved": run.portfolio.best.score < fn["best_score"],
        "wall_s": time.monotonic() - t_start,
        "censored": censored,
        "stats": dict(run.stats),
        "routing": run.routing.to_dict(),
    })
    if variant.cache == "warm":
        stats1 = dict(cache.stats)
        counts.cache_hits = stats1["hits"] - stats0["hits"]
        counts.real_compiles = stats1["misses"] - stats0["misses"]
        record["cache_stats"] = {"hits": counts.cache_hits, "misses": counts.real_compiles,
                                 "dir": str(warm_cache_dir)}
    else:
        counts.real_compiles = counts.compile_calls
    record["counts"] = {k: getattr(counts, k) for k in
                        ("compile_calls", "real_compiles", "cache_hits", "infra_retries")}
    record["compile_ms"] = round(counts.compile_ms, 1)
    record["score_ms"] = round(counts.score_ms, 1)
    record["score_calls"] = counts.score_calls
    _emit(timings_fh, "variant", t_start, fn, variant, None, record["status"], None, None, None, counts, run_meta)
    return record


def _emit(fh, kind, t0, fn, variant, step, ev_kind, operator, score, delta, counts, run_meta):
    row = {"ts": _now_utc(), "run_id": run_meta["run_id"], "function": fn["name"], "id": fn["id"],
           "policy": variant.policy.id, "beam": variant.policy.beam, "cache": variant.cache,
           "kind": kind, "step": step, "event": ev_kind, "operator": operator, "score": score,
           "score_delta": delta, "t_s": round(time.monotonic() - t0, 4),
           "compile_calls": counts.compile_calls, "real_compiles": counts.real_compiles}
    fh.write(json.dumps(row, sort_keys=True) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="path to the COPIED database (sqlite backup of the live DB)")
    ap.add_argument("--project-slug", default="slus-01363-4")
    ap.add_argument("--project-root", required=True, help="project root path inside this environment")
    ap.add_argument("--run-id", required=True, help="e.g. psx-ablation/run-001; artifacts land under benchmark-results/<run-id>")
    ap.add_argument("--sample", type=int, default=8, help="functions per ablation (default 8)")
    ap.add_argument("--max-steps", type=int, default=100, help="equal compile budget per function per variant")
    ap.add_argument("--timebox", type=float, default=1140.0, help="global wall clock budget in seconds (19 min default)")
    ap.add_argument("--per-run-wall", type=float, default=300.0, help="wall cap per variant")
    ap.add_argument("--results-root", required=True, help="writeable results root, e.g. the host benchmarks/results path mounted into the container")
    ap.add_argument("--no-resource-limits", action="store_true",
                    help="disable the per-compile prlimit caps for this run (default: caps stay on); "
                         "needed for qemu-emulated i386 toolchains on arm64 Docker hosts")
    args = ap.parse_args()

    results_root = Path(args.results_root)
    run_dir = results_root / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    warm_cache_dir = run_dir / "cache-warm"

    run_meta = {
        "run_id": args.run_id, "harness": Path(__file__).name, "repo_commit": _git_commit(),
        "project_slug": args.project_slug, "project_root": args.project_root, "db": args.db,
        "sample": args.sample, "max_steps": args.max_steps, "timebox_s": args.timebox,
        "per_run_wall_s": args.per_run_wall, "python": sys.version.split()[0],
        "platform": platform.platform(), "machine": platform.machine(),
        "capstone": getattr(__import__("capstone"), "__version__", "unknown"),
        "prlimit_disabled": bool(args.no_resource_limits), "started_utc": _now_utc(),
        "policies": {"guided": SEARCH_POLICY.to_dict(),
                     "baseline": SearchPolicy(id="baseline", version=1, beam=1, max_steps=args.max_steps).to_dict()},
    }

    # the coordinator's per-compile prlimit caps stay on unless --no-resource-limits is passed: they
    # break qemu-emulated i386 toolchains on arm64 Docker hosts, and this harness compiles
    # project-recorded sources on a disposable copy.
    if args.no_resource_limits:
        _real_which = shutil.which
        compiler.shutil.which = lambda name: None if name == "prlimit" else _real_which(name)

    net = RomNetwork(args.db, Path(args.project_root))
    proj = net._project(args.project_slug)
    first_attempt = net.store.one("SELECT COALESCE(MAX(id), 0) AS n FROM attempts")["n"]
    row = net.store.one("SELECT COUNT(*) FILTER (WHERE status='matched') AS matched, "
                        "COUNT(*) FILTER (WHERE status='open') AS open FROM functions WHERE project_id=?",
                        proj["id"])
    run_meta["db_state"] = {"matched": row["matched"], "open": row["open"], "project_id": proj["id"]}
    sample, skipped = sample_functions(net, proj, args.sample)

    summary = {"run_id": args.run_id, "functions": [], "skipped": skipped,
               "totals": {"functions": len(sample)}}
    timings_path = run_dir / "stage-timings.jsonl"
    attempts_path = run_dir / "attempts.jsonl"
    deadline = time.monotonic() + args.timebox

    with open(timings_path, "w") as timings_fh, open(attempts_path, "w") as attempts_fh:
        for fn in sample:
            if time.monotonic() > deadline:
                summary["functions"].append({"function": fn["name"], "id": fn["id"],
                                             "classification": "censored", "reason": "global-timebox"})
                continue
            ctx = net._context(proj["id"], fn["unit"])
            if ctx is None:
                summary["functions"].append({"function": fn["name"], "id": fn["id"],
                                             "classification": "error", "reason": "no-context"})
                continue
            fr = _FnResult(fn["id"], fn["name"], fn["unit"], fn["n_insns"], fn["best_score"])
            variants = [_Variant(SEARCH_POLICY, "cold"),
                        _Variant(SEARCH_POLICY, "warm"),
                        _Variant(SearchPolicy(id="baseline", version=1, beam=1, max_steps=args.max_steps), "cold"),
                        _Variant(SearchPolicy(id="baseline", version=1, beam=1, max_steps=args.max_steps), "warm")]
            rng = random.Random(f"order:{fn['id']}:{run_meta['run_id']}")
            rng.shuffle(variants)
            for variant in variants:
                if time.monotonic() > deadline:
                    fr.variants.append({"policy": variant.policy.id, "cache": variant.cache,
                                        "classification": "censored", "reason": "global-timebox"})
                    continue
                seed = f"ablate:{variant.policy.id}-v{variant.policy.version}:{fn['id']}"
                rec = run_variant(net, proj, fn, ctx, variant, seed, warm_cache_dir,
                                  args.max_steps, args.per_run_wall,
                                  timings_fh, attempts_fh, run_meta)
                if rec.get("status") == "toolchain-wall" and not fr.reason:
                    fr.reason = rec.get("reason", "")
                    fr.classification = "toolchain-wall"
                fr.variants.append(rec)
            summary["functions"].append({"function": fr.name, "id": fr.fn_id, "unit": fr.unit,
                                         "n_insns": fr.n_insns, "start_score": fr.start_score,
                                         "classification": fr.classification, "reason": fr.reason,
                                         "variants": fr.variants})

    # aggregate
    solved = {"guided": 0, "baseline": 0}
    improved = {"guided": 0, "baseline": 0}
    compiles = {"guided_cold": 0, "guided_warm": 0, "baseline_cold": 0, "baseline_warm": 0}
    censored = 0
    for f in summary["functions"]:
        if f["classification"] != "ran":
            censored += 1
        for v in f.get("variants", []):
            if v.get("matched"):
                solved[v["policy"]] += 1
            if v.get("improved"):
                improved[v["policy"]] += 1
            if "counts" in v and isinstance(v["counts"], dict):
                compiles[f"{v['policy']}_{v['cache']}"] += v["counts"].get("real_compiles", 0)
    summary["totals"] = {"functions": len(sample), "non_ran": censored, "solved": solved,
                         "improved": improved, "real_compiles": compiles}
    summary["finished_utc"] = _now_utc()
    summary["harness_meta"] = run_meta
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    (run_dir / "manifest.json").write_text(json.dumps(run_meta, indent=2, sort_keys=True), encoding="utf-8")
    corpus = {"sample": [{"function": f["function"], "id": f["id"], "unit": f["unit"],
                          "n_insns": f["n_insns"], "start_score": f["start_score"]}
                         for f in summary["functions"] if f["classification"] == "ran"],
              "skipped": skipped,
              "policies": run_meta["policies"],
              "seeded": "per (function id, policy id-version)"}
    (run_dir / "corpus-manifest.json").write_text(json.dumps(corpus, indent=2, sort_keys=True), encoding="utf-8")
    effective = {"schema_version": 1, "run_id": args.run_id, "harness": run_meta["harness"],
                 "args": vars(args), "prlimit_disabled": run_meta["prlimit_disabled"],
                 "feature_flags": _feature_flags(), "env": _decompnet_env(),
                 "policies": run_meta["policies"]}
    (run_dir / "effective-config.json").write_text(json.dumps(effective, indent=2, sort_keys=True), encoding="utf-8")
    # this harness makes no DB writes, so no match is accepted here; any receipt it did observe is listed
    _write_jsonl(run_dir / "verification-receipts.jsonl", _observed_receipts(net, first_attempt))
    _write_jsonl(run_dir / "provider-status.jsonl", _provider_status())
    _write_report(run_dir, summary)
    print(f"done: {len(sample)} functions -> {run_dir}")
    print(json.dumps(summary["totals"], indent=2))
    return 0


def _write_report(run_dir: Path, summary: dict) -> None:
    lines = [f"# PSX solve-rate ablation — {summary['run_id']}", "",
             f"Repo commit `{summary['harness_meta']['repo_commit']}` · started {summary['harness_meta']['started_utc']} · finished {summary['finished_utc']}",
             "", "## Policies", "```json", json.dumps(summary["harness_meta"]["policies"], indent=2), "```",
             "", "## Per-function results", "",
             "| function | unit | insns | start | guided | baseline | cold/warm compiles (G/B) |",
             "|---|---|---|---|---|---|---|"]
    for f in summary["functions"]:
        g = next((v for v in f["variants"] if v["policy"] == "guided"), {})
        b = next((v for v in f["variants"] if v["policy"] == "baseline"), {})
        cg = sum(v["counts"].get("real_compiles", 0) for v in f["variants"] if v["policy"] == "guided")
        cb = sum(v["counts"].get("real_compiles", 0) for v in f["variants"] if v["policy"] == "baseline")
        lines.append(f"| {f['function']} | {f['unit']} | {f['n_insns']} | {f['start_score']} | "
                     f"{g.get('final_score', '?')}{' ✓' if g.get('matched') else ''} | "
                     f"{b.get('final_score', '?')}{' ✓' if b.get('matched') else ''} | {cg} / {cb} |")
    lines += ["", "## Totals", "```json", json.dumps(summary["totals"], indent=2), "```",
              "", "See summary.json / attempts.jsonl / stage-timings.jsonl for the full records."]
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
