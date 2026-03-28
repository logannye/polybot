import pytest
from polybot.core.config import Settings


@pytest.fixture
def settings():
    return Settings(
        polymarket_api_key="test",
        polymarket_private_key="0x" + "ab" * 32,
        anthropic_api_key="test",
        openai_api_key="test",
        google_api_key="test",
        brave_api_key="test",
        database_url="postgresql://localhost/polybot_test",
        resend_api_key="test",
        twilio_account_sid="test",
        twilio_auth_token="test",
        twilio_from_number="+10000000000",
        alert_email="test@test.com",
        alert_phone="+10000000000",
    )
