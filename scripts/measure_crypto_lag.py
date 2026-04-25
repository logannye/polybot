"""Crypto Spot-Lag Empirical Measurement (v11.1 Layer-0 gate).

Runs alongside the bot to record lag observations on Polymarket BTC/ETH
price markets. Per the v11 spec § 7, this is a HARD GATE before any
v11.1 crypto strategy code ships. The recorded data answers:

  1. Does a tradeable lag exist?  (mean tail edge ≥ 3%)
  2. Does it persist long enough to fill?  (median ≥ 60s)
  3. Is the spot-source basis tight enough?  (Coinbase vs Binance 99p ≤ 0.3%)

If any criterion fails after 48h-7d of data, the v11.1 strategy track
is killed and engineering redirects.

Usage:
    uv run python scripts/measure_crypto_lag.py [--once]

Standalone — does not require the bot to be running. Polls every 30s.
Writes to polybot.crypto_lag_observations.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import re
import signal
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import asyncpg
import structlog

log = structlog.get_logger()

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://logannye@localhost:5432/polybot")
POLL_INTERVAL_SECONDS = 30.0
COINBASE_TICKER_URL = "https://api.exchange.coinbase.com/products/{product}/ticker"
COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/{product}/candles"
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

# The /events endpoint with tag_slug='crypto-prices' returns the structured
# price-target markets (e.g., "Will Bitcoin reach $200k by Dec 31, 2026?").
# /markets?tag_slug=crypto returns a generic mix that doesn't include these.
CRYPTO_EVENTS_TAG_SLUG = "crypto-prices"

# Map symbol → (coinbase product, binance pair).
SYMBOL_VENUES = {
    "BTC": ("BTC-USD", "BTCUSDT"),
    "ETH": ("ETH-USD", "ETHUSDT"),
}


# -----------------------------------------------------------------------------
# DB schema bootstrap
# -----------------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS crypto_lag_observations (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    market_id       TEXT NOT NULL,
    question        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    question_kind   TEXT NOT NULL,             -- 'european' | 'barrier_up' | 'barrier_down'
    side            TEXT NOT NULL,             -- 'yes_means_above' | 'yes_means_below' | 'yes_means_touch'
    strike          NUMERIC NOT NULL,
    resolution_ts   TIMESTAMPTZ,
    tau_hours       NUMERIC,
    polymarket_yes  NUMERIC,
    polymarket_no   NUMERIC,
    polymarket_depth NUMERIC,
    coinbase_spot   NUMERIC,
    binance_spot    NUMERIC,
    spot_divergence_pct NUMERIC,
    sigma_30d       NUMERIC,
    p_model_yes     NUMERIC,                   -- model's P(YES resolves)
    p_market_yes    NUMERIC,                   -- = polymarket_yes
    raw_edge        NUMERIC,
    parser_confidence NUMERIC                  -- 0-1
);

CREATE INDEX IF NOT EXISTS idx_crypto_lag_ts ON crypto_lag_observations(ts);
CREATE INDEX IF NOT EXISTS idx_crypto_lag_market ON crypto_lag_observations(market_id, ts);
CREATE INDEX IF NOT EXISTS idx_crypto_lag_symbol ON crypto_lag_observations(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_crypto_lag_kind ON crypto_lag_observations(question_kind, ts);
"""


# -----------------------------------------------------------------------------
# Market parser
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class CryptoMarketSpec:
    market_id: str
    question: str
    symbol: str                 # 'BTC' | 'ETH'
    question_kind: str          # 'european' | 'barrier_up' | 'barrier_down'
    side: str                   # what YES resolution means in this market
    strike: float               # strike price in USD
    resolution_ts: Optional[datetime]
    parser_confidence: float    # 0-1
    yes_price: float
    no_price: float
    depth_usd: float


# Patterns we recognize. confidence reflects regex match strength.
_SYMBOL_RE = re.compile(r"\b(bitcoin|btc|ethereum|eth)\b", re.IGNORECASE)
_STRIKE_RE = re.compile(
    r"\$?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)\s*(k|K|m|M)?")
