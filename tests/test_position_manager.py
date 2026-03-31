import pytest
from unittest.mock import AsyncMock, MagicMock
from polybot.trading.position_manager import (
    compute_unrealized_return,
    should_take_profit,
    should_cut_loss,
    ActivePositionManager,
)


# --- Pure function tests ---


class TestComputeUnrealizedReturn:
    def test_yes_profit(self):
        assert compute_unrealized_return("YES", 0.50, 0.60) == pytest.approx(0.20)

    def test_yes_loss(self):
        assert compute_unrealized_return("YES", 0.50, 0.40) == pytest.approx(-0.20)

    def test_no_profit(self):
        # Bought NO at 0.40 (yes was 0.60). Now yes=0.40 → NO price=0.60
        # return = (0.60 - 0.40) / 0.40 = 0.50
        assert compute_unrealized_return("NO", 0.40, 0.40) == pytest.approx(0.50)

    def test_no_loss(self):
        # Bought NO at 0.40 (yes was 0.60). Now yes=0.80 → NO price=0.20
        # return = (0.20 - 0.40) / 0.40 = -0.50
        assert compute_unrealized_return("NO", 0.40, 0.80) == pytest.approx(-0.50)

    def test_zero_entry_returns_zero(self):
        assert compute_unrealized_return("YES", 0.0, 0.50) == 0.0

    def test_no_change(self):
        assert compute_unrealized_return("YES", 0.50, 0.50) == pytest.approx(0.0)

    def test_no_no_change(self):
        # NO at 0.30 (yes=0.70). Now yes=0.70 → NO=0.30. No change.
        assert compute_unrealized_return("NO", 0.30, 0.70) == pytest.approx(0.0)


class TestShouldTakeProfit:
    def test_takes_profit_above_threshold(self):
        # 0.50 → 0.61 = 22% gain, above 20% threshold
        assert should_take_profit("YES", 0.50, 0.61, threshold=0.20) is True

    def test_no_profit_below_threshold(self):
        assert should_take_profit("YES", 0.50, 0.55, threshold=0.20) is False

    def test_takes_profit_no_side(self):
        # NO at 0.30, yes drops from 0.70 to 0.50 → NO now 0.50
        # return = (0.50 - 0.30) / 0.30 = 0.667
        assert should_take_profit("NO", 0.30, 0.50, threshold=0.20) is True


class TestShouldCutLoss:
    def test_cuts_loss_at_threshold(self):
        assert should_cut_loss("YES", 0.50, 0.375, threshold=0.25) is True

    def test_no_cut_within_threshold(self):
        assert should_cut_loss("YES", 0.50, 0.45, threshold=0.25) is False

    def test_cuts_loss_no_side(self):
        # NO at 0.40, yes rises from 0.60 to 0.85 → NO now 0.15
        # return = (0.15 - 0.40) / 0.40 = -0.625
        assert should_cut_loss("NO", 0.40, 0.85, threshold=0.25) is True


# --- Integration tests ---


@pytest.mark.asyncio
async def test_check_positions_take_profit():
    """Position with 25% gain should trigger take_profit exit."""
    db = AsyncMock()
    db.fetch = AsyncMock(return_value=[{
        "id": 1, "side": "YES", "entry_price": 0.50, "shares": 20.0,
        "position_size_usd": 10.0, "strategy": "forecast", "status": "dry_run",
        "polymarket_id": "mkt-1", "question": "Test market?",
        "ensemble_probability": 0.65,
    }])

    executor = AsyncMock()
    executor.exit_position = AsyncMock(return_value=2.50)

    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-1": {"yes_price": 0.625, "no_price": 0.375},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.20
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02

    email = AsyncMock()

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=email, settings=settings)
    await mgr.check_positions()

    executor.exit_position.assert_called_once_with(
        trade_id=1, exit_price=0.625, exit_reason="take_profit")
    email.send.assert_called_once()


@pytest.mark.asyncio
async def test_check_positions_stop_loss():
    """Position with 30% loss should trigger stop_loss exit."""
    db = AsyncMock()
    db.fetch = AsyncMock(return_value=[{
        "id": 2, "side": "YES", "entry_price": 0.50, "shares": 20.0,
        "position_size_usd": 10.0, "strategy": "forecast", "status": "dry_run",
        "polymarket_id": "mkt-2", "question": "Losing market?",
        "ensemble_probability": 0.65,
    }])

    executor = AsyncMock()
    executor.exit_position = AsyncMock(return_value=-3.00)

    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-2": {"yes_price": 0.35, "no_price": 0.65},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.20
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02

    email = AsyncMock()

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=email, settings=settings)
    await mgr.check_positions()

    executor.exit_position.assert_called_once_with(
        trade_id=2, exit_price=0.35, exit_reason="stop_loss")


