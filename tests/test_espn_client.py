"""Tests for ESPN scoreboard client — parse_espn_scoreboard()."""

import pytest
from polybot.sports.espn_client import parse_espn_scoreboard, SPORT_URLS


def _make_competitor(home_away: str, display_name: str, abbreviation: str, score: str) -> dict:
    return {
        "homeAway": home_away,
        "score": score,
        "team": {
            "displayName": display_name,
            "abbreviation": abbreviation,
        },
    }


def _make_event(
    espn_id: str,
    name: str,
    short_name: str,
    status_type_name: str,
    completed: bool,
    period: int,
    display_clock: str,
    home_name: str,
    home_abbrev: str,
    home_score: str,
    away_name: str,
    away_abbrev: str,
    away_score: str,
) -> dict:
    return {
        "id": espn_id,
        "name": name,
        "shortName": short_name,
        "status": {
            "period": period,
            "displayClock": display_clock,
            "type": {
                "name": status_type_name,
                "completed": completed,
            },
        },
        "competitions": [
            {
                "competitors": [
                    _make_competitor("home", home_name, home_abbrev, home_score),
                    _make_competitor("away", away_name, away_abbrev, away_score),
                ]
            }
        ],
    }


class TestParseEspnScoreboardNba:
    def test_parse_espn_scoreboard_nba(self):
        """NBA game in progress — all fields parsed correctly."""
        data = {
            "events": [
                _make_event(
                    espn_id="401585855",
                    name="Los Angeles Lakers at Golden State Warriors",
                    short_name="LAL @ GSW",
                    status_type_name="STATUS_IN_PROGRESS",
                    completed=False,
                    period=3,
                    display_clock="3:42",
                    home_name="Golden State Warriors",
                    home_abbrev="GSW",
                    home_score="88",
                    away_name="Los Angeles Lakers",
                    away_abbrev="LAL",
                    away_score="79",
                )
            ]
        }

        games = parse_espn_scoreboard(data, "nba")

        assert len(games) == 1
        g = games[0]
        assert g["espn_id"] == "401585855"
        assert g["sport"] == "nba"
        assert g["name"] == "Los Angeles Lakers at Golden State Warriors"
        assert g["short_name"] == "LAL @ GSW"
        assert g["home_team"] == "Golden State Warriors"
        assert g["away_team"] == "Los Angeles Lakers"
        assert g["home_abbrev"] == "GSW"
        assert g["away_abbrev"] == "LAL"
        assert g["home_score"] == 88
        assert g["away_score"] == 79
        assert g["period"] == 3
        assert g["clock"] == "3:42"
        assert g["status"] == "in_progress"
        assert g["completed"] is False


class TestParseEspnScoreboardSkipsScheduled:
    def test_parse_espn_scoreboard_skips_scheduled(self):
        """Scheduled games (STATUS_SCHEDULED) should be excluded from results."""
        data = {
            "events": [
                _make_event(
                    espn_id="401585856",
                    name="Chicago Bulls at Boston Celtics",
                    short_name="CHI @ BOS",
                    status_type_name="STATUS_SCHEDULED",
                    completed=False,
                    period=0,
                    display_clock="7:30 PM",
                    home_name="Boston Celtics",
                    home_abbrev="BOS",
                    home_score="0",
                    away_name="Chicago Bulls",
                    away_abbrev="CHI",
                    away_score="0",
                )
            ]
        }

        games = parse_espn_scoreboard(data, "nba")

        assert games == []

    def test_parse_espn_scoreboard_mixed_skips_only_scheduled(self):
        """Only scheduled game is skipped when mixed with an in-progress game."""
        data = {
            "events": [
                _make_event(
                    espn_id="401585857",
                    name="Miami Heat at Milwaukee Bucks",
                    short_name="MIA @ MIL",
                    status_type_name="STATUS_IN_PROGRESS",
                    completed=False,
                    period=2,
                    display_clock="5:00",
                    home_name="Milwaukee Bucks",
                    home_abbrev="MIL",
                    home_score="55",
                    away_name="Miami Heat",
                    away_abbrev="MIA",
                    away_score="50",
                ),
                _make_event(
                    espn_id="401585858",
                    name="Phoenix Suns at Denver Nuggets",
                    short_name="PHX @ DEN",
                    status_type_name="STATUS_SCHEDULED",
                    completed=False,
                    period=0,
                    display_clock="9:00 PM",
                    home_name="Denver Nuggets",
                    home_abbrev="DEN",
                    home_score="0",
                    away_name="Phoenix Suns",
                    away_abbrev="PHX",
                    away_score="0",
                ),
            ]
        }

        games = parse_espn_scoreboard(data, "nba")

        assert len(games) == 1
        assert games[0]["espn_id"] == "401585857"


class TestParseEspnScoreboardIncludesFinal:
    def test_parse_espn_scoreboard_includes_final(self):
        """Final games (STATUS_FINAL) should be included with completed=True."""
        data = {
            "events": [
                _make_event(
                    espn_id="401585859",
                    name="New York Knicks at Philadelphia 76ers",
                    short_name="NYK @ PHI",
                    status_type_name="STATUS_FINAL",
                    completed=True,
                    period=4,
                    display_clock="0:00",
                    home_name="Philadelphia 76ers",
                    home_abbrev="PHI",
                    home_score="108",
                    away_name="New York Knicks",
                    away_abbrev="NYK",
                    away_score="112",
                )
            ]
        }

        games = parse_espn_scoreboard(data, "nba")

        assert len(games) == 1
        g = games[0]
        assert g["espn_id"] == "401585859"
        assert g["sport"] == "nba"
        assert g["home_team"] == "Philadelphia 76ers"
        assert g["away_team"] == "New York Knicks"
        assert g["home_score"] == 108
        assert g["away_score"] == 112
        assert g["status"] == "final"
        assert g["completed"] is True

    def test_parse_espn_scoreboard_halftime_is_in_progress(self):
        """STATUS_HALFTIME should be normalized to in_progress."""
        data = {
            "events": [
                _make_event(
                    espn_id="401585860",
                    name="Atlanta Hawks at Cleveland Cavaliers",
                    short_name="ATL @ CLE",
                    status_type_name="STATUS_HALFTIME",
                    completed=False,
                    period=2,
                    display_clock="0:00",
                    home_name="Cleveland Cavaliers",
                    home_abbrev="CLE",
                    home_score="62",
                    away_name="Atlanta Hawks",
                    away_abbrev="ATL",
                    away_score="58",
                )
            ]
        }

        games = parse_espn_scoreboard(data, "nba")

        assert len(games) == 1
        assert games[0]["status"] == "in_progress"
        assert games[0]["completed"] is False

    def test_parse_espn_scoreboard_empty_events(self):
        """Empty events list returns empty list without error."""
        data = {"events": []}
        games = parse_espn_scoreboard(data, "mlb")
        assert games == []

    def test_parse_espn_scoreboard_missing_events_key(self):
        """Missing events key returns empty list without error."""
        games = parse_espn_scoreboard({}, "nhl")
        assert games == []


class TestSportURLs:
    def test_original_sports_present(self):
        assert "mlb" in SPORT_URLS
        assert "nba" in SPORT_URLS
        assert "nhl" in SPORT_URLS

    def test_ncaab_present(self):
        assert "ncaab" in SPORT_URLS
        assert "college-basketball" in SPORT_URLS["ncaab"]

    def test_soccer_leagues_present(self):
        for league in ["ucl", "epl", "laliga", "bundesliga", "mls"]:
            assert league in SPORT_URLS, f"{league} missing from SPORT_URLS"
