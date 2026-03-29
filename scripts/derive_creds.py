#!/usr/bin/env python3
"""Derive Polymarket CLOB API credentials from your private key.

Usage:
    uv run python scripts/derive_creds.py

Reads POLYMARKET_PRIVATE_KEY from .env and prints credentials to add to .env.
"""
from dotenv import load_dotenv
import os
import sys

load_dotenv()

private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
if not private_key:
    print("ERROR: POLYMARKET_PRIVATE_KEY not found in .env")
    sys.exit(1)

from py_clob_client.client import ClobClient

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=private_key,
)

print("Deriving CLOB API credentials...")
creds = client.create_or_derive_api_creds()
if not creds:
    print("ERROR: Failed to derive credentials")
    sys.exit(1)

print(f"\nSuccess! Add these to your .env file:\n")
print(f"POLYMARKET_API_KEY={creds.api_key}")
print(f"POLYMARKET_API_SECRET={creds.api_secret}")
print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
print(f"\nThen set DRY_RUN=false when you're ready for live trading.")
