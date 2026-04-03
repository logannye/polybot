"""Quote lifecycle management for market making."""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4
import structlog

log = structlog.get_logger()


@dataclass
class Quote:
    order_id: str
    token_id: str
    side: str        # "BUY" or "SELL"
    price: float
    size: float
    posted_at: datetime
    status: str = "live"  # live, partial, filled, cancelled, stale


@dataclass
class ActiveMarket:
    polymarket_id: str
    yes_token_id: str
    no_token_id: str
    category: str
    max_incentive_spread: float
    min_incentive_size: float
    bid_quote: Quote | None = None
    ask_quote: Quote | None = None
    fair_value: float = 0.5
    last_book_update: datetime | None = None


class QuoteManager:
    """Manages two-sided quotes across multiple markets."""

    def __init__(self, clob, settings, dry_run: bool = False):
        self._clob = clob
        self._settings = settings
        self._dry_run = dry_run
        self._active_quotes: dict[str, tuple[Quote | None, Quote | None]] = {}

    async def place_two_sided(
        self, market: ActiveMarket, bid_price: float, bid_size: float,
        ask_price: float, ask_size: float,
    ) -> tuple[str | None, str | None]:
        """Place a two-sided quote (bid + ask) with post_only=True."""
        bid_id = ask_id = None
        now = datetime.now(timezone.utc)

        if self._dry_run:
            bid_id = f"dry_{uuid4().hex[:8]}"
            ask_id = f"dry_{uuid4().hex[:8]}"
            bid_quote = Quote(order_id=bid_id, token_id=market.yes_token_id,
                              side="BUY", price=bid_price, size=bid_size, posted_at=now)
            ask_quote = Quote(order_id=ask_id, token_id=market.yes_token_id,
                              side="SELL", price=ask_price, size=ask_size, posted_at=now)
        else:
            try:
                bid_id = await self._clob.submit_order(
                    token_id=market.yes_token_id, side="BUY",
                    price=round(bid_price, 4), size=round(bid_size, 2),
                    post_only=True)
                bid_quote = Quote(order_id=bid_id, token_id=market.yes_token_id,
                                  side="BUY", price=bid_price, size=bid_size, posted_at=now)
            except Exception as e:
                log.error("mm_bid_failed", market=market.polymarket_id, error=str(e))
                bid_quote = None

            try:
                ask_id = await self._clob.submit_order(
                    token_id=market.yes_token_id, side="SELL",
                    price=round(ask_price, 4), size=round(ask_size, 2),
                    post_only=True)
                ask_quote = Quote(order_id=ask_id, token_id=market.yes_token_id,
                                  side="SELL", price=ask_price, size=ask_size, posted_at=now)
            except Exception as e:
                log.error("mm_ask_failed", market=market.polymarket_id, error=str(e))
                ask_quote = None

        self._active_quotes[market.polymarket_id] = (bid_quote, ask_quote)
        market.bid_quote = bid_quote
        market.ask_quote = ask_quote
        return bid_id, ask_id

    async def requote(
        self, market: ActiveMarket, new_bid: float, new_bid_size: float,
        new_ask: float, new_ask_size: float, threshold: float = 0.005,
    ) -> None:
        """Cancel and replace quotes only if price moved beyond threshold."""
        old_bid, old_ask = self._active_quotes.get(market.polymarket_id, (None, None))

        bid_moved = old_bid is None or abs(new_bid - old_bid.price) > threshold
        ask_moved = old_ask is None or abs(new_ask - old_ask.price) > threshold

        if not bid_moved and not ask_moved:
            return

        # Cancel existing quotes
        await self.cancel_market_quotes(market.polymarket_id)

        # Place new quotes
        await self.place_two_sided(market, new_bid, new_bid_size, new_ask, new_ask_size)
        log.debug("mm_requoted", market=market.polymarket_id,
                  bid=round(new_bid, 4), ask=round(new_ask, 4))

    async def cancel_market_quotes(self, polymarket_id: str) -> None:
        """Cancel all quotes for a specific market."""
        if not self._dry_run:
            old_bid, old_ask = self._active_quotes.get(polymarket_id, (None, None))
            ids_to_cancel = []
            if old_bid and old_bid.status == "live":
                ids_to_cancel.append(old_bid.order_id)
            if old_ask and old_ask.status == "live":
                ids_to_cancel.append(old_ask.order_id)
            if ids_to_cancel:
                await self._clob.cancel_orders_batch(ids_to_cancel)
        self._active_quotes.pop(polymarket_id, None)

    async def cancel_all_quotes(self) -> None:
        """Emergency: cancel all quotes across all markets."""
        if not self._dry_run:
            await self._clob.cancel_all_orders()
        self._active_quotes.clear()
        log.warning("mm_all_quotes_cancelled")

    def mark_all_stale(self) -> None:
        """Mark all quotes as stale (e.g. after heartbeat failure)."""
        for pid, (bid, ask) in self._active_quotes.items():
            if bid:
                bid.status = "stale"
            if ask:
                ask.status = "stale"

    def get_quotes(self, polymarket_id: str) -> tuple[Quote | None, Quote | None]:
        return self._active_quotes.get(polymarket_id, (None, None))
