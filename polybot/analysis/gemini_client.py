"""Gemini Flash client for Snipe T1 verification (v12 hardened).

Schema-strict response with grounding requirement: the verifier must
articulate a *concrete* reason a market is locked (a date, name, score,
quote, vote count, etc.). Hand-wavy reasons ("seems likely", "probably")
without grounding are auto-rejected before the result is returned.

Daily spend is capped to prevent runaway cost; on cap-hit we return
UNCERTAIN so the caller skips the trade.
"""
from __future__ import annotations

import json
import re
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
    reason: str = ""


APPROX_COST_PER_CALL_USD = 0.0015

# Words that, in absence of concrete grounding, indicate the verifier is
# guessing rather than verifying. We don't reject the words themselves —
# we reject reasons that have these AND lack any concrete grounding token.
_HEDGE_WORDS = re.compile(
    r"\b(seems?|likely|probably|possibly|might|could|appears?)\b",
    re.IGNORECASE)

# Concrete grounding tokens: digits, dates, score patterns, percentages.
# Case-sensitive on its own so [A-Z]{2,} actually means caps.
_GROUNDING_NUMERIC = re.compile(
    r"\d+(?:\.\d+)?%?"           # numbers / percents
    r"|\b\d{1,2}:\d{2}\b"        # times / scores
)
_GROUNDING_CAPS = re.compile(r"\b[A-Z]{2,}\b")    # AP, ESPN, NBA, EU
_GROUNDING_DATE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December|Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b",
    re.IGNORECASE)


def _has_grounding(s: str) -> bool:
    return bool(
        _GROUNDING_NUMERIC.search(s)
        or _GROUNDING_CAPS.search(s)
        or _GROUNDING_DATE.search(s)
    )


def validate_reason(reason: str, *, min_chars: int = 30) -> tuple[bool, str]:
    """Pure function. Returns (ok, rejection_reason).

    A reason is OK if it is at least `min_chars` long AND either contains a
    concrete grounding token OR contains no hedge words.

    "Trump conceded at 8:14pm ET" → ok (digits + AM/PM)
    "AP called the race for X" → ok (AP)
    "Game ended 7-2 in the 9th" → ok (score + 9th)
    "Seems likely the favorite wins" → reject (hedge, no grounding)
    "Race over" → reject (too short)
    """
    if not reason or len(reason.strip()) < min_chars:
        return False, "reason_too_short"
    has_hedge = bool(_HEDGE_WORDS.search(reason))
    has_grounding = _has_grounding(reason)
    if has_hedge and not has_grounding:
        return False, "hedge_without_grounding"
    return True, ""


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
    """Snipe T1 verifier with structured schema response and grounding gate."""

    def __init__(self, api_key: str, cap_usd: float = 2.0,
                 model: str = "gemini-2.5-flash",
                 min_reason_chars: int = 30):
        self._api_key = api_key
        self._cap_usd = cap_usd
        self._model = model
        self._min_reason_chars = min_reason_chars
        self._spend = DailySpendTracker()
        self._client = None

    def _ensure_client(self):
        if self._client is None:
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
        """Verify whether a market is mechanically locked.

        Returns UNCERTAIN with confidence=0 on:
          - daily spend cap hit
          - LLM call failure
          - malformed JSON response
          - reason fails grounding check (`validate_reason`)
        """
        if not self.can_spend():
            return GeminiResult(verdict="UNCERTAIN", confidence=0.0,
                                reason="daily_spend_cap_hit")

        prompt = (
            "You are verifying whether a Polymarket prediction market is "
            "MECHANICALLY LOCKED — i.e. the outcome is already determined "
            "by a known, verifiable real-world event, and the market price "
            "just hasn't converged yet.\n\n"
            "Return ONLY a JSON object with these exact fields:\n"
            "  - verdict: one of YES_LOCKED, NO_LOCKED, UNCERTAIN\n"
            "  - confidence: float in [0.0, 1.0]\n"
            "  - reason: a SHORT (1-2 sentences) sentence stating the "
            "CONCRETE grounding — a date, score, name, vote count, source, "
            "etc. Do not use words like 'seems', 'likely', 'probably' "
            "without grounding. Do not speculate.\n\n"
            "If you don't have concrete real-world evidence the outcome is "
            "decided, return UNCERTAIN with confidence below 0.5.\n\n"
            f"Question: {question}\n"
            f"Resolution time (UTC): {resolution_time_iso}\n"
            f"Hours remaining: {hours_remaining:.1f}\n"
            f"Current YES price: {yes_price:.3f}\n"
        )
        text = await self._generate(prompt)
        self._spend.accumulate(APPROX_COST_PER_CALL_USD)
        if not text:
            return GeminiResult(verdict="UNCERTAIN", confidence=0.0,
                                reason="llm_call_failed")
        result = _parse_verdict(text)

        # Apply grounding gate. If verdict was YES/NO_LOCKED but the reason
        # is hedge-without-grounding or too short, demote to UNCERTAIN. We
        # keep the raw reason in the returned result for the shadow log.
        if result.verdict in ("YES_LOCKED", "NO_LOCKED"):
            ok, why = validate_reason(result.reason,
                                       min_chars=self._min_reason_chars)
            if not ok:
                log.info("verifier_reason_rejected",
                         verdict=result.verdict, why=why,
                         reason=(result.reason or "")[:120])
                return GeminiResult(
                    verdict="UNCERTAIN", confidence=0.0,
                    reason=f"grounding_failed:{why}|{result.reason[:120]}")
        return result


def _parse_verdict(text: str) -> GeminiResult:
    """Parse Gemini response. Returns UNCERTAIN on malformed JSON."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(),
                     flags=re.MULTILINE)
    try:
        data = json.loads(cleaned)
        verdict = str(data.get("verdict", "UNCERTAIN")).upper()
        if verdict not in ("YES_LOCKED", "NO_LOCKED", "UNCERTAIN"):
            verdict = "UNCERTAIN"
        confidence = float(data.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))
        reason = str(data.get("reason", ""))[:500]
        return GeminiResult(verdict=verdict, confidence=confidence,    # type: ignore
                            reason=reason)
    except (json.JSONDecodeError, ValueError, TypeError):
        return GeminiResult(verdict="UNCERTAIN", confidence=0.0,
                            reason="malformed_json")
