"""Live Sports v10 engine — data pipeline + per-sport models + online calibrator.

Structure:
- ``espn_client`` — polls ESPN with 15s cadence across 9 leagues; 60s freshness guard
- ``win_prob`` — pure-function per-sport win-probability models
- ``calibrator`` — online isotonic regression per (sport, game_state_bucket)

Consumed by ``polybot.strategies.live_sports``.
"""

from polybot.sports.win_prob import compute_win_prob, GameState, SUPPORTED_SPORTS
from polybot.sports.calibrator import OnlineCalibrator

__all__ = [
    "compute_win_prob",
    "GameState",
    "SUPPORTED_SPORTS",
    "OnlineCalibrator",
]