@pytest.mark.asyncio
async def test_check_positions_no_exit_within_thresholds():
    """Position with 10% gain should not trigger any exit."""
    db = AsyncMock()
    db.fetch = AsyncMock(return_value=[{
        "id": 3, "side": "YES", "entry_price": 0.50, "shares": 20.0,
        "position_size_usd": 10.0, "strategy": "forecast", "status": "dry_run",
        "polymarket_id": "mkt-3", "question": "Neutral market?",
        "ensemble_probability": 0.65,
    }])

    executor = AsyncMock()
    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-3": {"yes_price": 0.55, "no_price": 0.45},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.20
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=AsyncMock(), settings=settings)
    await mgr.check_positions()

    executor.exit_position.assert_not_called()


@pytest.mark.asyncio
async def test_check_positions_skips_missing_price():
    """Position whose market is not in the price cache should be skipped."""
    db = AsyncMock()
    db.fetch = AsyncMock(return_value=[{
        "id": 4, "side": "YES", "entry_price": 0.50, "shares": 20.0,
        "position_size_usd": 10.0, "strategy": "forecast", "status": "dry_run",
        "polymarket_id": "mkt-missing", "question": "Missing price?",
        "ensemble_probability": 0.65,
    }])

    executor = AsyncMock()
    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {}

    settings = MagicMock()
    settings.take_profit_threshold = 0.20
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=AsyncMock(), settings=settings)
    await mgr.check_positions()

    executor.exit_position.assert_not_called()


@pytest.mark.asyncio
async def test_check_positions_empty_returns_early():
    """No open positions should return immediately."""
    db = AsyncMock()
    db.fetch = AsyncMock(return_value=[])

    executor = AsyncMock()
    scanner = MagicMock()

    settings = MagicMock()
    settings.take_profit_threshold = 0.20
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=AsyncMock(), settings=settings)
    await mgr.check_positions()

    scanner.get_all_cached_prices.assert_not_called()
    executor.exit_position.assert_not_called()


from datetime import datetime, timezone, timedelta


@pytest.mark.asyncio
async def test_check_positions_time_stop_forecast():
    """Forecast trade held > 120 minutes should trigger time_stop exit."""
    db = AsyncMock()
    opened_3h_ago = datetime.now(timezone.utc) - timedelta(hours=3)
    db.fetch = AsyncMock(return_value=[{
        "id": 10, "side": "YES", "entry_price": 0.50, "shares": 20.0,
        "position_size_usd": 10.0, "strategy": "forecast", "status": "dry_run",
        "polymarket_id": "mkt-time", "question": "Time stop test?",
        "ensemble_probability": 0.65, "opened_at": opened_3h_ago,
    }])

    executor = AsyncMock()
    executor.exit_position = AsyncMock(return_value=0.50)

    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-time": {"yes_price": 0.52, "no_price": 0.48},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.30
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02
    settings.forecast_time_stop_minutes = 120.0

    email = AsyncMock()

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=email, settings=settings)
    await mgr.check_positions()

    executor.exit_position.assert_called_once_with(
        trade_id=10, exit_price=0.52, exit_reason="time_stop")


@pytest.mark.asyncio
async def test_check_positions_no_time_stop_within_window():
    """Forecast trade held < 120 minutes should NOT trigger time_stop."""
    db = AsyncMock()
    opened_30m_ago = datetime.now(timezone.utc) - timedelta(minutes=30)
    db.fetch = AsyncMock(return_value=[{
        "id": 11, "side": "YES", "entry_price": 0.50, "shares": 20.0,
        "position_size_usd": 10.0, "strategy": "forecast", "status": "dry_run",
        "polymarket_id": "mkt-fresh", "question": "Fresh forecast?",
        "ensemble_probability": 0.65, "opened_at": opened_30m_ago,
    }])

    executor = AsyncMock()
    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-fresh": {"yes_price": 0.52, "no_price": 0.48},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.30
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02
    settings.forecast_time_stop_minutes = 120.0

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=AsyncMock(), settings=settings)
    await mgr.check_positions()

    executor.exit_position.assert_not_called()


@pytest.mark.asyncio
async def test_check_positions_no_time_stop_for_snipe():
    """Snipe trade held > 120 minutes should NOT trigger time_stop (forecast only)."""
    db = AsyncMock()
    opened_3h_ago = datetime.now(timezone.utc) - timedelta(hours=3)
    db.fetch = AsyncMock(return_value=[{
        "id": 12, "side": "YES", "entry_price": 0.95, "shares": 100.0,
        "position_size_usd": 95.0, "strategy": "snipe", "status": "dry_run",
        "polymarket_id": "mkt-snipe-old", "question": "Old snipe?",
        "ensemble_probability": None, "opened_at": opened_3h_ago,
    }])

    executor = AsyncMock()
    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-snipe-old": {"yes_price": 0.96, "no_price": 0.04},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.30
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02
    settings.forecast_time_stop_minutes = 120.0

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=AsyncMock(), settings=settings)
    await mgr.check_positions()

    # Snipe at 0.95 → 0.96 is only 1% gain, below 30% TP. No exit should fire.
    executor.exit_position.assert_not_called()
