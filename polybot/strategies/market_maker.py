"""Market-making strategy for Polymarket.

Earns revenue from spread capture, maker rebates, liquidity rewards, and holding
rewards by posting two-sided quotes on selected markets.

Requires live mode (not dry_run) and heartbeat protocol.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from uuid import uuid4

import structlog

from polybot.trading.inventory import InventoryTracker
from polybot.trading.quote_manager import QuoteManager, ActiveMarket
from polybot.markets.rewards import RewardsClient, compute_reward_score

log = structlog.get_logger()


class MarketMakerStrategy:
    """Two-sided market-making strategy for Polymarket."""

    name = "market_maker"

    def __init__(self, settings, clob, scanner, dry_run=False,
                 rewards_client=None, inventory=None, quote_manager=None):
        self.interval_seconds = settings.mm_cycle_seconds
        self.kelly_multiplier = settings.mm_kelly_mult
        self.max_single_pct = settings.mm_max_single_pct

        self._settings = settings
        self._clob = clob
        self._scanner = scanner
        self._dry_run = dry_run
        self._rewards = rewards_client or RewardsClient()
        self._inventory = inventory or InventoryTracker(
            max_per_market=settings.mm_max_inventory_per_market,
            max_total=settings.mm_max_total_inventory,
            max_skew_bps=settings.mm_max_skew_bps,
        )
        self._quote_mgr = quote_manager or QuoteManager(
            clob=clob, settings=settings, dry_run=dry_run)

        self._active_markets: dict[str, ActiveMarket] = {}
        self._last_selection: datetime | None = None
        self._heartbeat_id: str = str(uuid4())
        self._last_heartbeat: datetime | None = None
        self._vol_blacklist: dict[str, datetime] = {}  # market -> blacklist_until

    async def run_once(self, ctx) -> None:
        """Main 5-second loop: heartbeat, select, quote, check fills."""
        # 1. Heartbeat — MUST happen every <10s
        await self._send_heartbeat()

        # Ensure QuoteManager has DB access for trade tracking
        if self._quote_mgr._db is None and hasattr(ctx, 'db'):
            self._quote_mgr._db = ctx.db

        # 2. Re-select markets periodically
        now = datetime.now(timezone.utc)
        if (self._last_selection is None or
                (now - self._last_selection).total_seconds() > self._settings.mm_selection_interval_seconds):
            await self._select_markets(ctx)
            self._last_selection = now

        if not self._active_markets:
            return

        # 3. Manage quotes for each active market
        for market in list(self._active_markets.values()):
            try:
                await self._manage_quotes(market, ctx)
            except Exception as e:
                log.error("mm_quote_error", market=market.polymarket_id, error=str(e))

        # Dry-run observability: log quoting activity without simulating fills
        if self._dry_run and self._active_markets:
            log.info("mm_dry_run_cycle", active_markets=len(self._active_markets))

    async def _send_heartbeat(self) -> None:
        """Send heartbeat to Polymarket to keep orders alive."""
        if self._dry_run:
            self._last_heartbeat = datetime.now(timezone.utc)
            return
        try:
            self._heartbeat_id = await self._clob.send_heartbeat(self._heartbeat_id)
            self._last_heartbeat = datetime.now(timezone.utc)
        except Exception as e:
            log.critical("mm_heartbeat_failed", error=str(e))
            self._quote_mgr.mark_all_stale()

    async def _select_markets(self, ctx) -> None:
        """Score and select markets for quoting."""
        markets = await self._scanner.fetch_markets()
        if not markets:
            return

        now = datetime.now(timezone.utc)
        s = self._settings
        candidates = []

        for m in markets:
            pid = m.get("polymarket_id", "")
            # Skip blacklisted markets
            if pid in self._vol_blacklist and self._vol_blacklist[pid] > now:
                continue

            res_time = m.get("resolution_time")
            if not res_time:
                continue
            hours_to_res = (res_time - now).total_seconds() / 3600.0

            # Filters
            if hours_to_res < s.mm_min_resolution_hours:
                continue
            volume = m.get("volume_24h", 0)
            if volume < s.mm_min_volume_24h:
                continue
            depth = m.get("book_depth", 0)
            if depth < s.mm_min_book_depth:
                continue
            price = m.get("yes_price", 0.5)
            if price < 0.10 or price >= 0.90:
                continue

            # Score: volume + depth + midpoint proximity (prefer ~0.50 for 3x two-sided bonus)
            mid_score = 1.0 - 2.0 * abs(price - 0.5)  # 1.0 at 0.50, 0.0 at 0/1
            vol_score = min(volume / 50000.0, 1.0)
            depth_score = min(depth / 10000.0, 1.0)
            score = mid_score * 0.4 + vol_score * 0.3 + depth_score * 0.3

            candidates.append((score, m))

        # Sort by score descending, take top N
        candidates.sort(key=lambda x: x[0], reverse=True)
        selected = candidates[:s.mm_max_markets]

        # Update active markets
        new_active = {}
        for _score, m in selected:
            pid = m["polymarket_id"]
            if pid in self._active_markets:
                new_active[pid] = self._active_markets[pid]
            else:
                new_active[pid] = ActiveMarket(
                    polymarket_id=pid,
                    yes_token_id=m.get("yes_token_id", ""),
                    no_token_id=m.get("no_token_id", ""),
                    category=m.get("category", "unknown"),
                    max_incentive_spread=0.05,  # default, updated from rewards API
                    min_incentive_size=10.0,
                    fair_value=m.get("yes_price", 0.5),
                )

        # Cancel quotes on markets we're leaving
        for pid in self._active_markets:
            if pid not in new_active:
                await self._quote_mgr.cancel_market_quotes(pid)

        self._active_markets = new_active
        log.info("mm_markets_selected", count=len(new_active),
                 markets=[m.polymarket_id for m in new_active.values()])

    async def _manage_quotes(self, market: ActiveMarket, ctx) -> None:
        """Compute fair value and manage quotes for a single market."""
        # Get current price from scanner cache
        cached = self._scanner.get_cached_price(market.polymarket_id)
        if not cached:
            return
        current_price = cached.get("yes_price", market.fair_value)
        market.fair_value = current_price
        market.last_book_update = datetime.now(timezone.utc)

        s = self._settings

        # Compute base half-spread
        base_half_spread = s.mm_base_spread_bps / 20000.0  # bps to half-spread

        # Tighten toward max_incentive_spread for reward eligibility
        if market.max_incentive_spread > 0:
            reward_half = market.max_incentive_spread / 2.0
            base_half_spread = min(base_half_spread, reward_half * 0.9)  # 90% of max to score well

        # Floor
        min_half = s.mm_min_spread_bps / 20000.0
        base_half_spread = max(base_half_spread, min_half)

        # Inventory skew
        bid_skew, ask_skew = self._inventory.compute_skew(market.polymarket_id)

        # Compute quote prices
        bid_price = market.fair_value - base_half_spread + bid_skew
        ask_price = market.fair_value + base_half_spread + ask_skew

        # Clamp to valid range
        bid_price = max(0.01, min(0.99, bid_price))
        ask_price = max(0.01, min(0.99, ask_price))

        # Ensure bid < ask
        if bid_price >= ask_price:
            return

        # Quote sizes
        quote_size_shares = s.mm_quote_size_usd / max(market.fair_value, 0.01)

        # Ensure minimum incentive size
        quote_size_shares = max(quote_size_shares, market.min_incentive_size)

        # Check inventory limits — reduce size on heavy side
        inv = self._inventory.get_inventory(market.polymarket_id)
        bid_size = quote_size_shares
        ask_size = quote_size_shares
        if inv:
            if inv.net_delta > 0:
                # Long YES — reduce bid (buy less), increase ask (sell more)
                bid_size *= max(0.2, 1.0 - inv.net_delta / self._settings.mm_max_inventory_per_market)
            elif inv.net_delta < 0:
                # Short YES — increase bid, reduce ask
                ask_size *= max(0.2, 1.0 + inv.net_delta / self._settings.mm_max_inventory_per_market)

        # Look up DB market_id for trade tracking
        db_market_id = None
        if not self._dry_run:
            if not hasattr(self, '_market_db_ids'):
                self._market_db_ids = {}
            if market.polymarket_id in self._market_db_ids:
                db_market_id = self._market_db_ids[market.polymarket_id]
            elif hasattr(ctx, 'db') and ctx.db:
                db_market_id = await ctx.db.fetchval(
                    "SELECT id FROM markets WHERE polymarket_id = $1", market.polymarket_id)
                if db_market_id:
                    self._market_db_ids[market.polymarket_id] = db_market_id

        # Requote if moved beyond threshold
        await self._quote_mgr.requote(
            market, bid_price, bid_size, ask_price, ask_size,
            threshold=s.mm_requote_threshold, market_id=db_market_id)

    async def emergency_withdraw(self) -> None:
        """Cancel all quotes and stop market making."""
        await self._quote_mgr.cancel_all_quotes()
        self._active_markets.clear()
        log.warning("mm_emergency_withdraw")
