"""Tests for VerifierCache — TTL, drift, telemetry."""
from datetime import datetime, timezone, timedelta
import pytest

from polybot.analysis.verifier_cache import VerifierCache, CacheEntry
from polybot.analysis.gemini_client import GeminiResult


def _r(verdict="NO_LOCKED", confidence=1.0, reason="x"):
    return GeminiResult(verdict=verdict, confidence=confidence, reason=reason)


def test_lookup_hit_within_all_thresholds():
    c = VerifierCache(ttl_seconds=600.0)
    now = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    c.store("m1", _r(), yes_price=0.97, hours_remaining=4.0, now=now)
    hit = c.lookup("m1", yes_price=0.97, hours_remaining=4.0, now=now)
    assert hit is not None
    assert hit.verdict == "NO_LOCKED"
    assert c.stats()["hits"] == 1


def test_lookup_miss_when_absent():
    c = VerifierCache()
    assert c.lookup("never_seen", yes_price=0.97, hours_remaining=4.0) is None
    assert c.stats()["miss_absent"] == 1


def test_ttl_expiry():
    c = VerifierCache(ttl_seconds=60.0)
    t0 = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    c.store("m1", _r(), yes_price=0.97, hours_remaining=4.0, now=t0)
    later = t0 + timedelta(seconds=120)
    assert c.lookup("m1", yes_price=0.97, hours_remaining=4.0, now=later) is None
    assert c.stats()["miss_ttl"] == 1


def test_price_drift_invalidation():
    c = VerifierCache(ttl_seconds=600.0, price_drift_threshold=0.01)
    now = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    c.store("m1", _r(), yes_price=0.97, hours_remaining=4.0, now=now)
    # Within drift: hit
    assert c.lookup("m1", yes_price=0.975, hours_remaining=4.0, now=now) is not None
    # Beyond drift: miss
    assert c.lookup("m1", yes_price=0.985, hours_remaining=4.0, now=now) is None
    assert c.stats()["miss_price"] == 1


def test_hours_remaining_drift_invalidation():
    c = VerifierCache(ttl_seconds=600.0, hours_drift_threshold=1.0)
    now = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    c.store("m1", _r(), yes_price=0.97, hours_remaining=4.0, now=now)
    assert c.lookup("m1", yes_price=0.97, hours_remaining=4.5, now=now) is not None
    assert c.lookup("m1", yes_price=0.97, hours_remaining=2.5, now=now) is None
    assert c.stats()["miss_hours"] == 1


def test_overwrite_replaces_entry():
    c = VerifierCache(ttl_seconds=600.0)
    now = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    c.store("m1", _r("NO_LOCKED"), yes_price=0.97, hours_remaining=4.0, now=now)
    later = now + timedelta(seconds=300)
    c.store("m1", _r("YES_LOCKED"), yes_price=0.97, hours_remaining=4.0, now=later)
    hit = c.lookup("m1", yes_price=0.97, hours_remaining=4.0, now=later)
    assert hit.verdict == "YES_LOCKED"


def test_invalidate_drops_entry():
    c = VerifierCache()
    now = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    c.store("m1", _r(), yes_price=0.97, hours_remaining=4.0, now=now)
    assert c.invalidate("m1") is True
    assert c.invalidate("m1") is False
    assert c.lookup("m1", yes_price=0.97, hours_remaining=4.0, now=now) is None


def test_clear_drops_all():
    c = VerifierCache()
    now = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    c.store("m1", _r(), yes_price=0.97, hours_remaining=4.0, now=now)
    c.store("m2", _r(), yes_price=0.97, hours_remaining=4.0, now=now)
    c.clear()
    assert c.stats()["size"] == 0


def test_stats_hit_rate_math():
    c = VerifierCache()
    now = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    c.store("m1", _r(), yes_price=0.97, hours_remaining=4.0, now=now)
    c.lookup("m1", yes_price=0.97, hours_remaining=4.0, now=now)    # hit
    c.lookup("m1", yes_price=0.97, hours_remaining=4.0, now=now)    # hit
    c.lookup("m2", yes_price=0.97, hours_remaining=4.0, now=now)    # miss
    s = c.stats()
    assert s["hits"] == 2
    assert s["miss_absent"] == 1
    assert s["hit_rate"] == pytest.approx(2 / 3, abs=1e-3)
