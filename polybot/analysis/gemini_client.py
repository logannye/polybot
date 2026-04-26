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


# Phrases that indicate the verifier is reasoning about a *future-undetermined*
# outcome rather than a *mechanically locked* one. Observed in production at
# T+1h: NO_LOCKED@conf=1.0 verdicts where the reason says the event "has not
# yet occurred" or "could still occur" — the verifier is conflating the prior
# (unlikely event) with a lock (impossible event). These verdicts are
# structurally wrong and must be demoted to UNCERTAIN.
#
# A genuinely locked NO requires evidence the YES outcome is IMPOSSIBLE
# (deadline passed without occurrence, structural prerequisite missing,
# resolution criterion already failed). Phrases below indicate the LLM is
# arguing from "hasn't happened" to "won't happen" — which is the prior, not
# a lock.
_NON_LOCK_PATTERNS = re.compile(
    r"(has\s+not\s+(?:yet\s+)?(?:occurred|happened|passed|been|taken\s+place))"
    r"|(?:is|are|remains?)\s+(?:still\s+)?in\s+the\s+future"
    r"|(?:could|may|might)\s+(?:still|yet)\s+(?:occur|happen|come|be)"
    r"|(?:still|yet)\s+(?:to|could)\s+(?:occur|happen|come)"
    r"|remaining\s+\d+(?:\.\d+)?\s+(?:hours?|minutes?|days?|weeks?)"
    r"|(?:has|have)\s+not\s+(?:yet\s+)?(?:been\s+)?(?:resolved|determined|decided|announced|confirmed)"
    r"|(?:not\s+)?yet\s+(?:passed|elapsed|reached|known)"
    r"|outcome\s+(?:is\s+)?(?:not\s+)?(?:yet\s+)?(?:known|determined|decided|certain)"
    r"|(?:event|outcome|result|condition)s?\s+(?:could|can|may|might)\s+still",
    re.IGNORECASE)


def validate_reason(reason: str, *, min_chars: int = 30) -> tuple[bool, str]:
    """Pure function. Returns (ok, rejection_reason).

    Three independent gates, in order:
      1. Length floor (`min_chars`).
      2. Hedge-without-grounding: bans "seems/likely/probably" unless a
         concrete grounding token (digit, ALL-CAPS abbreviation, month) is
         also present.
      3. Non-lock semantic: bans phrasings that describe a *future-but-
         not-yet-determined* outcome. These are the prior, not a lock.

    Examples:
      "Trump conceded at 8:14pm ET" → ok
      "AP called the race for X candidate" → ok
      "FOMC has no scheduled meeting in April 2026" → ok (genuine lock —
         structural prerequisite missing, no future-tense hedge)
      "The election has not yet occurred" → reject (non_lock_phrasing)
      "April 30, 2026 is in the future, the event could still occur"
         → reject (non_lock_phrasing)
      "Seems likely the favorite wins" → reject (hedge, no grounding)
      "Race over" → reject (too short)
    """
    if not reason or len(reason.strip()) < min_chars:
        return False, "reason_too_short"
    # Non-lock phrasing is checked FIRST: it's the most specific structural
    # error (the LLM is reasoning from "hasn't happened" to "won't happen"),
    # and it's strictly worse than a hedge — a hedge with grounding can still
    # be a real lock, but non-lock phrasing always indicates wrong reasoning.
    if _NON_LOCK_PATTERNS.search(reason):
        return False, "non_lock_phrasing"
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
                 min_reason_chars: int = 30,
                 cache=None):
        """`cache` is an optional VerifierCache. When provided, repeat
        verifications of the same market within TTL skip the LLM call.
        """
        self._api_key = api_key
        self._cap_usd = cap_usd
        self._model = model
        self._min_reason_chars = min_reason_chars
        self._spend = DailySpendTracker()
        self._client = None
        self._cache = cache

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
        polymarket_id: str = "",
    ) -> GeminiResult:
        """Verify whether a market is mechanically locked.

        Returns UNCERTAIN with confidence=0 on:
          - daily spend cap hit
          - LLM call failure
          - malformed JSON response
          - reason fails grounding check (`validate_reason`)

        If a `cache` was supplied at construction and `polymarket_id` is
        provided, lookup is attempted first. On a hit we skip the LLM call
        entirely. On a miss we make the call and store the result.
        """
        # Cache lookup before anything else (including spend cap), since a
        # cached verdict consumes no budget.
        if self._cache is not None and polymarket_id:
            cached = self._cache.lookup(
                polymarket_id, yes_price=yes_price,
                hours_remaining=hours_remaining)
            if cached is not None:
                return cached

        if not self.can_spend():
            return GeminiResult(verdict="UNCERTAIN", confidence=0.0,
                                reason="daily_spend_cap_hit")

        prompt = (
            "You are verifying whether a Polymarket prediction market is "
            "MECHANICALLY LOCKED. A market is LOCKED only if ONE of:\n"
            "  (a) the YES outcome has already provably occurred — return "
            "YES_LOCKED;\n"
            "  (b) the YES outcome is now provably IMPOSSIBLE before "
            "resolution (deadline passed without occurrence, structural "
            "prerequisite missing, or resolution criterion already failed) "
            "— return NO_LOCKED.\n\n"
            "CRITICAL DISAMBIGUATION:\n"
            "An event that 'has not yet occurred' or 'could still occur' "
            "or 'is in the future' is NOT locked. The market price already "
            "reflects the prior — your job is to spot the cases where the "
            "outcome is now CERTAIN, not where it is merely unlikely.\n\n"
            "Examples:\n"
            "  GOOD NO_LOCKED: 'There is no FOMC meeting scheduled in April "
            "2026, so a rate cut at that meeting is impossible.' (structural "
            "prerequisite missing)\n"
            "  GOOD YES_LOCKED: 'AP called the race for Smith at 8:14pm ET; "
            "concession speech delivered.' (event has occurred)\n"
            "  BAD (must return UNCERTAIN): 'The election has not yet "
            "occurred' or 'The resolution date is in the future' or 'The "
            "event could still occur within remaining hours.' These describe "
            "the prior, not a lock.\n\n"
            "Return ONLY a JSON object with these exact fields:\n"
            "  - verdict: one of YES_LOCKED, NO_LOCKED, UNCERTAIN\n"
            "  - confidence: float in [0.0, 1.0]\n"
            "  - reason: ONE sentence stating the CONCRETE grounding — what "
            "happened, when, where, who said so. If you cannot articulate a "
            "specific real-world event that locks the outcome, return "
            "UNCERTAIN with confidence below 0.5.\n\n"
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
                result = GeminiResult(
                    verdict="UNCERTAIN", confidence=0.0,
                    reason=f"grounding_failed:{why}|{result.reason[:120]}")

        # Cache the (possibly demoted) result so the next cycle's lookup
        # short-circuits. Even UNCERTAIN responses are worth caching — they
        # mean the LLM declined to lock, and that decision is stable until
        # price/hours drift past invalidation thresholds.
        if self._cache is not None and polymarket_id:
            self._cache.store(
                polymarket_id, result,
                yes_price=yes_price, hours_remaining=hours_remaining)
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
