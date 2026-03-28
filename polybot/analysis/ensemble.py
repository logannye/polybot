import asyncio
import statistics
import structlog
import anthropic
import openai
from google import genai
from dataclasses import dataclass
from polybot.analysis.prompts import build_analysis_prompt, parse_llm_response

log = structlog.get_logger()
CONFIDENCE_WEIGHTS = {"high": 1.0, "medium": 0.7, "low": 0.4}


@dataclass
class ModelEstimate:
    model: str
    probability: float
    confidence: str
    reasoning: str


@dataclass
class EnsembleResult:
    estimates: list[ModelEstimate]
    ensemble_probability: float
    stdev: float


def aggregate_estimates(estimates: list[ModelEstimate], trust_weights: dict[str, float]) -> EnsembleResult:
    if not estimates:
        return EnsembleResult(estimates=[], ensemble_probability=0.5, stdev=1.0)
    if len(estimates) == 1:
        return EnsembleResult(estimates=estimates, ensemble_probability=estimates[0].probability, stdev=0.0)
    weighted_sum = 0.0
    weight_total = 0.0
    for est in estimates:
        tw = trust_weights.get(est.model, 0.333)
        cw = CONFIDENCE_WEIGHTS.get(est.confidence, 0.7)
        combined = tw * cw
        weighted_sum += est.probability * combined
        weight_total += combined
    ensemble_prob = weighted_sum / weight_total if weight_total > 0 else 0.5
    stdev = statistics.pstdev([e.probability for e in estimates])
    return EnsembleResult(estimates=estimates, ensemble_probability=ensemble_prob, stdev=stdev)


class EnsembleAnalyzer:
    def __init__(self, anthropic_key: str, openai_key: str, google_key: str):
        self._anthropic = anthropic.AsyncAnthropic(api_key=anthropic_key)
        self._openai = openai.AsyncOpenAI(api_key=openai_key)
        self._google = genai.Client(api_key=google_key)

    async def analyze(self, question: str, research_context: str, trust_weights: dict[str, float]) -> EnsembleResult:
        prompt = build_analysis_prompt(question, research_context)
        results = await asyncio.gather(
            self._call_claude(prompt),
            self._call_openai(prompt),
            self._call_gemini(prompt),
            return_exceptions=True,
        )
        estimates = [r for r in results if isinstance(r, ModelEstimate)]
        for r in results:
            if isinstance(r, Exception):
                log.error("llm_call_failed", error=str(r))
        return aggregate_estimates(estimates, trust_weights)

    async def _call_claude(self, prompt: str) -> ModelEstimate:
        response = await self._anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        parsed = parse_llm_response(response.content[0].text)
        if not parsed:
            raise ValueError("Claude returned unparseable response")
        return ModelEstimate(
            model="claude-sonnet-4.6",
            probability=parsed["probability"],
            confidence=parsed["confidence"],
            reasoning=parsed["reasoning"],
        )

    async def _call_openai(self, prompt: str) -> ModelEstimate:
        response = await self._openai.chat.completions.create(
            model="gpt-4o",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        parsed = parse_llm_response(response.choices[0].message.content)
        if not parsed:
            raise ValueError("GPT-4o returned unparseable response")
        return ModelEstimate(
            model="gpt-4o",
            probability=parsed["probability"],
            confidence=parsed["confidence"],
            reasoning=parsed["reasoning"],
        )

    async def _call_gemini(self, prompt: str) -> ModelEstimate:
        response = await self._google.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        parsed = parse_llm_response(response.text)
        if not parsed:
            raise ValueError("Gemini returned unparseable response")
        return ModelEstimate(
            model="gemini-2.5-flash",
            probability=parsed["probability"],
            confidence=parsed["confidence"],
            reasoning=parsed["reasoning"],
        )
