"""Immutable dependency identities for attempts (plan Section 18.3, invariants I-04/I-05).

An attempt captures a coherent snapshot *before* compilation; acceptance may only proceed while the
authoritative dependencies still match (the check itself lives in network.py, PR-06). Digests are
content-addressed: only material inputs participate, so a snapshot never guesses.
"""

from __future__ import annotations

import hashlib
import json
import time

SCORER_VERSION = 1        # scoring.py, baseline-compatible-v1
POLICY_VERSION = 1        # rom-function-policy-v1 (Section 18.4)
SNAPSHOT_SCHEMA = 2


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def target_digest(fn) -> str:
    """Target bytes, relocation layout and addressing identity — everything the judge reads from the
    target side of a diff."""
    return sha(json.dumps({
        "target": fn["target"].hex() if fn["target"] else None,
        "relocs": fn["relocs"],
        "vram": fn["vram"], "rom_offset": fn["rom_offset"],
        "fingerprint": fn["fingerprint"] if "fingerprint" in fn.keys() else None,
    }, sort_keys=True).encode())


def context_digest(ctx) -> str:
    """The exact header content compiled against. Version is recorded separately (a version bump with
    identical content is still a new dependency generation); content is identity."""
    return sha(json.dumps({"version": ctx["version"], "unit": ctx["unit"], "header": ctx["header"]},
                          sort_keys=True).encode())


COMPILE_KEYS = ("cmd", "include_dirs", "ro_paths", "sandbox", "image")


def compiler_digest(proj) -> str:
    """Compiler profile identity. Delegates to toolchains.profile_identity (PR-14): toolchain, ordered
    flags, wrapper, language, mode, ABI settings, scope, timeout AND the installed toolchain binary
    manifests, so a flag change, a wrapper change or a reinstalled toolchain between capture and
    acceptance is visible to the acceptance gate (V-05). Falls back to the plain config-dict hash if
    the profile module is unavailable."""
    raw = proj["config"]
    compiler = (json.loads(raw) if isinstance(raw, str) else (raw or {})).get("compiler", {})
    # keys the profile doesn't model but compiler_config compiles with: a changed command, include
    # directory, read-only mount or sandbox changes the output just as a flag does
    extra = {k: compiler.get(k) for k in COMPILE_KEYS if k in compiler} if isinstance(compiler, dict) else {}
    try:
        from . import toolchains

        identity = toolchains.profile_identity(proj)
    except Exception:  # profile identity unavailable: the conservative config-dict hash still catches flag changes
        return sha(json.dumps(compiler, sort_keys=True).encode())
    return identity if not extra else sha(json.dumps([identity, extra], sort_keys=True).encode())


def symbol_table_digest(store, project_id: int) -> str:
    """Symbol-resolution identity: the (address, name) rows the judge and renderer read for a project."""
    rows = store.all("SELECT address, name FROM symbols WHERE project_id=? ORDER BY address, name", project_id)
    return sha(json.dumps([[r["address"], r["name"]] for r in rows]).encode())


def capture(fn, proj, ctx, symbols_digest: str = "") -> dict:
    """The snapshot recorded with an attempt, before compilation."""
    return {
        "schema": SNAPSHOT_SCHEMA,
        "project_id": proj["id"],
        "function_id": fn["id"],
        "function_revision": fn["revision"] if "revision" in fn.keys() else 1,
        "context_version": ctx["version"] if ctx else 0,  # ctx can be a sqlite3.Row: no .get
        "context_digest": context_digest(ctx) if ctx else "",
        "target_digest": target_digest(fn),
        "compiler_digest": compiler_digest(proj),
        "symbols_digest": symbols_digest,  # "" when the caller did not capture it; then it is not checked
        "scorer_version": SCORER_VERSION,
        "policy_version": POLICY_VERSION,
    }


def snapshot_id(snap: dict) -> str:
    """A stable digest of the whole snapshot, used as the receipt's snapshot identity."""
    return sha(json.dumps(snap, sort_keys=True).encode())


def check_current(net, proj, fn, snap: dict) -> tuple[bool, str]:
    """Whether the snapshot's authoritative dependencies still hold now. (ok, reason)."""
    fresh = net.store.one("SELECT * FROM functions WHERE id=?", fn["id"])
    if fresh is None:
        return False, "function is gone"
    if fresh["revision"] != snap.get("function_revision", 1):
        return False, "function was split or reset"
    if target_digest(fresh) != snap["target_digest"]:
        return False, "target changed"
    if compiler_digest(net.store.one("SELECT * FROM projects WHERE id=?", proj["id"])) != snap["compiler_digest"]:
        return False, "compiler profile changed"
    if snap.get("symbols_digest") and symbol_table_digest(net.store, proj["id"]) != snap["symbols_digest"]:
        return False, "symbols changed"
    ctx = net._context(proj["id"], fresh["unit"])
    if ctx is None:  # no context row now: only a snapshot captured without one still matches
        changed = snap["context_version"] != 0 or snap["context_digest"] != ""
    else:
        changed = ctx["version"] != snap["context_version"] or context_digest(ctx) != snap["context_digest"]
    if changed:
        return False, "context changed"
    if snap.get("scorer_version") != SCORER_VERSION or snap.get("policy_version") != POLICY_VERSION:
        return False, "scorer or policy changed"
    return True, ""


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
