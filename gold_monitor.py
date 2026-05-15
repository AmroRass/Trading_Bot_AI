"""
gold_monitor.py - XAU/USD trade monitor (FIXED with regime awareness)

Account 005: Manual only — alerts
Account 006: Auto-execution with full hardened guards

FIXES APPLIED (P0 - CRITICAL):
  ✅ Execution regime classification with OR logic (not overly strict AND)
  ✅ Regime rules added to Claude prompt
  ✅ Console logging for regime blocks
  ✅ Extension check uses current close (not proposed entry)
  ✅ Duplicate key includes regime to allow regime-change re-entries

Key improvements merged from GPT version:
  - Completed candles only (no half-formed candles)
  - Live bid/ask price for execution validation
  - Strict Claude output validation
  - Live R:R revalidation with spread buffer
  - Daily loss cap, max trades/day, max consecutive losses
  - Cooldown after loss, duplicate signal suppression
  - Durable state in logs/ dir
  - Kill switch file (STOP_BOT)
  - 5M candle confirmation enforced in code
  - Max stop width cap
  - temperature=0 for Claude
  - Risk-based unit sizing for execution

Our additions preserved:
  - Full historical examples in prompt
  - Inter-signal state string
  - EMA50 context
  - Breakeven manager + session close
  - Support block by trend
  - Nearest structural level rule
  - Signal logging to API
  - Opposing signal warning
  - Simplified Telegram format
"""
import os
import json
import math
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import oandapyV20.endpoints.accounts as accounts_ep
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.trades as trades_ep
import oandapyV20.endpoints.pricing as pricing_ep
from dotenv import load_dotenv
from anthropic import Anthropic
load_dotenv()

# ── ENV ────────────────────────────────────────────────────────────────────────
OANDA_ACCESS_TOKEN = os.getenv("OANDA_ACCESS_TOKEN")
OANDA_ACCOUNT_ID   = os.getenv("OANDA_ACCOUNT_ID")
OANDA_ENVIRONMENT  = os.getenv("OANDA_ENVIRONMENT", "practice")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")
FINNHUB_API_KEY    = os.getenv("FINNHUB_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

# ── CONFIG ─────────────────────────────────────────────────────────────────────
INSTRUMENT        = "XAU_USD"
SHADOW_ACCOUNT_ID = "101-004-37417354-006"
SHADOW_UNITS      = 5          # fixed for shadow — consistent tracking
ACCOUNT_SIZE      = 10000
RISK_PCT          = 0.01       # 1% risk
MIN_RR            = 2.5
MIN_STOP_PTS      = 20.0
MAX_STOP_PTS      = 80.0
LEVEL_PROXIMITY   = 25.0
NEWS_BUFFER_MIN   = 30
SPREAD_BUFFER     = 0.5        # slippage buffer for live R:R check

# FTMO-style daily risk controls
DAILY_LOSS_CAP_PCT       = 0.04   # 4% daily loss cap ($400 on $10k)
MAX_TRADES_PER_DAY       = 3
MAX_CONSECUTIVE_LOSSES   = 2
COOLDOWN_AFTER_LOSS_MIN  = 45
DUPLICATE_COOLDOWN_MIN   = 60
DUPLICATE_PRICE_MOVE_PTS = 10.0

LONDON_START = 7
LONDON_END   = 12
NY_START     = 13
NY_END       = 17

RESISTANCE_LEVELS = {'today_high', 'prev_high'}
SUPPORT_LEVELS    = {'today_low', 'prev_low'}

GOLD_RELEVANT_EVENTS = [
    "fomc", "federal reserve", "fed rate", "interest rate decision",
    "cpi", "consumer price", "inflation", "nfp", "non-farm", "payroll",
    "ppi", "producer price", "gdp", "unemployment", "jobless",
    "powell", "fed chair", "treasury"
]

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
LOG_DIR    = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
STATE_FILE      = os.path.join(LOG_DIR, "gold_state.json")
KILL_SWITCH     = os.path.join(BASE_DIR, "STOP_BOT")
EC2_API         = "http://localhost:5000"

oanda = oandapyV20.API(access_token=OANDA_ACCESS_TOKEN, environment=OANDA_ENVIRONMENT)


# ── STATE ──────────────────────────────────────────────────────────────────────

def today_key():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def default_state():
    return {
        "recent_signals": [],
        "daily": {
            "date": today_key(),
            "start_nav": None,
            "executed_trades": 0,
            "consecutive_losses": 0,
            "last_loss_time": None,
        },
        "duplicate_signals": {},
    }

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                state = json.load(f)
        else:
            state = default_state()
    except:
        state = default_state()
    base = default_state()
    for k, v in base.items():
        state.setdefault(k, v)
    for k, v in base["daily"].items():
        state["daily"].setdefault(k, v)
    if state["daily"].get("date") != today_key():
        state["daily"] = default_state()["daily"]
    return state

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[STATE] Save failed: {e}")

def parse_dt(value):
    if not value: return None
    try: return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except: return None

def safe_float(value, default=None):
    try:
        if value is None: return default
        s = str(value).replace(",","").replace(":1","").strip()
        if s.upper() in {"N/A","NA","NONE","—",""}: return default
        return float(s)
    except: return default


# ── HELPERS ────────────────────────────────────────────────────────────────────

def get_session(hour_utc):
    if LONDON_START <= hour_utc < LONDON_END: return "LONDON"
    if NY_START <= hour_utc < NY_END: return "NEW YORK"
    return "OFF-HOURS"

def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4096], "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"[GOLD] Telegram error: {e}")


# ── MARKET DATA ────────────────────────────────────────────────────────────────

def get_candles(granularity, count, completed_only=True):
    params = {"count": count, "granularity": granularity, "price": "M"}
    r = instruments.InstrumentsCandles(INSTRUMENT, params=params)
    rv = oanda.request(r)
    rows = []
    for c in rv.get("candles", []):
        if completed_only and not c.get("complete", False):
            continue
        mid = c.get("mid", {})
        rows.append({"time": pd.to_datetime(c["time"]), "open": float(mid.get("o",0)),
                     "high": float(mid.get("h",0)), "low": float(mid.get("l",0)),
                     "close": float(mid.get("c",0)), "volume": int(c.get("volume",0))})
    df = pd.DataFrame(rows)
    if df.empty: return df
    df.set_index("time", inplace=True)
    return df

def get_live_price(allow_fallback=True):
    try:
        r = pricing_ep.PricingInfo(accountID=SHADOW_ACCOUNT_ID, params={"instruments": INSTRUMENT})
        rv = oanda.request(r)
        prices = rv.get("prices", [])
        if prices:
            p = prices[0]
            bid = float(p.get("closeoutBid") or p["bids"][0]["price"])
            ask = float(p.get("closeoutAsk") or p["asks"][0]["price"])
            return {"bid": bid, "ask": ask, "mid": (bid+ask)/2, "spread": ask-bid}
    except Exception as e:
        print(f"[PRICE] Live pricing failed: {e}")
    
    if not allow_fallback:
        raise RuntimeError("Live bid/ask unavailable — execution blocked")
    
    df = get_candles("M1", 3, completed_only=True)
    if not df.empty:
        mid = float(df.iloc[-1]["close"])
        return {"bid": mid, "ask": mid, "mid": mid, "spread": 0.0}
    raise RuntimeError("Could not fetch live price")

def get_account_balance(account_id=None):
    try:
        r = accounts_ep.AccountSummary(account_id or OANDA_ACCOUNT_ID)
        rv = oanda.request(r)
        return float(rv["account"]["balance"]), float(rv["account"].get("NAV", rv["account"]["balance"]))
    except: return ACCOUNT_SIZE, ACCOUNT_SIZE

def get_open_position(account_id=None):
    try:
        acct = account_id or OANDA_ACCOUNT_ID
        r = trades_ep.OpenTrades(acct)
        rv = oanda.request(r)
        open_trades = [t for t in rv.get("trades", []) if t.get("instrument") == INSTRUMENT]
        if not open_trades: return None
        total_units = sum(float(t["currentUnits"]) for t in open_trades)
        total_pl    = sum(float(t.get("unrealizedPL", 0)) for t in open_trades)
        return {
            "units": abs(total_units),
            "side": "LONG" if total_units > 0 else "SHORT",
            "unrealized_pl": total_pl,
            "trade_count": len(open_trades),
        }
    except Exception as e:
        print(f"[GOLD] Could not fetch open trades for {account_id}: {e}")
        return None


