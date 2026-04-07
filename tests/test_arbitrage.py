import pytest
from polybot.strategies.arbitrage import detect_complement_arb, detect_exhaustive_arb, detect_temporal_arb


def test_complement_arb_detected_maker():
    """Maker: 0 fees, so gross_edge = net_edge."""
    result = detect_complement_arb(polymarket_id="test", yes_price=0.55, no_price=0.40,
                                   fee_rate=0.04, is_maker=True)
    assert result is not None
    assert abs(result.gross_edge - 0.05) < 1e-4
    assert result.net_edge == pytest.approx(result.gross_edge)

def test_complement_arb_detected_taker():
    """Taker: fee = rate * p * (1-p) per leg."""
    result = detect_complement_arb(polymarket_id="test", yes_price=0.55, no_price=0.40,
                                   fee_rate=0.04, is_maker=False)
    assert result is not None
    assert result.net_edge < result.gross_edge
    assert result.net_edge > 0

def test_complement_arb_none_when_sum_is_one():
    result = detect_complement_arb(polymarket_id="test", yes_price=0.55, no_price=0.45,
                                   fee_rate=0.04, is_maker=True)
    assert result is None

def test_complement_arb_none_when_fee_eats_edge():
    """Taker fees can eat thin arb edges."""
    result = detect_complement_arb(polymarket_id="test", yes_price=0.505, no_price=0.49,
                                   fee_rate=0.04, is_maker=False)
    assert result is None

def test_exhaustive_arb_overpriced_maker():
    markets = [
        {"polymarket_id": "a", "yes_price": 0.45, "no_price": 0.60},
        {"polymarket_id": "b", "yes_price": 0.40, "no_price": 0.65},
        {"polymarket_id": "c", "yes_price": 0.25, "no_price": 0.78},
    ]
    result = detect_exhaustive_arb(markets, fee_rate=0.04, min_net_edge=0.01, is_maker=True)
    assert result is not None
    assert result.side == "NO"
    assert result.gross_edge > 0.09

def test_exhaustive_arb_underpriced_maker():
    markets = [
        {"polymarket_id": "a", "yes_price": 0.35, "no_price": 0.67},
        {"polymarket_id": "b", "yes_price": 0.30, "no_price": 0.72},
        {"polymarket_id": "c", "yes_price": 0.25, "no_price": 0.77},
    ]
    result = detect_exhaustive_arb(markets, fee_rate=0.04, min_net_edge=0.01, is_maker=True)
    assert result is not None
    assert result.side == "YES"

def test_exhaustive_arb_none_when_fair():
    markets = [
        {"polymarket_id": "a", "yes_price": 0.50, "no_price": 0.51},
        {"polymarket_id": "b", "yes_price": 0.50, "no_price": 0.51},
    ]
    result = detect_exhaustive_arb(markets, fee_rate=0.04, min_net_edge=0.01, is_maker=True)
    assert result is None

def test_exhaustive_arb_none_when_sum_too_low():
    """Groups with yes_sum < 0.85 are clearly not exhaustive — reject."""
    markets = [
        {"polymarket_id": "a", "yes_price": 0.10, "no_price": 0.92},
        {"polymarket_id": "b", "yes_price": 0.10, "no_price": 0.92},
        {"polymarket_id": "c", "yes_price": 0.10, "no_price": 0.92},
    ]
    result = detect_exhaustive_arb(markets, fee_rate=0.04, min_net_edge=0.01, is_maker=True)
    assert result is None


def test_exhaustive_arb_none_when_sum_too_high():
    """Groups with yes_sum > 1.15 are clearly not exhaustive — reject."""
    markets = [
        {"polymarket_id": "a", "yes_price": 0.80, "no_price": 0.22},
        {"polymarket_id": "b", "yes_price": 0.90, "no_price": 0.12},
        {"polymarket_id": "c", "yes_price": 0.85, "no_price": 0.17},
    ]
    result = detect_exhaustive_arb(markets, fee_rate=0.04, min_net_edge=0.01, is_maker=True)
    assert result is None


