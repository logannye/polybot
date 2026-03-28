import pytest
from polybot.trading.executor import OrderExecutor, compute_limit_price
from polybot.trading.wallet import WalletManager


class TestComputeLimitPrice:
    def test_buy_yes_places_above_best_bid(self):
        price = compute_limit_price(side="YES", best_bid=0.49, best_ask=0.51)
        assert 0.49 <= price <= 0.51

    def test_buy_no_places_above_best_bid(self):
        price = compute_limit_price(side="NO", best_bid=0.30, best_ask=0.35)
        assert 0.30 <= price <= 0.35

    def test_exit_crosses_spread(self):
        price = compute_limit_price(side="YES", best_bid=0.60, best_ask=0.65, is_exit=True)
        assert price <= 0.61


class TestWalletManager:
    def test_compute_shares_from_usd(self):
        wm = WalletManager.__new__(WalletManager)
        assert wm.compute_shares(usd_amount=20.0, price=0.50) == pytest.approx(40.0)

    def test_compute_shares_at_high_price(self):
        wm = WalletManager.__new__(WalletManager)
        assert wm.compute_shares(usd_amount=20.0, price=0.80) == pytest.approx(25.0)


class TestOrderExecutor:
    @pytest.fixture
    def executor(self):
        ex = OrderExecutor.__new__(OrderExecutor)
        ex._fill_timeout_seconds = 120
        return ex

    def test_should_cancel_stale_order(self, executor):
        assert executor.should_cancel_order(elapsed_seconds=130) is True

    def test_should_not_cancel_fresh_order(self, executor):
        assert executor.should_cancel_order(elapsed_seconds=60) is False