# ── LEVELS / CONTEXT ───────────────────────────────────────────────────────────

def get_key_levels(df_15m, df_daily):
    today = datetime.now(timezone.utc).date()
    tb = df_15m[df_15m.index.date == today]
    
    # Exclude latest completed candle so breakout can be detected
    # Without this, today_high moves up with price and bot always sees "near resistance"
    tb_prev = tb.iloc[:-1] if len(tb) > 1 else tb
    
    prev_high = prev_low = None
    if len(df_daily) >= 2:
        pd_row = df_daily.iloc[-2]
        prev_high = float(pd_row["high"])
        prev_low  = float(pd_row["low"])
    
    return {
        "today_high": float(tb_prev["high"].max()) if not tb_prev.empty else None,
        "today_low":  float(tb_prev["low"].min())  if not tb_prev.empty else None,
        "prev_high":  prev_high,
        "prev_low":   prev_low,
    }


def has_confirmed_breakout(df_15m, level, buffer_pts=2.0):
    """Check if price has broken above a level with momentum."""
    if df_15m is None or len(df_15m) < 2:
        return False
    
    last = df_15m.iloc[-1]
    prev = df_15m.iloc[-2]
    
    last_close = float(last["close"])
    prev_close = float(prev["close"])
    
    # Breakout: previous candle below level, current candle above level + buffer
    return prev_close <= level and last_close > level + buffer_pts


def has_confirmed_breakdown(df_15m, level, buffer_pts=2.0):
    """Check if price has broken below a level with momentum."""
    if df_15m is None or len(df_15m) < 2:
        return False
    
    last = df_15m.iloc[-1]
    prev = df_15m.iloc[-2]
    
    last_close = float(last["close"])
    prev_close = float(prev["close"])
    
    # Breakdown: previous candle above level, current candle below level - buffer
    return prev_close >= level and last_close < level - buffer_pts


def is_bullish_breakout_continuation(df_5m, df_15m, ema, ema50, level):
    """
    Valid bullish breakout continuation:
    - Initial break: prev candle below level, current candle above level+buffer
    - Accepted above: price holding above broken resistance with structure intact
    """
    if df_5m is None or df_15m is None or len(df_5m) < 5 or len(df_15m) < 2:
        return False
    
    last_15 = float(df_15m.iloc[-1]["close"])
    prev_15 = float(df_15m.iloc[-2]["close"])
    
    # Count recent bullish 5M candles
    recent_5m = df_5m.tail(3)
    bullish_5m = sum(
        1 for _, row in recent_5m.iterrows()
        if float(row["close"]) > float(row["open"])
    )
    recent_low = float(recent_5m["low"].min())
    
    # Check structural alignment
    price_above_ema50 = ema50 is not None and last_15 > float(ema50)
    ema_bullish = ema and float(ema.get("fast", 0)) > float(ema.get("slow", 0))
    
    # Initial break: prev candle below, current candle above with buffer
    initial_break = prev_15 <= level and last_15 > level + 2.0
    
    # Accepted above: price holding above broken resistance (not just first break)
    # Both candles above level, and recent 5M lows still respecting broken level
    accepted_above = (
        prev_15 > level + 2.0 and 
        last_15 > level + 2.0 and 
        recent_low > level - 3.0  # 5M lows not breaking back below (relaxed for gold volatility)
    )
    
    return (initial_break or accepted_above) and bullish_5m >= 2 and price_above_ema50 and ema_bullish


def is_bearish_breakdown_continuation(df_5m, df_15m, ema, ema50, level):
    """
    Valid bearish breakdown continuation:
    - Initial break: prev candle above level, current candle below level-buffer
    - Accepted below: price holding below broken support with structure intact
    """
    if df_5m is None or df_15m is None or len(df_5m) < 5 or len(df_15m) < 2:
        return False
    
    last_15 = float(df_15m.iloc[-1]["close"])
    prev_15 = float(df_15m.iloc[-2]["close"])
    
    # Count recent bearish 5M candles
    recent_5m = df_5m.tail(3)
    bearish_5m = sum(
        1 for _, row in recent_5m.iterrows()
        if float(row["close"]) < float(row["open"])
    )
    recent_high = float(recent_5m["high"].max())
    
    # Check structural alignment
    price_below_ema50 = ema50 is not None and last_15 < float(ema50)
    ema_bearish = ema and float(ema.get("fast", 0)) < float(ema.get("slow", 0))
    
    # Initial break: prev candle above, current candle below with buffer
    initial_break = prev_15 >= level and last_15 < level - 2.0
    
    # Accepted below: price holding below broken support (not just first break)
    # Both candles below level, and recent 5M highs not breaking back above
    accepted_below = (
        prev_15 < level - 2.0 and 
        last_15 < level - 2.0 and 
        recent_high < level + 3.0  # 5M highs not breaking back above (relaxed for gold volatility)
    )
    
    return (initial_break or accepted_below) and bearish_5m >= 2 and price_below_ema50 and ema_bearish


def get_macro_context(df_daily):
    if len(df_daily) < 10:
        return {"trend": "UNKNOWN", "weekly_high": None, "weekly_low": None, "summary": "Insufficient data"}
    week = df_daily.iloc[-5:]
    weekly_high = float(week["high"].max())
    weekly_low  = float(week["low"].min())
    recent = df_daily.iloc[-10:]
    highs = recent["high"].tolist()
    lows  = recent["low"].tolist()
    hh = sum(1 for i in range(1,len(highs)) if highs[i]>highs[i-1])
    lh = sum(1 for i in range(1,len(highs)) if highs[i]<highs[i-1])
    hl = sum(1 for i in range(1,len(lows))  if lows[i] >lows[i-1])
    ll = sum(1 for i in range(1,len(lows))  if lows[i] <lows[i-1])
    if ll>=6 and lh>=5:   daily_trend="STRONG BEARISH"
    elif ll>hh and lh>hl: daily_trend="BEARISH"
    elif hh>=6 and hl>=5: daily_trend="STRONG BULLISH"
    elif hh>ll and hl>lh: daily_trend="BULLISH"
    else:                 daily_trend="RANGING"
    last3 = df_daily.iloc[-3:]
    close_str = " → ".join([f"{c:.0f}" for c in last3["close"].tolist()])
    summary = (f"Daily trend: {daily_trend} ({hh} higher highs, {ll} lower lows in last 10 days)\n"
               f"Weekly range: {weekly_low:.1f} - {weekly_high:.1f} ({round(weekly_high-weekly_low,1)} pts)\n"
               f"Last 3 daily closes: {close_str}")
    return {"trend": daily_trend, "weekly_high": weekly_high, "weekly_low": weekly_low, "summary": summary}

def check_proximity(price, levels):
    hits = []
    for name, level in levels.items():
        if level and abs(price-level) <= LEVEL_PROXIMITY:
            hits.append({"name": name, "level": level, "distance": round(abs(price-level),1), "above": price>level})
    return hits

def get_structural_levels(price, levels, macro):
    candidates = {}
    for name, val in levels.items():
        if val is not None:
            candidates[name.replace("_"," ").title()] = float(val)
    if macro.get("weekly_high"): candidates["Weekly High"] = float(macro["weekly_high"])
    if macro.get("weekly_low"):  candidates["Weekly Low"]  = float(macro["weekly_low"])
    base = math.floor(price/50)*50
    for mult in range(-8,9):
        level = base + mult*50
        if abs(price-level) <= 400:
            candidates[f"Round {int(level)}"] = float(level)
    above = sorted([{"name":n,"level":v} for n,v in candidates.items() if v>price+2], key=lambda x:x["level"])
    below = sorted([{"name":n,"level":v} for n,v in candidates.items() if v<price-2], key=lambda x:x["level"], reverse=True)
    return above, below

