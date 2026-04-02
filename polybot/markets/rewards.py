"""Polymarket liquidity rewards API client and scoring."""

from dataclasses import dataclass
import structlog

log = structlog.get_logger()


@dataclass
class MarketRewardConfig:
    condition_id: str
    max_incentive_spread: float
    min_incentive_size: float


def compute_reward_score(spread: float, max_spread: float,
                         size: float, min_size: float) -> float:
    """Compute quadratic liquidity reward score.

    Polymarket scoring: S(v,s) = ((v-s)/v)^2
    where v = max_incentive_spread, s = actual spread from midpoint.
    Score is 0 if spread >= max_spread or size < min_size.
    """
    if spread >= max_spread or size < min_size:
        return 0.0
    if max_spread <= 0:
        return 0.0
    return ((max_spread - spread) / max_spread) ** 2


class RewardsClient:
    """Fetch liquidity reward parameters from Polymarket API."""

    def __init__(self, session=None):
        self._session = session

    async def fetch_reward_markets(self) -> list[MarketRewardConfig]:
        """Fetch markets with active liquidity reward programs.

        TODO: Implement once Polymarket rewards API endpoint is identified.
        For now, returns empty list — market selection will rely on
        volume/spread heuristics until reward data is available.
        """
        return []
