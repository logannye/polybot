import structlog
from twilio.rest import Client

log = structlog.get_logger()


def format_urgent_sms(event: str, pnl: float | None = None) -> str:
    if event == "circuit_breaker":
        return f"POLYBOT CIRCUIT BREAKER: Daily loss ${pnl:.2f}. Trading paused 12h."
    elif event == "system_down":
        return "POLYBOT SYSTEM DOWN: No heartbeat for 10+ min. Check VPS."
    elif event == "low_balance":
        return "POLYBOT LOW BALANCE: Wallet below $10 USDC."
    elif event == "big_trade":
        sign = "+" if pnl and pnl > 0 else ""
        return f"POLYBOT BIG TRADE: {sign}${pnl:.2f} on single position."
    return f"POLYBOT ALERT: {event}"


class SmsNotifier:
    def __init__(self, account_sid: str, auth_token: str, from_number: str, to_number: str):
        self._client = Client(account_sid, auth_token)
        self._from = from_number
        self._to = to_number

    async def send(self, message: str) -> None:
        try:
            self._client.messages.create(
                body=message,
                from_=self._from,
                to=self._to,
            )
            log.info("sms_sent", message=message[:50])
        except Exception as e:
            log.error("sms_failed", error=str(e))
