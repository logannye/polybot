"""Win-probability threshold resolution with hardcoded live-safety floor.

Quant-safety principle: the live entry threshold CANNOT be weakened via
config. Any attempt to set ``lg_min_win_prob`` below LIVE_WP_FLOOR is
raised to the floor at read time with a critical log. To actually loosen
the live gate, the code must be changed and reviewed.

Dry-run has a separate threshold (``lg_min_win_prob_dryrun``) that is
permitted to be looser for data collection. A lower-bound floor of
DRYRUN_WP_FLOOR prevents a fat-finger from disabling the gate entirely.
"""
from __future__ import annotations

import structlog

log = structlog.get_logger()

# Hardcoded live-safety floor. Cannot be bypassed by config — to change
# this, edit the constant in code and ship a PR.
LIVE_WP_FLOOR: float = 0.80

# Minimum dry-run threshold. Lower than this is treated as a misconfig.
DRYRUN_WP_FLOOR: float = 0.55


def get_active_wp_threshold(settings) -> float:
    """Resolve the active WP threshold based on dry_run mode + config.

    Live mode enforces ``LIVE_WP_FLOOR`` regardless of config value.
    Dry-run mode enforces ``DRYRUN_WP_FLOOR`` as a sanity floor.
    """
    live_threshold = float(getattr(settings, "lg_min_win_prob", 0.85))
    if live_threshold < LIVE_WP_FLOOR:
        log.critical(
            "wp_threshold_below_live_floor",
            configured=live_threshold, enforced=LIVE_WP_FLOOR,
            msg="refusing to weaken live WP gate via config")
        live_threshold = LIVE_WP_FLOOR

    if not bool(getattr(settings, "dry_run", True)):
        return live_threshold

    dryrun_threshold = float(getattr(settings, "lg_min_win_prob_dryrun",
                                       live_threshold))
    if dryrun_threshold < DRYRUN_WP_FLOOR:
        log.warning("dryrun_wp_threshold_below_floor",
                    configured=dryrun_threshold, enforced=DRYRUN_WP_FLOOR)
        dryrun_threshold = DRYRUN_WP_FLOOR
    return dryrun_threshold


def passes_live_threshold(calibrated_wp: float, settings) -> bool:
    """Return True if this WP would have entered under the LIVE threshold.

    Used to tag dry-run observations so analysis can filter to the
    live-projection subset even when running under a looser dry-run gate.
    """
    live_threshold = float(getattr(settings, "lg_min_win_prob", 0.85))
    live_threshold = max(LIVE_WP_FLOOR, live_threshold)
    return calibrated_wp >= live_threshold
