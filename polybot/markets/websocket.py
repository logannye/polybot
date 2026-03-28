import asyncio
import aiohttp
import structlog
from typing import Callable, Awaitable

log = structlog.get_logger()

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def should_early_exit(
    entry_price: float,
    current_price: float,
    side: str,
    ensemble_prob: float,
    early_exit_edge: float = 0.02,
) -> bool:
    if side == "YES":
        remaining_edge = ensemble_prob - current_price
    else:
        remaining_edge = (1 - ensemble_prob) - (1 - current_price)

    return round(remaining_edge, 10) <= early_exit_edge


def should_stop_loss(
    entry_price: float,
    current_price: float,
    side: str,
    stop_threshold: float = 0.15,
) -> bool:
    if side == "YES":
        loss_pct = (entry_price - current_price) / entry_price
    else:
        loss_pct = (current_price - entry_price) / (1 - entry_price)

    return loss_pct > stop_threshold


class PositionTracker:
    def __init__(
        self,
        on_early_exit: Callable[[int, float], Awaitable[None]],
        on_stop_loss: Callable[[int, float], Awaitable[None]],
    ):
        self._on_early_exit = on_early_exit
        self._on_stop_loss = on_stop_loss
        self._tracked: dict[str, dict] = {}  # token_id → position info
        self._running = False

    def track(self, token_id: str, trade_id: int, side: str,
              entry_price: float, ensemble_prob: float) -> None:
        self._tracked[token_id] = {
            "trade_id": trade_id,
            "side": side,
            "entry_price": entry_price,
            "ensemble_prob": ensemble_prob,
        }
        log.info("position_tracked", token_id=token_id, trade_id=trade_id)

    def untrack(self, token_id: str) -> None:
        self._tracked.pop(token_id, None)

    async def run(self, session: aiohttp.ClientSession) -> None:
        self._running = True
        while self._running:
            if not self._tracked:
                await asyncio.sleep(5)
                continue

            try:
                async with session.ws_connect(WS_URL) as ws:
                    for token_id in self._tracked:
                        await ws.send_json({
                            "type": "subscribe",
                            "channel": "price",
                            "token_id": token_id,
                        })

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_message(msg.json())
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break

            except Exception as e:
                log.error("ws_error", error=str(e))
                await asyncio.sleep(5)

    async def _handle_message(self, data: dict) -> None:
        token_id = data.get("token_id")
        price = data.get("price")

        if not token_id or not price or token_id not in self._tracked:
            return

        pos = self._tracked[token_id]
        current_price = float(price)

        if should_early_exit(
            pos["entry_price"], current_price, pos["side"], pos["ensemble_prob"]
        ):
            await self._on_early_exit(pos["trade_id"], current_price)
            self.untrack(token_id)

        elif should_stop_loss(pos["entry_price"], current_price, pos["side"]):
            await self._on_stop_loss(pos["trade_id"], current_price)
            self.untrack(token_id)

    def stop(self) -> None:
        self._running = False
