import pytest
from polybot.strategies.snipe import classify_snipe_tier, compute_snipe_edge


# --- Tier 0: Very extreme prices, close to resolution (<=24h) ---

def test_tier0_high_price():
    assert classify_snipe_tier(price=0.95, hours_remaining=3.0) == 0


def test_tier0_high_price_24h():
    assert classify_snipe_tier(price=0.96, hours_remaining=23.0) == 0


def test_tier0_no_side():
    assert classify_snipe_tier(price=0.05, hours_remaining=2.0) == 0


def test_tier0_no_side_boundary():
    assert classify_snipe_tier(price=0.04, hours_remaining=20.0) == 0


# --- Tier 1: Extreme prices, moderate time (<=12h) ---

def test_tier1_medium_price():
    assert classify_snipe_tier(price=0.85, hours_remaining=2.0) == 1


def test_tier1_no_side():
    assert classify_snipe_tier(price=0.15, hours_remaining=10.0) == 1


def test_tier1_boundary():
    """0.85 at 12h should be tier 1."""
    assert classify_snipe_tier(price=0.85, hours_remaining=12.0) == 1


# --- Tier 2: Moderate lean, wider window (<=72h) ---

def test_tier2_high_price():
    assert classify_snipe_tier(price=0.90, hours_remaining=36.0) == 2


def test_tier2_low_price():
    assert classify_snipe_tier(price=0.15, hours_remaining=40.0) == 2


def test_tier2_boundary():
    assert classify_snipe_tier(price=0.92, hours_remaining=47.0) == 2


def test_tier2_relaxed_85_at_30h():
    """0.85 at 30h: now falls into relaxed Tier 2 (>= 0.85, <= 72h)."""
    assert classify_snipe_tier(price=0.85, hours_remaining=30.0) == 2


def test_tier2_relaxed_85_at_60h():
    """0.85 at 60h: within relaxed Tier 2 window."""
    assert classify_snipe_tier(price=0.85, hours_remaining=60.0) == 2


def test_tier2_relaxed_72h():
    """0.90 at 60h: within relaxed Tier 2 window."""
    assert classify_snipe_tier(price=0.90, hours_remaining=60.0) == 2


def test_tier2_extreme_at_50h():
    """0.95 at 50h: outside Tier 0 (>24h) but inside Tier 2 (<=72h)."""
    assert classify_snipe_tier(price=0.95, hours_remaining=50.0) == 2


# --- Not a snipe candidate ---

def test_no_snipe_low_price():
    assert classify_snipe_tier(price=0.70, hours_remaining=2.0) is None


def test_tier3_extreme_at_80h():
    """0.95 at 80h: beyond Tier 2 (72h) but within Tier 3 (120h)."""
    assert classify_snipe_tier(price=0.95, hours_remaining=80.0) == 3


def test_tier2_widened_080_at_30h():
    """0.80 at 30h: within widened Tier 2 (>= 0.80, <= 72h)."""
    assert classify_snipe_tier(price=0.80, hours_remaining=30.0) == 2


def test_no_snipe_too_far():
    """0.95 at 130h: beyond Tier 3 window (120h)."""
    assert classify_snipe_tier(price=0.95, hours_remaining=130.0) is None


def test_no_snipe_low_price_below_tier3():
    """0.70 at 50h: below Tier 3 threshold (0.75), not a snipe candidate."""
    assert classify_snipe_tier(price=0.70, hours_remaining=50.0) is None


def test_tier3_moderate_lean():
    """0.75 at 100h: within Tier 3 (>= 0.75, <= 120h)."""
    assert classify_snipe_tier(price=0.75, hours_remaining=100.0) == 3


def test_tier3_no_side():
    """0.25 at 90h: NO side within Tier 3 (<= 0.25, <= 120h)."""
    assert classify_snipe_tier(price=0.25, hours_remaining=90.0) == 3


def test_tier2_widened_no_side():
    """0.20 at 50h: within widened Tier 2 (<= 0.20, <= 72h)."""
    assert classify_snipe_tier(price=0.20, hours_remaining=50.0) == 2


def test_no_snipe_zero_hours():
    assert classify_snipe_tier(price=0.95, hours_remaining=0) is None


