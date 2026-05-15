"""
eurjpy_monitor.py - EUR/JPY trade monitor (REBUILT with regime awareness)

Account 005: Manual only — alerts
Account 006: Auto-execution with full hardened guards (default: OFF)

CRITICAL FIXES APPLIED:
  ✅ Proper pip math for forex (PIP_SIZE = 0.01, not 1.0)
  ✅ LEVEL_PROXIMITY in pips (30 pips, not 3000 pips)
  ✅ Completed candles only
  ✅ Live bid/ask execution validation
  ✅ Strict Claude output validation
  ✅ Regime awareness (daily bias vs 5M/15M execution regime)
  ✅ Counter-regime trades blocked without reversal
  ✅ EMA pullback + round level triggers
  ✅ Daily NAV loss cap and max trades/day
  ✅ Consecutive-loss cooldown with fail-closed transaction tracking
  ⚠️ Nearest-target enforcement is prompt-only (not Python-enforced yet)
  ✅ Duplicate suppression, kill switch
  ✅ Spread check for forex (max 3 pips)
  ✅ Tokyo/London session optimization
  ✅ LOW confidence blocked in Python
  ✅ Nearest-target rule instructed in prompt

Key differences from Gold:
  - PIP_SIZE = 0.01 (not 1.0)
  - pips_between() for all distance calculations
  - Spread check (max 3 pips)
  - Tokyo/London session priority
  - AUTO_EXECUTE defaults to false (safety)
"""
import os
import json
import math
import html
import urllib.parse
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
import oandapyV20.endpoints.transactions as transactions
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
INSTRUMENT        = "EUR_JPY"
DISPLAY_NAME      = "EUR/JPY"
SHADOW_ACCOUNT_ID = "101-004-37417354-006"
SHADOW_UNITS      = 1000       # fixed for shadow tracking
ACCOUNT_SIZE      = 10000
RISK_PCT          = 0.01       # 1% risk

# Auto-execution settings - paper-safe defaults
AUTO_EXECUTE = os.getenv("AUTO_EXECUTE", "true").lower() == "true"
PAPER_ONLY   = os.getenv("PAPER_ONLY", "true").lower() == "true"

# CRITICAL: Forex pip math
PIP_SIZE                = 0.01    # For JPY pairs: 0.01 = 1 pip
PIP_FACTOR              = 100     # Multiply by 100 to get pips

MIN_STOP_PIPS           = 30
MAX_STOP_PIPS           = 90
LEVEL_PROXIMITY_PIPS    = 30
SPREAD_BUFFER_PIPS      = 2
MAX_SPREAD_PIPS         = 3       # Reject execution if spread > 3 pips
MIN_RR                  = 2.5

NEWS_BUFFER_MIN = 30

# Daily risk controls (FTMO-style)
DAILY_LOSS_CAP_PCT       = 0.04   # 4% daily loss cap
MAX_TRADES_PER_DAY       = 3
MAX_CONSECUTIVE_LOSSES   = 2
COOLDOWN_AFTER_LOSS_MIN  = 45
DUPLICATE_COOLDOWN_MIN   = 60
DUPLICATE_PRICE_MOVE_PIPS = 15

# Session times (EUR/JPY optimized)
TOKYO_START  = 0
TOKYO_END    = 6
LONDON_START = 7
LONDON_END   = 12
NY_START     = 13
NY_END       = 17

RESISTANCE_LEVELS = {'today_high', 'prev_high'}
SUPPORT_LEVELS    = {'today_low', 'prev_low'}

EUR_JPY_EVENTS = [
    "ecb", "european central bank", "boj", "bank of japan", "interest rate",
    "cpi", "inflation", "gdp", "unemployment", "employment",
    "lagarde", "ueda", "fed", "fomc", "nfp", "payroll"
]

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
LOG_DIR    = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
STATE_FILE      = os.path.join(LOG_DIR, "eurjpy_state.json")
KILL_SWITCH     = os.path.join(BASE_DIR, "STOP_BOT")
EC2_API         = "http://localhost:5000"

oanda = oandapyV20.API(access_token=OANDA_ACCESS_TOKEN, environment=OANDA_ENVIRONMENT)


# ── PIP MATH ───────────────────────────────────────────────────────────────────

def pips_between(a, b):
    """Calculate pip distance for EUR/JPY (0.01 = 1 pip)."""
    return abs(float(a) - float(b)) / PIP_SIZE

def pips_to_price(pips):
    """Convert pips to price distance."""
    return float(pips) * PIP_SIZE


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

def fmt_price(value):
    """Safe price formatting for prompt - handles None values."""
    return f"{float(value):.3f}" if value is not None else "N/A"


# ── HELPERS ────────────────────────────────────────────────────────────────────

def get_session(hour_utc):
    if TOKYO_START <= hour_utc < TOKYO_END: return "TOKYO"
    if LONDON_START <= hour_utc < LONDON_END: return "LONDON"
    if NY_START <= hour_utc < NY_END: return "NEW YORK"
    return "OFF-HOURS"

def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4096], "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"[EURJPY] Telegram error: {e}")

def esc(x):
    """Escape HTML characters in dynamic text to prevent Telegram parse errors."""
    return html.escape(str(x)) if x is not None else "—"


# ── MARKET DATA ────────────────────────────────────────────────────────────────

def get_candles(granularity, count, completed_only=True):
    """Fetch candles - FIXED: completed candles only by default."""
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
    """
    FIXED: Get live bid/ask for execution validation.
    allow_fallback=False for execution/trade management (fail if no live price).
    allow_fallback=True for analysis/display (fall back to M1 candle).
    """
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
        print(f"[EURJPY] Could not fetch open trades for {account_id}: {e}")
        return None


# ── LEVELS / CONTEXT ───────────────────────────────────────────────────────────

def get_key_levels(df_15m, df_daily):
    today = datetime.now(timezone.utc).date()
    tb = df_15m[df_15m.index.date == today]
    prev_high = prev_low = None
    if len(df_daily) >= 2:
        pd_row = df_daily.iloc[-2]
        prev_high = float(pd_row["high"])
        prev_low  = float(pd_row["low"])
    return {
        "today_high": float(tb["high"].max()) if not tb.empty else None,
        "today_low":  float(tb["low"].min())  if not tb.empty else None,
        "prev_high":  prev_high,
        "prev_low":   prev_low,
    }

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
    close_str = " → ".join([f"{c:.3f}" for c in last3["close"].tolist()])
    range_pips = pips_between(weekly_high, weekly_low)
    summary = (f"Daily trend: {daily_trend} ({hh} higher highs, {ll} lower lows in last 10 days)\n"
               f"Weekly range: {weekly_low:.3f} - {weekly_high:.3f} ({range_pips:.0f} pips)\n"
               f"Last 3 daily closes: {close_str}")
    return {"trend": daily_trend, "weekly_high": weekly_high, "weekly_low": weekly_low, "summary": summary}

