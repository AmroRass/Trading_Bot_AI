"""
gold_monitor.py - XAU/USD trade monitor

Account 005: Manual trading only — alerts sent, no auto-execution
Account 006: Full auto-execution on all valid signals (proper guards apply)

Guards for account 006:
  1. Valid LONG or SHORT setup
  2. Minimum stop distance 20 pts
  3. Minimum R:R 2.5:1
  4. LONG blocked near today_high/prev_high
  5. SHORT blocked near support unless price confirmed breakdown below
  6. No open XAU_USD position on 006
  7. No high impact news within 30 mins
"""
import os
import json
import time
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import oandapyV20.endpoints.accounts as accounts_ep
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.trades as trades_ep
from dotenv import load_dotenv
from anthropic import Anthropic
from ai_trade_pipeline import AITradePipeline, AITradePipelineConfig
load_dotenv()

OANDA_ACCESS_TOKEN = os.getenv("OANDA_ACCESS_TOKEN")
OANDA_ACCOUNT_ID   = os.getenv("OANDA_ACCOUNT_ID")
OANDA_ENVIRONMENT  = os.getenv("OANDA_ENVIRONMENT", "practice")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")
FINNHUB_API_KEY    = os.getenv("FINNHUB_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

INSTRUMENT        = "XAU_USD"
ACCOUNT_SIZE      = 10000
RISK_PCT          = 0.01
MIN_RR            = 2.5
MIN_STOP_PTS      = 20
SHADOW_ACCOUNT_ID = "101-004-37417354-006"
SHADOW_UNITS      = 5
LEVEL_PROXIMITY   = 25
NEWS_BUFFER_MIN   = 30
STATE_FILE        = "/tmp/gold_state.json"

# AI pipeline is observer-only here.
# It audits what it WOULD do, but it must not place orders.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

AI_GOLD_DRY_RUN = os.getenv("AI_GOLD_DRY_RUN", "true").lower() == "true"
AI_GOLD_EXECUTION_ENABLED = os.getenv("AI_GOLD_EXECUTION_ENABLED", "false").lower() == "true"
AI_GOLD_AUDIT_DIR = os.path.join(LOG_DIR, "gold_ai_dry_run")
AI_GOLD_AUDIT_DB = os.path.join(AI_GOLD_AUDIT_DIR, "decision_audit.db")

if AI_GOLD_EXECUTION_ENABLED:
    raise RuntimeError("AI_GOLD_EXECUTION_ENABLED must stay false during dry-run phase")

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

EC2_API = "http://localhost:5000"
oanda = oandapyV20.API(access_token=OANDA_ACCESS_TOKEN, environment=OANDA_ENVIRONMENT)


# ── STATE ──────────────────────────────────────────────────────────────────────

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                return json.load(f)
    except: pass
    return {"recent_signals": [], "last_state_str": ""}

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except: pass

def build_state_str(state, macro, price, shadow_pos):
    bias_code = "SBEAR" if "STRONG BEARISH" in macro.get("trend","") else \
                "BEAR"  if "BEARISH"        in macro.get("trend","") else \
                "SBULL" if "STRONG BULLISH" in macro.get("trend","") else \
                "BULL"  if "BULLISH"        in macro.get("trend","") else "RANG"
    if shadow_pos:
        side = "SH" if shadow_pos["side"] == "SHORT" else "LG"
        pl   = round(shadow_pos["unrealized_pl"], 0)
        trade_str = f"{side}|PL:{pl:+.0f}"
    else:
        trade_str = "FLAT"
    recent = state.get("recent_signals", [])[-3:]
    sig_codes = []
    for s in recent:
        d = s.get("direction","NT")
        c = s.get("confidence","L")[0]
        o = s.get("outcome","?")[0] if s.get("outcome") else "?"
        sig_codes.append(f"{d[0]}{c}:{o}")
    sigs = ",".join(sig_codes) if sig_codes else "none"
    return f"{bias_code}|{price:.0f}|{trade_str}|HIST:{sigs}"


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
                mins = (dt - now).total_seconds() / 60
                if -60 <= mins <= 1440:
                    relevant.append({"name": e.get("event",""), "time": dt.strftime("%H:%M UTC %d %b"), "minutes_away": round(mins)})
            except: continue
        return relevant[:5]
    except Exception as e:
        print(f"[GOLD] Calendar error: {e}")
        return []

def get_event_warning(events):
    for e in events:
        ma = e["minutes_away"]
        if 0 <= ma <= NEWS_BUFFER_MIN:
            return f"⚠️ <b>HIGH IMPACT EVENT IN {ma} MINS</b>\n{e['name']} — stand aside"
        if -15 <= ma < 0:
            return f"⚠️ <b>EVENT JUST RELEASED ({abs(ma)} mins ago)</b>\n{e['name']} — expect volatility"
    return ""

def get_candles(granularity, count):
    params = {"count": count, "granularity": granularity, "price": "M"}
    r = instruments.InstrumentsCandles(INSTRUMENT, params=params)
    rv = oanda.request(r)
    rows = []
    for c in rv.get("candles", []):
        mid = c.get("mid", {})
        rows.append({"time": pd.to_datetime(c["time"]), "open": float(mid.get("o",0)),
                     "high": float(mid.get("h",0)), "low": float(mid.get("l",0)),
                     "close": float(mid.get("c",0)), "volume": int(c.get("volume",0))})
    df = pd.DataFrame(rows)
    df.set_index("time", inplace=True)
    return df

def get_account_balance():
    try:
        r = accounts_ep.AccountSummary(OANDA_ACCOUNT_ID)
        rv = oanda.request(r)
        return float(rv["account"]["balance"])
    except: return ACCOUNT_SIZE

def get_open_position(account_id=None):
    try:
        acct = account_id or OANDA_ACCOUNT_ID
        r = trades_ep.OpenTrades(acct)
        rv = oanda.request(r)
        open_trades = [t for t in rv.get("trades", []) if t.get("instrument") == INSTRUMENT]
        if not open_trades:
            return None
        total_units = sum(float(t["currentUnits"]) for t in open_trades)
        total_pl    = sum(float(t.get("unrealizedPL", 0)) for t in open_trades)
        return {
            "units":         abs(total_units),
            "side":          "LONG" if total_units > 0 else "SHORT",
            "unrealized_pl": total_pl,
            "trade_count":   len(open_trades),
        }
    except Exception as e:
        print(f"[GOLD] Could not fetch open trades for {account_id}: {e}")
        return None

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
    if len(df_daily) < 5:
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
            hits.append({"name": name, "level": level,
                         "distance": round(abs(price-level),1), "above": price>level})
    return hits

def get_level_direction_context(proximity_hits):
    lines = []
    for h in proximity_hits:
        name = h['name']; dist = h['distance']
        if name == 'today_high':
            lines.append(f"⚠️ TODAY HIGH ({h['level']:.2f}, {dist:.1f}pts) = RESISTANCE. SHORT only. LONG invalid here.")
        elif name == 'today_low':
            lines.append(f"✅ TODAY LOW ({h['level']:.2f}, {dist:.1f}pts) = SUPPORT. LONG bounce in bullish trend. In bearish trend, SHORT here is valid — price likely breaking down.")
        elif name == 'prev_high':
            lines.append(f"⚠️ PREV HIGH ({h['level']:.2f}, {dist:.1f}pts) = RESISTANCE. SHORT only unless confirmed breakout.")
        elif name == 'prev_low':
            lines.append(f"✅ PREV LOW ({h['level']:.2f}, {dist:.1f}pts) = SUPPORT. LONG bounce or SHORT breakdown in bearish trend.")
    return "\n".join(lines) if lines else "No specific level direction context."

def get_structural_levels(price, levels, macro):
    import math
    candidates = {}
    for name, val in levels.items():
        if val is not None:
            candidates[name.replace("_"," ").title()] = float(val)
    if macro.get("weekly_high"): candidates["Weekly High"] = float(macro["weekly_high"])
    if macro.get("weekly_low"):  candidates["Weekly Low"]  = float(macro["weekly_low"])
    base = math.floor(price/50)*50
    for mult in range(-6,7):
        level = base + mult*50
        if abs(price-level) <= 300:
            candidates[f"Round {int(level)}"] = float(level)
    above = sorted([{"name":n,"level":v} for n,v in candidates.items() if v>price+2], key=lambda x:x["level"])
    below = sorted([{"name":n,"level":v} for n,v in candidates.items() if v<price-2], key=lambda x:x["level"], reverse=True)
    return above, below

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


# ── BREAKEVEN & SESSION MANAGER ────────────────────────────────────────────────

def manage_open_trades():
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
                    rc = trades_ep.TradeClose(SHADOW_ACCOUNT_ID, trade_id, data={"units": str(abs(int(units)))})
                    oanda.request(rc)
                    print(f"[MANAGER] Session close trade {trade_id} P&L ${current_pl:.2f}")
                    send_telegram(f"🔔 <b>Shadow Session Close</b>\nTrade {trade_id} closed\nP&L: ${current_pl:.2f}")
                except Exception as e:
                    print(f"[MANAGER] Session close failed: {e}")
                continue

            if stop_dist <= 0: continue
            pts_in_profit = current_pl / (SHADOW_UNITS if SHADOW_UNITS > 0 else 1)
            if pts_in_profit >= stop_dist:
                breakeven = round(entry_price, 3)
                if is_long and current_sl < breakeven-0.5:
                    try:
                        sl_id = sl_order.get("id")
                        if sl_id:
                            patch_data = {"order":{"price":f"{breakeven:.3f}","timeInForce":"GTC","type":"STOP_LOSS","tradeID":trade_id}}
                            pr = orders.OrderReplace(SHADOW_ACCOUNT_ID, sl_id, data=patch_data)
                            oanda.request(pr)
                            print(f"[MANAGER] Breakeven set trade {trade_id} @ {breakeven}")
                            send_telegram(f"🔒 <b>Shadow Breakeven</b>\nTrade {trade_id} SL → entry @ {breakeven}")
                    except Exception as e:
                        print(f"[MANAGER] Breakeven failed: {e}")
                elif not is_long and current_sl > breakeven+0.5:
                    try:
                        sl_id = sl_order.get("id")
                        if sl_id:
                            patch_data = {"order":{"price":f"{breakeven:.3f}","timeInForce":"GTC","type":"STOP_LOSS","tradeID":trade_id}}
                            pr = orders.OrderReplace(SHADOW_ACCOUNT_ID, sl_id, data=patch_data)
                            oanda.request(pr)
                            print(f"[MANAGER] Breakeven set trade {trade_id} @ {breakeven}")
                            send_telegram(f"🔒 <b>Shadow Breakeven</b>\nTrade {trade_id} SL → entry @ {breakeven}")
                    except Exception as e:
                        print(f"[MANAGER] Breakeven failed: {e}")
    except Exception as e:
        print(f"[MANAGER] Error: {e}")


# ── SHADOW ACCOUNT EXECUTION ───────────────────────────────────────────────────

def shadow_execute(setup, entry, stop, target, proximity_hits, daily_trend=''):
    try:
        if os.getenv("AUTO_EXECUTE", "false").lower() != "true":
            return None, "AUTO_EXECUTE disabled"

        if setup not in ("LONG", "SHORT"):
            return None, "No trade"

        stop_dist = abs(entry - stop)
        if stop_dist < MIN_STOP_PTS:
            return None, f"Stop too tight ({stop_dist:.1f}pts)"

        tp_dist = abs(target - entry)
        rr = tp_dist / stop_dist if stop_dist > 0 else 0
        if rr < MIN_RR:
            return None, f"R:R {rr:.2f} below {MIN_RR} minimum"

        # Block LONG near resistance
        resistance_hit = any(h['name'] in RESISTANCE_LEVELS for h in proximity_hits)
        if setup == "LONG" and resistance_hit:
            return None, "LONG blocked — near resistance"

        # Block SHORT near support ONLY in bullish trends
        support_hit = [h for h in proximity_hits if h['name'] in SUPPORT_LEVELS]
        if setup == "SHORT" and support_hit:
            if all(h['above'] for h in support_hit) and "BULLISH" in daily_trend:
                return None, "SHORT blocked — price above support in bullish trend"

        shadow_pos = get_open_position(SHADOW_ACCOUNT_ID)
        if shadow_pos is not None:
            return None, f"Shadow position already open ({shadow_pos['side']})"

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
        order_id = rv.get("orderFillTransaction",{}).get("id") or rv.get("relatedTransactionIDs",["?"])[0]
        print(f"[SHADOW] ✅ {setup} {SHADOW_UNITS} units | SL:{stop} TP:{target} | ID:{order_id}")
        return order_id, f"Shadow filled {SHADOW_UNITS} units @ market | ID:{order_id}"

    except Exception as e:
        print(f"[SHADOW] Execution failed: {e}")
        return None, str(e)


# ── SIGNAL LOGGING ─────────────────────────────────────────────────────────────

def log_signal_to_api(instrument, parsed, price, session, macro, ema, shadow_id, shadow_msg):
    try:
        setup = parsed.get("SETUP", "NO TRADE")
        now_iso = datetime.now(timezone.utc).isoformat()
        entry=stop=target=rr=stop_dist=None
        if setup in ("LONG","SHORT"):
            try: entry     = float(parsed.get("ENTRY",0)) or None
            except: pass
            try: stop      = float(parsed.get("STOP",0)) or None
            except: pass
            try: target    = float(parsed.get("TARGET",0)) or None
            except: pass
            try: rr        = float(parsed.get("RR","0").replace(":1","").strip()) or None
            except: pass
            try: stop_dist = float(parsed.get("STOP_DIST",0)) or None
            except: pass
        payload = {
            "id":           now_iso+"_"+instrument,
            "datetime":     now_iso,
            "instrument":   instrument,
            "session":      session,
            "price":        price,
            "direction":    setup,
            "confidence":   parsed.get("CONFIDENCE","LOW"),
            "bias":         parsed.get("BIAS",""),
            "reason":       parsed.get("REASON",""),
            "entry":        entry, "stop": stop, "target": target,
            "target_level": parsed.get("TARGET_LEVEL",""),
            "stop_dist":    stop_dist, "rr": rr,
            "ema":          ema["state"] if ema else "",
            "daily_trend":  macro.get("trend",""),
            "executed":     False,
            "exec_msg":     "",
            "skip_reason":  "",
            "shadow_id":    shadow_id,
            "shadow_msg":   shadow_msg,
        }
        requests.post(EC2_API+"/log_signal", json=payload, timeout=5)
        print("[GOLD] Signal logged")
    except Exception as e:
        print(f"[GOLD] Could not log signal: {e}")


# ── CLAUDE PROMPT ──────────────────────────────────────────────────────────────

def ask_claude(price, levels, proximity_hits, df_15m, session, balance, events, macro, ema=None, df_5m=None, state_str="", ema50=None):
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

    prompt = f"""You are a trading assistant analyzing XAU/USD (gold) for a discretionary trader.

INTER-SIGNAL STATE (read carefully — context from previous cycles):
{state_str if state_str else "No prior state — first signal of session."}
Decode: BIAS|PRICE|OPEN_TRADE(SH/LG/FLAT)|PL|HIST:signals(direction+confidence:outcome)
Use this to avoid repeating failed setups and to build on confirmed direction.

Study these real historical examples before analysing the current setup:

✅ GOOD: Mar 16 SHORT @ 5001.9 (+$20, 3.9:1 RR)
Price broke BELOW prev low 5009.7, retested from below, 2 consecutive bearish candles confirmed rejection.
Prev low flipped support→resistance. Stop just above prev low. Target today's low.
KEY: Structural break + retest + momentum confirmation + both trends bearish.

✅ GOOD: Mar 16 LONG @ 4998 (+$90)
Price dropped to today's low 4970, showed reversal candles, broke back above structure.
Consecutive bullish candles closing above the level.
KEY: Bounce off today's low + momentum flip + bullish candles confirming.

✅ GOOD: Mar 18 3x SHORT @ 4946-4969 (+$66 combined)
Daily AND intraday both strongly bearish. Consecutive bearish candles each making lower lows.
KEY: Strongly trending day, price at support breaking down, clean continuation candles.

✅ GOOD: Mar 20 SHORT @ 4612 (+$62)
Strong bearish trend day, price at today's low breaking down, 28pt stop, structural target 56pts away.
KEY: Trending conditions, clear level, adequate stop, structural target far enough away.

❌ BAD: Apr 2 3x LONG @ 4638-4644 (-$308)
Entered LONG below prev low resistance — entered into resistance not from support.
KEY MISTAKE: Buying below resistance hoping it breaks, not buying at support.

❌ BAD: Apr 6 3x LONG @ 4683-4704 (-$253)
Entered LONG when price near TODAY'S HIGH. Daily bullish but entry at resistance.
KEY MISTAKE: Bullish trend does NOT justify LONG at resistance.

❌ BAD: Mar 17 4x SHORT (-$34 combined)
Choppy ranging conditions. Stops of 8-13pts inside noise floor.
KEY MISTAKE: No momentum confirmation, stops too tight.

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

15M CANDLES (last 8):
{candle_str}
INTRADAY TREND: {intraday_trend} ({hh} HH vs {ll} LL)
EMA (9/26): {ema["state"] if ema else "N/A"}
EMA (50):   {ema50_str}

5M CANDLES (last 12):
{candle_5m_str}
5M TREND: {trend_5m} ({hh_5m} HH vs {ll_5m} LL)
Need 2+ consecutive 5m candles in trade direction. Mixed = reduce confidence.

EVENTS:
{events_str}

TARGET RULES:
- Target must be the NEAREST valid structural level that gives {MIN_RR}:1 RR
- Do NOT skip over closer levels to find a better R:R — that's gaming the filter
- If nearest level doesn't give {MIN_RR}:1 → output NO TRADE

CRITICAL RULES:
- LONG near today's high or prev high = INVALID. Max LOW confidence.
- Bullish daily trend does NOT justify LONG at resistance.
- SHORT near today's low or prev low = only valid in BEARISH trend (breakdown likely)
- In BULLISH trend, SHORT near support = INVALID
- Never short into a pullback/bounce — wait for rejection at resistance or confirmed breakdown
- Stop must be at structural invalidation, min {MIN_STOP_PTS}pts from entry
- Choppy 5m candles = MEDIUM at most
- Do not output LOW confidence with a LONG/SHORT setup — output NO TRADE instead
- If STATE shows same setup failed recently, reduce confidence by one level

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
    for line in response.split("\n"):
        if ":" not in line: continue
        key, _, val = line.strip().partition(":")
        key = key.strip().upper()
        if key in {"BIAS","SETUP","REASON","ENTRY","STOP","TARGET","TARGET_LEVEL","STOP_DIST","POSITION_SIZE","RR","CONFIDENCE","STATE"}:
            result[key] = val.strip()
    return result

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




# ── AI PIPELINE DRY-RUN OBSERVER ───────────────────────────────────────────────

class _StaticGoldReviewer:
    """Feeds the already-parsed legacy Claude decision into AITradePipeline."""
    def __init__(self, decision):
        self.decision = decision

    def review_setup(self, market_snapshot):
        return dict(self.decision)

def _ai_safe_float(value, default=None):
    try:
        if value is None:
            return default
        s = str(value).replace(",", "").replace(":1", "").strip()
        if s.upper() in {"N/A", "NA", "NONE", "—", ""}:
            return default
        return float(s)
    except Exception:
        return default

def _ai_count_consecutive_directional(df, direction):
    if df is None or df.empty:
        return 0
    count = 0
    for _, row in df.tail(10).iloc[::-1].iterrows():
        o = float(row["open"])
        c = float(row["close"])
        if direction == "bullish" and c > o:
            count += 1
        elif direction == "bearish" and c < o:
            count += 1
        else:
            break
    return count

def _ai_extension_check(price, ema, df_5m):
    try:
        fast = _ai_safe_float((ema or {}).get("fast"))
        if fast is None or df_5m is None or len(df_5m) < 5:
            return "UNKNOWN"
        recent = df_5m.tail(min(20, len(df_5m)))
        atr = float((recent["high"].astype(float) - recent["low"].astype(float)).abs().mean())
        if atr <= 0:
            return "UNKNOWN"
        mult = abs(float(price) - fast) / atr
        if mult >= 2.0:
            return f"EXTENDED - {mult:.1f}x ATR from EMA9"
        if mult >= 1.5:
            return f"MODERATELY_EXTENDED - {mult:.1f}x ATR from EMA9"
        return f"OK - {mult:.1f}x ATR from EMA9"
    except Exception:
        return "UNKNOWN"

def _legacy_parsed_to_ai_decision(parsed):
    setup = str(parsed.get("SETUP", "NO TRADE")).upper().strip()
    confidence = str(parsed.get("CONFIDENCE", "LOW")).upper().strip()

    if setup in {"LONG", "SHORT"} and confidence != "LOW":
        decision = "ENTER_NOW"
        ai_setup = setup
        entry_style = "BREAKOUT"
    else:
        decision = "NO_TRADE"
        ai_setup = "NONE"
        entry_style = "NONE"

    return {
        "decision": decision,
        "setup": ai_setup,
        "confidence": confidence if confidence in {"HIGH", "MEDIUM", "LOW"} else "LOW",
        "entry_style": entry_style,
        "reason": parsed.get("REASON", "Legacy Claude decision converted for AI dry-run."),
        "risk_comment": "Observer-only. Trading logic remains controlled by legacy gold_monitor.py.",
        "is_late_chase": False,
        "needs_pullback": False,
        "entry": _ai_safe_float(parsed.get("ENTRY")),
        "stop": _ai_safe_float(parsed.get("STOP")),
        "target": _ai_safe_float(parsed.get("TARGET")),
        "rr": _ai_safe_float(parsed.get("RR")),
        "stop_distance": _ai_safe_float(parsed.get("STOP_DIST")),
    }

def _build_ai_gold_snapshot(parsed, price, levels, proximity_hits, df_15m, df_5m, session, macro, ema, ema50, events):
    above_levels, below_levels = get_structural_levels(price, levels, macro)

    next_resistance = above_levels[0]["level"] if above_levels else None
    nearest_support = below_levels[0]["level"] if below_levels else None

    resistance_hits = [h for h in proximity_hits if h.get("name") in RESISTANCE_LEVELS]
    support_hits = [h for h in proximity_hits if h.get("name") in SUPPORT_LEVELS]

    resistance_level = float(resistance_hits[0]["level"]) if resistance_hits else None
    support_level = float(support_hits[0]["level"]) if support_hits else None

    last_close = float(df_15m.iloc[-1]["close"]) if df_15m is not None and len(df_15m) >= 1 else float(price)
    prev_close = float(df_15m.iloc[-2]["close"]) if df_15m is not None and len(df_15m) >= 2 else last_close

    breakout_confirmed = bool(resistance_level is not None and prev_close <= resistance_level and last_close > resistance_level)
    breakdown_confirmed = bool(support_level is not None and prev_close >= support_level and last_close < support_level)

    if breakout_confirmed:
        trigger_direction = "LONG"
        breakout_level = resistance_level
        level_name = resistance_hits[0]["name"]
    elif breakdown_confirmed:
        trigger_direction = "SHORT"
        breakout_level = support_level
        level_name = support_hits[0]["name"]
    else:
        trigger_direction = "NONE"
        breakout_level = resistance_level if resistance_level is not None else support_level
        level_name = proximity_hits[0]["name"] if proximity_hits else ""

    candles_above = 0
    candles_below = 0
    if df_15m is not None and len(df_15m) > 0 and breakout_level is not None:
        closes = df_15m.tail(5)["close"].astype(float)
        candles_above = int((closes > breakout_level).sum())
        candles_below = int((closes < breakout_level).sum())

    ema_state = str((ema or {}).get("state", "")).upper()
    ema_alignment = "bullish" if "BULLISH" in ema_state else "bearish" if "BEARISH" in ema_state else "neutral"
    price_vs_ema50 = "unknown"
    if ema50:
        price_vs_ema50 = "above" if float(price) > float(ema50) else "below"

    if ema_alignment == "bullish" and price_vs_ema50 == "above":
        market_state = "BULLISH"
    elif ema_alignment == "bearish" and price_vs_ema50 == "below":
        market_state = "BEARISH"
    else:
        market_state = macro.get("trend", "RANGING")

    entry = _ai_safe_float(parsed.get("ENTRY"), float(price))

    return {
        "instrument": INSTRUMENT,
        "current_price": float(price),
        "session": session,
        "regime": market_state,
        "daily_trend": macro.get("trend", "UNKNOWN"),
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
        "consecutive_bullish_candles": _ai_count_consecutive_directional(df_5m, "bullish"),
        "consecutive_bearish_candles": _ai_count_consecutive_directional(df_5m, "bearish"),

        "ema_alignment": ema_alignment,
        "price_vs_ema50": price_vs_ema50,
        "extension_check": _ai_extension_check(price, ema, df_5m),
        "distance_from_entry": abs(float(price) - float(entry)) if entry is not None else 0.0,
        "news_nearby": any(-15 <= int(e.get("minutes_away", 9999)) <= NEWS_BUFFER_MIN for e in events),
    }

def run_ai_gold_dry_run(parsed, price, levels, proximity_hits, df_15m, df_5m, session, macro, ema, ema50, events):
    if not AI_GOLD_DRY_RUN:
        return None

    try:
        snapshot = _build_ai_gold_snapshot(
            parsed, price, levels, proximity_hits, df_15m, df_5m,
            session, macro, ema, ema50, events
        )
        ai_decision = _legacy_parsed_to_ai_decision(parsed)

        pipeline = AITradePipeline(
            config=AITradePipelineConfig(
                instrument=INSTRUMENT,
                use_real_claude=False,
                only_review_interesting_setups=False,
                min_rr=float(MIN_RR),
                min_stop_distance=float(MIN_STOP_PTS),
                max_stop_distance=100.0,
                stop_buffer=0.5,
                allow_london=True,
                allow_new_york=True,
                allow_off_hours=False,
                audit_enabled=True,
                audit_db_path=AI_GOLD_AUDIT_DB,
                source="gold_monitor_money_ai_dry_run",
            ),
            reviewer=_StaticGoldReviewer(ai_decision),
        )

        result = pipeline.evaluate_snapshot(snapshot)

        validation = result.get("python_validation") or {}
        print(
            "[AI-DRY-RUN] "
            f"legacy={ai_decision.get('setup')} "
            f"final={result.get('final_action')} "
            f"code={result.get('final_reason_code')} "
            f"rr={validation.get('rr')}"
        )
        print(f"[AI-DRY-RUN] reason={result.get('final_reason')}")
        return result

    except Exception as e:
        print(f"[AI-DRY-RUN] Error: {e}")
        return {
            "final_action": "NO_TRADE",
            "final_reason": str(e),
            "final_reason_code": "AI_DRY_RUN_ERROR",
        }


# ── TELEGRAM MESSAGE ───────────────────────────────────────────────────────────

def format_telegram_message(price, levels, proximity_hits, parsed, session, now_str,
                             events, macro, shadow_id=None, shadow_msg="", ema=None,
                             balance=10000, open_pos_005=None, ema50=None):
    setup  = parsed.get("SETUP","NO TRADE")
    conf   = parsed.get("CONFIDENCE","LOW")
    emoji  = "🟢" if setup=="LONG" else "🔴" if setup=="SHORT" else "⚪"
    semoji = "🟦" if session=="LONDON" else "🟧" if session=="NEW YORK" else "⬜"
    dt     = macro.get("trend","UNKNOWN")
    dt_emoji = "📉📉" if "STRONG BEARISH" in dt else "📉" if "BEARISH" in dt else "📈📈" if "STRONG BULLISH" in dt else "📈" if "BULLISH" in dt else "↔️"

    if setup in ("LONG","SHORT"):
        try:
            _entry = float(parsed.get('ENTRY',0))
            _stop  = float(parsed.get('STOP',0))
            _dist  = abs(_entry-_stop)
            _risk  = balance*RISK_PCT
            if _dist < MIN_STOP_PTS:
                size_str = f"⚠️ Stop too tight ({_dist:.1f}pts)"
            elif _dist > 0:
                size_str = f"{int(round(_risk/_dist,0))} units"
            else:
                size_str = "—"
        except: size_str = "—"

        quality_str = ""
        if conf=="MEDIUM":
            stars, flags = get_medium_quality(parsed, macro, proximity_hits)
            quality_str = f" {stars} {' | '.join(flags)}"

        ema50_str = ""
        if ema50: ema50_str = f" | E50:{'↑' if price>ema50 else '↓'}"

        lines = [
            f"<b>{emoji} GOLD {setup}</b> — {conf}{semoji}",
            f"🕐 {now_str}",
            f"{dt_emoji} {dt}{ema50_str}",
            f"💰 Price: <b>{price:.2f}</b>",
            "",
            f"Entry:  <b>{parsed.get('ENTRY','—')}</b>",
            f"Stop:   {parsed.get('STOP','—')} ({parsed.get('STOP_DIST','—')}pts)",
            f"Target: {parsed.get('TARGET','—')} <i>({parsed.get('TARGET_LEVEL','—')})</i>",
            f"R:R:    {parsed.get('RR','—')} | Size: {size_str}",
            f"EMA:    {ema['state'] if ema else 'N/A'}",
            "",
            f"📝 {parsed.get('REASON','—')}",
        ]
        if quality_str: lines.append(f"Quality:{quality_str}")
        if shadow_id: lines.append(f"\n🤖 <b>006:</b> Auto-executed {SHADOW_UNITS} units | ID:{shadow_id}")
        elif shadow_msg: lines.append(f"\n🤖 <b>006:</b> Skipped — {shadow_msg}")
        if open_pos_005 and setup != open_pos_005["side"]:
            lines += ["", f"⚠️ <b>OPPOSING SIGNAL</b> — open {open_pos_005['side']} on 005 (P&L: ${open_pos_005['unrealized_pl']:.2f})", "Consider closing."]
        warning = get_event_warning(events)
        if warning: lines += ["", warning]
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
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%H:%M UTC  %d %b %Y")
    session = get_session(now_utc.hour)
    print(f"[GOLD] Running at {now_str} | Session: {session}")

    manage_open_trades()

    try:
        df_15m   = get_candles("M15", 100)
        df_5m    = get_candles("M5",  24)
        df_daily = get_candles("D",   20)
        if df_15m.empty:
            print("[GOLD] No candle data — skipping")
            return

        price   = float(df_15m.iloc[-1]["close"])
        balance = get_account_balance()
        levels  = get_key_levels(df_15m, df_daily)
        macro   = get_macro_context(df_daily)
        ema50   = get_ema50(df_15m)
        print(f"[GOLD] Price: {price:.3f} | Daily trend: {macro['trend']}")

        proximity_hits = check_proximity(price, levels)
        if not proximity_hits:
            print("[GOLD] Skipping — not near any key level")
            return

        print("[GOLD] Near key level — calling Claude")
        events     = get_upcoming_events()
        ema        = get_ema_cross(df_15m)
        state      = load_state()
        shadow_pos = get_open_position(SHADOW_ACCOUNT_ID)
        state_str  = build_state_str(state, macro, price, shadow_pos)

        claude_response = ask_claude(price, levels, proximity_hits, df_15m, session,
                                     balance, events, macro, ema=ema, df_5m=df_5m,
                                     state_str=state_str, ema50=ema50)
        print(f"[GOLD] Response:\n{claude_response}")

        parsed = parse_claude_response(claude_response)

        # Recalculate R:R in code — never trust Claude's calculation
        try:
            _e=float(parsed.get("ENTRY",0)); _s=float(parsed.get("STOP",0)); _t=float(parsed.get("TARGET",0))
            _sd=abs(_e-_s); _td=abs(_t-_e)
            if _sd>0 and _td>0: parsed["RR"] = str(round(_td/_sd,2))
            parsed["STOP_DIST"] = str(round(_sd,1))
        except: pass

        ai_dry_run_result = run_ai_gold_dry_run(
            parsed, price, levels, proximity_hits, df_15m, df_5m,
            session, macro, ema, ema50, events
        )

        setup      = parsed.get("SETUP","NO TRADE")
        confidence = parsed.get("CONFIDENCE","LOW")

        state["recent_signals"].append({
            "direction":  setup,
            "confidence": confidence,
            "outcome":    "PENDING",
            "entry":      parsed.get("ENTRY"),
            "datetime":   now_utc.isoformat(),
        })
        state["recent_signals"] = state["recent_signals"][-5:]
        save_state(state)

        open_pos_005 = get_open_position(OANDA_ACCOUNT_ID)

        shadow_id  = None
        shadow_msg = ""
        if setup in ("LONG","SHORT"):
            imminent = any(0 <= e["minutes_away"] <= NEWS_BUFFER_MIN for e in events)
            if imminent:
                shadow_msg = "News imminent — skipped"
            else:
                try:
                    s_entry  = float(parsed.get("ENTRY", price))
                    s_stop   = float(parsed.get("STOP", 0))
                    s_target = float(parsed.get("TARGET", 0))
                    if s_stop > 0 and s_target > 0:
                        shadow_id, shadow_msg = shadow_execute(
                            setup, s_entry, s_stop, s_target, proximity_hits, macro.get('trend','')
                        )
                except Exception as e:
                    shadow_msg = f"Error: {str(e)[:80]}"

        msg = format_telegram_message(
            price, levels, proximity_hits, parsed, session,
            now_str, events, macro,
            shadow_id=shadow_id, shadow_msg=shadow_msg,
            ema=ema, balance=balance, open_pos_005=open_pos_005, ema50=ema50
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
