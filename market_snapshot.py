"""
market_snapshot.py - Python fact-builder for market conditions

Purpose:
    Calculate objective market facts that Claude can review.
    
    Does NOT ask Claude anything.
    Does NOT execute trades.
    Just takes candles/price/levels and turns them into clean facts.

Responsibilities:
    - Normalize candle data
    - Calculate EMAs
    - Count momentum candles
    - Detect key levels
    - Confirm breakouts/breakdowns
    - Detect extension/late chase risk
    - Label market state
    
Design:
    raw market data
    → Python calculations
    → market_snapshot dict
    → ClaudeReviewer reviews it
    → Python validates
    → DecisionAuditor records it
"""

import pandas as pd
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple


class MarketSnapshotBuilder:
    """
    Builds objective market snapshots from candle data.
    
    No AI. No execution. Just math and facts.
    """
    
    def __init__(self, instrument: str = "XAU_USD"):
        self.instrument = instrument
    
    def build_snapshot(
        self,
        candles_5m: List[Dict[str, Any]],
        candles_daily: Optional[List[Dict[str, Any]]] = None,
        current_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Build a market snapshot from candle data.
        
        Args:
            candles_5m: List of 5-minute candles (most recent last)
            candles_daily: Optional daily candles for trend context
            current_time: Optional current time (defaults to now UTC)
            
        Returns:
            market_snapshot dict ready for ClaudeReviewer
        """
        if not candles_5m or len(candles_5m) < 50:
            return self._insufficient_data_snapshot()
        
        # Normalize candles to consistent format
        df_5m = self._normalize_candles(candles_5m)
        
        if df_5m is None or len(df_5m) < 50:
            return self._insufficient_data_snapshot()
        
        current_time = current_time or datetime.now(timezone.utc)
        
        # Normalize to UTC to keep session detection safe
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        else:
            current_time = current_time.astimezone(timezone.utc)
        
        current_price = float(df_5m.iloc[-1]["close"])
        
        # Calculate EMAs
        df_5m = self._calculate_emas(df_5m)
        
        # Detect session
        session = self._detect_session(current_time)
        
        # Get today's high/low from PRIOR candles only.
        # Exclude last 3 candles to avoid including the breakout sequence itself.
        today_high, today_low = self._get_today_levels(df_5m, current_time, exclude_recent=3)
        
        # Get previous day high/low
        prev_high, prev_low = self._get_previous_day_levels(candles_daily)
        
        # Find key levels
        breakout_level, next_resistance, nearest_support, level_name = self._identify_key_levels(
            current_price,
            today_high,
            today_low,
            prev_high,
            prev_low,
        )
        
        # Confirm breakout/breakdown
        breakout_confirmed, candles_above = self._confirm_breakout(df_5m, breakout_level)
        breakdown_confirmed, candles_below = self._confirm_breakdown(df_5m, breakout_level)
        
        # Get trigger direction
        trigger_direction = self._get_trigger_direction(
            current_price,
            breakout_level,
            breakout_confirmed,
            breakdown_confirmed,
        )
        
        # Count momentum candles
        consecutive_bullish, consecutive_bearish = self._count_momentum_candles(df_5m)
        
        # Check EMA alignment
        ema_alignment = self._check_ema_alignment(df_5m)
        price_vs_ema50 = self._check_price_vs_ema50(current_price, df_5m)
        
        # Calculate extension
        extension_check, distance_from_entry = self._check_extension(
            current_price,
            breakout_level,
            df_5m,
        )
        
        # Determine regime
        regime = self._determine_regime(df_5m, ema_alignment)
        
        # Label market state
        market_state = self._label_market_state(
            current_price,
            breakout_level,
            next_resistance,
            nearest_support,
            breakout_confirmed,
            breakdown_confirmed,
            regime,
            extension_check,
        )
        
        # Build snapshot
        snapshot = {
            "instrument": self.instrument,
            "current_price": current_price,
            "session": session,
            
            "regime": regime,
            "daily_trend": "RANGING",  # TODO: implement daily trend detection
            "market_state": market_state,
            
            "breakout_level": breakout_level,
            "next_resistance": next_resistance,
            "nearest_support": nearest_support,
            "level_name": level_name,
            "trigger_direction": trigger_direction,
            
            "breakout_confirmed": breakout_confirmed,
            "breakdown_confirmed": breakdown_confirmed,
            "candles_above_level": candles_above,
            "candles_below_level": candles_below,
            
            "consecutive_bullish_candles": consecutive_bullish,
            "consecutive_bearish_candles": consecutive_bearish,
            
            "ema_alignment": ema_alignment,
            "price_vs_ema50": price_vs_ema50,
            
            "extension_check": extension_check,
            "distance_from_entry": distance_from_entry,
            
            "news_nearby": False,  # TODO: implement news detection
        }
        
        return snapshot
    
    def _normalize_candles(self, candles: List[Dict[str, Any]]) -> Optional[pd.DataFrame]:
        """
        Normalize candles from OANDA format to consistent DataFrame.
        
        Handles both:
            - OANDA format: {"mid": {"o": "4720.1", ...}}
            - Flat format: {"open": 4720.1, ...}
        """
        try:
            normalized = []
            
            for candle in candles:
                if "mid" in candle:
                    # OANDA format
                    mid = candle["mid"]
                    row = {
                        "time": candle.get("time", ""),
                        "open": float(mid["o"]),
                        "high": float(mid["h"]),
                        "low": float(mid["l"]),
                        "close": float(mid["c"]),
                    }
                else:
                    # Already flat
                    row = {
                        "time": candle.get("time", ""),
                        "open": float(candle["open"]),
                        "high": float(candle["high"]),
                        "low": float(candle["low"]),
                        "close": float(candle["close"]),
                    }
                
                normalized.append(row)
            
            df = pd.DataFrame(normalized)
            
            # Convert time to datetime if present.
            # Invalid/missing times become NaT, so _get_today_levels can fall back safely.
            if "time" in df.columns and len(df) > 0:
                df["time"] = pd.to_datetime(df["time"], errors="coerce", utc=True)
            
            return df
        
        except Exception as e:
            print(f"[SNAPSHOT] Failed to normalize candles: {e}")
            return None
    
    def _calculate_emas(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate EMA9, EMA26, EMA50."""
        df = df.copy()
        
        df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
        df["ema26"] = df["close"].ewm(span=26, adjust=False).mean()
        df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
        
        return df
    
    def _detect_session(self, current_time: datetime) -> str:
        """
        Detect trading session based on UTC time.
        
        LONDON: 07:00–12:00 UTC
        NEW YORK: 13:00–17:00 UTC
        OFF_HOURS: otherwise
        """
        hour = current_time.hour
        
        if 7 <= hour < 12:
            return "LONDON"
        elif 13 <= hour < 17:
            return "NEW YORK"
        else:
            return "OFF_HOURS"
    
    def _get_today_levels(
        self,
        df: pd.DataFrame,
        current_time: datetime,
        exclude_recent: int = 2,
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Get today's high and low using prior candles only.

        Excluding recent candles prevents the trigger level from moving during
        the breakout/breakdown confirmation candles.
        
        This fixes the moving goalpost bug where today_high moves up with the
        current candle, making it impossible to "break" it.
        """
        try:
            if "time" not in df.columns or df["time"].isna().all():
                today_candles = df.tail(100)
            else:
                today_start = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
                today_candles = df[df["time"] >= today_start]

            if len(today_candles) <= exclude_recent:
                return None, None

            # Use only prior candles, not the most recent ones
            prior_candles = today_candles.iloc[:-exclude_recent]

            today_high = float(prior_candles["high"].max())
            today_low = float(prior_candles["low"].min())

            return today_high, today_low

        except Exception as e:
            print(f"[SNAPSHOT] Failed to get today levels: {e}")
            return None, None
    
    def _get_previous_day_levels(
        self,
        candles_daily: Optional[List[Dict[str, Any]]],
    ) -> Tuple[Optional[float], Optional[float]]:
        """Get previous day high and low."""
        if not candles_daily or len(candles_daily) < 2:
            return None, None
        
        try:
            df_daily = self._normalize_candles(candles_daily)
            if df_daily is None or len(df_daily) < 2:
                return None, None
            
            # Get second-to-last day (last day is today, incomplete)
            prev_day = df_daily.iloc[-2]
            
            prev_high = float(prev_day["high"])
            prev_low = float(prev_day["low"])
            
            return prev_high, prev_low
        
        except Exception as e:
            print(f"[SNAPSHOT] Failed to get previous day levels: {e}")
            return None, None
    
    def _identify_key_levels(
        self,
        current_price: float,
        today_high: Optional[float],
        today_low: Optional[float],
        prev_high: Optional[float],
        prev_low: Optional[float],
    ) -> Tuple[Optional[float], Optional[float], Optional[float], str]:
        """
        Identify candidate trigger and surrounding levels.

        breakout_level is only assigned when price has broken beyond a real trigger area.

        next_resistance should be the nearest unbroken resistance above price.
        nearest_support should be the nearest support below price.
        """
        round_resistance = self._find_next_round_level(current_price, direction="up")
        round_support = self._find_next_round_level(current_price, direction="down")

        resistance_candidates = []
        support_candidates = []

        if today_high is not None:
            resistance_candidates.append(("today_high", today_high))
        if prev_high is not None:
            resistance_candidates.append(("prev_high", prev_high))

        if today_low is not None:
            support_candidates.append(("today_low", today_low))
        if prev_low is not None:
            support_candidates.append(("prev_low", prev_low))

        # Include round levels as fallback levels
        resistance_candidates.append(("round_resistance", round_resistance))
        support_candidates.append(("round_support", round_support))

        # Nearest unbroken resistance above current price
        unbroken_resistances = [
            (name, level)
            for name, level in resistance_candidates
            if level > current_price
        ]

        if unbroken_resistances:
            _, next_resistance = min(unbroken_resistances, key=lambda x: x[1])
        else:
            next_resistance = round_resistance

        # Nearest support below current price
        supports_below = [
            (name, level)
            for name, level in support_candidates + resistance_candidates
            if level < current_price
        ]

        if supports_below:
            _, nearest_support = max(supports_below, key=lambda x: x[1])
        else:
            nearest_support = round_support

        # LONG trigger: nearest broken resistance below current price
        broken_resistances = [
            (name, level)
            for name, level in resistance_candidates
            if level < current_price and name != "round_resistance"
        ]

        if broken_resistances:
            level_name, breakout_level = max(broken_resistances, key=lambda x: x[1])
            nearest_support = breakout_level
            return breakout_level, next_resistance, nearest_support, level_name

        # SHORT trigger: nearest broken support above current price
        broken_supports = [
            (name, level)
            for name, level in support_candidates
            if level > current_price and name != "round_support"
        ]

        if broken_supports:
            level_name, breakout_level = min(broken_supports, key=lambda x: x[1])
            next_resistance = breakout_level
            return breakout_level, next_resistance, nearest_support, level_name

        return None, next_resistance, nearest_support, "mid_range"
    
    def _find_next_round_level(self, price: float, direction: str) -> float:
        """
        Find next round level.
        
        For XAU/USD: round to nearest 50 points (4700, 4750, 4800, etc.)
        """
        if direction == "up":
            return ((int(price) // 50) + 1) * 50
        else:
            return (int(price) // 50) * 50
    
    def _confirm_breakout(
        self,
        df: pd.DataFrame,
        level: Optional[float],
    ) -> Tuple[bool, int]:
        """
        Confirm breakout above trigger level using consecutive recent closes.
        
        Requires consecutive candles - not just any 2 out of 5.
        """
        if level is None:
            return False, 0

        recent = df.tail(5)
        candles_above = 0

        # Count consecutive closes above level (starting from most recent)
        for _, candle in recent.iloc[::-1].iterrows():
            if candle["close"] > level:
                candles_above += 1
            else:
                break

        return candles_above >= 2, candles_above
    
    def _confirm_breakdown(
        self,
        df: pd.DataFrame,
        level: Optional[float],
    ) -> Tuple[bool, int]:
        """
        Confirm breakdown below trigger level using consecutive recent closes.
        
        Requires consecutive candles - not just any 2 out of 5.
        """
        if level is None:
            return False, 0

        recent = df.tail(5)
        candles_below = 0

        # Count consecutive closes below level (starting from most recent)
        for _, candle in recent.iloc[::-1].iterrows():
            if candle["close"] < level:
                candles_below += 1
            else:
                break

        return candles_below >= 2, candles_below
    
    def _count_momentum_candles(self, df: pd.DataFrame) -> Tuple[int, int]:
        """
        Count consecutive bullish/bearish candles.
        
        Returns:
            (consecutive_bullish, consecutive_bearish)
        """
        recent = df.tail(10)
        
        consecutive_bullish = 0
        consecutive_bearish = 0
        
        # Count bullish streak
        for _, candle in recent[::-1].iterrows():  # Reverse to count from most recent
            is_bullish = candle["close"] > candle["open"]
            
            if is_bullish:
                consecutive_bullish += 1
            else:
                break
        
        # Count bearish streak
        for _, candle in recent[::-1].iterrows():
            is_bearish = candle["close"] < candle["open"]
            
            if is_bearish:
                consecutive_bearish += 1
            else:
                break
        
        return consecutive_bullish, consecutive_bearish
    
    def _check_ema_alignment(self, df: pd.DataFrame) -> str:
        """
        Check EMA alignment.
        
        bullish = EMA9 > EMA26
        bearish = EMA9 < EMA26
        neutral = mixed
        """
        try:
            last_row = df.iloc[-1]
            
            ema9 = last_row["ema9"]
            ema26 = last_row["ema26"]
            
            if ema9 > ema26:
                return "bullish"
            elif ema9 < ema26:
                return "bearish"
            else:
                return "neutral"
        
        except Exception:
            return "neutral"
    
    def _check_price_vs_ema50(self, price: float, df: pd.DataFrame) -> str:
        """Check if price is above/below EMA50."""
        try:
            ema50 = df.iloc[-1]["ema50"]
            
            if price > ema50:
                return "above"
            elif price < ema50:
                return "below"
            else:
                return "at"
        
        except Exception:
            return "unknown"
    
    def _check_extension(
        self,
        current_price: float,
        breakout_level: Optional[float],
        df: pd.DataFrame,
    ) -> Tuple[str, float]:
        """
        Check if price is extended from trigger/EMA.
        
        Returns:
            (extension_check, distance_from_entry)
        """
        if breakout_level is None:
            return "UNKNOWN", 0
        
        distance = abs(current_price - breakout_level)
        
        try:
            ema9 = df.iloc[-1]["ema9"]
            distance_from_ema = abs(current_price - ema9)
            
            # Calculate ATR (simplified: average of last 10 candle ranges)
            recent = df.tail(10)
            atr = (recent["high"] - recent["low"]).mean()
            
            # Guard against zero/invalid ATR
            if atr is None or atr <= 0:
                return "UNKNOWN_ATR", distance
            
            # Check if extended
            if distance_from_ema > 2 * atr:
                return f"EXTENDED - {distance_from_ema / atr:.1f}x ATR from EMA9", distance
            elif distance_from_ema > 1.5 * atr:
                return f"MODERATELY EXTENDED - {distance_from_ema / atr:.1f}x ATR from EMA9", distance
            else:
                return f"OK - within {distance_from_ema / atr:.1f}x ATR of EMA9", distance
        
        except Exception as e:
            return "UNKNOWN", distance
    
    def _determine_regime(self, df: pd.DataFrame, ema_alignment: str) -> str:
        """
        Determine market regime.
        
        BULLISH = bullish EMA alignment
        BEARISH = bearish EMA alignment
        CHOP = neutral or mixed
        """
        if ema_alignment == "bullish":
            return "BULLISH"
        elif ema_alignment == "bearish":
            return "BEARISH"
        else:
            return "CHOP"
    
    def _label_market_state(
        self,
        current_price: float,
        breakout_level: Optional[float],
        next_resistance: Optional[float],
        nearest_support: Optional[float],
        breakout_confirmed: bool,
        breakdown_confirmed: bool,
        regime: str,
        extension_check: str,
    ) -> str:
        """
        Label the current market state.
        
        Examples:
            BULLISH_TREND_IGNITION
            BULLISH_TREND_CONTINUATION_EXTENDED
            BULLISH_EXTENDED
            BEARISH_BREAKDOWN
            CHOP
        """
        # Check extension explicitly
        is_extended = extension_check.startswith("EXTENDED")
        is_moderately_extended = extension_check.startswith("MODERATELY EXTENDED")
        
        # Bullish states
        if regime == "BULLISH" and breakout_confirmed:
            if is_extended:
                return "BULLISH_EXTENDED"
            elif is_moderately_extended:
                return "BULLISH_TREND_CONTINUATION_EXTENDED"
            else:
                return "BULLISH_TREND_IGNITION"
        
        # Bearish states
        elif regime == "BEARISH" and breakdown_confirmed:
            if is_extended:
                return "BEARISH_EXTENDED"
            elif is_moderately_extended:
                return "BEARISH_BREAKDOWN_EXTENDED"
            else:
                return "BEARISH_BREAKDOWN"
        
        # Choppy
        elif regime == "CHOP":
            return "CHOP"
        
        # Default
        else:
            return "RANGING"
    
    def _get_trigger_direction(
        self,
        current_price: float,
        breakout_level: Optional[float],
        breakout_confirmed: bool,
        breakdown_confirmed: bool,
    ) -> str:
        """
        Identify whether the active trigger is LONG, SHORT, or NONE.
        
        This makes Claude's job cleaner - it can see trigger_direction="LONG"
        instead of guessing from breakout_confirmed.
        """
        if breakout_level is None:
            return "NONE"

        if breakout_confirmed and current_price > breakout_level:
            return "LONG"

        if breakdown_confirmed and current_price < breakout_level:
            return "SHORT"

        return "NONE"
    
    def _insufficient_data_snapshot(self) -> Dict[str, Any]:
        """Return a NO_TRADE snapshot when data is insufficient."""
        return {
            "instrument": self.instrument,
            "current_price": 0,
            "session": "UNKNOWN",
            "regime": "UNKNOWN",
            "daily_trend": "UNKNOWN",
            "market_state": "INSUFFICIENT_DATA",
            "breakout_level": None,
            "next_resistance": None,
            "nearest_support": None,
            "level_name": "",
            "trigger_direction": "NONE",
            "breakout_confirmed": False,
            "breakdown_confirmed": False,
            "candles_above_level": 0,
            "candles_below_level": 0,
            "consecutive_bullish_candles": 0,
            "consecutive_bearish_candles": 0,
            "ema_alignment": "neutral",
            "price_vs_ema50": "unknown",
            "extension_check": "INSUFFICIENT_DATA",
            "distance_from_entry": 0,
            "news_nearby": False,
        }


# ── TEST ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    """
    Test the market snapshot builder with fake candles.
    
    Run:
        python3 market_snapshot.py
    """
    
    import json
    
    print("=" * 80)
    print("TESTING MARKET SNAPSHOT BUILDER")
    print("=" * 80)
    
    # Create fake bullish breakout scenario
    print("\n[TEST 1] Bullish breakout above today's high")
    print("-" * 80)
    
    # Generate realistic fake candles:
    # First 50 candles: range between 4700-4720 (no breakout)
    # Last 3 candles: break above 4720 with closes at 4722, 4725, 4727
    
    fake_candles = []
    
    # Phase 1: Ranging between 4700-4720
    for i in range(50):
        base = 4710
        noise = ((i * 37) % 20) - 10  # Pseudo-random noise
        
        open_price = base + noise
        close_price = open_price + ((i % 3) - 1)  # Mixed candles
        high_price = min(max(open_price, close_price) + 2, 4720)  # Cap at 4720
        low_price = max(min(open_price, close_price) - 2, 4700)   # Floor at 4700
        
        candle = {
            "mid": {
                "o": str(open_price),
                "h": str(high_price),
                "l": str(low_price),
                "c": str(close_price),
            },
            "time": f"2025-05-15T{7 + (i // 12):02d}:{(i % 12) * 5:02d}:00Z",
        }
        
        fake_candles.append(candle)
    
    # Phase 2: Breakout above 4720
    breakout_candles = [
        {"open": 4718, "high": 4723, "low": 4717, "close": 4722},  # First break
        {"open": 4722, "high": 4726, "low": 4721, "close": 4725},  # Confirmation 1
        {"open": 4725, "high": 4728, "low": 4724, "close": 4727},  # Confirmation 2
    ]
    
    for i, prices in enumerate(breakout_candles):
        candle = {
            "mid": {
                "o": str(prices["open"]),
                "h": str(prices["high"]),
                "l": str(prices["low"]),
                "c": str(prices["close"]),
            },
            "time": f"2025-05-15T{11 + (i // 12):02d}:{15 + (i % 12) * 5:02d}:00Z",
        }
        fake_candles.append(candle)
    
    builder = MarketSnapshotBuilder(instrument="XAU_USD")
    
    snapshot = builder.build_snapshot(
        candles_5m=fake_candles,
        candles_daily=None,
        current_time=datetime(2025, 5, 15, 11, 30, 0, tzinfo=timezone.utc),
    )
    
    print("\nSnapshot:")
    print(json.dumps(snapshot, indent=2))
    
    print("\n" + "=" * 80)
    print("KEY CHECKS:")
    print("=" * 80)
    print(f"✓ breakout_level should be ~4720: {snapshot['breakout_level']}")
    print(f"✓ current_price should be ~4727: {snapshot['current_price']}")
    print(f"✓ trigger_direction should be LONG: {snapshot['trigger_direction']}")
    print(f"✓ breakout_confirmed should be True: {snapshot['breakout_confirmed']}")
    print(f"✓ candles_above_level should be >= 2: {snapshot['candles_above_level']}")
    print(f"✓ level_name should be 'today_high': {snapshot['level_name']}")
    print(f"✓ market_state should include BULLISH: {snapshot['market_state']}")
    
    print("\n" + "=" * 80)
    print("TESTS COMPLETE")
    print("=" * 80)
    print("\nThis snapshot is ready for claude_reviewer.py to review.")
