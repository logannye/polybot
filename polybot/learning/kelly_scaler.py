"""Per-strategy Beta-Binomial Kelly scaler — v10 spec §5 Loop 2.

Given a strategy's win/loss history, compute a posterior win-rate using
a Beta-Binomial model (Jeffreys prior). Compare the posterior 1σ band
to the strategy's self-reported predicted win rate:

- posterior 1σ below predicted → multiply Kelly by 0.5×
- posterior 1σ above predicted → multiply Kelly by 1.5×
- otherwise → 1.0×

Clamped to [0.25, 2.0]. Stored in ``strategy_performance.kelly_scaler``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class Posterior:
    mean: float          # posterior win-rate mean
    sigma: float         # posterior stdev (approx — Beta variance)
    n: int               # observation count


def _beta_posterior(wins: int, losses: int) -> Posterior:
    """Jeffreys prior Beta(0.5, 0.5) + observed data."""
    alpha = wins + 0.5
    beta = losses + 0.5
    total = alpha + beta
    mean = alpha / total
    # Beta variance = αβ / ((α+β)² (α+β+1))
    variance = (alpha * beta) / (total * total * (total + 1))
    sigma = math.sqrt(max(variance, 0.0))
    return Posterior(mean=mean, sigma=sigma, n=wins + losses)


def compute_kelly_scaler(
    wins: int, losses: int, predicted_prob: float,
    *, min_scale: float = 0.25, max_scale: float = 2.0,
    cold_start_n: int = 20,
) -> float:
    """Return a Kelly multiplier in ``[min_scale, max_scale]``.

    Cold-start: below ``cold_start_n`` observations, returns 1.0 (no opinion).
    """
    n = wins + losses
    if n < cold_start_n:
        return 1.0
    post = _beta_posterior(wins, losses)
    low = post.mean - post.sigma
    high = post.mean + post.sigma
    if low > predicted_prob:
        return min(max_scale, 1.5)
    if high < predicted_prob:
        return max(min_scale, 0.5)
    return 1.0


def compute_from_outcomes(
    outcomes: Iterable[dict], predicted_prob_key: str = "predicted_prob",
    pnl_key: str = "pnl", *, min_scale: float = 0.25, max_scale: float = 2.0,
    cold_start_n: int = 20,
) -> tuple[float, Optional[float]]:
    """Compute (kelly_scaler, avg_predicted_prob_or_None) from outcome rows."""
    wins = 0
    losses = 0
    predicted_sum = 0.0
    predicted_count = 0
    for row in outcomes:
        pnl = row.get(pnl_key)
        if pnl is None:
            continue
        if float(pnl) > 0:
            wins += 1
        else:
            losses += 1
        pred = row.get(predicted_prob_key)
        if pred is not None:
            predicted_sum += float(pred)
            predicted_count += 1
    avg_predicted = (predicted_sum / predicted_count) if predicted_count > 0 else None
    if avg_predicted is None:
        return 1.0, None
    scaler = compute_kelly_scaler(
        wins, losses, avg_predicted,
        min_scale=min_scale, max_scale=max_scale, cold_start_n=cold_start_n)
    return scaler, avg_predicted
