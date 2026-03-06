"""
technicals.py - Top-down trend analysis for gold trading.

Strategy:
  1. Daily EMA50 = macro bias (bullish = buys only, bearish = sells only)
  2. 1H EMA50 = session bias (must agree with daily)
  3. 5min EMA9/50 + ADX + slope + price position = entry timing
  4. ATR volatility filter — normal/elevated/extreme regimes
  5. Real gold market hours check
  6. Session filter — London + NY only
"""

import pandas as pd
import numpy as np
from datetime import datetime, timezone
from config import TRADE_CONFIG


# ─── Core Indicators ──────────────────────────────────────────────────────────

def compute_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)

    dm_plus  = ((high - high.shift()) > (low.shift() - low)).astype(float) * \
               (high - high.shift()).clip(lower=0)
    dm_minus = ((low.shift() - low) > (high - high.shift())).astype(float) * \
               (low.shift() - low).clip(lower=0)

    atr      = tr.ewm(span=period, adjust=False).mean()
    di_plus  = 100 * dm_plus.ewm(span=period,  adjust=False).mean() / atr
    di_minus = 100 * dm_minus.ewm(span=period, adjust=False).mean() / atr

    dx  = (100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)).fillna(0)
    adx = dx.ewm(span=period, adjust=False).mean()
    return adx


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)

    return tr.ewm(span=period, adjust=False).mean()


def get_ema_slope(ema_series: pd.Series, lookback: int = 5) -> float:
    """Returns slope of EMA over last N candles. Positive = up, Negative = down."""
    if len(ema_series) < lookback + 1:
        return 0.0
    return float(ema_series.iloc[-1] - ema_series.iloc[-lookback])


# ─── Session & Market Hours ───────────────────────────────────────────────────

def is_market_open() -> bool:
    """
    Returns True during real gold market hours.
    Gold trades Sunday 22:00 UTC to Friday 22:00 UTC.
    Daily maintenance break: 22:00-23:00 UTC.
    """
    now     = datetime.now(timezone.utc)
    weekday = now.weekday()  # 0=Mon, 6=Sun
    hour    = now.hour

    if weekday == 5:                    # Saturday — always closed
        return False
    if weekday == 6 and hour < 22:      # Sunday before 22:00 — closed
        return False
    if weekday == 4 and hour >= 22:     # Friday after 22:00 — closed
        return False
    if hour == 22:                      # Daily maintenance break
        return False

    return True


def is_trading_session() -> bool:
    """
    Returns True during high liquidity gold sessions.
    London: 07:00-12:00 UTC
    NY:     13:30-17:00 UTC
    """
    if not is_market_open():
        return False

    now  = datetime.now(timezone.utc)
    hour = now.hour
    minute = now.minute
    time_decimal = hour + minute / 60.0

    london = 7.0 <= time_decimal < 12.0
    ny     = 13.5 <= time_decimal < 17.0

    return london or ny


# ─── Volatility Regime ────────────────────────────────────────────────────────

def get_volatility_regime(df_5m: pd.DataFrame) -> dict:
    """
    Calculates ATR-based volatility regime.

    Returns:
        regime:       "normal" | "elevated" | "extreme"
        atr_current:  current 5min ATR
        atr_average:  20-period average ATR
        atr_ratio:    current / average
        sl_multiplier: dynamic SL multiplier for elevated regime
    """
    atr     = compute_atr(df_5m, period=14)
    current = atr.iloc[-1]
    average = atr.iloc[-20:].mean()
    ratio   = current / average if average > 0 else 1.0

    if ratio >= 3.0:
        regime = "extreme"
    elif ratio >= 1.5:
        regime = "elevated"
    else:
        regime = "normal"

    # Dynamic SL = 1.5x ATR as percentage of price
    price         = df_5m["close"].iloc[-1]
    dynamic_sl    = (current * 1.5) / price if price > 0 else TRADE_CONFIG["stop_loss_pct"]

    return {
        "regime":       regime,
        "atr_current":  round(current, 4),
        "atr_average":  round(average, 4),
        "atr_ratio":    round(ratio, 2),
        "dynamic_sl":   round(dynamic_sl, 5),
    }


# ─── Daily Trend ──────────────────────────────────────────────────────────────

def get_daily_bias(df_daily: pd.DataFrame) -> dict:
    """
    Returns macro trend direction from daily EMA50.
    bullish = price above EMA50 and slope positive
    bearish = price below EMA50 and slope negative
    """
    if df_daily is None or df_daily.empty or len(df_daily) < 15:
        return {"direction": "unknown", "slope": 0.0, "ema50": 0.0}

    ema50        = compute_ema(df_daily["close"], 10)
    latest_close = df_daily["close"].iloc[-1]
    latest_ema   = ema50.iloc[-1]
    slope        = get_ema_slope(ema50, lookback=5)

    if latest_close > latest_ema and slope > 0:
        direction = "bullish"
    elif latest_close < latest_ema and slope < 0:
        direction = "bearish"
    else:
        direction = "neutral"  # conflicting — price above but slope down or vice versa

    return {
        "direction": direction,
        "slope":     round(slope, 4),
        "ema50":     round(latest_ema, 4),
        "close":     round(latest_close, 4),
    }


# ─── 1H Trend ─────────────────────────────────────────────────────────────────

