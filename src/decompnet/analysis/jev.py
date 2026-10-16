"""Jev (TypeSafe System One) as a fast typed-decision gate for the matching loop.

Jev answers *typed* questions -- yes/no, pick-one, score -- with calibrated
probabilities in a few hundred milliseconds and never generates text, so it is a
decision layer, not a decompiler: it can triage a diff, rank options, or gate an
expensive compile, but it cannot write the C.

Wire shape (verified live against ``https://openrouter.ai/api/v1/systemone``):

* ``Noul(instructions=...)``            -> ``P(yes)``
* ``Choice(instructions=..., criteria={label: description|None})`` -> label + confidence
  A ``Choice`` **must** carry ``instructions``: criteria-only objects are rejected by
  the endpoint with ``invalid_union ... expected string, received undefined``.
* ``Score(instructions=..., criteria=[...])`` -> score + confidence

One OpenRouter key serves both Jev and the chat model. Point ``TYPESAFE_BASE_URL`` at
``https://openrouter.ai/api`` with a direct TypeSafe key, and set ``JEV_BACKEND=adapter``
to have an ordinary LLM answer the same questions when the key has no Jev access (that
fallback loses the latency and cost advantage, so it is opt-in).

This module is capability-gated and never on the critical path: every entry point
degrades to ``None`` when Jev is unavailable, and callers must treat an answer as
advice -- confidence included -- never as a verdict. The byte-exact compile judge
stays the only authority on whether a function matches.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

#: Where an operator drops the key so it never lands in the repository.
ENV_FILE = Path.home() / ".config" / "decompnet" / "jev.env"

#: Defaults mirror the documented OpenRouter wiring; no model id is hardcoded beyond these.
DEFAULT_BASE_URL = "https://openrouter.ai/api"
DEFAULT_MODEL = "~typesafe/jev-latest"
DEFAULT_TIMEOUT = 60.0


@dataclass(frozen=True)
class Answer:
    """One typed answer: exactly one of ``noul``/``choice``/``score`` is meaningful."""

    kind: str  # "noul" | "choice" | "score"
    noul: float | None = None
    choice: str | None = None
    score: float | None = None
    confidence: float | None = None
    probabilities: Mapping[str, float] = field(default_factory=dict)
    legend: Mapping[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.kind}
        if self.noul is not None:
            out["P(yes)"] = round(self.noul, 4)
        if self.choice is not None:
            out["choice"] = self.choice
            out["probabilities"] = {k: round(v, 4) for k, v in self.probabilities.items()}
        if self.score is not None:
            out["score"] = self.score
            out["legend"] = dict(self.legend)
        if self.confidence is not None:
            out["confidence"] = round(self.confidence, 4)
        return out


@dataclass(frozen=True)
class JevResult:
    """Answers plus the wall-clock cost, so callers can budget a gate."""

    answers: Mapping[str, Answer]
    latency_ms: float
    model: str

    def __getitem__(self, name: str) -> Answer:
        return self.answers[name]

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "latency_ms": round(self.latency_ms, 1),
            "answers": {k: a.as_dict() for k, a in self.answers.items()},
        }


class JevUnavailable(RuntimeError):
    """Raised only by :func:`require`; the soft entry points return ``None`` instead."""


# --------------------------------------------------------------------------- configuration


def _env_from_file(path: Path | None = None) -> dict[str, str]:
    """Read a KEY=value env file (``~/.config/decompnet/jev.env``) if it exists.

    The path is resolved at call time so tests can point :data:`ENV_FILE` elsewhere.
    """
    out: dict[str, str] = {}
    try:
        text = (path or ENV_FILE).read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def config(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Resolve Jev settings from the process env, then the on-disk env file."""
    merged = dict(_env_from_file())
    merged.update({k: v for k, v in os.environ.items() if v})
    if env:
        merged.update(env)
    key = merged.get("TYPESAFE_API_KEY") or merged.get("OPENROUTER_API_KEY") or merged.get("OPENAI_API_KEY")
    return {
        "api_key": key,
        "base_url": merged.get("TYPESAFE_BASE_URL") or DEFAULT_BASE_URL,
        "model": merged.get("JEV_MODEL") or DEFAULT_MODEL,
        "backend": (merged.get("JEV_BACKEND") or "typesafe").strip().lower(),
        "timeout": float(merged.get("JEV_TIMEOUT") or DEFAULT_TIMEOUT),
    }


