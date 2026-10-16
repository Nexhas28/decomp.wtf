"""Plan Phase 6 exit gate, R-01 through R-06 (PR-20/PR-21).

No compiler needed: features and ranking run on synthetic assembly strings and synthetic MIPS word
streams; the twin tests mock the judge (the only component that may ever set match state).
"""

from __future__ import annotations

import json
import struct

import pytest

from decompnet import similarity
from decompnet.app.engine import RomNetwork

# ------------------------------------------------------------------ synthetic data

# addiu sp, sp, -0x18; sw ra, 0x14(sp); lw v0, X(sp); jr ra; nop  — three near-identical functions
FUNC_A = bytes.fromhex("27bdffe8 afbf0014 8fa20000 03e00008 00000000")
FUNC_B = bytes.fromhex("27bdffe8 afbf0014 8fa20004 03e00008 00000000")
FUNC_C = bytes.fromhex("27bdffe8 afbf0014 8fa20008 03e00008 00000000")

# R-03: a 2000-instruction function: 1000 arithmetic ops shared by both candidates, then 1000 ops that
# differ in their pattern (the same mnemonic alphabet, so only order evidence separates them).
HEAD = ["addiu t0, t0, 1", "addu t1, t1, t2", "subu t2, t2, t3", "and t3, t3, t4",
        "or t4, t4, t5", "xor t5, t5, t6", "sll t6, t6, 2", "sra t7, t7, 1"]
TAIL = ["lw a0, 0x20(sp)", "sw a0, 0x20(sp)", "lbu a1, 0x21(sp)", "sb a1, 0x21(sp)",
        "lhu a2, 0x22(sp)", "sh a2, 0x22(sp)"]


def long_pair() -> tuple[list[str], list[str], list[str]]:
    head = [HEAD[i % len(HEAD)] for i in range(1000)]
    tail_a = [TAIL[i % len(TAIL)] for i in range(1000)]
    tail_b = [TAIL[(3 * i + 2) % len(TAIL)] for i in range(1000)]
    return head + tail_a, list(head + tail_a), head + tail_b


@pytest.fixture
def twin_game(tmp_path):
    """A ROM project with one matched function and one open twin: the same masked MIPS code, its data
    references pointing at another global (g_x vs D_80048000). Only the judge may match the twin."""
    net = RomNetwork(tmp_path / "app.db", tmp_path)

    def code(addr: int) -> bytes:
        return struct.pack(">IIII", 0x3C01 | ((addr >> 16) & 0xFFFF), 0x8C23 | (addr & 0xFFFF), 0x03E00008, 0)

    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, description, arch, big_endian, root, config) "
                   "VALUES(1, 'twinrom', 'twin', 'twin fixture', 'mips', 1, ?, '{}')", (str(tmp_path),))
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 1, 'int func_80041000(void);\nint func_80042000(void);\nextern int g_x;\n', "
                   "NULL, 'fixture', 0, '')")
        db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, unit, vram, "
                   "rom_offset, status, matched_source) VALUES(1,1,'func_80041000',16,4,?,?, '', '', 0x80041000, "
                   "0x1000, 'matched', ?)",
                   (code(0x80047000), json.dumps([[0, 5, 0x80047000], [4, 6, 0x80047000]]),
                    "int func_80041000(void) { return g_x; }"))
        db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, unit, vram, "
                   "rom_offset, status) VALUES(2,1,'func_80042000',16,4,?,?, '', '', 0x80042000, 0x1010, 'open')",
                   (code(0x80048000), json.dumps([[0, 5, 0x80048000], [4, 6, 0x80048000]])))
        db.execute("INSERT INTO symbols(project_id, name, address, size, kind) "
                   "VALUES(1,'g_x',0x80047000,4,'data'),(1,'D_80048000',0x80048000,4,'data')")
    return net


def counting_disassembler(monkeypatch, calls: dict) -> None:
    real = similarity.disassemble

    def counting(data, arch, mode, addr=0):
        calls["n"] += 1
        return real(data, arch, mode, addr)

    monkeypatch.setattr(similarity, "disassemble", counting)


# ------------------------------------------------------------------ R-01: caching

