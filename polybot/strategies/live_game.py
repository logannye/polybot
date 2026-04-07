"""LiveGameCloserStrategy — ESPN-powered live sports trading.

Polls ESPN every 30s for live MLB/NBA/NHL games, matches them to Polymarket
markets by team name, computes edge (ESPN win probability vs Polymarket price),
and enters positions on high-confidence plays.  Holds to game resolution.
"""

import json
import structlog

from polybot.strategies.base import Strategy, TradingContext
from polybot.trading.risk import PortfolioState, TradeProposal, bankroll_kelly_adjustment
from polybot.trading.kelly import compute_position_size
from polybot.notifications.email import format_trade_email
from polybot.analysis.espn_client import ESPNClient
from polybot.analysis.win_probability import compute_win_probability, TOTAL_PERIODS

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_search_tokens(team_name: str) -> list[str]:
    """Build search tokens from a team name.

    Returns the full name lowercased plus the last word (mascot).
    For example, "Cleveland Cavaliers" -> ["cleveland cavaliers", "cavaliers"].
    """
    lower = team_name.strip().lower()
    if not lower:
        return []
    tokens = [lower]
    parts = lower.split()
    if len(parts) > 1:
        tokens.append(parts[-1])
    return tokens


def _team_matches_question(team_name: str, question_lower: str) -> bool:
    """Return True if any search token for *team_name* appears in *question_lower*."""
    for token in _build_search_tokens(team_name):
        if token in question_lower:
            return True
    return False


def _parse_outcomes(raw_outcomes) -> list[str]:
    """Defensively parse the outcomes field — may be a JSON string or already a list."""
    if raw_outcomes is None:
        return []
    if isinstance(raw_outcomes, list):
        return [str(o) for o in raw_outcomes]
    if isinstance(raw_outcomes, str):
        try:
            parsed = json.loads(raw_outcomes)
            if isinstance(parsed, list):
                return [str(o) for o in parsed]
        except (json.JSONDecodeError, TypeError):
            pass
    return []


def match_game_to_market(game: dict, price_cache: dict[str, dict]) -> dict | None:
    """Match an ESPN game to a Polymarket market via team-name search.

    Parameters
    ----------
    game : dict
        ESPN game dict (from ESPNClient) with ``home_team``, ``away_team``, etc.
    price_cache : dict[str, dict]
        Polymarket price cache from ``scanner.get_all_cached_prices()``.

    Returns
    -------
    dict | None
        Matched market info with ``polymarket_id``, ``question``, ``yes_price``,
        ``yes_token_id``, ``no_token_id``, ``home_outcome``, ``away_outcome``,
        ``book_depth``, ``volume_24h``, ``category``, ``resolution_time``.
        Returns *None* if no match found.
    """
    home_team = game.get("home_team", "")
    away_team = game.get("away_team", "")

    if not home_team or not away_team:
        return None

    for pid, market in price_cache.items():
        question = market.get("question", "")
        q_lower = question.lower()

        # Both teams must appear in the question
        if not _team_matches_question(home_team, q_lower):
            continue
        if not _team_matches_question(away_team, q_lower):
            continue

        # Parse outcomes to determine which outcome is home vs away
        outcomes = _parse_outcomes(market.get("outcomes"))

        home_outcome = None
        away_outcome = None

        if len(outcomes) >= 2:
            for outcome in outcomes:
                outcome_lower = outcome.lower()
                if _team_matches_question(home_team, outcome_lower):
                    home_outcome = outcome
                elif _team_matches_question(away_team, outcome_lower):
                    away_outcome = outcome

        # If outcomes didn't resolve cleanly, fall back to positional assignment
        if home_outcome is None or away_outcome is None:
            if len(outcomes) >= 2:
                home_outcome = outcomes[0]
                away_outcome = outcomes[1]
            else:
                home_outcome = "YES"
                away_outcome = "NO"

        return {
            "polymarket_id": pid,
            "question": question,
            "yes_price": market.get("yes_price", 0.0),
            "yes_token_id": market.get("yes_token_id", ""),
            "no_token_id": market.get("no_token_id", ""),
            "home_outcome": home_outcome,
            "away_outcome": away_outcome,
            "book_depth": market.get("book_depth", 0.0),
            "volume_24h": market.get("volume_24h", 0.0),
            "category": market.get("category", "unknown"),
            "resolution_time": market.get("resolution_time"),
        }

    return None


