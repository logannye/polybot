#!/usr/bin/env python3
"""Resolve orphaned live trades by checking CLOB order status.

Checks a specific trade's CLOB order, then either marks it as filled
(if matched) or cancels and frees deployed capital (if live/cancelled).

Usage:
    cd ~/polybot && uv run python scripts/resolve_orphan_trade.py
"""
import os
import sys
import asyncio
import asyncpg
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

load_dotenv()

TRADE_ID = 942
CLOB_ORDER_ID = "0x40233f8a95c106e8503171e0a358ea0aef6ef720420197dfa445c0c8a08908f7"


async def main():
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not pk:
        print("FAIL: POLYMARKET_PRIVATE_KEY not set")
        sys.exit(1)

    client = ClobClient(
        host="https://clob.polymarket.com", chain_id=137, key=pk,
        creds=ApiCreds(
            api_key=os.environ.get("POLYMARKET_API_KEY", ""),
            api_secret=os.environ.get("POLYMARKET_API_SECRET", ""),
            api_passphrase=os.environ.get("POLYMARKET_API_PASSPHRASE", "")))

    # 1. Check CLOB order status
    print(f"Checking CLOB order status for trade #{TRADE_ID}...")
    try:
        result = client.get_order(CLOB_ORDER_ID)
        status_raw = result.get("status", "UNKNOWN").upper()
        size_matched = float(result.get("size_matched", 0))
        print(f"  CLOB status: {status_raw}, size_matched: {size_matched}")
    except Exception as e:
        print(f"  CLOB API error: {e}")
        print("  Treating as CANCELLED (order may have expired)")
        status_raw = "CANCELLED"

    # 2. Connect to DB
    db_url = os.environ.get("DATABASE_URL")
    conn = await asyncpg.connect(db_url)

    trade = await conn.fetchrow("SELECT * FROM trades WHERE id = $1", TRADE_ID)
    if not trade:
        print(f"Trade #{TRADE_ID} not found in DB")
        await conn.close()
        sys.exit(1)

    position_size = float(trade["position_size_usd"])
    print(f"  DB status: {trade['status']}, size: ${position_size:.2f}")

    if trade["status"] not in ("open",):
        print(f"  Trade is already {trade['status']} — nothing to do")
        await conn.close()
        return

    # 3. Resolve based on CLOB status
    if status_raw == "MATCHED":
        await conn.execute(
            "UPDATE trades SET status = 'filled' WHERE id = $1", TRADE_ID)
        print(f"  -> Marked as FILLED. Position manager will manage TP/SL.")
    else:
        # LIVE, CANCELLED, or unknown — cancel and free capital
        if status_raw == "LIVE":
            print(f"  Order still live after 3 days — cancelling...")
            try:
                client.cancel(CLOB_ORDER_ID)
                print(f"  -> CLOB order cancelled")
            except Exception as e:
                print(f"  -> Cancel failed (may already be dead): {e}")

        await conn.execute(
            "UPDATE trades SET status = 'cancelled' WHERE id = $1", TRADE_ID)
        await conn.execute(
            "UPDATE system_state SET total_deployed = GREATEST(0, total_deployed - $1) WHERE id = 1",
            position_size)
        print(f"  -> Marked as CANCELLED. Freed ${position_size:.2f} deployed capital.")

    # 4. Verify
    updated = await conn.fetchrow("SELECT status FROM trades WHERE id = $1", TRADE_ID)
    state = await conn.fetchrow("SELECT total_deployed FROM system_state WHERE id = 1")
    print(f"\n  Verification: trade status={updated['status']}, "
          f"total_deployed=${float(state['total_deployed']):.2f}")

    await conn.close()
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
