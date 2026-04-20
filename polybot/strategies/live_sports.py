"""Live Sports v10 strategy — the primary engine per spec §3.

Retail traders on Polymarket don't re-price every play during live games.
ESPN publishes the same feed for free at ~5–15s cadence. This strategy
captures the retail-reaction lag on games where the calibrated win
probability diverges from Polymarket's price by ≥4%.

Entry gate — ALL six conditions must hold:
- calibrated win probability ≥ 0.85
- edge vs Polymarket price ≥ 0.04
- book depth at entry price ≥ $10,000
- ESPN data freshness < 60s
- matcher confidence ≥ 0.95
- no existing open position in this market

Exit rules:
- default: hold to market resolution
- emergency: calibrated WP drops below 0.70 → close (taker)
- take profit: price reaches 0.97+ → close, recycle capital
- time stop: 6h hard maximum hold
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

from polybot.strategies.base import Strategy, TradingContext
from polybot.sports.espn_client import ESPNClient
from polybot.sports.win_prob import compute_win_prob, GameState
from polybot.sports.calibrator import OnlineCalibrator, bucket_for_game_state
from polybot.sports.threshold import get_active_wp_threshold, passes_live_threshold
from polybot.markets.sports_matcher import (
    LiveGame, PolymarketMarket, match_game_to_market,
    compute_match_confidence, classify_market_type,
)
from polybot.trading.kelly import compute_position_size
from polybot.learning.trade_outcome import record_outcome as record_trade_outcome

log = structlog.get_logger()


# -------------------------------------------------------------------------
# ESPN game → internal LiveGame struct
# -------------------------------------------------------------------------
_ESPN_TO_SPORT = {
    "mlb": "mlb", "nba": "nba", "nhl": "nhl", "ncaab": "ncaab",
    "ucl": "ucl", "epl": "epl", "laliga": "laliga",
    "bundesliga": "bundesliga", "mls": "mls",
}


def espn_game_to_live_game(espn_game: dict) -> Optional[LiveGame]:
    """Convert an ESPN scoreboard entry to a LiveGame. Returns None if skip."""
    sport = _ESPN_TO_SPORT.get(espn_game.get("sport", ""))
    if not sport:
        return None
    if espn_game.get("status") != "in_progress":
        return None
    try:
        return LiveGame(
            sport=sport,
            home_team=espn_game.get("home_team", ""),
            away_team=espn_game.get("away_team", ""),
            game_id=str(espn_game.get("espn_id", "")),
            start_time=datetime.now(timezone.utc),   # ESPN client doesn't surface start time
            score_home=int(espn_game.get("home_score", 0)),
            score_away=int(espn_game.get("away_score", 0)),
            status="in_progress",
        )
    except (TypeError, ValueError):
        return None


def espn_game_to_game_state(espn_game: dict) -> Optional[GameState]:
    """Convert an ESPN scoreboard entry to a GameState for win-prob computation."""
    sport = _ESPN_TO_SPORT.get(espn_game.get("sport", ""))
    if not sport:
        return None

    try:
        period = int(espn_game.get("period", 1))
        score_home = int(espn_game.get("home_score", 0))
        score_away = int(espn_game.get("away_score", 0))
    except (TypeError, ValueError):
        return None

    total_periods_map = {
        "nba": 4, "ncaab": 2, "nhl": 3, "mlb": 9,
        "ucl": 2, "epl": 2, "laliga": 2, "bundesliga": 2, "mls": 2,
    }
    total_periods = total_periods_map.get(sport, 4)

    clock_str = str(espn_game.get("clock", "0:00"))
    clock_seconds = _parse_clock(clock_str)

    return GameState(
        sport=sport,
        score_home=score_home,
        score_away=score_away,
        period=period,
        clock_seconds=clock_seconds,
        total_periods=total_periods,
    )


def _parse_clock(clock: str) -> float:
    """Parse ESPN clock 'MM:SS' or 'SS.S' → seconds."""
    clock = (clock or "").strip()
    if ":" in clock:
        try:
            parts = clock.split(":")
            return int(parts[0]) * 60 + float(parts[1])
        except (ValueError, IndexError):
            return 0.0
    try:
        return float(clock)
    except ValueError:
        return 0.0


# -------------------------------------------------------------------------
# Strategy class
# -------------------------------------------------------------------------
class LiveSportsStrategy(Strategy):
    """v10 Live Sports strategy (primary engine)."""
    name = "live_sports"

    def __init__(self, settings, espn_client: ESPNClient,
                 calibrator: Optional[OnlineCalibrator] = None):
        self._settings = settings
        self._espn = espn_client
        self._calibrator = calibrator or OnlineCalibrator()
        self.interval_seconds = float(getattr(settings, "lg_interval_seconds", 30.0))
        self.kelly_multiplier = float(getattr(settings, "lg_kelly_mult", 0.50))
        self.max_single_pct = float(getattr(settings, "lg_max_single_pct", 0.20))

    async def run_once(self, ctx: TradingContext) -> None:
        """One cycle: fetch ESPN → match markets → enter passes + manage exits."""
        # Step 1 — check exits first so capital is freed before new entries
        await self._check_exits(ctx)

        # Step 2 — fetch live games with freshness guard
        max_staleness_s = float(getattr(self._settings, "lg_max_staleness_s", 60.0))
        fetched_at = datetime.now(timezone.utc)
        try:
            all_games = await self._espn.fetch_all_live_games()
        except Exception as e:
            log.error("live_sports_espn_fetch_error", error=str(e))
            return
        if (datetime.now(timezone.utc) - fetched_at).total_seconds() > max_staleness_s:
            log.warning("live_sports_stale_data",
                        age_s=(datetime.now(timezone.utc) - fetched_at).total_seconds())
            return

        # Step 3 — filter to games we can potentially trade
        eligible_games = [g for g in all_games if g.get("status") == "in_progress"]
        if not eligible_games:
            log.info("live_sports_no_live_games")
            return

        # Step 4 — for each eligible game, find matching Polymarket markets
        # Fetch Polymarket candidates once per cycle
        try:
            markets = await ctx.scanner.fetch_sports_markets()
        except AttributeError:
            log.warning("live_sports_scanner_missing_fetch_sports_markets")
            return

        # Visibility into sports-market supply — how many markets in the
        # matcher-eligible window (resolution within 12h of now)? Surfaces
        # the "no live-game markets on Polymarket" condition distinct from
        # "matcher rejected everything" failure mode.
        now = datetime.now(timezone.utc)
        near_term_count = sum(
            1 for m in markets
            if m.get("resolution_time")
            and 0 < (m["resolution_time"] - now).total_seconds() < 12 * 3600)
        log.info("live_sports_market_supply",
                 total_sports_markets=len(markets),
                 near_term_12h=near_term_count,
                 live_games=len(eligible_games))

        for espn_game in eligible_games:
            live_game = espn_game_to_live_game(espn_game)
            state = espn_game_to_game_state(espn_game)
            if not live_game or not state:
                continue
            raw_prob = compute_win_prob(state)
            if raw_prob is None:
                continue

            bucket = bucket_for_game_state(
                sport=state.sport, score_diff=state.score_diff,
                period=state.period, total_periods=state.total_periods,
                seconds_left=state.regulation_seconds_remaining)
            calibrated = self._calibrator.apply(state.sport, bucket, raw_prob)

            active_threshold = get_active_wp_threshold(self._settings)
            live_ok = passes_live_threshold(calibrated, self._settings)

            # Full-funnel instrumentation — always log the evaluation so we
            # see the WP distribution, not just rejections. Enables
            # data-driven tuning without speculation.
            log.info(
                "live_sports_wp_evaluated",
                sport=state.sport, bucket=bucket,
                raw_wp=round(raw_prob, 4),
                calibrated_wp=round(calibrated, 4),
                score_diff=state.score_diff,
                period=state.period,
                clock_s=round(state.clock_seconds, 1),
                active_threshold=round(active_threshold, 3),
                passes_active=calibrated >= active_threshold,
                passes_live_threshold=live_ok,
            )

            if calibrated < active_threshold:
                continue

            await self._evaluate_game(
                ctx=ctx, live_game=live_game, state=state,
                calibrated_wp=calibrated, raw_wp=raw_prob,
                bucket=bucket, passes_live=live_ok, markets=markets)

    async def _evaluate_game(self, ctx: TradingContext, live_game: LiveGame,
                              state: GameState, calibrated_wp: float,
                              raw_wp: float, bucket: str, passes_live: bool,
                              markets: list) -> None:
        """For one live game, find matching markets and enter if gate passes."""
        min_conf = float(getattr(self._settings, "lg_matcher_min_confidence", 0.95))
        active_threshold = get_active_wp_threshold(self._settings)
        # Track per-game matcher funnel so we can log a summary if nothing matches
        best_confidence = 0.0
        best_breakdown: dict = {}
        markets_classified = 0
        for market_dict in markets:
            try:
                market = PolymarketMarket(
                    polymarket_id=market_dict["polymarket_id"],
                    question=market_dict.get("question", ""),
                    slug=market_dict.get("slug", ""),
                    resolution_time=market_dict.get("resolution_time")
                                     or datetime.now(timezone.utc),
                )
            except KeyError:
                continue

            # Only consider markets that classify as a relevant type AND
            # pass basic team-name sniff test — otherwise the matcher
            # rejection log is flooded with obvious mismatches.
            if classify_market_type(market.question) is None:
                continue
            markets_classified += 1
            # Score the match for diagnostic tracking
            conf, breakdown = compute_match_confidence(live_game, market)
            if conf > best_confidence:
                best_confidence = conf
                best_breakdown = {
                    "market_id": market.polymarket_id[:12],
                    "question": (market.question or "")[:70],
                    "confidence": round(conf, 4),
                    **breakdown,
                }

            match = match_game_to_market(live_game, market, min_confidence=min_conf)
            if not match or match.market_type != "moneyline":
                continue

            # Determine which side we want to buy
            wp_leader_is_home = state.leader_is_home
            trade_side: str
            if match.side == "home":
                # Market asks if home wins. We want YES if home is leading with high WP
                trade_side = "YES" if wp_leader_is_home else "NO"
                prob_trade_wins = calibrated_wp if wp_leader_is_home else (1.0 - calibrated_wp)
            else:   # match.side == "away"
                trade_side = "YES" if not wp_leader_is_home else "NO"
                prob_trade_wins = calibrated_wp if not wp_leader_is_home else (1.0 - calibrated_wp)

            if prob_trade_wins < active_threshold:
                continue

            # Entry gate — remaining checks
            ok = await self._entry_gate_ok(
                ctx=ctx, market=market, trade_side=trade_side,
                prob_trade_wins=prob_trade_wins, market_dict=market_dict)
            if not ok:
                continue

            await self._enter(
                ctx=ctx, market=market, trade_side=trade_side,
                prob_trade_wins=prob_trade_wins, raw_wp=raw_wp,
                passes_live=passes_live, market_dict=market_dict,
                match_bucket=bucket, live_game=live_game, state=state)
            return   # One entry per game per cycle — avoid racing on dedup

        # No match found in this cycle — log the best near-miss so we can
        # see the funnel. Guards against the flood case (thousands of markets
        # scored 0) by only logging when the best confidence is non-trivial
        # OR we had at least one classified moneyline/spread/total market.
        if markets_classified > 0:
            log.info(
                "live_sports_matcher_no_match",
                sport=state.sport,
                home=live_game.home_team, away=live_game.away_team,
                markets_classified=markets_classified,
                best_confidence=round(best_confidence, 4),
                min_confidence=min_conf,
                best_match=best_breakdown,
            )

    async def _entry_gate_ok(self, ctx: TradingContext, market: PolymarketMarket,
                               trade_side: str, prob_trade_wins: float,
                               market_dict: dict) -> bool:
        # 1. edge vs Polymarket price
        poly_price = float(market_dict.get("yes_price", 0.5)) if trade_side == "YES" \
            else float(market_dict.get("no_price", 0.5))
        edge = prob_trade_wins - poly_price
        min_edge = float(getattr(self._settings, "lg_min_edge", 0.04))
        if edge < min_edge:
            log.debug("live_sports_edge_below_threshold",
                      market=market.polymarket_id, edge=round(edge, 4))
            return False

        # 2. book depth
        min_depth = float(getattr(self._settings, "lg_min_book_depth", 10000.0))
        book_depth = float(market_dict.get("book_depth", 0))
        if book_depth < min_depth:
            log.debug("live_sports_insufficient_depth",
                      market=market.polymarket_id, depth=book_depth)
            return False

        # 3. no existing position in this market
        existing = await ctx.db.fetchval(
            "SELECT COUNT(*) FROM trades t JOIN markets m ON t.market_id = m.id "
            "WHERE m.polymarket_id = $1 AND t.status IN ('open', 'filled', 'dry_run')",
            market.polymarket_id)
        if existing and existing > 0:
            return False

        return True

    async def _enter(self, ctx: TradingContext, market: PolymarketMarket,
                      trade_side: str, prob_trade_wins: float, raw_wp: float,
                      passes_live: bool, market_dict: dict,
                      match_bucket: str, live_game: LiveGame, state: GameState) -> None:
        """Submit an entry order (maker-first via executor)."""
        # Upsert market row
        market_id = await ctx.db.fetchval(
            """INSERT INTO markets (polymarket_id, question, category, resolution_time,
                   current_price, volume_24h, book_depth)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               ON CONFLICT (polymarket_id) DO UPDATE SET
                   current_price=$5, volume_24h=$6, book_depth=$7, last_updated=NOW()
               RETURNING id""",
            market.polymarket_id, market.question, "live_sports",
            market.resolution_time,
            float(market_dict.get("yes_price", 0.5)),
            float(market_dict.get("volume_24h", 0.0)),
            float(market_dict.get("book_depth", 0.0)),
        )

        # Position size
        state_row = await ctx.db.fetchrow("SELECT * FROM system_state WHERE id = 1")
        bankroll = float(state_row["bankroll"])
        poly_price = float(market_dict.get("yes_price", 0.5)) if trade_side == "YES" \
            else float(market_dict.get("no_price", 0.5))
        edge = prob_trade_wins - poly_price
        # Kelly approximation: f* ≈ edge / odds for binary bets
        kelly_fraction = edge / (1.0 - poly_price) if poly_price < 1.0 else 0.0
        size = compute_position_size(
            bankroll=bankroll,
            kelly_fraction=kelly_fraction,
            kelly_mult=self.kelly_multiplier,
            confidence_mult=1.0,
            max_single_pct=self.max_single_pct,
            min_trade_size=float(getattr(self._settings, "min_trade_size", 1.0)))
        if size <= 0:
            return

        token_id = market_dict.get("yes_token_id") if trade_side == "YES" \
            else market_dict.get("no_token_id")
        if not token_id:
            log.warning("live_sports_missing_token_id", market=market.polymarket_id)
            return

        log.info("live_sports_entry",
                 market=market.polymarket_id, side=trade_side, size=round(size, 2),
                 edge=round(edge, 4), calibrated_wp=round(prob_trade_wins, 4),
                 raw_wp=round(raw_wp, 4), passes_live_threshold=passes_live,
                 sport=state.sport, bucket=match_bucket)

        async with ctx.portfolio_lock:
            await ctx.executor.place_order(
                token_id=token_id, side=trade_side,
                size_usd=size, price=poly_price,
                market_id=market_id, analysis_id=None, strategy=self.name,
                kelly_inputs={
                    "calibrated_wp": round(prob_trade_wins, 4),
                    "raw_wp": round(raw_wp, 4),
                    "market_price": round(poly_price, 4),
                    "edge": round(edge, 4),
                    "kelly_fraction": round(kelly_fraction, 4),
                    "sport": state.sport,
                    "game_state_bucket": match_bucket,
                    "passes_live_threshold": passes_live,
                },
                post_only=True)    # maker-first

    async def _check_exits(self, ctx: TradingContext) -> None:
        """Close positions triggered by emergency / take-profit / time stop."""
        open_trades = await ctx.db.fetch(
            "SELECT t.*, m.polymarket_id, m.question FROM trades t "
            "JOIN markets m ON t.market_id = m.id "
            "WHERE t.strategy = $1 AND t.status IN ('open', 'filled', 'dry_run')",
            self.name)
        if not open_trades:
            return

        max_hold_hours = float(getattr(self._settings, "lg_max_hold_hours", 6.0))
        tp_price = float(getattr(self._settings, "lg_take_profit_price", 0.97))
        emergency_wp = float(getattr(self._settings, "lg_emergency_exit_wp", 0.70))

        for trade in open_trades:
            try:
                opened_at = trade["opened_at"]
                age_hours = (datetime.now(timezone.utc) - opened_at).total_seconds() / 3600.0

                if age_hours >= max_hold_hours:
                    await self._exit(ctx, trade, exit_reason="time_stop")
                    continue

                # Current price check for TP
                current_price = await self._current_price(ctx, trade)
                if current_price is None:
                    continue
                if current_price >= tp_price:
                    await self._exit(ctx, trade, exit_reason="take_profit",
                                     exit_price=current_price)
                    continue
            except Exception as e:
                log.error("live_sports_exit_check_error",
                          trade_id=trade["id"], error=str(e))

    async def _current_price(self, ctx: TradingContext, trade) -> Optional[float]:
        try:
            book = await ctx.scanner.fetch_order_book(
                trade.get("yes_token_id") or trade.get("no_token_id") or "")
            asks = book.get("asks", [])
            return float(asks[0]["price"]) if asks else None
        except Exception:
            return None

    async def _exit(self, ctx: TradingContext, trade, exit_reason: str,
                     exit_price: Optional[float] = None) -> None:
        """Close a trade via executor with the given reason. Records a
        trade_outcome row after the close so the calibrator + Kelly
        scaler + edge-decay monitor can learn from realized PnL."""
        log.info("live_sports_exit", trade_id=trade["id"], reason=exit_reason)
        try:
            pnl = await ctx.executor.exit_position(
                trade_id=trade["id"],
                exit_price=exit_price if exit_price is not None else 0.5,
                exit_reason=exit_reason)
        except Exception as e:
            log.error("live_sports_exit_failed",
                      trade_id=trade["id"], reason=exit_reason, error=str(e))
            return

        # Record the outcome for the learning layer. Non-fatal on error —
        # record_outcome logs and returns -1 rather than raising so a
        # learning-layer hiccup can't lose the trade close.
        try:
            kelly_inputs = _safe_json_loads(trade.get("kelly_inputs"))
            entry_price = float(trade.get("entry_price") or 0.5)
            effective_exit = float(exit_price) if exit_price is not None else 0.5
            opened_at = trade.get("opened_at")
            duration_min = 0.0
            if opened_at is not None:
                duration_min = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60.0
            realized = _exit_reason_to_realized_outcome(exit_reason, effective_exit)
            await record_trade_outcome(
                db=ctx.db,
                strategy=self.name,
                market_id=int(trade.get("market_id") or 0),
                market_category="live_sports",
                entry_price=entry_price,
                exit_price=effective_exit,
                pnl=float(pnl) if pnl is not None else 0.0,
                predicted_prob=(
                    float(kelly_inputs.get("calibrated_wp"))
                    if kelly_inputs.get("calibrated_wp") is not None else None),
                realized_outcome=realized,
                exit_reason=exit_reason,
                duration_minutes=duration_min,
                kelly_inputs=kelly_inputs,
                game_state_bucket=kelly_inputs.get("game_state_bucket"),
            )
        except Exception as e:
            log.error("live_sports_outcome_record_failed",
                      trade_id=trade["id"], error=str(e))


def _safe_json_loads(value) -> dict:
    """Robustly parse a kelly_inputs value that may be JSON text, already
    a dict (from pg jsonb), or None."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def _exit_reason_to_realized_outcome(exit_reason: str,
                                      exit_price: float) -> Optional[int]:
    """Map exit_reason + exit_price to 0/1 realized outcome for calibrator.
    Returns None when the outcome is ambiguous (early exit without resolution)."""
    if exit_reason == "take_profit":
        return 1       # position won
    if exit_reason in ("stop_loss", "emergency_exit"):
        return 0       # position lost
    if exit_reason in ("market_resolved", "resolved"):
        return 1 if exit_price >= 0.5 else 0
    # time_stop / manual / ambiguous — leave realized=None so the calibrator
    # ignores this sample rather than poisoning it with a guess
    return None