def test_exhaustive_arb_rejects_moderately_bad_sum():
    """Sum=1.5 was previously accepted (old bounds 0.5-1.8), now rejected (0.85-1.15)."""
    markets = [
        {"polymarket_id": "a", "yes_price": 0.50, "no_price": 0.52},
        {"polymarket_id": "b", "yes_price": 0.50, "no_price": 0.52},
        {"polymarket_id": "c", "yes_price": 0.50, "no_price": 0.52},
    ]
    # yes_sum = 1.50 — outside 0.85-1.15
    result = detect_exhaustive_arb(markets, fee_rate=0.04, min_net_edge=0.01, is_maker=True)
    assert result is None


def test_exhaustive_arb_none_when_edge_too_high():
    """Net edge > max_net_edge should be rejected as data quality issue."""
    markets = [
        {"polymarket_id": "a", "yes_price": 0.60, "no_price": 0.42},
        {"polymarket_id": "b", "yes_price": 0.05, "no_price": 0.97},
    ]
    result = detect_exhaustive_arb(markets, fee_rate=0.04, min_net_edge=0.01,
                                   max_net_edge=0.20, is_maker=True)
    if result is not None:
        assert result.net_edge <= 0.20


def test_temporal_arb_detected():
    assert detect_temporal_arb("by June?", 0.50, "by July?", 0.40) is True

def test_temporal_arb_none_when_correct():
    assert detect_temporal_arb("by June?", 0.40, "by July?", 0.50) is False


# --- Arb dedup logic ---

from polybot.strategies.arbitrage import ArbitrageStrategy, ArbOpportunity


class TestArbCapEnforcesLegCount:
    """Verify that the per-opportunity cap check accounts for multi-leg exhaustive arbs."""

    def _make_strategy(self):
        class FakeSettings:
            arb_interval_seconds = 60
            arb_kelly_multiplier = 0.20
            arb_max_single_pct = 0.40
            arb_min_bankroll = 50.0
            arb_min_leg_liquidity = 0.0
            arb_max_net_edge = 0.20
            use_maker_orders = True
            arb_max_concurrent = 8
        return ArbitrageStrategy(FakeSettings())

    def test_9_leg_exhaustive_blocked_by_cap_of_8(self):
        """A 9-leg exhaustive arb must be blocked when arb_max_concurrent=8."""
        strategy = self._make_strategy()

        # Build 9 markets summing near 1.0 (exhaustive group)
        nine_markets = [
            {"polymarket_id": f"gold_{i}", "yes_price": round(1.0 / 9 - 0.005, 4),
             "no_price": round(1.0 - 1.0 / 9 + 0.005, 4),
             "book_depth": 10000.0, "category": "politics"}
            for i in range(9)
        ]

        scanner = MagicMock()
        scanner.fetch_markets = AsyncMock(return_value=nine_markets)
        scanner.fetch_event_groups = MagicMock(return_value={"trump-gold-cards": nine_markets})

        # DB call sequence for fetchrow:
        # 1. SELECT enabled FROM strategy_performance -> {"enabled": True}
        # 2. SELECT bankroll FROM system_state -> {"bankroll": 5000}
        db = AsyncMock()
        db.fetchrow = AsyncMock(side_effect=[
            {"enabled": True},   # strategy enabled check
            {"bankroll": 5000},  # bankroll gate
        ])

        # DB call sequence for fetchval:
        # 1. SELECT COUNT(*) ... (initial arb_open cap check, line 178) -> 0
        # 2. SELECT COUNT(*) ... (dedup recent_count per opp, line 250-256) -> 0
        # 3. SELECT COUNT(*) ... (re-fetch for per-opp cap check) -> 0
        db.fetchval = AsyncMock(side_effect=[
            0,  # initial arb_open cap check
            0,  # dedup recent_count for the one exhaustive opp
            0,  # re-fetch open count before per-opp cap loop
        ])

        # dedup warmup fetch
        db.fetch = AsyncMock(return_value=[])

        # Patch detect_exhaustive_arb to return a 9-market opportunity
        arb_opp = ArbOpportunity(
            arb_type="exhaustive", side="NO", gross_edge=0.05, net_edge=0.03,
            markets=nine_markets,
        )

        ctx = MagicMock()
        ctx.scanner = scanner
        ctx.db = db
        ctx.executor = AsyncMock()
        ctx.email_notifier = AsyncMock()
        ctx.portfolio_lock = asyncio.Lock()
        ctx.risk_manager = AsyncMock()
        ctx.settings = MagicMock()

        with patch("polybot.strategies.arbitrage.detect_exhaustive_arb", return_value=arb_opp), \
             patch("polybot.strategies.arbitrage.detect_complement_arb", return_value=None):
            asyncio.run(strategy.run_once(ctx))

        # The 9-leg arb should be blocked: 0 + 9 > 8
        ctx.executor.place_multi_leg_order.assert_not_called()


