import pytest
from polybot.trading.inventory import InventoryTracker, MarketInventory


class TestMarketInventory:
    def test_net_delta_long_yes(self):
        inv = MarketInventory(polymarket_id="m1", yes_shares=100, no_shares=50)
        assert inv.net_delta == 50

    def test_net_delta_long_no(self):
        inv = MarketInventory(polymarket_id="m1", yes_shares=30, no_shares=80)
        assert inv.net_delta == -50

    def test_net_delta_flat(self):
        inv = MarketInventory(polymarket_id="m1", yes_shares=50, no_shares=50)
        assert inv.net_delta == 0


class TestInventoryTracker:
    def test_record_fill_buy(self):
        tracker = InventoryTracker()
        tracker.record_fill("m1", "BUY", 0.60, 10.0)
        inv = tracker.get_inventory("m1")
        assert inv is not None
        assert inv.yes_shares == 10.0
        assert inv.cost_basis == pytest.approx(6.0)

    def test_record_fill_sell(self):
        tracker = InventoryTracker()
        tracker.record_fill("m1", "BUY", 0.60, 10.0)
        tracker.record_fill("m1", "SELL", 0.65, 5.0)
        inv = tracker.get_inventory("m1")
        assert inv.yes_shares == 5.0

    def test_compute_skew_flat(self):
        tracker = InventoryTracker(max_skew_bps=100)
        bid_adj, ask_adj = tracker.compute_skew("m1")
        assert bid_adj == 0.0
        assert ask_adj == 0.0

    def test_compute_skew_long(self):
        tracker = InventoryTracker(max_per_market=50.0, max_skew_bps=100)
        tracker.record_fill("m1", "BUY", 0.50, 25.0)
        # net_delta = 25, max = 50, so skew_frac = 0.5
        bid_adj, ask_adj = tracker.compute_skew("m1")
        assert bid_adj == pytest.approx(0.005)  # 0.5 * 100/10000

    def test_compute_skew_clamped(self):
        tracker = InventoryTracker(max_per_market=10.0, max_skew_bps=100)
        tracker.record_fill("m1", "BUY", 0.50, 100.0)
        # net_delta = 100 >> max=10, so frac clamped to 1.0
        bid_adj, _ = tracker.compute_skew("m1")
        assert bid_adj == pytest.approx(0.01)  # 1.0 * 100/10000

    def test_get_total_exposure(self):
        tracker = InventoryTracker()
        tracker.record_fill("m1", "BUY", 0.50, 20.0)
        tracker.record_fill("m2", "BUY", 0.60, 10.0)
        exposure = tracker.get_total_exposure()
        # m1: |20| * 0.5 = 10.0, m2: |10| * 0.5 = 5.0
        assert exposure == pytest.approx(15.0)

    def test_all_inventories(self):
        tracker = InventoryTracker()
        tracker.record_fill("m1", "BUY", 0.50, 10.0)
        tracker.record_fill("m2", "BUY", 0.60, 5.0)
        all_inv = tracker.all_inventories()
        assert len(all_inv) == 2
        assert "m1" in all_inv
        assert "m2" in all_inv