def test_r01_features_cached_and_reused(monkeypatch):
    """A repeated search against an unchanged corpus rebuilds nothing (P6.2): features are built once per
    (function, revision, target digest, architecture, mode) and reused."""
    idx = similarity.FeatureIndex()
    calls = {"n": 0}
    counting_disassembler(monkeypatch, calls)
    funcs = {1: FUNC_A, 2: FUNC_B, 3: FUNC_C}
    for fid, data in funcs.items():
        idx.features_for(fid, 1, f"d{fid}", "mips", "big", data, addr=0x80001000)
    assert calls["n"] == 3
    # the same identity again: cache hit, no disassembly
    f1 = idx.features_for(1, 1, "d1", "mips", "big", FUNC_A, addr=0x80001000)
    assert calls["n"] == 3
    assert f1.length == 5 and f1.norm > 0 and f1.revision == 1 and f1.digest == "d1"
    assert set(f1.hist) == {"addiu", "sw", "lw", "jr", "nop"}
    # repeated queries reuse the index entirely
    idx.search(1, "mips", "big")
    idx.search(1, "mips", "big")
    assert calls["n"] == 3
    st = idx.stats()
    assert st["builds"] == 3 and st["entries"] == 3 and st["hits"] >= 1
    # the index is versioned and diagnosable (P6.2)
    assert st["version"] == similarity.FEATURE_VERSION
    assert st["partitions"]["mips/big/bucket-0"] == 3
    assert st["inverted_terms"] >= 3


# ------------------------------------------------------------------ R-02: invalidation

def test_r02_invalidation_on_revision_target_mode_and_split(monkeypatch):
    """A function split (revision bump), target revision, mode change or explicit split invalidates just the
    affected entry: it is rebuilt and re-partitioned on next use (P6.2)."""
    idx = similarity.FeatureIndex()
    calls = {"n": 0}
    counting_disassembler(monkeypatch, calls)
    idx.features_for(1, 1, "d1", "mips", "big", FUNC_A)
    assert calls["n"] == 1
    # revision bump (split / reset)
    idx.features_for(1, 2, "d1", "mips", "big", FUNC_A)
    assert calls["n"] == 2
    # target digest changed
    idx.features_for(1, 2, "d2", "mips", "big", FUNC_A)
    assert calls["n"] == 3
    # unchanged identity: cached
    assert idx.features_for(1, 2, "d2", "mips", "big", FUNC_A).digest == "d2"
    assert calls["n"] == 3
    # mode change (endianness): rebuilt and moved to the other partition
    idx.features_for(1, 2, "d2", "mips", "little", FUNC_A)
    assert calls["n"] == 4
    parts = idx.stats()["partitions"]
    assert "mips/big/bucket-0" not in parts
    assert "mips/little/bucket-0" in parts
    # explicit invalidation (a split): entry and postings gone; next use rebuilds
    idx.invalidate(1)
    assert idx.stats()["entries"] == 0 and idx.stats()["inverted_terms"] == 0
    idx.features_for(1, 3, "d3", "mips", "little", FUNC_A)
    assert calls["n"] == 5
    assert idx.stats()["invalidations"] >= 4


# ------------------------------------------------------------------ R-03: long functions

def test_r03_long_functions_share_a_prefix_but_differ_in_the_tail():
    """Whole-function evidence distinguishes long functions whose first 800 instructions are identical
    (the old prefix-limited comparator saw both as the same)."""
    target, cand_x, cand_y = long_pair()
    assert len(target) == 2000
    assert target[:800] == cand_y[:800] == cand_x[:800]  # prefix-only comparison cannot tell them apart
    assert similarity.score(target, cand_x) == pytest.approx(1.0)
    assert similarity.score(target, cand_y) < 1.0
    hits = similarity.rank(target, {"exact": cand_x, "prefix": cand_y})
    assert hits[0][0] == "exact" and hits[0][1] == pytest.approx(1.0)
    assert hits[1][0] == "prefix" and hits[1][1] < 1.0
    # the cached features are whole-function: the tail is in the histogram, and the long bucket is used
    idx = similarity.FeatureIndex()
    idx.features_for(1, 1, "d1", "syn", "syn", target)
    idx.features_for(2, 1, "d2", "syn", "syn", cand_x)
    idx.features_for(3, 1, "d3", "syn", "syn", cand_y)
    f = idx.features(1)
    assert f.length == 2000 and similarity.size_bucket(f.length) == 4
    assert all(m in f.hist for m in ("lw", "sb"))  # tail mnemonics are part of the summary
    matches = {m.key: m for m in idx.search(1, "syn", "syn", top=5)}
    assert matches[2].similarity == pytest.approx(1.0)
    assert matches[3].similarity < 1.0
    assert matches[2].route == "windowed-sequence"  # bounded windows, not a prefix


