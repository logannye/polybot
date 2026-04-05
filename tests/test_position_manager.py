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
async def test_snipe_skips_early_exit_even_with_ensemble_prob():
    """Snipe trades must never trigger early_exit, even with non-None ensemble_probability."""
    db = AsyncMock()
    db.fetch = AsyncMock(return_value=[{
        "id": 50, "side": "YES", "entry_price": 0.97, "shares": 100.0,
        "position_size_usd": 97.0, "strategy": "snipe", "status": "dry_run",
        "polymarket_id": "mkt-snipe", "question": "Snipe early_exit test?",
        "ensemble_probability": 1.0, "opened_at": None,
    }])

    executor = AsyncMock()
    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-snipe": {"yes_price": 0.96, "no_price": 0.04},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.20
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=AsyncMock(), settings=settings)
    await mgr.check_positions()

    # Snipe should NOT trigger early_exit regardless of ensemble_probability
    executor.exit_position.assert_not_called()


@pytest.mark.asyncio
async def test_check_positions_time_stop_forecast():
    """Losing forecast trade held > effective stop should trigger time_stop exit (long-dated market)."""
    db = AsyncMock()
    opened_8h_ago = datetime.now(timezone.utc) - timedelta(hours=8)
    resolves_72h = datetime.now(timezone.utc) + timedelta(hours=72)
    db.fetch = AsyncMock(return_value=[{
        "id": 10, "side": "YES", "entry_price": 0.50, "shares": 20.0,
        "position_size_usd": 10.0, "strategy": "forecast", "status": "dry_run",
        "polymarket_id": "mkt-time", "question": "Time stop test?",
        "ensemble_probability": 0.65, "opened_at": opened_8h_ago,
        "resolution_time": resolves_72h,
    }])

    executor = AsyncMock()
    executor.exit_position = AsyncMock(return_value=-0.80)

    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-time": {"yes_price": 0.48, "no_price": 0.52},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.30
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02
    settings.forecast_time_stop_minutes = 60.0
    settings.forecast_time_stop_fraction = 0.10
    settings.forecast_time_stop_max_minutes = 480.0
    settings.forecast_time_stop_min_resolution_hours = 48.0

    email = AsyncMock()

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=email, settings=settings)
    await mgr.check_positions()

    # 72h > 48h threshold → time-stop active
    # effective_stop = max(60, 0.10 * 72 * 60) = max(60, 432) = 432 min; held 480 > 432 → fires
    executor.exit_position.assert_called_once_with(
        trade_id=10, exit_price=0.48, exit_reason="time_stop")


