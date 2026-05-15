"""
claude_reviewer.py - Structured Claude trade reviewer

Claude's job: Review market setup quality and return a decision.
Python's job: Calculate all prices, R:R, and validate execution.

This module is standalone and does NOT modify existing monitor scripts.
"""
import os
import json
from typing import Dict, Any, Optional
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()


class ClaudeReviewer:
    """
    Claude trade reviewer - returns structured decisions WITHOUT calculating prices.
    
    Input: Market snapshot dict with facts calculated by Python
    Output: Trading decision with reasoning
    """
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY not found in environment")
        self.client = Anthropic(api_key=self.api_key)
    
    def review_setup(self, market_snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """
        Review a market setup and return a structured trading decision.
        
        Args:
            market_snapshot: Dictionary containing market facts calculated by Python
            
        Returns:
            Decision dictionary with structure:
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
        try:
            prompt = self._build_prompt(market_snapshot)
            response = self._call_claude(prompt)
            decision = self._parse_response(response)
            validated = self._validate_decision(decision)
            return validated
            
        except Exception as e:
            print(f"[CLAUDE_REVIEWER] Error: {e}")
            return self._fallback_response(f"Error: {str(e)}")
    
    def _build_prompt(self, snapshot: Dict[str, Any]) -> str:
        """Build focused prompt asking Claude to review setup quality."""
        
        # Extract snapshot fields with safe defaults
        instrument = snapshot.get("instrument", "UNKNOWN")
        price = snapshot.get("price", 0)
        regime = snapshot.get("regime", "UNKNOWN")
        daily_trend = snapshot.get("daily_trend", "UNKNOWN")
        market_state = snapshot.get("market_state", "UNKNOWN")
        
        # Level context
        nearest_resistance = snapshot.get("nearest_resistance")
        nearest_support = snapshot.get("nearest_support")
        at_key_level = snapshot.get("at_key_level", False)
        level_name = snapshot.get("level_name", "")
        
        # Breakout/breakdown confirmation
        breakout_confirmed = snapshot.get("breakout_confirmed", False)
        breakdown_confirmed = snapshot.get("breakdown_confirmed", False)
        candles_above_level = snapshot.get("candles_above_level", 0)
        candles_below_level = snapshot.get("candles_below_level", 0)
        
        # Momentum
        consecutive_bullish = snapshot.get("consecutive_bullish_candles", 0)
        consecutive_bearish = snapshot.get("consecutive_bearish_candles", 0)
        
        # Structure
        ema_alignment = snapshot.get("ema_alignment", "neutral")
        price_vs_ema50 = snapshot.get("price_vs_ema50", "neutral")
        
        # Distance checks
        extension_check = snapshot.get("extension_check", "OK")
        distance_from_entry = snapshot.get("distance_from_entry", 0)
        
        # Context
        session = snapshot.get("session", "UNKNOWN")
        news_nearby = snapshot.get("news_nearby", False)
        
        prompt = f"""You are a trade setup reviewer. Your job is to evaluate if this setup is clean or messy.

DO NOT calculate entry, stop, or target prices - Python handles all arithmetic.
DO NOT calculate R:R - Python will do this.
If the setup requires knowing whether R:R is valid, do not guess. Mention the risk in risk_comment and let Python validate it.

Your job: Return a JSON decision about whether to enter now, wait for pullback, or skip.

INSTRUMENT: {instrument}
CURRENT PRICE: {price}
SESSION: {session}

MARKET STATE:
{market_state}

MARKET REGIME (5M/15M structure):
{regime}

DAILY TREND:
{daily_trend}

KEY LEVELS:
- Nearest resistance: {nearest_resistance}
- Nearest support: {nearest_support}
- At key level: {at_key_level} ({level_name})

BREAKOUT/BREAKDOWN STATUS:
- Breakout confirmed (close above resistance): {breakout_confirmed}
- Breakdown confirmed (close below support): {breakdown_confirmed}
- Candles above breakout level: {candles_above_level}
- Candles below breakdown level: {candles_below_level}

MOMENTUM:
- Consecutive bullish 5M candles: {consecutive_bullish}
- Consecutive bearish 5M candles: {consecutive_bearish}

STRUCTURE:
- EMA alignment: {ema_alignment}
- Price vs EMA50: {price_vs_ema50}

EXTENSION CHECK:
{extension_check}

DISTANCE / CHASE CONTEXT:
- Distance from original entry/breakout area: {distance_from_entry}

NEWS NEARBY: {news_nearby}

TRADING RULES:
1. ENTER_NOW if:
   - Clean breakout/breakdown with 2+ confirming candles in direction
   - Regime aligned (bullish regime + long, or bearish regime + short)
   - Not extended from entry point
   - Not a late chase (price hasn't already moved 80%+ to target)

2. WAIT_PULLBACK if:
   - Direction is correct but entry is extended
   - Good setup but waiting for better price (pullback to EMA or support/resistance)

3. NO_TRADE if:
   - Counter-regime without reversal structure
   - Mixed/choppy candles (no clear momentum)
   - Late chase (price already at target)
   - News risk nearby
   - No clear edge

CRITICAL: Return ONLY valid JSON. No markdown, no explanation outside JSON.

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
  "reason": "Clean breakout above resistance with 2 bullish candles confirming. Regime aligned.",
  "risk_comment": "Target nearby - R:R may be tight",
  "is_late_chase": false,
  "needs_pullback": false
}}

