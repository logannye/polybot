"""
Polymarket fee calculator with category-specific rates.

Polymarket fee formula: shares * feeRate * price * (1 - price)
Makers always pay 0%. Taker rates vary by category.
"""

CATEGORY_FEE_RATES: dict[str, dict[str, float]] = {
    "crypto": {"taker_rate": 0.072, "maker_rebate_pct": 0.20},
    "sports": {"taker_rate": 0.030, "maker_rebate_pct": 0.25},
    "finance": {"taker_rate": 0.040, "maker_rebate_pct": 0.25},
    "politics": {"taker_rate": 0.040, "maker_rebate_pct": 0.25},
    "tech": {"taker_rate": 0.040, "maker_rebate_pct": 0.25},
    "economics": {"taker_rate": 0.050, "maker_rebate_pct": 0.25},
    "culture": {"taker_rate": 0.050, "maker_rebate_pct": 0.25},
    "weather": {"taker_rate": 0.050, "maker_rebate_pct": 0.25},
    "geopolitics": {"taker_rate": 0.0, "maker_rebate_pct": 0.0},
}

DEFAULT_TAKER_RATE = 0.040


def _match_category(category: str) -> str | None:
    """Match a category string to a known fee schedule key.

    Longer keys are checked first so "geopolitics" matches before "politics".
    """
    cat_lower = category.lower().strip()
    # Sort by key length descending to avoid prefix conflicts
    for key in sorted(CATEGORY_FEE_RATES, key=len, reverse=True):
        if key in cat_lower:
            return key
    return None


def get_fee_rate(category: str) -> float:
    """Look up the base taker fee rate for a category."""
    key = _match_category(category)
    if key is not None:
        return CATEGORY_FEE_RATES[key]["taker_rate"]
    return DEFAULT_TAKER_RATE


def get_maker_rebate_pct(category: str) -> float:
    """Look up the maker rebate percentage for a category."""
    key = _match_category(category)
    if key is not None:
        return CATEGORY_FEE_RATES[key]["maker_rebate_pct"]
    return 0.25


def compute_taker_fee_per_share(price: float, fee_rate: float) -> float:
    """Polymarket taker fee per share: feeRate * price * (1 - price)."""
    return fee_rate * price * (1.0 - price)


def compute_taker_fee_per_dollar(price: float, fee_rate: float) -> float:
    """Fee drag per dollar spent buying shares at `price`.

    fee_per_share = feeRate * price * (1 - price)
    cost_per_share = price
    fee_per_dollar = fee_per_share / price = feeRate * (1 - price)
    """
    return fee_rate * (1.0 - price)


def compute_maker_fee() -> float:
    """Makers pay 0% on Polymarket. Always."""
    return 0.0


def compute_maker_rebate(taker_fee: float, category: str) -> float:
    """Estimate maker rebate earned when a taker fills our resting order."""
    rebate_pct = get_maker_rebate_pct(category)
    return taker_fee * rebate_pct
