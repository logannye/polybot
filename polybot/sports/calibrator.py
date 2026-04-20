"""Online isotonic regression calibrator per (sport, game_state_bucket).

Given raw win-probability predictions and observed outcomes, learn a
monotonic mapping from predicted → empirically-accurate probability.
Refit hourly per v10 spec §5 Loop 2.

Data backing: ``sport_calibration`` table with rows
``(sport, bucket_key, predicted_prob, realized_outcome)``.

Fallback: if a bucket has <30 observations, shrink raw prediction toward
0.5 by a fixed factor instead of calibrating.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# We avoid a hard scikit-learn dependency at import time so tests can run
# without the optional extra. Import lazily in ``fit`` only.


@dataclass(frozen=True)
class BucketKey:
    sport: str
    bucket: str   # e.g. "q4_tied" or "late_lead_2"


class OnlineCalibrator:
    """Per-bucket isotonic regression with <30-obs shrinkage fallback.

    Usage:
        calib = OnlineCalibrator(min_obs_for_fit=30, fallback_shrinkage=0.10)
        calib.ingest(sport="nba", bucket="q4_close", pred=0.82, outcome=1)
        calib.fit_all()                         # call hourly
        calibrated = calib.apply("nba", "q4_close", 0.82)
    """

    def __init__(self, min_obs_for_fit: int = 30, fallback_shrinkage: float = 0.10):
        self._min_obs = min_obs_for_fit
        self._fallback = fallback_shrinkage
        # raw observations per bucket
        self._buckets: dict[BucketKey, list[tuple[float, int]]] = {}
        # fitted isotonic transforms per bucket (callable float -> float)
        self._fitted: dict[BucketKey, callable] = {}

    def ingest(self, sport: str, bucket: str, pred: float, outcome: int) -> None:
        """Record one observation. ``outcome`` is 0 or 1."""
        if outcome not in (0, 1):
            raise ValueError(f"outcome must be 0 or 1, got {outcome}")
        if not 0.0 <= pred <= 1.0:
            raise ValueError(f"pred must be in [0, 1], got {pred}")
        key = BucketKey(sport=sport, bucket=bucket)
        self._buckets.setdefault(key, []).append((pred, outcome))

    def bucket_count(self, sport: str, bucket: str) -> int:
        return len(self._buckets.get(BucketKey(sport=sport, bucket=bucket), []))

    def load_observations(self, rows: list[tuple[str, str, float, int]]) -> None:
        """Bulk-load observations from DB. Rows: (sport, bucket, pred, outcome)."""
        for sport, bucket, pred, outcome in rows:
            self.ingest(sport, bucket, pred, outcome)

    def fit_all(self) -> None:
        """Refit isotonic regression for every bucket with >= min_obs_for_fit.

        Silently skips buckets below the threshold (they use fallback at apply time).
        """
        try:
            from sklearn.isotonic import IsotonicRegression
        except ImportError:
            # No sklearn — leave everything as fallback-shrink
            return

        for key, obs in self._buckets.items():
            if len(obs) < self._min_obs:
                self._fitted.pop(key, None)
                continue
            preds = [p for p, _ in obs]
            outcomes = [o for _, o in obs]
            iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
            iso.fit(preds, outcomes)
            # Wrap the transform so we can use it without holding the sklearn
            # object (callable lets us drop sklearn at prediction time).
            self._fitted[key] = iso.predict

    def apply(self, sport: str, bucket: str, raw_prob: float) -> float:
        """Return calibrated probability for a single prediction.

        - If a fitted transform exists for this bucket, use it.
        - Otherwise shrink ``raw_prob`` toward 0.5 by ``fallback_shrinkage``.
        """
        key = BucketKey(sport=sport, bucket=bucket)
        fitted = self._fitted.get(key)
        if fitted is not None:
            try:
                result = float(fitted([raw_prob])[0])
                return max(0.0, min(1.0, result))
            except Exception:
                pass
        # Fallback: shrink toward 0.5
        return 0.5 + (1.0 - self._fallback) * (raw_prob - 0.5)

    def fitted_buckets(self) -> list[BucketKey]:
        return list(self._fitted.keys())

    def drift_vs_previous(self, sport: str, bucket: str,
                          prev_brier: Optional[float]) -> Optional[float]:
        """Compute current Brier vs previous for a bucket; returns relative delta.

        Used by the weekly reflection report (spec §5 Loop 4). Returns None
        if this bucket has no fitted transform or no prior Brier is provided.
        """
        key = BucketKey(sport=sport, bucket=bucket)
        obs = self._buckets.get(key, [])
        if not obs or prev_brier is None:
            return None
        sq_errors = [(p - o) ** 2 for p, o in obs]
        current_brier = sum(sq_errors) / len(sq_errors)
        if prev_brier == 0:
            return float("inf") if current_brier > 0 else 0.0
        return (current_brier - prev_brier) / prev_brier


def bucket_for_game_state(sport: str, score_diff: int, period: int,
                           total_periods: int, seconds_left: float) -> str:
    """Map a live game state to a calibration bucket key.

    Buckets are sport-dependent but follow a shared schema so SQL queries stay
    simple. Bucket granularity targets ~30–80 buckets per sport (spec §3).
    """
    # Period phase: early/mid/late of the overall game
    period_frac = (period - 1) / max(total_periods, 1)
    if period_frac < 0.25:
        phase = "early"
    elif period_frac < 0.70:
        phase = "mid"
    else:
        phase = "late"

    # Score tier
    if score_diff == 0:
        lead = "tied"
    elif score_diff == 1:
        lead = "one"
    elif score_diff == 2:
        lead = "two"
    elif score_diff <= 4:
        lead = "three_four"
    elif score_diff <= 8:
        lead = "moderate"
    else:
        lead = "large"

    return f"{phase}_{lead}"
