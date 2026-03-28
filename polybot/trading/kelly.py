from dataclasses import dataclass


@dataclass
class KellyResult:
    side: str
    edge: float
    odds: float
    kelly_fraction: float


def compute_kelly(ensemble_prob: float, market_price: float) -> KellyResult:
    yes_edge = ensemble_prob - market_price
    no_edge = (1 - ensemble_prob) - (1 - market_price)

    if yes_edge >= no_edge and yes_edge > 0:
        side = "YES"
        edge = yes_edge
        buy_price = market_price
    elif no_edge > 0:
        side = "NO"
        edge = no_edge
        buy_price = 1 - market_price
    else:
        return KellyResult(side="YES", edge=0.0, odds=0.0, kelly_fraction=0.0)

    odds = (1 / buy_price) - 1
    kelly_fraction = edge / (1 - buy_price) if buy_price < 1.0 else 0.0
    return KellyResult(side=side, edge=edge, odds=odds, kelly_fraction=kelly_fraction)


def compute_position_size(
    bankroll: float,
    kelly_fraction: float,
    kelly_mult: float = 0.25,
    confidence_mult: float = 1.0,
    max_single_pct: float = 0.15,
    min_trade_size: float = 2.0,
) -> float:
    if kelly_fraction <= 0:
        return 0.0
    raw_size = bankroll * kelly_fraction * kelly_mult * confidence_mult
    max_size = bankroll * max_single_pct
    size = min(raw_size, max_size)
    if size < min_trade_size:
        return 0.0
    return round(size, 2)