# ------------------------------------------------------------------ R-04: twins need the judge

def test_r04_twin_with_another_global_needs_the_judge(twin_game, monkeypatch):
    """Identical fingerprints are a retrieval relationship, not proof (P6.1): the adapted source — names
    swapped — still goes to the judge, and only its verdict matters."""
    net = twin_game
    slug = "twinrom"
    a = net._function("func_80041000", slug)
    b = net._function("func_80042000", slug)
    judged = []

    def judge(agent, proj, a, b, source):
        judged.append(source)
        return ("miss", "different initial value")

    monkeypatch.setattr(net.twins, "_attempt_twin", judge)
    assert net.twins.run(slug).tried == 1
    (source,) = judged
    assert "D_80048000" in source and "g_x" not in source  # the names were adapted, not trusted
    # the identical machine code never matched by itself
    assert net._function("func_80042000", slug)["status"] == "open"
    assert net.twins.miss_why(b["id"]) == "different initial value"


def test_r04b_retrieval_never_updates_match_state(twin_game, monkeypatch):
    """Even when the judge approves, the twin pipeline itself never writes match state: it records the
    retrieval route that produced the verified reuse (P6.5)."""
    net = twin_game
    slug = "twinrom"
    monkeypatch.setattr(net.twins, "_attempt_twin", lambda agent, proj, a, b, source: ("match", ""))
    job = net.twins.run(slug)
    assert job.matched == 1
    assert net._function("func_80042000", slug)["status"] == "open"  # the judge's job, not retrieval's
    assert similarity.reuse_stats(1)["routes"] == {"exact-twin": 1}


# ------------------------------------------------------------------ R-05: dependency-aware retry

def test_r05_failed_twin_retried_when_dependencies_change(twin_game, monkeypatch):
    """A failed twin is retried the moment a material dependency changes (header, target) — not merely
    after a TTL — and stays on a bounded cooldown while nothing changed (P6.1)."""
    net = twin_game
    slug = "twinrom"
    proj = net.project_row(slug)
    a_id = net._function("func_80041000", slug)["id"]
    b_id = net._function("func_80042000", slug)["id"]
    monkeypatch.setattr(net.twins, "_attempt_twin", lambda *args: ("miss", "undefined identifier 'g_x'"))
    assert net.twins.run(slug).tried == 1
    # nothing changed: the bounded cooldown applies
    assert net.twins.run(slug).tried == 0
    assert net.twins.known_miss(proj, a_id, b_id) is True
    # the header the twin compiles against changes: retried at once, no TTL wait
    with net.store.tx() as db:
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 2, 'extern int g_x;\n/* changed */', NULL, 'x', 0, '')")
    assert net.twins.known_miss(proj, a_id, b_id) is False
    assert net.twins.run(slug).tried == 1
    # the destination target changes: a new dependency state, retried too
    with net.store.tx() as db:
        db.execute("UPDATE functions SET vram=0x80043000 WHERE id=?", (b_id,))
    net.twins.forget(proj)
    assert net.twins.known_miss(proj, a_id, b_id) is False
    assert net.twins.run(slug).tried == 1
    # each failure is remembered under its own dependency identity (source + targets + contexts + compiler)
    keys = {r["source_hash"][21:] for r in net.store.all("SELECT source_hash FROM twin_misses WHERE function_id=?",
                                                         b_id)}
    assert len(keys) == 3


# ------------------------------------------------------------------ R-06: usability and provenance

