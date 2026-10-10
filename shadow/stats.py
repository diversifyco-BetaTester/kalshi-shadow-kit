"""
stats.py — confidence intervals that respect correlation.

Buckets of one Kalshi event are correlated (exactly one wins), so resample whole groups
(events), not individual rows — otherwise the CI is too narrow and a fake edge looks real.
"""
from __future__ import annotations

import random


def block_bootstrap_ci(groups: list[list[float]], reps: int = 2000, seed: int = 7) -> tuple[float, float, float]:
    """Mean and 95% CI of all values, resampling whole groups with replacement."""
    flat = [x for g in groups for x in g]
    if not flat:
        raise ValueError("no data")
    rng = random.Random(seed)
    means = []
    for _ in range(reps):
        sample = [x for g in (rng.choice(groups) for _ in groups) for x in g]
        means.append(sum(sample) / len(sample))
    means.sort()
    return sum(flat) / len(flat), means[int(0.025 * reps)], means[int(0.975 * reps)]


def rule_of_three(n: int) -> float:
    """95% upper bound on an event's rate after observing it 0 times in n trials."""
    return 3.0 / n if n else 1.0
