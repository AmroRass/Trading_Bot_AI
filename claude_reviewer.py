"""
claude_reviewer.py - Structured Claude trade reviewer

Claude's job:
- Review market setup quality.
- Decide whether the setup is clean, extended, messy, or unsafe.

Python's job:
- Calculate objective market facts.
- Validate prices, levels, R:R, stop distance, open positions, and execution safety.

This module is standalone and does NOT modify existing monitor scripts.
"""

import os
import re
import json
from typing import Dict, Any, Optional

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()


class ClaudeReviewer:
    """
    Claude trade reviewer - returns structured decisions WITHOUT calculating prices.

    Input:
        market_snapshot dict with facts calculated by Python.

    Output:
        {
            "decision": "ENTER_NOW | WAIT_PULLBACK | NO_TRADE",
            "setup": "LONG | SHORT | NONE",
            "confidence": "HIGH | MEDIUM | LOW",
            "entry_style": "BREAKOUT | PULLBACK | REVERSAL | NONE",
            "reason": str,
            "risk_comment": str,
            "is_late_chase": bool,
            "needs_pullback": bool
        }
    """

    def __init__(self, api_key: Optional[str] = None, model: str = "claude-sonnet-4-6"):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY not found in environment")

        self.client = Anthropic(api_key=self.api_key)
        self.model = model

    def review_setup(self, market_snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """
        Review a market setup and return a structured trading decision.

        Claude reviews setup quality.
        Python validates the returned decision against objective snapshot facts.
        """
        try:
            prompt = self._build_prompt(market_snapshot)
            response = self._call_claude(prompt)
            decision = self._parse_response(response)
            validated = self._validate_decision(decision)
            sanity_checked = self._sanity_check_against_snapshot(validated, market_snapshot)
            return sanity_checked

        except Exception as e:
            print(f"[CLAUDE_REVIEWER] Error: {e}")
            return self._fallback_response(f"Error: {str(e)}")

    def _build_prompt(self, snapshot: Dict[str, Any]) -> str:
        """Build focused prompt asking Claude to review setup quality."""

        instrument = snapshot.get("instrument", "UNKNOWN")

        # Support both names safely.
        price = snapshot.get("current_price", snapshot.get("price", 0))

        regime = snapshot.get("regime", "UNKNOWN")
        daily_trend = snapshot.get("daily_trend", "UNKNOWN")
        market_state = snapshot.get("market_state", "UNKNOWN")

        breakout_level = snapshot.get("breakout_level")
        next_resistance = snapshot.get("next_resistance")
        nearest_support = snapshot.get("nearest_support")
        at_key_level = snapshot.get("at_key_level", False)
        level_name = snapshot.get("level_name", "")

        breakout_confirmed = snapshot.get("breakout_confirmed", False)
        breakdown_confirmed = snapshot.get("breakdown_confirmed", False)
        candles_above_level = snapshot.get("candles_above_level", 0)
        candles_below_level = snapshot.get("candles_below_level", 0)

        consecutive_bullish = snapshot.get("consecutive_bullish_candles", 0)
        consecutive_bearish = snapshot.get("consecutive_bearish_candles", 0)

        ema_alignment = snapshot.get("ema_alignment", "neutral")
        price_vs_ema50 = snapshot.get("price_vs_ema50", "neutral")

        extension_check = snapshot.get("extension_check", "UNKNOWN")
        distance_from_entry = snapshot.get("distance_from_entry", 0)

        session = snapshot.get("session", "UNKNOWN")
        news_nearby = snapshot.get("news_nearby", False)

        prompt = f"""You are a trade setup reviewer.

Your job is to judge whether the setup is clean, messy, extended, unsafe, or worth waiting for.

DO NOT calculate entry, stop, target, stop distance, or R:R.
Python handles all arithmetic and final execution validation.
If R:R, stop distance, or target validity matters, mention the concern in risk_comment and let Python validate it.

IMPORTANT LEVEL DEFINITIONS:
- breakout_level = the trigger level already broken by price.
- next_resistance = the next upside obstacle or target area. This is NOT already broken unless CURRENT PRICE is above it.
- nearest_support = the nearest downside support area.
- Use "trigger level" when referring to breakout_level.
- Do NOT say price broke above next_resistance unless CURRENT PRICE is actually above next_resistance.
- Do NOT say price broke below nearest_support unless CURRENT PRICE is actually below nearest_support.

INSTRUMENT: {instrument}
CURRENT PRICE: {price}
SESSION: {session}

MARKET STATE:
{market_state}

MARKET REGIME:
{regime}

DAILY TREND:
{daily_trend}

KEY LEVELS:
- Trigger level already broken / tested: {breakout_level} ({level_name})
- Next resistance / upside target area: {next_resistance}
- Nearest support / downside level: {nearest_support}
- At key level: {at_key_level}

BREAKOUT / BREAKDOWN STATUS:
- Breakout confirmed by close above trigger level: {breakout_confirmed}
- Breakdown confirmed by close below trigger level: {breakdown_confirmed}
- Candles closed above trigger level: {candles_above_level}
- Candles closed below trigger level: {candles_below_level}

MOMENTUM:
- Consecutive bullish 5M candles: {consecutive_bullish}
- Consecutive bearish 5M candles: {consecutive_bearish}

STRUCTURE:
- EMA alignment: {ema_alignment}
- Price vs EMA50: {price_vs_ema50}

EXTENSION CHECK:
{extension_check}

DISTANCE / CHASE CONTEXT:
- Distance from original entry / trigger area: {distance_from_entry}

NEWS NEARBY:
{news_nearby}

TRADING RULES:
1. ENTER_NOW if:
   - Clean breakout/breakdown with 2+ confirming candles in the trade direction.
   - Regime is aligned with the trade direction.
   - Price is not extended from the trigger area.
   - This is not a late chase.

2. WAIT_PULLBACK if:
   - Direction is valid but price is extended.
   - Setup is good, but entry quality is poor.
   - Better entry is likely near EMA, support, resistance retest, or trigger retest.

3. NO_TRADE if:
   - Setup is counter-regime without real reversal structure.
   - Candles are mixed or choppy.
   - Price is already near the target area.
   - News risk is nearby.
   - There is no clear edge.

CRITICAL OUTPUT RULES:
Return ONLY valid JSON.
No markdown.
No explanation outside JSON.

Return JSON in this EXACT format:
{{
  "decision": "ENTER_NOW" or "WAIT_PULLBACK" or "NO_TRADE",
  "setup": "LONG" or "SHORT" or "NONE",
  "confidence": "HIGH" or "MEDIUM" or "LOW",
  "entry_style": "BREAKOUT" or "PULLBACK" or "REVERSAL" or "NONE",
  "reason": "1-2 sentence explanation",
  "risk_comment": "Key risk or concern",
  "is_late_chase": true or false,
  "needs_pullback": true or false
}}

Example valid response:
{{
  "decision": "ENTER_NOW",
  "setup": "LONG",
  "confidence": "MEDIUM",
  "entry_style": "BREAKOUT",
  "reason": "Price has cleanly broken above the trigger level with 2 confirming bullish candles. Regime and EMA structure are aligned with the long setup.",
  "risk_comment": "Next resistance may limit upside, so Python must validate R:R before execution.",
  "is_late_chase": false,
  "needs_pullback": false
}}

Now review this setup and return ONLY the JSON decision:"""

        return prompt

    def _call_claude(self, prompt: str) -> str:
        """Call Claude API and return response text."""
        try:
            message = self.client.messages.create(
                model=self.model,
                max_tokens=500,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
            return message.content[0].text.strip()
        except Exception as e:
            raise RuntimeError(f"Claude API call failed: {e}")

    def _parse_response(self, response: str) -> Dict[str, Any]:
        """
        Parse Claude's response into structured dict.
        Handles accidental markdown code blocks.
        """
        try:
            cleaned = response.strip()

            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]

            if cleaned.startswith("```"):
                cleaned = cleaned[3:]

            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]

            cleaned = cleaned.strip()
            parsed = json.loads(cleaned)

            if not isinstance(parsed, dict):
                raise ValueError("Claude response JSON is not an object")

            return parsed

        except json.JSONDecodeError as e:
            print(f"[CLAUDE_REVIEWER] JSON parse error: {e}")
            print(f"[CLAUDE_REVIEWER] Response was: {response[:300]}")
            raise ValueError(f"Could not parse Claude response as JSON: {e}")

    def _to_bool(self, value: Any) -> bool:
        """
        Safely convert value to bool.
        Handles the Python gotcha: bool("false") == True.
        """
        if isinstance(value, bool):
            return value

        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1"}

        return bool(value)

    def _safe_float(self, value: Any) -> Optional[float]:
        """Convert numeric-ish values to float, otherwise return None."""
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _validate_decision(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate decision fields and convert invalid responses to safe NO_TRADE.
        """

        valid_decisions = {"ENTER_NOW", "WAIT_PULLBACK", "NO_TRADE"}
        valid_setups = {"LONG", "SHORT", "NONE"}
        valid_confidence = {"HIGH", "MEDIUM", "LOW"}
        valid_styles = {"BREAKOUT", "PULLBACK", "REVERSAL", "NONE"}

        required = [
            "decision",
            "setup",
            "confidence",
            "entry_style",
            "reason",
            "risk_comment",
            "is_late_chase",
            "needs_pullback",
        ]

        missing = [field for field in required if field not in decision]
        if missing:
            print(f"[CLAUDE_REVIEWER] Missing fields: {missing}")
            return self._fallback_response(f"Missing fields: {missing}")

        # Normalize string fields.
        for field in ["decision", "setup", "confidence", "entry_style"]:
            if isinstance(decision.get(field), str):
                decision[field] = decision[field].strip().upper()

        if decision["decision"] not in valid_decisions:
            print(f"[CLAUDE_REVIEWER] Invalid decision: {decision['decision']}")
            return self._fallback_response(f"Invalid decision: {decision['decision']}")

        if decision["setup"] not in valid_setups:
            print(f"[CLAUDE_REVIEWER] Invalid setup: {decision['setup']}")
            return self._fallback_response(f"Invalid setup: {decision['setup']}")

        if decision["confidence"] not in valid_confidence:
            print(f"[CLAUDE_REVIEWER] Invalid confidence: {decision['confidence']}")
            decision["confidence"] = "LOW"

        if decision["entry_style"] not in valid_styles:
            print(f"[CLAUDE_REVIEWER] Invalid entry_style: {decision['entry_style']}")
            decision["entry_style"] = "NONE"

        decision["reason"] = str(decision.get("reason", "No reason provided")).strip()
        decision["risk_comment"] = str(decision.get("risk_comment", "No risk comment")).strip()

        decision["is_late_chase"] = self._to_bool(decision["is_late_chase"])
        decision["needs_pullback"] = self._to_bool(decision["needs_pullback"])

        # Logical consistency.
        if decision["decision"] == "NO_TRADE":
            decision["setup"] = "NONE"
            decision["entry_style"] = "NONE"
            decision["confidence"] = "LOW"
            decision["is_late_chase"] = self._to_bool(decision.get("is_late_chase", False))
            decision["needs_pullback"] = False

        if decision["decision"] == "ENTER_NOW" and decision["setup"] == "NONE":
            print("[CLAUDE_REVIEWER] ENTER_NOW with setup=NONE")
            return self._fallback_response("Inconsistent decision: ENTER_NOW with no setup")

        if decision["decision"] == "WAIT_PULLBACK" and decision["setup"] == "NONE":
            print("[CLAUDE_REVIEWER] WAIT_PULLBACK with setup=NONE")
            return self._fallback_response("Inconsistent decision: WAIT_PULLBACK with no setup")

        if decision["decision"] == "WAIT_PULLBACK":
            decision["needs_pullback"] = True

        return decision

    def _text_claims_level_broken_above(self, text: str, level: Optional[float]) -> bool:
        """Detect if Claude appears to claim a given level was broken above."""
        if level is None:
            return False

        text = text.lower()

        for match in re.finditer(r"\b\d{3,5}(?:\.\d+)?\b", text):
            try:
                found = float(match.group(0))
            except ValueError:
                continue

            if abs(found - level) <= 0.1:
                window = text[max(0, match.start() - 45): match.end() + 45]
                bullish_words = ["above", "broke", "broken", "breakout", "cleared"]
                if any(word in window for word in bullish_words):
                    return True

        phrase_flags = [
            "above next resistance",
            "broke next resistance",
            "broken next resistance",
            "cleared next resistance",
            "breakout above next resistance",
        ]

        return any(phrase in text for phrase in phrase_flags)

    def _text_claims_level_broken_below(self, text: str, level: Optional[float]) -> bool:
        """Detect if Claude appears to claim a given level was broken below."""
        if level is None:
            return False

        text = text.lower()

        for match in re.finditer(r"\b\d{3,5}(?:\.\d+)?\b", text):
            try:
                found = float(match.group(0))
            except ValueError:
                continue

            if abs(found - level) <= 0.1:
                window = text[max(0, match.start() - 45): match.end() + 45]
                bearish_words = ["below", "broke", "broken", "breakdown", "cleared"]
                if any(word in window for word in bearish_words):
                    return True

        phrase_flags = [
            "below nearest support",
            "broke nearest support",
            "broken nearest support",
            "cleared nearest support",
            "breakdown below nearest support",
        ]

        return any(phrase in text for phrase in phrase_flags)

    def _sanity_check_against_snapshot(
        self,
        decision: Dict[str, Any],
        snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Validate Claude's decision against objective facts from Python.

        This catches:
        - News-risk ENTER_NOW decisions.
        - ENTER_NOW without actual confirmation.
        - LONG claims that price broke next resistance when it has not.
        - SHORT claims that price broke support when it has not.
        - Extended entries incorrectly marked ENTER_NOW.
        """

        if decision["decision"] == "NO_TRADE":
            return decision

        price = self._safe_float(snapshot.get("current_price", snapshot.get("price")))
        breakout_level = self._safe_float(snapshot.get("breakout_level"))
        next_resistance = self._safe_float(snapshot.get("next_resistance"))
        nearest_support = self._safe_float(snapshot.get("nearest_support"))

        news_nearby = self._to_bool(snapshot.get("news_nearby", False))
        breakout_confirmed = self._to_bool(snapshot.get("breakout_confirmed", False))
        breakdown_confirmed = self._to_bool(snapshot.get("breakdown_confirmed", False))

        candles_above = int(snapshot.get("candles_above_level", 0) or 0)
        candles_below = int(snapshot.get("candles_below_level", 0) or 0)

        extension_check = str(snapshot.get("extension_check", "")).lower()

        combined_text = f"{decision.get('reason', '')} {decision.get('risk_comment', '')}".lower()

        if news_nearby and decision["decision"] == "ENTER_NOW":
            print("[CLAUDE_REVIEWER] Sanity fail: ENTER_NOW during news risk")
            return self._fallback_response("Sanity check failed: news risk nearby")

        if decision["decision"] == "ENTER_NOW" and "extended" in extension_check:
            print("[CLAUDE_REVIEWER] Sanity fail: ENTER_NOW while extension_check says EXTENDED")
            return {
                "decision": "WAIT_PULLBACK",
                "setup": decision["setup"],
                "confidence": "LOW",
                "entry_style": "PULLBACK",
                "reason": "Setup direction may be valid, but Python extension check says price is extended from the trigger area.",
                "risk_comment": "Avoid chasing extended move. Wait for pullback or retest.",
                "is_late_chase": True,
                "needs_pullback": True,
            }

        if decision["decision"] == "ENTER_NOW" and decision["setup"] == "LONG":
            if not breakout_confirmed:
                print("[CLAUDE_REVIEWER] Sanity fail: LONG ENTER_NOW without breakout_confirmed")
                return self._fallback_response("Sanity check failed: LONG entry without confirmed breakout")

            if candles_above < 2:
                print("[CLAUDE_REVIEWER] Sanity fail: LONG ENTER_NOW with fewer than 2 candles above trigger")
                return self._fallback_response("Sanity check failed: LONG entry lacks 2 confirming candles")

            if price is not None and breakout_level is not None and price <= breakout_level:
                print("[CLAUDE_REVIEWER] Sanity fail: LONG ENTER_NOW but price is not above trigger")
                return self._fallback_response("Sanity check failed: price is not above long trigger level")

            if (
                price is not None
                and next_resistance is not None
                and price < next_resistance
                and self._text_claims_level_broken_above(combined_text, next_resistance)
            ):
                print("[CLAUDE_REVIEWER] Sanity fail: Claude claimed next resistance was broken")
                return self._fallback_response(
                    "Sanity check failed: Claude described price as above next_resistance when it is not"
                )

        if decision["decision"] == "ENTER_NOW" and decision["setup"] == "SHORT":
            if not breakdown_confirmed:
                print("[CLAUDE_REVIEWER] Sanity fail: SHORT ENTER_NOW without breakdown_confirmed")
                return self._fallback_response("Sanity check failed: SHORT entry without confirmed breakdown")

            if candles_below < 2:
                print("[CLAUDE_REVIEWER] Sanity fail: SHORT ENTER_NOW with fewer than 2 candles below trigger")
                return self._fallback_response("Sanity check failed: SHORT entry lacks 2 confirming candles")

            if price is not None and breakout_level is not None and price >= breakout_level:
                print("[CLAUDE_REVIEWER] Sanity fail: SHORT ENTER_NOW but price is not below trigger")
                return self._fallback_response("Sanity check failed: price is not below short trigger level")

            if (
                price is not None
                and nearest_support is not None
                and price > nearest_support
                and self._text_claims_level_broken_below(combined_text, nearest_support)
            ):
                print("[CLAUDE_REVIEWER] Sanity fail: Claude claimed nearest support was broken")
                return self._fallback_response(
                    "Sanity check failed: Claude described price as below nearest_support when it is not"
                )

        return decision

    def _fallback_response(self, error_msg: str) -> Dict[str, Any]:
        """Return safe NO_TRADE response when Claude fails or violates sanity checks."""
        return {
            "decision": "NO_TRADE",
            "setup": "NONE",
            "confidence": "LOW",
            "entry_style": "NONE",
            "reason": f"Claude reviewer error: {error_msg}",
            "risk_comment": "Invalid, unsafe, or inconsistent response from Claude",
            "is_late_chase": False,
            "needs_pullback": False,
        }


# ── TEST ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    """
    Run:
        python3 claude_reviewer.py
    """

    print("=" * 80)
    print("TESTING CLAUDE REVIEWER")
    print("=" * 80)

    reviewer = ClaudeReviewer()

    # Test 1: Clean bullish breakout
    print("\n[TEST 1] Clean bullish breakout above trigger level")
    print("-" * 80)

    snapshot_breakout = {
        "instrument": "XAU_USD",
        "current_price": 4732.5,
        "regime": "BULLISH",
        "daily_trend": "RANGING",
        "market_state": "BULLISH_TREND_IGNITION",
        "breakout_level": 4721.7,
        "next_resistance": 4750.0,
        "nearest_support": 4710.0,
        "at_key_level": True,
        "level_name": "today_high trigger",
        "breakout_confirmed": True,
        "breakdown_confirmed": False,
        "candles_above_level": 2,
        "candles_below_level": 0,
        "consecutive_bullish_candles": 3,
        "consecutive_bearish_candles": 0,
        "ema_alignment": "bullish",
        "price_vs_ema50": "above",
        "extension_check": "OK - within 1.2x ATR of EMA9",
        "distance_from_entry": 0,
        "session": "LONDON",
        "news_nearby": False,
    }

    decision1 = reviewer.review_setup(snapshot_breakout)
    print("\nClaude's Decision:")
    print(json.dumps(decision1, indent=2))

    # Test 1B: Local regression check for the exact level-confusion bug.
    print("\n[TEST 1B] Local sanity check: Claude falsely claims next resistance was broken")
    print("-" * 80)

    fake_bad_decision = {
        "decision": "ENTER_NOW",
        "setup": "LONG",
        "confidence": "HIGH",
        "entry_style": "BREAKOUT",
        "reason": "Clean breakout above 4750 resistance with bullish confirmation.",
        "risk_comment": "R:R should be checked by Python.",
        "is_late_chase": False,
        "needs_pullback": False,
    }

    checked_bad = reviewer._sanity_check_against_snapshot(fake_bad_decision, snapshot_breakout)
    print("\nSanity Check Result:")
    print(json.dumps(checked_bad, indent=2))

    # Test 2: Counter-regime / chop
    print("\n" + "=" * 80)
    print("[TEST 2] Counter-regime/choppy setup should reject")
    print("-" * 80)

    snapshot_counter = {
        "instrument": "XAU_USD",
        "current_price": 4745.0,
        "regime": "BULLISH",
        "daily_trend": "BULLISH",
        "market_state": "CHOP",
        "breakout_level": None,
        "next_resistance": 4750.0,
        "nearest_support": 4720.0,
        "at_key_level": True,
        "level_name": "near resistance",
        "breakout_confirmed": False,
        "breakdown_confirmed": False,
        "candles_above_level": 0,
        "candles_below_level": 0,
        "consecutive_bullish_candles": 1,
        "consecutive_bearish_candles": 1,
        "ema_alignment": "bullish",
        "price_vs_ema50": "above",
        "extension_check": "OK",
        "distance_from_entry": 0,
        "session": "NEW YORK",
        "news_nearby": False,
    }

    decision2 = reviewer.review_setup(snapshot_counter)
    print("\nClaude's Decision:")
    print(json.dumps(decision2, indent=2))

    # Test 3: Extended price
    print("\n" + "=" * 80)
    print("[TEST 3] Extended price should wait for pullback")
    print("-" * 80)

    snapshot_extended = {
        "instrument": "XAU_USD",
        "current_price": 4748.0,
        "regime": "BULLISH",
        "daily_trend": "BULLISH",
        "market_state": "BULLISH_TREND_IGNITION_EXTENDED",
        "breakout_level": 4710.0,
        "next_resistance": 4750.0,
        "nearest_support": 4710.0,
        "at_key_level": False,
        "level_name": "old trigger",
        "breakout_confirmed": True,
        "breakdown_confirmed": False,
        "candles_above_level": 5,
        "candles_below_level": 0,
        "consecutive_bullish_candles": 5,
        "consecutive_bearish_candles": 0,
        "ema_alignment": "bullish",
        "price_vs_ema50": "above",
        "extension_check": "EXTENDED - 2.5x ATR from EMA9",
        "distance_from_entry": 38,
        "session": "NEW YORK",
        "news_nearby": False,
    }

    decision3 = reviewer.review_setup(snapshot_extended)
    print("\nClaude's Decision:")
    print(json.dumps(decision3, indent=2))

    # Test 4: News nearby
    print("\n" + "=" * 80)
    print("[TEST 4] News nearby should reject")
    print("-" * 80)

    snapshot_news = {
        "instrument": "XAU_USD",
        "current_price": 4730.0,
        "regime": "BULLISH",
        "daily_trend": "BULLISH",
        "market_state": "NO_TRADE_NEWS_RISK",
        "breakout_level": None,
        "next_resistance": 4750.0,
        "nearest_support": 4710.0,
        "at_key_level": True,
        "level_name": "news risk, no valid trigger",
        "breakout_confirmed": False,
        "breakdown_confirmed": False,
        "candles_above_level": 0,
        "candles_below_level": 0,
        "consecutive_bullish_candles": 2,
        "consecutive_bearish_candles": 0,
        "ema_alignment": "bullish",
        "price_vs_ema50": "above",
        "extension_check": "OK",
        "distance_from_entry": 0,
        "session": "NEW YORK",
        "news_nearby": True,
    }

    decision4 = reviewer.review_setup(snapshot_news)
    print("\nClaude's Decision:")
    print(json.dumps(decision4, indent=2))

    print("\n" + "=" * 80)
    print("TESTS COMPLETE")
    print("=" * 80)
