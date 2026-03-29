import asyncio
import resource
import structlog
from datetime import datetime, timezone, timedelta
from polybot.strategies.base import Strategy, TradingContext
from polybot.trading.risk import PortfolioState

log = structlog.get_logger()


class Engine:
    def __init__(self, db, scanner, researcher, ensemble, executor, recorder,
                 risk_manager, settings, email_notifier, position_manager, clob=None):
        self._db = db
        self._scanner = scanner
        self._researcher = researcher
        self._ensemble = ensemble
        self._executor = executor
        self._recorder = recorder
        self._risk = risk_manager
        self._settings = settings
        self._email = email_notifier
        self._position_manager = position_manager
        self._clob = clob
        self._context = TradingContext(
            db=db, scanner=scanner, risk_manager=risk_manager,
            portfolio_lock=asyncio.Lock(), executor=executor,
            email_notifier=email_notifier, settings=settings)
        self._strategies: list[Strategy] = []
        self._last_heartbeats: dict[str, datetime] = {}
        self._last_self_assess: datetime | None = None

    def add_strategy(self, strategy: Strategy) -> None:
        self._strategies.append(strategy)

    async def run_forever(self):
        log.info("engine_starting", strategies=[s.name for s in self._strategies])
        await self._reconcile_on_startup()
        tasks = [self._run_strategy(s) for s in self._strategies]
        tasks.append(self._run_periodic(self._health_check, self._settings.health_check_interval))
        tasks.append(self._run_periodic(self._maybe_self_assess, 60))
        if not self._settings.dry_run and self._clob:
            tasks.append(self._run_periodic(self._fill_monitor, 30))
        tasks.append(self._run_periodic(self._resolution_monitor, 60))
        await asyncio.gather(*tasks)

    async def _run_strategy(self, strategy: Strategy):
        consecutive_errors = 0
        while True:
            try:
                await strategy.run_once(self._context)
                consecutive_errors = 0
                self._last_heartbeats[strategy.name] = datetime.now(timezone.utc)
            except Exception as e:
                consecutive_errors += 1
                log.error("strategy_error", strategy=strategy.name,
                          error=str(e), consecutive=consecutive_errors)
                if consecutive_errors >= 5:
                    log.critical("strategy_disabled", strategy=strategy.name)
                    await self._context.email_notifier.send(
                        f"[POLYBOT CRITICAL] {strategy.name} disabled",
                        f"Strategy {strategy.name} disabled after 5 consecutive errors: {e}")
                    return
            await asyncio.sleep(strategy.interval_seconds)

    async def _run_periodic(self, func, interval_seconds):
        while True:
            try:
                await func()
            except Exception as e:
                log.error("periodic_error", func=func.__name__, error=str(e))
            await asyncio.sleep(interval_seconds)

    async def _reconcile_on_startup(self):
        try:
            open_trades = await self._db.fetch("SELECT * FROM trades WHERE status = 'open'")
            for trade in open_trades:
                market = await self._db.fetchrow(
                    "SELECT * FROM markets WHERE id = $1", trade["market_id"])
                if market and market["resolution_time"] < datetime.now(timezone.utc):
                    resolved = await self._scanner.fetch_market_resolution(
                        market["polymarket_id"])
                    if resolved is not None:
                        await self._recorder.record_resolution(trade["id"], resolved)
                        await self._db.execute(
                            "UPDATE system_state SET total_deployed = total_deployed - $1 WHERE id = 1",
                            float(trade["position_size_usd"]))
                        log.info("reconciled_stale_trade", trade_id=trade["id"], outcome=resolved)
            # Sync bankroll from CLOB on startup (live mode only)
            if not self._settings.dry_run and self._clob:
                try:
                    balance = await self._clob.get_balance()
                    await self._db.execute("UPDATE system_state SET bankroll = $1 WHERE id = 1", balance)
                    log.info("startup_bankroll_synced", balance=balance)
                except Exception as e:
                    log.error("startup_bankroll_sync_failed", error=str(e))
        except Exception as e:
            log.error("reconciliation_error", error=str(e))

    async def _health_check(self):
        now = datetime.now(timezone.utc)
        for name, last in self._last_heartbeats.items():
            elapsed = (now - last).total_seconds()
            if elapsed > self._settings.heartbeat_critical_seconds:
                await self._context.email_notifier.send(
                    f"[POLYBOT CRITICAL] {name} unresponsive",
                    f"Strategy {name} has not completed a cycle in {elapsed:.0f}s")
            elif elapsed > self._settings.heartbeat_warn_seconds:
                log.warning("heartbeat_warn", strategy=name, elapsed=elapsed)
        try:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            rss_mb = usage.ru_maxrss / 1024 / 1024
            if rss_mb > 512:
                log.warning("high_memory", rss_mb=rss_mb)
        except Exception:
            pass

        # Stale positions check
        try:
            open_trades = await self._db.fetch(
                """SELECT t.id, t.opened_at, m.resolution_time
                   FROM trades t JOIN markets m ON t.market_id = m.id
                   WHERE t.status = 'open'""")
            for t in open_trades:
                total_duration = (t["resolution_time"] - t["opened_at"]).total_seconds()
                elapsed = (datetime.now(timezone.utc) - t["opened_at"]).total_seconds()
                if total_duration > 0 and elapsed / total_duration > 0.80:
                    log.warning("stale_position", trade_id=t["id"],
                                pct_elapsed=elapsed / total_duration)
        except Exception as e:
            log.error("stale_check_failed", error=str(e))

    async def _fill_monitor(self):
        open_orders = await self._db.fetch(
            "SELECT * FROM trades WHERE status = 'open' AND clob_order_id IS NOT NULL")
        for trade in open_orders:
            try:
                status = await self._clob.get_order_status(trade["clob_order_id"])
            except Exception as e:
                log.error("fill_check_failed", trade_id=trade["id"], error=str(e))
                continue
            if status["status"] == "matched":
                await self._db.execute("UPDATE trades SET status = 'filled' WHERE id = $1", trade["id"])
                try:
                    balance = await self._clob.get_balance()
                    await self._db.execute("UPDATE system_state SET bankroll = $1 WHERE id = 1", balance)
                except Exception as e:
                    log.error("bankroll_sync_failed", error=str(e))
                await self._context.email_notifier.send(
                    f"Trade filled: order {trade['clob_order_id']}",
                    f"<p>Trade #{trade['id']} filled. Strategy: {trade['strategy']}</p>")
                log.info("order_filled", trade_id=trade["id"], clob_order_id=trade["clob_order_id"])
            elif status["status"] == "cancelled":
                await self._db.execute("UPDATE trades SET status = 'cancelled' WHERE id = $1", trade["id"])
                await self._db.execute(
                    "UPDATE system_state SET total_deployed = total_deployed - $1 WHERE id = 1",
                    float(trade["position_size_usd"]))
                log.info("order_cancelled_externally", trade_id=trade["id"])
            elif status["status"] == "live":
                elapsed = (datetime.now(timezone.utc) - trade["opened_at"]).total_seconds()
                timeout = (self._settings.arb_fill_timeout_seconds if trade["strategy"] == "arbitrage"
                           else self._settings.fill_timeout_seconds)
                if elapsed > timeout:
                    await self._clob.cancel_order(trade["clob_order_id"])
                    await self._db.execute("UPDATE trades SET status = 'cancelled' WHERE id = $1", trade["id"])
                    await self._db.execute(
                        "UPDATE system_state SET total_deployed = total_deployed - $1 WHERE id = 1",
                        float(trade["position_size_usd"]))
                    log.info("order_timed_out", trade_id=trade["id"], elapsed=elapsed)

    async def _resolution_monitor(self):
        resolvable = await self._db.fetch(
            "SELECT * FROM trades WHERE status IN ('filled', 'dry_run')")
        now = datetime.now(timezone.utc)
        for trade in resolvable:
            market = await self._db.fetchrow(
                "SELECT * FROM markets WHERE id = $1", trade["market_id"])
            if not market or market["resolution_time"] > now:
                continue
            try:
                outcome = await self._scanner.fetch_market_resolution(market["polymarket_id"])
            except Exception as e:
                log.error("resolution_check_failed", trade_id=trade["id"], error=str(e))
                continue
            if outcome is None:
                continue
            if trade["status"] == "filled":
                await self._recorder.record_resolution(trade["id"], outcome)
                await self._db.execute(
                    "UPDATE system_state SET total_deployed = total_deployed - $1 WHERE id = 1",
                    float(trade["position_size_usd"]))
                # Update daily_pnl
                resolved_trade = await self._db.fetchrow("SELECT pnl FROM trades WHERE id = $1", trade["id"])
                if resolved_trade and resolved_trade["pnl"] is not None:
                    await self._db.execute(
                        "UPDATE system_state SET daily_pnl = daily_pnl + $1 WHERE id = 1",
                        float(resolved_trade["pnl"]))
                if self._clob:
                    try:
                        balance = await self._clob.get_balance()
                        await self._db.execute("UPDATE system_state SET bankroll = $1 WHERE id = 1", balance)
                    except Exception as e:
                        log.error("bankroll_sync_failed", error=str(e))
                log.info("trade_resolved", trade_id=trade["id"], outcome=outcome)
            elif trade["status"] == "dry_run":
                entry = float(trade["entry_price"])
                shares = float(trade["shares"])
                if trade["side"] == "YES":
                    pnl = shares * (outcome - entry)
                else:
                    pnl = shares * ((1 - outcome) - (1 - entry))
                await self._db.execute(
                    """UPDATE trades SET status='dry_run_resolved', pnl=$1, exit_price=$2,
                       exit_reason='resolution', closed_at=$3 WHERE id=$4""",
                    pnl, float(outcome), now, trade["id"])
                await self._db.execute(
                    """UPDATE system_state SET bankroll = bankroll + $1,
                       total_deployed = total_deployed - $2, daily_pnl = daily_pnl + $1
                       WHERE id = 1""",
                    pnl, float(trade["position_size_usd"]))
                log.info("dry_run_resolved", trade_id=trade["id"], outcome=outcome, simulated_pnl=pnl)

    async def _maybe_self_assess(self):
        now = datetime.now(timezone.utc)
        if now.hour != 0:
            return
        if self._last_self_assess and (now - self._last_self_assess).total_seconds() < 82800:
            return

        from polybot.learning.self_assess import (
            suggest_kelly_adjustment, suggest_edge_threshold, check_strategy_kill_switch)
        from polybot.notifications.email import format_daily_report

        state = await self._db.fetchrow("SELECT * FROM system_state WHERE id = 1")
        if not state:
            return

        # Kelly adjustment
        trades = await self._db.fetch(
            "SELECT pnl FROM trades WHERE status='closed' AND closed_at > NOW() - INTERVAL '7 days'")
        cumulative, peak, max_dd = 0.0, 0.0, 0.0
        for t in trades:
            cumulative += float(t["pnl"] or 0)
            peak = max(peak, cumulative)
            dd = (peak - cumulative) / max(float(state["bankroll"]), 1)
            max_dd = max(max_dd, dd)

        new_kelly = suggest_kelly_adjustment(float(state["kelly_mult"]), max_dd)

        # Edge threshold
        edge_trades = await self._db.fetch(
            """SELECT a.edge, t.pnl FROM trades t JOIN analyses a ON t.analysis_id = a.id
               WHERE t.status='closed' AND t.closed_at > NOW() - INTERVAL '7 days'""")
        buckets: dict[float, dict] = {}
        for t in edge_trades:
            bucket_key = round(float(t["edge"]) * 20) / 20
            if bucket_key not in buckets:
                buckets[bucket_key] = {"count": 0, "total_pnl": 0.0}
            buckets[bucket_key]["count"] += 1
            buckets[bucket_key]["total_pnl"] += float(t["pnl"] or 0)
        new_edge = suggest_edge_threshold(float(state["edge_threshold"]), buckets)

        await self._db.execute(
            "UPDATE system_state SET kelly_mult=$1, edge_threshold=$2 WHERE id=1",
            new_kelly, new_edge)

        # Strategy kill switch
        strat_rows = await self._db.fetch("SELECT * FROM strategy_performance")
        for s in strat_rows:
            should_kill = check_strategy_kill_switch(
                s["total_trades"], float(s["total_pnl"]),
                self._settings.strategy_kill_min_trades)
            if should_kill and s["enabled"]:
                await self._db.execute(
                    "UPDATE strategy_performance SET enabled = FALSE WHERE strategy = $1",
                    s["strategy"])
                await self._context.email_notifier.send(
                    f"[POLYBOT WARNING] {s['strategy']} strategy killed",
                    f"Strategy {s['strategy']} disabled: negative P&L over {s['total_trades']} trades")

        # Circuit breaker check
        portfolio = PortfolioState(
            bankroll=float(state["bankroll"]),
            total_deployed=float(state["total_deployed"]),
            daily_pnl=float(state["daily_pnl"]),
            open_count=0, category_deployed={},
            circuit_breaker_until=state.get("circuit_breaker_until"))
        triggered, until = self._risk.check_circuit_breaker(portfolio)
        if triggered:
            post_breaker_until = until + timedelta(hours=self._settings.post_breaker_cooldown_hours)
            await self._db.execute(
                "UPDATE system_state SET circuit_breaker_until=$1, post_breaker_until=$2 WHERE id=1",
                until, post_breaker_until)

        # Daily report
        day_trades = await self._db.fetch(
            """SELECT t.*, m.question FROM trades t JOIN markets m ON t.market_id = m.id
               WHERE t.closed_at > NOW() - INTERVAL '24 hours'""")
        strategy_breakdown = []
        for strat_name in ("arbitrage", "snipe", "forecast"):
            strat_trades = [t for t in day_trades if t.get("strategy") == strat_name]
            wins = sum(1 for t in strat_trades if t["pnl"] and float(t["pnl"]) > 0)
            losses = len(strat_trades) - wins
            pnl = sum(float(t["pnl"] or 0) for t in strat_trades)
            strategy_breakdown.append({
                "strategy": strat_name, "trades": len(strat_trades),
                "pnl": pnl, "wins": wins, "losses": losses})

        models = await self._db.fetch("SELECT * FROM model_performance")
        open_positions = await self._db.fetch(
            """SELECT t.side, t.entry_price, t.position_size_usd, m.question
               FROM trades t JOIN markets m ON t.market_id = m.id WHERE t.status = 'open'""")

        first_trade = await self._db.fetchval("SELECT MIN(opened_at) FROM trades")
        days_running = max(1, (now - first_trade).days) if first_trade else 1

        strat_statuses = []
        for s in strat_rows:
            status = "active" if s["enabled"] else "disabled"
            strat_statuses.append(f"{s['strategy']}: {status}")
        strategies_status = ", ".join(strat_statuses) if strat_statuses else "all active"

        report = format_daily_report(
            date=now.strftime("%Y-%m-%d"),
            starting_bankroll=float(state["bankroll"]) - sum(float(t["pnl"] or 0) for t in day_trades),
            ending_bankroll=float(state["bankroll"]),
            strategy_breakdown=strategy_breakdown,
            total_trades_cumulative=sum(s["total_trades"] for s in strat_rows),
            total_pnl_cumulative=sum(float(s["total_pnl"]) for s in strat_rows),
            days_running=days_running,
            model_performance=[
                {"model": m["model_name"], "brier": float(m["brier_score_ema"]),
                 "trust": float(m["trust_weight"])} for m in models],
            open_positions=[
                {"question": p["question"], "side": p["side"],
                 "price": float(p["entry_price"]), "size": float(p["position_size_usd"])}
                for p in open_positions],
            api_errors=0, strategies_status=strategies_status)

        await self._context.email_notifier.send(
            f"[POLYBOT] Daily Report — {now.strftime('%Y-%m-%d')}", f"<pre>{report}</pre>")

        # Reset daily P&L for next day
        await self._db.execute("UPDATE system_state SET daily_pnl = 0 WHERE id = 1")

        self._last_self_assess = now
        log.info("self_assessment_complete", kelly=new_kelly, edge=new_edge, max_dd=max_dd)
