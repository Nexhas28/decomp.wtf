"""Shared statistics for the ablation/recall harnesses (plan Section 21: distributions, not single
runs). Pure python so every number can be unit-tested without a compiler.

Honesty rules this file encodes:
* a rate is reported with its interval and its denominator — never a bare percentage;
* a "no difference" result is a result: intervals that overlap are reported as inconclusive rather
  than as a win for whichever arm happened to lead;
* small samples are named as such (the interval carries that information).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval for k successes in n trials (95% by default).

    Preferred over the normal approximation at the small n this harness works with, and it never
    leaves [0, 1]."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass
class Arm:
    """One policy/variant's outcomes across seeds and functions."""

    name: str
    trials: int = 0
    solved: int = 0
    improved: int = 0
    compiles: int = 0
    wall_s: float = 0.0

    def add(self, solved: bool, improved: bool, compiles: int, wall_s: float) -> None:
        self.trials += 1
        self.solved += int(solved)
        self.improved += int(improved)
        self.compiles += int(compiles)
        self.wall_s += float(wall_s)

    def to_dict(self) -> dict:
        lo, hi = wilson(self.solved, self.trials)
        ilo, ihi = wilson(self.improved, self.trials)
        return {
            "arm": self.name,
            "trials": self.trials,
            "solved": self.solved,
            "solved_rate": round(self.solved / self.trials, 4) if self.trials else None,
            "solved_rate_ci95": [round(lo, 4), round(hi, 4)],
            "improved": self.improved,
            "improved_rate": round(self.improved / self.trials, 4) if self.trials else None,
            "improved_rate_ci95": [round(ilo, 4), round(ihi, 4)],
            "compiles": self.compiles,
            "compiles_per_trial": round(self.compiles / self.trials, 2) if self.trials else None,
            "wall_s": round(self.wall_s, 2),
        }


def compare(arms: list[Arm], metric: str = "solved_rate") -> dict:
    """Compare arms on one metric without overclaiming.

    Verdicts: ``leads`` when the leader's interval clears every other arm's; ``inconclusive`` when
    intervals overlap (the honest default for these sample sizes); ``no_data`` when an arm is empty."""
    data = [a.to_dict() for a in arms if a.trials]
    if len(data) < 2:
        return {"metric": metric, "verdict": "no_data", "arms": data}
    key = f"{metric}_ci95"
    ranked = sorted(data, key=lambda d: (d.get(metric) or 0.0), reverse=True)
    leader, challengers = ranked[0], ranked[1:]
    leads = all(leader[key][0] > c[key][1] for c in challengers if c.get(key))
    return {
        "metric": metric,
        "verdict": "leads" if leads else "inconclusive",
        "leader": leader["arm"],
        "reason": (f"{leader['arm']} interval {leader[key]} clears the others"
                   if leads else "arms' 95% intervals overlap: not enough evidence to separate them"),
        "arms": data,
    }
