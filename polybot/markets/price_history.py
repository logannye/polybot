"""Price history scanner — finds big moves in CLOB price history data."""

import asyncio
import structlog

log = structlog.get_logger()


def detect_big_moves(
    prices: list[float],
    threshold: float = 0.05,
) -> dict | None:
    """Detect a significant price move in a price series.

    Compares recent prices (last 20%) against the baseline (first 60%)
    to find moves that haven't fully reverted.

    Args:
        prices: Chronological list of price points (e.g., 1-min intervals).
        threshold: Minimum absolute move to consider significant.

    Returns:
        Dict with direction, magnitude, recent_price, reference_price,
        or None if no significant move detected.
    """
    if len(prices) < 3:
        return None

    baseline_end = max(1, int(len(prices) * 0.6))
    recent_start = max(baseline_end, int(len(prices) * 0.8))

    baseline = prices[:baseline_end]
    recent = prices[recent_start:]

    if not baseline or not recent:
        return None

    baseline_mid = sum(baseline) / len(baseline)
    recent_mid = sum(recent) / len(recent)

    move = recent_mid - baseline_mid

    if abs(move) < threshold:
        return None

    return {
        "direction": "up" if move > 0 else "down",
        "magnitude": abs(move),
        "recent_price": recent_mid,
        "reference_price": baseline_mid,
    }


class PriceHistoryScanner:
    """Scans high-volume markets for recent big price moves via CLOB price history.

    Fetches price history in parallel using asyncio.gather with a concurrency
    semaphore to avoid overwhelming the CLOB API.
    """

    def __init__(
        self,
        scanner,
        min_volume: float = 5000.0,
        move_threshold: float = 0.05,
        max_markets: int = 500,
        concurrency: int = 50,
    ):
        self._scanner = scanner
        self._min_volume = min_volume
        self._move_threshold = move_threshold
        self._max_markets = max_markets
        self._semaphore = asyncio.Semaphore(concurrency)

    async def _fetch_one(self, m: dict) -> dict | None:
        """Fetch price history for one market and check for big moves."""
        async with self._semaphore:
            try:
                prices = await self._scanner.fetch_price_history(
                    m["yes_token_id"], interval="2h")
                if not prices:
                    return None
                result = detect_big_moves(prices, threshold=self._move_threshold)
                if result:
                    return {
                        "polymarket_id": m.get("polymarket_id", ""),
                        "question": m.get("question", ""),
                        "yes_price": m.get("yes_price", 0),
                        **result,
                    }
            except Exception as e:
                log.debug("price_history_scan_error",
                          market=m.get("polymarket_id"), error=str(e))
            return None

    async def scan_for_moves(self) -> list[dict]:
        """Scan top markets by volume for big recent price moves."""
        price_cache = self._scanner.get_all_cached_prices()
        if not price_cache:
            return []

        candidates = [
            m for m in price_cache.values()
            if m.get("volume_24h", 0) >= self._min_volume
            and m.get("yes_token_id")
        ]
        candidates.sort(key=lambda m: m.get("volume_24h", 0), reverse=True)
        candidates = candidates[:self._max_markets]

        if not candidates:
            return []

        results = await asyncio.gather(
            *(self._fetch_one(m) for m in candidates),
            return_exceptions=True)

        moves = [r for r in results if isinstance(r, dict)]

        log.info("price_history_scan_complete",
                 scanned=len(candidates), moves_found=len(moves))
        return moves
