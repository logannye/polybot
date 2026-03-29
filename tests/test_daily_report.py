from polybot.notifications.email import format_daily_report


def test_daily_report_contains_bankroll():
    report = format_daily_report(
        date="2026-03-28",
        starting_bankroll=100.0, ending_bankroll=104.50,
        strategy_breakdown=[
            {"strategy": "arbitrage", "trades": 2, "pnl": 3.0, "wins": 2, "losses": 0},
            {"strategy": "snipe", "trades": 1, "pnl": 1.5, "wins": 1, "losses": 0},
            {"strategy": "forecast", "trades": 0, "pnl": 0.0, "wins": 0, "losses": 0},
        ],
        total_trades_cumulative=10, total_pnl_cumulative=4.50, days_running=2,
        model_performance=[
            {"model": "claude-sonnet-4.6", "brier": 0.18, "trust": 0.41},
        ],
        open_positions=[], api_errors=0, strategies_status="all active")
    assert "$100.00" in report
    assert "$104.50" in report
    assert "+$4.50" in report
    assert "arbitrage" in report.lower()
    assert "all active" in report.lower()


def test_daily_report_negative_pnl():
    report = format_daily_report(
        date="2026-03-28",
        starting_bankroll=100.0, ending_bankroll=95.0,
        strategy_breakdown=[], total_trades_cumulative=5,
        total_pnl_cumulative=-5.0, days_running=1,
        model_performance=[], open_positions=[],
        api_errors=3, strategies_status="forecast disabled")
    assert "-$5.00" in report
    assert "forecast disabled" in report


def test_daily_report_with_open_positions():
    report = format_daily_report(
        date="2026-03-28",
        starting_bankroll=100.0, ending_bankroll=102.0,
        strategy_breakdown=[],
        total_trades_cumulative=5, total_pnl_cumulative=2.0, days_running=1,
        model_performance=[],
        open_positions=[
            {"question": "Will X happen?", "side": "YES", "price": 0.62, "size": 5.20},
        ],
        api_errors=0, strategies_status="all active")
    assert "Will X happen?" in report
    assert "YES" in report
