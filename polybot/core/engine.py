import asyncio
import structlog
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from polybot.markets.filters import MarketCandidate, filter_markets
from polybot.trading.kelly import compute_kelly, compute_position_size
from polybot.trading.risk import PortfolioState, TradeProposal, RiskManager
from polybot.analysis.quant import (
    compute_line_movement,
    compute_volume_spike,
    compute_book_imbalance,
    compute_spread_signal,
    compute_time_decay,
    compute_composite_score,
    QuantSignals,
)

log = structlog.get_logger()


@dataclass
class CycleResult:
    skipped: bool = False
    reason: str = ""
    markets_scanned: int = 0
    markets_analyzed: int = 0
    trades_placed: int = 0
    errors: list[str] = field(default_factory=list)


class Engine:
    def __init__(self, db, scanner, researcher, ensemble, executor, recorder, risk_manager, settings):
        self._db = db
        self._scanner = scanner
        self._researcher = researcher
        self._ensemble = ensemble
        self._executor = executor
        self._recorder = recorder
        self._risk = risk_manager
        self._settings = settings

    async def run_cycle(self) -> CycleResult:
        result = CycleResult()
        state_row = await self._db.fetchrow("SELECT * FROM system_state WHERE id = 1")

        if not state_row:
            log.error("no_system_state")
            result.skipped = True
            result.reason = "no_system_state"
            return result

        # Check circuit breaker
        cb_until = state_row["circuit_breaker_until"]
        if cb_until and cb_until > datetime.now(timezone.utc):
            log.info("circuit_breaker_active", until=cb_until)
            result.skipped = True
            result.reason = "circuit_breaker"
            return result

        bankroll = float(state_row["bankroll"])
        total_deployed = float(state_row["total_deployed"])
        daily_pnl = float(state_row["daily_pnl"])
        kelly_mult = float(state_row["kelly_mult"])
        edge_threshold = float(state_row.get("edge_threshold", self._settings.edge_threshold))

        # SCAN
        raw_markets = await self._scanner.fetch_markets()
        result.markets_scanned = len(raw_markets)

        # Get existing analysis timestamps for cooldown
        candidates = []
        for m in raw_markets:
            last_analysis = await self._db.fetchrow(
                "SELECT timestamp, ensemble_probability FROM analyses a "
                "JOIN markets mk ON a.market_id = mk.id "
                "WHERE mk.polymarket_id = $1 ORDER BY a.timestamp DESC LIMIT 1",
                m["polymarket_id"],
            )
            candidates.append(MarketCandidate(
                polymarket_id=m["polymarket_id"],
                question=m["question"],
                category=m["category"],
                resolution_time=m["resolution_time"],
                current_price=m["yes_price"],
                book_depth=m.get("book_depth", 0),
                last_analyzed_at=last_analysis["timestamp"] if last_analysis else None,
                previous_price=float(last_analysis["ensemble_probability"]) if last_analysis else None,
            ))

        # FILTER
        filtered = filter_markets(
            candidates,
            resolution_hours_max=self._settings.resolution_hours_max,
            min_book_depth=self._settings.min_book_depth,
            min_price=self._settings.min_price,
            max_price=self._settings.max_price,
            cooldown_minutes=self._settings.cooldown_minutes,
            price_move_threshold=self._settings.price_move_threshold,
        )

        # Get portfolio state for risk checks
        open_trades = await self._db.fetch("SELECT * FROM trades WHERE status = 'open'")
        cat_deployed: dict[str, float] = {}
        for t in open_trades:
            mkt = await self._db.fetchrow("SELECT category FROM markets WHERE id = $1", t["market_id"])
            if mkt:
                cat = mkt["category"]
                cat_deployed[cat] = cat_deployed.get(cat, 0) + float(t["position_size_usd"])

        portfolio = PortfolioState(
            bankroll=bankroll,
            total_deployed=total_deployed,
            daily_pnl=daily_pnl,
            open_count=len(open_trades),
            category_deployed=cat_deployed,
            circuit_breaker_until=cb_until,
        )

        # Check circuit breaker trigger
        triggered, until = self._risk.check_circuit_breaker(portfolio)
        if triggered:
            await self._db.execute(
                "UPDATE system_state SET circuit_breaker_until = $1 WHERE id = 1", until,
            )
            result.skipped = True
            result.reason = "circuit_breaker_triggered"
            return result

        # Get model trust weights
        model_rows = await self._db.fetch("SELECT model_name, trust_weight FROM model_performance")
        trust_weights = {r["model_name"]: float(r["trust_weight"]) for r in model_rows}

        # ANALYZE + SCORE + SIZE + EXECUTE (top 10 candidates)
        for candidate in filtered[:10]:
            try:
                await self._analyze_and_trade(
                    candidate, trust_weights, bankroll, kelly_mult,
                    edge_threshold, portfolio,
                )
                result.markets_analyzed += 1
            except Exception as e:
                log.error("analysis_error", market=candidate.polymarket_id, error=str(e))
                result.errors.append(str(e))

        # Update last scan time
        await self._db.execute(
            "UPDATE system_state SET last_scan_at = $1 WHERE id = 1",
            datetime.now(timezone.utc),
        )

        log.info(
            "cycle_complete",
            scanned=result.markets_scanned,
            analyzed=result.markets_analyzed,
            trades=result.trades_placed,
        )
        return result

    async def _analyze_and_trade(
        self, candidate, trust_weights, bankroll, kelly_mult,
        edge_threshold, portfolio,
    ):
        # Web research
        research = await self._researcher.search(candidate.question)

        # Ensemble analysis + quant signals in parallel
        ensemble_result, quant = await asyncio.gather(
            self._ensemble.analyze(candidate.question, research, trust_weights),
            self._compute_quant(candidate),
        )

        composite = compute_composite_score(quant, self._settings.quant_weights)

        # Skip if quant says bad timing
        if composite < -0.3:
            log.info("quant_skip", market=candidate.polymarket_id, score=composite)
            return

        # Score
        kelly_result = compute_kelly(ensemble_result.ensemble_probability, candidate.current_price)
        if kelly_result.edge < edge_threshold:
            log.debug("low_edge", market=candidate.polymarket_id, edge=kelly_result.edge)
            return

        # Confidence multiplier
        conf_mult = self._risk.confidence_multiplier(
            stdev=ensemble_result.stdev,
            quant_score=composite,
            stdev_low=self._settings.ensemble_stdev_low,
            stdev_high=self._settings.ensemble_stdev_high,
            mult_low=self._settings.confidence_mult_low,
            mult_mid=self._settings.confidence_mult_mid,
            mult_high=self._settings.confidence_mult_high,
            quant_neg_mult=self._settings.quant_negative_mult,
        )

        # Size
        size = compute_position_size(
            bankroll=bankroll,
            kelly_fraction=kelly_result.kelly_fraction,
            kelly_mult=kelly_mult,
            confidence_mult=conf_mult,
            max_single_pct=self._settings.max_single_position_pct,
            min_trade_size=self._settings.min_trade_size,
        )

        if size <= 0:
            return

        # Upsert market
        market_id = await self._db.fetchval(
            """INSERT INTO markets (polymarket_id, question, category, resolution_time, current_price)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (polymarket_id) DO UPDATE SET current_price=$5, last_updated=NOW()
               RETURNING id""",
            candidate.polymarket_id, candidate.question, candidate.category,
            candidate.resolution_time, candidate.current_price,
        )

        # Record analysis
        analysis_id = await self._db.fetchval(
            """INSERT INTO analyses (market_id, model_estimates, ensemble_probability,
               ensemble_stdev, quant_signals, edge, web_research_summary)
               VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
            market_id,
            [{"model": e.model, "probability": e.probability,
              "confidence": e.confidence, "reasoning": e.reasoning}
             for e in ensemble_result.estimates],
            ensemble_result.ensemble_probability,
            ensemble_result.stdev,
            {"composite": composite, "line_movement": quant.line_movement,
             "volume_spike": quant.volume_spike, "book_imbalance": quant.book_imbalance,
             "spread": quant.spread, "time_decay": quant.time_decay},
            kelly_result.edge,
            research,
        )

        # Risk check
        proposal = TradeProposal(
            size_usd=size,
            category=candidate.category,
            book_depth=candidate.book_depth,
        )
        risk_result = self._risk.check(portfolio, proposal)
        if not risk_result.allowed:
            log.info("risk_rejected", market=candidate.polymarket_id, reason=risk_result.reason)
            return

        # Execute
        log.info(
            "trading",
            market=candidate.polymarket_id,
            side=kelly_result.side,
            size=size,
            edge=kelly_result.edge,
            ensemble_prob=ensemble_result.ensemble_probability,
        )

    async def _compute_quant(self, candidate) -> QuantSignals:
        # Fetch price history and order book data
        try:
            price_history = await self._scanner.fetch_price_history(
                candidate.polymarket_id
            )
            book = await self._scanner.fetch_order_book(candidate.polymarket_id)
        except Exception:
            return QuantSignals(0, 0, 0, 0, 0)

        bids = book.get("bids", [])
        asks = book.get("asks", [])
        bid_depth = sum(float(b.get("size", 0)) for b in bids)
        ask_depth = sum(float(a.get("size", 0)) for a in asks)
        best_bid = float(bids[0]["price"]) if bids else candidate.current_price - 0.01
        best_ask = float(asks[0]["price"]) if asks else candidate.current_price + 0.01

        hours_remaining = max(
            0,
            (candidate.resolution_time - datetime.now(timezone.utc)).total_seconds() / 3600,
        )

        return QuantSignals(
            line_movement=compute_line_movement(
                price_history or [candidate.current_price],
                candidate.current_price,
            ),
            volume_spike=compute_volume_spike(0, 0),  # filled when volume data available
            book_imbalance=compute_book_imbalance(bid_depth, ask_depth),
            spread=compute_spread_signal(best_bid, best_ask),
            time_decay=compute_time_decay(hours_remaining),
        )

    async def run_forever(self):
        log.info("engine_starting")
        self._last_self_assess: datetime | None = None
        while True:
            try:
                await self.run_cycle()
                await self._maybe_self_assess()
            except Exception as e:
                log.error("cycle_error", error=str(e))
            await asyncio.sleep(self._settings.scan_interval_seconds)

    async def _maybe_self_assess(self):
        now = datetime.now(timezone.utc)
        if self._last_self_assess and (now - self._last_self_assess).total_seconds() < 86400:
            return

        from polybot.learning.self_assess import suggest_kelly_adjustment, suggest_edge_threshold

        state = await self._db.fetchrow("SELECT * FROM system_state WHERE id = 1")
        if not state:
            return

        # Compute max drawdown from trade history
        trades = await self._db.fetch(
            "SELECT pnl FROM trades WHERE status='closed' AND closed_at > NOW() - INTERVAL '7 days'"
        )
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in trades:
            cumulative += float(t["pnl"] or 0)
            peak = max(peak, cumulative)
            dd = (peak - cumulative) / max(float(state["bankroll"]), 1)
            max_dd = max(max_dd, dd)

        new_kelly = suggest_kelly_adjustment(float(state["kelly_mult"]), max_dd)

        # Edge threshold tuning
        edge_trades = await self._db.fetch(
            """SELECT a.edge, t.pnl FROM trades t JOIN analyses a ON t.analysis_id = a.id
               WHERE t.status='closed' AND t.closed_at > NOW() - INTERVAL '7 days'"""
        )
        buckets: dict[float, dict] = {}
        for t in edge_trades:
            edge_val = round(float(t["edge"]), 2)
            bucket_key = round(edge_val * 20) / 20  # round to nearest 0.05
            if bucket_key not in buckets:
                buckets[bucket_key] = {"count": 0, "total_pnl": 0.0}
            buckets[bucket_key]["count"] += 1
            buckets[bucket_key]["total_pnl"] += float(t["pnl"] or 0)

        new_edge = suggest_edge_threshold(float(state["edge_threshold"]), buckets)

        await self._db.execute(
            "UPDATE system_state SET kelly_mult=$1, edge_threshold=$2 WHERE id=1",
            new_kelly, new_edge,
        )
        self._last_self_assess = now
        log.info("self_assessment_complete", kelly=new_kelly, edge=new_edge, max_dd=max_dd)