def get_htf_bias(df_1h: pd.DataFrame) -> dict:
    """
    Returns session trend direction from 1H EMA50.
    """
    if df_1h is None or df_1h.empty or len(df_1h) < 55:
        return {"direction": "unknown", "slope": 0.0, "ema50": 0.0}

    ema50        = compute_ema(df_1h["close"], 50)
    latest_close = df_1h["close"].iloc[-1]
    latest_ema   = ema50.iloc[-1]
    slope        = get_ema_slope(ema50, lookback=5)

    if latest_close > latest_ema:
        direction = "bullish"
    elif latest_close < latest_ema:
        direction = "bearish"
    else:
        direction = "neutral"

    return {
        "direction": direction,
        "slope":     round(slope, 4),
        "ema50":     round(latest_ema, 4),
        "close":     round(latest_close, 4),
    }


# ─── 5min Signal ──────────────────────────────────────────────────────────────

def get_trend_signal(df_5m: pd.DataFrame, df_1h: pd.DataFrame = None, df_daily: pd.DataFrame = None) -> dict:
    """
    Full top-down trend analysis.

    Returns complete signal dict including:
    - daily_bias:     macro direction
    - htf_bias:       1H direction
    - direction:      5min EMA crossover direction
    - confirmed:      True if ALL conditions pass
    - trade_bias:     final allowed trade direction ("buy", "sell", or None)
    - volatility:     regime dict
    - in_session:     bool
    - reject_reason:  why confirmed=False
    """
    empty = {
        "direction": "neutral", "strength": 0, "confirmed": False,
        "trade_bias": None, "reject_reason": "Insufficient data",
        "daily_bias": {"direction": "unknown"},
        "htf_bias":   {"direction": "unknown"},
        "volatility": {"regime": "normal", "atr_ratio": 1.0},
        "in_session": False, "close": 0, "slope": 0,
    }

    if df_5m is None or df_5m.empty or len(df_5m) < TRADE_CONFIG["ema_slow"] + 20:
        return empty

    # ── Indicators ──
    ema_fast     = compute_ema(df_5m["close"], TRADE_CONFIG["ema_fast"])
    ema_slow     = compute_ema(df_5m["close"], TRADE_CONFIG["ema_slow"])
    adx          = compute_adx(df_5m, TRADE_CONFIG["adx_period"])
    latest_fast  = ema_fast.iloc[-1]
    latest_slow  = ema_slow.iloc[-1]
    latest_adx   = adx.iloc[-1]
    latest_close = df_5m["close"].iloc[-1]
    slope        = get_ema_slope(ema_fast, lookback=5)

    # ── 5min direction ──
    if latest_fast > latest_slow:
        direction = "bullish"
    elif latest_fast < latest_slow:
        direction = "bearish"
    else:
        direction = "neutral"

    # ── Higher timeframe bias ──
    daily_bias = get_daily_bias(df_daily)
    htf_bias   = get_htf_bias(df_1h)

    # ── Volatility regime ──
    volatility = get_volatility_regime(df_5m)

    # ── Session ──
    in_session = is_trading_session()

    # ── Checks ──
    adx_ok        = latest_adx >= TRADE_CONFIG["adx_threshold"]
    slope_agrees  = (direction == "bullish" and slope > 0) or \
                    (direction == "bearish" and slope < 0)
    price_agrees  = (direction == "bullish" and latest_close > latest_slow) or \
                    (direction == "bearish" and latest_close < latest_slow)
    daily_agrees  = daily_bias["direction"] == direction
    htf_agrees    = htf_bias["direction"] == direction
    not_extreme   = volatility["regime"] != "extreme"

    # ── Reject reasons ──
    reject_reasons = []
    if direction == "neutral":
        reject_reasons.append("No EMA crossover")
    if not adx_ok:
        reject_reasons.append(f"ADX too low ({latest_adx:.1f}<{TRADE_CONFIG['adx_threshold']})")
    if not slope_agrees:
        reject_reasons.append(f"EMA slope disagrees (slope={slope:.2f})")
    if not price_agrees:
        reject_reasons.append("Price on wrong side of EMA50")
    if not daily_agrees:
        reject_reasons.append(f"Daily bias disagrees (daily={daily_bias['direction']}, 5min={direction})")
    if not htf_agrees:
        reject_reasons.append(f"1H bias disagrees (1H={htf_bias['direction']}, 5min={direction})")
    if not not_extreme:
        reject_reasons.append(f"Extreme volatility (ATR ratio={volatility['atr_ratio']}x)")
    if not in_session:
        reject_reasons.append("Outside session hours")

    confirmed = (
        direction != "neutral" and
        adx_ok and
        slope_agrees and
        price_agrees and
        daily_agrees and
        htf_agrees and
        not_extreme and
        in_session
    )

    # Trade bias — what direction are we allowed to trade
    trade_bias = None
    if confirmed:
        trade_bias = "buy" if direction == "bullish" else "sell"

    return {
        "direction":    direction,
        "strength":     round(latest_adx, 2),
        "ema_fast":     round(latest_fast, 4),
        "ema_slow":     round(latest_slow, 4),
        "slope":        round(slope, 4),
        "confirmed":    confirmed,
        "trade_bias":   trade_bias,
        "reject_reason": " | ".join(reject_reasons) if reject_reasons else "All conditions met",
        "daily_bias":   daily_bias,
        "htf_bias":     htf_bias,
        "volatility":   volatility,
        "in_session":   in_session,
        "close":        round(latest_close, 4),
    }
