import pytest
from polybot.trading.fees import (
    get_fee_rate, get_maker_rebate_pct,
    compute_taker_fee_per_share, compute_taker_fee_per_dollar,
    compute_maker_fee, compute_maker_rebate,
)


class TestGetFeeRate:
    def test_crypto(self):
        assert get_fee_rate("crypto") == 0.072

    def test_sports(self):
        assert get_fee_rate("sports") == 0.030

    def test_geopolitics_is_free(self):
        assert get_fee_rate("geopolitics") == 0.0

    def test_politics(self):
        assert get_fee_rate("politics") == 0.040

    def test_unknown_category_returns_default(self):
        assert get_fee_rate("alien_markets") == 0.040

    def test_case_insensitive(self):
        assert get_fee_rate("CRYPTO") == 0.072
        assert get_fee_rate("Sports") == 0.030

    def test_substring_match(self):
        assert get_fee_rate("us-politics") == 0.040


class TestTakerFees:
    def test_midpoint_price(self):
        # At p=0.50, fee = rate * 0.50 * 0.50 = rate * 0.25
        fee = compute_taker_fee_per_share(0.50, 0.04)
        assert fee == pytest.approx(0.01)

    def test_extreme_price_low_fee(self):
        # At p=0.95, fee = 0.04 * 0.95 * 0.05 = 0.0019
        fee = compute_taker_fee_per_share(0.95, 0.04)
        assert fee == pytest.approx(0.0019)

    def test_symmetric(self):
        # Fee at p=0.20 should equal fee at p=0.80
        assert compute_taker_fee_per_share(0.20, 0.04) == pytest.approx(
            compute_taker_fee_per_share(0.80, 0.04))

    def test_zero_at_extremes(self):
        assert compute_taker_fee_per_share(0.0, 0.04) == 0.0
        assert compute_taker_fee_per_share(1.0, 0.04) == 0.0


class TestTakerFeePerDollar:
    def test_basic(self):
        # fee_per_dollar = feeRate * (1 - price)
        assert compute_taker_fee_per_dollar(0.50, 0.04) == pytest.approx(0.02)

    def test_at_extreme_price(self):
        # At p=0.95: 0.04 * 0.05 = 0.002
        assert compute_taker_fee_per_dollar(0.95, 0.04) == pytest.approx(0.002)

    def test_crypto_midpoint(self):
        # Crypto at p=0.50: 0.072 * 0.50 = 0.036
        assert compute_taker_fee_per_dollar(0.50, 0.072) == pytest.approx(0.036)

    def test_much_less_than_flat_2pct_at_extreme(self):
        # At p=0.95 with finance rate, fee_per_dollar = 0.002
        # Old flat model assumed 0.02 — 10x overestimate
        actual = compute_taker_fee_per_dollar(0.95, 0.04)
        assert actual < 0.02 / 5  # At least 5x less than old flat rate


class TestMakerFee:
    def test_always_zero(self):
        assert compute_maker_fee() == 0.0


class TestMakerRebate:
    def test_sports_rebate(self):
        # 25% of taker fee
        taker_fee = compute_taker_fee_per_share(0.50, 0.03)
        rebate = compute_maker_rebate(taker_fee, "sports")
        assert rebate == pytest.approx(taker_fee * 0.25)

    def test_crypto_rebate(self):
        taker_fee = compute_taker_fee_per_share(0.50, 0.072)
        rebate = compute_maker_rebate(taker_fee, "crypto")
        assert rebate == pytest.approx(taker_fee * 0.20)

    def test_geopolitics_no_rebate(self):
        rebate = compute_maker_rebate(0.0, "geopolitics")
        assert rebate == 0.0
