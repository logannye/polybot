import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from polybot.notifications.email import EmailNotifier, format_trade_email, format_daily_summary
from polybot.notifications.sms import SmsNotifier, format_urgent_sms


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


class TestFormatSms:
    def test_circuit_breaker(self):
        msg = format_urgent_sms("circuit_breaker", pnl=-60.0)
        assert "CIRCUIT BREAKER" in msg.upper() or "circuit breaker" in msg.lower()

    def test_system_down(self):
        msg = format_urgent_sms("system_down")
        assert "down" in msg.lower()


class TestEmailNotifier:
    @pytest.mark.asyncio
    async def test_send_calls_resend(self):
        with patch("polybot.notifications.email.resend") as mock_resend:
            mock_resend.Emails.send = MagicMock(return_value={"id": "123"})
            notifier = EmailNotifier(api_key="test", to_email="test@test.com")
            await notifier.send("Test Subject", "<p>Hello</p>")
            mock_resend.Emails.send.assert_called_once()


class TestSmsNotifier:
    @pytest.mark.asyncio
    async def test_send_calls_twilio(self):
        with patch("polybot.notifications.sms.Client") as MockClient:
            mock_client = MagicMock()
            mock_client.messages.create = MagicMock(return_value=MagicMock(sid="SM123"))
            MockClient.return_value = mock_client
            notifier = SmsNotifier(
                account_sid="test", auth_token="test",
                from_number="+10000000000", to_number="+10000000001",
            )
            await notifier.send("Test alert")
            mock_client.messages.create.assert_called_once()