# "Reach"/"hit" semantics: YES if spot ever touches strike during the period.
# "Dip to": same — barrier touch from above.
# "Close above"/"end the month above": European (price-at-expiry).
_BARRIER_UP_RE = re.compile(
    r"\b(reach|hit|exceed|cross|breach|touch)\b", re.IGNORECASE)
_BARRIER_DOWN_RE = re.compile(
    r"\b(dip to|drop to|fall to|crash to|decline to|sink to)\b", re.IGNORECASE)
_EUROPEAN_ABOVE_RE = re.compile(
    r"\b(close above|end (the )?(month|year|day) above|above .* (on|at|by) [a-zA-Z])\b",
    re.IGNORECASE)
_EUROPEAN_BELOW_RE = re.compile(
    r"\b(close below|end (the )?(month|year|day) below|below .* (on|at|by) [a-zA-Z])\b",
    re.IGNORECASE)


def _parse_strike(text: str) -> Optional[tuple[float, float]]:
    """Returns (strike_usd, regex_confidence) or None."""
    m = _STRIKE_RE.search(text)
    if not m:
        return None
    raw, multiplier = m.group(1), m.group(2)
    try:
        val = float(raw.replace(",", ""))
    except ValueError:
        return None
    if multiplier and multiplier.lower() == "k":
        val *= 1_000
    elif multiplier and multiplier.lower() == "m":
        val *= 1_000_000
    if val < 100:
        # Reject tiny numbers — likely not a price strike.
        return None
    # Confidence: explicit $ or thousands separator boosts confidence.
    conf = 0.6
    if "$" in m.group(0):
        conf += 0.2
    if "," in raw:
        conf += 0.1
    if multiplier:
        conf += 0.1
    return val, min(conf, 1.0)


def _parse_symbol(text: str) -> Optional[tuple[str, float]]:
    m = _SYMBOL_RE.search(text)
    if not m:
        return None
    token = m.group(1).lower()
    if token in ("bitcoin", "btc"):
        return "BTC", 0.95
    if token in ("ethereum", "eth"):
        return "ETH", 0.95
    return None


def _classify_question_kind(question: str) -> tuple[str, str, float]:
    """Returns (question_kind, side_semantics, confidence).

    question_kind:
      - 'european':       resolution depends on price AT a specific time
      - 'barrier_up':     YES if spot touches strike from below at any point
      - 'barrier_down':   YES if spot touches strike from above at any point

    side_semantics describes what YES resolution implies about the price
    relative to strike, used for downstream model selection.
    """
    if _EUROPEAN_ABOVE_RE.search(question):
        return "european", "yes_means_above", 0.85
    if _EUROPEAN_BELOW_RE.search(question):
        return "european", "yes_means_below", 0.85
    if _BARRIER_DOWN_RE.search(question):
        return "barrier_down", "yes_means_touch", 0.90
    if _BARRIER_UP_RE.search(question):
        return "barrier_up", "yes_means_touch", 0.90
    # Default: treat as European above (lower confidence) — the most
    # common ambiguous case is "Will X be above $Y by Z?".
    return "european", "yes_means_above", 0.4


