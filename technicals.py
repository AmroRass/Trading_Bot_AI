"""
technicals.py - Computes trend signals from price data.

Improvements over v1:
  1. EMA slope confirmation — EMA9 must be pointing in trade direction
  2. Crossover recency — only trade if crossover happened within last 10 candles
  3. Price position — price must be on correct side of EMA50
  4. HTF confirmation — 1H EMA50 must agree with 15min signal
"""

import pandas as pd
import numpy as np
from datetime import datetime, timezone
from config import TRADE_CONFIG


def compute_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Compute ADX (trend strength indicator)."""
    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)

    dm_plus  = ((high - high.shift()) > (low.shift() - low)).astype(float) * (high - high.shift()).clip(lower=0)
    dm_minus = ((low.shift() - low) > (high - high.shift())).astype(float) * (low.shift() - low).clip(lower=0)

    atr      = tr.ewm(span=period, adjust=False).mean()
    di_plus  = 100 * dm_plus.ewm(span=period,  adjust=False).mean() / atr
    di_minus = 100 * dm_minus.ewm(span=period, adjust=False).mean() / atr

    dx  = (100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)).fillna(0)
    adx = dx.ewm(span=period, adjust=False).mean()
    return adx


def get_ema_slope(ema_series: pd.Series, lookback: int = 3) -> float:
    """
    Returns the slope of the EMA over the last N candles.
    Positive = pointing up, Negative = pointing down.
    """
    if len(ema_series) < lookback + 1:
        return 0.0
    return float(ema_series.iloc[-1] - ema_series.iloc[-lookback])


def get_crossover_age(ema_fast: pd.Series, ema_slow: pd.Series) -> int:
    """
    Returns how many candles ago the last EMA crossover happened.
    Returns 999 if no crossover found in recent history.
    """
    diff = ema_fast - ema_slow
    sign = np.sign(diff)
    sign_change = sign.diff().abs()

    # Find most recent crossover
    crossover_indices = sign_change[sign_change > 0].index
    if len(crossover_indices) == 0:
        return 999

    last_crossover = crossover_indices[-1]
    all_indices = ema_fast.index.tolist()
    if last_crossover not in all_indices:
        return 999

    age = len(all_indices) - all_indices.index(last_crossover) - 1
    return age


def get_htf_direction(df_1h: pd.DataFrame) -> str:
    """
    Checks 1H chart trend direction using EMA50.
    Returns 'bullish', 'bearish', or 'unknown'.
    """
    if df_1h is None or df_1h.empty or len(df_1h) < 55:
        return "unknown"

    ema50        = compute_ema(df_1h["close"], 50)
    latest_close = df_1h["close"].iloc[-1]
    latest_ema   = ema50.iloc[-1]

    if latest_close > latest_ema:
        return "bullish"
    elif latest_close < latest_ema:
        return "bearish"
    return "unknown"


def is_trading_session() -> bool:
    """Returns True if current UTC time is within configured session hours."""
    from config import SESSION_CONFIG
    if not SESSION_CONFIG["enabled"]:
        return True
    hour = datetime.now(timezone.utc).hour
    return SESSION_CONFIG["start_hour_utc"] <= hour < SESSION_CONFIG["end_hour_utc"]


def get_trend_signal(df: pd.DataFrame, df_1h: pd.DataFrame = None) -> dict:
    """
    Returns trend direction and strength based on EMA crossover + ADX.

    Confirmation requires ALL of:
      1. ADX >= threshold (trend is strong)
      2. EMA9 slope agrees with direction (trend is still moving)
      3. Crossover happened within last 10 candles (signal is fresh)
      4. Price on correct side of EMA50 (price confirms the trend)
      5. HTF agrees (1H EMA50 points same direction) if enabled
      6. In session (if session filter enabled)

    Returns:
      direction:        "bullish" | "bearish" | "neutral"
      strength:         ADX value
      confirmed:        bool - all conditions passed
      reject_reason:    why confirmed=False (for logging)
      htf_direction:    direction of 1H chart
      htf_agrees:       bool
      in_session:       bool
      close:            latest price
    """
    empty = {
        "direction": "neutral", "strength": 0, "confirmed": False,
        "reject_reason": "Insufficient data",
        "htf_direction": "unknown", "htf_agrees": False,
        "in_session": False, "close": 0
    }

    if df.empty or len(df) < TRADE_CONFIG["ema_slow"] + 10:
        return empty

    ema_fast = compute_ema(df["close"], TRADE_CONFIG["ema_fast"])
    ema_slow = compute_ema(df["close"], TRADE_CONFIG["ema_slow"])
    adx      = compute_adx(df, TRADE_CONFIG["adx_period"])

    latest_fast  = ema_fast.iloc[-1]
    latest_slow  = ema_slow.iloc[-1]
    latest_adx   = adx.iloc[-1]
    latest_close = df["close"].iloc[-1]

    # 1. Direction from EMA crossover
    if latest_fast > latest_slow:
        direction = "bullish"
    elif latest_fast < latest_slow:
        direction = "bearish"
    else:
        direction = "neutral"

    # 2. EMA9 slope — must point in trade direction
    slope = get_ema_slope(ema_fast, lookback=3)
    slope_agrees = (direction == "bullish" and slope > 0) or \
                   (direction == "bearish" and slope < 0)

    # 3. Crossover recency — must be within last 10 candles
    crossover_age = get_crossover_age(ema_fast, ema_slow)
    crossover_fresh = crossover_age <= 10

    # 4. Price position — price must be on correct side of EMA50
    price_agrees = (direction == "bullish" and latest_close > latest_slow) or \
                   (direction == "bearish" and latest_close < latest_slow)

    # 5. HTF confirmation
    in_session    = is_trading_session()
    htf_direction = get_htf_direction(df_1h)

    if TRADE_CONFIG.get("htf_confirmation", False) and htf_direction != "unknown":
        htf_agrees = htf_direction == direction
    else:
        htf_agrees = True

    # 6. ADX threshold
    adx_ok = latest_adx >= TRADE_CONFIG["adx_threshold"]

    # Build reject reason for logging
    reject_reasons = []
    if direction == "neutral":
        reject_reasons.append("No EMA crossover")
    if not adx_ok:
        reject_reasons.append(f"ADX too low ({latest_adx:.1f}<{TRADE_CONFIG['adx_threshold']})")
    if not slope_agrees:
        reject_reasons.append(f"EMA slope disagrees (slope={slope:.2f})")
    if not crossover_fresh:
        reject_reasons.append(f"Crossover too old ({crossover_age} candles ago)")
    if not price_agrees:
        reject_reasons.append("Price on wrong side of EMA50")
    if not htf_agrees:
        reject_reasons.append(f"HTF disagrees (1H={htf_direction}, 15m={direction})")
    if not in_session:
        reject_reasons.append("Outside session hours")

    # All conditions must pass
    confirmed = (
        adx_ok and
        direction != "neutral" and
        slope_agrees and
        crossover_fresh and
        price_agrees and
        htf_agrees and
        in_session
    )

    return {
        "direction":      direction,
        "strength":       round(latest_adx, 2),
        "ema_fast":       round(latest_fast, 4),
        "ema_slow":       round(latest_slow, 4),
        "slope":          round(slope, 4),
        "crossover_age":  crossover_age,
        "confirmed":      confirmed,
        "reject_reason":  " | ".join(reject_reasons) if reject_reasons else "All conditions met",
        "htf_direction":  htf_direction,
        "htf_agrees":     htf_agrees,
        "in_session":     in_session,
        "close":          round(latest_close, 4),
    }
