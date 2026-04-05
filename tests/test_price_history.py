import pytest
from unittest.mock import AsyncMock, MagicMock
from polybot.markets.price_history import detect_big_moves


class TestDetectBigMoves:
    def test_detects_drop(self):
        prices = [0.60] * 15 + [0.55, 0.52, 0.50, 0.50, 0.50]
        result = detect_big_moves(prices, threshold=0.05)
        assert result is not None
        assert result["direction"] == "down"
        assert result["magnitude"] >= 0.09
        assert result["recent_price"] == pytest.approx(0.505)
        assert result["reference_price"] == pytest.approx(0.60)

    def test_detects_spike(self):
        prices = [0.40] * 15 + [0.45, 0.48, 0.50, 0.52, 0.52]
        result = detect_big_moves(prices, threshold=0.05)
        assert result is not None
        assert result["direction"] == "up"
        assert result["magnitude"] >= 0.10

    def test_ignores_small_moves(self):
        prices = [0.50] * 15 + [0.51, 0.52, 0.53, 0.53, 0.53]
        result = detect_big_moves(prices, threshold=0.05)
        assert result is None

    def test_ignores_fully_reverted(self):
        # The series spikes then reverts: baseline avg includes the spike,
        # recent window ends at 0.50 which is below the baseline avg (0.5575).
        # With threshold=0.10 the move (0.0575) is filtered out.
        prices = [0.50, 0.55, 0.60, 0.58, 0.55, 0.52, 0.50, 0.50]
        result = detect_big_moves(prices, threshold=0.10)
        assert result is None

    def test_detects_partial_reversion(self):
        prices = [0.50] * 10 + [0.55, 0.60, 0.62, 0.60, 0.58]
        result = detect_big_moves(prices, threshold=0.05)
        assert result is not None
        assert result["direction"] == "up"

    def test_empty_prices(self):
        assert detect_big_moves([], threshold=0.05) is None

    def test_too_few_prices(self):
        assert detect_big_moves([0.50, 0.60], threshold=0.05) is None


from polybot.markets.price_history import PriceHistoryScanner


@pytest.mark.asyncio
async def test_scanner_finds_big_move():
    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-1": {
            "yes_price": 0.55, "no_price": 0.45,
            "yes_token_id": "tok_yes_1", "no_token_id": "tok_no_1",
            "volume_24h": 50000, "question": "Big mover?",
            "polymarket_id": "mkt-1",
        },
    }
    scanner.fetch_price_history = AsyncMock(return_value=[
        0.60, 0.60, 0.60, 0.60, 0.60,
        0.60, 0.60, 0.58, 0.56, 0.55,
        0.55, 0.54, 0.53, 0.53, 0.53,
    ])

    phs = PriceHistoryScanner(
        scanner=scanner, min_volume=1000, move_threshold=0.05, max_markets=50)
    moves = await phs.scan_for_moves()

    assert len(moves) >= 1
    assert moves[0]["polymarket_id"] == "mkt-1"
    assert moves[0]["direction"] == "down"


@pytest.mark.asyncio
async def test_scanner_skips_low_volume():
    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-low": {
            "yes_price": 0.50, "volume_24h": 100,
            "yes_token_id": "tok", "polymarket_id": "mkt-low",
            "question": "Low volume?",
        },
    }
    scanner.fetch_price_history = AsyncMock()

    phs = PriceHistoryScanner(scanner=scanner, min_volume=1000)
    moves = await phs.scan_for_moves()

    assert len(moves) == 0
    scanner.fetch_price_history.assert_not_called()


@pytest.mark.asyncio
async def test_scanner_handles_empty_history():
    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-empty": {
            "yes_price": 0.50, "volume_24h": 5000,
            "yes_token_id": "tok", "polymarket_id": "mkt-empty",
            "question": "Empty history?",
        },
    }
    scanner.fetch_price_history = AsyncMock(return_value=[])

    phs = PriceHistoryScanner(scanner=scanner, min_volume=1000)
    moves = await phs.scan_for_moves()

    assert len(moves) == 0


import asyncio


@pytest.mark.asyncio
async def test_scanner_fetches_in_parallel():
    """Scanner should fetch multiple markets concurrently, not sequentially."""
    call_times = []

    async def mock_fetch(token_id, interval="1h"):
        call_times.append(asyncio.get_event_loop().time())
        await asyncio.sleep(0.01)
        return [0.60] * 12 + [0.50, 0.50, 0.50]

    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        f"mkt-{i}": {
            "yes_price": 0.50, "volume_24h": 10000,
            "yes_token_id": f"tok_{i}", "polymarket_id": f"mkt-{i}",
            "question": f"Market {i}?",
        }
        for i in range(10)
    }
    scanner.fetch_price_history = mock_fetch

    phs = PriceHistoryScanner(
        scanner=scanner, min_volume=1000, move_threshold=0.05, max_markets=50,
        concurrency=5)
    moves = await phs.scan_for_moves()

    assert len(moves) == 10
    elapsed = call_times[-1] - call_times[0]
    assert elapsed < 0.05


@pytest.mark.asyncio
async def test_scanner_respects_concurrency_limit():
    """Scanner should not exceed the concurrency limit."""
    max_concurrent = 0
    current_concurrent = 0
    lock = asyncio.Lock()

    async def mock_fetch(token_id, interval="1h"):
        nonlocal max_concurrent, current_concurrent
        async with lock:
            current_concurrent += 1
            max_concurrent = max(max_concurrent, current_concurrent)
        await asyncio.sleep(0.02)
        async with lock:
            current_concurrent -= 1
        return [0.60] * 12 + [0.50, 0.50, 0.50]

    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        f"mkt-{i}": {
            "yes_price": 0.50, "volume_24h": 10000,
            "yes_token_id": f"tok_{i}", "polymarket_id": f"mkt-{i}",
            "question": f"Market {i}?",
        }
        for i in range(20)
    }
    scanner.fetch_price_history = mock_fetch

    phs = PriceHistoryScanner(
        scanner=scanner, min_volume=1000, move_threshold=0.05,
        max_markets=50, concurrency=3)
    await phs.scan_for_moves()

    assert max_concurrent <= 3


@pytest.mark.asyncio
async def test_scanner_one_failure_doesnt_block_others():
    """A single market failing should not prevent other markets from being scanned."""
    call_count = 0

    async def mock_fetch(token_id, interval="1h"):
        nonlocal call_count
        call_count += 1
        if token_id == "tok_bad":
            raise ConnectionError("API down")
        return [0.60] * 12 + [0.50, 0.50, 0.50]

    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-good": {
            "yes_price": 0.50, "volume_24h": 10000,
            "yes_token_id": "tok_good", "polymarket_id": "mkt-good",
            "question": "Good?",
        },
        "mkt-bad": {
            "yes_price": 0.50, "volume_24h": 20000,
            "yes_token_id": "tok_bad", "polymarket_id": "mkt-bad",
            "question": "Bad?",
        },
    }
    scanner.fetch_price_history = mock_fetch

    phs = PriceHistoryScanner(
        scanner=scanner, min_volume=1000, move_threshold=0.05, max_markets=50)
    moves = await phs.scan_for_moves()

    assert call_count == 2
    assert len(moves) == 1
    assert moves[0]["polymarket_id"] == "mkt-good"
