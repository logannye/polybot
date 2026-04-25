"""v11.0b — Pregame Sharp-Line Strategy.

Closing-line value (CLV) is the most well-studied quantity in sports
betting. ESPN's matchup predictor (BPI) tracks sportsbook closing lines
closely and is freely available via the /summary endpoint. Polymarket
pre-game prices often haven't fully converged when betting volume picks
up at line-set time — that's our edge.

Entry window: 15-60 minutes before game start.
Exit: hold to resolution; pre-tip emergency if calibrated WP falls below
0.50; take profit at 0.95 pre-tip.

V1 trades MONEYLINE markets only. Pre-game spread/total markets carry
different variance characteristics and need separate calibration; defer
to v11.0b-2.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

from polybot.strategies.base import Strategy, TradingContext
from polybot.markets.sports_matcher import (
    LiveGame, PolymarketMarket, MatchResult, match_game_to_market,
)
from polybot.sports.espn_client import ESPNClient

log = structlog.get_logger()


class PregameSharpStrategy(Strategy):
    """v11.0b pregame sharp-line strategy."""
    name = "pregame_sharp"

    def __init__(self, settings, espn_client: ESPNClient):
        self._settings = settings
        self._espn = espn_client
        self.interval_seconds = float(getattr(settings, "pg_interval_seconds", 60.0))
        self.kelly_multiplier = float(getattr(settings, "pg_kelly_mult", 0.40))
        self.max_single_pct = float(getattr(settings, "pg_max_single_pct", 0.12))

    # --------------------------------------------------------------- main

    async def run_once(self, ctx: TradingContext) -> None:
        """One cycle: fetch upcoming games → match markets → enter."""
        # Step 1 — manage exits on existing pregame positions
        await self._check_exits(ctx)

        # Step 2 — gather pregame events across configured sports
        sports = self._configured_sports()
        if not sports:
            return
        all_pregames: list[tuple[dict, str]] = []
        for sport in sports:
            try:
                events = await self._espn.fetch_pregame_events(sport)
            except Exception as e:
                log.warning("pregame_espn_fetch_error", sport=sport, error=str(e))
                continue
            for ev in events:
                start = ev.get("start_time")
                if not isinstance(start, datetime):
                    continue
                if not self._within_pregame_window(start):
                    continue
                all_pregames.append((ev, sport))

        log.info("pregame_cycle",
                 events_in_window=len(all_pregames),
                 sports_checked=len(sports))

        if not all_pregames:
            return

        # Step 3 — for each pregame in our window, fetch BPI + scan markets
        sports_markets = await self._fetch_pregame_markets(ctx)

        for ev, sport in all_pregames:
            try:
                summary = await self._espn.fetch_pregame_summary(sport, ev["espn_id"])
            except Exception as e:
                log.warning("pregame_summary_error",
                            event=ev.get("espn_id"), error=str(e))
                continue
            if summary is None:
                continue
            home_win_prob = summary["home_win_prob"]
            staleness = (datetime.now(timezone.utc) - summary["fetched_at"]).total_seconds()
            max_staleness = float(getattr(self._settings, "pg_max_bpi_staleness_s", 21600))
            if staleness > max_staleness:
                continue

            await self._scan_markets_for_pregame(
                ctx=ctx, ev=ev, sport=sport, home_win_prob=home_win_prob,
                markets=sports_markets)

    # ---------------------------------------------------------- internals

    def _configured_sports(self) -> list[str]:
        raw = getattr(self._settings, "pg_sports", None) or getattr(
            self._settings, "lg_sports", "mlb,nba,nhl,ncaab")
        return [s.strip() for s in raw.split(",") if s.strip()]

    def _within_pregame_window(self, start_time: datetime) -> bool:
        """True iff start_time is inside [pg_min_minutes_to_start, pg_max_minutes_to_start]."""
        now = datetime.now(timezone.utc)
        delta_min = (start_time - now).total_seconds() / 60.0
        lo = float(getattr(self._settings, "pg_min_minutes_to_start", 15))
        hi = float(getattr(self._settings, "pg_max_minutes_to_start", 60))
        return lo <= delta_min <= hi

    async def _fetch_pregame_markets(self, ctx: TradingContext) -> list:
        """Pull active sports markets from the scanner once per cycle.

        Reuses the live-sports scanner — pre-game markets carry the same
        category and structure; they just resolve later.
        """
        try:
            return await ctx.scanner.fetch_sports_markets()
        except Exception as e:
            log.warning("pregame_scanner_error", error=str(e))
            return []

    async def _scan_markets_for_pregame(
        self, *, ctx: TradingContext, ev: dict, sport: str,
        home_win_prob: float, markets: list,
    ) -> None:
        """For one pregame event, scan markets for matches + entries."""
        live_game = LiveGame(
            sport=sport, home_team=ev["home_team"], away_team=ev["away_team"],
            game_id=ev["espn_id"], start_time=ev["start_time"],
            score_home=0, score_away=0, status="scheduled",
        )
        min_conf = float(getattr(self._settings, "pg_matcher_min_confidence", 0.95))

        for market_dict in markets:
            market = PolymarketMarket(
                polymarket_id=market_dict.get("polymarket_id", ""),
                question=market_dict.get("question", ""),
                slug=market_dict.get("slug", ""),
                resolution_time=market_dict.get("resolution_time")
                or (datetime.now(timezone.utc) + timedelta(hours=4)),
            )
            match = match_game_to_market(live_game, market, min_confidence=min_conf)
            if match is None or match.market_type != "moneyline":
                continue   # v1 = moneyline-only

            entry = self._evaluate_pregame(
                match=match, home_win_prob=home_win_prob,
                market_dict=market_dict)
            if entry is None:
                continue

            ok = await self._entry_gate_ok(
                ctx=ctx, market=market, trade_side=entry["trade_side"],
                prob_trade_wins=entry["prob_trade_wins"],
                market_dict=market_dict)
            if not ok:
                continue

            await self._enter(
                ctx=ctx, market=market, trade_side=entry["trade_side"],
                prob_trade_wins=entry["prob_trade_wins"],
                home_win_prob=home_win_prob, market_dict=market_dict,
                ev=ev, sport=sport)
            return   # one entry per cycle to avoid race on dedupe

    def _evaluate_pregame(
        self, *, match: MatchResult, home_win_prob: float, market_dict: dict,
    ) -> Optional[dict]:
        """Map ESPN BPI to a side + edge, return entry dict or None."""
        # Translate BPI into the same orientation as the matched side.
        if match.side == "home":
            yes_prob = home_win_prob
        else:
            yes_prob = 1.0 - home_win_prob

        yes_price = float(market_dict.get("yes_price", 0.5))
        no_price = float(market_dict.get("no_price", 0.5))
        yes_edge = yes_prob - yes_price
        no_edge = (1.0 - yes_prob) - no_price

        min_edge = float(getattr(self._settings, "pg_min_edge", 0.04))
        min_calibrated = float(getattr(self._settings, "pg_min_calibrated_wp", 0.60))

        # Pick the side with the larger positive edge whose probability
        # also clears the calibrated-WP floor.
        candidates = []
        if yes_prob >= min_calibrated and yes_edge >= min_edge:
            candidates.append((yes_edge, "YES", yes_prob))
        if (1.0 - yes_prob) >= min_calibrated and no_edge >= min_edge:
            candidates.append((no_edge, "NO", 1.0 - yes_prob))
        if not candidates:
            return None
        # Larger edge wins
        candidates.sort(key=lambda c: c[0], reverse=True)
        edge, trade_side, prob = candidates[0]
        return {
            "trade_side": trade_side, "prob_trade_wins": prob,
            "yes_edge": yes_edge, "no_edge": no_edge,
            "home_win_prob": home_win_prob,
        }

    async def _entry_gate_ok(self, ctx: TradingContext, market: PolymarketMarket,
                              trade_side: str, prob_trade_wins: float,
                              market_dict: dict) -> bool:
        # 1. edge vs polymarket price (already filtered upstream, redundant
        #    but cheap safety net)
        poly_price = float(market_dict.get("yes_price", 0.5)) if trade_side == "YES" \
            else float(market_dict.get("no_price", 0.5))
        edge = prob_trade_wins - poly_price
        min_edge = float(getattr(self._settings, "pg_min_edge", 0.04))
        if edge < min_edge:
            return False

        # 2. book depth — relaxed in dry-run for flow observation; floor $500
        if getattr(self._settings, "dry_run", False):
            min_depth = max(500.0, float(getattr(
                self._settings, "pg_min_book_depth_dryrun", 1000.0)))
        else:
            min_depth = float(getattr(self._settings, "pg_min_book_depth", 5000.0))
        book_depth = float(market_dict.get("book_depth", 0))
        if book_depth < min_depth:
            log.debug("pregame_insufficient_depth",
                      market=market.polymarket_id, depth=book_depth,
                      min_depth=min_depth)
            return False

        # 3. no existing position from EITHER live_sports or pregame_sharp.
        # Both strategies trade resolutionally on the same game; they must
        # not double-book a market.
        existing = await ctx.db.fetchval(
            """SELECT COUNT(*) FROM trades t JOIN markets m ON t.market_id = m.id
               WHERE m.polymarket_id = $1
                 AND t.status IN ('open', 'filled', 'dry_run')
                 AND t.strategy IN ('live_sports', 'pregame_sharp')""",
            market.polymarket_id)
        if existing and existing > 0:
            return False
        return True

    async def _enter(self, *, ctx: TradingContext, market: PolymarketMarket,
                       trade_side: str, prob_trade_wins: float,
                       home_win_prob: float, market_dict: dict,
                       ev: dict, sport: str) -> None:
        """Place the order. Mirrors LiveSports._enter conventions."""
        from polybot.trading.kelly import compute_position_size

        token_id = (market_dict.get("yes_token_id") if trade_side == "YES"
                    else market_dict.get("no_token_id"))
        if not token_id:
            return

        poly_price = float(market_dict.get("yes_price", 0.5)) if trade_side == "YES" \
            else float(market_dict.get("no_price", 0.5))

        market_id = await ctx.db.fetchval(
            """INSERT INTO markets (polymarket_id, question, slug, resolution_time, category)
               VALUES ($1, $2, $3, $4, 'sports')
               ON CONFLICT (polymarket_id) DO UPDATE
                 SET question = EXCLUDED.question
               RETURNING id""",
            market.polymarket_id, market.question, market.slug,
            market.resolution_time)

        state_row = await ctx.db.fetchrow("SELECT * FROM system_state WHERE id = 1")
        if not state_row:
            return
        bankroll = float(state_row["bankroll"])

        # Apply per-strategy Kelly scaler if available.
        scaler = 1.0
        try:
            scaler_row = await ctx.db.fetchrow(
                "SELECT kelly_scaler FROM strategy_performance WHERE strategy = $1",
                self.name)
            if scaler_row and scaler_row["kelly_scaler"]:
                scaler = max(0.25, min(2.0, float(scaler_row["kelly_scaler"])))
        except Exception:
            pass
        effective_kelly_mult = self.kelly_multiplier * scaler

        size, kelly_fraction = compute_position_size(
            edge=(prob_trade_wins - poly_price),
            probability=prob_trade_wins, bankroll=bankroll, price=poly_price,
            max_single_pct=self.max_single_pct,
            kelly_mult=effective_kelly_mult,
        )
        min_size = float(getattr(self._settings, "min_trade_size", 1.0))
        if size < min_size:
            return

        kelly_inputs = {
            "calibrated_wp": round(prob_trade_wins, 4),
            "home_win_prob": round(home_win_prob, 4),
            "market_price": round(poly_price, 4),
            "edge": round(prob_trade_wins - poly_price, 4),
            "kelly_fraction": round(kelly_fraction, 4),
            "kelly_mult": round(effective_kelly_mult, 4),
            "sport": sport,
            "espn_event_id": ev.get("espn_id"),
            "market_type": "moneyline",
            "minutes_to_start": round(
                (ev["start_time"] - datetime.now(timezone.utc)).total_seconds() / 60.0, 1),
        }

        log.info("pregame_entry",
                 market=market.polymarket_id, side=trade_side,
                 size=round(size, 2), edge=round(prob_trade_wins - poly_price, 4),
                 prob=round(prob_trade_wins, 4), home_wp=round(home_win_prob, 4),
                 sport=sport, kelly_mult=round(effective_kelly_mult, 3))

        async with ctx.portfolio_lock:
            await ctx.executor.place_order(
                token_id=token_id, side=trade_side,
                size_usd=size, price=poly_price,
                market_id=market_id, analysis_id=None, strategy=self.name,
                kelly_inputs=kelly_inputs,
                post_only=True)   # maker-first

    async def _check_exits(self, ctx: TradingContext) -> None:
        """Manage open pregame positions.

        v1 exit policy:
          - Pre-tip emergency: BPI WP drops below pg_emergency_exit_wp → close
          - Take profit: market price ≥ pg_take_profit_price → close
          - Default: hold to resolution
        Once the game has started, pregame disengages — the position is
        held to resolution at whatever the oracle settles. Live Sports
        operates on different (in-play) markets and won't conflict.
        """
        try:
            open_trades = await ctx.db.fetch(
                """SELECT t.id, t.market_id, t.side, t.entry_price, t.kelly_inputs,
                          m.polymarket_id, m.question, m.resolution_time
                   FROM trades t JOIN markets m ON t.market_id = m.id
                   WHERE t.strategy = $1
                     AND t.status IN ('open', 'filled', 'dry_run')""",
                self.name)
        except Exception as e:
            log.warning("pregame_exits_query_error", error=str(e))
            return

        if not open_trades:
            return

        emergency = float(getattr(self._settings, "pg_emergency_exit_wp", 0.50))
        tp = float(getattr(self._settings, "pg_take_profit_price", 0.95))

        for trade in open_trades:
            kelly = trade.get("kelly_inputs") or {}
            if isinstance(kelly, str):
                import json
                try:
                    kelly = json.loads(kelly)
                except (ValueError, TypeError):
                    kelly = {}
            espn_id = kelly.get("espn_event_id")
            sport = kelly.get("sport")
            if not espn_id or not sport:
                continue

            # Has the game started? If so, leave it alone (hold to resolution).
            event_start = None
            try:
                events = await self._espn.fetch_pregame_events(sport)
                evt = next((e for e in events if e.get("espn_id") == espn_id), None)
                if evt is None:
                    # Likely game has started — pregame disengages.
                    continue
                event_start = evt.get("start_time")
            except Exception:
                continue
            if not event_start or event_start <= datetime.now(timezone.utc):
                continue

            # Re-fetch BPI for emergency exit
            try:
                summary = await self._espn.fetch_pregame_summary(sport, espn_id)
            except Exception:
                continue
            if summary is None:
                continue
            home_wp = summary["home_win_prob"]
            # Side semantics same as on entry
            wp_for_our_side = home_wp if trade["side"] == "YES" else (1.0 - home_wp)
            # Check moneyline orientation: if entry kelly inputs say sport
            # the market resolves on the home team and we bought YES, our
            # side is home; otherwise inverted. v1 keeps it simple — assume
            # YES = home as the most common matcher convention.

            if wp_for_our_side < emergency:
                log.info("pregame_emergency_exit",
                         trade=trade["id"], wp=round(wp_for_our_side, 4),
                         emergency=emergency)
                await self._close_position(ctx, trade,
                                            exit_reason="pregame_emergency")
                continue

            # Take profit — needs current market price; for v1 we read from
            # ctx.scanner cache if available.
            try:
                price_cache = ctx.scanner.get_all_cached_prices() \
                    if hasattr(ctx.scanner, "get_all_cached_prices") else {}
            except Exception:
                price_cache = {}
            cached = price_cache.get(trade["polymarket_id"])
            if cached:
                cur_price = float(cached.get("yes_price", 0.5)) \
                    if trade["side"] == "YES" \
                    else float(cached.get("no_price", 0.5))
                if cur_price >= tp:
                    log.info("pregame_take_profit",
                             trade=trade["id"], cur_price=round(cur_price, 4))
                    await self._close_position(ctx, trade,
                                                exit_reason="take_profit")

    async def _close_position(self, ctx: TradingContext, trade: dict,
                                *, exit_reason: str) -> None:
        """Close a pregame position via the executor."""
        try:
            await ctx.executor.close_position(
                trade_id=trade["id"], exit_reason=exit_reason)
        except AttributeError:
            # Older executors don't have close_position — fall back to a
            # status update so the position isn't lost.
            await ctx.db.execute(
                "UPDATE trades SET status='cancelled' WHERE id = $1", trade["id"])
        except Exception as e:
            log.warning("pregame_close_failed",
                        trade=trade["id"], error=str(e))
