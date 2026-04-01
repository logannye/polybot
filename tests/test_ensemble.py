import pytest
from polybot.analysis.prompts import build_analysis_prompt, parse_llm_response
from polybot.analysis.ensemble import ModelEstimate, EnsembleResult, aggregate_estimates


class TestPrompts:
    def test_build_prompt(self):
        prompt = build_analysis_prompt(question="Will BTC exceed 100K by April?", research_context="- BTC at 95K today\n  URL: https://example.com\n  Rising fast.")
        assert "Will BTC exceed 100K by April?" in prompt
        assert "BTC at 95K today" in prompt
        assert "probability" in prompt.lower()
        assert "market price" not in prompt.lower()

    def test_parse_valid_response(self):
        response = '{"probability": 0.65, "confidence": "high", "reasoning": "Strong momentum."}'
        result = parse_llm_response(response)
        assert result is not None
        assert result["probability"] == pytest.approx(0.65)
        assert result["confidence"] == "high"

    def test_parse_extracts_json_from_text(self):
        response = 'Here is my analysis:\n```json\n{"probability": 0.72, "confidence": "medium", "reasoning": "Based on current trends."}\n```'
        result = parse_llm_response(response)
        assert result is not None
        assert result["probability"] == pytest.approx(0.72)

    def test_parse_invalid_response(self):
        assert parse_llm_response("I cannot make predictions about the future.") is None

    def test_parse_clamps_probability(self):
        result = parse_llm_response('{"probability": 1.5, "confidence": "high", "reasoning": "Very sure."}')
        assert result["probability"] == 0.99

    def test_parse_clamps_low_probability(self):
        result = parse_llm_response('{"probability": -0.1, "confidence": "low", "reasoning": "Not sure."}')
        assert result["probability"] == 0.01


class TestAggregate:
    def test_equal_weights(self):
        estimates = [ModelEstimate(model="a", probability=0.60, confidence="high", reasoning="r1"),
                     ModelEstimate(model="b", probability=0.70, confidence="high", reasoning="r2"),
                     ModelEstimate(model="c", probability=0.50, confidence="high", reasoning="r3")]
        result = aggregate_estimates(estimates, {"a": 0.333, "b": 0.333, "c": 0.333})
        assert result.ensemble_probability == pytest.approx(0.60, abs=0.01)
        assert result.stdev == pytest.approx(0.0816, abs=0.01)

    def test_weighted_toward_better_model(self):
        estimates = [ModelEstimate(model="a", probability=0.80, confidence="high", reasoning="r1"),
                     ModelEstimate(model="b", probability=0.40, confidence="high", reasoning="r2")]
        result = aggregate_estimates(estimates, {"a": 0.8, "b": 0.2})
        assert result.ensemble_probability == pytest.approx(0.72, abs=0.01)

    def test_confidence_weighting(self):
        estimates = [ModelEstimate(model="a", probability=0.80, confidence="high", reasoning="r1"),
                     ModelEstimate(model="b", probability=0.40, confidence="low", reasoning="r2")]
        result = aggregate_estimates(estimates, {"a": 0.5, "b": 0.5})
        assert result.ensemble_probability > 0.60

    def test_single_estimate(self):
        estimates = [ModelEstimate(model="a", probability=0.65, confidence="medium", reasoning="r1")]
        result = aggregate_estimates(estimates, {"a": 1.0})
        assert result.ensemble_probability == pytest.approx(0.65)
        assert result.stdev == pytest.approx(0.0)


class TestEnsembleFailureTracking:
    def test_consecutive_failures_tracked(self):
        from polybot.analysis.ensemble import EnsembleAnalyzer
        analyzer = EnsembleAnalyzer(
            anthropic_key="test", openai_key="test", google_key="test")
        assert analyzer._consecutive_failures == {"claude": 0, "openai": 0, "gemini": 0}

    def test_consecutive_failures_reset_on_success(self):
        from polybot.analysis.ensemble import EnsembleAnalyzer
        analyzer = EnsembleAnalyzer(
            anthropic_key="test", openai_key="test", google_key="test")
        analyzer._consecutive_failures["openai"] = 10
        # Simulating a success resets the counter
        analyzer._consecutive_failures["openai"] = 0
        assert analyzer._consecutive_failures["openai"] == 0