def get_level_direction_context(proximity_hits):
    lines = []
    for h in proximity_hits:
        name = h['name']; dist = h['distance']
        if name == 'today_high':
            lines.append(f"⚠️ TODAY HIGH ({h['level']:.2f}, {dist:.1f}pts) = potential resistance. SHORT only with reversal confirmation. LONG only after confirmed breakout/retest.")
        elif name == 'today_low':
            lines.append(f"✅ TODAY LOW ({h['level']:.2f}, {dist:.1f}pts) = potential support. LONG bounce with confirmation. SHORT only after confirmed breakdown.")
        elif name == 'prev_high':
            lines.append(f"⚠️ PREV HIGH ({h['level']:.2f}, {dist:.1f}pts) = potential resistance. SHORT only with rejection. LONG only after confirmed breakout/retest.")
        elif name == 'prev_low':
            lines.append(f"✅ PREV LOW ({h['level']:.2f}, {dist:.1f}pts) = potential support/flip level. Trade only after confirmation.")
    return "\n".join(lines) if lines else "No specific level direction context."

def get_ema_cross(df_15m, fast=9, slow=26):
    try:
        closes = df_15m["close"].astype(float)
        ema_fast = closes.ewm(span=fast, adjust=False).mean()
        ema_slow = closes.ewm(span=slow, adjust=False).mean()
        cf = round(float(ema_fast.iloc[-1]),3); cs = round(float(ema_slow.iloc[-1]),3)
        pf = round(float(ema_fast.iloc[-2]),3); ps = round(float(ema_slow.iloc[-2]),3)
        crossed_bullish = pf<=ps and cf>cs
        crossed_bearish = pf>=ps and cf<cs
        if crossed_bullish:   state = "BULLISH CROSS (just crossed up)"
        elif crossed_bearish: state = "BEARISH CROSS (just crossed down)"
        elif cf>cs:           state = f"BULLISH (fast {round(cf-cs,2)} above slow)"
        else:                 state = f"BEARISH (fast {round(cs-cf,2)} below slow)"
        return {"state":state,"fast":cf,"slow":cs,"bullish":cf>cs,"just_crossed":crossed_bullish or crossed_bearish}
    except:
        return {"state":"UNKNOWN","fast":0,"slow":0,"bullish":None,"just_crossed":False}

def get_ema50(df_15m):
    try:
        closes = df_15m["close"].astype(float)
        ema50 = closes.ewm(span=50, adjust=False).mean()
        return round(float(ema50.iloc[-1]),3)
    except: return None

def get_recent_range_atr(df, lookback=20):
    """Average candle range over recent candles — used for extension checks."""
    if df is None or len(df) < 3: return 0.0
    recent = df.tail(min(lookback, len(df)))
    ranges = (recent["high"].astype(float) - recent["low"].astype(float)).abs()
    return float(ranges.mean()) if not ranges.empty else 0.0

def get_ema_fast(df, span=9):
    """Get fast EMA from any timeframe dataframe."""
    try:
        closes = df["close"].astype(float)
        return round(float(closes.ewm(span=span, adjust=False).mean().iloc[-1]), 3)
    except:
        return None

def is_near_round_level(price, step=50, proximity=15):
    """Check if price is near a round level (e.g., 4650, 4700)."""
    nearest = round(price / step) * step
    return abs(price - nearest) <= proximity

def is_near_fast_ema(price, df_5m, mult=1.0):
    """Check if price is near 5M EMA9 (pullback continuation setup)."""
    fast = get_ema_fast(df_5m, span=9)
    atr = get_recent_range_atr(df_5m, lookback=20)
    if fast is None or atr <= 0:
        return False
    return abs(price - fast) <= atr * mult

def get_execution_regime(df_5m, df_15m, ema, ema50):
    """
    Classify current 5M/15M execution regime as BULLISH, BEARISH, or RANGING.
    Separate from daily macro bias — controls entry timing.
    FIXED: Uses OR logic — structure OR momentum, not both required.
    """
    if df_5m is None or df_15m is None or len(df_5m) < 8 or df_15m.empty:
        return "RANGING"
    price = float(df_15m.iloc[-1]["close"])
    fast  = float(ema.get("fast", price)) if ema else price
    slow  = float(ema.get("slow", price)) if ema else price
    recent = df_5m.tail(8)
    highs  = recent["high"].astype(float).tolist()
    lows   = recent["low"].astype(float).tolist()
    closes = recent["close"].astype(float).tolist()
    hh = sum(1 for i in range(1,len(highs))  if highs[i]  > highs[i-1])
    lh = sum(1 for i in range(1,len(highs))  if highs[i]  < highs[i-1])
    hl = sum(1 for i in range(1,len(lows))   if lows[i]   > lows[i-1])
    ll = sum(1 for i in range(1,len(lows))   if lows[i]   < lows[i-1])
    bc = sum(1 for i in range(1,len(closes)) if closes[i] > closes[i-1])
    
    # FIXED: BULLISH = price above EMA50, fast > slow, AND (structure OR momentum)
    if ema50 is not None and price > float(ema50) and fast > slow and (hh >= 3 or bc >= 5):
        return "BULLISH"
    # FIXED: BEARISH = price below EMA50, fast < slow, AND (structure OR momentum)
    if ema50 is not None and price < float(ema50) and fast < slow and (ll >= 3 or bc <= 2):
        return "BEARISH"
    return "RANGING"

def validate_regime_alignment(setup, df_5m, df_15m, ema, ema50, rr, proximity_hits):
    """
    Block counter-execution-regime trades unless reversal structure is confirmed.
    FIXED: Logs reason to console before returning.
    """
    if setup not in {"LONG","SHORT"}: return True, "No setup"
    if df_5m is None or len(df_5m) < 10: return True, "Not enough 5M data"

    regime = get_execution_regime(df_5m, df_15m, ema, ema50)
    prev_range    = df_5m.iloc[-9:-1]  # Previous 8 completed candles
    local_support = float(prev_range["low"].min())
    local_resist  = float(prev_range["high"].max())
    last_close    = float(df_5m.iloc[-1]["close"])
    prev_close    = float(df_5m.iloc[-2]["close"])
    breakdown     = last_close < local_support and prev_close >= local_support
    breakout      = last_close > local_resist  and prev_close <= local_resist

    if regime == "BULLISH" and setup == "SHORT":
        if not (breakdown and rr >= MIN_RR):
            reason = f"SHORT in bullish regime — no breakdown (close={last_close:.1f} vs support={local_support:.1f}, R:R={rr:.2f})"
            print(f"[REGIME] ⛔ {reason}")  # FIXED: Console logging
            return False, reason
    if regime == "BEARISH" and setup == "LONG":
        if not (breakout and rr >= MIN_RR):
            reason = f"LONG in bearish regime — no breakout (close={last_close:.1f} vs resist={local_resist:.1f}, R:R={rr:.2f})"
            print(f"[REGIME] ⛔ {reason}")  # FIXED: Console logging
            return False, reason
    if regime == "RANGING":
        near_key = any(h["name"] in {"today_high","today_low","prev_high","prev_low"} for h in proximity_hits)
        if not near_key:
            reason = "Ranging regime — not at key level edge"
            print(f"[REGIME] ⚠️ {reason}")  # FIXED: Console logging
            return False, reason

    print(f"[REGIME] ✅ {regime} regime allows {setup} (breakdown={breakdown}, breakout={breakout}, R:R={rr:.2f})")  # FIXED: Console logging
    return True, f"Regime OK ({regime})"

def validate_extension(setup, df_5m, atr_mult=1.5):
    """
    Block entries that are too extended from the 5M EMA9 — prevents chasing.
    FIXED: Uses 5M EMA9, not 15M EMA.
    """
    if df_5m is None or len(df_5m) < 20: 
        return True, "Extension check skipped"
    
    fast = get_ema_fast(df_5m, span=9)
    if fast is None: 
        return True, "Extension check skipped"
    
    current_close = float(df_5m.iloc[-1]["close"])
    atr = get_recent_range_atr(df_5m, lookback=20)
    if atr <= 0: 
        return True, "Extension check skipped"
    
    extension = abs(current_close - fast)
    threshold = atr * atr_mult
    if extension > threshold:
        reason = f"Price too extended from 5M EMA9 ({extension:.1f}pts > {threshold:.1f}pts threshold)"
        print(f"[EXTENSION] ⛔ {reason}")
        return False, reason
    
    print(f"[EXTENSION] ✅ Extension OK ({extension:.1f}pts < {threshold:.1f}pts threshold)")
    return True, "Extension OK"

