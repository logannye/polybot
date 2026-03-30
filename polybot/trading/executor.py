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
                 clob=None, dry_run: bool = False):
        self._scanner = scanner
        self._wallet = wallet
        self._db = db
        self._fill_timeout_seconds = fill_timeout_seconds
        self._clob = clob
        self._dry_run = dry_run

    def should_cancel_order(self, elapsed_seconds: float) -> bool:
        return elapsed_seconds > self._fill_timeout_seconds

    async def place_order(self, token_id, side, size_usd, price, market_id, analysis_id,
                          strategy: str = "forecast", kelly_inputs: dict | None = None):
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
                    token_id=token_id, side=side, price=price, size=shares)
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
                                    kelly_inputs: dict | None = None) -> list[dict | None]:
        results = []
        for leg in legs:
            result = await self.place_order(
                token_id=leg["token_id"], side=leg["side"],
                size_usd=leg["size_usd"], price=leg["price"],
                market_id=leg["market_id"], analysis_id=leg.get("analysis_id"),
                strategy=strategy, kelly_inputs=kelly_inputs)
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
