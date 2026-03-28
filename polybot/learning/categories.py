from dataclasses import dataclass

@dataclass
class CategoryStats:
    total_trades: int
    total_pnl: float
    win_count: int

def compute_category_bias(stats: CategoryStats, min_trades: int = 20) -> float:
    if stats.total_trades < min_trades:
        return 0.0
    win_rate = stats.win_count / stats.total_trades
    avg_pnl = stats.total_pnl / stats.total_trades
    win_signal = (win_rate - 0.5) * 2
    pnl_signal = max(-1.0, min(1.0, avg_pnl / 5.0))
    return (win_signal + pnl_signal) / 2
