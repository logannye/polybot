"""Win probability model for NBA, MLB, and NHL games.

Pure functions mapping score lead + game clock position to win probability.
Uses empirical scaling coefficients derived from historical sports data.
Conservative by design — better to miss trades than overestimate certainty.
"""

from __future__ import annotations

# Default total periods per sport, exported for use by strategies.
TOTAL_PERIODS: dict[str, int] = {
    "nba": 4,
    "mlb": 9,
    "nhl": 3,
}


def compute_win_probability(
    sport: str,
    lead: int,
    period: int,
    total_periods: int,
    completed: bool = False,
) -> float | None:
    """Return win probability (0.0–1.0) for the team holding *lead*.

    Args:
        sport: One of "nba", "mlb", "nhl" (case-insensitive).
        lead: Score difference for the team of interest.  Positive = leading,
              negative = trailing, zero = tied.
        period: Current period / inning (1-based).
        total_periods: Total periods / innings in regulation.
        completed: If True the game is final; returns a deterministic result.

    Returns:
        Win probability as a float in [0.0, 1.0], or None for unknown sports.
    """
    sport_key = sport.lower()

    if sport_key not in ("nba", "mlb", "nhl"):
        return None

    if completed:
        if lead > 0:
            return 1.0
        elif lead < 0:
            return 0.0
        else:
            return 0.5

    game_progress = min(period / total_periods, 1.0)

    if sport_key == "nba":
        return _nba_win_prob(lead, game_progress)
    elif sport_key == "mlb":
        return _mlb_win_prob(lead, game_progress)
    else:  # nhl
        return _nhl_win_prob(lead, game_progress)


def _nba_win_prob(lead: int, game_progress: float) -> float:
    """NBA model: each point worth 1 % at Q1 -> 3.5 % at Q4.

    Formula: 0.5 + sign * abs_lead * (0.01 + 0.025 * game_progress)
    Clamped to [0.01, 0.99].
    """
    sign = 1 if lead >= 0 else -1
    abs_lead = abs(lead)
    per_point = 0.01 + 0.025 * game_progress
    prob = 0.5 + sign * abs_lead * per_point
    return max(0.01, min(0.99, prob))


def _mlb_win_prob(lead: int, game_progress: float) -> float:
    """MLB model: strongly late-weighted — early innings have little value.

    Formula: 0.5 + sign * abs_lead * (-0.03 + 0.35 * game_progress)
    Clamped to [0.01, 0.99].

    Calibrated so that a 1-run lead in the 9th is ~0.82 win probability
    while a 3-run lead in the 3rd is ~0.76 — reflecting baseball's high
    variance in early innings.
    """
    sign = 1 if lead >= 0 else -1
    abs_lead = abs(lead)
    per_run = -0.03 + 0.35 * game_progress
    prob = 0.5 + sign * abs_lead * per_run
    return max(0.01, min(0.99, prob))


def _nhl_win_prob(lead: int, game_progress: float) -> float:
    """NHL model: each goal worth 10 % at period 1 -> 27 % at period 3.

    Formula: 0.5 + sign * abs_lead * (0.10 + 0.17 * game_progress)
    Clamped to [0.01, 0.99].

    Calibrated so that a 1-goal lead in the 3rd period is ~0.77 win
    probability — hockey goals are decisive but comebacks still happen.
    """
    sign = 1 if lead >= 0 else -1
    abs_lead = abs(lead)
    per_goal = 0.10 + 0.17 * game_progress
    prob = 0.5 + sign * abs_lead * per_goal
    return max(0.01, min(0.99, prob))