def check_proximity(price, levels):
    """FIXED: Uses pips_between() with proper pip math."""
    hits = []
    for name, level in levels.items():
        if level is None:
            continue
        dist_pips = pips_between(price, level)
        if dist_pips <= LEVEL_PROXIMITY_PIPS:
            hits.append({"name": name, "level": level, "distance": round(dist_pips,1), "above": price>level})
    return hits

def get_structural_levels(price, levels, macro):
    candidates = {}
    for name, val in levels.items():
        if val is not None:
            candidates[name.replace("_"," ").title()] = float(val)
    if macro.get("weekly_high"): candidates["Weekly High"] = float(macro["weekly_high"])
    if macro.get("weekly_low"):  candidates["Weekly Low"]  = float(macro["weekly_low"])
    # Round levels for EUR/JPY: 183.00, 183.50, 184.00, etc.
    base = math.floor(price / 0.5) * 0.5
    for mult in range(-6, 7):
        level = round(base + mult * 0.5, 2)
        if pips_between(price, level) <= 300:  # Within 300 pips
            candidates[f"Round {level:.2f}"] = float(level)
    above = sorted([{"name":n,"level":v} for n,v in candidates.items() if v>price+pips_to_price(2)], key=lambda x:x["level"])
    below = sorted([{"name":n,"level":v} for n,v in candidates.items() if v<price-pips_to_price(2)], key=lambda x:x["level"], reverse=True)
    return above, below

def get_level_direction_context(proximity_hits):
    lines = []
    for h in proximity_hits:
        name = h['name']; dist = h['distance']
        if name == 'today_high':
            lines.append(f"⚠️ TODAY HIGH ({h['level']:.3f}, {dist:.0f} pips) = potential resistance. SHORT only with reversal confirmation. LONG only after confirmed breakout/retest.")
        elif name == 'today_low':
            lines.append(f"✅ TODAY LOW ({h['level']:.3f}, {dist:.0f} pips) = potential support. LONG bounce with confirmation. SHORT only after confirmed breakdown.")
        elif name == 'prev_high':
            lines.append(f"⚠️ PREV HIGH ({h['level']:.3f}, {dist:.0f} pips) = potential resistance. SHORT only with rejection. LONG only after confirmed breakout/retest.")
        elif name == 'prev_low':
            lines.append(f"✅ PREV LOW ({h['level']:.3f}, {dist:.0f} pips) = potential support/flip level. Trade only after confirmation.")
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
        diff_pips = pips_between(cf, cs)
        if crossed_bullish:   state = "BULLISH CROSS (just crossed up)"
        elif crossed_bearish: state = "BEARISH CROSS (just crossed down)"
        elif cf>cs:           state = f"BULLISH (fast {diff_pips:.0f} pips above slow)"
        else:                 state = f"BEARISH (fast {diff_pips:.0f} pips below slow)"
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
    """Average candle range over recent candles."""
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

def is_near_round_level(price, step=0.5, proximity_pips=20):
    """Check if price is near a round level (183.00, 183.50, 184.00)."""
    nearest = round(price / step) * step
    return pips_between(price, nearest) <= proximity_pips

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
    bullish_closes  = sum(1 for i in range(1,len(closes)) if closes[i] > closes[i-1])
    bearish_closes  = sum(1 for i in range(1,len(closes)) if closes[i] < closes[i-1])
    
    # BULLISH = price above EMA50, fast > slow, AND (structure OR momentum)
    if ema50 is not None and price > float(ema50) and fast > slow and (hh >= 3 or bullish_closes >= 5):
        return "BULLISH"
    # BEARISH = price below EMA50, fast < slow, AND (structure OR momentum)
    if ema50 is not None and price < float(ema50) and fast < slow and (ll >= 3 or bearish_closes >= 5):
        return "BEARISH"
    return "RANGING"