def has_consecutive_directional_candles(df, direction, n=2):
    if df is None or len(df) < n: return False
    recent = df.tail(n)
    if direction == "SHORT":
        return all(float(row.close) < float(row.open) for _, row in recent.iterrows())
    if direction == "LONG":
        return all(float(row.close) > float(row.open) for _, row in recent.iterrows())
    return False


# ── NEWS ───────────────────────────────────────────────────────────────────────

def get_upcoming_events():
    if not FINNHUB_API_KEY: return []
    try:
        now = datetime.now(timezone.utc)
        end = now + timedelta(hours=24)
        resp = requests.get("https://finnhub.io/api/v1/calendar/economic", params={
            "from": now.strftime("%Y-%m-%d"), "to": end.strftime("%Y-%m-%d"), "token": FINNHUB_API_KEY
        }, timeout=10)
        if not resp.ok: return []
        events = resp.json().get("economicCalendar", [])
        relevant = []
        for e in events:
            if str(e.get("impact","")).lower() not in ("high","3"): continue
            name = str(e.get("event","")).lower()
            if not any(kw in name for kw in GOLD_RELEVANT_EVENTS): continue
            try:
                ts = e.get("time","") or e.get("date","")
                dt = datetime.fromisoformat(ts.replace("Z","+00:00"))
                mins = (dt-now).total_seconds()/60
                if -60 <= mins <= 1440:
                    relevant.append({"name":e.get("event",""),"time":dt.strftime("%H:%M UTC %d %b"),"minutes_away":round(mins)})
            except: continue
        return relevant[:5]
    except Exception as e:
        print(f"[GOLD] Calendar error: {e}")
        return []

def is_news_blocked(events):
    for e in events:
        ma = int(e["minutes_away"])
        if -15 <= ma <= NEWS_BUFFER_MIN:
            label = f"in {ma}m" if ma >= 0 else f"{abs(ma)}m ago"
            return True, f"{e['name']} ({label})"
    return False, ""

def get_event_warning(events):
    blocked, reason = is_news_blocked(events)
    if blocked:
        return f"⚠️ <b>NEWS BLOCK</b>\n{reason} — stand aside"
    return ""


# ── STATE STRING ───────────────────────────────────────────────────────────────

def build_state_str(state, macro, price, shadow_pos):
    trend = macro.get("trend","")
    bias_code = ("SBEAR" if "STRONG BEARISH" in trend else "BEAR" if "BEARISH" in trend else
                 "SBULL" if "STRONG BULLISH" in trend else "BULL" if "BULLISH" in trend else "RANG")
    if shadow_pos:
        side = "SH" if shadow_pos["side"]=="SHORT" else "LG"
        pl   = round(shadow_pos["unrealized_pl"],0)
        trade_str = f"{side}|PL:{pl:+.0f}"
    else:
        trade_str = "FLAT"
    recent = state.get("recent_signals",[])[-5:]
    sig_codes = []
    for s in recent:
        d = s.get("direction","NT"); c = s.get("confidence","L")[:1]; o = (s.get("outcome","?")[:1] if s.get("outcome") else "?")
        sig_codes.append(f"{d[:1]}{c}:{o}")
    sigs = ",".join(sig_codes) if sig_codes else "none"
    return f"{bias_code}|{price:.0f}|{trade_str}|HIST:{sigs}"


# ── DAILY RISK LIMITS ──────────────────────────────────────────────────────────

def check_daily_limits(state, nav):
    daily = state.setdefault("daily", default_state()["daily"])
    if daily.get("date") != today_key():
        daily.clear(); daily.update(default_state()["daily"])
    if daily.get("start_nav") is None:
        daily["start_nav"] = nav
    start_nav = float(daily.get("start_nav") or nav)
    if start_nav > 0:
        dd_pct = (start_nav - nav) / start_nav
        if dd_pct >= DAILY_LOSS_CAP_PCT:
            return False, f"Daily loss cap hit ({dd_pct*100:.2f}%)"
    if int(daily.get("executed_trades",0)) >= MAX_TRADES_PER_DAY:
        return False, f"Max trades/day hit ({MAX_TRADES_PER_DAY})"
    if int(daily.get("consecutive_losses",0)) >= MAX_CONSECUTIVE_LOSSES:
        return False, f"Max consecutive losses ({MAX_CONSECUTIVE_LOSSES})"
    last_loss = parse_dt(daily.get("last_loss_time"))
    if last_loss:
        mins = (datetime.now(timezone.utc)-last_loss).total_seconds()/60
        if mins < COOLDOWN_AFTER_LOSS_MIN:
            return False, f"Cooldown after loss ({mins:.0f}/{COOLDOWN_AFTER_LOSS_MIN}m)"
    return True, "OK"

def check_duplicate(state, key, price):
    rec = state.get("duplicate_signals",{}).get(key)
    if not rec: return False, ""
    last_time = parse_dt(rec.get("time"))
    if not last_time: return False, ""
    age_min = (datetime.now(timezone.utc)-last_time).total_seconds()/60
    last_price = safe_float(rec.get("price"))
    if last_price is None: return False, ""
    moved = abs(price-last_price)
    if age_min < DUPLICATE_COOLDOWN_MIN and moved < DUPLICATE_PRICE_MOVE_PTS:
        return True, f"Duplicate ({age_min:.0f}m old, {moved:.1f}pts move)"
    return False, ""

def mark_duplicate(state, key, price):
    state.setdefault("duplicate_signals",{})[key] = {
        "time": datetime.now(timezone.utc).isoformat(), "price": price
    }


# ── CLAUDE PROMPT ──────────────────────────────────────────────────────────────