def test_no_snipe_negative_hours():
    assert classify_snipe_tier(price=0.95, hours_remaining=-1.0) is None


def test_snipe_edge_maker_zero_fee():
    """Maker orders: full edge, no fee deduction."""
    edge = compute_snipe_edge(buy_price=0.95, fee_per_dollar=0.0)
    assert abs(edge - 0.05) < 1e-9


def test_snipe_edge_taker_fee():
    """Taker at p=0.95 with finance rate: fee_per_dollar = 0.04 * 0.05 = 0.002."""
    from polybot.trading.fees import compute_taker_fee_per_dollar
    fee_pd = compute_taker_fee_per_dollar(0.95, 0.04)
    edge = compute_snipe_edge(buy_price=0.95, fee_per_dollar=fee_pd)
    assert abs(edge - 0.048) < 1e-4


def test_snipe_edge_negative():
    """Even with zero fee, p=0.99 leaves only 1 cent of edge."""
    edge = compute_snipe_edge(buy_price=0.995, fee_per_dollar=0.01)
    assert edge < 0


from polybot.strategies.snipe import compute_tiered_kelly_scale


def test_tiered_kelly_base_edge():
    """Edge 2-3% gets no boost (1.0x)."""
    assert compute_tiered_kelly_scale(0.025) == 1.0


def test_tiered_kelly_mid_edge():
    """Edge 3-5% gets 1.5x boost."""
    assert compute_tiered_kelly_scale(0.04) == 1.5


def test_tiered_kelly_high_edge():
    """Edge 5%+ gets 2.0x boost."""
    assert compute_tiered_kelly_scale(0.06) == 2.0


def test_tiered_kelly_boundary_3pct():
    """Exactly 3% gets the 1.5x boost."""
    assert compute_tiered_kelly_scale(0.03) == 1.5


def test_tiered_kelly_boundary_5pct():
    """Exactly 5% gets the 2.0x boost."""
    assert compute_tiered_kelly_scale(0.05) == 2.0


def test_tiered_kelly_below_min():
    """Edge below 2% still gets 1.0x (no penalty)."""
    assert compute_tiered_kelly_scale(0.01) == 1.0


from datetime import datetime, timezone, timedelta
from polybot.strategies.snipe import check_snipe_cooldown


def test_cooldown_blocks_recent_exit():
    """Market exited 1 hour ago with 4-hour cooldown should be blocked."""
    now = datetime.now(timezone.utc)
    cooldowns = {
        "mkt-1": {"exit_time": now - timedelta(hours=1), "exit_price": 0.95},
    }
    result = check_snipe_cooldown(
        "mkt-1", current_price=0.95, cooldowns=cooldowns,
        cooldown_hours=4.0, reentry_threshold=0.03)
    assert result is False


def test_cooldown_allows_after_expiry():
    """Market exited 5 hours ago with 4-hour cooldown should be allowed."""
    now = datetime.now(timezone.utc)
    cooldowns = {
        "mkt-1": {"exit_time": now - timedelta(hours=5), "exit_price": 0.95},
    }
    result = check_snipe_cooldown(
        "mkt-1", current_price=0.95, cooldowns=cooldowns,
        cooldown_hours=4.0, reentry_threshold=0.03)
    assert result is True


def test_cooldown_allows_unknown_market():
    """Market not in cooldowns should be allowed."""
    result = check_snipe_cooldown(
        "mkt-new", current_price=0.95, cooldowns={},
        cooldown_hours=4.0, reentry_threshold=0.03)
    assert result is True


def test_cooldown_allows_reentry_on_price_move():
    """Market in cooldown but price moved 4% should be allowed (re-entry)."""
    now = datetime.now(timezone.utc)
    cooldowns = {
        "mkt-1": {"exit_time": now - timedelta(hours=1), "exit_price": 0.92},
    }
    result = check_snipe_cooldown(
        "mkt-1", current_price=0.96, cooldowns=cooldowns,
        cooldown_hours=4.0, reentry_threshold=0.03)
    assert result is True


