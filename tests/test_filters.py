import pytest
from datetime import datetime, timezone, timedelta
from polybot.markets.filters import MarketCandidate, filter_markets


def _make_market(polymarket_id="m1", resolution_time=None, current_price=0.50, book_depth=1000.0, last_analyzed_at=None, previous_price=None):
    if resolution_time is None:
        resolution_time = datetime.now(timezone.utc) + timedelta(hours=24)
    return MarketCandidate(polymarket_id=polymarket_id, question="Will X happen?", category="politics",
        resolution_time=resolution_time, current_price=current_price, book_depth=book_depth,
        last_analyzed_at=last_analyzed_at, previous_price=previous_price)


class TestFilterMarkets:
    def test_passes_valid_market(self):
        assert len(filter_markets([_make_market()])) == 1

    def test_rejects_long_dated(self):
        assert len(filter_markets([_make_market(resolution_time=datetime.now(timezone.utc) + timedelta(hours=100))], resolution_hours_max=72)) == 0

    def test_rejects_low_liquidity(self):
        assert len(filter_markets([_make_market(book_depth=200.0)], min_book_depth=500.0)) == 0

    def test_rejects_extreme_low_price(self):
        assert len(filter_markets([_make_market(current_price=0.03)], min_price=0.05)) == 0

    def test_rejects_extreme_high_price(self):
        assert len(filter_markets([_make_market(current_price=0.97)], max_price=0.95)) == 0

    def test_rejects_recently_analyzed_no_price_move(self):
        m = _make_market(last_analyzed_at=datetime.now(timezone.utc) - timedelta(minutes=10), previous_price=0.50)
        assert len(filter_markets([m], cooldown_minutes=30, price_move_threshold=0.03)) == 0

    def test_allows_recently_analyzed_with_price_move(self):
        m = _make_market(current_price=0.55, last_analyzed_at=datetime.now(timezone.utc) - timedelta(minutes=10), previous_price=0.50)
        assert len(filter_markets([m], cooldown_minutes=30, price_move_threshold=0.03)) == 1

    def test_allows_old_analysis(self):
        m = _make_market(last_analyzed_at=datetime.now(timezone.utc) - timedelta(minutes=60), previous_price=0.50)
        assert len(filter_markets([m], cooldown_minutes=30, price_move_threshold=0.03)) == 1

    def test_multiple_markets_mixed(self):
        markets = [_make_market(polymarket_id="good"), _make_market(polymarket_id="bad_price", current_price=0.01), _make_market(polymarket_id="bad_depth", book_depth=100.0)]
        result = filter_markets(markets, min_book_depth=500.0, min_price=0.05)
        assert len(result) == 1
        assert result[0].polymarket_id == "good"
