"""Phase 0 transition audit — READ ONLY.

Verifies:
  1. CLOB connectivity + wallet USDC balance
  2. Scanner can fetch markets + price cache populated
  3. For each currently-locked market, real order-book bid/ask spread
     compared against scanner cached price
  4. Maker-fill viability assessment (reject if spread > 5pp)

No orders are placed. No state is mutated.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Ensure we run with polybot's package on path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polybot.core.config import Settings
from polybot.markets.scanner import PolymarketScanner
from polybot.trading.clob import ClobGateway


CLOB_HOST = "https://clob.polymarket.com"


async def main():
    settings = Settings(_env_file=".env")
    print("=" * 70)
    print("PHASE 0 AUDIT  (read-only)")
    print("=" * 70)
    print()

    # ── 1. CLOB + wallet ──────────────────────────────────────────────
    print("[1/4] Initializing CLOB gateway...")
    try:
        clob = ClobGateway(
            host=CLOB_HOST,
            chain_id=int(getattr(settings, "polymarket_chain_id", 137)),
            private_key=settings.polymarket_private_key,
            api_key=settings.polymarket_api_key,
            api_secret=settings.polymarket_api_secret,
            api_passphrase=settings.polymarket_api_passphrase,
        )
        print("    CLOB initialized OK")
    except Exception as e:
        print(f"    FAIL: {e}")
        return

    print("\n[2/4] Wallet USDC balance...")
    try:
        balance = await clob.get_balance()
        print(f"    Wallet USDC = ${balance:.4f}")
        if balance < 1.0:
            print("    ⚠  Balance < $1 — micro-test cannot proceed")
        elif balance < 30.0:
            print(f"    ⚠  Balance ${balance:.2f} is below the $30 micro-test cap; can only test up to balance")
        else:
            print(f"    ✓  Balance sufficient for $30 micro-test budget")
    except Exception as e:
        print(f"    FAIL: {e}")

    # ── 3. Scanner ────────────────────────────────────────────────────
    print("\n[3/4] Initializing scanner + fetching active markets...")
    scanner = PolymarketScanner(api_key=settings.polymarket_api_key)
    await scanner.start()
    try:
        markets = await scanner.fetch_markets()
        print(f"    Active markets fetched: {len(markets)}")
    except Exception as e:
        print(f"    FAIL: {e}")
        await scanner.close()
        return

    # ── 4. Spread analysis on currently-locked market ────────────────
    print("\n[4/4] Maker-fill viability audit on currently-locked markets...")
    landsman_pid = "0x0dd54661592b361dd215f93c4e443621ae49d43c74e45d9ab7580c84f3b20a2c"
    kim_k_pid = "0xad74ec4c2b537ba1d1c914dee6a9a551136da2bc6706908aa406b028a5ce4849"
    target_pids = [landsman_pid, kim_k_pid]

    for pid in target_pids:
        market = next((m for m in markets if m.get("polymarket_id") == pid), None)
        if not market:
            print(f"    Market {pid[:18]}... not in active feed — likely already past resolution")
            continue

        cached_yes = market.get("yes_price")
        no_token_id = market.get("no_token_id")
        yes_token_id = market.get("yes_token_id")

        print(f"\n  Market: {market.get('question', '')[:65]}")
        print(f"    polymarket_id: {pid[:18]}...")
        print(f"    Scanner cached YES price: {cached_yes}")
        print(f"    Hours to resolution: {((market.get('resolution_time') - __import__('datetime').datetime.now(__import__('datetime').timezone.utc)).total_seconds()/3600):.1f}")

        for side, token_id in [("NO", no_token_id), ("YES", yes_token_id)]:
            if not token_id:
                print(f"    {side} token_id missing")
                continue
            print(f"\n    --- {side} side (token_id {token_id[:20]}...) ---")
            try:
                summary = await clob.get_order_book_summary(token_id)
                if summary is None:
                    print(f"    No order book — empty or error")
                    continue
                spread_pp = summary["spread"]
                print(f"    best_bid={summary['best_bid']:.4f}  best_ask={summary['best_ask']:.4f}  spread={spread_pp:.4f} ({spread_pp*100:.2f}pp)")

                # Maker-fill viability check
                if spread_pp > 0.05:
                    print(f"    ⚠  SPREAD > 5pp — post_only orders unlikely to fill")
                elif spread_pp > 0.02:
                    print(f"    ⚠  SPREAD 2-5pp — fills possible but slippage risk")
                else:
                    print(f"    ✓  SPREAD ≤ 2pp — maker fills realistic")

                # Compare to scanner cache (NO side: 1 - cached_yes)
                if side == "NO" and cached_yes is not None:
                    expected_no_price = 1.0 - float(cached_yes)
                    book_mid = (summary["best_bid"] + summary["best_ask"]) / 2.0
                    deviation = abs(book_mid - expected_no_price)
                    print(f"    Scanner-derived NO mid: {expected_no_price:.4f}  vs book mid: {book_mid:.4f}  Δ={deviation:.4f}")
                    if deviation > 0.05:
                        print(f"    ⚠  DEVIATION > 5pp — scanner cache may be stale or computing differently than book")
            except Exception as e:
                print(f"    FAIL: {e}")

    await scanner.close()
    print("\n" + "=" * 70)
    print("Phase 0 audit complete (read-only).")
    print("=" * 70)


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent.parent)
    asyncio.run(main())
