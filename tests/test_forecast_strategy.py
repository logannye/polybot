import pytest
from unittest.mock import MagicMock
from polybot.strategies.forecast import EnsembleForecastStrategy


def test_forecast_strategy_attrs():
    settings = MagicMock()
    settings.forecast_interval_seconds = 300
    settings.forecast_kelly_mult = 0.25
    settings.forecast_max_single_pct = 0.15
    s = EnsembleForecastStrategy(settings=settings, ensemble=MagicMock(), researcher=MagicMock())
    assert s.name == "forecast"
    assert s.interval_seconds == 300
    assert s.kelly_multiplier == 0.25
    assert s.max_single_pct == 0.15
