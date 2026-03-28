import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta
from polybot.core.engine import Engine, CycleResult


class TestEngine:
    @pytest.fixture
    def mock_deps(self):
        risk = MagicMock()
        risk.check_circuit_breaker.return_value = (False, None)
        return {
            "db": AsyncMock(),
            "scanner": AsyncMock(),
            "researcher": AsyncMock(),
            "ensemble": AsyncMock(),
            "executor": AsyncMock(),
            "recorder": AsyncMock(),
            "risk_manager": risk,
            "settings": MagicMock(),
        }

    @pytest.mark.asyncio
    async def test_cycle_skips_during_circuit_breaker(self, mock_deps):
        mock_deps["db"].fetchrow = AsyncMock(return_value={
            "circuit_breaker_until": datetime.now(timezone.utc) + timedelta(hours=6),
            "bankroll": 300.0,
            "total_deployed": 0.0,
            "daily_pnl": -70.0,
        })
        mock_deps["settings"].scan_interval_seconds = 300

        engine = Engine(**mock_deps)
        result = await engine.run_cycle()
        assert result.skipped is True
        assert result.reason == "circuit_breaker"

    @pytest.mark.asyncio
    async def test_cycle_processes_markets(self, mock_deps):
        mock_deps["db"].fetchrow = AsyncMock(return_value={
            "circuit_breaker_until": None,
            "bankroll": 300.0,
            "total_deployed": 50.0,
            "daily_pnl": 5.0,
            "kelly_mult": 0.25,
            "edge_threshold": 0.05,
            "category_scores": {},
        })
        mock_deps["db"].fetch = AsyncMock(return_value=[])
        mock_deps["scanner"].fetch_markets = AsyncMock(return_value=[])
        mock_deps["settings"].scan_interval_seconds = 300
        mock_deps["settings"].resolution_hours_max = 72
        mock_deps["settings"].min_book_depth = 500.0
        mock_deps["settings"].min_price = 0.05
        mock_deps["settings"].max_price = 0.95
        mock_deps["settings"].cooldown_minutes = 30
        mock_deps["settings"].price_move_threshold = 0.03
        mock_deps["settings"].edge_threshold = 0.05

        engine = Engine(**mock_deps)
        result = await engine.run_cycle()
        assert result.skipped is False
        assert result.markets_scanned == 0