def parse_crypto_market(market_dict: dict) -> Optional[CryptoMarketSpec]:
    """Extract a CryptoMarketSpec from a Gamma market dict, or None.

    Confidence floor: the result's parser_confidence is conservative;
    consumers should treat <0.7 as "log but skip" for trade decisions.
    For lag measurement, we record everything ≥0.6 to build the dataset.
    """
    question = (market_dict.get("question") or "").strip()
    if not question:
        return None
    sym = _parse_symbol(question)
    if not sym:
        return None
    symbol, sym_conf = sym
    if symbol not in SYMBOL_VENUES:
        return None
    strike = _parse_strike(question)
    if not strike:
        return None
    strike_val, strike_conf = strike

    question_kind, side, side_conf = _classify_question_kind(question)
    confidence = (sym_conf + strike_conf + side_conf) / 3.0

    end_date_raw = market_dict.get("endDate") or market_dict.get("end_date")
    resolution_ts = None
    if end_date_raw:
        try:
            resolution_ts = datetime.fromisoformat(
                end_date_raw.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            resolution_ts = None

    # Prices come as a JSON string list.
    import json
    prices_raw = market_dict.get("outcomePrices") or "[]"
    try:
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        yes_price = float(prices[0]) if len(prices) >= 2 else 0.5
        no_price = float(prices[1]) if len(prices) >= 2 else 0.5
    except (ValueError, TypeError, IndexError):
        yes_price = no_price = 0.5

    depth = 0.0
    try:
        depth = float(
            market_dict.get("liquidity") or market_dict.get("liquidityNum") or 0)
    except (ValueError, TypeError):
        depth = 0.0

    return CryptoMarketSpec(
        market_id=str(market_dict.get("conditionId") or market_dict.get("id") or ""),
        question=question,
        symbol=symbol,
        question_kind=question_kind,
        side=side,
        strike=strike_val,
        resolution_ts=resolution_ts,
        parser_confidence=confidence,
        yes_price=yes_price,
        no_price=no_price,
        depth_usd=depth,
    )


# -----------------------------------------------------------------------------
# Spot fetch
# -----------------------------------------------------------------------------
async def fetch_coinbase_spot(session: aiohttp.ClientSession,
                               product: str) -> Optional[float]:
    try:
        async with session.get(
            COINBASE_TICKER_URL.format(product=product), timeout=5
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return float(data["price"])
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError):
        return None


async def fetch_binance_spot(session: aiohttp.ClientSession,
                              symbol_pair: str) -> Optional[float]:
    try:
        async with session.get(
            BINANCE_TICKER_URL, params={"symbol": symbol_pair}, timeout=5
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return float(data["price"])
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError):
        return None


# -----------------------------------------------------------------------------
# Realized vol (cached per symbol, refreshed hourly)
# -----------------------------------------------------------------------------
@dataclass
class VolCache:
    last_refresh: dict[str, datetime] = field(default_factory=dict)
    sigmas: dict[str, float] = field(default_factory=dict)


_vol_cache = VolCache()


async def get_realized_vol_30d(session: aiohttp.ClientSession,
                                product: str) -> Optional[float]:
    """30d realized vol from 1h Coinbase candles, annualized.

    Note: 30d at 1h resolution = 720 bars, well within Coinbase's 300-bar
    response cap, so we use 7d and scale. Adequate for the lag-measurement
    use case — strategy code in v11.1 will use full 30d via batched calls.
    """
    cached_at = _vol_cache.last_refresh.get(product)
    if cached_at and (datetime.now(timezone.utc) - cached_at).total_seconds() < 3600:
        return _vol_cache.sigmas.get(product)
    try:
        async with session.get(
            COINBASE_CANDLES_URL.format(product=product),
            params={"granularity": "3600"}, timeout=10
        ) as resp:
            if resp.status != 200:
                return _vol_cache.sigmas.get(product)
            candles = await resp.json()
            if not isinstance(candles, list) or len(candles) < 24:
                return _vol_cache.sigmas.get(product)
            # candle = [time, low, high, open, close, volume]
            closes = [c[4] for c in reversed(candles) if c and len(c) >= 5]
            if len(closes) < 24:
                return _vol_cache.sigmas.get(product)
            log_returns = [
                math.log(closes[i] / closes[i - 1])
                for i in range(1, len(closes))
                if closes[i - 1] > 0
            ]
            if len(log_returns) < 10:
                return _vol_cache.sigmas.get(product)
            stdev = statistics.pstdev(log_returns)
            # Annualize from hourly: sqrt(8760 hours/year)
            sigma = stdev * math.sqrt(8760)
            _vol_cache.last_refresh[product] = datetime.now(timezone.utc)
            _vol_cache.sigmas[product] = sigma
            return sigma
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, IndexError):
        return _vol_cache.sigmas.get(product)


