import structlog
from datetime import datetime, timezone
from polybot.learning.calibration import compute_brier_score, update_trust_weight

log = structlog.get_logger()

class TradeRecorder:
    def __init__(self, db, cold_start_trades=30, brier_ema_alpha=0.15):
        self._db = db
        self._cold_start_trades = cold_start_trades
        self._brier_ema_alpha = brier_ema_alpha

    async def record_resolution(self, trade_id: int, outcome: int) -> None:
        trade = await self._db.fetchrow("SELECT * FROM trades WHERE id = $1", trade_id)
        if not trade:
            return
        analysis = await self._db.fetchrow("SELECT * FROM analyses WHERE id = $1", trade["analysis_id"])
        if not analysis:
            return
        if trade["side"] == "YES":
            pnl = trade["shares"] * (outcome - trade["entry_price"])
        else:
            pnl = trade["shares"] * ((1 - outcome) - (1 - trade["entry_price"]))
        await self._db.execute(
            "UPDATE trades SET status='closed', exit_price=$1, exit_reason='resolution', pnl=$2, closed_at=$3 WHERE id=$4",
            float(outcome), pnl, datetime.now(timezone.utc), trade_id)

        # Update strategy performance
        await self._db.execute(
            """UPDATE strategy_performance SET
               total_trades = total_trades + 1,
               winning_trades = winning_trades + CASE WHEN $1 > 0 THEN 1 ELSE 0 END,
               total_pnl = total_pnl + $1,
               last_updated = $2
               WHERE strategy = $3""",
            pnl, datetime.now(timezone.utc), trade.get("strategy", "forecast"))

        for est in analysis["model_estimates"]:
            brier = compute_brier_score(est["probability"], outcome)
            model_perf = await self._db.fetchrow("SELECT * FROM model_performance WHERE model_name = $1", est["model"])
            if model_perf:
                new_ema = update_trust_weight(float(model_perf["brier_score_ema"]), brier, alpha=self._brier_ema_alpha)
                await self._db.execute(
                    "UPDATE model_performance SET brier_score_ema=$1, resolved_count=resolved_count+1, last_updated=$2 WHERE model_name=$3",
                    new_ema, datetime.now(timezone.utc), est["model"])

        models = await self._db.fetch("SELECT * FROM model_performance")
        total_resolved = sum(float(m["resolved_count"]) for m in models)

        # Rebalance trust weights after every resolution (Brier EMA provides smoothing)
        # During cold start (< min trades), keep equal weights for stability
        if total_resolved >= self._cold_start_trades:
            total_inv = sum(1.0 / max(float(m["brier_score_ema"]), 0.01) for m in models)
            for m in models:
                weight = (1.0 / max(float(m["brier_score_ema"]), 0.01)) / total_inv
                await self._db.execute("UPDATE model_performance SET trust_weight=$1 WHERE model_name=$2", weight, m["model_name"])

        log.info("resolution_recorded", trade_id=trade_id, outcome=outcome, pnl=pnl)
