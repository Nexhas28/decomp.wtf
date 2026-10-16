"""PR-17 (P5.1-P5.3): labelled operator categories, the lightweight analyser and per-mutation
provenance for the permuter (plan tests P-01..P-03, plus P-04 determinism groundwork).

Pure Python: no compiler or toolchain is involved. The tests cover

* P-01 - round-trip identity: untouched inputs are byte-identical and unmodified inputs produce no mutation
* P-02 - dependent assignments, volatile accesses and unknown calls are never swapped by restricted
  operators; independent statements are swapped and recorded as restricted
* P-03 - signedness and control-flow exploration is labelled exploratory, and every mutation record
  carries its category and span
* P-04 - same seed + same source + same policy replays the same mutation sequence (determinism groundwork)
"""

from __future__ import annotations

import random

from decompnet import permuter
from decompnet.permuter import (
    DEFAULT_POLICY,
    EXPLORATORY,
    FALLBACK,
    OPERATORS,
    RESTRICTED,
    Mutation,
    Span,
    analyze,
    digest,
    mutate,
    propose,
    swap_allowed,
)

# ---------------------------------------------------------------- fixtures

DEPENDENT = """s32 test(void) {
    s32 a;
    s32 b;
    b = a;
    a = 1 + 1;
    return b;
}
"""

INDEPENDENT = """int f(void) {
    int a;
    int b;
    a = 1;
    b = 2;
    return a;
}
"""

VOLATILE = """int test(void) {
    volatile int x;
    int y;
    x = 1;
    y = 2;
    return y;
}
"""

CALLS = """int test(void) {
    int a;
    int b;
    b = a;
    foo();
    return b;
}
"""

MEMORY = """int test(void) {
    int *p;
    int q;
    *p = 1;
    q = 2;
    return q;
}
"""

RICH = """s32 collide(s32 a, s32 b) {
    s32 x;
    s32 y;
    s32 z;
    x = a + b;
    y = a * b;
    z = 0;
    if (a < b) {
        z = x;
    } else {
        z = y;
    }
    while (z) {
        z--;
    }
    return z;
}
"""

QUIRKY = """s32 quirky(s32 a) {
    s32 x;
    /* a block comment
       that spans lines */
    char *s = "hello; } { /* not code */";
    x = a;        // trailing comment
    x += 1;
    if (x != 0) {
        x = (s32)a + x;
    }
    return x;
}
"""

CRLF = "int f(void) {\r\n    return 0;\r\n}\r\n"


def find_mutation(source: str, operator: str, seeds: int = 400, policy=None):
    """First (record, text) whose operator matches, scanning deterministic seeds."""
    for seed in range(seeds):
        rec: list[Mutation] = []
        out = mutate(source, random.Random(seed), policy=policy, record=rec)
        if out is not None and rec[-1].operator == operator:
            return rec[-1], out
    return None, None


def covered_lines(m: Mutation) -> set[int]:
    out = set()
    for s in m.spans:
        out.update(range(s.start_line, s.end_line + 1))
    return out


# ---------------------------------------------------------------- P-01

def test_p01_round_trip_preserves_source_bytes():
    """Untouched inputs are byte-identical: the analyser never reformats (plan P-01)."""
    for src in (RICH, QUIRKY, DEPENDENT, CRLF, "int f(void) { return 0; }", ""):
        assert analyze(src).text == src


def test_p01b_unmodified_inputs_produce_no_mutation():
    """Inputs with nothing to permute (or nothing the analyser will touch) yield no mutation."""
    for src in ("int f(void) { return 0; }",
                "int f(void) {\n    return 0;\n}\n",
                "int f(void) {\n    asm(\"nop\");\n    return 0;\n}\n",
                CRLF):
        for seed in range(20):
            assert mutate(src, random.Random(seed)) is None, (src, seed)


def test_p01c_mutations_preserve_untouched_lines():
    """After a mutation, every line outside the recorded spans is byte-identical (plan P5.2 req. 1)."""
    policy = {op: w for op, w in DEFAULT_POLICY.items()
              if op in ("swap_statements", "cast_add", "cast_remove", "retype_local",
                        "register_toggle", "reorder_decls", "inc_style", "comparison_flip",
                        "cond_bool_form", "while_for")}  # line-count-preserving operators only
    original = QUIRKY.split("\n")
    found = 0
    for seed in range(100):
        rec: list[Mutation] = []
        out = mutate(QUIRKY, random.Random(seed), policy=policy, record=rec)
        if out is None:
            continue
        found += 1
        m = rec[-1]
        touched = covered_lines(m)
        for i, line in enumerate(out.split("\n")):
            if i not in touched:
                assert line == original[i], (seed, i, m.operator)
        for span in m.spans:
            assert span.start_line <= span.end_line
            assert span.start_col <= span.end_col
    assert found > 0


