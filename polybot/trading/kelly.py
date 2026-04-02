from dataclasses import dataclass


@dataclass
class KellyResult:
    side: str
    edge: float
    odds: float
    kelly_fraction: float


def compute_kelly(ensemble_prob: float, market_price: float, fee_per_dollar: float = 0.0) -> KellyResult:
    """Compute Kelly criterion for a prediction market trade.

    Args:
        ensemble_prob: Estimated true probability (0-1).
        market_price: Current YES share price (0-1).
        fee_per_dollar: Fee drag per dollar spent. Use 0.0 for maker orders,
            or compute via fees.compute_taker_fee_per_dollar() for taker orders.
    """
    yes_edge = ensemble_prob - market_price
    no_edge = (1 - ensemble_prob) - (1 - market_price)

    if yes_edge >= no_edge and yes_edge > 0:
        side, gross_edge, buy_price, win_prob = "YES", yes_edge, market_price, ensemble_prob
    elif no_edge > 0:
        side = "NO"
        gross_edge = no_edge
        buy_price = 1 - market_price
        win_prob = 1 - ensemble_prob
    else:
        return KellyResult(side="YES", edge=0.0, odds=0.0, kelly_fraction=0.0)

    net_edge = gross_edge - fee_per_dollar
    if net_edge <= 0:
        return KellyResult(side=side, edge=0.0, odds=0.0, kelly_fraction=0.0)

    odds = (1 / buy_price) - 1
    kelly_fraction = net_edge / (1 - buy_price) if buy_price < 1.0 else 0.0
    return KellyResult(side=side, edge=net_edge, odds=odds, kelly_fraction=kelly_fraction)


def compute_position_size(
    bankroll: float,
    kelly_fraction: float,
    kelly_mult: float = 0.25,
    confidence_mult: float = 1.0,
    max_single_pct: float = 0.15,
    min_trade_size: float = 1.0,
) -> float:
    if kelly_fraction <= 0:
        return 0.0
    raw_size = bankroll * kelly_fraction * kelly_mult * confidence_mult
    max_size = bankroll * max_single_pct
    size = min(raw_size, max_size)
    if size < min_trade_size:
        return 0.0
    return round(size, 2)
