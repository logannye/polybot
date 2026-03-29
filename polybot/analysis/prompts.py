import json
import re


def build_analysis_prompt(question: str, research_context: str) -> str:
    return f"""You are a prediction market analyst. Your task is to estimate the probability that the following question resolves YES.

## Question
{question}

## Recent Research
{research_context}

## Instructions
1. Analyze the question and research context carefully.
2. Estimate the probability that this resolves YES.
3. Do NOT anchor to the current trading price — form your own independent estimate.
4. Return your answer as JSON with exactly these fields:

```json
{{
  "probability": <float between 0.01 and 0.99>,
  "confidence": "<low|medium|high>",
  "reasoning": "<2 sentence justification>"
}}
```

Return ONLY the JSON object, no other text."""


def parse_llm_response(response: str) -> dict | None:
    try:
        data = json.loads(response.strip())
        return _validate_parsed(data)
    except json.JSONDecodeError:
        pass
    json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response, re.DOTALL)
    if json_match:
        try:
            return _validate_parsed(json.loads(json_match.group(1)))
        except json.JSONDecodeError:
            pass
    json_match = re.search(r'\{[^{}]*"probability"[^{}]*\}', response)
    if json_match:
        try:
            return _validate_parsed(json.loads(json_match.group(0)))
        except json.JSONDecodeError:
            pass
    return None


def _validate_parsed(data: dict) -> dict | None:
    if "probability" not in data:
        return None
    prob = max(0.01, min(0.99, float(data["probability"])))
    confidence = data.get("confidence", "medium")
    if confidence not in ("low", "medium", "high"):
        confidence = "medium"
    return {"probability": prob, "confidence": confidence, "reasoning": data.get("reasoning", "")}


def build_snipe_prompt(question: str, resolution_time: str, hours_remaining: float, price: float) -> str:
    return f"""You are verifying whether a prediction market's outcome is already determined.

Question: {question}
Resolves at: {resolution_time} ({hours_remaining:.1f}h from now)
Current YES price: {price}

Is the outcome of this question ALREADY DETERMINED based on events that have occurred? Answer ONLY with JSON:
{{"determined": true/false, "outcome": "YES"/"NO"/"UNKNOWN", "confidence": 0.0-1.0, "reason": "..."}}

Return ONLY the JSON object, no other text."""


def parse_snipe_response(response: str) -> dict | None:
    try:
        data = json.loads(response.strip())
        return _validate_snipe(data)
    except json.JSONDecodeError:
        pass
    json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response, re.DOTALL)
    if json_match:
        try:
            return _validate_snipe(json.loads(json_match.group(1)))
        except json.JSONDecodeError:
            pass
    json_match = re.search(r'\{[^{}]*"determined"[^{}]*\}', response)
    if json_match:
        try:
            return _validate_snipe(json.loads(json_match.group(0)))
        except json.JSONDecodeError:
            pass
    return None


def _validate_snipe(data: dict) -> dict | None:
    if "determined" not in data:
        return None
    determined = bool(data["determined"])
    outcome = data.get("outcome", "UNKNOWN")
    if outcome not in ("YES", "NO", "UNKNOWN"):
        outcome = "UNKNOWN"
    confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
    return {"determined": determined, "outcome": outcome, "confidence": confidence, "reason": data.get("reason", "")}


def build_challenge_prompt(question: str, initial_prob: float,
                           market_price: float, reasoning: str) -> str:
    """Second-pass prompt: reveal market price and ask LLM to revise."""
    return f"""You previously estimated this prediction market question at {initial_prob:.0%} probability:

## Question
{question}

## Your initial reasoning
{reasoning}

## Market information
The market is currently trading at ${market_price:.2f} (i.e., the crowd estimates {market_price:.0%}).

## Instructions
The market price represents real money from many participants. If your estimate differs significantly from the market, one of you is likely wrong.

Consider: What might the market know that you don't? Are there specific facts, deadlines, or developments that could explain the market price?

After considering this, give your REVISED probability estimate.

Return ONLY JSON:
```json
{{"probability": <float between 0.01 and 0.99>, "confidence": "<low|medium|high>", "reasoning": "<why you revised or maintained your estimate>"}}
```"""


def build_quick_screen_prompt(question: str, price: float, resolution_time: str) -> str:
    return f"""Prediction market question: {question}
Current YES price: ${price}
Resolves: {resolution_time}

Estimate the true probability this resolves YES.
Return ONLY: {{"probability": <float>, "reasoning": "<1 sentence>"}}"""