def available(env: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """``(available, reason)`` -- the reason is meant to be shown, never parsed."""
    cfg = config(env)
    if not cfg["api_key"]:
        return False, f"no Jev key (set OPENROUTER_API_KEY or write {ENV_FILE})"
    if cfg["backend"] == "adapter":
        return False, "JEV_BACKEND=adapter (LLM fallback is not wired here)"
    try:
        import typesafe_sdk  # noqa: F401
    except Exception as exc:  # pragma: no cover - import guard
        return False, f"typesafe-sdk not installed: {exc}"
    return True, "ready"


# --------------------------------------------------------------------------- question builders


def yes_no(question: str) -> dict[str, Any]:
    """A ``Noul`` (yes/no) question with a calibrated ``P(yes)``."""
    return {"type": "noul", "instructions": question}


def choose(question: str, options: Sequence[str], criteria: Mapping[str, str] | None = None) -> dict[str, Any]:
    """A ``Choice`` question. ``instructions`` is mandatory (see module docstring)."""
    if not options:
        raise ValueError("choose() needs at least one option")
    crit: dict[str, Any] = {}
    for opt in options:
        crit[opt] = (criteria or {}).get(opt) or None
    return {"type": "choice", "instructions": question, "criteria": crit}


def score(question: str, legend: Sequence[str]) -> dict[str, Any]:
    """A ``Score`` question; ``legend[i]`` describes score ``i`` starting at zero."""
    return {"type": "score", "instructions": question, "criteria": list(legend)}


# --------------------------------------------------------------------------- the client


def _client(cfg: Mapping[str, Any]):  # pragma: no cover - thin SDK wrapper
    from typesafe_sdk import TypeSafeClient

    return TypeSafeClient(api_key=cfg["api_key"], base_url=cfg["base_url"], model=cfg["model"])


def _parse(answers: Mapping[str, Any]) -> dict[str, Answer]:
    out: dict[str, Answer] = {}
    for name, a in answers.items():
        kind = getattr(a, "type", "")
        if kind == "noul":
            out[name] = Answer(kind="noul", noul=float(a.noul))
        elif kind == "choice":
            out[name] = Answer(
                kind="choice",
                choice=a.choice,
                confidence=float(getattr(a, "confidence", 0.0) or 0.0),
                probabilities=dict(getattr(a, "probabilities", {}) or {}),
            )
        else:
            out[name] = Answer(
                kind="score",
                score=float(getattr(a, "score", 0.0) or 0.0),
                confidence=float(getattr(a, "confidence", 0.0) or 0.0),
                legend={str(k): str(v) for k, v in (getattr(a, "legend", None) or {}).items()},
            )
    return out


def ask(
    state: str,
    questions: Mapping[str, Mapping[str, Any]],
    *,
    env: Mapping[str, str] | None = None,
    client: Any | None = None,
    timeout: float | None = None,
) -> JevResult | None:
    """Ask one or more typed questions about ``state``; ``None`` when Jev is unavailable.

    ``state`` is the only text Jev sees, so it must carry every fact the question needs:
    Jev answers about what it is given, it does not go looking for context.
    """
    ok, reason = available(env)
    cfg = config(env)
    if not ok and client is None:
        return None
    client = client or _client(cfg)
    started = time.perf_counter()
    response = client.system_one(state, dict(questions), timeout=timeout or cfg["timeout"])
    latency = 1000 * (time.perf_counter() - started)
    return JevResult(answers=_parse(response.answers), latency_ms=latency, model=cfg["model"])


def require(
    state: str,
    questions: Mapping[str, Mapping[str, Any]],
    *,
    env: Mapping[str, str] | None = None,
    client: Any | None = None,
) -> JevResult:
    """Like :func:`ask`, but raise :class:`JevUnavailable` instead of returning ``None``."""
    result = ask(state, questions, env=env, client=client)
    if result is None:
        raise JevUnavailable(available(env)[1])
    return result


# --------------------------------------------------------------------------- decomp-specific gate

#: Next actions a diff can route to, each naming a tool the fork already has.
DIFF_ACTIONS: tuple[str, ...] = (
    "fix the source shape (rewrite the C in the offending case body)",
    "force the assembler variant (blocking load-delay nops / move-vs-addu)",
    "run the maspsx pipeline (macro or switch table expansion)",
    "run the permuter from this source",
    "accept as a near-match and move on",
)

ACTION_TO_HINT = {
    DIFF_ACTIONS[0]: "source",
    DIFF_ACTIONS[1]: "variant",
    DIFF_ACTIONS[2]: "maspsx",
    DIFF_ACTIONS[3]: "permuter",
    DIFF_ACTIONS[4]: "accept",
}


def summarize_diff(markers: Mapping[str, int], rows: Iterable[str] = (), *, limit: int = 12) -> str:
    """Build the state string for :func:`classify_diff` from markers and diff rows."""
    hist = ", ".join(f"{k}={v}" for k, v in sorted(markers.items()) if v)
    lines = [l.strip() for l in rows if l.strip()][:limit]
    parts = [f"Diff marker histogram: {hist or 'no rows differ'}."]
    if lines:
        parts.append("Worst rows (target | ours):")
        parts.extend(lines)
    return "\n".join(parts)


def classify_diff(
    state: str,
    *,
    env: Mapping[str, str] | None = None,
    client: Any | None = None,
) -> JevResult | None:
    """Route a near-miss diff to the next action, plus a yes/no on "register-only?"."""
    questions = {
        "register_only": yes_no(
            "Do the differences consist only of register names, with every instruction otherwise "
            "identical in kind, position and operands?"
        ),
        "action": choose(
            "A compiled function does not match the target ROM yet. What is the single best next action?",
            DIFF_ACTIONS,
            criteria={
                DIFF_ACTIONS[0]: "Rewrite the C so the compiler emits the target's shape",
                DIFF_ACTIONS[1]: "Let the alternate assembler pipeline try the same source",
                DIFF_ACTIONS[2]: "Let the maspsx pipeline try the same source",
                DIFF_ACTIONS[3]: "Mutate the current source with the permuter",
                DIFF_ACTIONS[4]: "Stop; the remaining difference is not worth more cycles",
            },
        ),
    }
    return ask(state, questions, env=env, client=client)


def hint_for(result: JevResult | None) -> str | None:
    """Map a :func:`classify_diff` result to the fork's short hint vocabulary."""
    if result is None or "action" not in result.answers:
        return None
    return ACTION_TO_HINT.get(result.answers["action"].choice or "")