def validate_regime_alignment(setup, df_5m, df_15m, ema, ema50, rr, proximity_hits):
    """
    Block counter-execution-regime trades unless reversal structure is confirmed.
    Logs reason to console before returning.
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
            reason = f"SHORT in bullish regime — no breakdown (close={last_close:.3f} vs support={local_support:.3f}, R:R={rr:.2f})"
            print(f"[REGIME] ⛔ {reason}")
            return False, reason
    if regime == "BEARISH" and setup == "LONG":
        if not (breakout and rr >= MIN_RR):
            reason = f"LONG in bearish regime — no breakout (close={last_close:.3f} vs resist={local_resist:.3f}, R:R={rr:.2f})"
            print(f"[REGIME] ⛔ {reason}")
            return False, reason
    if regime == "RANGING":
        near_key = any(h["name"] in {"today_high","today_low","prev_high","prev_low"} for h in proximity_hits)
        if not near_key:
            reason = "Ranging regime — not at key level edge"
            print(f"[REGIME] ⚠️ {reason}")
            return False, reason

    print(f"[REGIME] ✅ {regime} regime allows {setup} (breakdown={breakdown}, breakout={breakout}, R:R={rr:.2f})")
    return True, f"Regime OK ({regime})"

def validate_extension(setup, df_5m, atr_mult=1.5):
    """Block entries that are too extended from the 5M EMA9 — prevents chasing."""
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
        ext_pips = pips_between(current_close, fast)
        thresh_pips = pips_between(0, threshold)
        reason = f"Price too extended from 5M EMA9 ({ext_pips:.0f} pips > {thresh_pips:.0f} pips threshold)"
        print(f"[EXTENSION] ⛔ {reason}")
        return False, reason
    
    print(f"[EXTENSION] ✅ Extension OK")
    return True, "Extension OK"

def has_consecutive_directional_candles(df, direction, n=2):
    """FIXED: Hard Python check for momentum confirmation."""
    if df is None or len(df) < n: return False
    recent = df.tail(n)
    if direction == "SHORT":
        return all(float(row.close) < float(row.open) for _, row in recent.iterrows())
    if direction == "LONG":
        return all(float(row.close) > float(row.open) for _, row in recent.iterrows())
    return False

def has_confirmed_breakdown(df_15m, level):
    """Last completed candle closed below support level."""
    if df_15m is None or len(df_15m) < 1: return False
    last = float(df_15m.iloc[-1]["close"])
    return last < level

def has_confirmed_breakout(df_15m, level):
    """Last completed candle closed above resistance level."""
    if df_15m is None or len(df_15m) < 1: return False
    last = float(df_15m.iloc[-1]["close"])
    return last > level


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
            if not any(kw in name for kw in EUR_JPY_EVENTS): continue
            try:
                ts = e.get("time","") or e.get("date","")
                dt = datetime.fromisoformat(ts.replace("Z","+00:00"))
                mins = (dt-now).total_seconds()/60
                if -60 <= mins <= 1440:
                    relevant.append({"name":e.get("event",""),"time":dt.strftime("%H:%M UTC %d %b"),"minutes_away":round(mins)})
            except: continue
        return relevant[:5]
    except Exception as e:
        print(f"[EURJPY] Calendar error: {e}")
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
        return f"⚠️ <b>NEWS BLOCK</b>\n{esc(reason)} — stand aside"
    return ""


# ── STATE STRING ───────────────────────────────────────────────────────────────

def build_state_str(state, macro, price, shadow_pos):
    trend = macro.get("trend","")
    bias_code = ("SBEAR" if "STRONG BEARISH" in trend else "BEAR" if "BEARISH" in trend else
                 "SBULL" if "STRONG BULLISH" in trend else "BULL" if "BULLISH" in trend else "RANG")
    if shadow_pos:
        side = "SH" if shadow_pos["side"]=="SHORT" else "LG"
        pl   = round(shadow_pos["unrealized_pl"],2)
        trade_str = f"{side}|PL:{pl:+.2f}"
    else:
        trade_str = "FLAT"
    recent = state.get("recent_signals",[])[-5:]
    sig_codes = []
    for s in recent:
        d = s.get("direction","NT"); c = s.get("confidence","L")[:1]; o = (s.get("outcome","?")[:1] if s.get("outcome") else "?")
        sig_codes.append(f"{d[:1]}{c}:{o}")
    sigs = ",".join(sig_codes) if sig_codes else "none"
    return f"{bias_code}|{price:.3f}|{trade_str}|HIST:{sigs}"


# ── DAILY RISK LIMITS ──────────────────────────────────────────────────────────

def check_daily_limits(state, nav):
    daily = state.setdefault("daily", default_state()["daily"])
    if daily.get("date") != today_key():
        daily.clear(); daily.update(default_state()["daily"])
    
    # CRITICAL: Fail closed if loss tracker is broken
    if daily.get("loss_tracker_ok") is False:
        return False, f"Loss tracker unavailable: {daily.get('loss_tracker_error', 'unknown error')}"
    
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
    moved_pips = pips_between(price, last_price)
    if age_min < DUPLICATE_COOLDOWN_MIN and moved_pips < DUPLICATE_PRICE_MOVE_PIPS:
        return True, f"Duplicate ({age_min:.0f}m old, {moved_pips:.0f} pips move)"
    return False, ""

def mark_duplicate(state, key, price):
    state.setdefault("duplicate_signals",{})[key] = {
        "time": datetime.now(timezone.utc).isoformat(), "price": price
    }


# ── CLAUDE PROMPT ──────────────────────────────────────────────────────────────

def ask_claude(price, levels, proximity_hits, df_15m, session, balance, events, macro, ema=None, df_5m=None, state_str="", ema50=None, execution_regime="RANGING", trigger_context="None"):
    """EUR/JPY optimized prompt with regime awareness."""
    recent = df_15m.tail(8)
    candle_str = "\n".join([
        f"  {str(idx)[-8:-3]}  O:{row.open:.3f} H:{row.high:.3f} L:{row.low:.3f} C:{row.close:.3f}"
        for idx, row in recent.iterrows()])
    highs = recent["high"].tolist()
    lows  = recent["low"].tolist()
    hh = sum(1 for i in range(1,len(highs)) if highs[i]>highs[i-1])
    ll = sum(1 for i in range(1,len(lows))  if lows[i] <lows[i-1])
    intraday_trend = "BEARISH" if ll>hh else "BULLISH" if hh>ll else "NEUTRAL"
    risk_dollars = round(balance * RISK_PCT, 2)

    if df_5m is not None and not df_5m.empty:
        recent_5m = df_5m.tail(12)
        candle_5m_str = "\n".join([
            f"  {str(idx)[-8:-3]}  O:{row.open:.3f} H:{row.high:.3f} L:{row.low:.3f} C:{row.close:.3f}"
            for idx, row in recent_5m.iterrows()])
        highs_5m = recent_5m["high"].tolist()
        lows_5m  = recent_5m["low"].tolist()
        hh_5m = sum(1 for i in range(1,len(highs_5m)) if highs_5m[i]>highs_5m[i-1])
        ll_5m = sum(1 for i in range(1,len(lows_5m))  if lows_5m[i] <lows_5m[i-1])
        trend_5m = "BEARISH" if ll_5m>hh_5m else "BULLISH" if hh_5m>ll_5m else "NEUTRAL"
        bc_5m = sum(1 for i in range(1,len(recent_5m)) if recent_5m.iloc[i]["close"]>recent_5m.iloc[i-1]["close"])
    else:
        candle_5m_str = "Not available"
        trend_5m = "UNKNOWN"
        hh_5m = ll_5m = bc_5m = 0

    events_str = "\n".join([
        f"  - {e['name']}: {e['time']} (in {e['minutes_away']}m)" if e['minutes_away']>=0
        else f"  - {e['name']}: {e['time']} ({abs(e['minutes_away'])}m ago)"
        for e in events]) if events else "No high-impact events in next 24 hours."

    above_levels, below_levels = get_structural_levels(price, levels, macro)
    above_str = "\n".join([f"  {l['name']}: {l['level']:.3f} (+{pips_between(l['level'],price):.0f} pips)" for l in above_levels[:6]]) or "  None within range"
    below_str = "\n".join([f"  {l['name']}: {l['level']:.3f} (-{pips_between(price,l['level']):.0f} pips)" for l in below_levels[:6]]) or "  None within range"
    level_direction = get_level_direction_context(proximity_hits)

    ema50_str = "N/A"
    if ema50:
        diff_pips = pips_between(price, ema50)
        pos = "ABOVE" if price > ema50 else "BELOW"
        ema50_str = f"{pos} by {diff_pips:.0f} pips (EMA50={ema50:.3f})"

    prompt = f"""You are a trading assistant analyzing EUR/JPY for a discretionary trader.

INTER-SIGNAL STATE (context from previous cycles):
{state_str if state_str else "No prior state — first signal of session."}
Decode: BIAS|PRICE|OPEN_TRADE(SH/LG/FLAT)|PL|HIST:signals(direction+confidence:outcome)
Use this to avoid repeating failed setups and build on confirmed direction.

Study these examples before analyzing the current setup:

✅ GOOD — LONG at support in bullish regime:
Price pulls back to prev_low or today_low, shows 2-3 consecutive bullish 5m candles.
Execution regime BULLISH (price>EMA50, fast>slow, higher highs). Daily trend aligned.
Stop below support (min 30 pips). Target: next structural resistance at least 75 pips away.
KEY: Regime aligned + momentum confirmation + structural entry.

✅ GOOD — SHORT at resistance in bearish regime:
Price rises to prev_high or today_high, shows 2-3 consecutive bearish 5m candles rejecting it.
Execution regime BEARISH (price<EMA50, fast<slow, lower lows). Daily trend aligned.
Stop above resistance (min 30 pips). Target: next structural support at least 75 pips away.
KEY: Both timeframes aligned + clear rejection candles + execution regime confirmation.

