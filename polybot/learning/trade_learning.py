import json
import structlog
from datetime import datetime, timezone

from polybot.learning.calibration import compute_brier_score, update_trust_weight

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
        await self._update_proxy_trust_weights(trade, analysis)

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

    async def _update_proxy_trust_weights(self, trade, analysis) -> None:
        """Use trade outcome (TP/SL/early-exit) as proxy for model accuracy."""
        if not getattr(self._settings, 'enable_proxy_trust_learning', True):
            return
        if trade.get("exit_reason") == "resolution":
            return  # Already handled by TradeRecorder with hard outcomes

        model_estimates = []
        if analysis and analysis.get("model_estimates"):
            raw = analysis["model_estimates"]
            model_estimates = json.loads(raw) if isinstance(raw, str) else raw
        if not model_estimates:
            return  # Snipe trades have no model estimates

        exit_reason = trade.get("exit_reason", "")
        pnl = float(trade["pnl"] or 0)

        if exit_reason == "take_profit":
            proxy_outcome = 1.0 if trade["side"] == "YES" else 0.0
            proxy_alpha = self._settings.proxy_brier_alpha_tp
        elif exit_reason == "stop_loss":
            proxy_outcome = 0.0 if trade["side"] == "YES" else 1.0
            proxy_alpha = self._settings.proxy_brier_alpha_sl
        elif exit_reason in ("early_exit", "time_stop"):
            if pnl > 0:
                proxy_outcome = 1.0 if trade["side"] == "YES" else 0.0
            else:
                proxy_outcome = 0.0 if trade["side"] == "YES" else 1.0
            proxy_alpha = self._settings.proxy_brier_alpha_weak
        else:
            return

        for est in model_estimates:
            prob = est.get("probability")
            model_name = est.get("model")
            if prob is None or model_name is None:
                continue
            brier = compute_brier_score(float(prob), int(proxy_outcome))
            model_perf = await self._db.fetchrow(
                "SELECT * FROM model_performance WHERE model_name = $1", model_name)
            if model_perf:
                new_ema = update_trust_weight(
                    float(model_perf["brier_score_ema"]), brier, alpha=proxy_alpha)
                await self._db.execute(
                    """UPDATE model_performance SET brier_score_ema=$1,
                       resolved_count=resolved_count+1, last_updated=$2
                       WHERE model_name=$3""",
                    new_ema, datetime.now(timezone.utc), model_name)

        await self._rebalance_trust_weights()

    async def compute_optimal_thresholds(self) -> None:
        """Analyze recent trades to find optimal TP/SL thresholds per strategy."""
        if not getattr(self._settings, 'enable_adaptive_thresholds', True):
            return
        min_trades = getattr(self._settings, 'adaptive_threshold_min_trades', 10)

        for strategy in ("snipe", "forecast"):
            trades = await self._db.fetch(
                """SELECT t.exit_reason, t.pnl, t.entry_price, t.exit_price, t.side,
                          t.opened_at, t.closed_at
                   FROM trades t
                   WHERE t.strategy = $1
                     AND t.status IN ('closed', 'dry_run_resolved')
                     AND t.closed_at > NOW() - INTERVAL '14 days'""",
                strategy)

            if len(trades) < min_trades:
                continue

            returns_at_exit = []
            for t in trades:
                entry = float(t["entry_price"])
                exit_p = float(t["exit_price"] or 0)
                if entry <= 0:
                    continue
                if t["side"] == "YES":
                    ret = (exit_p - entry) / entry
                else:
                    no_entry = 1 - entry
                    no_exit = 1 - exit_p
                    ret = (no_exit - no_entry) / no_entry if no_entry > 0 else 0
                returns_at_exit.append({
                    "return": ret,
                    "pnl": float(t["pnl"] or 0),
                })

            if not returns_at_exit:
                continue

            # Find optimal TP: threshold that maximizes frequency-weighted expected value
            default_tp = getattr(self._settings, 'take_profit_threshold', 0.30)
            best_tp, best_tp_ev = default_tp, 0.0
            for tp_pct in range(10, 55, 5):
                tp_test = tp_pct / 100.0
                wins = [r for r in returns_at_exit if r["return"] >= tp_test]
                if len(wins) < 3:
                    continue
                avg_win_pnl = sum(r["pnl"] for r in wins) / len(wins)
                freq = len(wins) / len(returns_at_exit)
                ev = avg_win_pnl * freq
                if ev > best_tp_ev:
                    best_tp_ev = ev
                    best_tp = tp_test

            # Find optimal SL: threshold that minimizes frequency-weighted loss
            default_sl = getattr(self._settings, 'stop_loss_threshold', 0.25)
            best_sl, best_sl_cost = default_sl, float('inf')
            for sl_pct in range(10, 45, 5):
                sl_test = sl_pct / 100.0
                losses = [r for r in returns_at_exit if r["return"] <= -sl_test]
                if len(losses) < 3:
                    continue
                avg_loss = abs(sum(r["pnl"] for r in losses)) / len(losses)
                freq = len(losses) / len(returns_at_exit)
                cost = avg_loss * freq
                if cost < best_sl_cost:
                    best_sl_cost = cost
                    best_sl = sl_test

            # Clamp to safe ranges
            best_tp = max(0.10, min(0.50, best_tp))
            best_sl = max(0.10, min(0.40, best_sl))

            # Store in strategy_performance.learned_params
            current = await self._db.fetchval(
                "SELECT learned_params FROM strategy_performance WHERE strategy = $1",
                strategy)
            params = json.loads(current) if current and current != '{}' else {}
            params["take_profit_threshold"] = round(best_tp, 2)
            params["stop_loss_threshold"] = round(best_sl, 2)
            params["threshold_sample_size"] = len(returns_at_exit)
            params["threshold_updated_at"] = datetime.now(timezone.utc).isoformat()

            await self._db.execute(
                "UPDATE strategy_performance SET learned_params = $1 WHERE strategy = $2",
                json.dumps(params), strategy)

            log.info("adaptive_thresholds", strategy=strategy,
                     tp=best_tp, sl=best_sl, sample_size=len(returns_at_exit))

    async def _rebalance_trust_weights(self) -> None:
        """Rebalance trust weights across all models (inverse Brier EMA)."""
        models = await self._db.fetch("SELECT * FROM model_performance")
        if not models:
            return
        total_inv = sum(1.0 / max(float(m["brier_score_ema"]), 0.01) for m in models)
        for m in models:
            weight = (1.0 / max(float(m["brier_score_ema"]), 0.01)) / total_inv
            await self._db.execute(
                "UPDATE model_performance SET trust_weight=$1 WHERE model_name=$2",
                weight, m["model_name"])

    async def compute_snipe_params(self) -> None:
        """Analyze snipe trade performance to find optimal edge threshold."""
        if not getattr(self._settings, 'enable_snipe_learning', True):
            return

        trades = await self._db.fetch(
            """SELECT t.pnl, t.entry_price, t.exit_reason, t.side,
                      t.opened_at, t.closed_at, a.edge
               FROM trades t
               JOIN analyses a ON t.analysis_id = a.id
               WHERE t.strategy = 'snipe'
                 AND t.status IN ('closed', 'dry_run_resolved')
                 AND t.closed_at > NOW() - INTERVAL '14 days'""")

        if len(trades) < 5:
            return

        # Bucket by edge level (0.05 increments)
        edge_buckets = {}
        for t in trades:
            edge = float(t["edge"])
            bucket = round(edge * 20) / 20
            if bucket not in edge_buckets:
                edge_buckets[bucket] = {"count": 0, "total_pnl": 0.0}
            edge_buckets[bucket]["count"] += 1
            edge_buckets[bucket]["total_pnl"] += float(t["pnl"] or 0)

        # Find minimum profitable edge bucket
        sorted_edges = sorted(edge_buckets.keys())
        default_min_edge = getattr(self._settings, 'snipe_min_net_edge', 0.02)
        optimal_min_edge = default_min_edge
        for edge_key in sorted_edges:
            bucket = edge_buckets[edge_key]
            if bucket["count"] >= 3 and bucket["total_pnl"] > 0:
                optimal_min_edge = edge_key
                break

        optimal_min_edge = max(0.01, min(0.10, optimal_min_edge))

        # Bucket by price level (0.10 increments)
        price_buckets = {}
        for t in trades:
            price = float(t["entry_price"])
            bucket = round(price * 10) / 10
            if bucket not in price_buckets:
                price_buckets[bucket] = {"count": 0, "total_pnl": 0.0, "wins": 0}
            price_buckets[bucket]["count"] += 1
            price_buckets[bucket]["total_pnl"] += float(t["pnl"] or 0)
            if float(t["pnl"] or 0) > 0:
                price_buckets[bucket]["wins"] += 1

        # Store learned params
        current = await self._db.fetchval(
            "SELECT learned_params FROM strategy_performance WHERE strategy = 'snipe'")
        params = json.loads(current) if current and current != '{}' else {}
        params["optimal_min_edge"] = round(optimal_min_edge, 3)
        params["edge_buckets"] = {str(k): v for k, v in edge_buckets.items()}
        params["price_buckets"] = {str(k): v for k, v in price_buckets.items()}
        params["snipe_sample_size"] = len(trades)
        params["snipe_params_updated_at"] = datetime.now(timezone.utc).isoformat()

        await self._db.execute(
            "UPDATE strategy_performance SET learned_params = $1 WHERE strategy = 'snipe'",
            json.dumps(params))

        log.info("snipe_params_learned", optimal_min_edge=optimal_min_edge,
                 sample_size=len(trades))