def test_cooldown_blocks_small_price_move():
    """Market in cooldown with only 1% price move should still be blocked."""
    now = datetime.now(timezone.utc)
    cooldowns = {
        "mkt-1": {"exit_time": now - timedelta(hours=1), "exit_price": 0.95},
    }
    result = check_snipe_cooldown(
        "mkt-1", current_price=0.96, cooldowns=cooldowns,
        cooldown_hours=4.0, reentry_threshold=0.03)
    assert result is False


from unittest.mock import AsyncMock, MagicMock
from polybot.strategies.snipe import verify_snipe_via_odds


@pytest.mark.asyncio
async def test_odds_verify_confirms_yes_snipe():
    """Sportsbook consensus > 85% for YES snipe should verify."""
    odds_client = MagicMock()
    odds_client.credits_exhausted = False
    odds_client.fetch_odds = AsyncMock(return_value=[{
        "id": "evt1", "sport_key": "basketball_nba",
        "home_team": "Thunder", "away_team": "Jazz",
        "bookmakers": [
            {"key": "fanduel", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Thunder", "price": -1000},
                {"name": "Jazz", "price": 600},
            ]}]},
            {"key": "draftkings", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Thunder", "price": -900},
                {"name": "Jazz", "price": 550},
            ]}]},
        ],
    }])

    result = await verify_snipe_via_odds(
        odds_client=odds_client,
        question="Will the Oklahoma City Thunder win on 2026-04-05?",
        side="YES",
        min_consensus=0.85,
    )
    assert result is True


@pytest.mark.asyncio
async def test_odds_verify_rejects_weak_consensus():
    """Sportsbook consensus 60% for YES snipe should NOT verify."""
    odds_client = MagicMock()
    odds_client.credits_exhausted = False
    odds_client.fetch_odds = AsyncMock(return_value=[{
        "id": "evt2", "sport_key": "basketball_nba",
        "home_team": "Lakers", "away_team": "Mavericks",
        "bookmakers": [
            {"key": "fanduel", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Lakers", "price": -150},
                {"name": "Mavericks", "price": 125},
            ]}]},
        ],
    }])

    result = await verify_snipe_via_odds(
        odds_client=odds_client,
        question="Will the Los Angeles Lakers win on 2026-04-05?",
        side="YES",
        min_consensus=0.85,
    )
    assert result is False


@pytest.mark.asyncio
async def test_odds_verify_returns_false_no_data():
    """No odds data should return False (fall back to LLM)."""
    odds_client = MagicMock()
    odds_client.credits_exhausted = False
    odds_client.fetch_odds = AsyncMock(return_value=[])

    result = await verify_snipe_via_odds(
        odds_client=odds_client,
        question="Will something happen?",
        side="YES",
        min_consensus=0.85,
    )
    assert result is False


@pytest.mark.asyncio
async def test_odds_verify_short_circuits_when_credits_exhausted():
    """verify_snipe_via_odds should return False immediately when credits are exhausted."""
    from polybot.strategies.snipe import verify_snipe_via_odds
    from unittest.mock import AsyncMock, MagicMock, PropertyMock

    odds_client = MagicMock()
    type(odds_client).credits_exhausted = PropertyMock(return_value=True)
    odds_client.fetch_odds = AsyncMock()

    result = await verify_snipe_via_odds(
        odds_client=odds_client,
        question="Will the Los Angeles Lakers win?",
        side="YES",
        min_consensus=0.85,
    )

    assert result is False
    odds_client.fetch_odds.assert_not_called()


@pytest.mark.asyncio
async def test_odds_verify_no_side_snipe():
    """NO-side snipe: consensus < 15% (i.e., NO is > 85%) should verify."""
    odds_client = MagicMock()
    odds_client.credits_exhausted = False
    odds_client.fetch_odds = AsyncMock(return_value=[{
        "id": "evt3", "sport_key": "basketball_nba",
        "home_team": "Thunder", "away_team": "Jazz",
        "bookmakers": [
            {"key": "fanduel", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Jazz", "price": 3000},
                {"name": "Thunder", "price": -10000},
            ]}]},
        ],
    }])

    result = await verify_snipe_via_odds(
        odds_client=odds_client,
        question="Will the Utah Jazz win on 2026-04-05?",
        side="NO",
        min_consensus=0.85,
    )
    assert result is True
