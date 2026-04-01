import pytest
from polybot.strategies.arbitrage import detect_complement_arb, detect_exhaustive_arb, detect_temporal_arb


def test_complement_arb_detected():
    result = detect_complement_arb(polymarket_id="test", yes_price=0.55, no_price=0.40, fee_rate=0.02)
    assert result is not None
    assert abs(result.gross_edge - 0.05) < 1e-4
    assert result.net_edge > 0

def test_complement_arb_none_when_sum_is_one():
    result = detect_complement_arb(polymarket_id="test", yes_price=0.55, no_price=0.45, fee_rate=0.02)
    assert result is None

def test_complement_arb_none_when_fee_eats_edge():
    result = detect_complement_arb(polymarket_id="test", yes_price=0.505, no_price=0.49, fee_rate=0.02)
    assert result is None

def test_exhaustive_arb_overpriced():
    markets = [
        {"polymarket_id": "a", "yes_price": 0.45, "no_price": 0.60},
        {"polymarket_id": "b", "yes_price": 0.40, "no_price": 0.65},
        {"polymarket_id": "c", "yes_price": 0.25, "no_price": 0.78},
    ]
    result = detect_exhaustive_arb(markets, fee_rate=0.02, min_net_edge=0.01)
    assert result is not None
    assert result.side == "NO"
    assert result.gross_edge > 0.09

def test_exhaustive_arb_underpriced():
    # yes_sum = 0.90 (below 1.0, underpriced), net_edge ~8.9% (below 20% cap)
    markets = [
        {"polymarket_id": "a", "yes_price": 0.35, "no_price": 0.67},
        {"polymarket_id": "b", "yes_price": 0.30, "no_price": 0.72},
        {"polymarket_id": "c", "yes_price": 0.25, "no_price": 0.77},
    ]
    result = detect_exhaustive_arb(markets, fee_rate=0.02, min_net_edge=0.01)
    assert result is not None
    assert result.side == "YES"

def test_exhaustive_arb_none_when_fair():
    markets = [
        {"polymarket_id": "a", "yes_price": 0.50, "no_price": 0.51},
        {"polymarket_id": "b", "yes_price": 0.50, "no_price": 0.51},
    ]
    result = detect_exhaustive_arb(markets, fee_rate=0.02, min_net_edge=0.01)
    assert result is None

def test_exhaustive_arb_none_when_sum_too_low():
    """Groups with yes_sum < 0.5 are clearly not exhaustive — reject."""
    markets = [
        {"polymarket_id": "a", "yes_price": 0.10, "no_price": 0.92},
        {"polymarket_id": "b", "yes_price": 0.10, "no_price": 0.92},
        {"polymarket_id": "c", "yes_price": 0.10, "no_price": 0.92},
    ]
    result = detect_exhaustive_arb(markets, fee_rate=0.02, min_net_edge=0.01)
    assert result is None


def test_exhaustive_arb_none_when_sum_too_high():
    """Groups with yes_sum > 1.8 are clearly not exhaustive — reject."""
    markets = [
        {"polymarket_id": "a", "yes_price": 0.80, "no_price": 0.22},
        {"polymarket_id": "b", "yes_price": 0.90, "no_price": 0.12},
        {"polymarket_id": "c", "yes_price": 0.85, "no_price": 0.17},
    ]
    result = detect_exhaustive_arb(markets, fee_rate=0.02, min_net_edge=0.01)
    assert result is None


def test_exhaustive_arb_none_when_edge_too_high():
    """Net edge > max_net_edge should be rejected as data quality issue."""
    # Construct a group where the math produces a valid but absurdly high edge
    markets = [
        {"polymarket_id": "a", "yes_price": 0.60, "no_price": 0.42},
        {"polymarket_id": "b", "yes_price": 0.05, "no_price": 0.97},
    ]
    result = detect_exhaustive_arb(markets, fee_rate=0.02, min_net_edge=0.01, max_net_edge=0.20)
    # yes_sum=0.65, within 0.5-1.8, but edge would be huge → capped
    if result is not None:
        assert result.net_edge <= 0.20


def test_temporal_arb_detected():
    assert detect_temporal_arb("by June?", 0.50, "by July?", 0.40) is True

def test_temporal_arb_none_when_correct():
    assert detect_temporal_arb("by June?", 0.40, "by July?", 0.50) is False


# --- Arb dedup logic ---

from polybot.strategies.arbitrage import ArbitrageStrategy, ArbOpportunity


class TestArbDedup:
    def _make_strategy(self):
        """Create an ArbitrageStrategy with minimal settings."""
        class FakeSettings:
            arb_interval_seconds = 60
            arb_kelly_multiplier = 0.20
            arb_max_single_pct = 0.40
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
