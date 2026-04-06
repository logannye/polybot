import pytest
from polybot.markets.scanner import parse_gamma_market, CATEGORY_TAGS

# Minimal valid base fields shared across tests
_BASE = {
    "conditionId": "0xtest",
    "question": "Will this happen?",
    "outcomes": '["Yes", "No"]',
    "outcomePrices": '["0.60", "0.40"]',
    "clobTokenIds": '["tok1", "tok2"]',
    "endDate": "2026-06-01T00:00:00Z",
    "volume24hr": 1000,
    "liquidityNum": 500,
    "active": True,
    "closed": False,
    "slug": "will-this-happen",
}


def _make_raw(**overrides):
    return {**_BASE, **overrides}


class TestTagExtraction:
    def test_extracts_tags_from_single_event(self):
        raw = _make_raw(events=[
            {"tags": [{"label": "Politics", "slug": "politics"}, {"label": "Trump", "slug": "trump"}]}
        ])
        result = parse_gamma_market(raw)
        assert result is not None
        assert "politics" in result["tags"]
        assert "trump" in result["tags"]

    def test_extracts_tags_across_multiple_events(self):
        raw = _make_raw(events=[
            {"tags": [{"label": "Crypto", "slug": "crypto"}]},
            {"tags": [{"label": "Finance", "slug": "finance"}, {"label": "Bitcoin", "slug": "bitcoin"}]},
        ])
        result = parse_gamma_market(raw)
        assert result is not None
        assert "crypto" in result["tags"]
        assert "finance" in result["tags"]
        assert "bitcoin" in result["tags"]

    def test_no_events_returns_empty_tags(self):
        raw = _make_raw()  # no "events" key
        result = parse_gamma_market(raw)
        assert result is not None
        assert result["tags"] == []

    def test_empty_events_list_returns_empty_tags(self):
        raw = _make_raw(events=[])
        result = parse_gamma_market(raw)
        assert result is not None
        assert result["tags"] == []

    def test_events_without_tags_key_returns_empty(self):
        raw = _make_raw(events=[{"id": 1, "title": "Some event"}])
        result = parse_gamma_market(raw)
        assert result is not None
        assert result["tags"] == []

    def test_events_with_empty_tags_list(self):
        raw = _make_raw(events=[{"tags": []}])
        result = parse_gamma_market(raw)
        assert result is not None
        assert result["tags"] == []

    def test_deduplication_across_events(self):
        raw = _make_raw(events=[
            {"tags": [{"label": "Politics", "slug": "politics"}]},
            {"tags": [{"label": "Politics", "slug": "politics"}, {"label": "Trump", "slug": "trump"}]},
        ])
        result = parse_gamma_market(raw)
        assert result is not None
        assert result["tags"].count("politics") == 1

    def test_deduplication_within_single_event(self):
        raw = _make_raw(events=[
            {"tags": [
                {"label": "Crypto", "slug": "crypto"},
                {"label": "Crypto", "slug": "crypto"},
            ]}
        ])
        result = parse_gamma_market(raw)
        assert result is not None
        assert result["tags"].count("crypto") == 1

    def test_tags_are_lowercased(self):
        raw = _make_raw(events=[
            {"tags": [{"label": "POLITICS", "slug": "POLITICS"}]}
        ])
        result = parse_gamma_market(raw)
        assert result is not None
        assert "politics" in result["tags"]
        assert "POLITICS" not in result["tags"]

    def test_tags_with_whitespace_are_stripped(self):
        raw = _make_raw(events=[
            {"tags": [{"label": "Sports", "slug": "  sports  "}]}
        ])
        result = parse_gamma_market(raw)
        assert result is not None
        assert "sports" in result["tags"]

    def test_tags_with_empty_slug_are_skipped(self):
        raw = _make_raw(events=[
            {"tags": [{"label": "No Slug", "slug": ""}, {"label": "Crypto", "slug": "crypto"}]}
        ])
        result = parse_gamma_market(raw)
        assert result is not None
        assert "" not in result["tags"]
        assert "crypto" in result["tags"]

    def test_tags_order_preserved(self):
        raw = _make_raw(events=[
            {"tags": [{"label": "Crypto", "slug": "crypto"}]},
            {"tags": [{"label": "Finance", "slug": "finance"}]},
        ])
        result = parse_gamma_market(raw)
        assert result is not None
        assert result["tags"] == ["crypto", "finance"]


class TestCategoryDerivation:
    def test_category_derived_from_politics_tag(self):
        raw = _make_raw(events=[
            {"tags": [{"label": "Politics", "slug": "politics"}, {"label": "Trump", "slug": "trump"}]}
        ])
        result = parse_gamma_market(raw)
        assert result is not None
        assert result["category"] == "politics"

    def test_category_derived_from_crypto_tag(self):
        raw = _make_raw(events=[
            {"tags": [{"label": "Crypto", "slug": "crypto"}]}
        ])
        result = parse_gamma_market(raw)
        assert result is not None
        assert result["category"] == "crypto"

    def test_first_recognized_tag_wins(self):
        # "trump" is not in CATEGORY_TAGS; "politics" should win
        raw = _make_raw(events=[
            {"tags": [{"label": "Trump", "slug": "trump"}, {"label": "Politics", "slug": "politics"}]}
        ])
        result = parse_gamma_market(raw)
        assert result is not None
        assert result["category"] == "politics"

    def test_unrecognized_tags_fall_back_to_raw_category(self):
        raw = _make_raw(
            events=[{"tags": [{"label": "Trump", "slug": "trump"}]}],
            category="SomeRawCategory",
        )
        result = parse_gamma_market(raw)
        assert result is not None
        assert result["category"] == "SomeRawCategory"

    def test_no_events_falls_back_to_raw_category(self):
        raw = _make_raw(category="Crypto")
        result = parse_gamma_market(raw)
        assert result is not None
        assert result["category"] == "Crypto"

    def test_no_events_no_category_falls_back_to_slug(self):
        raw = _make_raw()  # no events, no category; slug="will-this-happen"
        result = parse_gamma_market(raw)
        assert result is not None
        assert result["category"] == "will-this-happen"

    def test_geopolitics_recognized(self):
        raw = _make_raw(events=[{"tags": [{"label": "Geopolitics", "slug": "geopolitics"}]}])
        result = parse_gamma_market(raw)
        assert result is not None
        assert result["category"] == "geopolitics"

    def test_sports_recognized(self):
        raw = _make_raw(events=[{"tags": [{"label": "Sports", "slug": "sports"}]}])
        result = parse_gamma_market(raw)
        assert result is not None
        assert result["category"] == "sports"

    def test_all_category_tags_are_recognized(self):
        """Every tag in CATEGORY_TAGS should be derivable as a category."""
        for cat in CATEGORY_TAGS:
            raw = _make_raw(events=[{"tags": [{"label": cat.title(), "slug": cat}]}])
            result = parse_gamma_market(raw)
            assert result is not None, f"parse_gamma_market returned None for tag '{cat}'"
            assert result["category"] == cat, f"Expected category '{cat}', got '{result['category']}'"

    def test_tags_key_present_in_output(self):
        raw = _make_raw()
        result = parse_gamma_market(raw)
        assert result is not None
        assert "tags" in result
        assert isinstance(result["tags"], list)