Now review this setup and return ONLY the JSON decision:"""

        return prompt
    
    def _call_claude(self, prompt: str) -> str:
        """Call Claude API and return response text."""
        try:
            message = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=500,
                temperature=0,
                messages=[{"role": "user", "content": prompt}]
            )
            return message.content[0].text.strip()
        except Exception as e:
            raise RuntimeError(f"Claude API call failed: {e}")
    
    def _parse_response(self, response: str) -> Dict[str, Any]:
        """
        Parse Claude's response into structured dict.
        Handles markdown code blocks and extracts JSON.
        """
        try:
            # Remove markdown code blocks if present
            cleaned = response.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]  # Remove ```json
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]   # Remove ```
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]  # Remove trailing ```
            
            cleaned = cleaned.strip()
            
            # Parse JSON
            parsed = json.loads(cleaned)
            return parsed
            
        except json.JSONDecodeError as e:
            print(f"[CLAUDE_REVIEWER] JSON parse error: {e}")
            print(f"[CLAUDE_REVIEWER] Response was: {response[:200]}")
            raise ValueError(f"Could not parse Claude response as JSON: {e}")
    
    def _to_bool(self, value: Any) -> bool:
        """
        Safely convert value to bool.
        Handles the Python gotcha: bool("false") == True
        """
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1"}
        return bool(value)
    
    def _validate_decision(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate decision fields and convert invalid responses to safe NO_TRADE.
        
        This ensures the bot never crashes on bad Claude output.
        """
        valid_decisions = {"ENTER_NOW", "WAIT_PULLBACK", "NO_TRADE"}
        valid_setups = {"LONG", "SHORT", "NONE"}
        valid_confidence = {"HIGH", "MEDIUM", "LOW"}
        valid_styles = {"BREAKOUT", "PULLBACK", "REVERSAL", "NONE"}
        
        # Check required fields exist
        required = ["decision", "setup", "confidence", "entry_style", "reason"]
        missing = [f for f in required if f not in decision]
        if missing:
            print(f"[CLAUDE_REVIEWER] Missing fields: {missing}")
            return self._fallback_response(f"Missing fields: {missing}")
        
        # Validate decision
        if decision["decision"] not in valid_decisions:
            print(f"[CLAUDE_REVIEWER] Invalid decision: {decision['decision']}")
            return self._fallback_response(f"Invalid decision: {decision['decision']}")
        
        # Validate setup
        if decision["setup"] not in valid_setups:
            print(f"[CLAUDE_REVIEWER] Invalid setup: {decision['setup']}")
            return self._fallback_response(f"Invalid setup: {decision['setup']}")
        
        # Validate confidence
        if decision["confidence"] not in valid_confidence:
            print(f"[CLAUDE_REVIEWER] Invalid confidence: {decision['confidence']}")
            decision["confidence"] = "LOW"  # Downgrade invalid confidence to LOW
        
        # Validate entry_style
        if decision["entry_style"] not in valid_styles:
            print(f"[CLAUDE_REVIEWER] Invalid entry_style: {decision['entry_style']}")
            decision["entry_style"] = "NONE"
        
        # Ensure boolean fields exist and are bool
        decision.setdefault("is_late_chase", False)
        decision.setdefault("needs_pullback", False)
        decision["is_late_chase"] = self._to_bool(decision["is_late_chase"])
        decision["needs_pullback"] = self._to_bool(decision["needs_pullback"])
        
        # Ensure text fields exist
        decision.setdefault("reason", "No reason provided")
        decision.setdefault("risk_comment", "No risk comment")
        
        # Logical consistency: NO_TRADE should have NONE setup
        if decision["decision"] == "NO_TRADE" and decision["setup"] != "NONE":
            print(f"[CLAUDE_REVIEWER] Inconsistent: NO_TRADE but setup={decision['setup']}, correcting to NONE")
            decision["setup"] = "NONE"
            decision["entry_style"] = "NONE"
        
        # Logical consistency: ENTER_NOW should have a setup
        if decision["decision"] == "ENTER_NOW" and decision["setup"] == "NONE":
            print(f"[CLAUDE_REVIEWER] Inconsistent: ENTER_NOW but setup=NONE, converting to NO_TRADE")
            return self._fallback_response("Inconsistent decision: ENTER_NOW with no setup")
        
        return decision
    
    def _fallback_response(self, error_msg: str) -> Dict[str, Any]:
        """Return safe NO_TRADE response when Claude fails."""
        return {
            "decision": "NO_TRADE",
            "setup": "NONE",
            "confidence": "LOW",
            "entry_style": "NONE",
            "reason": f"Claude reviewer error: {error_msg}",
            "risk_comment": "Invalid response from Claude",
            "is_late_chase": False,
            "needs_pullback": False,
        }