def ask_claude(price, levels, proximity_hits, df_15m, session, balance, events, macro, ema=None, df_5m=None, state_str="", ema50=None, execution_regime="RANGING", trigger_context="None"):
    recent = df_15m.tail(8)
    candle_str = "\n".join([
        f"  {str(idx)[-8:-3]}  O:{row.open:.1f} H:{row.high:.1f} L:{row.low:.1f} C:{row.close:.1f}"
        for idx, row in recent.iterrows()])
    highs = recent["high"].tolist(); lows = recent["low"].tolist()
    hh = sum(1 for i in range(1,len(highs)) if highs[i]>highs[i-1])
    ll = sum(1 for i in range(1,len(lows))  if lows[i] <lows[i-1])
    intraday_trend = "BEARISH" if ll>hh else "BULLISH" if hh>ll else "NEUTRAL"
    risk_dollars = round(balance*RISK_PCT,2)

    if df_5m is not None and not df_5m.empty:
        recent_5m = df_5m.tail(12)
        candle_5m_str = "\n".join([
            f"  {str(idx)[-8:-3]}  O:{row.open:.1f} H:{row.high:.1f} L:{row.low:.1f} C:{row.close:.1f}"
            for idx, row in recent_5m.iterrows()])
        highs_5m = recent_5m["high"].tolist(); lows_5m = recent_5m["low"].tolist()
        hh_5m = sum(1 for i in range(1,len(highs_5m)) if highs_5m[i]>highs_5m[i-1])
        ll_5m = sum(1 for i in range(1,len(lows_5m))  if lows_5m[i] <lows_5m[i-1])
        trend_5m = "BEARISH" if ll_5m>hh_5m else "BULLISH" if hh_5m>ll_5m else "NEUTRAL"
    else:
        candle_5m_str = "Not available"; trend_5m = "UNKNOWN"; hh_5m = ll_5m = 0

    events_str = "\n".join([
        f"  - {e['name']}: {e['time']} (in {e['minutes_away']}m)" if e['minutes_away']>=0
        else f"  - {e['name']}: {e['time']} ({abs(e['minutes_away'])}m ago)"
        for e in events]) if events else "No high-impact events in next 24 hours."

    above_levels, below_levels = get_structural_levels(price, levels, macro)
    above_str = "\n".join([f"  {l['name']}: {l['level']:.2f} (+{l['level']-price:.1f} pts)" for l in above_levels[:6]]) or "  None within range"
    below_str = "\n".join([f"  {l['name']}: {l['level']:.2f} (-{price-l['level']:.1f} pts)" for l in below_levels[:6]]) or "  None within range"
    level_direction = get_level_direction_context(proximity_hits)

    ema50_str = "N/A"
    if ema50:
        diff = round(price-ema50,2)
        ema50_str = f"{'ABOVE' if diff>0 else 'BELOW'} by {abs(diff):.1f}pts (EMA50={ema50:.1f})"

    prompt = f"""You are a trading analyst for XAU/USD. Python enforces all rules — your job is to propose a clean setup or say NO TRADE.

INTER-SIGNAL STATE:
{state_str if state_str else "No prior state."}
Decode: BIAS|PRICE|OPEN_TRADE|PL|HIST — use to avoid repeating failed setups.

HISTORICAL EXAMPLES — learn from these:

✅ GOOD: Mar 16 SHORT @ 5001.9 (+$20, 3.9:1)
Price broke BELOW prev low, retested from below, 2 bearish candles confirmed rejection.
KEY: Structural break + retest + momentum confirmation + both trends bearish.

✅ GOOD: Mar 18 3x SHORT @ 4946-4969 (+$66)
Daily AND intraday both strongly bearish. Consecutive bearish candles making lower lows.
KEY: Trending day, price at support breaking down, clean continuation candles.

✅ GOOD: Mar 20 SHORT @ 4612 (+$62)
Strong bearish trend, price at today's low breaking down, 28pt stop, structural target 56pts away.
KEY: Trending conditions, clear level, adequate stop, nearest structural target.

❌ BAD: Apr 6 3x LONG @ 4683-4704 (-$253)
Entered LONG when price near TODAY'S HIGH. Daily bullish but entry at resistance.
KEY MISTAKE: Bullish trend does NOT justify LONG at resistance.

❌ BAD: Apr 29 SHORT @ 4538 (-$91) — SL hit
Entered SHORT mid-bounce, not at resistance. Price was recovering, stopped out immediately.
KEY MISTAKE: Shorting into green candles during pullback. Need entry AT resistance, not mid-range.

❌ BAD: Target gaming R:R — entered SHORT at 4538, target 4450 just to pass 2.5 R:R filter.
Nearest level was 4510 (only 1.3R) — should have been NO TRADE.
KEY MISTAKE: Never skip over closer levels to find one that passes R:R. Use nearest or NO TRADE.

Current time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}
Session: {session}
Current price: {price:.3f}
Risk per trade: ${risk_dollars} (1%)
Minimum R:R: {MIN_RR}:1
Minimum stop: {MIN_STOP_PTS} points

MACRO CONTEXT:
{macro['summary']}

EXECUTION REGIME (5M/15M structure):
{execution_regime}

TRADE TRIGGER CONTEXT:
{trigger_context}
This explains why the bot called you — key level proximity, round level, or EMA pullback.

REGIME RULES:
- BULLISH regime: Shorts require lower high + breakdown + R:R ≥ {MIN_RR}
- BEARISH regime: Longs require higher low + breakout + R:R ≥ {MIN_RR}
- RANGING regime: Trade only at range edges
- If daily bias conflicts with execution regime, do NOT force a trade in daily direction
- Counter-execution-regime trades require FULL reversal structure

KEY LEVELS:
- Today High:    {levels['today_high']}
- Today Low:     {levels['today_low']}
- Prev Day High: {levels['prev_high']}
- Prev Day Low:  {levels['prev_low']}
- Weekly High:   {macro['weekly_high']}
- Weekly Low:    {macro['weekly_low']}

NEAR THESE LEVELS:
{json.dumps(proximity_hits, indent=2) if proximity_hits else "None"}

LEVEL DIRECTION:
{level_direction}

LEVELS ABOVE (resistance / long targets):
{above_str}

LEVELS BELOW (support / short targets):
{below_str}

15M CANDLES (completed, last 8):
{candle_str}
INTRADAY TREND: {intraday_trend} ({hh} HH vs {ll} LL)
EMA (9/26): {ema["state"] if ema else "N/A"}
EMA (50):   {ema50_str}

5M CANDLES (completed, last 12):
{candle_5m_str}
5M TREND: {trend_5m} ({hh_5m} HH vs {ll_5m} LL)
Need 2+ consecutive completed 5m candles in trade direction.

EVENTS:
{events_str}

TARGET RULES:
- Use the NEAREST valid structural level that gives {MIN_RR}:1 RR
- Do NOT skip over closer levels to find a better R:R — that's gaming the filter
- If nearest level doesn't give {MIN_RR}:1 → output NO TRADE

CRITICAL RULES:
- Use the NEAREST valid structural target in the trade direction.
- Do NOT skip closer levels to find one that gives better R:R.
- If the nearest realistic target does not give 2.5:1, output NO TRADE.
- LONG near today_high/prev_high: Valid if price closes ABOVE resistance with bullish candle OR if breakout continuation confirmed (see TREND DAY EXCEPTION below).
- SHORT near today_low/prev_low: Valid if price closes BELOW support with bearish candle OR if breakdown continuation confirmed.
- In BULLISH execution regime, SHORT requires reversal structure (not just high-touch).
- In BEARISH execution regime, LONG requires reversal structure (not just low-touch).
- Never short into a pullback/bounce — wait for rejection at resistance or confirmed breakdown.
- Never long into a rejection — wait for bounce at support or confirmed breakout.
- Stop must be at structural invalidation, min {MIN_STOP_PTS}pts from entry.
- Choppy 5m candles = MEDIUM at most.
- Do not output LOW confidence with a LONG/SHORT setup — output NO TRADE instead.
- If STATE shows same setup failed recently, reduce confidence by one level.

TREND DAY EXCEPTION:
If execution regime is BULLISH, price closes above previous today_high/prev_high, EMA9 > EMA26, price is above EMA50, and 2+ completed 5M candles are bullish, a LONG breakout continuation is valid even if price is near today_high. Do not call it "resistance" after a confirmed breakout — the broken level is now SUPPORT.

If execution regime is BEARISH, price closes below previous today_low/prev_low, EMA9 < EMA26, price is below EMA50, and 2+ completed 5M candles are bearish, a SHORT breakdown continuation is valid even if price is near today_low. The broken level is now RESISTANCE.

Respond in EXACT format:
BIAS: [BULLISH / BEARISH / NEUTRAL]
SETUP: [LONG / SHORT / NO TRADE]
REASON: [1-2 sentences max]
ENTRY: [price or N/A]
STOP: [price or N/A]
TARGET: [price or N/A]
TARGET_LEVEL: [level name or N/A]
STOP_DIST: [points or N/A]
POSITION_SIZE: [N/A — calculated by bot]
RR: [ratio or N/A]
CONFIDENCE: [HIGH / MEDIUM / LOW]
STATE: [updated state string]"""

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=600, temperature=0,
                                  messages=[{"role":"user","content":prompt}])
    return msg.content[0].text.strip()


def parse_claude_response(response):
    result = {}
    allowed = {"BIAS","SETUP","REASON","ENTRY","STOP","TARGET","TARGET_LEVEL","STOP_DIST","POSITION_SIZE","RR","CONFIDENCE","STATE"}
    for line in response.split("\n"):
        if ":" not in line: continue
        key, _, val = line.strip().partition(":")
        key = key.strip().upper()
        if key in allowed:
            result[key] = val.strip()
    return result

