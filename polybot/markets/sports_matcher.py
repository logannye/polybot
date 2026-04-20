"""Sports market matcher — the highest-risk component per v10 spec §3.

Maps ESPN live games onto Polymarket markets via a 3-pass pipeline:
1. Exact team-name normalization (per-league dictionary)
2. Market-type classification (regex on title)
3. Confidence score (name match + slug + resolution-time proximity)

Trade only when confidence ≥ 0.95. Below that, return None so the strategy
skips rather than trades the wrong market. Exhaustive tests in
tests/test_sports_matcher.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

MarketType = Literal["moneyline", "spread", "total"]


@dataclass(frozen=True)
class LiveGame:
    sport: str                           # normalized: nba / nhl / mlb / ncaab / ucl / epl / ...
    home_team: str                       # canonical name (post-normalization)
    away_team: str
    game_id: str                         # ESPN event id
    start_time: datetime                 # UTC
    score_home: int
    score_away: int
    status: str                          # "in_progress" | "final" | "scheduled"


@dataclass(frozen=True)
class PolymarketMarket:
    polymarket_id: str
    question: str
    slug: str                            # polymarket market slug
    resolution_time: datetime            # UTC


@dataclass(frozen=True)
class MatchResult:
    market: PolymarketMarket
    live_game: LiveGame
    market_type: MarketType
    side: Literal["home", "away", "over", "under"]   # which side the market pays
    confidence: float                    # 0.0–1.0
    line: Optional[float] = None         # spread or total line, if applicable


# -------------------------------------------------------------------------
# Pass 1 — team name normalization
# -------------------------------------------------------------------------
# Each mapping: normalized_canonical_name -> set of all known variants (lower).
# Applied bidirectionally: ESPN name → canonical, Polymarket text → canonical.
# Only teams we actually trade on need entries; others fall through to None
# (the match rejects via confidence floor).

NBA_ALIASES: dict[str, frozenset[str]] = {
    "thunder": frozenset({"oklahoma city thunder", "okc thunder", "okc", "thunder"}),
    "lakers": frozenset({"los angeles lakers", "la lakers", "lakers"}),
    "warriors": frozenset({"golden state warriors", "gsw", "warriors"}),
    "celtics": frozenset({"boston celtics", "celtics"}),
    "nuggets": frozenset({"denver nuggets", "nuggets"}),
    "bucks": frozenset({"milwaukee bucks", "bucks"}),
    "heat": frozenset({"miami heat", "heat"}),
    "suns": frozenset({"phoenix suns", "suns"}),
    "sixers": frozenset({"philadelphia 76ers", "76ers", "sixers"}),
    "mavericks": frozenset({"dallas mavericks", "mavs", "mavericks"}),
    "knicks": frozenset({"new york knicks", "ny knicks", "knicks"}),
    "nets": frozenset({"brooklyn nets", "bkn nets", "nets"}),
    "clippers": frozenset({"los angeles clippers", "la clippers", "clippers"}),
    "cavaliers": frozenset({"cleveland cavaliers", "cavs", "cavaliers"}),
    "timberwolves": frozenset({"minnesota timberwolves", "wolves", "timberwolves"}),
    "hawks": frozenset({"atlanta hawks", "hawks"}),
    "magic": frozenset({"orlando magic", "magic"}),
    "pacers": frozenset({"indiana pacers", "pacers"}),
    "grizzlies": frozenset({"memphis grizzlies", "grizzlies"}),
    "pelicans": frozenset({"new orleans pelicans", "pelicans"}),
    "kings": frozenset({"sacramento kings", "kings"}),
    "rockets": frozenset({"houston rockets", "rockets"}),
    "jazz": frozenset({"utah jazz", "jazz"}),
    "spurs": frozenset({"san antonio spurs", "spurs"}),
    "trail_blazers": frozenset({"portland trail blazers", "blazers", "trail blazers"}),
    "raptors": frozenset({"toronto raptors", "raptors"}),
    "wizards": frozenset({"washington wizards", "wizards"}),
    "pistons": frozenset({"detroit pistons", "pistons"}),
    "hornets": frozenset({"charlotte hornets", "hornets"}),
    "bulls": frozenset({"chicago bulls", "bulls"}),
}

NHL_ALIASES: dict[str, frozenset[str]] = {
    "oilers": frozenset({"edmonton oilers", "oilers"}),
    "avalanche": frozenset({"colorado avalanche", "avs", "avalanche"}),
    "rangers": frozenset({"new york rangers", "ny rangers", "rangers"}),
    "bruins": frozenset({"boston bruins", "bruins"}),
    "leafs": frozenset({"toronto maple leafs", "maple leafs", "leafs"}),
    "panthers": frozenset({"florida panthers", "panthers"}),
    "lightning": frozenset({"tampa bay lightning", "lightning"}),
    "canucks": frozenset({"vancouver canucks", "canucks"}),
    "knights": frozenset({"vegas golden knights", "golden knights", "knights"}),
    "hurricanes": frozenset({"carolina hurricanes", "canes", "hurricanes"}),
    "stars": frozenset({"dallas stars", "stars"}),
    "devils": frozenset({"new jersey devils", "nj devils", "devils"}),
    "flyers": frozenset({"philadelphia flyers", "flyers"}),
    "ducks": frozenset({"anaheim ducks", "ducks"}),
    "canadiens": frozenset({"montreal canadiens", "habs", "canadiens"}),
    "kings_nhl": frozenset({"los angeles kings", "la kings"}),
    "jets": frozenset({"winnipeg jets", "jets"}),
    "senators": frozenset({"ottawa senators", "sens", "senators"}),
    "predators": frozenset({"nashville predators", "preds", "predators"}),
    "wild": frozenset({"minnesota wild", "wild"}),
    "islanders": frozenset({"new york islanders", "ny islanders", "islanders"}),
    "penguins": frozenset({"pittsburgh penguins", "pens", "penguins"}),
    "red_wings": frozenset({"detroit red wings", "red wings"}),
    "flames": frozenset({"calgary flames", "flames"}),
    "sabres": frozenset({"buffalo sabres", "sabres"}),
    "sharks": frozenset({"san jose sharks", "sharks"}),
    "blackhawks": frozenset({"chicago blackhawks", "hawks_nhl", "blackhawks"}),
    "kraken": frozenset({"seattle kraken", "kraken"}),
    "blues": frozenset({"st. louis blues", "st louis blues", "blues"}),
    "coyotes": frozenset({"arizona coyotes", "coyotes", "utah hockey club"}),
    "columbus": frozenset({"columbus blue jackets", "cbj", "blue jackets"}),
    "capitals": frozenset({"washington capitals", "caps", "capitals"}),
}

MLB_ALIASES: dict[str, frozenset[str]] = {
    "dodgers": frozenset({"los angeles dodgers", "la dodgers", "dodgers"}),
    "yankees": frozenset({"new york yankees", "ny yankees", "yankees"}),
    "mets": frozenset({"new york mets", "ny mets", "mets"}),
    "red_sox": frozenset({"boston red sox", "red sox", "bosox"}),
    "braves": frozenset({"atlanta braves", "braves"}),
    "astros": frozenset({"houston astros", "astros"}),
    "phillies": frozenset({"philadelphia phillies", "phillies"}),
    "padres": frozenset({"san diego padres", "padres"}),
    "cubs": frozenset({"chicago cubs", "cubs"}),
    "guardians": frozenset({"cleveland guardians", "guardians", "indians"}),
    "rangers_mlb": frozenset({"texas rangers"}),
    "orioles": frozenset({"baltimore orioles", "orioles", "o's"}),
    "brewers": frozenset({"milwaukee brewers", "brewers"}),
    "rays": frozenset({"tampa bay rays", "rays"}),
    "tigers": frozenset({"detroit tigers", "tigers"}),
    "giants": frozenset({"san francisco giants", "sf giants", "giants"}),
    "cardinals": frozenset({"st. louis cardinals", "st louis cardinals", "cards", "cardinals"}),
    "mariners": frozenset({"seattle mariners", "mariners"}),
    "blue_jays": frozenset({"toronto blue jays", "blue jays", "jays"}),
    "royals": frozenset({"kansas city royals", "kc royals", "royals"}),
    "white_sox": frozenset({"chicago white sox", "white sox"}),
    "twins": frozenset({"minnesota twins", "twins"}),
    "angels": frozenset({"los angeles angels", "la angels", "angels"}),
    "athletics": frozenset({"oakland athletics", "a's", "athletics"}),
    "pirates": frozenset({"pittsburgh pirates", "pirates", "bucs"}),
    "rockies": frozenset({"colorado rockies", "rockies"}),
    "marlins": frozenset({"miami marlins", "marlins"}),
    "reds": frozenset({"cincinnati reds", "reds"}),
    "nationals": frozenset({"washington nationals", "nats", "nationals"}),
    "diamondbacks": frozenset({"arizona diamondbacks", "d-backs", "dbacks", "diamondbacks"}),
}

# Consolidated per-league lookup: lowercase variant -> canonical key
def _build_reverse_lookup(alias_map: dict[str, frozenset[str]]) -> dict[str, str]:
    return {variant: canonical
            for canonical, variants in alias_map.items()
            for variant in variants}

NBA_LOOKUP = _build_reverse_lookup(NBA_ALIASES)
NHL_LOOKUP = _build_reverse_lookup(NHL_ALIASES)
MLB_LOOKUP = _build_reverse_lookup(MLB_ALIASES)

SPORT_LOOKUPS: dict[str, dict[str, str]] = {
    "nba": NBA_LOOKUP,
    "ncaab": {},   # intentionally empty — NCAAB matcher uses full text
    "nhl": NHL_LOOKUP,
    "mlb": MLB_LOOKUP,
    "ucl": {},     # soccer uses full club names
    "epl": {},
    "laliga": {},
    "bundesliga": {},
    "mls": {},
}


def normalize_team_name(raw: str, sport: str) -> Optional[str]:
    """Return canonical team key, or None if no match.

    Tries exact match first, then word-by-word search in the alias table.
    """
    if not raw:
        return None
    lookup = SPORT_LOOKUPS.get(sport)
    if not lookup:
        # No lookup table — return a cleaned version of the raw name
        return _clean(raw)
    lowered = raw.lower().strip()
    if lowered in lookup:
        return lookup[lowered]
    # Try containing phrase
    for variant, canonical in lookup.items():
        if variant in lowered:
            return canonical
    return None


def _clean(s: str) -> str:
    """Lowercase, strip, collapse whitespace, drop common punctuation."""
    return re.sub(r"\s+", " ", re.sub(r"[.,'’!?]", "", s.lower())).strip()


# -------------------------------------------------------------------------
# Pass 2 — market type classification
# -------------------------------------------------------------------------
_SPREAD_RE = re.compile(
    r"spread\s*:?\s*([A-Za-z .'-]+?)\s*\(([-+]?\d+(?:\.\d+)?)\)", re.IGNORECASE)
_TOTAL_RE = re.compile(
    r"(?:total|o/u|over/under).*?(\d+(?:\.\d+)?)", re.IGNORECASE)
_MONEYLINE_RE = re.compile(
    r"(?:will\s+(?:the\s+)?([A-Za-z .'-]+?)\s+(?:beat|win\s+against|defeat))"
    r"|(?:([A-Za-z .'-]+?)\s+(?:vs\.?|at|@)\s+([A-Za-z .'-]+?))",
    re.IGNORECASE)


def classify_market_type(question: str) -> Optional[tuple[MarketType, Optional[float]]]:
    """Return (market_type, line_or_None), or None if unclassifiable."""
    q = question or ""
    m_spread = _SPREAD_RE.search(q)
    if m_spread:
        try:
            return "spread", float(m_spread.group(2))
        except ValueError:
            return "spread", None
    m_total = _TOTAL_RE.search(q)
    if m_total:
        try:
            return "total", float(m_total.group(1))
        except ValueError:
            return "total", None
    m_ml = _MONEYLINE_RE.search(q)
    if m_ml:
        return "moneyline", None
    return None


# -------------------------------------------------------------------------
# Pass 3 — confidence score + final match
# -------------------------------------------------------------------------
def _slug_score(game: LiveGame, market: PolymarketMarket) -> float:
    """0–1 score based on whether the market slug contains the teams."""
    slug = (market.slug or "").lower()
    home_canonical = (game.home_team or "").lower()
    away_canonical = (game.away_team or "").lower()
    score = 0.0
    if home_canonical and home_canonical in slug:
        score += 0.5
    if away_canonical and away_canonical in slug:
        score += 0.5
    return score


def _time_proximity_score(game: LiveGame, market: PolymarketMarket,
                          window_hours: float = 12.0) -> float:
    """1.0 if market resolution is within ``window_hours`` of game start, else 0."""
    if not game.start_time or not market.resolution_time:
        return 0.0
    delta = abs((market.resolution_time - game.start_time).total_seconds()) / 3600.0
    if delta <= window_hours:
        return 1.0 - (delta / window_hours) * 0.5   # 1.0 at 0h, 0.5 at 12h
    return 0.0


def _team_name_score(game: LiveGame, market: PolymarketMarket) -> float:
    """Score based on whether both teams appear in the market question."""
    question_lower = (market.question or "").lower()
    home = (game.home_team or "").lower()
    away = (game.away_team or "").lower()
    score = 0.0
    if home and home in question_lower:
        score += 0.5
    if away and away in question_lower:
        score += 0.5
    return score


def _determine_side(game: LiveGame, market: PolymarketMarket,
                    market_type: MarketType) -> Optional[Literal["home", "away", "over", "under"]]:
    """Figure out which side this market is asking about."""
    q = (market.question or "").lower()
    home = (game.home_team or "").lower()
    away = (game.away_team or "").lower()
    if market_type == "total":
        if "over" in q:
            return "over"
        if "under" in q:
            return "under"
        return None
    # moneyline / spread — which team is the market about?
    # Take the first team mentioned in the question
    home_idx = q.find(home) if home else -1
    away_idx = q.find(away) if away else -1
    if home_idx == -1 and away_idx == -1:
        return None
    if away_idx == -1:
        return "home"
    if home_idx == -1:
        return "away"
    return "home" if home_idx < away_idx else "away"


def match_game_to_market(game: LiveGame, market: PolymarketMarket,
                          min_confidence: float = 0.95) -> Optional[MatchResult]:
    """Return a MatchResult only when confidence ≥ min_confidence, else None.

    This is the gatekeeper — it MUST refuse matches below the confidence
    floor rather than return a best-effort guess. Wrong-market trades are
    the failure mode that v10 spec §3 calls out as highest risk.
    """
    classification = classify_market_type(market.question)
    if not classification:
        return None
    market_type, line = classification

    name_score = _team_name_score(game, market)
    slug_score = _slug_score(game, market)
    time_score = _time_proximity_score(game, market)

    # Confidence is a weighted average. Team-name match dominates because
    # a market on the wrong game is useless even if the slug and time match.
    confidence = 0.55 * name_score + 0.25 * slug_score + 0.20 * time_score

    if confidence < min_confidence:
        return None

    side = _determine_side(game, market, market_type)
    if side is None:
        return None

    return MatchResult(
        market=market,
        live_game=game,
        market_type=market_type,
        side=side,
        confidence=confidence,
        line=line,
    )
