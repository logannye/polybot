import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from polybot.notifications.email import EmailNotifier, format_trade_email, format_daily_summary


class TestFormatTradeEmail:
    def test_formats_trade(self):
        html = format_trade_email(
            event="executed",
            market="Will BTC hit 100K?",
            side="YES",
            size=15.0,
            price=0.50,
            edge=0.10,
        )
        assert "BTC" in html
        assert "YES" in html
        assert "15.0" in html

    def test_formats_resolution(self):
        html = format_trade_email(
            event="resolved",
            market="Will BTC hit 100K?",
            side="YES",
            size=15.0,
            price=0.50,
            edge=0.10,
            pnl=7.50,
            outcome="YES",
        )
        assert "7.50" in html or "7.5" in html


class TestFormatDailySummary:
    def test_formats_summary(self):
        html = format_daily_summary(
            bankroll=315.0,
            daily_pnl=15.0,
            trades_count=5,
            win_count=3,
        )
        assert "315" in html
        assert "15" in html


class TestEmailNotifier:
    @pytest.mark.asyncio
    async def test_send_calls_resend(self):
        with patch("polybot.notifications.email.resend") as mock_resend:
            mock_resend.Emails.send = MagicMock(return_value={"id": "123"})
            notifier = EmailNotifier(api_key="test", to_email="test@test.com")
            await notifier.send("Test Subject", "<p>Hello</p>")
            mock_resend.Emails.send.assert_called_once()
