import resend
import structlog

log = structlog.get_logger()


def format_trade_email(
    event: str, market: str, side: str, size: float, price: float, edge: float,
    pnl: float | None = None, outcome: str | None = None,
) -> str:
    if event == "executed":
        return f"""<h2>Trade Executed</h2>
<p><b>Market:</b> {market}</p>
<p><b>Side:</b> {side} @ ${price:.4f}</p>
<p><b>Size:</b> ${size:.2f}</p>
<p><b>Edge:</b> {edge:.1%}</p>"""
    elif event == "resolved":
        pnl_str = f"${pnl:+.2f}" if pnl is not None else "N/A"
        return f"""<h2>Trade Resolved</h2>
<p><b>Market:</b> {market}</p>
<p><b>Outcome:</b> {outcome}</p>
<p><b>Side:</b> {side} @ ${price:.4f}</p>
<p><b>P&L:</b> {pnl_str}</p>"""
    return f"<p>Trade event: {event}</p>"


def format_daily_summary(
    bankroll: float, daily_pnl: float, trades_count: int, win_count: int,
) -> str:
    win_rate = (win_count / trades_count * 100) if trades_count > 0 else 0
    return f"""<h2>Daily Summary</h2>
<p><b>Bankroll:</b> ${bankroll:.2f}</p>
<p><b>Day P&L:</b> ${daily_pnl:+.2f}</p>
<p><b>Trades:</b> {trades_count} ({win_rate:.0f}% win rate)</p>"""


class EmailNotifier:
    def __init__(self, api_key: str, to_email: str):
        resend.api_key = api_key
        self._to = to_email

    async def send(self, subject: str, html: str) -> None:
        try:
            resend.Emails.send({
                "from": "Polybot <alerts@polybot.dev>",
                "to": [self._to],
                "subject": f"[Polybot] {subject}",
                "html": html,
            })
            log.info("email_sent", subject=subject)
        except Exception as e:
            log.error("email_failed", subject=subject, error=str(e))