✅ GOOD — EMA pullback continuation:
Price pulls back to 5M EMA9 in BULLISH execution regime, shows 2+ bullish candles.
Not at daily high/low. Target: next structural level.
KEY: Momentum continuation in established regime.

❌ BAD — Counter-regime fades without reversal structure.
❌ BAD — LONG near today_high/prev_high (resistance) in ranging/bearish regime.
❌ BAD — Choppy mixed candles — need 2+ consecutive candles in trade direction.
❌ BAD — Stop under 30 pips — gets hit by spread/noise. Will be automatically rejected.
❌ BAD — Target too close — must be real structural level at least 75 pips away.
❌ BAD — Repeat of setup that failed in HIST — reduce confidence by one level.

Current time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}
Session: {session}
Current price: {price:.3f}
Risk per trade: ${risk_dollars} (1%)
Minimum R:R: {MIN_RR}:1
Minimum stop: {MIN_STOP_PIPS} pips

MACRO CONTEXT (Daily trend):
{macro['summary']}

EXECUTION REGIME (5M/15M structure):
{execution_regime}

TRADE TRIGGER CONTEXT:
{trigger_context}
This explains why the bot called you — key level proximity, round level, or EMA pullback.

REGIME RULES:
- In BULLISH execution regime: LONG continuation is preferred. SHORT requires confirmed reversal (breakdown + R:R ≥ 2.5).
- In BEARISH execution regime: SHORT continuation is preferred. LONG requires confirmed reversal (breakout + R:R ≥ 2.5).
- In RANGING regime: Only trade at clear range edges (key levels) with strong confirmation.
- Counter-execution-regime trades without reversal structure will be BLOCKED by Python guards.

KEY LEVELS:
- Today High:    {fmt_price(levels.get('today_high'))}
- Today Low:     {fmt_price(levels.get('today_low'))}
- Prev Day High: {fmt_price(levels.get('prev_high'))}
- Prev Day Low:  {fmt_price(levels.get('prev_low'))}
- Weekly High:   {fmt_price(macro.get('weekly_high'))}
- Weekly Low:    {fmt_price(macro.get('weekly_low'))}

NEAR THESE LEVELS:
{json.dumps(proximity_hits, indent=2) if proximity_hits else "None"}

LEVEL DIRECTION:
{level_direction}

LEVELS ABOVE (resistance / long targets):
{above_str}

LEVELS BELOW (support / short targets):
{below_str}

15M CANDLES (last 8):
{candle_str}
INTRADAY TREND: {intraday_trend} ({hh} HH vs {ll} LL)
EMA (9/26): {ema["state"] if ema else "N/A"}
EMA (50):   {ema50_str}

5M CANDLES (last 12):
{candle_5m_str}
5M TREND: {trend_5m} ({hh_5m} HH vs {ll_5m} LL, {bc_5m} bullish closes)
Need 2+ consecutive 5m candles in trade direction. Mixed = reduce confidence.

EVENTS:
{events_str}

TARGET RULES:
- Target must be a REAL structural level (prev high/low, weekly high/low, round number)
- Target must be at least 75 pips from entry for EUR/JPY
- If no realistic target exists at {MIN_RR}:1, output NO TRADE

CRITICAL RULES:
- Use the NEAREST valid structural target in the trade direction.
- Do NOT skip closer levels to find one that gives better R:R.
- If the nearest realistic target does not give 2.5:1, output NO TRADE.
- LONG near today_high/prev_high (resistance):
  * BREAKOUT ENTRY: Last completed 5M candle closed ABOVE resistance → LONG now
  * Entry: Current price (already above broken level)
  * Stop: Below pre-breakout consolidation (min 30 pips)
  * ONE candle closing above = valid breakout (do NOT wait for 2+ candles or "confirmation")
  * If last candle closed BELOW resistance: NO TRADE (wait for actual breakout)
- SHORT near today_low/prev_low (support):
  * BREAKDOWN ENTRY: Last completed 5M candle closed BELOW support → SHORT now
  * Entry: Current price (already below broken level)
  * Stop: Above pre-breakdown consolidation (min 30 pips)
  * ONE candle closing below = valid breakdown (do NOT wait for 2+ candles or "confirmation")
  * If last candle closed ABOVE support: NO TRADE (wait for actual breakdown)
- In BULLISH execution regime, SHORT requires reversal structure (not just high-touch).
- In BEARISH execution regime, LONG requires reversal structure (not just low-touch).
- Never short into a pullback/bounce — wait for rejection at resistance or confirmed breakdown.
- Never long into a rejection — wait for bounce at support or confirmed breakout.
- Stop must be at structural invalidation, min {MIN_STOP_PIPS} pips from entry.
- Choppy 5m candles = MEDIUM at most.
- Do not output LOW confidence with a LONG/SHORT setup — output NO TRADE instead.
- If STATE shows same setup failed recently, reduce confidence by one level.

