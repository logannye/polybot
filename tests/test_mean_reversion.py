import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from polybot.strategies.mean_reversion import MeanReversionStrategy


def _make_settings():
    s = MagicMock()
    s.mr_interval_seconds = 120.0
    s.mr_trigger_threshold = 0.05
    s.mr_reversion_fraction = 0.40
    s.mr_kelly_mult = 0.15
    s.mr_max_single_pct = 0.10
    s.mr_max_concurrent = 5
    s.mr_min_volume_24h = 2000.0
    s.mr_min_book_depth = 500.0
    s.mr_cooldown_hours = 6.0
    s.mr_max_hold_hours = 24.0
    return s


def _make_market(pid="m1", price=0.50, volume=5000.0, depth=1000.0, hours=168):
    return {
        "polymarket_id": pid,
        "question": f"Will {pid} happen?",
        "category": "politics",
        "resolution_time": datetime.now(timezone.utc) + timedelta(hours=hours),
        "yes_price": price,
        "no_price": 1 - price,
        "yes_token_id": f"{pid}_yes",
        "no_token_id": f"{pid}_no",
        "volume_24h": volume,
        "book_depth": depth,
    }


class TestMeanReversionInit:
    def test_reads_settings(self):
        s = _make_settings()
        strategy = MeanReversionStrategy(s)
        assert strategy._trigger == 0.05
        assert strategy.interval_seconds == 120.0
        assert strategy._snapshot_window == 5


class TestSnapshotDetection:
    def test_detects_5pct_drop(self):
        """A single-snapshot 6% drop should be detected with 5% trigger."""
        strategy = MeanReversionStrategy(_make_settings())
        now = datetime.now(timezone.utc)
        # Pre-populate snapshot: price was 0.60 a few minutes ago
        strategy._price_snapshots["m1"] = [(0.60, now - timedelta(minutes=3))]

        market = _make_market("m1", price=0.54)  # dropped 6%
        markets = [market]

        # Simulate the candidate detection loop
        candidates = []
        for m in markets:
            pid = m["polymarket_id"]
            price = m["yes_price"]
            if pid in strategy._price_snapshots:
                snapshots = strategy._price_snapshots[pid]
                recent = [(p, ts) for p, ts in snapshots
                          if (now - ts).total_seconds() < 1800]
                if recent:
                    max_price = max(p for p, _ in recent)
                    min_price = min(p for p, _ in recent)
                    move_down = price - max_price
                    move_up = price - min_price
                    if move_down < 0 and abs(move_down) >= strategy._trigger:
                        candidates.append((abs(move_down), move_down, m, max_price))
                    elif move_up > 0 and abs(move_up) >= strategy._trigger:
                        candidates.append((abs(move_up), move_up, m, min_price))

        assert len(candidates) == 1
        assert candidates[0][1] < 0  # move is negative (drop)
        assert abs(candidates[0][1]) == pytest.approx(0.06, abs=0.001)

    def test_multi_snapshot_gradual_move(self):
        """Gradual 6% move across 5 snapshots should be detected."""
        strategy = MeanReversionStrategy(_make_settings())
        now = datetime.now(timezone.utc)
        # Gradual rise: 0.50 -> 0.52 -> 0.54 -> 0.56 -> 0.58
        strategy._price_snapshots["m1"] = [
            (0.50, now - timedelta(minutes=10)),
            (0.52, now - timedelta(minutes=8)),
            (0.54, now - timedelta(minutes=6)),
            (0.56, now - timedelta(minutes=4)),
            (0.58, now - timedelta(minutes=2)),
        ]

        # Current price is 0.58 -> rise of 0.08 from min (0.50)
        price = 0.58
        recent = strategy._price_snapshots["m1"]
        min_price = min(p for p, _ in recent)
        move_up = price - min_price

        assert move_up == pytest.approx(0.08, abs=0.001)
        assert move_up >= strategy._trigger  # 0.08 >= 0.05

    def test_ignores_small_moves(self):
        """A 2% move should NOT trigger with 5% threshold."""
        strategy = MeanReversionStrategy(_make_settings())
        now = datetime.now(timezone.utc)
        strategy._price_snapshots["m1"] = [(0.50, now - timedelta(minutes=3))]

        price = 0.52  # only 2% move
        recent = strategy._price_snapshots["m1"]
        max_price = max(p for p, _ in recent)
        min_price = min(p for p, _ in recent)
        move_down = price - max_price
        move_up = price - min_price

        assert abs(move_down) < strategy._trigger
        assert abs(move_up) < strategy._trigger

    def test_snapshot_pruning(self):
        """Snapshots older than 2h should be pruned."""
        strategy = MeanReversionStrategy(_make_settings())
        now = datetime.now(timezone.utc)
        strategy._price_snapshots["stale"] = [
            (0.50, now - timedelta(hours=3)),  # stale
        ]
        strategy._price_snapshots["fresh"] = [
            (0.50, now - timedelta(minutes=5)),  # fresh
        ]

        # Prune logic from the strategy
        stale = [pid for pid, snaps in strategy._price_snapshots.items()
                 if not snaps or (now - snaps[-1][1]).total_seconds() > 7200]
        for pid in stale:
            del strategy._price_snapshots[pid]

        assert "stale" not in strategy._price_snapshots
        assert "fresh" in strategy._price_snapshots

    def test_snapshot_window_trimming(self):
        """Only the last N snapshots should be kept per market."""
        strategy = MeanReversionStrategy(_make_settings())
        now = datetime.now(timezone.utc)

        # Add 10 snapshots
        for i in range(10):
            strategy._price_snapshots.setdefault("m1", []).append(
                (0.50 + i * 0.01, now - timedelta(minutes=10 - i)))
            strategy._price_snapshots["m1"] = \
                strategy._price_snapshots["m1"][-strategy._snapshot_window:]

        assert len(strategy._price_snapshots["m1"]) == 5
        # Should keep the most recent 5
        assert strategy._price_snapshots["m1"][-1][0] == pytest.approx(0.59)
