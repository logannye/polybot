import json
import structlog
from datetime import datetime, timezone
from polybot.strategies.base import Strategy, TradingContext
from polybot.trading.risk import PortfolioState, TradeProposal, bankroll_kelly_adjustment
from polybot.trading.kelly import compute_position_size
from polybot.analysis.prompts import build_snipe_prompt, parse_snipe_response
from polybot.notifications.email import format_trade_email

log = structlog.get_logger()


def classify_snipe_tier(price: float, hours_remaining: float, max_hours: float = 6.0) -> int | None:
    """
    Classify a market as a snipe candidate.

    Returns:
    - 0: High confidence tier (price >= 0.92 or <= 0.08, no LLM needed)
    - 1: Medium confidence tier (0.80-0.92 or 0.08-0.20, requires LLM verification)
    - None: Not a snipe candidate
    """
    if hours_remaining > max_hours or hours_remaining <= 0:
        return None
    if price >= 0.92:
        return 0
    if price >= 0.80 and hours_remaining <= 3.0:
        return 1
    if price <= 0.08:
        return 0
    if price <= 0.20 and hours_remaining <= 3.0:
        return 1
    return None


def compute_snipe_edge(buy_price: float, fee_rate: float = 0.02) -> float:
    """
    Compute net edge for a snipe trade.

    For YES bets: edge = (1.0 - buy_price) - fee_rate
    buy_price is the market price paid
    """
    return (1.0 - buy_price) - fee_rate


class ResolutionSnipeStrategy(Strategy):
    name = "snipe"

    def __init__(self, settings, ensemble=None):
        self.interval_seconds = settings.snipe_interval_seconds
        self.kelly_multiplier = settings.snipe_kelly_mult
        self.max_single_pct = settings.snipe_max_single_pct
        self._min_net_edge = settings.snipe_min_net_edge
        self._min_confidence = settings.snipe_min_confidence
        self._max_hours = settings.snipe_hours_max
        self._fee_rate = settings.polymarket_fee_rate
        self._ensemble = ensemble

    async def run_once(self, ctx: TradingContext) -> None:
        enabled = await ctx.db.fetchval(
            "SELECT enabled FROM strategy_performance WHERE strategy = 'snipe'")
        if enabled is False:
            return

        raw_markets = await ctx.scanner.fetch_markets()
        now = datetime.now(timezone.utc)

        for m in raw_markets:
            hours_remaining = (m["resolution_time"] - now).total_seconds() / 3600
            tier = classify_snipe_tier(m["yes_price"], hours_remaining, self._max_hours)
            if tier is None:
                continue

            if m["yes_price"] >= 0.80:
                side, buy_price = "YES", m["yes_price"]
            elif m["yes_price"] <= 0.20:
                side, buy_price = "NO", 1 - m["yes_price"]
            else:
                continue

            net_edge = compute_snipe_edge(buy_price, self._fee_rate)
            if net_edge < self._min_net_edge:
                continue

            if tier == 1 and self._ensemble:
                prompt = build_snipe_prompt(m["question"], str(m["resolution_time"]), hours_remaining, m["yes_price"])
                try:
                    response = await self._ensemble._google.aio.models.generate_content(
                        model="gemini-2.5-flash", contents=prompt)
                    parsed = parse_snipe_response(response.text)
                    if not parsed or not parsed["determined"] or parsed["confidence"] < self._min_confidence:
                        continue
                    if parsed["outcome"] == "NO" and side == "YES":
                        continue
                    if parsed["outcome"] == "YES" and side == "NO":
                        continue
                except Exception as e:
                    log.error("snipe_llm_error", error=str(e))
                    continue

            async with ctx.portfolio_lock:
                state_row = await ctx.db.fetchrow("SELECT * FROM system_state WHERE id = 1")
                if not state_row:
                    continue
                bankroll = float(state_row["bankroll"])
                kelly_adj = bankroll_kelly_adjustment(
                    bankroll=bankroll, base_kelly=self.kelly_multiplier,
                    post_breaker_until=state_row.get("post_breaker_until"),
                    post_breaker_reduction=ctx.settings.post_breaker_kelly_reduction,
                    survival_threshold=ctx.settings.bankroll_survival_threshold,
                    growth_threshold=ctx.settings.bankroll_growth_threshold,
                )
                kelly_fraction = net_edge / (1 - buy_price) if buy_price < 1.0 else 0.0
                size = compute_position_size(
                    bankroll=bankroll, kelly_fraction=kelly_fraction, kelly_mult=kelly_adj,
                    confidence_mult=1.0, max_single_pct=self.max_single_pct,
                    min_trade_size=ctx.settings.min_trade_size)
                if size <= 0:
                    continue

                open_trades = await ctx.db.fetch("SELECT * FROM trades WHERE status = 'open'")
                portfolio = PortfolioState(
                    bankroll=bankroll, total_deployed=float(state_row["total_deployed"]),
                    daily_pnl=float(state_row["daily_pnl"]),
                    open_count=len(open_trades), category_deployed={},
                    circuit_breaker_until=state_row.get("circuit_breaker_until"))
                proposal = TradeProposal(size_usd=size, category=m.get("category", "unknown"),
                                          book_depth=m.get("book_depth", 1000.0))
                risk_result = ctx.risk_manager.check(portfolio, proposal, max_single_pct=self.max_single_pct)
                if not risk_result.allowed:
                    continue

                # Upsert market record
                market_id = await ctx.db.fetchval(
                    """INSERT INTO markets (polymarket_id, question, category, resolution_time, current_price)
                       VALUES ($1, $2, $3, $4, $5)
                       ON CONFLICT (polymarket_id) DO UPDATE SET current_price=$5, last_updated=NOW()
                       RETURNING id""",
                    m["polymarket_id"], m["question"], m.get("category", "unknown"),
                    m["resolution_time"], m["yes_price"],
                )

                # Create analysis record for the snipe
                analysis_id = await ctx.db.fetchval(
                    """INSERT INTO analyses (market_id, model_estimates, ensemble_probability,
                       ensemble_stdev, quant_signals, edge)
                       VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
                    market_id, json.dumps([]), buy_price, 0.0, json.dumps({}), net_edge,
                )

                token_id = m.get("yes_token_id", "") if side == "YES" else m.get("no_token_id", "")
                result = await ctx.executor.place_order(
                    token_id=token_id, side=side, size_usd=size,
                    price=buy_price, market_id=market_id,
                    analysis_id=analysis_id, strategy=self.name,
                )
                if not result:
                    continue

            log.info("snipe_trade", market=m["polymarket_id"], side=side, price=buy_price,
                     edge=net_edge, size=size, tier=tier)
            await ctx.email_notifier.send(
                f"[POLYBOT] Trade executed: {m['question'][:60]}",
                format_trade_email(event="executed", market=m["question"], side=side,
                                   size=size, price=buy_price, edge=net_edge))