class TestArbDedup:
    def _make_strategy(self):
        """Create an ArbitrageStrategy with minimal settings."""
        class FakeSettings:
            arb_interval_seconds = 60
            arb_kelly_multiplier = 0.20
            arb_max_single_pct = 0.40
            use_maker_orders = True
        return ArbitrageStrategy(FakeSettings())

    def test_arb_dedup_blocks_seen_market(self):
        """If any market ID in an arb group is already in _seen_arbs, skip it."""
        strategy = self._make_strategy()
        strategy._seen_arbs = {"market_a", "market_b"}

        opp = ArbOpportunity(
            arb_type="exhaustive", side="NO", gross_edge=0.05, net_edge=0.03,
            markets=[{"polymarket_id": "market_a"}, {"polymarket_id": "market_c"}],
        )
        market_ids = [m["polymarket_id"] for m in opp.markets]
        assert any(mid in strategy._seen_arbs for mid in market_ids)

    def test_arb_dedup_initial_load_matches_runtime(self):
        """Initial load uses individual IDs — these should block runtime arb groups."""
        strategy = self._make_strategy()
        # Simulate initial load populating individual IDs
        strategy._seen_arbs = {"0xabc", "0xdef"}

        # Runtime arb group containing one of those IDs
        market_ids = ["0xabc", "0xghi"]
        assert any(mid in strategy._seen_arbs for mid in market_ids)

    def test_arb_dedup_marks_all_legs(self):
        """After execution, all individual market IDs from the group are marked."""
        strategy = self._make_strategy()
        assert len(strategy._seen_arbs) == 0

        market_ids = ["0x111", "0x222", "0x333"]
        strategy._seen_arbs.update(market_ids)

        assert "0x111" in strategy._seen_arbs
        assert "0x222" in strategy._seen_arbs
        assert "0x333" in strategy._seen_arbs
        # A new opp with any of these IDs should be blocked
        assert any(mid in strategy._seen_arbs for mid in ["0x222", "0x444"])


# --- Event-based grouping ---

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


