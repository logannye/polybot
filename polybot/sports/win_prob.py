"""Per-sport pure-function win-probability models.

Each model takes a ``GameState`` and returns the probability that the
"leader side" (the side with ``score_diff > 0``) wins.

Conservative by design — prefer miss-a-trade over overestimate-certainty.
Values are shrunk toward 0.5 in high-uncertainty states. The online
calibrator (``sports/calibrator.py``) corrects systematic bias empirically.

Sports covered in this module:
- NBA, NHL, MLB, NCAAB — full models
- UCL, EPL, La Liga, Bundesliga, MLS — soccer shared model
- Unsupported sports return ``None`` so the live_sports strategy skips them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Optional

Sport = Literal[
    "nba", "nhl", "mlb", "ncaab",
    "ucl", "epl", "laliga", "bundesliga", "mls",
]

SUPPORTED_SPORTS: tuple[Sport, ...] = (
    "nba", "nhl", "mlb", "ncaab",
    "ucl", "epl", "laliga", "bundesliga", "mls",
)

SOCCER_SPORTS: frozenset[str] = frozenset({"ucl", "epl", "laliga", "bundesliga", "mls"})


@dataclass(frozen=True)
class GameState:
    """Immutable snapshot of a live game. All fields are optional because
    ESPN data completeness varies by sport. Downstream code falls back to
    shrunk-to-0.5 when the fields it needs are missing.
    """
    sport: str                    # one of SUPPORTED_SPORTS (lowercase key)
    score_home: int
    score_away: int
    period: int                   # quarter/inning/half/set (sport-specific)
    clock_seconds: float          # seconds REMAINING in current period
    total_periods: int            # regulation periods (4 NBA, 3 NHL, 9 MLB, 2 soccer, 2 NCAAB)
    possession: Optional[str] = None   # "home" | "away" | None (side with the ball)
    bases: Optional[str] = None        # MLB only: "101" = 1st/3rd, etc.
    outs: Optional[int] = None         # MLB only
    balls: Optional[int] = None        # MLB only
    strikes: Optional[int] = None      # MLB only

    @property
    def score_diff(self) -> int:
        """Score of the leader minus the trailer. Always non-negative;
        check ``leader_is_home`` for side."""
        return abs(self.score_home - self.score_away)

    @property
    def leader_is_home(self) -> bool:
        return self.score_home >= self.score_away

    @property
    def regulation_seconds_remaining(self) -> float:
        """Approximate regulation seconds remaining across all periods."""
        period_seconds_map = {
            "nba": 12 * 60,
            "ncaab": 20 * 60,
            "nhl": 20 * 60,
            "mlb": 0,  # inning time is not fixed; use period count
        }
        if self.sport in SOCCER_SPORTS:
            return 45 * 60  # half length
        per_period = period_seconds_map.get(self.sport, 0)
        if per_period == 0:
            return 0.0
        future_periods = max(0, self.total_periods - self.period)
        return self.clock_seconds + future_periods * per_period


def _shrink_to_half(prob: float, shrinkage: float = 0.2) -> float:
    """Pull probability toward 0.5 by ``shrinkage`` fraction. Used when the
    game state is too early or too noisy to trust raw model output."""
    return 0.5 + (1.0 - shrinkage) * (prob - 0.5)


def _clamp(x: float, lo: float = 0.01, hi: float = 0.99) -> float:
    return max(lo, min(hi, x))


def _nba_like(state: GameState, scale_seconds: float = 10.0) -> float:
    """NBA-like win-probability (applies to NBA and NCAAB with tuning).

    Uses a logistic curve over ``score_diff / sqrt(regulation_seconds_remaining)``.
    Constants calibrated informally against historical win-prob curves;
    the online calibrator (``sports/calibrator.py``) provides the empirical
    correction.
    """
    seconds_left = state.regulation_seconds_remaining
    if seconds_left <= 0:
        # End of regulation — assume leader wins outright (tied → 0.5)
        return 0.999 if state.score_diff > 0 else 0.5
    # Normalized "lead-per-minute" analogue
    normalized = state.score_diff / math.sqrt(seconds_left / scale_seconds)
    prob_leader = 1.0 / (1.0 + math.exp(-normalized))
    return _clamp(prob_leader)


def _nhl_hockey(state: GameState) -> float:
    """NHL win probability — goal-differential dominant in hockey."""
    seconds_left = state.regulation_seconds_remaining
    if state.score_diff == 0:
        return 0.5
    if seconds_left <= 0:
        return 0.999
    # Each remaining goal's impact shrinks with less time
    # A 2-goal lead with 5 min left is very high; same lead with 40 min is moderate
    minutes_left = seconds_left / 60.0
    # Empirical constant ~0.45 tuned on NHL historical third-period WP charts
    score = state.score_diff * (1.0 + 4.0 / max(minutes_left, 1.0))
    prob_leader = 1.0 / (1.0 + math.exp(-0.45 * score))
    return _clamp(prob_leader)


def _mlb_baseball(state: GameState) -> float:
    """MLB win probability — uses inning + outs + score as primary features.

    Uses a simplified lookup because true MLB WP requires run expectancy
    tables that are out of scope for v10. The online calibrator corrects
    systematic bias.
    """
    if state.score_diff == 0:
        return 0.5
    innings_left = max(0, state.total_periods - state.period)  # top/bottom aggregation
    outs = state.outs if state.outs is not None else 0
    # Outs convert to "half-innings remaining" units: each out = -1/3 of current inning
    outs_remaining_in_current = max(0, 3 - outs)
    half_innings_left = innings_left + outs_remaining_in_current / 3.0
    if state.score_diff >= 5:
        # Large leads stabilize quickly
        base = 0.90 + 0.03 * min(half_innings_left, 3)
    elif state.score_diff >= 3:
        base = 0.78 + 0.04 * min(half_innings_left, 3)
    elif state.score_diff == 2:
        base = 0.66
    else:
        base = 0.58
    # Late-game multiplier
    if half_innings_left < 2:
        base = _clamp(base + 0.05, 0.55, 0.995)
    return _clamp(base)


def _soccer(state: GameState) -> float:
    """Soccer win probability — shared model across EPL/UCL/La Liga/Bundesliga/MLS.

    Soccer is low-scoring so goal-diff dominates. Uses a Poisson-like
    survival curve: probability of the trailer catching up shrinks
    exponentially with time remaining.
    """
    if state.score_diff == 0:
        return 0.5
    seconds_left = state.regulation_seconds_remaining
    if seconds_left <= 0:
        return 0.999
    minutes_left = seconds_left / 60.0
    # ~0.025 goals/minute typical EPL rate, so P(trailer scores N) ≈ Poisson
    # For simplicity approximate: prob_leader = 1 - exp(-lambda * time * diff)
    lambda_goal = 0.025
    # Probability that trailer catches up in remaining time ≈ exp fading
    prob_catch_up = math.exp(-lambda_goal * minutes_left * state.score_diff ** 1.5)
    prob_leader = 1.0 - 0.5 * prob_catch_up
    return _clamp(prob_leader)


def compute_win_prob(state: GameState) -> Optional[float]:
    """Return win probability of the leader side (0–1), or None if sport unsupported.

    Returns 0.5 if tied.
    """
    if state.sport not in SUPPORTED_SPORTS:
        return None

    if state.sport == "nba":
        raw = _nba_like(state, scale_seconds=10.0)
    elif state.sport == "ncaab":
        raw = _nba_like(state, scale_seconds=8.0)   # NCAAB is higher-variance
    elif state.sport == "nhl":
        raw = _nhl_hockey(state)
    elif state.sport == "mlb":
        raw = _mlb_baseball(state)
    elif state.sport in SOCCER_SPORTS:
        raw = _soccer(state)
    else:
        return None

    # Early-game shrinkage: first 20% of regulation time gets shrunk heavily
    if state.sport in ("nba", "ncaab", "nhl"):
        total_reg = {"nba": 48 * 60, "ncaab": 40 * 60, "nhl": 60 * 60}[state.sport]
        elapsed_frac = 1.0 - (state.regulation_seconds_remaining / total_reg)
        if elapsed_frac < 0.20:
            raw = _shrink_to_half(raw, shrinkage=0.40)
        elif elapsed_frac < 0.50:
            raw = _shrink_to_half(raw, shrinkage=0.15)

    return raw