def test_r06_usable_source_and_provenance_rank_visibly():
    """Compatible and incompatible matched examples compete on usability, and the shortlist shows why each
    was retrieved, its provenance and its adaptation requirements (P6.4)."""
    prog = [f"addiu a0, a0, {i % 7}" for i in range(40)] + ["jr ra", "nop"]
    variant = prog[:20] + ["addu a0, a0, a0"] + prog[21:]
    idx = similarity.FeatureIndex()
    for fid, data in ((1, prog), (2, prog), (3, prog), (4, variant)):
        idx.features_for(fid, 1, f"d{fid}", "syn", "syn", data)
    infos = {
        2: similarity.CandidateInfo(key=2, source_available=False, verified=False,
                                    compiler_profile="gcc-2.7.2-kmc", language="C"),
        3: similarity.CandidateInfo(key=3, source_available=True, verified=False, language="C++",
                                    adaptation='extern "C" linkage', provenance="matched example"),
        4: similarity.CandidateInfo(key=4, source_available=True, verified=True,
                                    compiler_profile="gcc-2.7.2-kmc", language="C",
                                    verification_history="matched 2025-01-01 (verified)",
                                    reloc_compatible=True, provenance="verified match"),
    }
    target_info = similarity.CandidateInfo(key=0, compiler_profile="gcc-2.7.2-kmc", language="C")
    hits = idx.search(1, "syn", "syn", top=3, infos=infos, target_info=target_info)
    by_key = {m.key: m for m in hits}
    # 4 is near-identical AND fully usable: it outranks 2's perfect binary similarity
    assert hits[0].key == 4
    assert by_key[2].similarity == pytest.approx(1.0)
    assert by_key[4].similarity < 1.0 and by_key[4].rank_score > by_key[2].rank_score
    assert by_key[4].source_available and by_key[4].verified and by_key[4].compiler_ok
    assert by_key[4].provenance == "verified match" and by_key[4].verification_history
    assert by_key[2].source_available is False
    assert "no accessible source" in by_key[2].why  # informative, but less immediately reusable
    assert by_key[3].language == "C++" and by_key[3].adaptation == 'extern "C" linkage'
    assert "adaptation" in by_key[3].why
    assert hits[-1].key == 2  # the binary-only reference ranks below both usable sources
    # P6.5: acceptance feedback marks the source available and records the retrieval route
    idx.acceptance(2, source_fid=4, route="structural-rerank")
    st = idx.stats()
    assert st["available_sources"] == 1 and st["reuse_routes"] == {2: "structural-rerank"}
    assert similarity.reuse_stats(0)["routes"]["structural-rerank"] == 1


# ------------------------------------------------------------------ P6 exit gate: wiring and opt-in structure

@pytest.fixture
def look_alike_game(tmp_path):
    """A ROM project of three near-identical functions: one open (the query), one open and one matched."""
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, description, arch, big_endian, root, config) "
                   "VALUES(1, 'simrom', 'sim', 'similarity fixture', 'mips', 1, ?, '{}')", (str(tmp_path),))
        for fid, name, code, status, source in ((1, "func_a", FUNC_A, "open", None),
                                                (2, "func_b", FUNC_B, "open", None),
                                                (3, "func_c", FUNC_C, "matched", "int func_c(void) { return 1; }")):
            db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, unit, vram, "
                       "rom_offset, status, matched_source) VALUES(?,1,?,20,5,?,'[]','','',?,?,?,?)",
                       (fid, name, code, 0x80041000 + 0x100 * fid, 0x1000 * fid, status, source))
    return net


def test_engine_similar_builds_features_once(look_alike_game):
    """The second identical query is served from the shared index: nothing rebuilt, only cache hits."""
    net = look_alike_game
    proj = net.project_row("simrom")
    fn = net._function("func_a", "simrom")
    first = net.similar(proj, fn, top=5)
    index = net._similarity_index
    builds, hits = index.builds, index.hits
    assert builds == 3
    assert [r["name"] for r, _ in first] == ["func_c", "func_b"]  # matched first
    assert all(sim == pytest.approx(1.0) for _, sim in first)
    second = net.similar(proj, fn, top=5)
    assert index.builds == builds
    assert index.hits > hits
    assert [(r["name"], s) for r, s in second] == [(r["name"], s) for r, s in first]