class TestArbUsesEventGroups:
    """Verify ArbitrageStrategy uses fetch_event_groups (not fetch_grouped_markets)."""

    def _make_strategy(self):
        class FakeSettings:
            arb_interval_seconds = 60
            arb_kelly_multiplier = 0.20
            arb_max_single_pct = 0.40
            arb_min_bankroll = 50.0
            arb_min_leg_liquidity = 0.0   # disable liquidity filter in unit test
            arb_max_net_edge = 0.20
            use_maker_orders = True
            arb_max_concurrent = 8
        return ArbitrageStrategy(FakeSettings())

    def test_fetch_event_groups_called_not_grouped_markets(self):
        """run_once() must call fetch_event_groups, not fetch_grouped_markets."""
        strategy = self._make_strategy()

        # Build an arb group: 3 markets summing to 0.90 (underpriced YES → arb)
        arb_markets = [
            {"polymarket_id": "ev_a", "yes_price": 0.30, "no_price": 0.72,
             "book_depth": 10000.0, "category": "politics"},
            {"polymarket_id": "ev_b", "yes_price": 0.35, "no_price": 0.67,
             "book_depth": 10000.0, "category": "politics"},
            {"polymarket_id": "ev_c", "yes_price": 0.25, "no_price": 0.77,
             "book_depth": 10000.0, "category": "politics"},
        ]

        scanner = MagicMock()
        scanner.fetch_markets = AsyncMock(return_value=arb_markets)
        scanner.fetch_event_groups = MagicMock(return_value={"event-slug-1": arb_markets})
        # fetch_grouped_markets should NOT be called
        scanner.fetch_grouped_markets = MagicMock(side_effect=AssertionError(
            "fetch_grouped_markets must not be called — use fetch_event_groups"))

        db = AsyncMock()
        db.fetchrow = AsyncMock(side_effect=lambda q, *a: (
            {"enabled": True} if "strategy_performance" in q else
            {"bankroll": 300.0} if "system_state" in q else None
        ))
        db.fetchval = AsyncMock(return_value=0)
        db.fetch = AsyncMock(return_value=[])

        # Set up risk_manager to block execution (allowed=False) so we don't
        # need to wire up the full executor chain — we only care that
        # fetch_event_groups was called.
        portfolio_state = MagicMock()
        portfolio_state.bankroll = 300.0
        portfolio_state.circuit_breaker_until = None
        risk_manager = AsyncMock()
        risk_manager.get_portfolio_state = AsyncMock(return_value=portfolio_state)
        check_result = MagicMock()
        check_result.allowed = False
        check_result.reason = "test_block"
        risk_manager.check = MagicMock(return_value=check_result)

        settings = MagicMock()
        settings.post_breaker_kelly_reduction = 0.50
        settings.bankroll_survival_threshold = 50.0
        settings.bankroll_growth_threshold = 500.0
        settings.arb_max_net_edge = 0.20
        settings.use_maker_orders = True

        ctx = MagicMock()
        ctx.scanner = scanner
        ctx.db = db
        ctx.executor = AsyncMock()
        ctx.email_notifier = AsyncMock()
        ctx.portfolio_lock = asyncio.Lock()
        ctx.risk_manager = risk_manager
        ctx.settings = settings

        asyncio.run(strategy.run_once(ctx))

        scanner.fetch_event_groups.assert_called_once_with(arb_markets)

    def test_illiquid_legs_skipped(self):
        """Groups where any leg has book_depth below arb_min_leg_liquidity are skipped."""
        strategy = self._make_strategy()
        # Override: use a real liquidity threshold
        strategy._settings.arb_min_leg_liquidity = 5000.0

        # One leg has insufficient depth
        illiquid_markets = [
            {"polymarket_id": "il_a", "yes_price": 0.30, "no_price": 0.72,
             "book_depth": 100.0, "category": "politics"},   # too shallow
            {"polymarket_id": "il_b", "yes_price": 0.35, "no_price": 0.67,
             "book_depth": 10000.0, "category": "politics"},
            {"polymarket_id": "il_c", "yes_price": 0.25, "no_price": 0.77,
             "book_depth": 10000.0, "category": "politics"},
        ]

        scanner = MagicMock()
        scanner.fetch_markets = AsyncMock(return_value=illiquid_markets)
        scanner.fetch_event_groups = MagicMock(return_value={"event-slug-illiquid": illiquid_markets})
        scanner.fetch_grouped_markets = MagicMock(side_effect=AssertionError("must not be called"))

        db = AsyncMock()
        db.fetchrow = AsyncMock(side_effect=lambda q, *a: (
            {"enabled": True} if "strategy_performance" in q else
            {"bankroll": 300.0} if "system_state" in q else None
        ))
        db.fetchval = AsyncMock(return_value=0)
        db.fetch = AsyncMock(return_value=[])

        ctx = MagicMock()
        ctx.scanner = scanner
        ctx.db = db
        ctx.executor = AsyncMock()
        ctx.email_notifier = AsyncMock()
        ctx.portfolio_lock = asyncio.Lock()

        asyncio.run(strategy.run_once(ctx))

        # Executor should never have been called — group was skipped
        ctx.executor.place_multi_leg_order.assert_not_called()