# -----------------------------------------------------------------------------
# BS implied probability
# -----------------------------------------------------------------------------
def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def implied_prob_above(spot: float, strike: float, tau_years: float,
                        sigma_annual: float) -> Optional[float]:
    """European P(spot at expiry > strike) under driftless GBM."""
    if spot <= 0 or strike <= 0 or tau_years <= 0 or sigma_annual <= 0:
        return None
    try:
        d = (math.log(strike / spot) - (-0.5 * sigma_annual * sigma_annual) * tau_years) \
            / (sigma_annual * math.sqrt(tau_years))
    except (ValueError, ZeroDivisionError):
        return None
    return 1.0 - _normal_cdf(d)


def implied_prob_barrier_touch(spot: float, strike: float, tau_years: float,
                                 sigma_annual: float) -> Optional[float]:
    """P(max or min of spot over [0,T] touches strike), under driftless GBM.

    Reflection-principle result: for a one-sided barrier at level K,
    P(touch from below | S_0 < K) = 2 × P(S_T ≥ K)
    P(touch from above | S_0 > K) = 2 × P(S_T ≤ K)
    Capped at 1 — the formula is exact under driftless GBM and slightly
    underestimates touch probability when drift is nonzero in the
    direction of the barrier (which we treat as zero in v1).
    """
    if spot <= 0 or strike <= 0 or tau_years <= 0 or sigma_annual <= 0:
        return None
    p_above_at_expiry = implied_prob_above(spot, strike, tau_years, sigma_annual)
    if p_above_at_expiry is None:
        return None
    if spot < strike:
        # Barrier above current spot — touch probability ≈ 2 × P(S_T > K)
        return min(1.0, 2.0 * p_above_at_expiry)
    # Barrier below current spot — touch probability ≈ 2 × P(S_T < K)
    return min(1.0, 2.0 * (1.0 - p_above_at_expiry))


def implied_prob_yes(spec: CryptoMarketSpec, spot: float, tau_years: float,
                      sigma_annual: float) -> Optional[float]:
    """Map (question_kind, side) → P(YES resolves)."""
    if spec.question_kind == "european":
        p_above = implied_prob_above(spot, spec.strike, tau_years, sigma_annual)
        if p_above is None:
            return None
        return p_above if spec.side == "yes_means_above" else (1.0 - p_above)
    if spec.question_kind in ("barrier_up", "barrier_down"):
        # Both barrier types resolve YES on touch; the difference is just
        # which side of spot the strike is on, which the touch formula
        # already handles via the spot vs strike comparison.
        return implied_prob_barrier_touch(spot, spec.strike, tau_years, sigma_annual)
    return None


# -----------------------------------------------------------------------------
# Market discovery
# -----------------------------------------------------------------------------
async def fetch_crypto_markets(session: aiohttp.ClientSession) -> list[dict]:
    """Pull active crypto-prices events from Gamma and flatten to markets.

    Each crypto event (e.g., "What price will Bitcoin hit in 2026?")
    contains 3-12 markets with different (strike, date) pairs. We
    flatten so each row in the return list is a single market.
    """
    params = {
        "active": "true",
        "closed": "false",
        "tag_slug": CRYPTO_EVENTS_TAG_SLUG,
        "limit": "100",
    }
    out: list[dict] = []
    seen_ids: set[str] = set()
    try:
        async with session.get(GAMMA_EVENTS_URL, params=params,
                                 timeout=15) as resp:
            if resp.status != 200:
                log.warning("gamma_events_non_200", status=resp.status)
                return []
            data = await resp.json()
            if not isinstance(data, list):
                return []
            for ev in data:
                event_end = ev.get("endDate") or ev.get("end_date")
                for m in (ev.get("markets") or []):
                    if not m.get("active") or m.get("closed"):
                        continue
                    if m.get("acceptingOrders") is False:
                        continue
                    mid = str(m.get("conditionId") or m.get("id") or "")
                    if not mid or mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                    # Inherit endDate from the event if the market doesn't
                    # have its own. Crypto event markets typically carry
                    # per-market endDate already.
                    if not m.get("endDate"):
                        m["endDate"] = event_end
                    out.append(m)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        log.warning("gamma_events_error", error=str(e))
        return []
    return out


