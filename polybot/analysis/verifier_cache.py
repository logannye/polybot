"""In-memory cache for snipe verifier results.

Empirical motivation (v12.1, 2026-04-26): the same ~15 markets surface every
60s scan cycle and we were re-verifying each one with Gemini Flash on every
pass. At ~14 calls/cycle × 60 cycles/hour × $0.0015/call ≈ $1.26/hour, the
default $2 daily cap blew at ~T+1.6h, leaving the bot running blind for the
remaining 22+ hours of every UTC day. Caching kills this entirely.

Design:
- Key: polymarket_id only.
- Invalidation triggers (any of):
    1. TTL expiry (default 30 minutes — matches typical book staleness)
    2. yes_price drift > 0.01 since cached entry (1¢ price move = re-evaluate)
    3. hours_remaining drift > 1.0 (resolution clock changed materially)
- Storage: in-memory dict (process-local). Cache rebuilds from scratch on
  restart, which is fine — first cycle after restart re-verifies everything.

Pure side-effect-free policy: this module decides hit/miss + invalidation.
The owning client owns the actual LLM call.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import structlog

from polybot.analysis.gemini_client import GeminiResult

log = structlog.get_logger()


@dataclass(frozen=True)
class CacheEntry:
    result: GeminiResult
    cached_at: datetime
    yes_price: float
    hours_remaining: float


class VerifierCache:
    """Process-local cache of Gemini verifier verdicts."""

    def __init__(self,
                 ttl_seconds: float = 1800.0,         # 30 min
                 price_drift_threshold: float = 0.01,
                 hours_drift_threshold: float = 1.0):
        self._store: dict[str, CacheEntry] = {}
        self._ttl = ttl_seconds
        self._price_drift = price_drift_threshold
        self._hours_drift = hours_drift_threshold
        # Telemetry
        self._hits = 0
        self._misses_ttl = 0
        self._misses_price = 0
        self._misses_hours = 0
        self._misses_absent = 0

    def lookup(self, polymarket_id: str, *, yes_price: float,
               hours_remaining: float,
               now: Optional[datetime] = None) -> Optional[GeminiResult]:
        """Return cached result if still valid, else None.

        Increments per-reason miss counters when invalidated.
        """
        now = now or datetime.now(timezone.utc)
        entry = self._store.get(polymarket_id)
        if entry is None:
            self._misses_absent += 1
            return None
        age = (now - entry.cached_at).total_seconds()
        if age > self._ttl:
            self._misses_ttl += 1
            return None
        if abs(yes_price - entry.yes_price) > self._price_drift:
            self._misses_price += 1
            return None
        if abs(hours_remaining - entry.hours_remaining) > self._hours_drift:
            self._misses_hours += 1
            return None
        self._hits += 1
        return entry.result

    def store(self, polymarket_id: str, result: GeminiResult, *,
              yes_price: float, hours_remaining: float,
              now: Optional[datetime] = None) -> None:
        """Cache a verifier result. Overwrites any existing entry."""
        now = now or datetime.now(timezone.utc)
        self._store[polymarket_id] = CacheEntry(
            result=result, cached_at=now,
            yes_price=yes_price, hours_remaining=hours_remaining,
        )

    def invalidate(self, polymarket_id: str) -> bool:
        """Drop a single entry. Returns True if something was removed."""
        return self._store.pop(polymarket_id, None) is not None

    def clear(self) -> None:
        """Drop all entries. Used at process boundaries."""
        self._store.clear()

    def stats(self) -> dict:
        """Snapshot of cache telemetry — useful in periodic logs."""
        total = (self._hits + self._misses_ttl + self._misses_price
                 + self._misses_hours + self._misses_absent)
        hit_rate = self._hits / total if total else 0.0
        return {
            "size": len(self._store),
            "hits": self._hits,
            "miss_ttl": self._misses_ttl,
            "miss_price": self._misses_price,
            "miss_hours": self._misses_hours,
            "miss_absent": self._misses_absent,
            "hit_rate": round(hit_rate, 4),
        }
