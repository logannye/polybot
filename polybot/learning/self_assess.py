import structlog

log = structlog.get_logger()
KELLY_MIN = 0.15
KELLY_MAX = 0.35
KELLY_STEP = 0.02
EDGE_STEP = 0.01
EDGE_MIN = 0.03
EDGE_MAX = 0.15

def suggest_kelly_adjustment(current_mult, max_drawdown_pct, drawdown_tolerance=0.30):
    if max_drawdown_pct > drawdown_tolerance:
        new_mult = current_mult - KELLY_STEP
    elif max_drawdown_pct < drawdown_tolerance * 0.5:
        new_mult = current_mult + KELLY_STEP
    else:
        return current_mult
    new_mult = max(KELLY_MIN, min(KELLY_MAX, new_mult))
    log.info("kelly_adjustment", old=current_mult, new=new_mult, drawdown=max_drawdown_pct)
    return new_mult

def suggest_edge_threshold(current_threshold, edge_buckets):
    if not edge_buckets:
        return current_threshold
    sorted_edges = sorted(edge_buckets.keys())
    if not sorted_edges:
        return current_threshold
    lowest = sorted_edges[0]
    bucket = edge_buckets[lowest]
    if bucket["count"] < 5:
        return current_threshold
    avg_pnl = bucket["total_pnl"] / bucket["count"]
    if avg_pnl < 0:
        new_threshold = current_threshold + EDGE_STEP
    elif avg_pnl > 0 and current_threshold > EDGE_MIN:
        new_threshold = current_threshold - EDGE_STEP
    else:
        return current_threshold
    new_threshold = max(EDGE_MIN, min(EDGE_MAX, new_threshold))
    log.info("edge_threshold_adjustment", old=current_threshold, new=new_threshold)
    return new_threshold

def check_strategy_kill_switch(total_trades: int, total_pnl: float, min_trades: int = 50) -> bool:
    """Returns True if strategy should be disabled (negative P&L over enough trades)."""
    if total_trades < min_trades:
        return False
    return total_pnl < 0
