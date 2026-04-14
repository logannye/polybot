#!/usr/bin/env python3
"""Pre-live validation of all CLOB API interactions.

Runs automatically before live trading starts. Tests every API
endpoint that caused bugs during our first live deployment.

Usage:
    cd ~/polybot && uv run python scripts/live_preflight.py
"""
import os
import sys
import re
from uuid import uuid4
from dotenv import load_dotenv

load_dotenv()

CHECKS_PASSED = 0
CHECKS_FAILED = 0


def check(name: str, passed: bool, detail: str = ""):
    global CHECKS_PASSED, CHECKS_FAILED
    if passed:
        CHECKS_PASSED += 1
        print(f"  OK  {name}")
    else:
        CHECKS_FAILED += 1
        print(f"  FAIL  {name}: {detail}")


def main():
    global CHECKS_PASSED, CHECKS_FAILED
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

    print("=== Polybot Live Preflight ===\n")

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

    # 1. Balance check
    print("1. Balance API")
    try:
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        result = client.get_balance_allowance(params)
        balance = int(result.get("balance", 0)) / 1e6
        check("get_balance_allowance returns dollars", 0 <= balance < 1_000_000,
              f"got {balance}")
        check("balance > $0", balance > 0, f"balance is ${balance:.2f}")
    except Exception as e:
        check("get_balance_allowance", False, str(e)[:100])

    # 2. Heartbeat chain
    print("\n2. Heartbeat API")
    try:
        hb_id = str(uuid4())
        try:
            result = client.post_heartbeat(hb_id)
            hb_id = result.get("heartbeat_id", hb_id) if isinstance(result, dict) else str(result)
        except Exception as e:
            match = re.search(r"'heartbeat_id': '([^']+)'", str(e))
            if match:
                hb_id = match.group(1)
                result = client.post_heartbeat(hb_id)
                hb_id = result.get("heartbeat_id", hb_id) if isinstance(result, dict) else str(result)
            else:
                raise
        check("heartbeat #1 (with resync)", True)

        for i in [2, 3]:
            result = client.post_heartbeat(hb_id)
            hb_id = result.get("heartbeat_id", hb_id) if isinstance(result, dict) else str(result)
            check(f"heartbeat #{i}", True)
    except Exception as e:
        check("heartbeat chain", False, str(e)[:100])

    # 3. Order book access
    print("\n3. Order Book API")
    try:
        ok = client.get_ok()
        check("CLOB API reachable", ok is not None)
    except Exception as e:
        check("CLOB API reachable", False, str(e)[:100])

    # 4. Conditional token approval
    print("\n4. Token Approvals")
    try:
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware
        from eth_account import Account

        w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        address = Account.from_key(pk).address

        CT = w3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
        ct_abi = [{"constant": True, "inputs": [
            {"name": "account", "type": "address"},
            {"name": "operator", "type": "address"}],
            "name": "isApprovedForAll", "outputs": [{"name": "", "type": "bool"}],
            "type": "function"}]
        ct = w3.eth.contract(address=CT, abi=ct_abi)

        for name, addr in [
            ("Exchange", "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"),
            ("NegRisk", "0xC5d563A36AE78145C45a50134d48A1215220f80a"),
            ("NegRiskAdapter", "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
        ]:
            approved = ct.functions.isApprovedForAll(
                address, w3.to_checksum_address(addr)).call()
            check(f"CT approval: {name}", approved, "not approved")
    except Exception as e:
        check("token approvals", False, str(e)[:100])

    # 5. Deployment stage check
    print("\n5. Deployment Stage")
    from polybot.core.config import Settings
    settings = Settings()
    stage = getattr(settings, "live_deployment_stage", "dry_run")
    check(f"deployment stage is '{stage}'",
          stage in ("micro_test", "full"),
          f"stage is '{stage}' — must be 'micro_test' or 'full' for live trading")

    # Summary
    print(f"\n=== Results: {CHECKS_PASSED} passed, {CHECKS_FAILED} failed ===")
    if CHECKS_FAILED > 0:
        print("\nFAIL: Fix the above issues before enabling live trading.")
        sys.exit(1)
    else:
        print("\nPASS: All preflight checks passed. Safe to trade live.")
        sys.exit(0)


if __name__ == "__main__":
    main()