# ── TEST ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    """
    Test the Claude reviewer with a fake Gold market snapshot.
    
    Run this file directly to test: python3 claude_reviewer.py
    """
    
    print("=" * 80)
    print("TESTING CLAUDE REVIEWER")
    print("=" * 80)
    
    # Create reviewer instance
    reviewer = ClaudeReviewer()
    
    # Test Case 1: Clean bullish breakout
    print("\n[TEST 1] Clean bullish breakout above today's high")
    print("-" * 80)
    
    snapshot_breakout = {
        "instrument": "XAU_USD",
        "price": 4732.5,
        "regime": "BULLISH",
        "daily_trend": "RANGING",
        "market_state": "BULLISH_TREND_IGNITION",
        "nearest_resistance": 4750.0,
        "nearest_support": 4710.0,
        "at_key_level": True,
        "level_name": "today_high",
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
    
    # Test Case 2: Counter-regime short without reversal
    print("\n" + "=" * 80)
    print("[TEST 2] Counter-regime short in bullish regime (should reject)")
    print("-" * 80)
    
    snapshot_counter = {
        "instrument": "XAU_USD",
        "price": 4745.0,
        "regime": "BULLISH",
        "daily_trend": "BULLISH",
        "market_state": "CHOP",
        "nearest_resistance": 4750.0,
        "nearest_support": 4720.0,
        "at_key_level": True,
        "level_name": "resistance",
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
    
    # Test Case 3: Extended price - needs pullback
    print("\n" + "=" * 80)
    print("[TEST 3] Extended price - should wait for pullback")
    print("-" * 80)
    
    snapshot_extended = {
        "instrument": "XAU_USD",
        "price": 4748.0,
        "regime": "BULLISH",
        "daily_trend": "BULLISH",
        "market_state": "BULLISH_TREND_IGNITION_EXTENDED",
        "nearest_resistance": 4750.0,
        "nearest_support": 4710.0,
        "at_key_level": False,
        "level_name": "",
        "breakout_confirmed": True,
        "breakdown_confirmed": False,
        "candles_above_level": 5,
        "candles_below_level": 0,
        "consecutive_bullish_candles": 5,
        "consecutive_bearish_candles": 0,
        "ema_alignment": "bullish",
        "price_vs_ema50": "above",
        "extension_check": "EXTENDED - 2.5x ATR from EMA9",
        "distance_from_entry": 38,  # Already moved 38 points
        "session": "NEW YORK",
        "news_nearby": False,
    }
    
    decision3 = reviewer.review_setup(snapshot_extended)
    print("\nClaude's Decision:")
    print(json.dumps(decision3, indent=2))
    
    # Test Case 4: News nearby
    print("\n" + "=" * 80)
    print("[TEST 4] News nearby - should reject")
    print("-" * 80)
    
    snapshot_news = {
        "instrument": "XAU_USD",
        "price": 4730.0,
        "regime": "BULLISH",
        "daily_trend": "BULLISH",
        "market_state": "NO_TRADE_NEWS_RISK",
        "nearest_resistance": 4750.0,
        "nearest_support": 4710.0,
        "at_key_level": True,
        "level_name": "support",
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
