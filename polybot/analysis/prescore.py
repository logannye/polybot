from polybot.analysis.quant import QuantSignals, compute_composite_score
from polybot.learning.categories import CategoryStats, compute_category_bias


def prescore(candidate, category_stats: dict[str, CategoryStats], quant: QuantSignals,
             quant_weights: dict[str, float] | None = None) -> float:
    """
    Pre-LLM scoring function that ranks markets without making LLM calls.

    Factors:
    - Price distance from 0.5 (midprice preferred)
    - Book depth (deeper is better for execution)
    - Category historical performance bias
    - Quantitative signals (positive helps, negative ignored)
    """
    score = 0.0

    # Prefer mid-price (0.5) — penalty for extreme prices
    score += (0.5 - abs(candidate.current_price - 0.5)) * 2.0

    # Deeper book is better for execution
    score += min(candidate.book_depth / 5000, 1.0) * 1.5

    # Category bias based on historical win rate and PnL
    cat_stats = category_stats.get(candidate.category)
    if cat_stats:
        cat_bias = compute_category_bias(cat_stats)
        score += cat_bias * 1.0

    # Quantitative signals — positive helps, negative is ignored
    if quant_weights:
        composite = compute_composite_score(quant, quant_weights)
    else:
        # Default equal weighting
        composite = (quant.line_movement + quant.volume_spike + quant.book_imbalance
                     + quant.spread + quant.time_decay) / 5.0

    score += max(composite, 0) * 1.5

    return score