def validate_claude_output(parsed):
    setup = parsed.get("SETUP", "NO TRADE")
    if setup not in {"LONG","SHORT","NO TRADE"}: 
        return False, f"Invalid SETUP: {setup}"
    
    # For NO TRADE, only require SETUP, BIAS, REASON
    if setup == "NO TRADE":
        if "BIAS" not in parsed: return False, "Missing BIAS"
        if "REASON" not in parsed: return False, "Missing REASON"
        if parsed.get("BIAS") not in {"BULLISH","BEARISH","NEUTRAL"}: 
            return False, f"Invalid BIAS: {parsed.get('BIAS')}"
        return True, "OK"
    
    # For LONG/SHORT, require all fields
    required = ["BIAS","SETUP","REASON","ENTRY","STOP","TARGET","RR","CONFIDENCE"]
    missing = [k for k in required if k not in parsed]
    if missing: return False, f"Missing fields: {missing}"
    
    if parsed["BIAS"] not in {"BULLISH","BEARISH","NEUTRAL"}: 
        return False, f"Invalid BIAS: {parsed['BIAS']}"
    if parsed["CONFIDENCE"] not in {"HIGH","MEDIUM","LOW"}: 
        return False, f"Invalid CONFIDENCE: {parsed['CONFIDENCE']}"
    
    for k in ["ENTRY","STOP","TARGET"]:
        if safe_float(parsed.get(k)) is None: 
            return False, f"Invalid {k}: {parsed.get(k)}"
    
    return True, "OK"

def get_medium_quality(parsed, macro, proximity_hits):
    scores = []; flags = []
    try:
        rr = float(parsed.get("RR","0").replace(":1","").strip())
        if rr>=3.0: scores.append(2); flags.append(f"R:R {rr}:1 ✅✅")
        elif rr>=2.5: scores.append(1); flags.append(f"R:R {rr}:1 ✅")
        else: scores.append(0); flags.append(f"R:R {rr}:1 ⚠️")
    except: flags.append("R:R unknown")
    daily = macro.get("trend",""); setup = parsed.get("SETUP","NO TRADE")
    if ("BULLISH" in daily and setup=="LONG") or ("BEARISH" in daily and setup=="SHORT"):
        scores.append(2); flags.append("Trends aligned ✅✅")
    elif "RANGING" in daily: scores.append(0); flags.append("Daily ranging ⚠️")
    else: scores.append(0); flags.append("Trend conflict ❌")
    has_key = any(h['name'] in ['today_high','today_low','prev_high','prev_low'] for h in proximity_hits)
    if has_key: scores.append(2); flags.append("Key level ✅✅")
    else: scores.append(0); flags.append("Weak level ⚠️")
    total = sum(scores)
    stars = "⭐⭐⭐" if total>=5 else "⭐⭐" if total>=3 else "⭐"
    return stars, flags


# ── EXECUTION VALIDATION ───────────────────────────────────────────────────────

def validate_for_execution(parsed, live, proximity_hits, df_15m, df_5m, macro, events, state, nav, ema=None, ema50=None):
    setup = parsed.get("SETUP","NO TRADE")
    confidence = parsed.get("CONFIDENCE","LOW")

    if setup not in {"LONG","SHORT"}: return False, "No executable setup", {}
    if confidence == "LOW": return False, "LOW confidence blocked", {}
    if os.path.exists(KILL_SWITCH): return False, "STOP_BOT kill switch active", {}
    
    if OANDA_ENVIRONMENT.lower() != "practice":
        return False, f"PAPER ONLY: refusing to trade on {OANDA_ENVIRONMENT}", {}
    
    if os.getenv("AUTO_EXECUTE","true").lower() != "true": return False, "AUTO_EXECUTE disabled", {}

    blocked, reason = is_news_blocked(events)
    if blocked: return False, reason, {}

    ok, reason = check_daily_limits(state, nav)
    if not ok: return False, reason, {}

    # CRITICAL: Fetch fresh live price for execution (gold moves fast)
    # Use allow_fallback=False to block execution if real bid/ask unavailable
    live = get_live_price(allow_fallback=False)
    entry = float(live["ask"] if setup=="LONG" else live["bid"])
    stop   = safe_float(parsed.get("STOP"))
    target = safe_float(parsed.get("TARGET"))
    if stop is None or target is None: return False, "Invalid stop/target", {}

    # Geometry check with live price
    if setup=="LONG" and not (stop < entry < target):
        return False, f"Invalid LONG geometry: stop={stop} entry={entry} target={target}", {}
    if setup=="SHORT" and not (target < entry < stop):
        return False, f"Invalid SHORT geometry: target={target} entry={entry} stop={stop}", {}

    # Stop distance
    stop_dist = abs(entry - stop)
    if stop_dist < MIN_STOP_PTS: return False, f"Stop too tight ({stop_dist:.1f}pts)", {}
    if stop_dist > MAX_STOP_PTS: return False, f"Stop too wide ({stop_dist:.1f}pts)", {}

    # R:R with spread buffer
    if setup == "LONG":
        risk   = (entry - stop)  + SPREAD_BUFFER
        reward = (target - entry) - SPREAD_BUFFER
    else:
        risk   = (stop - entry)  + SPREAD_BUFFER
        reward = (entry - target) - SPREAD_BUFFER
    if risk <= 0 or reward <= 0: return False, "Invalid risk/reward after spread", {}
    rr = reward / risk

    # FIXED: Regime alignment check BEFORE R:R and momentum checks
    # This ensures we log the regime reason when it's the actual blocker
    regime_ok, regime_reason = validate_regime_alignment(setup, df_5m, df_15m, ema, ema50, rr, proximity_hits)
    if not regime_ok:
        return False, regime_reason, {}

    # Now check R:R
    if rr < MIN_RR: return False, f"Live R:R {rr:.2f} below {MIN_RR}", {}

    # Momentum check after regime
    if not has_consecutive_directional_candles(df_5m, setup, n=2):
        return False, "No 2-candle 5M momentum confirmation", {}

    # FIXED: Extension guard
    ext_ok, ext_reason = validate_extension(setup, df_5m)
    if not ext_ok:
        return False, ext_reason, {}

    # Structure rules with candle confirmation
    daily_trend = macro.get("trend","")
    resistance_hits = [h for h in proximity_hits if h['name'] in RESISTANCE_LEVELS]
    if setup=="LONG" and resistance_hits:
        level = float(resistance_hits[0]["level"])
        
        # Allow LONG if either:
        # 1. Clean breakout confirmed (prev candle below, current above)
        # 2. Bullish breakout continuation mode (structure + momentum aligned)
        breakout_ok = has_confirmed_breakout(df_15m, level)
        continuation_ok = is_bullish_breakout_continuation(df_5m, df_15m, ema, ema50, level)
        
        if not (breakout_ok or continuation_ok):
            return False, f"LONG blocked — near {resistance_hits[0]['name']} without confirmed breakout", {}
    
    support_hits = [h for h in proximity_hits if h['name'] in SUPPORT_LEVELS]
    if setup=="SHORT" and support_hits:
        level = float(support_hits[0]["level"])
        
        # Allow SHORT if either:
        # 1. Clean breakdown confirmed (prev candle above, current below)
        # 2. Bearish breakdown continuation mode (structure + momentum aligned)
        breakdown_ok = has_confirmed_breakdown(df_15m, level)
        continuation_ok = is_bearish_breakdown_continuation(df_5m, df_15m, ema, ema50, level)
        
        # FIXED: Bullish daily trend becomes stricter filter, not absolute ban
        if "BULLISH" in daily_trend and not (breakdown_ok or continuation_ok):
            return False, "SHORT blocked — bullish daily trend and no confirmed support breakdown", {}
        # For all cases, require breakdown OR continuation if above support
        if all(h['above'] for h in support_hits):
            if not (breakdown_ok or continuation_ok):
                return False, f"SHORT blocked — above {support_hits[0]['name']} without confirmed breakdown", {}

    # No open position
    shadow_pos = get_open_position(SHADOW_ACCOUNT_ID)
    if shadow_pos is not None: return False, f"Shadow position open ({shadow_pos['side']})", {}

    exec_ctx = {
        "entry": round(entry,3), "stop": round(stop,3), "target": round(target,3),
        "rr": round(rr,2), "stop_dist": round(stop_dist,2),
        "units": SHADOW_UNITS,
    }
    return True, "OK", exec_ctx


# ── SHADOW EXECUTION ───────────────────────────────────────────────────────────

