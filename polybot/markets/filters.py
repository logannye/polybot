from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import structlog

log = structlog.get_logger()


@dataclass
class MarketCandidate:
    polymarket_id: str
    question: str
    category: str
    resolution_time: datetime
    current_price: float
    book_depth: float
    last_analyzed_at: datetime | None = None
    previous_price: float | None = None


def filter_markets(markets, resolution_hours_max=72, min_book_depth=500.0, min_price=0.05, max_price=0.95, cooldown_minutes=30, price_move_threshold=0.03):
    now = datetime.now(timezone.utc)
    max_resolution = now + timedelta(hours=resolution_hours_max)
    cooldown_cutoff = now - timedelta(minutes=cooldown_minutes)
    passed = []
    for m in markets:
        if m.resolution_time > max_resolution:
            continue
        if m.resolution_time <= now:
            continue
        if m.book_depth < min_book_depth:
            continue
        if m.current_price < min_price or m.current_price > max_price:
            continue
        if m.last_analyzed_at and m.last_analyzed_at > cooldown_cutoff:
            if m.previous_price is not None:
                if abs(m.current_price - m.previous_price) < price_move_threshold:
                    continue
            else:
                continue
        passed.append(m)
    log.info("filter_complete", input=len(markets), output=len(passed))
    return passed
