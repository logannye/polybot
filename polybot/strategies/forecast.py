import asyncio
import json
import structlog
from datetime import datetime, timezone

from polybot.strategies.base import Strategy, TradingContext
from polybot.markets.filters import MarketCandidate, filter_markets
from polybot.analysis.prescore import prescore
from polybot.analysis.quant import (
    compute_line_movement,
    compute_book_imbalance,
    compute_spread_signal,
    compute_time_decay,
    compute_volume_spike,
    compute_composite_score,
    QuantSignals,
)
from polybot.trading.kelly import compute_kelly, compute_position_size
from polybot.analysis.ensemble import shrink_toward_market
from polybot.trading.risk import PortfolioState, TradeProposal, bankroll_kelly_adjustment
from polybot.notifications.email import format_trade_email

log = structlog.get_logger()


def _lookup_calibration_correction(prob: float, corrections: dict | None) -> float:
    """Find the correction for the nearest calibration bin to this probability."""
    if not corrections:
        return 0.0
    bin_mids = []
    for k, v in corrections.items():
        try:
            bin_mids.append((float(k), float(v)))
        except (ValueError, TypeError):
            continue
    if not bin_mids:
        return 0.0
    nearest = min(bin_mids, key=lambda x: abs(x[0] - prob))
    return max(-0.10, min(0.10, nearest[1]))


def check_forecast_blacklist(
    polymarket_id: str,
    blacklist: dict[str, list],
) -> bool:
    """Return True if market is blacklisted (2+ stop-losses in recent history)."""
    losses = blacklist.get(polymarket_id, [])
    return len(losses) >= 2


_STRATEGY_DISABLED_REASON = "strategy_disabled"