def shadow_execute(setup, exec_ctx):
    try:
        side  = "buy" if setup=="LONG" else "sell"
        units = str(exec_ctx["units"]) if side=="buy" else str(-exec_ctx["units"])
        order_data = {
            "order": {
                "type": "MARKET", "instrument": INSTRUMENT, "units": units,
                "stopLossOnFill":   {"price": f"{exec_ctx['stop']:.3f}",   "timeInForce": "GTC"},
                "takeProfitOnFill": {"price": f"{exec_ctx['target']:.3f}", "timeInForce": "GTC"},
            }
        }
        r  = orders.OrderCreate(SHADOW_ACCOUNT_ID, data=order_data)
        rv = oanda.request(r)
        order_id = rv.get("orderFillTransaction",{}).get("id") or rv.get("relatedTransactionIDs",["?"])[0]
        print(f"[SHADOW] ✅ {setup} {exec_ctx['units']} units | SL:{exec_ctx['stop']} TP:{exec_ctx['target']} | RR:{exec_ctx['rr']} | ID:{order_id}")
        return order_id, f"Filled {exec_ctx['units']} units | RR:{exec_ctx['rr']} | ID:{order_id}"
    except Exception as e:
        print(f"[SHADOW] Failed: {e}")
        return None, str(e)


# ── BREAKEVEN & SESSION MANAGER ────────────────────────────────────────────────

def manage_open_trades(state):
    try:
        now_utc = datetime.now(timezone.utc)
        session_close = (now_utc.hour==17 and now_utc.minute<15)

        r = trades_ep.OpenTrades(SHADOW_ACCOUNT_ID)
        rv = oanda.request(r)
        open_trades = [t for t in rv.get("trades",[]) if t.get("instrument")==INSTRUMENT]
        if not open_trades: return

        for trade in open_trades:
            trade_id    = trade["id"]
            units       = float(trade["currentUnits"])
            entry_price = float(trade["price"])
            current_pl  = float(trade.get("unrealizedPL",0))
            is_long     = units > 0
            sl_order    = trade.get("stopLossOrder",{})
            current_sl  = float(sl_order.get("price",0)) if sl_order else 0
            if not current_sl: continue
            stop_dist = abs(entry_price - current_sl)

            if session_close:
                try:
                    rc = trades_ep.TradeClose(SHADOW_ACCOUNT_ID, trade_id, data={"units": "ALL"})
                    oanda.request(rc)
                    print(f"[MANAGER] Session close trade {trade_id} P&L ${current_pl:.2f}")
                    send_telegram(f"🔔 <b>Shadow Session Close</b>\nTrade {trade_id} closed\nP&L: ${current_pl:.2f}")
                    daily = state.setdefault("daily", default_state()["daily"])
                    if current_pl < 0:
                        daily["consecutive_losses"] = int(daily.get("consecutive_losses",0)) + 1
                        daily["last_loss_time"] = now_utc.isoformat()
                    else:
                        daily["consecutive_losses"] = 0
                    save_state(state)
                except Exception as e:
                    print(f"[MANAGER] Session close failed: {e}")
                continue

            if stop_dist <= 0: continue
            pts_in_profit = current_pl / abs(units) if abs(units) > 0 else 0
            if pts_in_profit >= stop_dist:
                breakeven = round(entry_price, 3)
                should_move = (is_long and current_sl < breakeven-0.5) or (not is_long and current_sl > breakeven+0.5)
                if should_move:
                    try:
                        sl_id = sl_order.get("id")
                        if sl_id:
                            patch_data = {"order":{"price":f"{breakeven:.3f}","timeInForce":"GTC","type":"STOP_LOSS","tradeID":trade_id}}
                            pr = orders.OrderReplace(SHADOW_ACCOUNT_ID, sl_id, data=patch_data)
                            oanda.request(pr)
                            print(f"[MANAGER] Breakeven set trade {trade_id} @ {breakeven}")
                            send_telegram(f"🔒 <b>Shadow Breakeven</b>\nTrade {trade_id} SL → {breakeven}")
                    except Exception as e:
                        print(f"[MANAGER] Breakeven failed: {e}")
    except Exception as e:
        print(f"[MANAGER] Error: {e}")


# ── SIGNAL LOGGING ─────────────────────────────────────────────────────────────

def log_signal_to_api(instrument, parsed, price, session, macro, ema, shadow_id, shadow_msg):
    try:
        setup = parsed.get("SETUP","NO TRADE")
        now_iso = datetime.now(timezone.utc).isoformat()
        entry=stop=target=rr=stop_dist=None
        if setup in {"LONG","SHORT"}:
            entry=safe_float(parsed.get("ENTRY")); stop=safe_float(parsed.get("STOP"))
            target=safe_float(parsed.get("TARGET")); rr=safe_float(parsed.get("RR"))
            stop_dist=safe_float(parsed.get("STOP_DIST"))
        payload = {
            "id": now_iso+"_"+instrument, "datetime": now_iso, "instrument": instrument,
            "session": session, "price": price, "direction": setup,
            "confidence": parsed.get("CONFIDENCE","LOW"), "bias": parsed.get("BIAS",""),
            "reason": parsed.get("REASON",""), "entry": entry, "stop": stop, "target": target,
            "target_level": parsed.get("TARGET_LEVEL",""), "stop_dist": stop_dist, "rr": rr,
            "ema": ema["state"] if ema else "", "daily_trend": macro.get("trend",""),
            "executed": bool(shadow_id), "exec_msg": shadow_msg if shadow_id else "",
            "skip_reason": shadow_msg if not shadow_id else "",
            "shadow_id": shadow_id, "shadow_msg": shadow_msg,
        }
        requests.post(EC2_API+"/log_signal", json=payload, timeout=5)
        print("[GOLD] Signal logged")
    except Exception as e:
        print(f"[GOLD] Could not log signal: {e}")


# ── TELEGRAM ───────────────────────────────────────────────────────────────────

def format_telegram_message(price, proximity_hits, parsed, session, now_str, events, macro,
                             shadow_id=None, shadow_msg="", ema=None, balance=10000,
                             open_pos_005=None, ema50=None, exec_ctx=None):
    setup  = parsed.get("SETUP","NO TRADE")
    conf   = parsed.get("CONFIDENCE","LOW")
    emoji  = "🟢" if setup=="LONG" else "🔴" if setup=="SHORT" else "⚪"
    semoji = "🟦" if session=="LONDON" else "🟧" if session=="NEW YORK" else "⬜"
    dt     = macro.get("trend","UNKNOWN")
    dt_emoji = "📉📉" if "STRONG BEARISH" in dt else "📉" if "BEARISH" in dt else "📈📈" if "STRONG BULLISH" in dt else "📈" if "BULLISH" in dt else "↔️"

    if setup in {"LONG","SHORT"}:
        rr_str  = f"{exec_ctx['rr']} (live)" if exec_ctx else parsed.get("RR","—")
        size_str = f"{exec_ctx['units']} units" if exec_ctx else f"{SHADOW_UNITS} units"
        ema50_str = ""
        if ema50: ema50_str = f" | E50:{'↑' if price>ema50 else '↓'}"
        quality_str = ""
        if conf=="MEDIUM":
            stars, flags = get_medium_quality(parsed, macro, proximity_hits)
            quality_str = f" {stars} {' | '.join(flags)}"
        lines = [
            f"<b>{emoji} GOLD {setup}</b> — {conf}{semoji}",
            f"🕐 {now_str}",
            f"{dt_emoji} {dt}{ema50_str}",
            f"💰 Price: <b>{price:.2f}</b>",
            "",
            f"Entry:  <b>{exec_ctx['entry'] if exec_ctx else parsed.get('ENTRY','—')}</b>",
            f"Stop:   {exec_ctx['stop'] if exec_ctx else parsed.get('STOP','—')} ({exec_ctx['stop_dist'] if exec_ctx else parsed.get('STOP_DIST','—')}pts)",
            f"Target: {exec_ctx['target'] if exec_ctx else parsed.get('TARGET','—')} <i>({parsed.get('TARGET_LEVEL','—')})</i>",
            f"R:R:    {rr_str} | Size: {size_str}",
            f"EMA:    {ema['state'] if ema else 'N/A'}",
            "",
            f"📝 {parsed.get('REASON','—')}",
        ]
        if quality_str: lines.append(f"Quality:{quality_str}")
        if shadow_id: lines.append(f"\n🤖 <b>006:</b> Auto-executed | ID:{shadow_id}")
        elif shadow_msg: lines.append(f"\n🤖 <b>006:</b> Skipped — {shadow_msg}")
        if open_pos_005 and setup != open_pos_005["side"]:
            lines += ["", f"⚠️ <b>OPPOSING SIGNAL</b> — open {open_pos_005['side']} on 005 (P&L: ${open_pos_005['unrealized_pl']:.2f})", "Consider closing."]
    else:
        lines = [
            f"⚪ GOLD — NO TRADE  {semoji} {session}",
            f"🕐 {now_str} | 💰 {price:.2f}",
            f"{dt_emoji} {dt}",
            f"📝 {parsed.get('REASON','—')}",
        ]
    warning = get_event_warning(events)
    if warning: lines += ["", warning]
    return "\n".join(lines)


