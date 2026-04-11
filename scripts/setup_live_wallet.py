#!/usr/bin/env python3
"""One-time setup: swap native USDC → USDC.e, approve, and deposit into Polymarket.

Usage:
    cd ~/polybot && uv run python scripts/setup_live_wallet.py
"""
import os
import sys
import time
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account

load_dotenv()

PRIVATE_KEY = os.environ["POLYMARKET_PRIVATE_KEY"]
RPC_URL = "https://polygon-bor-rpc.publicnode.com"

# Token addresses on Polygon
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Polymarket exchange contracts (from py-clob-client config)
EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

# Uniswap V3 SwapRouter02 on Polygon
SWAP_ROUTER = "0x68b3465833fb72B5A828cCEEcb26E19D7B77850D"

# ERC20 ABI (approve + balanceOf)
ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}],
     "type": "function"},
    {"constant": False, "inputs": [{"name": "_spender", "type": "address"},
     {"name": "_value", "type": "uint256"}], "name": "approve",
     "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"},
     {"name": "_spender", "type": "address"}], "name": "allowance",
     "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]

# Uniswap V3 SwapRouter02 exactInputSingle ABI
SWAP_ROUTER_ABI = [
    {"inputs": [{"components": [
        {"name": "tokenIn", "type": "address"},
        {"name": "tokenOut", "type": "address"},
        {"name": "fee", "type": "uint24"},
        {"name": "recipient", "type": "address"},
        {"name": "amountIn", "type": "uint256"},
        {"name": "amountOutMinimum", "type": "uint256"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"}
    ], "name": "params", "type": "tuple"}],
     "name": "exactInputSingle", "outputs": [{"name": "amountOut", "type": "uint256"}],
     "stateMutability": "payable", "type": "function"},
]

w3 = Web3(Web3.HTTPProvider(RPC_URL))
from web3.middleware import ExtraDataToPOAMiddleware
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
account = Account.from_key(PRIVATE_KEY)
ADDRESS = account.address


def send_tx(tx):
    """Sign, send, and wait for a transaction."""
    tx["nonce"] = w3.eth.get_transaction_count(ADDRESS)
    tx["from"] = ADDRESS
    if "gas" not in tx:
        tx["gas"] = w3.eth.estimate_gas(tx)
    # Remove legacy gasPrice if present (Polygon uses EIP-1559)
    tx.pop("gasPrice", None)
    if "maxFeePerGas" not in tx:
        base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
        tx["maxPriorityFeePerGas"] = w3.to_wei(30, "gwei")
        tx["maxFeePerGas"] = base_fee * 2 + tx["maxPriorityFeePerGas"]
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  Tx sent: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
        print(f"  FAILED! Receipt: {receipt}")
        sys.exit(1)
    print(f"  Confirmed in block {receipt['blockNumber']}, gas used: {receipt['gasUsed']}")
    return receipt


def main():
    usdc_native = w3.eth.contract(address=w3.to_checksum_address(USDC_NATIVE), abi=ERC20_ABI)
    usdc_e = w3.eth.contract(address=w3.to_checksum_address(USDC_E), abi=ERC20_ABI)

    native_bal = usdc_native.functions.balanceOf(ADDRESS).call()
    print(f"Native USDC balance: ${native_bal / 1e6:.2f}")
    print(f"POL balance: {w3.eth.get_balance(ADDRESS) / 1e18:.4f}")
    print()

    if native_bal == 0:
        print("No native USDC to swap. Checking USDC.e...")
        bridged_bal = usdc_e.functions.balanceOf(ADDRESS).call()
        if bridged_bal > 0:
            print(f"USDC.e balance: ${bridged_bal / 1e6:.2f} — skipping swap.")
            native_bal = 0
        else:
            print("No USDC of any kind. Nothing to do.")
            return

    # --- Step 1: Approve Uniswap router to spend native USDC ---
    base_tx = {"chainId": 137, "from": ADDRESS}

    if native_bal > 0:
        print("Step 1: Approve Uniswap router for native USDC...")
        approve_tx = usdc_native.functions.approve(
            w3.to_checksum_address(SWAP_ROUTER), native_bal
        ).build_transaction(base_tx)
        send_tx(approve_tx)
        print()

        # --- Step 2: Swap native USDC → USDC.e via Uniswap V3 ---
        print(f"Step 2: Swap ${native_bal / 1e6:.2f} native USDC → USDC.e...")
        router = w3.eth.contract(address=w3.to_checksum_address(SWAP_ROUTER), abi=SWAP_ROUTER_ABI)

        # 0.5% slippage tolerance
        min_out = int(native_bal * 0.995)

        swap_tx = router.functions.exactInputSingle((
            w3.to_checksum_address(USDC_NATIVE),   # tokenIn
            w3.to_checksum_address(USDC_E),         # tokenOut
            500,                                     # fee tier (0.05% for stablecoins)
            ADDRESS,                                 # recipient
            native_bal,                              # amountIn
            min_out,                                 # amountOutMinimum
            0,                                       # sqrtPriceLimitX96 (0 = no limit)
        )).build_transaction({**base_tx, "value": 0})
        send_tx(swap_tx)
        print()

    # Check USDC.e balance after swap
    bridged_bal = usdc_e.functions.balanceOf(ADDRESS).call()
    print(f"USDC.e balance after swap: ${bridged_bal / 1e6:.2f}")
    print()

    if bridged_bal == 0:
        print("ERROR: No USDC.e after swap!")
        return

    # --- Step 3: Approve Polymarket exchange contracts ---
    MAX_UINT256 = 2**256 - 1
    contracts_to_approve = [
        ("Exchange", EXCHANGE),
        ("NegRiskExchange", NEG_RISK_EXCHANGE),
        ("NegRiskAdapter", NEG_RISK_ADAPTER),
    ]

    for name, addr in contracts_to_approve:
        current_allowance = usdc_e.functions.allowance(ADDRESS, w3.to_checksum_address(addr)).call()
        if current_allowance >= bridged_bal:
            print(f"Step 3: {name} already approved.")
            continue
        print(f"Step 3: Approve {name} ({addr[:10]}...) for USDC.e...")
        approve_tx = usdc_e.functions.approve(
            w3.to_checksum_address(addr), MAX_UINT256
        ).build_transaction(base_tx)
        send_tx(approve_tx)
    print()

    # --- Step 4: Verify via CLOB API ---
    print("Step 4: Checking CLOB balance...")
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=PRIVATE_KEY,
        creds=ApiCreds(
            api_key=os.environ["POLYMARKET_API_KEY"],
            api_secret=os.environ["POLYMARKET_API_SECRET"],
            api_passphrase=os.environ["POLYMARKET_API_PASSPHRASE"],
        ),
    )

    # Trigger balance refresh
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    client.update_balance_allowance(params)
    time.sleep(2)

    result = client.get_balance_allowance(params)
    print(f"CLOB balance: {result}")
    print()
    print("Done! Restart polybot to begin live trading.")


if __name__ == "__main__":
    main()
