"""Per-(strategy, category) edge-decay monitor — v10 spec §5 Loop 2.

Compare realized edge on the last 50 trades vs the last 200. When the
short-window mean is negative while the long-window mean is positive,
disable that ``(strategy, category)`` pair for 48h.

This catches regime changes before they drain the bankroll. The disable
is transient (48h); the daily kill-switch (§5 Loop 3) handles permanent
deactivation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


SHORT_WINDOW = 50
LONG_WINDOW = 200


@dataclass(frozen=True)
class DecayVerdict:
    should_disable: bool
    reason: Optional[str]
    short_mean: Optional[float]
    long_mean: Optional[float]
    short_n: int
    long_n: int


def _pnl_mean(rows: list[dict], window: int, pnl_key: str = "pnl") -> Optional[float]:
    if not rows:
        return None
    recent = rows[-window:]
    valid = [float(r[pnl_key]) for r in recent if r.get(pnl_key) is not None]
    if not valid:
        return None
    return sum(valid) / len(valid)


def evaluate_decay(
    outcomes: Iterable[dict], *,
    short_window: int = SHORT_WINDOW, long_window: int = LONG_WINDOW,
    pnl_key: str = "pnl", min_obs_for_disable: int = SHORT_WINDOW,
) -> DecayVerdict:
    """Sort outcomes chronologically, compute short/long means, and decide.

    Rows must be iterables of dicts with a ``pnl`` key (number-like).
    The caller supplies the strategy+category filter.
    """
    rows = [r for r in outcomes if r.get(pnl_key) is not None]
    rows.sort(key=lambda r: r.get("id") or 0)
    short = _pnl_mean(rows, short_window, pnl_key)
    long_ = _pnl_mean(rows, long_window, pnl_key)

    short_n = min(len(rows), short_window)
    long_n = min(len(rows), long_window)

    if short is None or long_ is None:
        return DecayVerdict(False, None, short, long_, short_n, long_n)

    if short_n < min_obs_for_disable:
        return DecayVerdict(False, None, short, long_, short_n, long_n)

    if short < 0 and long_ > 0:
        return DecayVerdict(
            True,
            f"short-window mean {short:.3f} < 0 while long-window mean {long_:.3f} > 0",
            short, long_, short_n, long_n,
        )

    return DecayVerdict(False, None, short, long_, short_n, long_n)