# ── MAIN ───────────────────────────────────────────────────────────────────────

def main():
    state = load_state()
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%H:%M UTC  %d %b %Y")
    session = get_session(now_utc.hour)
    print(f"[GOLD] Running at {now_str} | Session: {session}")

    manage_open_trades(state)
    
    # Block new entries during session close window
    if now_utc.hour == 17 and now_utc.minute < 15:
        print("[GOLD] Skipping — session close window")
        return

    try:
        df_15m   = get_candles("M15", 120, completed_only=True)
        df_5m    = get_candles("M5",  36,  completed_only=True)
        df_daily = get_candles("D",   30,  completed_only=True)

        if df_15m.empty or df_5m.empty:
            print("[GOLD] No candle data — skipping")
            return

        live    = get_live_price()
        price   = float(live["mid"])
        balance, nav = get_account_balance(SHADOW_ACCOUNT_ID)
        levels  = get_key_levels(df_15m, df_daily)
        macro   = get_macro_context(df_daily)
        ema     = get_ema_cross(df_15m)
        ema50   = get_ema50(df_15m)
        execution_regime = get_execution_regime(df_5m, df_15m, ema, ema50)
        print(f"[GOLD] Price: {price:.3f} | Spread: {live['spread']:.3f} | Daily trend: {macro['trend']} | Exec regime: {execution_regime}")

        check_daily_limits(state, nav)
        save_state(state)

        proximity_hits = check_proximity(price, levels)
        near_round = is_near_round_level(price)
        near_ema = is_near_fast_ema(price, df_5m)
        
        if not (proximity_hits or near_round or near_ema):
            print("[GOLD] Skipping — not near key level, round level, or EMA pullback")
            return

        # Build trigger context for Claude
        trigger_context = []
        if proximity_hits:
            trigger_context.append("key level: " + ", ".join([h["name"] for h in proximity_hits]))
        if near_round:
            nearest_round = round(price / 50) * 50
            trigger_context.append(f"round level: {nearest_round}")
        if near_ema:
            ema9_5m = get_ema_fast(df_5m, span=9)
            trigger_context.append(f"5M EMA9 pullback area: {ema9_5m}")
        trigger_context = " | ".join(trigger_context) if trigger_context else "None"

        print("[GOLD] Near tradeable zone — calling Claude")
        if proximity_hits:
            print(f"[GOLD] Key levels: {[h['name'] for h in proximity_hits]}")
        if near_round:
            print(f"[GOLD] Near round level")
        if near_ema:
            print(f"[GOLD] Near 5M EMA9 pullback")
        events     = get_upcoming_events()
        shadow_pos = get_open_position(SHADOW_ACCOUNT_ID)
        state_str  = build_state_str(state, macro, price, shadow_pos)

        claude_response = ask_claude(price, levels, proximity_hits, df_15m, session,
                                     balance, events, macro, ema=ema, df_5m=df_5m,
                                     state_str=state_str, ema50=ema50, execution_regime=execution_regime,
                                     trigger_context=trigger_context)
        print(f"[GOLD] Response:\n{claude_response}")

        parsed = parse_claude_response(claude_response)
        valid, parse_msg = validate_claude_output(parsed)
        if not valid:
            print(f"[GOLD] Invalid Claude output: {parse_msg}")
            parsed = {"BIAS":"NEUTRAL","SETUP":"NO TRADE","REASON":parse_msg,
                      "ENTRY":"N/A","STOP":"N/A","TARGET":"N/A","TARGET_LEVEL":"N/A",
                      "STOP_DIST":"N/A","RR":"N/A","CONFIDENCE":"LOW","STATE":state_str}

        setup      = parsed.get("SETUP","NO TRADE")
        confidence = parsed.get("CONFIDENCE","LOW")

        # Recalculate RR from Claude prices for display
        if setup in {"LONG","SHORT"}:
            e=safe_float(parsed.get("ENTRY")); s=safe_float(parsed.get("STOP")); t=safe_float(parsed.get("TARGET"))
            if e and s and t:
                sd = abs(e-s); td = abs(t-e)
                if sd>0 and td>0: parsed["RR"] = str(round(td/sd,2))
                parsed["STOP_DIST"] = str(round(sd,1))

        shadow_id  = None
        shadow_msg = ""
        exec_ctx   = None

        if setup in {"LONG","SHORT"}:
            # Stable duplicate key - avoid using actual price values
            if proximity_hits:
                near_names = "|".join(sorted([h["name"] for h in proximity_hits]))
            elif near_round and near_ema:
                near_names = "round_level|ema9_pullback"
            elif near_round:
                near_names = "round_level"
            elif near_ema:
                near_names = "ema9_pullback"
            else:
                near_names = "none"
            
            # FIXED: Include execution regime in duplicate key
            sig_key = f"{INSTRUMENT}|{setup}|{execution_regime}|{near_names}|{parsed.get('TARGET_LEVEL','')}"
            suppressed, suppress_reason = check_duplicate(state, sig_key, price)
            if suppressed:
                print(f"[GOLD] {suppress_reason}")
                shadow_msg = suppress_reason
            else:
                ok, reason, ctx = validate_for_execution(
                    parsed, live, proximity_hits, df_15m, df_5m, macro, events, state, nav, ema=ema, ema50=ema50
                )
                if ok:
                    exec_ctx = ctx
                    shadow_id, shadow_msg = shadow_execute(setup, exec_ctx)
                    if shadow_id:
                        state["daily"]["executed_trades"] = int(state["daily"].get("executed_trades",0))+1
                else:
                    shadow_msg = reason
                    mark_duplicate(state, sig_key, price)

        state["recent_signals"].append({
            "direction": setup, "confidence": confidence,
            "outcome": "EXECUTED" if shadow_id else ("SKIPPED" if setup!="NO TRADE" else "NONE"),
            "entry": parsed.get("ENTRY"), "datetime": now_utc.isoformat(),
        })
        state["recent_signals"] = state["recent_signals"][-20:]
        save_state(state)

        open_pos_005 = get_open_position(OANDA_ACCOUNT_ID)
        msg = format_telegram_message(
            price, proximity_hits, parsed, session, now_str, events, macro,
            shadow_id=shadow_id, shadow_msg=shadow_msg, ema=ema,
            balance=balance, open_pos_005=open_pos_005, ema50=ema50, exec_ctx=exec_ctx
        )
        send_telegram(msg)
        log_signal_to_api(INSTRUMENT, parsed, price, session, macro, ema, shadow_id, shadow_msg)
        print(f"[GOLD] Done — Setup: {setup} | Conf: {confidence} | Shadow: {shadow_id or shadow_msg}")

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[GOLD] Error: {e}\n{tb}")
        err_str = str(e)
        if "401" in err_str or "Insufficient authorization" in err_str: friendly = "OANDA API token expired."
        elif "529" in err_str or "Overloaded" in err_str: friendly = "Anthropic API overloaded."
        elif "ConnectionError" in err_str or "timeout" in err_str.lower(): friendly = "Network connection error."
        else: friendly = err_str[:200]
        tb_lines = [l for l in tb.strip().split("\n") if l.strip()]
        last_tb  = tb_lines[-1] if tb_lines else ""
        send_telegram(f"⚠️ <b>Gold Monitor Error</b>\n<b>What:</b> {friendly}\n<b>Detail:</b> <code>{last_tb[:150]}</code>")


if __name__ == "__main__":
    main()
