from polybot.analysis.prescore import prescore
from polybot.markets.filters import MarketCandidate
from polybot.learning.categories import CategoryStats
from polybot.analysis.quant import QuantSignals
from datetime import datetime, timezone, timedelta


def make_candidate(price=0.50, book_depth=2000.0, category="politics"):
    return MarketCandidate(
        polymarket_id="test", question="Test?", category=category,
        resolution_time=datetime.now(timezone.utc) + timedelta(hours=48),
        current_price=price, book_depth=book_depth)


def test_prescore_midprice_scores_higher():
    mid = prescore(make_candidate(price=0.50), {}, QuantSignals(0, 0, 0, 0, 0))
    extreme = prescore(make_candidate(price=0.90), {}, QuantSignals(0, 0, 0, 0, 0))
    assert mid > extreme


def test_prescore_deeper_book_scores_higher():
    shallow = prescore(make_candidate(book_depth=200), {}, QuantSignals(0, 0, 0, 0, 0))
    deep = prescore(make_candidate(book_depth=5000), {}, QuantSignals(0, 0, 0, 0, 0))
    assert deep > shallow


def test_prescore_positive_quant_helps():
    zero_quant = prescore(make_candidate(), {}, QuantSignals(0, 0, 0, 0, 0))
    good_quant = prescore(make_candidate(), {}, QuantSignals(0.5, 0.5, 0.5, 0.5, 0.5))
    assert good_quant > zero_quant


def test_prescore_negative_quant_no_penalty():
    zero_quant = prescore(make_candidate(), {}, QuantSignals(0, 0, 0, 0, 0))
    neg_quant = prescore(make_candidate(), {}, QuantSignals(-0.5, -0.5, -0.5, -0.5, -0.5))
    assert abs(zero_quant - neg_quant) < 1e-9


def test_prescore_with_category_bias():
    stats = {"politics": CategoryStats(total_trades=30, total_pnl=15.0, win_count=20)}
    with_bias = prescore(make_candidate(category="politics"), stats, QuantSignals(0, 0, 0, 0, 0))
    without_bias = prescore(make_candidate(category="unknown"), stats, QuantSignals(0, 0, 0, 0, 0))
    assert with_bias > without_bias
