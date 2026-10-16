"""Model routing per task class (plan P11.5): configurable model identities for
easy-edit vs structural-investigation work, measurement-gated escalation, and per-run
logging of model + prompt-policy version. No config means the exact behaviour as before.

No agent CLI or network needed: the routing decision is a pure function of config and
recorded measurements, and the Manager seams are exercised with __new__ instances.
"""

from __future__ import annotations

import json

import pytest

from decompnet.app import agents, chat
from decompnet.app.agents import DEFAULT_TASK_CLASS, PROMPT_POLICY_VERSION, TASK_CLASSES


def routing(tmp_path):
    return agents.Routing(tmp_path / "model-routing.json")


def manager(tmp_path, config):
    m = agents.Manager.__new__(agents.Manager)
    m.routing = routing(tmp_path)
    m.routing_config = lambda: config
    return m


def session(options=None, task_class=DEFAULT_TASK_CLASS):
    s = agents.Session("claude-1", "claude", "c", "demo", "claude-1", options or {})
    s.task_class = task_class
    return s


def test_no_routing_config_keeps_default_behaviour():
    m = agents.Manager.__new__(agents.Manager)
    m.routing_config = lambda: {}
    assert m._routed_model(session({"model": "user-model"})) == "user-model"  # explicit still wins
    assert m._routed_model(session()) == ""  # no model, no config: the CLI's own default


def test_config_routes_each_task_class_to_its_model(tmp_path):
    m = manager(tmp_path, {"easy-edit": {"model": "cheap-edit"},
                           "structural-investigation": {"model": "big-reasoner"}})
    assert m._routed_model(session()) == "cheap-edit"
    assert m._routed_model(session(task_class="structural-investigation")) == "big-reasoner"


def test_explicit_user_model_wins_over_the_config(tmp_path):
    m = manager(tmp_path, {"easy-edit": {"model": "cheap-edit"}})
    assert m._routed_model(session({"model": "my-choice"})) == "my-choice"


def test_no_model_is_hardcoded(tmp_path):
    """Two different configs route to exactly the configured identities: nothing baked in."""
    m = manager(tmp_path, {"easy-edit": {"model": "aaa/one", "candidate": "aaa/two"}})
    assert m._routed_model(session()) == "aaa/one"  # candidate has no measurements: never picked
    m.routing_config = lambda: {"easy-edit": {"model": "zzz/other"}}
    assert m._routed_model(session()) == "zzz/other"
    m.routing_config = lambda: {}
    assert m._routed_model(session()) == ""


def test_escalation_only_after_measured_benefit():
    cfg = {"model": "cheap", "candidate": "fancy", "min_runs": 2, "min_improvement": 2}
    # no measurements at all
    assert not agents.should_escalate(cfg, {})
    # the current model measured better
    assert not agents.should_escalate(cfg, {"cheap": {"runs": 2, "matches": 5, "policy": PROMPT_POLICY_VERSION},
                                            "fancy": {"runs": 2, "matches": 3, "policy": PROMPT_POLICY_VERSION}})
    # candidate better, but below the required margin
    assert not agents.should_escalate(cfg, {"cheap": {"runs": 2, "matches": 3, "policy": PROMPT_POLICY_VERSION},
                                            "fancy": {"runs": 2, "matches": 4, "policy": PROMPT_POLICY_VERSION}})
    # candidate better, but not enough runs yet
    assert not agents.should_escalate(cfg, {"cheap": {"runs": 2, "matches": 1, "policy": PROMPT_POLICY_VERSION},
                                            "fancy": {"runs": 1, "matches": 5, "policy": PROMPT_POLICY_VERSION}})
    # measured better by the required margin and enough runs: escalate
    assert agents.should_escalate(cfg, {"cheap": {"runs": 2, "matches": 1, "policy": PROMPT_POLICY_VERSION},
                                        "fancy": {"runs": 2, "matches": 3, "policy": PROMPT_POLICY_VERSION}})
    # measurements from an older prompt policy are not comparable
    assert not agents.should_escalate(cfg, {"cheap": {"runs": 2, "matches": 1, "policy": PROMPT_POLICY_VERSION},
                                            "fancy": {"runs": 2, "matches": 3, "policy": PROMPT_POLICY_VERSION - 1}})


def test_escalation_needs_a_configured_candidate():
    assert not agents.should_escalate({"model": "cheap"}, {"cheap": {"runs": 1, "matches": 0, "policy": 1}})
    assert not agents.should_escalate({}, {})


def test_candidate_is_promoted_after_measuring_better(tmp_path):
    """The candidate only ever runs when the operator let it (explicit model or a config
    flip); promotion is a consequence of its measured benefit, not a hardcoded preference."""
    m = manager(tmp_path, {"easy-edit": {"model": "cheap", "candidate": "fancy",
                                         "min_runs": 1, "min_improvement": 1}})
    assert m._routed_model(session()) == "cheap"
    m.routing.record("easy-edit", "cheap", matches=0)
    m.routing.record("easy-edit", "fancy", matches=2)  # operator ran the candidate explicitly
    assert m._routed_model(session()) == "fancy"


def test_measurements_persist_and_accumulate(tmp_path):
    r = routing(tmp_path)
    r.record("easy-edit", "cheap", matches=1)
    r.record("easy-edit", "cheap", matches=2)
    r.record("structural-investigation", "big", matches=0)
    stats = r.stats()
    assert stats["easy-edit"]["cheap"] == {"runs": 2, "matches": 3, "policy": PROMPT_POLICY_VERSION}
    assert stats["structural-investigation"]["big"]["matches"] == 0
    # survives a reload from disk
    assert agents.Routing(tmp_path / "model-routing.json").stats() == stats


def test_measurements_restart_when_the_prompt_policy_changes(tmp_path):
    r = routing(tmp_path)
    r.record("easy-edit", "cheap", matches=5)
    r.record("easy-edit", "cheap", matches=1, runs=1, policy=PROMPT_POLICY_VERSION + 1)
    assert r.stats("easy-edit")["cheap"] == {"runs": 1, "matches": 1, "policy": PROMPT_POLICY_VERSION + 1}


def test_run_log_line_carries_model_and_prompt_policy():
    line = agents.run_log("structural-investigation", "big-reasoner")
    assert "structural-investigation" in line and "big-reasoner" in line
    assert f"prompt-policy-v{PROMPT_POLICY_VERSION}" in line
    assert "default" in agents.run_log("easy-edit", "")


def test_sessions_expose_task_class_and_prompt_policy():
    s = session(task_class="structural-investigation")
    s.routed_model = "big-reasoner"
    snap = s.snapshot()
    assert snap["task_class"] == "structural-investigation"
    assert snap["prompt_policy"] == PROMPT_POLICY_VERSION
    assert snap["model"] == ""


def test_start_agents_options_carry_the_validated_task_class():
    opts = chat.agent_options({}, per_session=3, task="structural-investigation")
    assert opts == {"per_session": 3, "task_class": "structural-investigation"}
    assert chat.agent_options({"per_session": 5}) == {"per_session": 5}  # default: easy-edit in the Manager
    with pytest.raises(ValueError, match="task class"):
        chat.agent_options({}, task="not-a-class")
    assert DEFAULT_TASK_CLASS == "easy-edit" and TASK_CLASSES == ("easy-edit", "structural-investigation")
