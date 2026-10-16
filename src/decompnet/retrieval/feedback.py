"""A.14 — P6.5 acceptance-feedback wiring: from a verified match's receipt back into the
similarity index and the reuse ledger, plus the P10.5 holdout leakage gate for retrieval surfaces.

Everything here is diagnostics and feedback only: it reads verification receipts and the admitted
registry, marks index availability and records routes in the reuse ledger. It never touches match
state — status, best scores, attempts and receipts belong to the judge.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import similarity, snapshots

#: Verification-receipt ``origin`` values that name a retrieval route, mapped to the canonical
#: :data:`decompnet.retrieval.ROUTE_KINDS` names. Anything else (a plain agent/room/permuter match)
#: is not a retrieval route.
ROUTE_BY_ORIGIN = {
    "exact_twins": "exact_twins",
    "exact_twin": "exact_twins",
    "exact-twin": "exact_twins",   # the historical spelling twins.py records directly
    "twin": "exact_twins",
    "twins": "exact_twins",
    "lightweight_similarity": "lightweight_similarity",
    "similarity": "lightweight_similarity",
    "lookalike": "lightweight_similarity",
    "function_id": "function_id",
    "fid": "function_id",
    "bsim": "bsim",
}


def route_from_origin(origin: str | None) -> str | None:
    """The canonical retrieval route a verification-receipt origin names, or None when the origin
    is not a retrieval route (a plain agent/room/permuter match)."""
    key = (origin or "").strip().lower()
    if key.startswith("retrieval:"):  # the engine records a retrieval route as 'retrieval:<route>'
        key = key[len("retrieval:"):]
    return ROUTE_BY_ORIGIN.get(key)


def similarity_index(net, proj) -> similarity.FeatureIndex:
    """The engine's lazy per-project :class:`decompnet.similarity.FeatureIndex` (P6.2), created on
    first acceptance feedback so that plain match paths pay nothing until a match happens."""
    index = getattr(net, "_similarity_index", None)
    if index is None:
        index = similarity.FeatureIndex(project_id=proj["id"])
        net._similarity_index = index
    return index


#: P10.5 acceptance class per ``(project id, function id)``, as :func:`record_acceptance` classified it.
ACCEPTANCE_CLASSES: dict[tuple[int, int], str] = {}


def acceptance_class(net, proj, function_id: int, route: str | None) -> str:
    """P10.5 class of one verified acceptance: ``exact_duplicate`` when another matched function of the
    project has the same target digest; ``indexed_copy`` when the accepted source digest is a
    reference-registry entry's source digest for this target (skipped quietly without a registry);
    otherwise ``cross_function_reuse`` for a retrieval route and ``not_retrieval`` for a plain origin."""
    fn = net.store.one("SELECT * FROM functions WHERE id=?", function_id)
    if fn is not None and fn["target"]:
        mine = snapshots.target_digest(fn)
        twins = net.store.all("SELECT * FROM functions WHERE project_id=? AND id<>? AND status='matched' "
                              "AND target IS NOT NULL", proj["id"], function_id)
        if any(snapshots.target_digest(other) == mine for other in twins):
            return "exact_duplicate"
        if fn["matched_source"]:
            try:
                registry = net.load_reference_sources(proj["id"])
            except Exception:  # no admitted registry to copy from
                registry = None
            source_digest = snapshots.sha(fn["matched_source"].encode())
            if registry is not None and any(entry.source_digest == source_digest
                                            and entry.target_identity == mine for entry in registry):
                return "indexed_copy"
    return "cross_function_reuse" if route else "not_retrieval"


def record_acceptance(net, proj, function_id: int) -> str | None:
    """A.14 / P6.5 #1-#3, called from ``on_match`` after a verified match.

    * builds the function's cached feature entry (when it has target bytes), so the acceptance has
      an index entry to mark (P6.2/P6.5 #1);
    * marks the entry source-available — the freshly matched source is now a usable matched example;
    * records the retrieval route its verification receipt names (P6.5 #4). A receipt with a plain
      origin (``agent``/``room``/...) marks availability but never invents a retrieval route: the
      ledger entry records the origin as-is (``agent``);
    * classifies the acceptance (P10.5, see :func:`acceptance_class`) into :data:`ACCEPTANCE_CLASSES`.

    Returns the acceptance class, or None when there is no receipt. Never writes match state.
    """
    receipt = net.store.one("SELECT origin FROM verification_receipts WHERE function_id=?", function_id)
    if receipt is None:
        return None
    origin = receipt["origin"] or ""
    route = route_from_origin(origin)
    fn = net.store.one("SELECT * FROM functions WHERE id=?", function_id)
    index = similarity_index(net, proj)
    if fn is not None and fn["target"]:
        try:  # a function we cannot disassemble still gets its availability/route feedback
            index.features_for(function_id,
                               fn["revision"] if "revision" in fn.keys() else 1,
                               snapshots.target_digest(fn),
                               proj["arch"] or "mips",
                               "little" if not proj["big_endian"] else "big",
                               fn["target"], addr=fn["vram"] or 0)
        except Exception:
            pass
    klass = acceptance_class(net, proj, function_id, route)
    index.acceptance(function_id, route=route or origin, reuse_class=klass)
    ACCEPTANCE_CLASSES[(proj["id"], function_id)] = klass
    return klass


def holdout_names(proj) -> list[str]:
    """The function names a project declares as benchmark holdouts (P10.5): the list under the
    project config key ``retrieval.holdout`` plus a ``holdout.txt`` in the project root (one function
    name per line, ``#`` starts a comment). A missing or unreadable source contributes nothing."""
    names: list[str] = []
    try:
        listed = (json.loads(proj["config"] or "{}").get("retrieval") or {}).get("holdout") or []
        names += [str(name).strip() for name in listed] if isinstance(listed, list) else []
    except (ValueError, AttributeError, TypeError):
        pass
    try:
        text = (Path(proj["root"]) / "holdout.txt").read_text(encoding="utf-8")
    except (OSError, ValueError):
        text = ""
    for line in text.splitlines():
        name = line.split("#", 1)[0].strip()
        if name:
            names.append(name)
    return [name for name in dict.fromkeys(names) if name]


def load_holdout_answers(net, proj) -> dict:
    """The project's holdout answers for the P10.5 leakage gate, keyed by answer id (the function
    name): ``{name: {"target_digest", "source_digest", "source_location"}}``. Names that are not
    functions of this project are skipped; an unmatched function has an empty source digest."""
    answers: dict = {}
    for name in holdout_names(proj):
        row = net.store.one("SELECT * FROM functions WHERE project_id=? AND name=?", proj["id"], name)
        if row is None:
            continue
        matched = row["matched_source"]
        answers[name] = {
            "target_digest": snapshots.target_digest(row),
            "source_digest": snapshots.sha(matched.encode()) if matched else "",
            "source_location": "",
        }
    return answers


def surface_leaks(net, proj, fn, config) -> list:
    """A.14 #3 / P10.5: before retrieval candidates are surfaced for one function, run the admitted
    registry's holdout leakage check. Returns the :class:`~decompnet.retrieval.Leakage` rows that
    concern *this* function (its target digest is a registered holdout answer whose source/artifact
    is in the corpus); an empty list means surface normally.

    With every retrieval route disabled this returns [] without consulting the registry, so the
    flags-off surface stays byte-identical to the pre-retrieval brief.
    """
    if not (config.function_id_retrieval or config.bsim_retrieval):
        return []
    answers = getattr(net, "_holdout_answers", None)
    if answers:  # set directly (tests); otherwise read the project's own holdout declarations
        answers = answers.get(proj["id"], {}) if isinstance(answers, dict) else {}
    else:
        answers = load_holdout_answers(net, proj)
    if not answers:
        return []
    leaks = net.load_reference_sources(proj["id"]).leakage_check(answers)
    if not leaks:
        return []
    mine = snapshots.target_digest(fn)
    return [leak for leak in leaks if answers.get(leak.answer_id, {}).get("target_digest") == mine]