# ---------------------------------------------------------------- P-02

def assert_never_swapped(src: str, pair: tuple[int, int], seeds: int = 150, rng_base: int = 0):
    """The protected adjacent pair is refused as a restricted swap, and no mutation ever reorders it."""
    an = analyze(src)
    assert not swap_allowed(an, an.stmts[pair[0]], an.stmts[pair[1]])
    first, second = src.split("\n")[pair[0]], src.split("\n")[pair[1]]
    for seed in range(seeds):
        rec: list[Mutation] = []
        out = mutate(src, random.Random(rng_base + seed), record=rec)
        if out is None:
            continue
        m = rec[-1]
        # the pair is the only adjacent statement candidate, so the restricted swap operator never fires
        assert m.operator != "swap_statements", (seed, m.operator)
        if m.category == RESTRICTED:
            lines = out.split("\n")
            if first in lines and second in lines:
                assert lines.index(first) < lines.index(second), (seed, m.operator)


def test_p02_dependent_assignments_never_swapped():
    """`b = a; a = 1 + 1;` is never reordered by a restricted operator (plan P-02)."""
    assert_never_swapped(DEPENDENT, (3, 4))


def test_p02b_volatile_accesses_never_swapped():
    an = analyze(VOLATILE)
    assert an.volatile_names == frozenset({"x"})
    assert_never_swapped(VOLATILE, (3, 4))


def test_p02c_unknown_calls_never_swapped():
    an = analyze(CALLS)
    assert an.stmts[4].calls
    assert_never_swapped(CALLS, (3, 4), rng_base=1000)


def test_p02d_possible_memory_conflicts_never_swapped():
    an = analyze(MEMORY)
    assert an.stmts[3].memory
    assert_never_swapped(MEMORY, (3, 4))


def test_p02e_independent_statements_swap_as_restricted():
    """Independent assignments ARE swapped, and the record is restricted with the two-line span."""
    an = analyze(INDEPENDENT)
    assert swap_allowed(an, an.stmts[3], an.stmts[4])
    m, out = find_mutation(INDEPENDENT, "swap_statements")
    assert m is not None and out is not None
    assert m.category == RESTRICTED
    assert m.spans == (Span.whole(INDEPENDENT.split("\n"), 3, 4),)
    lines = out.split("\n")
    assert lines[3] == "    b = 2;" and lines[4] == "    a = 1;"
    assert m.parent_digest == digest(INDEPENDENT) and m.child_digest == digest(out)


# ---------------------------------------------------------------- P-03

def test_p03_categories_are_fixed_by_construction():
    """Signedness/control-flow operators are exploratory; dependency-checked ones are restricted."""
    exploratory = {"retype_local", "cast_add", "cast_remove", "return_ternary", "branch_swap",
                   "loop_bound_explore"}
    restricted = {"swap_statements", "reorder_decls", "temp_introduce", "temp_eliminate",
                  "inc_style", "comparison_flip", "cond_bool_form", "while_for", "register_toggle"}
    assert exploratory | restricted == set(OPERATORS)
    for op_id in exploratory:
        assert OPERATORS[op_id].category == EXPLORATORY
    for op_id in restricted:
        assert OPERATORS[op_id].category == RESTRICTED


def test_p03b_signedness_exploration_is_labelled_exploratory():
    src = "int f(void) {\n    s32 x;\n    x = 3;\n    return x;\n}\n"
    m, out = find_mutation(src, "retype_local")
    assert m is not None and out is not None
    assert m.category == EXPLORATORY
    assert m.spans and m.spans[0].start_line == 1  # the declaration line
    assert "x" in out and out.split("\n")[1].rstrip(";").split()[-1] == "x"


def test_p03c_control_flow_exploration_is_labelled_exploratory():
    src = "int g(void) {\n    int a;\n    int b;\n    if (a) return b;\n    return 0;\n}\n"
    m, out = find_mutation(src, "return_ternary")
    assert m is not None and out is not None
    assert m.category == EXPLORATORY
    assert m.spans == (Span.whole(src.split("\n"), 3, 4),)  # both reshaped lines
    assert "?" in out and out.count("return") == 1
    # the reverse direction is exploratory too
    split_src = "int g(void) {\n    int a;\n    return a ? 3 : 0;\n}\n"
    m2, out2 = find_mutation(split_src, "return_ternary")
    assert m2 is not None and out2 is not None and m2.category == EXPLORATORY
    assert out2.count("return") == 2 and "if (a)" in out2


def test_p03d_every_record_carries_category_and_span():
    for src in (RICH, DEPENDENT, VOLATILE, QUIRKY):
        for seed in range(60):
            rec: list[Mutation] = []
            out = mutate(src, random.Random(seed), record=rec)
            if out is None:
                continue
            m = rec[-1]
            assert m.category in (RESTRICTED, EXPLORATORY, FALLBACK)
            assert m.spans
            assert m.text == out
            assert m.parent_digest == digest(src)
            assert m.child_digest == digest(out)