class EnsembleForecastStrategy(Strategy):
    name = "forecast"

    def __init__(self, settings, ensemble, researcher):
        self.interval_seconds: float = settings.forecast_interval_seconds
        self.kelly_multiplier: float = settings.forecast_kelly_mult
        self.max_single_pct: float = settings.forecast_max_single_pct
        self._settings = settings
        self._ensemble = ensemble
        self._researcher = researcher
        self._loss_blacklist: dict[str, list] = {}

    async def run_once(self, ctx: TradingContext) -> None:
        # 1. Check if this strategy is enabled
        enabled_row = await ctx.db.fetchrow(
            "SELECT enabled FROM strategy_performance WHERE strategy = $1",
            self.name,
        )
        if enabled_row and not enabled_row["enabled"]:
            log.info("strategy_skipped", strategy=self.name, reason=_STRATEGY_DISABLED_REASON)
            return

        # 2. Read system_state: bankroll, kelly_mult, edge_threshold, calibration_corrections
        state_row = await ctx.db.fetchrow("SELECT * FROM system_state WHERE id = 1")
        if not state_row:
            log.error("no_system_state", strategy=self.name)
            return

        bankroll = float(state_row["bankroll"])
        kelly_mult = float(state_row["kelly_mult"])
        edge_threshold = float(state_row["edge_threshold"])
        raw_cal = state_row["calibration_corrections"] or {}
        calibration_corrections: dict[str, float] = json.loads(raw_cal) if isinstance(raw_cal, str) else raw_cal

        # Refresh forecast loss blacklist
        recent_losses = await ctx.db.fetch(
            """SELECT m.polymarket_id, t.closed_at
               FROM trades t JOIN markets m ON t.market_id = m.id
               WHERE t.strategy = 'forecast'
                 AND t.exit_reason IN ('stop_loss', 'time_stop')
                 AND t.closed_at > NOW() - INTERVAL '12 hours'""")
        self._loss_blacklist = {}
        for row in recent_losses:
            pid = row["polymarket_id"]
            self._loss_blacklist.setdefault(pid, []).append(row["closed_at"])

        # 3. Scan + filter markets
        raw_markets = await ctx.scanner.fetch_markets()
        candidates = []
        for m in raw_markets:
            last_analysis = await ctx.db.fetchrow(
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
                volume_24h=m.get("volume_24h", 0),
                last_analyzed_at=last_analysis["timestamp"] if last_analysis else None,
                previous_price=float(last_analysis["ensemble_probability"]) if last_analysis else None,
                yes_token_id=m.get("yes_token_id", ""),
                no_token_id=m.get("no_token_id", ""),
                no_price=m.get("no_price", 0.0),
            ))

        filtered = filter_markets(
            candidates,
            resolution_hours_max=getattr(self._settings, "resolution_hours_max", 72),
            min_book_depth=getattr(self._settings, "min_book_depth", 500.0),
            min_price=getattr(self._settings, "min_price", 0.05),
            max_price=getattr(self._settings, "max_price", 0.95),
            cooldown_minutes=getattr(self._settings, "cooldown_minutes", 30),
            price_move_threshold=getattr(self._settings, "price_move_threshold", 0.03),
        )

        if not filtered:
            log.info("no_filtered_markets", strategy=self.name)
            return

        # Load category stats for prescore
        category_stats = await self._load_category_stats(ctx)

        # 4. Prescore — compute quant signals, rank, take top N
        prescore_top_n: int = getattr(self._settings, "prescore_top_n", 5)
        scored: list[tuple[float, MarketCandidate, QuantSignals]] = []
        for candidate in filtered:
            quant = await self._compute_quant(candidate, ctx)
            score = prescore(candidate, category_stats, quant,
                             getattr(self._settings, "quant_weights", None))
            scored.append((score, candidate, quant))

        scored.sort(key=lambda x: x[0], reverse=True)
        top_n = scored[:prescore_top_n]

        # 5. Quick screen — discard candidates where |quick_prob - market_price| is too small
        quick_screen_min_edge: float = getattr(self._settings, "quick_screen_max_edge_gap", 0.05)
        passed_screen: list[tuple[MarketCandidate, QuantSignals]] = []
        for _score, candidate, quant in top_n:
            quick_prob = await self._ensemble.quick_screen(
                candidate.question,
                candidate.current_price,
                candidate.resolution_time.isoformat(),
            )
            if quick_prob is None:
                # Can't screen, include conservatively
                passed_screen.append((candidate, quant))
                continue
            apparent_edge = abs(quick_prob - candidate.current_price)
            if apparent_edge < quick_screen_min_edge:
                log.debug(
                    "quick_screen_skip",
                    market=candidate.polymarket_id,
                    apparent_edge=apparent_edge,
                    threshold=quick_screen_min_edge,
                )
                continue
            passed_screen.append((candidate, quant))

        if not passed_screen:
            log.info("all_quick_screened_out", strategy=self.name)
            return

        # Portfolio state for risk checks
        open_trades = await ctx.db.fetch("SELECT * FROM trades WHERE status = 'open'")
        cat_deployed: dict[str, float] = {}
        for t in open_trades:
            mkt = await ctx.db.fetchrow("SELECT category FROM markets WHERE id = $1", t["market_id"])
            if mkt:
                cat = mkt["category"]
                cat_deployed[cat] = cat_deployed.get(cat, 0.0) + float(t["position_size_usd"])

        total_deployed = float(state_row["total_deployed"])
        daily_pnl = float(state_row["daily_pnl"])
        cb_until = state_row["circuit_breaker_until"]

        portfolio = PortfolioState(
            bankroll=bankroll,
            total_deployed=total_deployed,
            daily_pnl=daily_pnl,
            open_count=len(open_trades),
            category_deployed=cat_deployed,
            circuit_breaker_until=cb_until,
        )

        # Model trust weights
        model_rows = await ctx.db.fetch("SELECT model_name, trust_weight FROM model_performance")
        trust_weights = {r["model_name"]: float(r["trust_weight"]) for r in model_rows}

        # 6. Full ensemble on remaining candidates (top 2-3 after quick screen)
        full_ensemble_limit: int = getattr(self._settings, "full_ensemble_limit", 3)
        trades_placed = 0
        for candidate, quant in passed_screen[:full_ensemble_limit]:
            try:
                await self._full_analyze_and_trade(
                    candidate=candidate,
                    quant=quant,
                    trust_weights=trust_weights,
                    bankroll=bankroll,
                    kelly_mult=kelly_mult,
                    edge_threshold=edge_threshold,
                    portfolio=portfolio,
                    calibration_corrections=calibration_corrections,
                    ctx=ctx,
                )
                trades_placed += 1
            except Exception as e:
                import traceback
                log.error("forecast_analysis_error", market=candidate.polymarket_id,
                          error=str(e), traceback=traceback.format_exc())

        log.info("forecast_cycle_complete",
                 markets_scanned=len(raw_markets), filtered=len(filtered),
                 passed_screen=len(passed_screen), analyzed=min(len(passed_screen), full_ensemble_limit),
                 deployed_pct=round(total_deployed / max(bankroll, 1) * 100, 1))

    async def _full_analyze_and_trade(
        self,
        candidate: MarketCandidate,
        quant: QuantSignals,
        trust_weights: dict[str, float],
        bankroll: float,
        kelly_mult: float,
        edge_threshold: float,
        portfolio: PortfolioState,
        calibration_corrections: dict[str, float],
        ctx: TradingContext,
    ) -> None:
        # Market loss blacklist: skip markets with 2+ recent stop-losses
        if check_forecast_blacklist(candidate.polymarket_id, self._loss_blacklist):
            log.info("forecast_blacklisted", market=candidate.polymarket_id,
                     losses=len(self._loss_blacklist.get(candidate.polymarket_id, [])))
            return

        # Category performance filter: skip categories with negative ROI
        if getattr(self._settings, 'forecast_category_filter_enabled', True):
            _cat_min_trades = getattr(self._settings, "forecast_category_min_trades", 10)
            _cat_min_avg = getattr(self._settings, "forecast_category_min_avg_pnl", -1.0)
            try:
                _sys_state = await ctx.db.fetchrow("SELECT category_scores FROM system_state WHERE id = 1")
                if _sys_state:
                    import json as _json
                    _cat_scores_raw = _sys_state.get("category_scores") if hasattr(_sys_state, "get") else None
                    if _cat_scores_raw:
                        _cat_scores = _json.loads(_cat_scores_raw) if isinstance(_cat_scores_raw, str) else _cat_scores_raw
                        _cat_data = _cat_scores.get(candidate.category) if isinstance(_cat_scores, dict) else None
                        if _cat_data and isinstance(_cat_data, dict) and _cat_data.get("trades", 0) >= _cat_min_trades:
                            _avg_pnl = _cat_data.get("pnl", 0) / max(_cat_data["trades"], 1)
                            if _avg_pnl < _cat_min_avg:
                                log.info("forecast_category_filtered", market=candidate.polymarket_id,
                                         category=candidate.category, avg_pnl=round(_avg_pnl, 2))
                                return
            except Exception:
                pass  # Category filtering is best-effort; don't block trades on DB errors

        # 6a. Web research first, then ensemble with full context
        research = await self._researcher.search(candidate.question)
        ensemble_result = await self._ensemble.analyze(
            candidate.question, research, trust_weights
        )

        # Consensus requirement: at least N models must agree on direction
        _min_consensus = getattr(self._settings, "forecast_min_consensus", 2)
        _consensus_margin = getattr(self._settings, "forecast_consensus_margin", 0.02)
        if len(ensemble_result.estimates) >= 2:
            _above = sum(1 for e in ensemble_result.estimates
                         if e.probability > candidate.current_price + _consensus_margin)
            _below = sum(1 for e in ensemble_result.estimates
                         if e.probability < candidate.current_price - _consensus_margin)
            if max(_above, _below) < _min_consensus:
                log.info("forecast_no_consensus", market=candidate.polymarket_id,
                         above=_above, below=_below, threshold=_min_consensus)
                return

        composite = compute_composite_score(quant, getattr(self._settings, "quant_weights", None) or {})

        # Quant veto
        if composite < -0.3:
            log.info("quant_skip", market=candidate.polymarket_id, composite=composite)
            return

        # 7. Market-efficiency shrinkage + calibration correction
        raw_prob = ensemble_result.ensemble_probability
        prob = shrink_toward_market(raw_prob, candidate.current_price, shrinkage=0.15)

        # Challenge pass: if ensemble disagrees with market by >15%,
        # ask Gemini Flash to revise after seeing the market price
        disagreement = abs(raw_prob - candidate.current_price)
        if disagreement > 0.15:
            best_reasoning = max(ensemble_result.estimates,
                                 key=lambda e: abs(e.probability - candidate.current_price)).reasoning
            revised = await self._ensemble.challenge_estimate(
                candidate.question, raw_prob, candidate.current_price, best_reasoning)
            if revised is not None:
                # Blend: 50% shrunk ensemble + 50% challenge revision
                prob = prob * 0.5 + revised * 0.5
                log.info("challenge_blended", market=candidate.polymarket_id,
                         raw=raw_prob, shrunk=shrink_toward_market(raw_prob, candidate.current_price),
                         revised=revised, final=prob)

        if calibration_corrections:
            correction = _lookup_calibration_correction(prob, calibration_corrections)
            prob = max(0.01, min(0.99, prob + correction))

        prob = max(0.01, min(0.99, prob))

        # 8a. Fee-adjusted Kelly (makers pay 0%, takers pay category-specific rate)
        from polybot.trading.fees import get_fee_rate as _get_fee_rate, compute_taker_fee_per_dollar
        if self._settings.use_maker_orders:
            _fee_per_dollar = 0.0
        else:
            _cat_rate = _get_fee_rate(candidate.category)
            _buy_price = candidate.current_price if prob > candidate.current_price else 1.0 - candidate.current_price
            _fee_per_dollar = compute_taker_fee_per_dollar(_buy_price, _cat_rate)
        kelly_result = compute_kelly(
            prob,
            candidate.current_price,
            fee_per_dollar=_fee_per_dollar,
        )
        if kelly_result.edge < edge_threshold:
            log.debug("low_edge", market=candidate.polymarket_id, edge=kelly_result.edge)
            return

        # 8b. Confidence modulation
        conf_mult = ctx.risk_manager.confidence_multiplier(
            stdev=ensemble_result.stdev,
            quant_score=composite,
            stdev_low=getattr(self._settings, "ensemble_stdev_low", 0.05),
            stdev_high=getattr(self._settings, "ensemble_stdev_high", 0.15),
            mult_low=getattr(self._settings, "confidence_mult_low", 1.2),
            mult_mid=getattr(self._settings, "confidence_mult_mid", 1.0),
            mult_high=getattr(self._settings, "confidence_mult_high", 0.6),
            quant_neg_mult=getattr(self._settings, "quant_negative_mult", 0.5),
        )

        # Bankroll-based kelly adjustment
        effective_kelly = bankroll_kelly_adjustment(
            bankroll=bankroll,
            base_kelly=kelly_mult,
            post_breaker_until=portfolio.circuit_breaker_until,
            post_breaker_reduction=getattr(ctx.settings, "post_breaker_kelly_reduction", 0.50),
            survival_threshold=getattr(ctx.settings, "bankroll_survival_threshold", 50.0),
            growth_threshold=getattr(ctx.settings, "bankroll_growth_threshold", 500.0),
        )

        # Contrarian bet guard: be cautious when disagreeing with extreme market consensus
        market_price = candidate.current_price
        if (market_price > 0.95 or market_price < 0.05) and abs(prob - market_price) > 0.20:
            log.info("contrarian_skip_extreme", market=candidate.polymarket_id,
                     market_price=market_price, ensemble_prob=prob)
            return
        if (market_price > 0.90 or market_price < 0.10) and abs(prob - market_price) > 0.30:
            # Halve the effective kelly for high-disagreement contrarian bets
            effective_kelly *= 0.5
            log.info("contrarian_halved", market=candidate.polymarket_id,
                     market_price=market_price, ensemble_prob=prob)

        # 8c. Edge skepticism — large edges are more likely miscalibration
        skepticism = ctx.risk_manager.edge_skepticism_discount(kelly_result.edge)

        # 8d. Position size
        size = compute_position_size(
            bankroll=bankroll,
            kelly_fraction=kelly_result.kelly_fraction,
            kelly_mult=effective_kelly,
            confidence_mult=conf_mult * skepticism,
            max_single_pct=self.max_single_pct,
            min_trade_size=getattr(self._settings, "min_trade_size", 1.0),
        )

        if size <= 0:
            return

        # Upsert market record
        market_id = await ctx.db.fetchval(
            """INSERT INTO markets (polymarket_id, question, category, resolution_time,
                   current_price, volume_24h, book_depth)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               ON CONFLICT (polymarket_id) DO UPDATE SET
                   current_price=$5, volume_24h=$6, book_depth=$7, last_updated=NOW()
               RETURNING id""",
            candidate.polymarket_id, candidate.question, candidate.category,
            candidate.resolution_time, candidate.current_price,
            candidate.volume_24h, candidate.book_depth,
        )

        # Record analysis
        analysis_id = await ctx.db.fetchval(
            """INSERT INTO analyses (market_id, model_estimates, ensemble_probability,
               ensemble_stdev, quant_signals, edge, web_research_summary)
               VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
            market_id,
            json.dumps([{"model": e.model, "probability": e.probability,
              "confidence": e.confidence, "reasoning": e.reasoning}
             for e in ensemble_result.estimates]),
            prob,
            ensemble_result.stdev,
            json.dumps({
                "composite": composite,
                "line_movement": quant.line_movement,
                "volume_spike": quant.volume_spike,
                "book_imbalance": quant.book_imbalance,
                "spread": quant.spread,
                "time_decay": quant.time_decay,
            }),
            kelly_result.edge,
            research,
        )

        # 9. Dedup + risk check under portfolio lock (atomic with trade insert)
        proposal = TradeProposal(
            size_usd=size,
            category=candidate.category,
            book_depth=candidate.book_depth,
        )

        async with ctx.portfolio_lock:
            # Dedup: check for existing positions in this market
            existing = await ctx.db.fetchval(
                """SELECT COUNT(*) FROM trades
                   WHERE market_id = $1 AND status IN ('open', 'filled', 'dry_run')""",
                market_id)
            max_per_market = getattr(self._settings, "max_positions_per_market", 1)
            if existing >= max_per_market:
                log.info("market_dedup_skip", market=candidate.polymarket_id,
                         existing=existing, max=max_per_market)
                return

            # Fresh portfolio state for risk check
            fresh_state = await ctx.db.fetchrow("SELECT * FROM system_state WHERE id = 1")
            portfolio = PortfolioState(
                bankroll=float(fresh_state["bankroll"]),
                total_deployed=float(fresh_state["total_deployed"]),
                daily_pnl=float(fresh_state["daily_pnl"]),
                open_count=portfolio.open_count,
                category_deployed=portfolio.category_deployed,
                circuit_breaker_until=fresh_state["circuit_breaker_until"],
            )

            risk_result = ctx.risk_manager.check(proposal=proposal, state=portfolio,
                                                 max_single_pct=self.max_single_pct)
            if not risk_result.allowed:
                log.info("risk_rejected", market=candidate.polymarket_id, reason=risk_result.reason)
                return

            token_id = candidate.yes_token_id if kelly_result.side == "YES" else candidate.no_token_id
            price = candidate.current_price if kelly_result.side == "YES" else candidate.no_price

            log.info(
                "forecast_trade",
                strategy=self.name,
                market=candidate.polymarket_id,
                side=kelly_result.side,
                size=size,
                edge=kelly_result.edge,
                ensemble_prob=prob,
            )
            await ctx.executor.place_order(
                token_id=token_id,
                side=kelly_result.side,
                size_usd=size,
                price=price,
                market_id=market_id,
                analysis_id=analysis_id,
                strategy=self.name,
                kelly_inputs={
                    "ensemble_prob": round(prob, 4),
                    "market_price": round(candidate.current_price, 4),
                    "edge": round(kelly_result.edge, 4),
                    "kelly_fraction": round(kelly_result.kelly_fraction, 4),
                    "confidence_mult": round(conf_mult, 4),
                    "skepticism": round(skepticism, 4),
                    "effective_kelly": round(effective_kelly, 4),
                    "stdev": round(ensemble_result.stdev, 4),
                    "composite_quant": round(composite, 4),
                },
                post_only=self._settings.use_maker_orders,
            )
            await ctx.email_notifier.send(
                f"[POLYBOT] Trade executed: {candidate.question[:60]}",
                format_trade_email(event="executed", market=candidate.question, side=kelly_result.side,
                                   size=size, price=candidate.current_price, edge=kelly_result.edge))

    async def _compute_quant(self, candidate: MarketCandidate, ctx: TradingContext) -> QuantSignals:
        try:
            price_history = await ctx.scanner.fetch_price_history(candidate.polymarket_id)
            book = await ctx.scanner.fetch_order_book(candidate.polymarket_id)
        except Exception:
            return QuantSignals(0, 0, 0, 0, 0)

        bids = book.get("bids", [])
        asks = book.get("asks", [])
        bid_depth = sum(float(b.get("size", 0)) for b in bids)
        ask_depth = sum(float(a.get("size", 0)) for a in asks)
        best_bid = float(bids[0]["price"]) if bids else candidate.current_price - 0.01
        best_ask = float(asks[0]["price"]) if asks else candidate.current_price + 0.01

        hours_remaining = max(
            0.0,
            (candidate.resolution_time - datetime.now(timezone.utc)).total_seconds() / 3600,
        )

        return QuantSignals(
            line_movement=compute_line_movement(
                price_history or [candidate.current_price],
                candidate.current_price,
            ),
            volume_spike=compute_volume_spike(0, 0),
            book_imbalance=compute_book_imbalance(bid_depth, ask_depth),
            spread=compute_spread_signal(best_bid, best_ask),
            time_decay=compute_time_decay(hours_remaining),
        )

    async def _load_category_stats(self, ctx: TradingContext) -> dict:
        from polybot.learning.categories import CategoryStats
        rows = await ctx.db.fetch(
            """SELECT category,
                      COUNT(*) AS total_trades,
                      SUM(pnl) AS total_pnl,
                      SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS win_count
               FROM trades t
               JOIN markets m ON t.market_id = m.id
               WHERE t.status = 'closed'
               GROUP BY category"""
        )
        return {
            r["category"]: CategoryStats(
                total_trades=int(r["total_trades"]),
                total_pnl=float(r["total_pnl"] or 0),
                win_count=int(r["win_count"] or 0),
            )
            for r in rows
        }
