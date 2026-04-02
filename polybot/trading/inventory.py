"""Inventory tracking and quote skew computation for market making."""

from dataclasses import dataclass, field
import structlog

log = structlog.get_logger()


@dataclass
class MarketInventory:
    polymarket_id: str
    yes_shares: float = 0.0
    no_shares: float = 0.0
    cost_basis: float = 0.0
    realized_pnl: float = 0.0

    @property
    def net_delta(self) -> float:
        """Positive = long YES, negative = long NO."""
        return self.yes_shares - self.no_shares


class InventoryTracker:
    """Track inventory per market and compute quote skew for market making."""

    def __init__(self, max_per_market: float = 50.0, max_total: float = 200.0,
                 max_skew_bps: int = 100):
        self._positions: dict[str, MarketInventory] = {}
        self._max_per_market = max_per_market
        self._max_total = max_total
        self._max_skew_bps = max_skew_bps

    def get_or_create(self, polymarket_id: str) -> MarketInventory:
        if polymarket_id not in self._positions:
            self._positions[polymarket_id] = MarketInventory(polymarket_id=polymarket_id)
        return self._positions[polymarket_id]

    def record_fill(self, polymarket_id: str, side: str, price: float, size: float) -> None:
        """Record a fill event, updating inventory."""
        inv = self.get_or_create(polymarket_id)
        cost = price * size
        if side == "BUY":
            inv.yes_shares += size
            inv.cost_basis += cost
        elif side == "SELL":
            inv.yes_shares -= size
            inv.cost_basis -= cost
        log.debug("mm_fill_recorded", market=polymarket_id, side=side,
                  price=price, size=size, net_delta=inv.net_delta)

    def compute_skew(self, polymarket_id: str) -> tuple[float, float]:
        """Compute bid/ask adjustments based on inventory.

        Returns (bid_adjustment, ask_adjustment) in price units.
        When long YES: widen ask (raise), tighten bid (raise) to encourage selling.
        When short YES: tighten ask (lower), widen bid (lower) to encourage buying.
        """
        inv = self._positions.get(polymarket_id)
        if not inv:
            return 0.0, 0.0
        if self._max_per_market <= 0:
            return 0.0, 0.0
        # Linear skew proportional to net delta
        skew_frac = inv.net_delta / self._max_per_market
        skew_frac = max(-1.0, min(1.0, skew_frac))  # clamp
        skew = skew_frac * (self._max_skew_bps / 10000.0)
        return skew, skew

    def is_at_limit(self, polymarket_id: str) -> bool:
        """Check if inventory for this market is at the hard limit."""
        inv = self._positions.get(polymarket_id)
        if not inv:
            return False
        return abs(inv.net_delta * 0.50) >= self._max_per_market  # rough USD estimate at midpoint

    def get_total_exposure(self) -> float:
        """Total absolute net exposure across all markets (rough USD)."""
        return sum(abs(inv.net_delta) * 0.50 for inv in self._positions.values())

    def get_inventory(self, polymarket_id: str) -> MarketInventory | None:
        return self._positions.get(polymarket_id)

    def all_inventories(self) -> dict[str, MarketInventory]:
        return dict(self._positions)
