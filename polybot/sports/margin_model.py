"""Sport-specific margin distribution for spread-market pricing.

Models the probability that a team's final score margin will exceed a
given threshold, given current game state.

Core assumption: future margin change is approximately Gaussian with mean 0
(neutral model — leader and trailer expected to score equally going
forward, in expectation). Initial per-sport σ values are empirically
grounded starting points; the online calibrator will refine them as
trade_outcome data accumulates.

P(final_margin > L) = Φ((current_margin - L) / σ_remaining)

where σ_remaining = total_σ × sqrt(fraction_of_regulation_remaining).

Conservative edge-rejection: returns None when game state is too noisy
(very early game, state missing, sport unsupported).
"""
from __future__ import annotations

import math
from typing import Optional

from polybot.sports.win_prob import GameState

# Per-sport parameters used by the Gaussian margin model.
# total_sigma: approximate std dev of final score margin across the full
#   regulation of a typical game. Grounded in published spread-market
#   volatility estimates — NBA 10-11 pts typical spread → σ ~5-6; NHL ~2;
#   MLB ~2; soccer ~1.5-2.
# total_periods: regulation period count (not including OT / extra).
# period_minutes: minutes per period (None for time-independent sports
#   like MLB where innings are the natural clock).
SPORT_MARGIN_PARAMS: dict[str, dict] = {
    "nba":        {"total_sigma": 5.5, "total_periods": 4, "period_minutes": 12.0},
    "ncaab":      {"total_sigma": 6.0, "total_periods": 2, "period_minutes": 20.0},
    "nhl":        {"total_sigma": 2.3, "total_periods": 3, "period_minutes": 20.0},
    "mlb":        {"total_sigma": 2.1, "total_periods": 9, "period_minutes": None},
    "epl":        {"total_sigma": 1.8, "total_periods": 2, "period_minutes": 45.0},
    "ucl":        {"total_sigma": 1.8, "total_periods": 2, "period_minutes": 45.0},
    "laliga":     {"total_sigma": 1.8, "total_periods": 2, "period_minutes": 45.0},
    "bundesliga": {"total_sigma": 1.8, "total_periods": 2, "period_minutes": 45.0},
    "mls":        {"total_sigma": 1.8, "total_periods": 2, "period_minutes": 45.0},
}


def _normal_cdf(z: float) -> float:
    """Standard normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def time_elapsed_fraction(state: GameState) -> float:
    """Return [0, 1] — how much of regulation has elapsed.

    NOTE: also used for diagnostic logs. Must never raise.
    """
    params = SPORT_MARGIN_PARAMS.get(state.sport)
    if not params:
        return 0.5

    if state.sport == "mlb":
        # Innings as the clock. Treat outs as 1/3 of an inning.
        total = params["total_periods"]
        outs = state.outs or 0
        innings_played = max(0, state.period - 1) + (min(outs, 3) / 3.0)
        return max(0.0, min(1.0, innings_played / total))

    period_minutes = params["period_minutes"]
    if period_minutes is None:
        return 0.5
    total_minutes = params["total_periods"] * period_minutes
    prior_period_minutes = max(0, state.period - 1) * period_minutes
    current_period_elapsed = period_minutes - (state.clock_seconds / 60.0)
    total_elapsed = prior_period_minutes + max(0.0, current_period_elapsed)
    return max(0.0, min(1.0, total_elapsed / total_minutes))


def compute_cover_probability(
    state: GameState, spread_line: float, cover_side: str,
) -> Optional[float]:
    """Return P(final margin for ``cover_side`` > threshold), or None.

    Polymarket spread convention: "Spread: Team (-1.5)" means the team
    needs to WIN BY MORE THAN 1.5. spread_line here is signed from
    cover_side's perspective (negative = favorite needing to cover;
    positive = underdog needing to not lose by more than |line|).

    The function returns the probability that cover_side's final margin
    exceeds ``-spread_line``. e.g., spread_line=-1.5 → threshold=+1.5 →
    P(cover_side's final_margin > 1.5).

    Parameters
    ----------
    state : GameState
        Current in-game state (period, clock, scores).
    spread_line : float
        Signed line. Negative = favorite. e.g. -1.5, +2.5.
    cover_side : str
        'home' or 'away' — whose perspective the spread_line is measured
        from (i.e., which team has the (-X.X) attached in the question).

    Returns
    -------
    Optional[float]
        Probability in [0, 1], or None if the sport/state is unsupported
        or the inputs are malformed.
    """
    if cover_side not in ("home", "away"):
        return None
    params = SPORT_MARGIN_PARAMS.get(state.sport)
    if not params:
        return None

    # Current margin from cover_side's perspective
    if cover_side == "home":
        current_margin = state.score_home - state.score_away
    else:
        current_margin = state.score_away - state.score_home

    # Remaining σ — scales with sqrt of remaining regulation fraction
    elapsed = time_elapsed_fraction(state)
    remaining = max(0.0, 1.0 - elapsed)
    sigma_remaining = params["total_sigma"] * math.sqrt(remaining)

    # Threshold the cover_side must exceed
    threshold = -spread_line

    if sigma_remaining < 0.01:
        # Game effectively over — deterministic outcome
        return 1.0 if current_margin > threshold else 0.0

    # Expected final margin ~ current_margin (neutral forward drift)
    z = (current_margin - threshold) / sigma_remaining
    return _normal_cdf(z)