# ---------------------------------------------------------------- P-04 (groundwork)

def test_p04_same_seed_source_and_policy_replay_the_same_sequence():
    """Determinism groundwork: identical (seed, source, policy) replays the same mutation history."""
    policy = dict(DEFAULT_POLICY)

    def run():
        rng = random.Random(0xC0FFEE)
        cur, seq, prev = RICH, [], RICH
        for _ in range(30):
            rec: list[Mutation] = []
            out = mutate(cur, rng, policy=policy, record=rec)
            if out is None:
                seq.append(None)
                continue
            m = rec[-1]
            assert m.parent_digest == digest(prev)  # provenance chains across mutations
            seq.append((m, out))
            cur, prev = out, out
        return seq

    first, second = run(), run()
    assert first == second
    assert first and any(m is not None for m in first)


def test_p04b_policy_filters_operators_deterministically():
    policy = {"swap_statements": 1}
    for seed in range(40):
        rec: list[Mutation] = []
        out = mutate(INDEPENDENT, random.Random(seed), policy=policy, record=rec)
        if out is not None:
            assert rec[-1].operator == "swap_statements"
            return
    raise AssertionError("swap_statements never applied under a swap-only policy")


# ------------------------------------------- unsupported constructs (P5.2 req. 3)

def test_unsupported_constructs_are_reported_and_never_rewritten():
    asm_src = ("int f(void) {\n    int a;\n    int b;\n    asm(\"nop\");\n"
               "    b = 1;\n    a = 2;\n    return a;\n}\n")
    an = analyze(asm_src)
    assert an.unsupported and "asm" in an.unsupported[0].reason
    for seed in range(100):
        rec: list[Mutation] = []
        out = mutate(asm_src, random.Random(seed), record=rec)
        if out is None:
            continue
        assert 3 not in covered_lines(rec[-1])  # the asm line is never touched

    prep_src = "int f(void) {\n#define X 1\n    return 0;\n}\n"
    assert "preprocessor" in analyze(prep_src).unsupported[0].reason

    multi_src = "int f(void) {\n    int a;\n    int b;\n    a = 1; b = 2;\n    return a;\n}\n"
    assert "multiple statements" in analyze(multi_src).unsupported[0].reason

    vla_src = "int f(int n) {\n    int a;\n    char buf[n];\n    return a;\n}\n"
    vla_an = analyze(vla_src)
    assert any("variable-length array" in u.reason and "buf" in u.reason for u in vla_an.unsupported)
    assert not vla_an.stmts[2].reorderable and vla_an.stmts[1].reorderable

    for u in an.unsupported:
        assert u.reason and 0 <= u.span.start_line <= u.span.end_line


def test_scope_separated_declarations_are_not_reordered():
    src = ("int f(void) {\n    int a;\n    {\n        int b;\n    }\n    {\n        int c;\n    }\n"
           "    return a;\n}\n")
    m, out = find_mutation(src, "reorder_decls", seeds=120)
    assert m is None and out is None  # a, b, c live in three different scopes


# ------------------------------------------- operator preconditions (P5.3)

def test_temp_introduce_requires_independent_integer_operands():
    src = ("int f(void) {\n    int x;\n    int a;\n    int b;\n    x = a + b;\n    return x;\n}\n")
    m, out = find_mutation(src, "temp_introduce")
    assert m is not None and out is not None
    assert m.category == RESTRICTED
    lines = out.split("\n")
    assert "x = a;" in lines[4] and "x += b;" in lines[5]
    # b reading x changes evaluation order: refused
    dep = src.replace("x = a + b;", "x = a + x;")
    m2, out2 = find_mutation(dep, "temp_introduce", seeds=150)
    assert m2 is None and out2 is None
    # floating-point types are refused (type handling is explicit)
    flt = src.replace("int x;", "float x;").replace("int a;", "float a;").replace("int b;", "float b;")
    m3, out3 = find_mutation(flt, "temp_introduce", seeds=150)
    assert m3 is None and out3 is None


def test_temp_ops_use_signature_parameter_types():
    src = ("s32 f(s32 a, s32 b) {\n    s32 x;\n    x = a + b;\n    return x;\n}\n")
    an = analyze(src)
    assert an.params == {"a": ("s32", False, False), "b": ("s32", False, False)}
    m, out = find_mutation(src, "temp_introduce")
    assert m is not None and out is not None and "x += b;" in out


