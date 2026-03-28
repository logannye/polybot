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