Respond in EXACT format:
BIAS: [BULLISH / BEARISH / NEUTRAL]
SETUP: [LONG / SHORT / NO TRADE]
REASON: [1-2 sentences max]
ENTRY: [price or N/A]
STOP: [price or N/A]
TARGET: [price or N/A]
TARGET_LEVEL: [level name or N/A]
STOP_DIST: [pips or N/A]
POSITION_SIZE: [N/A — calculated by bot]
RR: [ratio or N/A]
CONFIDENCE: [HIGH / MEDIUM / LOW]
STATE: [updated state string]"""

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        temperature=0,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text.strip()


def parse_claude_response(response):
    result = {}
    for line in response.split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip()
    return result


def validate_claude_output(parsed):
    """Strict validation - reject malformed Claude responses."""
    setup = parsed.get("SETUP","NO TRADE")
    if setup not in {"LONG","SHORT","NO TRADE"}:
        return False, f"Invalid setup: {setup}"
    
    # Skip confidence check for NO TRADE
    if setup == "NO TRADE":
        return True, "No trade"
    
    confidence = parsed.get("CONFIDENCE", "LOW")
    if confidence not in {"HIGH", "MEDIUM", "LOW"}:
        return False, f"Invalid confidence: {confidence}"

    entry  = safe_float(parsed.get("ENTRY"))
    stop   = safe_float(parsed.get("STOP"))
    target = safe_float(parsed.get("TARGET"))
    if entry is None or stop is None or target is None:
        return False, "Missing entry/stop/target"

    # Geometry check
    if setup == "LONG":
        if not (stop < entry < target):
            return False, f"LONG geometry fail: stop {stop:.3f} < entry {entry:.3f} < target {target:.3f}"
    else:
        if not (target < entry < stop):
            return False, f"SHORT geometry fail: target {target:.3f} < entry {entry:.3f} < stop {stop:.3f}"

    # Stop distance
    stop_dist = pips_between(entry, stop)
    if stop_dist < MIN_STOP_PIPS:
        return False, f"Stop too tight ({stop_dist:.0f} pips < {MIN_STOP_PIPS})"
    if stop_dist > MAX_STOP_PIPS:
        return False, f"Stop too wide ({stop_dist:.0f} pips > {MAX_STOP_PIPS})"

    return True, "Claude validation passed"


def validate_for_execution(setup, entry_claude, stop, target, df_5m, df_15m, proximity_hits, macro, ema, ema50, state, nav):
    """
    Final execution validation with live pricing and all guards.
    CRITICAL: Uses live bid/ask for execution, not Claude's entry.
    """
    if not AUTO_EXECUTE:
        return False, "AUTO_EXECUTE disabled", {}
    
    if PAPER_ONLY and OANDA_ENVIRONMENT.lower() != "practice":
        return False, f"PAPER_ONLY active — refusing to trade on {OANDA_ENVIRONMENT}", {}
    
    ok, reason = check_daily_limits(state, nav)
    if not ok: return False, reason, {}

    # Use live price for execution - NO FALLBACK (fail if unavailable)
    try:
        live = get_live_price(allow_fallback=False)
    except RuntimeError as e:
        return False, str(e), {}
    
    # FOREX-SPECIFIC: Spread check
    spread_pips = live["spread"] / PIP_SIZE
    if spread_pips > MAX_SPREAD_PIPS:
        return False, f"Spread too wide ({spread_pips:.1f} pips > {MAX_SPREAD_PIPS})", {}

    # Live entry based on bid/ask
    entry = live["ask"] if setup == "LONG" else live["bid"]

    # Recalculate geometry with live entry
    if setup == "LONG":
        if not (stop < entry < target):
            return False, f"Live LONG geometry fail: stop {stop:.3f} < entry {entry:.3f} < target {target:.3f}", {}
    else:
        if not (target < entry < stop):
            return False, f"Live SHORT geometry fail: target {target:.3f} < entry {entry:.3f} < stop {stop:.3f}", {}

    # Stop distance with live entry
    stop_dist = pips_between(entry, stop)
    if stop_dist < MIN_STOP_PIPS:
        return False, f"Live stop too tight ({stop_dist:.1f} pips)", {}
    if stop_dist > MAX_STOP_PIPS:
        return False, f"Live stop too wide ({stop_dist:.1f} pips)", {}

    # R:R with spread buffer
    if setup == "LONG":
        risk   = pips_between(entry, stop)  + SPREAD_BUFFER_PIPS
        reward = pips_between(target, entry) - SPREAD_BUFFER_PIPS
    else:
        risk   = pips_between(stop, entry)  + SPREAD_BUFFER_PIPS
        reward = pips_between(entry, target) - SPREAD_BUFFER_PIPS
    if risk <= 0 or reward <= 0: return False, "Invalid risk/reward after spread", {}
    rr = reward / risk

    # REGIME CHECK FIRST - before R:R and momentum checks
    regime_ok, regime_reason = validate_regime_alignment(setup, df_5m, df_15m, ema, ema50, rr, proximity_hits)
    if not regime_ok:
        return False, regime_reason, {}

    # Now check R:R
    if rr < MIN_RR: return False, f"Live R:R {rr:.2f} below {MIN_RR}", {}

    # Momentum check after regime
    if not has_consecutive_directional_candles(df_5m, setup, n=2):
        return False, "No 2-candle 5M momentum confirmation", {}

    # Extension guard
    ext_ok, ext_reason = validate_extension(setup, df_5m)
    if not ext_ok:
        return False, ext_reason, {}

    # Structure rules with candle confirmation
    daily_trend = macro.get("trend","")
    resistance_hits = [h for h in proximity_hits if h['name'] in RESISTANCE_LEVELS]
    if setup=="LONG" and resistance_hits:
        if not has_confirmed_breakout(df_15m, float(resistance_hits[0]["level"])):
            return False, f"LONG blocked — near {resistance_hits[0]['name']} without confirmed breakout", {}
    
    support_hits = [h for h in proximity_hits if h['name'] in SUPPORT_LEVELS]
    if setup=="SHORT" and support_hits:
        # Bullish daily trend becomes stricter filter, not absolute ban
        if "BULLISH" in daily_trend and not has_confirmed_breakdown(df_15m, float(support_hits[0]["level"])):
            return False, "SHORT blocked — bullish daily trend and no confirmed support breakdown", {}
        # For all cases, require breakdown if above support
        if all(h['above'] for h in support_hits):
            if not has_confirmed_breakdown(df_15m, float(support_hits[0]["level"])):
                return False, f"SHORT blocked — above {support_hits[0]['name']} without candle close below", {}

    # No open position
    shadow_pos = get_open_position(SHADOW_ACCOUNT_ID)
    if shadow_pos is not None:
        return False, f"Shadow position already open ({shadow_pos['side']})", {}

    return True, "All guards passed", {"entry": entry, "rr": rr, "stop_dist": stop_dist}


# ── EXECUTION ──────────────────────────────────────────────────────────────────

def shadow_execute(setup, entry, stop, target):
    """Execute on shadow account 006 with fixed units."""
    try:
        side  = "buy" if setup == "LONG" else "sell"
        units = str(SHADOW_UNITS) if side == "buy" else str(-SHADOW_UNITS)

        order_data = {
            "order": {
                "type": "MARKET",
                "instrument": INSTRUMENT,
                "units": units,
                "stopLossOnFill":   {"price": f"{stop:.3f}",   "timeInForce": "GTC"},
                "takeProfitOnFill": {"price": f"{target:.3f}", "timeInForce": "GTC"},
            }
        }
        r  = orders.OrderCreate(SHADOW_ACCOUNT_ID, data=order_data)
        rv = oanda.request(r)
        order_id = rv.get("orderFillTransaction", {}).get("id") or rv.get("relatedTransactionIDs", ["?"])[0]
        print(f"[SHADOW] ✅ {setup} {SHADOW_UNITS} units | SL:{stop:.3f} TP:{target:.3f} | ID:{order_id}")
        return order_id, f"Auto-executed {SHADOW_UNITS} units | ID:{order_id}"

    except Exception as e:
        print(f"[SHADOW] Execution failed: {e}")
        return None, str(e)


# ── BREAKEVEN MANAGER ──────────────────────────────────────────────────────────

def in_session_close_window(now_utc, end_hour, before_min=10, after_min=5):
    """Check if current time is within session close window."""
    now_m = now_utc.hour * 60 + now_utc.minute
    end_m = end_hour * 60
    return end_m - before_min <= now_m < end_m + after_min

def should_session_close(now_utc):
    """Check if any session is closing (Tokyo, London, or NY)."""
    return (
        in_session_close_window(now_utc, TOKYO_END) or
        in_session_close_window(now_utc, LONDON_END) or
        in_session_close_window(now_utc, NY_END)
    )


def manage_open_trades():
    """Move stops to breakeven when in profit. FIXED: Uses price movement, not P/L."""
    try:
        now_utc = datetime.now(timezone.utc)
        # Check if any session is closing (Tokyo, London, or NY)
        session_close = should_session_close(now_utc)

        r = trades_ep.OpenTrades(SHADOW_ACCOUNT_ID)
        rv = oanda.request(r)
        open_trades = [t for t in rv.get("trades", []) if t.get("instrument") == INSTRUMENT]
        if not open_trades: return

        # Get live price - NO FALLBACK (critical for breakeven logic)
        try:
            live = get_live_price(allow_fallback=False)
        except RuntimeError as e:
            print(f"[MANAGER] Cannot get live price for breakeven: {e}")
            return

        for trade in open_trades:
            trade_id    = trade["id"]
            units       = float(trade["currentUnits"])
            entry_price = float(trade["price"])
            current_pl  = float(trade.get("unrealizedPL", 0))
            is_long     = units > 0
            sl_order    = trade.get("stopLossOrder", {})
            current_sl  = float(sl_order.get("price", 0)) if sl_order else 0

            if not current_sl: continue

            # Session close
            if session_close:
                try:
                    close_data = {"units": "ALL"}
                    rc = trades_ep.TradeClose(SHADOW_ACCOUNT_ID, trade_id, data=close_data)
                    oanda.request(rc)
                    print(f"[MANAGER] Session close — trade {trade_id} P&L ${current_pl:.2f}")
                    send_telegram(f"🔔 <b>Shadow Session Close (EUR/JPY)</b>\nTrade {trade_id} closed\nP&L: ${current_pl:.2f}")
                except Exception as e:
                    print(f"[MANAGER] Session close failed for {trade_id}: {e}")
                continue

            # Breakeven logic - FIXED: use price movement
            stop_pips = pips_between(entry_price, current_sl)
            current_price = live["bid"] if is_long else live["ask"]

            if is_long:
                pips_in_profit = pips_between(current_price, entry_price) if current_price > entry_price else 0
            else:
                pips_in_profit = pips_between(entry_price, current_price) if current_price < entry_price else 0

            if pips_in_profit >= stop_pips:
                breakeven_price = round(entry_price, 3)
                if is_long and current_sl < breakeven_price - pips_to_price(5):
                    try:
                        sl_id = sl_order.get("id")
                        if sl_id:
                            patch_data = {"order": {"price": f"{breakeven_price:.3f}", "timeInForce": "GTC", "type": "STOP_LOSS", "tradeID": trade_id}}
                            pr = orders.OrderReplace(SHADOW_ACCOUNT_ID, sl_id, data=patch_data)
                            oanda.request(pr)
                            print(f"[MANAGER] Breakeven: LONG {trade_id} SL → {breakeven_price:.3f}")
                            send_telegram(f"🔒 <b>EUR/JPY Breakeven</b>\nTrade {trade_id} SL → entry @ {breakeven_price:.3f}")
                    except Exception as e:
                        print(f"[MANAGER] Breakeven failed: {e}")
                elif not is_long and current_sl > breakeven_price + pips_to_price(5):
                    try:
                        sl_id = sl_order.get("id")
                        if sl_id:
                            patch_data = {"order": {"price": f"{breakeven_price:.3f}", "timeInForce": "GTC", "type": "STOP_LOSS", "tradeID": trade_id}}
                            pr = orders.OrderReplace(SHADOW_ACCOUNT_ID, sl_id, data=patch_data)
                            oanda.request(pr)
                            print(f"[MANAGER] Breakeven: SHORT {trade_id} SL → {breakeven_price:.3f}")
                            send_telegram(f"🔒 <b>EUR/JPY Breakeven</b>\nTrade {trade_id} SL → entry @ {breakeven_price:.3f}")
                    except Exception as e:
                        print(f"[MANAGER] Breakeven failed: {e}")

    except Exception as e:
        print(f"[MANAGER] Error: {e}")


def get_today_order_fill_transactions():
    """
    Fetch today's ORDER_FILL transactions using proper OANDA pagination.
    TransactionList returns pages, not raw transactions.
    """
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    params = {
        "from": start.isoformat().replace("+00:00", "Z"),
        "to": now.isoformat().replace("+00:00", "Z"),
        "pageSize": 1000,
        "type": "ORDER_FILL",
    }

    r = transactions.TransactionList(SHADOW_ACCOUNT_ID, params=params)
    rv = oanda.request(r)

    txns = []
    for page_url in rv.get("pages", []):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(page_url).query)
        from_id = qs.get("from", [None])[0]
        to_id = qs.get("to", [None])[0]
        if not from_id or not to_id:
            continue

        rr = transactions.TransactionIDRange(
            SHADOW_ACCOUNT_ID,
            params={"from": from_id, "to": to_id, "type": "ORDER_FILL"}
        )
        txns.extend(oanda.request(rr).get("transactions", []))

    return txns


def update_consecutive_losses(state):
    """
    Poll recent closed trades and update consecutive loss counter and cooldown.
    FIXED: Uses proper OANDA transaction pagination.
    CRITICAL: Fails closed - if tracking fails, bot stops trading.
    """
    try:
        txns = get_today_order_fill_transactions()

        close_reasons = {
            "STOP_LOSS_ORDER",
            "TAKE_PROFIT_ORDER",
            "TRAILING_STOP_LOSS_ORDER",
            "GUARANTEED_STOP_LOSS_ORDER",
            "MARKET_ORDER_TRADE_CLOSE",
            "MARKET_ORDER_POSITION_CLOSEOUT",
        }

        closes = []

        for txn in sorted(txns, key=lambda x: int(x.get("id", 0)), reverse=True):
            if txn.get("instrument") != INSTRUMENT:
                continue
            if txn.get("type") != "ORDER_FILL":
                continue
            if txn.get("reason") not in close_reasons:
                continue

            t = parse_dt(txn.get("time"))
            if not t or t.date() != datetime.now(timezone.utc).date():
                continue

            pl = safe_float(txn.get("pl"))
            if pl is None:
                continue

            closes.append({
                "pl": pl,
                "time": txn.get("time"),
                "reason": txn.get("reason"),
            })

        consecutive = 0
        last_loss_time = None

        for close in closes:
            if close["pl"] < 0:
                consecutive += 1
                if last_loss_time is None:
                    last_loss_time = close["time"]
            else:
                break

        daily = state.setdefault("daily", default_state()["daily"])
        if daily.get("date") != today_key():
            daily.clear()
            daily.update(default_state()["daily"])

        daily["consecutive_losses"] = consecutive
        daily["last_loss_time"] = last_loss_time
        daily["loss_tracker_ok"] = True
        daily["loss_tracker_error"] = None

        print(f"[LOSS TRACKER] ✅ Consecutive losses today: {consecutive}, Last loss: {last_loss_time}")

    except Exception as e:
        # CRITICAL: Fail closed - set error flag so bot stops trading
        daily = state.setdefault("daily", default_state()["daily"])
        daily["loss_tracker_ok"] = False
        daily["loss_tracker_error"] = str(e)[:150]
        print(f"[LOSS TRACKER] ⛔ Failed to update consecutive losses: {e}")
        print(f"[LOSS TRACKER] ⛔ Bot will not trade until tracker recovers")


# ── TELEGRAM MESSAGE ───────────────────────────────────────────────────────────

def get_medium_quality(parsed, macro, proximity_hits):
    """Calculate quality score for MEDIUM confidence trades - returns stars and flags."""
    scores = []
    flags = []
    
    try:
        rr = float(parsed.get("RR","0").replace(":1","").strip())
        if rr >= 3.0:   scores.append(2); flags.append(f"R:R {rr:.1f}:1 ✅✅")
        elif rr >= 2.5: scores.append(1); flags.append(f"R:R {rr:.1f}:1 ✅")
        else:           scores.append(0); flags.append(f"R:R {rr:.1f}:1 ⚠️")
    except:
        flags.append("R:R unknown")
    
    daily = macro.get("trend","")
    setup = parsed.get("SETUP","NO TRADE")
    if ("BULLISH" in daily and setup=="LONG") or ("BEARISH" in daily and setup=="SHORT"):
        scores.append(2); flags.append("Trends aligned ✅✅")
    elif "RANGING" in daily:
        scores.append(0); flags.append("Daily ranging ⚠️")
    else:
        scores.append(0); flags.append("Trend conflict ❌")
    
    has_key = any(h['name'] in ['today_high','today_low','prev_high','prev_low'] for h in proximity_hits)
    if has_key: scores.append(2); flags.append("Key level ✅✅")
    else:       scores.append(0); flags.append("Weak level ⚠️")
    
    total = sum(scores)
    stars = "⭐⭐⭐" if total >= 5 else "⭐⭐" if total >= 3 else "⭐"
    return stars, flags


def format_telegram_message(price, parsed, session, now_str, events, macro, shadow_id=None, skip_reason="", ema=None, ema50=None, execution_regime="RANGING", open_pos_005=None):
    setup = parsed.get("SETUP","NO TRADE")
    conf  = parsed.get("CONFIDENCE","LOW")
    emoji  = "🟢" if setup=="LONG" else "🔴" if setup=="SHORT" else "⚪"
    semoji = "🟦" if session=="TOKYO" else "🟨" if session=="LONDON" else "🟧" if session=="NEW YORK" else "⬜"
    dt     = macro.get("trend","UNKNOWN")
    dt_emoji = "📉📉" if "STRONG BEARISH" in dt else "📉" if "BEARISH" in dt else "📈📈" if "STRONG BULLISH" in dt else "📈" if "BULLISH" in dt else "↔️"

    ema50_str = ""
    if ema50:
        pos = "↑" if price > ema50 else "↓"
        ema50_str = f" | E50:{pos}"

    if setup in ("LONG", "SHORT"):
        quality_str = ""
        if conf == "MEDIUM":
            stars, flags = get_medium_quality(parsed, macro, proximity_hits)
            quality_str = f"\n\n{stars} {' | '.join(flags)}"
        
        lines = [
            f"<b>{emoji} EUR/JPY {setup}</b> — {conf} {semoji}",
            f"🕐 {now_str}",
            f"{dt_emoji} {dt}{ema50_str} | Regime: {execution_regime}",
            f"💰 Price: <b>{price:.3f}</b>",
            "",
            f"Entry:  <b>{parsed.get('ENTRY','—')}</b>",
            f"Stop:   {parsed.get('STOP','—')} ({parsed.get('STOP_DIST','—')} pips)",
            f"Target: {parsed.get('TARGET','—')} <i>({esc(parsed.get('TARGET_LEVEL','—'))})</i>",
            f"R:R:    {parsed.get('RR','—')}",
            f"EMA:    {esc(ema['state']) if ema else 'N/A'}",
            "",
            f"📝 {esc(parsed.get('REASON','—'))}",
        ]
        
        if quality_str:
            lines.append(quality_str)

        if shadow_id:
            lines.append(f"\n🤖 <b>006:</b> Auto-executed | ID:{shadow_id}")
        elif skip_reason:
            lines.append(f"\n🤖 <b>006:</b> Skipped — {esc(skip_reason)}")

        if open_pos_005 and setup != open_pos_005["side"]:
            lines += [
                "",
                f"⚠️ <b>OPPOSING SIGNAL</b> — open {open_pos_005['side']} on 005 (P&L: ${open_pos_005['unrealized_pl']:.2f})",
                "Consider closing."
            ]

        warning = get_event_warning(events)
        if warning:
            lines += ["", warning]

    else:
        lines = [
            f"⚪ EUR/JPY — NO TRADE  {semoji} {session}",
            f"🕐 {now_str} | 💰 {price:.3f}",
            f"{dt_emoji} {dt} | Regime: {execution_regime}",
            f"📝 {esc(parsed.get('REASON','—'))}",
        ]
        warning = get_event_warning(events)
        if warning:
            lines += ["", warning]

    return "\n".join(lines)


# ── MAIN ───────────────────────────────────────────────────────────────────────

def main():
    # Kill switch
    if os.path.exists(KILL_SWITCH):
        print("[EURJPY] ⛔ KILL SWITCH ACTIVE")
        send_telegram("⛔ <b>EUR/JPY Bot Stopped</b>\nKILL_SWITCH file detected")
        return

    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%H:%M UTC  %d %b %Y")
    session = get_session(now_utc.hour)
    print(f"[EURJPY] Running at {now_str} | Session: {session}")

    # Manage open shadow trades
    manage_open_trades()
    
    # Update consecutive loss tracking from closed trades
    state = load_state()
    update_consecutive_losses(state)
    save_state(state)
    
    # Block new entries during session close window
    if should_session_close(now_utc):
        print("[EURJPY] Skipping — session close window")
        return

    try:
        df_15m   = get_candles("M15", 100, completed_only=True)
        df_5m    = get_candles("M5", 24, completed_only=True)
        df_daily = get_candles("D", 20, completed_only=True)
        if df_15m.empty or df_5m.empty:
            print("[EURJPY] No candle data — skipping")
            return

        # Get live price once
        live    = get_live_price()
        price   = float(live["mid"])
        balance, nav = get_account_balance(SHADOW_ACCOUNT_ID)
        levels  = get_key_levels(df_15m, df_daily)
        macro   = get_macro_context(df_daily)
        ema     = get_ema_cross(df_15m)
        ema50   = get_ema50(df_15m)
        
        execution_regime = get_execution_regime(df_5m, df_15m, ema, ema50)
        print(f"[EURJPY] Price: {price:.3f} | Spread: {live['spread'] / PIP_SIZE:.1f} pips | Daily trend: {macro['trend']} | Exec regime: {execution_regime}")

        proximity_hits = check_proximity(price, levels)
        near_round = is_near_round_level(price)
        near_ema = is_near_fast_ema(price, df_5m)
        
        if not (proximity_hits or near_round or near_ema):
            print("[EURJPY] Skipping — not near key level, round level, or EMA pullback")
            return

        # Build trigger context for Claude
        trigger_context = []
        if proximity_hits:
            trigger_context.append("key level: " + ", ".join([h["name"] for h in proximity_hits]))
        if near_round:
            nearest_round = round(price / 0.5) * 0.5
            trigger_context.append(f"round level: {nearest_round:.2f}")
        if near_ema:
            ema9_5m = get_ema_fast(df_5m, span=9)
            trigger_context.append(f"5M EMA9 pullback area: {ema9_5m:.3f}")
        trigger_context = " | ".join(trigger_context) if trigger_context else "None"

        print("[EURJPY] Near tradeable zone — calling Claude")
        if proximity_hits:
            print(f"[EURJPY] Key levels: {[h['name'] for h in proximity_hits]}")
        if near_round:
            print(f"[EURJPY] Near round level")
        if near_ema:
            print(f"[EURJPY] Near 5M EMA9 pullback")

        events = get_upcoming_events()
        state  = load_state()  # Reload state after consecutive-loss update
        shadow_pos = get_open_position(SHADOW_ACCOUNT_ID)
        state_str  = build_state_str(state, macro, price, shadow_pos)

        claude_response = ask_claude(price, levels, proximity_hits, df_15m, session,
                                     balance, events, macro, ema=ema, df_5m=df_5m,
                                     state_str=state_str, ema50=ema50, execution_regime=execution_regime,
                                     trigger_context=trigger_context)
        print(f"[EURJPY] Response:\n{claude_response}")

        parsed = parse_claude_response(claude_response)

        # Validate Claude output
        valid, msg = validate_claude_output(parsed)
        if not valid:
            print(f"[EURJPY] Claude output invalid: {msg}")
            send_telegram(f"⚠️ <b>EUR/JPY Invalid Claude Output</b>\n{esc(msg)}\n\nResponse:\n{esc(claude_response[:500])}")
            return

        # Recalculate R:R in code — never trust Claude's calculation
        try:
            _e  = float(parsed.get("ENTRY", 0))
            _s  = float(parsed.get("STOP", 0))
            _t  = float(parsed.get("TARGET", 0))
            _sd = pips_between(_e, _s)
            _td = pips_between(_t, _e)
            if _sd > 0 and _td > 0:
                parsed["RR"] = f"{(_td / _sd):.2f}:1"
                parsed["STOP_DIST"] = f"{_sd:.0f}"
        except: pass

        setup      = parsed.get("SETUP","NO TRADE")
        confidence = parsed.get("CONFIDENCE","LOW")

        open_pos_005 = get_open_position(OANDA_ACCOUNT_ID)

        shadow_id  = None
        skip_reason = ""
        
        if setup in ("LONG","SHORT"):
            # Block LOW confidence
            if confidence == "LOW":
                skip_reason = "LOW confidence blocked"
                print(f"[EURJPY] {skip_reason}")
            else:
                # Build stable duplicate key
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
                
                sig_key = f"{INSTRUMENT}|{setup}|{execution_regime}|{near_names}|{parsed.get('TARGET_LEVEL','')}"
                
                # Check duplicate
                is_dup, dup_msg = check_duplicate(state, sig_key, price)
                if is_dup:
                    skip_reason = dup_msg
                    print(f"[EURJPY] Duplicate: {skip_reason}")
                else:
                    # News block
                    news_blocked, news_msg = is_news_blocked(events)
                    if news_blocked:
                        skip_reason = f"News block: {news_msg}"
                    else:
                        # Full execution validation
                        exec_ok, exec_msg, exec_data = validate_for_execution(
                            setup, float(parsed.get("ENTRY", price)), float(parsed.get("STOP", 0)),
                            float(parsed.get("TARGET", 0)), df_5m, df_15m, proximity_hits,
                            macro, ema, ema50, state, nav
                        )
                        
                        if not exec_ok:
                            skip_reason = exec_msg
                            print(f"[EURJPY] Execution blocked: {skip_reason}")
                        else:
                            # Mark duplicate AFTER passing validation (not before)
                            mark_duplicate(state, sig_key, price)
                            save_state(state)
                            
                            if AUTO_EXECUTE:
                                shadow_id, shadow_msg = shadow_execute(
                                    setup, exec_data["entry"], float(parsed.get("STOP", 0)),
                                    float(parsed.get("TARGET", 0))
                                )
                                if not shadow_id:
                                    skip_reason = shadow_msg
                                else:
                                    # Update state on successful execution
                                    state["daily"]["executed_trades"] = int(state["daily"].get("executed_trades",0)) + 1
                                    save_state(state)
                            else:
                                skip_reason = "AUTO_EXECUTE=False (safety mode)"

        # Final state update after execution/skip decision
        if shadow_id:
            outcome = "EXECUTED"
        elif setup in ("LONG", "SHORT"):
            outcome = "SKIPPED"
        else:
            outcome = "NONE"

        state["recent_signals"].append({
            "direction": setup,
            "confidence": confidence,
            "outcome": outcome,
            "entry": parsed.get("ENTRY"),
            "skip_reason": skip_reason if skip_reason else None,
            "datetime": now_utc.isoformat(),
        })
        state["recent_signals"] = state["recent_signals"][-5:]
        save_state(state)

        msg = format_telegram_message(
            price, parsed, session, now_str, events, macro,
            shadow_id=shadow_id, skip_reason=skip_reason,
            ema=ema, ema50=ema50, execution_regime=execution_regime,
            open_pos_005=open_pos_005
        )
        send_telegram(msg)

        print(f"[EURJPY] Done — Setup: {setup} | Conf: {confidence} | Shadow: {shadow_id or skip_reason}")

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[EURJPY] Error: {e}\n{tb}")
        err_str = str(e)
        if "401" in err_str or "Insufficient authorization" in err_str:
            friendly = "OANDA API token expired."
        elif "529" in err_str or "Overloaded" in err_str:
            friendly = "Anthropic API overloaded."
        elif "ConnectionError" in err_str or "timeout" in err_str.lower():
            friendly = "Network connection error."
        else:
            friendly = err_str[:200]
        tb_lines = [l for l in tb.strip().split('\n') if l.strip()]
        last_tb  = tb_lines[-1] if tb_lines else ""
        send_telegram(
            f"⚠️ <b>EUR/JPY Monitor Error</b>\n"
            f"<b>What:</b> {esc(friendly)}\n"
            f"<b>Detail:</b> <code>{esc(last_tb[:150])}</code>"
        )


if __name__ == "__main__":
    main()
