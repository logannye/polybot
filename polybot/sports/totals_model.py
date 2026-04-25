"""Sport-specific total-points distribution for O/U-market pricing.

Models the probability that a game's final combined score will exceed a
given line, given current game state.

Core assumption: future scoring continues at the current per-unit-time
pace, with Gaussian noise. The expected remaining total is the current
scoring rate × remaining fraction; the variance scales with the
remaining fraction. For very early game (<10% elapsed) the pace
projection is unstable, so a league-average fallback is used.

P(final_total > line) = 1 - Φ((line - expected_final_total) / σ_total_remaining)
expected_final_total = current_total + expected_remaining
σ_total_remaining = total_σ_total × sqrt(fraction_of_regulation_remaining)

Conservative edge-rejection: returns None when sport unsupported or inputs
malformed (negative line, invalid side, etc.).
"""
from __future__ import annotations

import math
from typing import Optional

from polybot.sports.margin_model import time_elapsed_fraction
from polybot.sports.win_prob import GameState

# Per-sport parameters used by the Gaussian totals model.
# total_sigma_total: approximate stdev of FINAL combined score across the
#   full regulation of a typical game. Grounded in published O/U-market
#   volatility estimates: NBA totals σ ~13-14 pts, MLB σ ~3.5-4 runs,
#   NHL σ ~1.5-1.8 goals, NCAAB σ ~15-16 pts, soccer σ ~1.2-1.4 goals.
# expected_total_avg: typical FINAL combined score for the sport — used
#   to project pace from current_total when the game is early.
SPORT_TOTALS_PARAMS: dict[str, dict] = {
    "nba":        {"total_sigma_total": 14.0, "expected_total_avg": 224.0},
    "ncaab":      {"total_sigma_total": 16.0, "expected_total_avg": 145.0},
    "nhl":        {"total_sigma_total": 1.6,  "expected_total_avg": 6.2},
    "mlb":        {"total_sigma_total": 4.0,  "expected_total_avg": 8.8},
    "epl":        {"total_sigma_total": 1.3,  "expected_total_avg": 2.8},
    "ucl":        {"total_sigma_total": 1.3,  "expected_total_avg": 2.7},
    "laliga":     {"total_sigma_total": 1.3,  "expected_total_avg": 2.6},
    "bundesliga": {"total_sigma_total": 1.4,  "expected_total_avg": 3.1},
    "mls":        {"total_sigma_total": 1.4,  "expected_total_avg": 2.9},
}


def _normal_cdf(z: float) -> float:
    """Standard normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def compute_total_probability(
    state: GameState, line: float, side: str,
) -> Optional[float]:
    """Return P(final combined total > line) for "over", or P(< line) for
    "under". None if sport unsupported or inputs invalid.

    Forward projection: expected_remaining = expected_total_avg ×
    (1 - elapsed). For early-game states this anchors to the league
    average; as elapsed → 1, the remaining contribution → 0 and the
    distribution collapses around current_total.

    Variance scales with sqrt of the remaining-fraction, matching the
    margin_model treatment for spreads.

    Parameters
    ----------
    state : GameState
        Current in-game state (period, clock, scores).
    line : float
        Total line. Must be ≥ 0.
    side : str
        'over' or 'under'.

    Returns
    -------
    Optional[float]
        Probability in [0, 1], or None for unsupported/invalid inputs.
    """
    if side not in ("over", "under"):
        return None
    if line < 0:
        return None
    params = SPORT_TOTALS_PARAMS.get(state.sport)
    if not params:
        return None

    current_total = state.score_home + state.score_away
    elapsed = time_elapsed_fraction(state)
    remaining = max(0.0, 1.0 - elapsed)

    # Project remaining scoring. Linear-pace projection past the
    # early-game-noise threshold; league average before then.
    EARLY_GAME_PACE_THRESHOLD = 0.10
    if elapsed < EARLY_GAME_PACE_THRESHOLD:
        expected_remaining = params["expected_total_avg"] * remaining
    else:
        pace_per_unit = current_total / elapsed
        expected_remaining = pace_per_unit * remaining
    expected_final = current_total + expected_remaining

    # Final-total variance scales with sqrt of remaining fraction
    sigma_remaining = params["total_sigma_total"] * math.sqrt(remaining)

    if sigma_remaining < 0.01:
        # Game effectively over — deterministic outcome on actual total
        if side == "over":
            return 1.0 if current_total > line else 0.0
        return 1.0 if current_total < line else 0.0

    # P(over) = 1 - Φ((line - expected_final) / sigma)
    z = (line - expected_final) / sigma_remaining
    p_over = 1.0 - _normal_cdf(z)
    return p_over if side == "over" else (1.0 - p_over)
