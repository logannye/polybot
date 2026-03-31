import json
import structlog
from datetime import datetime, timezone

log = structlog.get_logger()


class TradeLearner:
    """Per-trade learning hook: fires after every trade close to update analytics."""

    def __init__(self, db, settings):
        self._db = db
        self._settings = settings

    async def on_trade_closed(self, trade_id: int) -> None:
        """Main entry point: called after every trade close."""
        trade = await self._db.fetchrow("SELECT * FROM trades WHERE id = $1", trade_id)
        if not trade:
            return
        analysis = await self._db.fetchrow(
            "SELECT * FROM analyses WHERE id = $1", trade["analysis_id"])

        await self._update_exit_reason_stats(trade)
        await self._update_category_scores(trade)
        await self._update_strategy_avg_edge(trade, analysis)

        log.debug("trade_learned", trade_id=trade_id, strategy=trade.get("strategy"),
                  exit_reason=trade.get("exit_reason"))

    async def _update_exit_reason_stats(self, trade) -> None:
        """Track count/pnl/hold_time per exit reason per strategy in learned_params."""
        strategy = trade.get("strategy", "forecast")
        exit_reason = trade.get("exit_reason") or "unknown"
        pnl = float(trade["pnl"] or 0)
        hold_minutes = 0.0
        if trade.get("opened_at") and trade.get("closed_at"):
            hold_minutes = (trade["closed_at"] - trade["opened_at"]).total_seconds() / 60

        current = await self._db.fetchval(
            "SELECT learned_params FROM strategy_performance WHERE strategy = $1",
            strategy)
        params = json.loads(current) if current and current != '{}' else {}

        exit_stats = params.get("exit_stats", {})
        reason_data = exit_stats.get(exit_reason, {
            "count": 0, "total_pnl": 0.0, "avg_hold_minutes": 0.0,
        })
        n = reason_data["count"]
        reason_data["count"] = n + 1
        reason_data["total_pnl"] = reason_data["total_pnl"] + pnl
        reason_data["avg_hold_minutes"] = (reason_data["avg_hold_minutes"] * n + hold_minutes) / (n + 1)
        exit_stats[exit_reason] = reason_data
        params["exit_stats"] = exit_stats

        await self._db.execute(
            "UPDATE strategy_performance SET learned_params = $1 WHERE strategy = $2",
            json.dumps(params), strategy)

    async def _update_category_scores(self, trade) -> None:
        """Fill the always-empty system_state.category_scores JSONB."""
        market = await self._db.fetchrow(
            "SELECT category FROM markets WHERE id = $1", trade["market_id"])
        if not market:
            return
        category = market["category"]
        pnl = float(trade["pnl"] or 0)

        state = await self._db.fetchrow(
            "SELECT category_scores FROM system_state WHERE id = 1")
        raw = state["category_scores"] if state else None
        scores = json.loads(raw) if isinstance(raw, str) and raw else (raw if isinstance(raw, dict) else {})

        cat_data = scores.get(category, {"trades": 0, "pnl": 0.0, "wins": 0})
        cat_data["trades"] += 1
        cat_data["pnl"] += pnl
        if pnl > 0:
            cat_data["wins"] += 1
        scores[category] = cat_data

        await self._db.execute(
            "UPDATE system_state SET category_scores = $1 WHERE id = 1",
            json.dumps(scores))

    async def _update_strategy_avg_edge(self, trade, analysis) -> None:
        """Fill the always-zero strategy_performance.avg_edge via running average."""
        if not analysis:
            return
        strategy = trade.get("strategy", "forecast")
        edge = float(analysis["edge"])

        current = await self._db.fetchrow(
            "SELECT avg_edge, total_trades FROM strategy_performance WHERE strategy = $1",
            strategy)
        if not current:
            return
        n = current["total_trades"]
        old_avg = float(current["avg_edge"])
        new_avg = (old_avg * max(n - 1, 0) + edge) / max(n, 1)

        await self._db.execute(
            "UPDATE strategy_performance SET avg_edge = $1 WHERE strategy = $2",
            round(new_avg, 4), strategy)
