"""Tests for the Jev typed-decision gate.

Offline tests use a fake client, so they run anywhere. The one live test skips with an
explicit reason unless a Jev key is configured -- no network in CI by default.
"""

from __future__ import annotations

import types

import pytest

from decompnet.analysis import jev


class _Answer:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Response:
    def __init__(self, answers):
        self.answers = answers


class _FakeClient:
    """Records the request and replays canned typed answers."""

    def __init__(self, answers):
        self._answers = answers
        self.calls: list[tuple] = []

    def system_one(self, state, questions, timeout=None):
        self.calls.append((state, questions, timeout))
        return _Response(self._answers)


def _env(tmp_path, **over):
    values = {"OPENROUTER_API_KEY": "test-key", "JEV_BACKEND": "typesafe"}
    values.update(over)
    path = tmp_path / "jev.env"
    path.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
    return path


# --------------------------------------------------------------------------- configuration


def test_config_prefers_environment_over_file(tmp_path, monkeypatch):
    path = _env(tmp_path, JEV_MODEL="model-from-file")
    monkeypatch.setattr(jev, "ENV_FILE", path)
    monkeypatch.setenv("JEV_MODEL", "model-from-env")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    cfg = jev.config()
    assert cfg["model"] == "model-from-env"
    assert cfg["base_url"] == jev.DEFAULT_BASE_URL
    assert cfg["api_key"] == "test-key"  # falls back to OPENROUTER_API_KEY


def test_available_without_key_names_the_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(jev, "ENV_FILE", tmp_path / "missing.env")
    for var in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    ok, reason = jev.available()
    assert ok is False
    assert "OPENROUTER_API_KEY" in reason


def test_adapter_backend_is_not_a_silent_substitute(tmp_path, monkeypatch):
    path = _env(tmp_path, JEV_BACKEND="adapter")
    monkeypatch.setattr(jev, "ENV_FILE", path)
    for var in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "TYPESAFE_API_KEY", "JEV_BACKEND"):
        monkeypatch.delenv(var, raising=False)
    ok, reason = jev.available()
    assert ok is False and "adapter" in reason


# --------------------------------------------------------------------------- question builders


def test_yes_no_shape():
    assert jev.yes_no("Is it blue?") == {"type": "noul", "instructions": "Is it blue?"}


def test_choose_always_carries_instructions():
    """Criteria-only choices are rejected by the endpoint, so instructions must be present."""
    q = jev.choose("Pick one", ["a", "b"], criteria={"a": "first"})
    assert q["type"] == "choice"
    assert q["instructions"] == "Pick one"
    assert set(q["criteria"]) == {"a", "b"}
    assert q["criteria"]["a"] == "first"
    assert q["criteria"]["b"] is None


def test_choose_rejects_empty_options():
    with pytest.raises(ValueError):
        jev.choose("Pick one", [])


def test_score_legend_is_ordered():
    q = jev.score("How close?", ["far", "close", "exact"])
    assert q["criteria"] == ["far", "close", "exact"]


# --------------------------------------------------------------------------- parsing / gating


def test_ask_parses_every_answer_kind(tmp_path, monkeypatch):
    monkeypatch.setattr(jev, "ENV_FILE", _env(tmp_path))
    client = _FakeClient(
        {
            "reg": _Answer(type="noul", noul=0.81),
            "act": _Answer(type="choice", choice="b", confidence=0.42, probabilities={"a": 0.3, "b": 0.42}),
            "sim": _Answer(type="score", score=2.0, confidence=0.5, legend={"0": "far", "2": "exact"}),
        }
    )
    result = jev.ask("state text", {"reg": jev.yes_no("q"), "act": jev.choose("q", ["a", "b"])}, client=client)
    assert result is not None
    assert result["reg"].noul == pytest.approx(0.81)
    assert result["act"].choice == "b"
    assert result["sim"].score == pytest.approx(2.0)
    assert result["sim"].legend == {"0": "far", "2": "exact"}
    assert result.latency_ms >= 0.0
    state, questions, _ = client.calls[0]
    assert state == "state text" and "act" in questions


def test_ask_returns_none_when_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(jev, "ENV_FILE", tmp_path / "missing.env")
    for var in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert jev.ask("state", {"q": jev.yes_no("q")}) is None
    with pytest.raises(jev.JevUnavailable):
        jev.require("state", {"q": jev.yes_no("q")})


def test_summarize_diff_keeps_the_worst_rows():
    text = jev.summarize_diff({"r": 2, "i": 0}, ["[r] lw $v0, 0x10($s0) | lw $v0, 0x10($s1)"], limit=3)
    assert "r=2" in text and "i=0" not in text
    assert "$s0" in text


def test_classify_diff_maps_choice_to_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(jev, "ENV_FILE", _env(tmp_path))
    answer = jev.DIFF_ACTIONS[3]
    client = _FakeClient(
        {
            "register_only": _Answer(type="noul", noul=0.9),
            "action": _Answer(type="choice", choice=answer, confidence=0.6, probabilities={answer: 0.6}),
        }
    )
    result = jev.classify_diff("state", client=client)
    assert jev.hint_for(result) == "permuter"
    assert jev.hint_for(None) is None


# --------------------------------------------------------------------------- live (opt-in)


def test_live_jev_answers_a_typed_question():
    """Live round trip; skipped unless a Jev key is configured (network + credits)."""
    ok, reason = jev.available()
    if not ok:
        pytest.skip(f"Jev unavailable: {reason}")
    result = jev.ask(
        "A compiled PlayStation function matches its target ROM in all 950 instructions; "
        "the only difference is that the ROM reads a counter through register $s0 where ours uses $s1.",
        {"register_only": jev.yes_no("Is the remaining difference limited to register allocation?")},
    )
    assert result is not None
    answer = result["register_only"]
    assert 0.0 <= (answer.noul or 0.0) <= 1.0
    assert result.latency_ms < 30_000
