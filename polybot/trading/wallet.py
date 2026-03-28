import structlog
from web3 import AsyncWeb3
from eth_account import Account

log = structlog.get_logger()


class WalletManager:
    def __init__(self, private_key: str, rpc_url: str = "https://polygon-rpc.com"):
        self._account = Account.from_key(private_key)
        self._w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
        self.address = self._account.address

    async def get_usdc_balance(self) -> float:
        usdc_address = AsyncWeb3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
        abi = [{"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"}]
        contract = self._w3.eth.contract(address=usdc_address, abi=abi)
        balance_raw = await contract.functions.balanceOf(self.address).call()
        return balance_raw / 1e6

    def compute_shares(self, usd_amount: float, price: float) -> float:
        if price <= 0:
            return 0.0
        return usd_amount / price

    def sign_order(self, order_data: dict) -> dict:
        return {"signature": "0x...", "order": order_data}
