"""Thin async Gemini Flash client with daily spend cap.

v10 spec §4 — Snipe T1 verification uses Gemini Flash and must halt
snipe for the rest of the UTC day if daily spend exceeds ``cap_usd``
(default $2).

The client is deliberately minimal:
- one method ``verify_snipe`` returning ``(verdict, confidence)``
- internal daily spend tracker keyed by UTC date
- no retry logic — we fail the verification on error (safer than
  accidentally approving a bad trade)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

import structlog

log = structlog.get_logger()

Verdict = Literal["YES_LOCKED", "NO_LOCKED", "UNCERTAIN"]


@dataclass
class GeminiResult:
    verdict: Verdict
    confidence: float


# Approximate per-call cost (input+output tokens for Flash 2.5 pricing circa
# April 2026). Used for the daily spend accumulator.
APPROX_COST_PER_CALL_USD = 0.0015


@dataclass
class DailySpendTracker:
    date_utc: str = ""
    spend_usd: float = 0.0

    def accumulate(self, cost_usd: float) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.date_utc:
            self.date_utc = today
            self.spend_usd = 0.0
        self.spend_usd += cost_usd

    def current_spend(self) -> float:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.date_utc:
            return 0.0
        return self.spend_usd


class GeminiClient:
    """Minimal Gemini Flash client for snipe T1 verification."""

    def __init__(self, api_key: str, cap_usd: float = 2.0,
                 model: str = "gemini-2.5-flash"):
        self._api_key = api_key
        self._cap_usd = cap_usd
        self._model = model
        self._spend = DailySpendTracker()
        self._client = None    # lazy-init on first call

    def _ensure_client(self):
        if self._client is None:
            # Import lazily so tests can run without google-genai installed
            from google import genai
            self._client = genai.Client(api_key=self._api_key)

    async def _generate(self, prompt: str) -> Optional[str]:
        self._ensure_client()
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model, contents=prompt)
            return response.text
        except Exception as e:
            log.error("gemini_generate_failed", error=str(e)[:200])
            return None

    def can_spend(self) -> bool:
        return self._spend.current_spend() < self._cap_usd

    def current_spend(self) -> float:
        return self._spend.current_spend()

    async def verify_snipe(
        self, question: str, resolution_time_iso: str,
        hours_remaining: float, yes_price: float,
    ) -> GeminiResult:
        """Ask Gemini: is this market resolved or about to resolve?

        Returns a GeminiResult. If the spend cap is hit, returns UNCERTAIN
        with confidence=0 so the caller skips the trade.
        """
        if not self.can_spend():
            return GeminiResult(verdict="UNCERTAIN", confidence=0.0)

        prompt = (
            "You are verifying a prediction-market snipe. Given the question "
            "and context below, return JSON with:\n"
            "- verdict: YES_LOCKED / NO_LOCKED / UNCERTAIN\n"
            "- confidence: 0.0-1.0\n\n"
            f"Question: {question}\n"
            f"Resolution time: {resolution_time_iso}\n"
            f"Hours remaining: {hours_remaining:.1f}\n"
            f"Current YES price: {yes_price:.3f}\n\n"
            "Return ONLY the JSON object, no explanation."
        )
        text = await self._generate(prompt)
        self._spend.accumulate(APPROX_COST_PER_CALL_USD)
        if not text:
            return GeminiResult(verdict="UNCERTAIN", confidence=0.0)
        return _parse_verdict(text)


def _parse_verdict(text: str) -> GeminiResult:
    """Parse Gemini JSON response. Permissive — return UNCERTAIN on malformed."""
    import json
    import re

    # Strip markdown code fences if present
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(),
                     flags=re.MULTILINE)
    try:
        data = json.loads(cleaned)
        verdict = data.get("verdict", "UNCERTAIN").upper()
        if verdict not in ("YES_LOCKED", "NO_LOCKED", "UNCERTAIN"):
            verdict = "UNCERTAIN"
        confidence = float(data.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))
        return GeminiResult(verdict=verdict, confidence=confidence)   # type: ignore
    except (json.JSONDecodeError, ValueError, TypeError):
        return GeminiResult(verdict="UNCERTAIN", confidence=0.0)
