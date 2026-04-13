import asyncio
import structlog
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, AssetType, BalanceAllowanceParams, OrderArgs, OrderType

log = structlog.get_logger()


class ClobGateway:
    def __init__(self, host: str, chain_id: int, private_key: str,
                 api_key: str, api_secret: str, api_passphrase: str):
        creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
        self._client = ClobClient(host=host, chain_id=chain_id, key=private_key, creds=creds)
        log.info("clob_gateway_initialized", address=self._client.get_address())

    async def submit_order(self, token_id: str, side: str, price: float,
                           size: float, order_type: str = "GTC",
                           post_only: bool = False) -> str:
        # Strategies pass "YES"/"NO" but the CLOB API expects "BUY"/"SELL".
        # Buying YES or NO tokens is always a "BUY" — the token_id determines
        # which outcome token. "SELL" is only for exiting existing positions.
        clob_side = "BUY" if side in ("YES", "NO", "BUY") else side
        order_args = OrderArgs(token_id=token_id, price=price, size=size, side=clob_side)
        ot = getattr(OrderType, order_type, OrderType.GTC)
        def _create_and_post():
            signed_order = self._client.create_order(order_args)
            return self._client.post_order(signed_order, orderType=ot, post_only=post_only)
        result = await asyncio.to_thread(_create_and_post)
        order_id = result.get("orderID") or result.get("id", "")
        log.info("clob_order_submitted", order_id=order_id, token_id=token_id,
                 side=side, price=price, size=size, post_only=post_only)
        return order_id

    async def cancel_order(self, clob_order_id: str) -> bool:
        try:
            result = await asyncio.to_thread(self._client.cancel, clob_order_id)
            log.info("clob_order_cancelled", order_id=clob_order_id)
            return bool(result.get("canceled", False))
        except Exception as e:
            log.error("clob_cancel_failed", order_id=clob_order_id, error=str(e))
            return False

    async def get_order_status(self, clob_order_id: str) -> dict:
        result = await asyncio.to_thread(self._client.get_order, clob_order_id)
        status_raw = result.get("status", "").upper()
        status_map = {"LIVE": "live", "MATCHED": "matched", "CANCELLED": "cancelled", "CANCELED": "cancelled"}
        return {"status": status_map.get(status_raw, status_raw.lower()), "size_matched": float(result.get("size_matched", 0))}

    async def sell_shares(self, token_id: str, price: float, size: float,
                          post_only: bool = False) -> str:
        """Place a sell order for shares we already own."""
        order_args = OrderArgs(token_id=token_id, price=price, size=size, side="SELL")
        def _create_and_post():
            signed_order = self._client.create_order(order_args)
            return self._client.post_order(signed_order, orderType=OrderType.GTC, post_only=post_only)
        result = await asyncio.to_thread(_create_and_post)
        order_id = result.get("orderID") or result.get("id", "")
        log.info("clob_sell_submitted", order_id=order_id, token_id=token_id,
                 price=price, size=size, post_only=post_only)
        return order_id

    async def get_balance(self) -> float:
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        result = await asyncio.to_thread(self._client.get_balance_allowance, params)
        return float(result.get("balance", 0)) / 1e6

    async def get_market_price(self, token_id: str) -> float | None:
        """Fetch real-time buy price from the CLOB order book (best ask).

        Uses the actual order book, not the theoretical midpoint price.
        Returns the cheapest price someone is willing to sell at, which
        is the price needed to guarantee an instant taker fill.
        """
        try:
            book = await asyncio.to_thread(self._client.get_order_book, token_id)
            if book.asks:
                best_ask = float(book.asks[0].price)
                spread = best_ask - float(book.bids[0].price) if book.bids else 1.0
                log.debug("clob_book_price", token_id=token_id[:20],
                          best_ask=best_ask, spread=round(spread, 4))
                return best_ask
            return None
        except Exception as e:
            log.warning("clob_get_price_failed", token_id=token_id[:20], error=str(e))
            return None

    async def get_book_spread(self, token_id: str) -> float | None:
        """Get the bid-ask spread for a token. Returns None on error."""
        try:
            book = await asyncio.to_thread(self._client.get_order_book, token_id)
            if book.asks and book.bids:
                return float(book.asks[0].price) - float(book.bids[0].price)
            return None
        except Exception:
            return None

    # --- Market-making support methods ---

    async def send_heartbeat(self, heartbeat_id: str) -> str:
        """Send heartbeat to keep orders alive. MUST be called every <10s."""
        result = await asyncio.to_thread(self._client.post_heartbeat, heartbeat_id)
        new_id = result if isinstance(result, str) else str(result)
        return new_id

    async def cancel_all_orders(self) -> bool:
        """Emergency: cancel all resting orders."""
        try:
            await asyncio.to_thread(self._client.cancel_all)
            log.info("clob_all_orders_cancelled")
            return True
        except Exception as e:
            log.error("clob_cancel_all_failed", error=str(e))
            return False

    async def cancel_orders_batch(self, order_ids: list[str]) -> bool:
        """Cancel multiple orders in one call."""
        try:
            await asyncio.to_thread(self._client.cancel_orders, order_ids)
            log.info("clob_batch_cancelled", count=len(order_ids))
            return True
        except Exception as e:
            log.error("clob_batch_cancel_failed", error=str(e))
            return False

    async def submit_batch_orders(self, orders: list[dict],
                                  post_only: bool = True) -> list[str]:
        """Batch submit up to 15 post-only orders.

        Each dict: {token_id, side, price, size}.
        """
        from py_clob_client.clob_types import PostOrdersArgs

        def _build_and_post():
            args = []
            for o in orders:
                order_args = OrderArgs(
                    token_id=o["token_id"], price=o["price"],
                    size=o["size"], side=o["side"])
                signed = self._client.create_order(order_args)
                args.append(PostOrdersArgs(
                    order=signed, orderType=OrderType.GTC, postOnly=post_only))
            return self._client.post_orders(args)

        result = await asyncio.to_thread(_build_and_post)
        order_ids = []
        if isinstance(result, list):
            for r in result:
                order_ids.append(r.get("orderID") or r.get("id", ""))
        log.info("clob_batch_submitted", count=len(orders), post_only=post_only)
        return order_ids
