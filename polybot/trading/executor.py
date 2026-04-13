import structlog
from datetime import datetime, timezone

log = structlog.get_logger()


def compute_limit_price(side: str, best_bid: float, best_ask: float,
                        is_exit: bool = False, cross_spread: bool = False) -> float:
    if is_exit:
        return round(best_bid, 4)
    if cross_spread:
        return round(best_ask, 4)
    spread = best_ask - best_bid
    tick = max(0.001, spread * 0.1)
    price = best_bid + tick
    price = min(price, best_ask)
    return round(price, 4)


class OrderExecutor:
    def __init__(self, scanner, wallet, db, fill_timeout_seconds: int = 120,
                 clob=None, dry_run: bool = False, trade_learner=None):
        self._scanner = scanner
        self._wallet = wallet
        self._db = db
        self._fill_timeout_seconds = fill_timeout_seconds
        self._clob = clob
        self._dry_run = dry_run
        self._trade_learner = trade_learner

    def should_cancel_order(self, elapsed_seconds: float) -> bool:
        return elapsed_seconds > self._fill_timeout_seconds

    async def place_order(self, token_id, side, size_usd, price, market_id, analysis_id,
                          strategy: str = "forecast", kelly_inputs: dict | None = None,
                          post_only: bool = False):
        shares = self._wallet.compute_shares(size_usd, price)
        if shares <= 0:
            return None

        status = "dry_run" if self._dry_run else "open"
        log.info("placing_order", market_id=market_id, side=side, size_usd=size_usd,
                 price=price, shares=shares, strategy=strategy, dry_run=self._dry_run)

        import json as _json
        kelly_json = _json.dumps(kelly_inputs) if kelly_inputs else "{}"
        trade_id = await self._db.fetchval(
            """INSERT INTO trades (market_id, analysis_id, side, entry_price, position_size_usd,
               shares, kelly_inputs, status, strategy)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id""",
            market_id, analysis_id, side, price, size_usd, shares, kelly_json, status, strategy)

        # Lock deployed capital
        await self._db.execute(
            "UPDATE system_state SET total_deployed = total_deployed + $1 WHERE id = 1",
            size_usd)

        clob_order_id = None
        if not self._dry_run and self._clob is not None:
            try:
                clob_order_id = await self._clob.submit_order(
                    token_id=token_id, side=side, price=price, size=shares,
                    post_only=post_only)
                await self._db.execute(
                    "UPDATE trades SET clob_order_id = $1 WHERE id = $2",
                    clob_order_id, trade_id)
            except Exception as e:
                log.error("clob_submit_failed", trade_id=trade_id, error=str(e))
                await self._db.execute(
                    "UPDATE trades SET status = 'cancelled' WHERE id = $1", trade_id)
                await self._db.execute(
                    "UPDATE system_state SET total_deployed = total_deployed - $1 WHERE id = 1",
                    size_usd)
                return None

        return {"trade_id": trade_id, "order_id": clob_order_id, "shares": shares}

    async def place_multi_leg_order(self, legs: list[dict], strategy: str = "arbitrage",
                                    kelly_inputs: dict | None = None,
                                    post_only: bool = False) -> list[dict | None]:
        results = []
        for leg in legs:
            result = await self.place_order(
                token_id=leg["token_id"], side=leg["side"],
                size_usd=leg["size_usd"], price=leg["price"],
                market_id=leg["market_id"], analysis_id=leg.get("analysis_id"),
                strategy=strategy, kelly_inputs=kelly_inputs,
                post_only=post_only)
            results.append(result)
        return results

    async def close_position(self, trade_id, exit_price, exit_reason, shares, entry_price, side):
        if side == "YES":
            pnl = shares * (exit_price - entry_price)
        else:
            pnl = shares * ((1 - exit_price) - (1 - entry_price))
        await self._db.execute(
            """UPDATE trades SET status='closed', exit_price=$1, exit_reason=$2, pnl=$3, closed_at=$4 WHERE id=$5""",
            exit_price, exit_reason, pnl, datetime.now(timezone.utc), trade_id)
        log.info("position_closed", trade_id=trade_id, pnl=pnl, reason=exit_reason)
        return pnl

    async def exit_position(self, trade_id: int, exit_price: float,
                            exit_reason: str) -> float | None:
        """
        Self-contained position exit: looks up trade data, computes PnL,
        updates trade + system_state + strategy_performance.

        Works for both dry_run and live trades.
        """
        trade = await self._db.fetchrow(
            "SELECT * FROM trades WHERE id = $1", trade_id)
        if not trade or trade["status"] not in ("filled", "dry_run"):
            return None

        entry_price = float(trade["entry_price"])
        shares = float(trade["shares"])
        side = trade["side"]
        position_size = float(trade["position_size_usd"])
        strategy = trade.get("strategy", "forecast")

        # PnL: exit_price is the share value we're selling at
        pnl = shares * (exit_price - entry_price)

        now = datetime.now(timezone.utc)
        closed_status = "dry_run_resolved" if trade["status"] == "dry_run" else "closed"

        # For live filled trades, submit sell order
        if trade["status"] == "filled" and not self._dry_run and self._clob:
            market = await self._db.fetchrow(
                "SELECT polymarket_id FROM markets WHERE id = $1", trade["market_id"])
            if market:
                market_data = self._scanner.get_cached_price(market["polymarket_id"])
                if market_data:
                    token_id = market_data.get("yes_token_id") if side == "YES" else market_data.get("no_token_id")
                    if token_id:
                        # Haircut: sell 99.9% of shares to avoid exceeding on-chain balance.
                        # The CLOB fills can settle with slightly fewer shares than computed
                        # due to rounding in on-chain token transfers.
                        sell_size = round(shares * 0.999, 6)
                        sold = False
                        for attempt, size_mult in enumerate([1.0, 0.99], start=1):
                            try:
                                await self._clob.sell_shares(
                                    token_id=token_id, price=exit_price,
                                    size=round(sell_size * size_mult, 6))
                                sold = True
                                break
                            except Exception as e:
                                err = str(e)
                                log.warning("exit_sell_attempt_failed", trade_id=trade_id,
                                            attempt=attempt, size=round(sell_size * size_mult, 6),
                                            error=err)
                                if "not enough balance" not in err.lower():
                                    break  # Non-balance error, don't retry
                        if not sold:
                            # Force-close in DB to free capital. A stuck position blocking
                            # all trading for hours is worse than losing dust.
                            log.error("exit_sell_force_close", trade_id=trade_id,
                                      shares=shares, exit_price=exit_price)
                            pnl = 0.0  # Assume breakeven — actual shares still on-chain

        await self._db.execute(
            """UPDATE trades SET status=$1, exit_price=$2, exit_reason=$3,
               pnl=$4, closed_at=$5 WHERE id=$6""",
            closed_status, exit_price, exit_reason, pnl, now, trade_id)

        # Free deployed capital + update bankroll for dry_run
        if trade["status"] == "dry_run":
            await self._db.execute(
                """UPDATE system_state SET
                   bankroll = bankroll + $1,
                   total_deployed = total_deployed - $2,
                   daily_pnl = daily_pnl + $1
                   WHERE id = 1""",
                pnl, position_size)
        else:
            await self._db.execute(
                "UPDATE system_state SET total_deployed = total_deployed - $1 WHERE id = 1",
                position_size)

        # Update strategy_performance
        await self._db.execute(
            """UPDATE strategy_performance SET
               total_trades = total_trades + 1,
               winning_trades = winning_trades + CASE WHEN $1 > 0 THEN 1 ELSE 0 END,
               total_pnl = total_pnl + $1, last_updated = $2
               WHERE strategy = $3""",
            pnl, now, strategy)

        log.info("position_exited", trade_id=trade_id, pnl=round(pnl, 4),
                 reason=exit_reason, side=side, entry=entry_price,
                 exit=exit_price, strategy=strategy)
        if self._trade_learner:
            try:
                await self._trade_learner.on_trade_closed(trade_id)
            except Exception as e:
                log.error("trade_learning_error", trade_id=trade_id, error=str(e))
        return pnl