def test_engine_similar_keeps_usability_rank_within_matched_first(look_alike_game, monkeypatch):
    """Matched examples still come first, but inside each group the usability-adjusted rank_score (P6.4)
    decides, not the raw similarity."""
    net = look_alike_game
    with net.store.tx() as db:
        db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, unit, vram, "
                   "rom_offset, status, matched_source) VALUES(4,1,'func_d',20,5,?,'[]','','',?,?,'matched', "
                   "'int func_d(void) { return 2; }')", (FUNC_C, 0x80041400, 0x4000))
    proj = net.project_row("simrom")
    fn = net._function("func_a", "simrom")

    def match(key, sim, rank):
        return similarity.Match(key=key, similarity=sim, rank_score=rank, why="", route="full-sequence",
                                source_available=True, verified=False, verification_history="", compiler_ok=None,
                                language=None, context_available=True, reloc_compatible=None, adaptation="",
                                provenance="")

    # func_d is more similar but less usable than func_c; the open func_b outranks both but stays last
    fake = [match(4, 0.95, 0.80), match(2, 1.0, 1.30), match(3, 0.90, 0.90)]
    monkeypatch.setattr(similarity.FeatureIndex, "search", lambda self, *args, **kwargs: list(fake))
    hits = net.similar(proj, fn, top=5)
    assert [r["name"] for r, _ in hits] == ["func_c", "func_d", "func_b"]
    assert [sim for _, sim in hits] == [0.90, 0.95, 1.0]


def test_structural_term_is_opt_in(monkeypatch):
    """Same mnemonics, different constants: only the structural rerank notices the difference."""
    a = similarity.Features.from_pairs([("addiu", "t0, t0, 100")] * 8)
    b = similarity.Features.from_pairs([("addiu", "t0, t0, 1")] * 8)
    monkeypatch.delenv("DECOMPNET_STRUCTURAL_RERANK", raising=False)
    assert similarity.structural_rerank_enabled() is False
    off = similarity._similarity(a, b)
    assert off == pytest.approx(1.0)  # 0.6 * sequence + 0.4 * cosine
    monkeypatch.setenv("DECOMPNET_STRUCTURAL_RERANK", "1")
    assert similarity.structural_rerank_enabled() is True
    on = similarity._similarity(a, b)
    assert 0.0 <= on < off
    monkeypatch.setenv("DECOMPNET_STRUCTURAL_RERANK", "0")
    assert similarity.structural_rerank_enabled() is False


def test_widths_alone_do_not_enable_structural_term(monkeypatch):
    """Mnemonic-only features carry access widths but no operand data, so the structural term stays off."""
    f = similarity.Features.from_ops(("lwz", "addi", "bl", "blr", "stw"))
    f2 = similarity.Features.from_ops(("stw", "addi", "bl", "lwz", "blr"))
    assert f.widths and not similarity._has_operand_data(f)
    monkeypatch.setenv("DECOMPNET_STRUCTURAL_RERANK", "1")
    on = similarity._similarity(f, f2)
    monkeypatch.delenv("DECOMPNET_STRUCTURAL_RERANK", raising=False)
    off = similarity._similarity(f, f2)
    assert on == pytest.approx(off)


def test_route_label_is_per_pair():
    """Only the pair that actually used windowed comparison is labelled windowed."""
    base = [HEAD[i % len(HEAD)] for i in range(1100)]
    shorter = base + [HEAD[i % len(HEAD)] for i in range(50)]    # 1150 ops: whole-sequence comparison
    longer = base + [HEAD[i % len(HEAD)] for i in range(200)]    # 1300 ops: bounded windows
    assert not similarity.uses_windows(1100, 1150) and similarity.uses_windows(1100, 1300)
    idx = similarity.FeatureIndex()
    for fid, data in ((1, base), (2, shorter), (3, longer)):
        idx.features_for(fid, 1, f"d{fid}", "syn", "syn", data)
    matches = {m.key: m for m in idx.search(1, "syn", "syn", top=5)}
    assert matches[2].route == "full-sequence"
    assert matches[3].route == "windowed-sequence"
