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


def format_daily_report(
    date: str, starting_bankroll: float, ending_bankroll: float,
    strategy_breakdown: list[dict], total_trades_cumulative: int,
    total_pnl_cumulative: float, days_running: int,
    model_performance: list[dict], open_positions: list[dict],
    api_errors: int, strategies_status: str,
) -> str:
    day_pnl = ending_bankroll - starting_bankroll
    pnl_pct = (day_pnl / starting_bankroll * 100) if starting_bankroll > 0 else 0

    def format_pnl(value: float) -> str:
        if value >= 0:
            return f"+${value:.2f}"
        else:
            return f"-${abs(value):.2f}"

    lines = [
        f"POLYBOT DAILY REPORT — {date}",
        "",
        "BANKROLL",
        f"  Starting:     ${starting_bankroll:.2f}",
        f"  Ending:       ${ending_bankroll:.2f}",
        f"  Day P&L:      {format_pnl(day_pnl)}  ({pnl_pct:+.1f}%)",
        "",
        "STRATEGY BREAKDOWN",
    ]
    for s in strategy_breakdown:
        w, l = s.get("wins", 0), s.get("losses", 0)
        pnl = s.get("pnl", 0.0)
        lines.append(f"  {s['strategy']:12s}  {s['trades']} trades, "
                     f"{format_pnl(pnl)}  ({w}W / {l}L)")

    lines += [
        "",
        "CUMULATIVE (since launch)",
        f"  Total trades: {total_trades_cumulative}",
        f"  Total P&L:    {format_pnl(total_pnl_cumulative)}",
        f"  Days running: {days_running}",
        "",
        "MODEL PERFORMANCE",
    ]
    for m in model_performance:
        lines.append(f"  {m['model']:20s}  Brier {m['brier']:.3f}, trust {m['trust']:.2f}")

    lines += ["", f"OPEN POSITIONS ({len(open_positions)})"]
    for p in open_positions:
        lines.append(f"  - \"{p.get('question', '?')}\" — {p.get('side', '?')} "
                     f"@ ${p.get('price', 0):.2f}, ${p.get('size', 0):.2f} deployed")

    lines += [
        "",
        "SYSTEM HEALTH",
        f"  API errors:   {api_errors}",
        f"  Strategies:   {strategies_status}",
    ]
    return "\n".join(lines)


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