def compute_game_edge(win_prob: float, polymarket_price: float) -> dict:
    """Compare ESPN-derived win probability to Polymarket YES price.

    Returns
    -------
    dict
        ``side`` ("YES" or "NO"), ``edge`` (float), ``buy_price`` (float).
        The edge is always the maximum of the two sides (YES or NO).
    """
    yes_edge = win_prob - polymarket_price
    no_edge = (1 - win_prob) - (1 - polymarket_price)  # simplifies to polymarket_price - win_prob

    if yes_edge >= no_edge:
        return {"side": "YES", "edge": yes_edge, "buy_price": polymarket_price}
    else:
        return {"side": "NO", "edge": no_edge, "buy_price": 1.0 - polymarket_price}


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class LiveGameCloserStrategy(Strategy):
    """Trade live sports games where ESPN win probability diverges from Polymarket price."""

    name = "live_game"

    def __init__(self, settings, espn_client: ESPNClient | None = None):
        self.interval_seconds = getattr(settings, "lg_interval_seconds", 30.0)
        self.kelly_multiplier = getattr(settings, "lg_kelly_mult", 0.50)
        self.max_single_pct = getattr(settings, "lg_max_single_pct", 0.25)
        self._min_edge = getattr(settings, "lg_min_edge", 0.04)
        self._min_win_prob = getattr(settings, "lg_min_win_prob", 0.85)
        self._min_book_depth = getattr(settings, "lg_min_book_depth", 10_000.0)
        self._max_concurrent = getattr(settings, "lg_max_concurrent", 6)
        self._espn = espn_client or ESPNClient()
        self._traded_games: set[str] = set()

    async def run_once(self, ctx: TradingContext) -> None:
        # 1. Check if strategy is enabled in DB
        enabled = await ctx.db.fetchval(
            "SELECT enabled FROM strategy_performance WHERE strategy = 'live_game'")
        if enabled is False:
            return

        # 2. Check concurrent position cap
        open_lg_count = await ctx.db.fetchval(
            """SELECT COUNT(*) FROM trades
               WHERE strategy = 'live_game'
                 AND status IN ('open', 'filled', 'dry_run')""")
        if (open_lg_count or 0) >= self._max_concurrent:
            log.info("lg_max_concurrent_reached", open=open_lg_count,
                     max=self._max_concurrent)
            return

        # 3. Fetch live games from ESPN
        games = await self._espn.fetch_all_live_games()
        if not games:
            log.debug("lg_no_live_games")
            return

        # 4. Get Polymarket price cache
        price_cache = ctx.scanner.get_all_cached_prices()
        if not price_cache:
            log.debug("lg_no_price_cache")
            return

        for game in games:
            # Skip completed and already-traded games
            if game.get("completed"):
                continue
            if game.get("status") != "in_progress":
                continue

            espn_id = game.get("espn_id", "")
            if espn_id in self._traded_games:
                continue

            sport = game.get("sport", "")
            total_periods = TOTAL_PERIODS.get(sport)
            if total_periods is None:
                continue

            # 5a. Compute home team win probability
            lead = game.get("home_score", 0) - game.get("away_score", 0)
            period = game.get("period", 0)
            home_wp = compute_win_probability(
                sport=sport, lead=lead, period=period,
                total_periods=total_periods, completed=False,
            )
            if home_wp is None:
                continue

            # 5b. Skip if neither side is high-confidence
            dominant_wp = max(home_wp, 1 - home_wp)
            if dominant_wp < self._min_win_prob:
                continue

            # 5c. Match to Polymarket market
            matched = match_game_to_market(game, price_cache)
            if matched is None:
                continue

            # 5d. Skip if book depth too shallow
            if matched["book_depth"] < self._min_book_depth:
                log.debug("lg_low_depth", espn_id=espn_id,
                          depth=matched["book_depth"], min=self._min_book_depth)
                continue

            # 5e. Determine YES/NO mapping
            #     We need to know: does YES = home team winning?
            #     The first outcome corresponds to YES token.
            outcomes = _parse_outcomes(
                ctx.scanner.get_cached_price(matched["polymarket_id"]).get("outcomes")
                if ctx.scanner.get_cached_price(matched["polymarket_id"]) else None
            )
            # Determine if YES token corresponds to home team
            yes_is_home = True  # default assumption
            if outcomes and len(outcomes) >= 2:
                home_tokens = _build_search_tokens(game["home_team"])
                away_tokens = _build_search_tokens(game["away_team"])
                first_outcome_lower = outcomes[0].lower()
                # Check if first outcome (YES side) matches away team
                for token in away_tokens:
                    if token in first_outcome_lower:
                        yes_is_home = False
                        break

            # Map win probability to the correct Polymarket side
            if yes_is_home:
                wp_for_yes = home_wp
            else:
                wp_for_yes = 1 - home_wp

            # 5f. Compute edge
            edge_info = compute_game_edge(wp_for_yes, matched["yes_price"])
            if edge_info["edge"] < self._min_edge:
                continue

            log.info("lg_opportunity",
                     espn_id=espn_id,
                     game=game.get("short_name", ""),
                     sport=sport,
                     home_wp=round(home_wp, 3),
                     polymarket_price=matched["yes_price"],
                     side=edge_info["side"],
                     edge=round(edge_info["edge"], 4),
                     market=matched["polymarket_id"])

            # 5h. Trade execution inside portfolio lock
            async with ctx.portfolio_lock:
                state_row = await ctx.db.fetchrow(
                    "SELECT * FROM system_state WHERE id = 1")
                if not state_row:
                    continue

                bankroll = float(state_row["bankroll"])
                kelly_adj = bankroll_kelly_adjustment(
                    bankroll=bankroll,
                    base_kelly=self.kelly_multiplier,
                    post_breaker_until=state_row.get("post_breaker_until"),
                )

                kelly_fraction = (
                    edge_info["edge"] / (1 - edge_info["buy_price"])
                    if edge_info["buy_price"] < 1.0 else 0.0
                )
                size = compute_position_size(
                    bankroll=bankroll,
                    kelly_fraction=kelly_fraction,
                    kelly_mult=kelly_adj,
                    confidence_mult=1.0,
                    max_single_pct=self.max_single_pct,
                    min_trade_size=getattr(ctx.settings, "min_trade_size", 1.0),
                )
                if size <= 0:
                    continue

                portfolio = PortfolioState(
                    bankroll=bankroll,
                    total_deployed=float(state_row["total_deployed"]),
                    daily_pnl=float(state_row["daily_pnl"]),
                    open_count=(open_lg_count or 0),
                    category_deployed={},
                    circuit_breaker_until=state_row.get("circuit_breaker_until"),
                )
                proposal = TradeProposal(
                    size_usd=size,
                    category=matched.get("category", "unknown"),
                    book_depth=matched.get("book_depth", 1000.0),
                )
                risk_result = ctx.risk_manager.check(
                    portfolio, proposal, max_single_pct=self.max_single_pct)
                if not risk_result.allowed:
                    log.info("lg_rejected_risk", espn_id=espn_id,
                             reason=risk_result.reason)
                    continue

                # Upsert market record
                market_id = await ctx.db.fetchval(
                    """INSERT INTO markets (polymarket_id, question, category,
                           resolution_time, current_price, volume_24h, book_depth)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)
                       ON CONFLICT (polymarket_id) DO UPDATE SET
                           current_price=$5, volume_24h=$6, book_depth=$7,
                           last_updated=NOW()
                       RETURNING id""",
                    matched["polymarket_id"], matched["question"],
                    matched.get("category", "unknown"),
                    matched.get("resolution_time"), matched["yes_price"],
                    matched.get("volume_24h"), matched.get("book_depth"),
                )

                # Insert analysis record
                kelly_inputs = {
                    "win_prob": round(wp_for_yes, 4),
                    "polymarket_price": matched["yes_price"],
                    "edge": round(edge_info["edge"], 4),
                    "kelly_fraction": round(kelly_fraction, 4),
                    "kelly_adj": round(kelly_adj, 4),
                }
                quant_signals = {
                    "sport": sport,
                    "home_team": game.get("home_team", ""),
                    "away_team": game.get("away_team", ""),
                    "home_score": game.get("home_score", 0),
                    "away_score": game.get("away_score", 0),
                    "period": period,
                    "home_wp": round(home_wp, 4),
                    "dominant_wp": round(dominant_wp, 4),
                }
                analysis_id = await ctx.db.fetchval(
                    """INSERT INTO analyses (market_id, model_estimates,
                           ensemble_probability, ensemble_stdev,
                           quant_signals, edge)
                       VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
                    market_id, json.dumps(kelly_inputs), wp_for_yes, 0.0,
                    json.dumps(quant_signals), edge_info["edge"],
                )

                # Place order
                token_id = (
                    matched["yes_token_id"]
                    if edge_info["side"] == "YES"
                    else matched["no_token_id"]
                )
                result = await ctx.executor.place_order(
                    token_id=token_id,
                    side=edge_info["side"],
                    size_usd=size,
                    price=edge_info["buy_price"],
                    market_id=market_id,
                    analysis_id=analysis_id,
                    strategy=self.name,
                )
                if not result:
                    continue

            # Mark game as traded (outside lock)
            self._traded_games.add(espn_id)

            log.info("lg_trade_placed",
                     espn_id=espn_id,
                     game=game.get("short_name", ""),
                     side=edge_info["side"],
                     price=edge_info["buy_price"],
                     edge=round(edge_info["edge"], 4),
                     size=size,
                     market=matched["polymarket_id"])

            await ctx.email_notifier.send(
                f"[POLYBOT] Live game trade: {game.get('short_name', '')}",
                format_trade_email(
                    event="executed",
                    market=matched["question"],
                    side=edge_info["side"],
                    size=size,
                    price=edge_info["buy_price"],
                    edge=edge_info["edge"],
                ),
            )

        log.info("lg_cycle_complete", games_checked=len(games))