def test_temp_eliminate_merges_split_temporaries():
    src = ("int f(void) {\n    int x;\n    int a;\n    int b;\n    x = a;\n    x += b;\n"
           "    return x;\n}\n")
    m, out = find_mutation(src, "temp_eliminate")
    assert m is not None and out is not None
    assert m.category == RESTRICTED
    assert m.spans == (Span.whole(src.split("\n"), 4, 5),)
    assert "x = a + b;" in out
    dep = src.replace("x += b;", "x += x;")
    m2, out2 = find_mutation(dep, "temp_eliminate", seeds=150)
    assert m2 is None and out2 is None


def test_inc_style_keeps_indentation_and_is_restricted():
    src = "int g(void) {\n    int c;\n    c++;\n    return c;\n}\n"
    m, out = find_mutation(src, "inc_style")
    assert m is not None and out is not None
    assert m.category == RESTRICTED
    assert out.split("\n")[2] in ("    ++c;", "    c += 1;")
    # volatile targets are refused
    vol = src.replace("int c;", "volatile int c;")
    m2, out2 = find_mutation(vol, "inc_style", seeds=150)
    assert m2 is None and out2 is None


def test_cond_bool_form_requires_integer_condition():
    src = ("int g(void) {\n    int c;\n    if (c != 0) {\n        return 1;\n    }\n"
           "    return 0;\n}\n")
    m, out = find_mutation(src, "cond_bool_form")
    assert m is not None and out is not None
    assert m.category == RESTRICTED and "if (c)" in out
    flt = src.replace("int c;", "float c;")
    m2, out2 = find_mutation(flt, "cond_bool_form", seeds=150)
    assert m2 is None and out2 is None
    unknown = src.replace("int c;", "")
    m3, out3 = find_mutation(unknown, "cond_bool_form", seeds=150)
    assert m3 is None and out3 is None


def test_comparison_flip_is_restricted_in_conditions():
    src = "int g(void) {\n    int a;\n    int b;\n    if (a < b) {\n        return 1;\n    }\n    return 0;\n}\n"
    m, out = find_mutation(src, "comparison_flip")
    assert m is not None and out is not None
    assert m.category == RESTRICTED and "if (b > a)" in out
    assert m.spans and m.spans[0].start_line == 3


def test_while_for_forms_are_restricted_equivalents():
    w = "int g(void) {\n    int c;\n    while (c) {\n        c--;\n    }\n    return c;\n}\n"
    m, out = find_mutation(w, "while_for")
    assert m is not None and out is not None
    assert m.category == RESTRICTED and "for (; c;) {" in out
    f = w.replace("while (c) {", "for (; c;) {")
    m2, out2 = find_mutation(f, "while_for")
    assert m2 is not None and out2 is not None
    assert m2.category == RESTRICTED and "while (c) {" in out2
    # conditions with calls are refused
    fc = w.replace("while (c) {", "while (f(c)) {")
    m3, out3 = find_mutation(fc, "while_for", seeds=150)
    assert m3 is None and out3 is None


def test_register_hint_requires_address_never_taken():
    ok_src = "int g(void) {\n    s32 x;\n    x = 3;\n    return x;\n}\n"
    m, out = find_mutation(ok_src, "register_toggle")
    assert m is not None and out is not None
    assert m.category == RESTRICTED and "register s32 x;" in out
    taken = ("int g(void) {\n    s32 x;\n    s32 *q;\n    q = &x;\n    return *q;\n}\n")
    for seed in range(150):
        rec: list[Mutation] = []
        out2 = mutate(taken, random.Random(seed), record=rec)
        if out2 and rec[-1].operator == "register_toggle":
            assert "register s32 x;" not in out2  # x's address is taken


# ---------------------------------------------------------------- fallback

def test_fallback_engine_is_labelled_for_unstructured_sources():
    """A K&R-style function the analyser cannot structure falls back to the legacy engine, labelled."""
    knr = "int f(void)\nint b;\nint a;\n{\n    b = 1;\n    a = 2;\n    return b;\n}\n"
    assert analyze(knr).body is None
    for seed in range(60):
        rec: list[Mutation] = []
        out = mutate(knr, random.Random(seed), record=rec)
        if out is None:
            continue
        m = rec[-1]
        assert m.category == FALLBACK
        assert m.operator.startswith("legacy:")
        assert m.spans
        return
    raise AssertionError("fallback produced no mutation")


def test_structured_sources_never_use_the_fallback():
    for seed in range(80):
        rec: list[Mutation] = []
        out = mutate(RICH, random.Random(seed), record=rec)
        if out is not None:
            assert rec[-1].category in (RESTRICTED, EXPLORATORY)


def test_propose_and_mutate_share_the_record_contract():
    m = propose(RICH, random.Random(5))
    assert m is None or isinstance(m, Mutation)
    assert permuter.digest(RICH) == digest(RICH)  # stable digests
    assert permuter.digest(RICH) != digest(RICH + "\n")