# -----------------------------------------------------------------------------
# Main loop
# -----------------------------------------------------------------------------
async def run_measurement_cycle(session: aiohttp.ClientSession,
                                  pool: asyncpg.Pool) -> int:
    """One measurement cycle. Returns count of observations recorded."""
    raw_markets = await fetch_crypto_markets(session)
    if not raw_markets:
        log.warning("no_crypto_markets_returned")
        return 0

    parsed: list[CryptoMarketSpec] = []
    for raw in raw_markets:
        spec = parse_crypto_market(raw)
        if spec and spec.parser_confidence >= 0.6:
            parsed.append(spec)

    if not parsed:
        log.warning("no_parseable_crypto_markets",
                    raw_count=len(raw_markets))
        return 0

    # Fetch spot for each symbol once per cycle.
    spots: dict[str, dict[str, Optional[float]]] = {}
    sigmas: dict[str, Optional[float]] = {}
    for symbol, (cb_product, bn_pair) in SYMBOL_VENUES.items():
        cb, bn, sigma = await asyncio.gather(
            fetch_coinbase_spot(session, cb_product),
            fetch_binance_spot(session, bn_pair),
            get_realized_vol_30d(session, cb_product),
        )
        spots[symbol] = {"coinbase": cb, "binance": bn}
        sigmas[symbol] = sigma

    # Compute observation rows
    now = datetime.now(timezone.utc)
    rows = []
    for spec in parsed:
        sym_spots = spots.get(spec.symbol, {})
        cb = sym_spots.get("coinbase")
        bn = sym_spots.get("binance")
        sigma = sigmas.get(spec.symbol)
        if cb is None or sigma is None:
            continue
        # Use coinbase spot as the truth (most common Polymarket resolver).
        divergence_pct = None
        if bn is not None and cb > 0:
            divergence_pct = abs(cb - bn) / cb * 100.0

        tau_hours = None
        tau_years = None
        if spec.resolution_ts:
            delta_s = (spec.resolution_ts - now).total_seconds()
            if delta_s > 0:
                tau_hours = delta_s / 3600.0
                tau_years = delta_s / (365.0 * 24.0 * 3600.0)

        p_model_yes = None
        p_market_yes = spec.yes_price
        raw_edge = None
        if tau_years and tau_years > 0:
            p_model_yes = implied_prob_yes(spec, cb, tau_years, sigma)
            if p_model_yes is not None:
                raw_edge = abs(p_model_yes - p_market_yes)

        rows.append((
            now, spec.market_id, spec.question, spec.symbol,
            spec.question_kind, spec.side,
            spec.strike, spec.resolution_ts, tau_hours,
            spec.yes_price, spec.no_price, spec.depth_usd,
            cb, bn, divergence_pct, sigma,
            p_model_yes, p_market_yes, raw_edge,
            spec.parser_confidence,
        ))

    if rows:
        async with pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO crypto_lag_observations
                (ts, market_id, question, symbol, question_kind, side,
                 strike, resolution_ts, tau_hours, polymarket_yes, polymarket_no,
                 polymarket_depth, coinbase_spot, binance_spot, spot_divergence_pct,
                 sigma_30d, p_model_yes, p_market_yes, raw_edge, parser_confidence)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)
                """,
                rows,
            )

    log.info("lag_measurement_cycle_complete",
             markets_returned=len(raw_markets),
             markets_parsed=len(parsed),
             observations_recorded=len(rows))
    return len(rows)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true",
                         help="Run a single cycle then exit")
    args = parser.parse_args()

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )
    log.info("crypto_lag_measurement_starting", db=DATABASE_URL,
             interval_s=POLL_INTERVAL_SECONDS, once=args.once)

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
    log.info("schema_ready")

    stop_event = asyncio.Event()

    def _stop(*_):
        log.info("stop_requested")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    async with aiohttp.ClientSession(
        headers={"User-Agent": "polybot-lag-measurement/1.0"}
    ) as session:
        cycle = 0
        while not stop_event.is_set():
            cycle += 1
            try:
                await run_measurement_cycle(session, pool)
            except Exception as e:
                log.error("measurement_cycle_failed",
                          cycle=cycle, error=str(e), error_type=type(e).__name__)
            if args.once:
                break
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    await pool.close()
    log.info("crypto_lag_measurement_stopped", cycles=cycle)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