@pytest.mark.asyncio
async def test_check_positions_no_time_stop_within_window():
    """Forecast trade held < effective stop should NOT trigger time_stop."""
    db = AsyncMock()
    opened_55m_ago = datetime.now(timezone.utc) - timedelta(minutes=55)
    resolves_2h = datetime.now(timezone.utc) + timedelta(hours=2)
    db.fetch = AsyncMock(return_value=[{
        "id": 11, "side": "YES", "entry_price": 0.50, "shares": 20.0,
        "position_size_usd": 10.0, "strategy": "forecast", "status": "dry_run",
        "polymarket_id": "mkt-fresh", "question": "Fresh forecast?",
        "ensemble_probability": 0.65, "opened_at": opened_55m_ago,
        "resolution_time": resolves_2h,
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
    settings.forecast_time_stop_minutes = 60.0
    settings.forecast_time_stop_fraction = 0.10
    settings.forecast_time_stop_max_minutes = 480.0
    settings.forecast_time_stop_min_resolution_hours = 48.0

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
    resolves_1h = datetime.now(timezone.utc) + timedelta(hours=1)
    db.fetch = AsyncMock(return_value=[{
        "id": 12, "side": "YES", "entry_price": 0.95, "shares": 100.0,
        "position_size_usd": 95.0, "strategy": "snipe", "status": "dry_run",
        "polymarket_id": "mkt-snipe-old", "question": "Old snipe?",
        "ensemble_probability": None, "opened_at": opened_3h_ago,
        "resolution_time": resolves_1h,
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
    settings.forecast_time_stop_minutes = 20.0
    settings.forecast_time_stop_fraction = 0.10
    settings.forecast_time_stop_max_minutes = 480.0
    settings.forecast_time_stop_min_resolution_hours = 48.0

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=AsyncMock(), settings=settings)
    await mgr.check_positions()

    # Snipe at 0.95 → 0.96 is only 1% gain, below 30% TP. No exit should fire.
    executor.exit_position.assert_not_called()


@pytest.mark.asyncio
async def test_time_stop_skips_profitable_trade():
    """Forecast trade past time-stop but profitable should NOT be time-stopped."""
    db = AsyncMock()
    opened_65m_ago = datetime.now(timezone.utc) - timedelta(minutes=65)
    resolves_2h = datetime.now(timezone.utc) + timedelta(hours=2)
    db.fetch = AsyncMock(return_value=[{
        "id": 20, "side": "YES", "entry_price": 0.50, "shares": 20.0,
        "position_size_usd": 10.0, "strategy": "forecast", "status": "dry_run",
        "polymarket_id": "mkt-profit", "question": "Profitable but old?",
        "ensemble_probability": 0.65, "opened_at": opened_65m_ago,
        "resolution_time": resolves_2h,
    }])

    executor = AsyncMock()
    scanner = MagicMock()
    # 10% gain (0.50 → 0.55), below 30% TP threshold
    scanner.get_all_cached_prices.return_value = {
        "mkt-profit": {"yes_price": 0.55, "no_price": 0.45},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.30
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02
    settings.forecast_time_stop_minutes = 60.0
    settings.forecast_time_stop_fraction = 0.10
    settings.forecast_time_stop_max_minutes = 480.0
    settings.forecast_time_stop_min_resolution_hours = 48.0

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=AsyncMock(), settings=settings)
    await mgr.check_positions()

    # Profitable: time-stop skipped, 10% gain below 30% TP, no exit fires
    executor.exit_position.assert_not_called()


@pytest.mark.asyncio
async def test_time_stop_exits_flat_trade():
    """Forecast trade past time-stop at breakeven (unrealized=0) should be time-stopped (long-dated)."""
    db = AsyncMock()
    opened_8h_ago = datetime.now(timezone.utc) - timedelta(hours=8)
    resolves_72h = datetime.now(timezone.utc) + timedelta(hours=72)
    db.fetch = AsyncMock(return_value=[{
        "id": 21, "side": "YES", "entry_price": 0.50, "shares": 20.0,
        "position_size_usd": 10.0, "strategy": "forecast", "status": "dry_run",
        "polymarket_id": "mkt-flat", "question": "Flat trade?",
        "ensemble_probability": 0.65, "opened_at": opened_8h_ago,
        "resolution_time": resolves_72h,
    }])

    executor = AsyncMock()
    executor.exit_position = AsyncMock(return_value=0.0)

    scanner = MagicMock()
    # Exactly breakeven: entry 0.50, current 0.50
    scanner.get_all_cached_prices.return_value = {
        "mkt-flat": {"yes_price": 0.50, "no_price": 0.50},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.30
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02
    settings.forecast_time_stop_minutes = 60.0
    settings.forecast_time_stop_fraction = 0.10
    settings.forecast_time_stop_max_minutes = 480.0
    settings.forecast_time_stop_min_resolution_hours = 48.0

    email = AsyncMock()

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=email, settings=settings)
    await mgr.check_positions()

    # 72h > 48h → time-stop active; held 480min > 432min effective → fires
    executor.exit_position.assert_called_once_with(
        trade_id=21, exit_price=0.50, exit_reason="time_stop")


@pytest.mark.asyncio
async def test_profitable_trade_still_hits_tp_after_time_stop_skip():
    """Profitable trade past time-stop should fall through and hit take_profit."""
    db = AsyncMock()
    opened_65m_ago = datetime.now(timezone.utc) - timedelta(minutes=65)
    resolves_2h = datetime.now(timezone.utc) + timedelta(hours=2)
    db.fetch = AsyncMock(return_value=[{
        "id": 22, "side": "YES", "entry_price": 0.50, "shares": 20.0,
        "position_size_usd": 10.0, "strategy": "forecast", "status": "dry_run",
        "polymarket_id": "mkt-tp", "question": "TP after time-stop skip?",
        "ensemble_probability": 0.65, "opened_at": opened_65m_ago,
        "resolution_time": resolves_2h,
    }])

    executor = AsyncMock()
    executor.exit_position = AsyncMock(return_value=3.50)

    scanner = MagicMock()
    # 36% gain (0.50 → 0.68), above 30% TP threshold
    scanner.get_all_cached_prices.return_value = {
        "mkt-tp": {"yes_price": 0.68, "no_price": 0.32},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.30
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02
    settings.forecast_time_stop_minutes = 60.0
    settings.forecast_time_stop_fraction = 0.10
    settings.forecast_time_stop_max_minutes = 480.0
    settings.forecast_time_stop_min_resolution_hours = 48.0

    email = AsyncMock()

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=email, settings=settings)
    await mgr.check_positions()

    # Time-stop skipped (profitable), then TP fires
    executor.exit_position.assert_called_once_with(
        trade_id=22, exit_price=0.68, exit_reason="take_profit")


@pytest.mark.asyncio
async def test_time_stop_skipped_near_resolution():
    """Forecast trade with ≤48h to resolution should NOT be time-stopped regardless of hold time."""
    db = AsyncMock()
    opened_6h_ago = datetime.now(timezone.utc) - timedelta(hours=6)
    resolves_24h = datetime.now(timezone.utc) + timedelta(hours=24)
    db.fetch = AsyncMock(return_value=[{
        "id": 40, "side": "YES", "entry_price": 0.50, "shares": 20.0,
        "position_size_usd": 10.0, "strategy": "forecast", "status": "dry_run",
        "polymarket_id": "mkt-near", "question": "Near-resolution market?",
        "ensemble_probability": 0.65, "opened_at": opened_6h_ago,
        "resolution_time": resolves_24h,
    }])

    executor = AsyncMock()
    scanner = MagicMock()
    # Slightly losing: -4% (within stop-loss threshold of 25%)
    scanner.get_all_cached_prices.return_value = {
        "mkt-near": {"yes_price": 0.48, "no_price": 0.52},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.30
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02
    settings.forecast_time_stop_minutes = 60.0
    settings.forecast_time_stop_fraction = 0.10
    settings.forecast_time_stop_max_minutes = 480.0
    settings.forecast_time_stop_min_resolution_hours = 48.0

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=AsyncMock(), settings=settings)
    await mgr.check_positions()

    # 24h ≤ 48h → time-stop skipped; -4% within 25% SL → no exit
    executor.exit_position.assert_not_called()


@pytest.mark.asyncio
async def test_time_stop_fires_far_resolution():
    """Forecast trade with >48h to resolution should still be time-stopped normally."""
    db = AsyncMock()
    opened_10h_ago = datetime.now(timezone.utc) - timedelta(hours=10)
    resolves_96h = datetime.now(timezone.utc) + timedelta(hours=96)
    db.fetch = AsyncMock(return_value=[{
        "id": 41, "side": "YES", "entry_price": 0.50, "shares": 20.0,
        "position_size_usd": 10.0, "strategy": "forecast", "status": "dry_run",
        "polymarket_id": "mkt-far", "question": "Far-resolution market?",
        "ensemble_probability": 0.55, "opened_at": opened_10h_ago,
        "resolution_time": resolves_96h,
    }])

    executor = AsyncMock()
    executor.exit_position = AsyncMock(return_value=-0.50)

    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-far": {"yes_price": 0.48, "no_price": 0.52},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.30
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02
    settings.forecast_time_stop_minutes = 60.0
    settings.forecast_time_stop_fraction = 0.10
    settings.forecast_time_stop_max_minutes = 480.0
    settings.forecast_time_stop_min_resolution_hours = 48.0

    email = AsyncMock()

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=email, settings=settings)
    await mgr.check_positions()

    # 96h > 48h → time-stop active
    # effective_stop = min(480, max(60, 0.10 * 96 * 60)) = min(480, 576) = 480 min
    # Held 600min > 480 → fires
    executor.exit_position.assert_called_once_with(
        trade_id=41, exit_price=0.48, exit_reason="time_stop")


@pytest.mark.asyncio
async def test_dynamic_time_stop_long_dated_market():
    """Long-dated market (48h) should get ~288min effective stop, not exit at 65min."""
    db = AsyncMock()
    opened_65m_ago = datetime.now(timezone.utc) - timedelta(minutes=65)
    resolves_48h = datetime.now(timezone.utc) + timedelta(hours=48)
    db.fetch = AsyncMock(return_value=[{
        "id": 30, "side": "NO", "entry_price": 0.65, "shares": 30.0,
        "position_size_usd": 19.5, "strategy": "forecast", "status": "dry_run",
        "polymarket_id": "mkt-long", "question": "Long-dated NCAA market?",
        "ensemble_probability": 0.30, "opened_at": opened_65m_ago,
        "resolution_time": resolves_48h,
    }])

    executor = AsyncMock()
    scanner = MagicMock()
    # Slightly losing (NO side: entry 0.65, current NO = 1-0.36 = 0.64)
    scanner.get_all_cached_prices.return_value = {
        "mkt-long": {"yes_price": 0.36, "no_price": 0.64},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.30
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02
    settings.forecast_time_stop_minutes = 20.0       # floor
    settings.forecast_time_stop_fraction = 0.10       # 10% of 48h = 288 min
    settings.forecast_time_stop_max_minutes = 480.0   # cap
    settings.forecast_time_stop_min_resolution_hours = 48.0

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=AsyncMock(), settings=settings)
    await mgr.check_positions()

    # effective_stop = max(20, 0.10 * 48 * 60) = max(20, 288) = 288 min
    # Held only 65 min < 288 → should NOT time-stop
    executor.exit_position.assert_not_called()


@pytest.mark.asyncio
async def test_dynamic_time_stop_respects_cap():
    """Very long-dated market should cap at max_minutes, not hold forever."""
    db = AsyncMock()
    opened_10h_ago = datetime.now(timezone.utc) - timedelta(hours=10)
    resolves_200h = datetime.now(timezone.utc) + timedelta(hours=200)
    db.fetch = AsyncMock(return_value=[{
        "id": 31, "side": "YES", "entry_price": 0.50, "shares": 20.0,
        "position_size_usd": 10.0, "strategy": "forecast", "status": "dry_run",
        "polymarket_id": "mkt-cap", "question": "Very long market?",
        "ensemble_probability": 0.55, "opened_at": opened_10h_ago,
        "resolution_time": resolves_200h,
    }])

    executor = AsyncMock()
    executor.exit_position = AsyncMock(return_value=-0.50)

    scanner = MagicMock()
    # Slightly losing
    scanner.get_all_cached_prices.return_value = {
        "mkt-cap": {"yes_price": 0.48, "no_price": 0.52},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.30
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02
    settings.forecast_time_stop_minutes = 20.0
    settings.forecast_time_stop_fraction = 0.10       # 10% of 200h = 1200 min
    settings.forecast_time_stop_max_minutes = 480.0   # cap
    settings.forecast_time_stop_min_resolution_hours = 48.0

    email = AsyncMock()

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=email, settings=settings)
    await mgr.check_positions()

    # effective_stop = min(480, max(20, 0.10 * 200 * 60)) = min(480, 1200) = 480 min
    # Held 600 min > 480 → should time-stop
    executor.exit_position.assert_called_once_with(
        trade_id=31, exit_price=0.48, exit_reason="time_stop")


@pytest.mark.asyncio
async def test_mr_min_edge_no_early_exit():
    """MR trade with expected_reversion=0.02 must NOT trigger generic early_exit.

    Bug: MR stores tp_yes_price as ensemble_probability. The generic
    should_early_exit computes remaining_edge = tp - current = 0.02,
    which equals early_exit_edge (0.02), triggering immediate exit.
    """
    db = AsyncMock()
    db.fetch = AsyncMock(return_value=[{
        "id": 100, "side": "YES", "entry_price": 0.595, "shares": 5.97,
        "position_size_usd": 3.55, "strategy": "mean_reversion",
        "status": "dry_run", "opened_at": datetime.now(timezone.utc),
        "polymarket_id": "mkt-mr-min", "question": "MR min edge test?",
        "resolution_time": datetime.now(timezone.utc) + timedelta(hours=168),
        "ensemble_probability": 0.615,  # this is tp_yes_price, NOT ensemble prob
        "kelly_inputs": {
            "move": -0.05, "old_price": 0.645, "trigger_price": 0.595,
            "expected_reversion": 0.02, "tp_yes_price": 0.615,
            "sl_yes_price": 0.5825, "max_hold_hours": 24.0,
        },
    }])

    executor = AsyncMock()
    scanner = MagicMock()
    # Price unchanged from entry — no TP or SL hit
    scanner.get_all_cached_prices.return_value = {
        "mkt-mr-min": {"yes_price": 0.595, "no_price": 0.405},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.20
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=AsyncMock(), settings=settings)
    await mgr.check_positions()

    # Must NOT exit — the MR custom block found no TP/SL, and generic
    # early_exit should be skipped entirely for mean_reversion trades
    executor.exit_position.assert_not_called()


@pytest.mark.asyncio
async def test_mr_min_edge_no_side_no_early_exit():
    """MR NO-side trade with expected_reversion=0.02 must NOT early_exit."""
    db = AsyncMock()
    db.fetch = AsyncMock(return_value=[{
        "id": 101, "side": "NO", "entry_price": 0.445, "shares": 5.82,
        "position_size_usd": 2.59, "strategy": "mean_reversion",
        "status": "dry_run", "opened_at": datetime.now(timezone.utc),
        "polymarket_id": "mkt-mr-no", "question": "MR NO side test?",
        "resolution_time": datetime.now(timezone.utc) + timedelta(hours=168),
        "ensemble_probability": 0.535,  # tp_yes_price, NOT ensemble prob
        "kelly_inputs": {
            "move": 0.05, "old_price": 0.505, "trigger_price": 0.555,
            "expected_reversion": 0.02, "tp_yes_price": 0.535,
            "sl_yes_price": 0.5675, "max_hold_hours": 24.0,
        },
    }])

    executor = AsyncMock()
    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-mr-no": {"yes_price": 0.555, "no_price": 0.445},
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
async def test_mr_custom_tp_still_fires():
    """MR trade that hits its tp_yes_price must still exit as take_profit."""
    db = AsyncMock()
    db.fetch = AsyncMock(return_value=[{
        "id": 102, "side": "YES", "entry_price": 0.24, "shares": 18.32,
        "position_size_usd": 4.37, "strategy": "mean_reversion",
        "status": "dry_run", "opened_at": datetime.now(timezone.utc),
        "polymarket_id": "mkt-mr-tp", "question": "MR TP test?",
        "resolution_time": datetime.now(timezone.utc) + timedelta(hours=168),
        "ensemble_probability": 0.306,
        "kelly_inputs": {
            "move": -0.066, "old_price": 0.306, "trigger_price": 0.24,
            "expected_reversion": 0.0264, "tp_yes_price": 0.2664,
            "sl_yes_price": 0.2235, "max_hold_hours": 24.0,
        },
    }])

    executor = AsyncMock()
    executor.exit_position = AsyncMock(return_value=1.24)

    scanner = MagicMock()
    # Price reverted past the TP target (0.306 > 0.2664)
    scanner.get_all_cached_prices.return_value = {
        "mkt-mr-tp": {"yes_price": 0.306, "no_price": 0.694},
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
        trade_id=102, exit_price=0.306, exit_reason="take_profit")
