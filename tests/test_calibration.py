"""Tests for the calibration correction module."""

import pytest
from polybot.analysis.calibration import (
    calibration_adjusted_prob,
    is_political_market,
    get_domain_slope,
)


class TestCalibrationAdjustedProb:
    def test_70_cent_political_maps_to_approx_75(self):
        """A 70-cent political contract (slope 1.31) implies ~75% true probability.

        logit(0.70) ≈ 0.847, slope * logit ≈ 1.110, sigmoid(1.110) ≈ 0.752.
        """
        result = calibration_adjusted_prob(0.70, slope=1.31)
        assert 0.73 < result < 0.78, f"Expected 0.73 < {result} < 0.78"

    def test_30_cent_contract_maps_to_15_to_25(self):
        """A 30-cent contract (slope 1.31) should map to between 0.15 and 0.25."""
        result = calibration_adjusted_prob(0.30, slope=1.31)
        assert 0.15 < result < 0.25, f"Expected 0.15 < {result} < 0.25"

    def test_50_cent_stays_at_50(self):
        """A 50-cent contract stays at ~0.50 regardless of slope (pivot point)."""
        for slope in [1.0, 1.05, 1.10, 1.31, 1.50]:
            result = calibration_adjusted_prob(0.50, slope=slope)
            assert abs(result - 0.50) < 1e-6, (
                f"Expected ~0.50 for slope={slope}, got {result}"
            )

    def test_output_clamped_to_01_99(self):
        """Output should be clamped to [0.01, 0.99]."""
        # Very high price with high slope
        result_high = calibration_adjusted_prob(0.999, slope=2.0)
        assert result_high <= 0.99

        # Very low price with high slope
        result_low = calibration_adjusted_prob(0.001, slope=2.0)
        assert result_low >= 0.01

    def test_slope_1_is_identity(self):
        """Slope of 1.0 should return approximately the same price (identity)."""
        for price in [0.10, 0.30, 0.50, 0.70, 0.90]:
            result = calibration_adjusted_prob(price, slope=1.0)
            assert abs(result - price) < 1e-6, (
                f"Expected identity for price={price}, got {result}"
            )

    def test_sports_slope_gives_minimal_correction(self):
        """Sports slope 1.05: a 0.70 price should map to 0.70-0.75 (small correction)."""
        result = calibration_adjusted_prob(0.70, slope=1.05)
        assert 0.70 <= result <= 0.75, f"Expected 0.70 <= {result} <= 0.75"

    def test_favorites_are_boosted(self):
        """Prices above 0.50 should be boosted upward by any slope > 1."""
        for price in [0.55, 0.60, 0.70, 0.80]:
            result = calibration_adjusted_prob(price, slope=1.31)
            assert result > price, f"Expected {result} > {price} (favorite boost)"

    def test_underdogs_are_compressed(self):
        """Prices below 0.50 should be compressed downward by any slope > 1."""
        for price in [0.45, 0.40, 0.30, 0.20]:
            result = calibration_adjusted_prob(price, slope=1.31)
            assert result < price, f"Expected {result} < {price} (underdog compression)"


class TestIsPoliticalMarket:
    def test_politics_tag_is_political(self):
        assert is_political_market(["politics"]) is True

    def test_geopolitics_tag_is_political(self):
        assert is_political_market(["geopolitics"]) is True

    def test_global_elections_tag_is_political(self):
        assert is_political_market(["global-elections"]) is True

    def test_world_tag_is_political(self):
        assert is_political_market(["world"]) is True

    def test_trump_presidency_tag_is_political(self):
        assert is_political_market(["trump-presidency"]) is True

    def test_foreign_policy_tag_is_political(self):
        assert is_political_market(["foreign-policy"]) is True

    def test_sports_only_is_not_political(self):
        assert is_political_market(["sports"]) is False

    def test_empty_tags_is_not_political(self):
        assert is_political_market([]) is False

    def test_crypto_is_not_political(self):
        assert is_political_market(["crypto", "finance"]) is False

    def test_mixed_tags_with_political_is_political(self):
        assert is_political_market(["sports", "politics", "finance"]) is True


class TestGetDomainSlope:
    def test_politics_returns_1_31(self):
        assert get_domain_slope(["politics"]) == 1.31

    def test_geopolitics_returns_1_31(self):
        assert get_domain_slope(["geopolitics"]) == 1.31

    def test_crypto_returns_1_05(self):
        assert get_domain_slope(["crypto"]) == 1.05

    def test_sports_returns_1_05(self):
        assert get_domain_slope(["sports"]) == 1.05

    def test_finance_returns_1_10(self):
        assert get_domain_slope(["finance"]) == 1.10

    def test_world_returns_1_20(self):
        assert get_domain_slope(["world"]) == 1.20

    def test_unknown_tags_return_default_1_10(self):
        assert get_domain_slope(["unknown-tag", "another-unknown"]) == 1.10

    def test_empty_tags_return_default_1_10(self):
        assert get_domain_slope([]) == 1.10

    def test_first_matching_tag_wins(self):
        """The first tag in the list that matches a known domain wins."""
        result = get_domain_slope(["politics", "crypto"])
        assert result == 1.31

    def test_mixed_known_unknown_returns_known(self):
        result = get_domain_slope(["unknown", "sports"])
        assert result == 1.05
