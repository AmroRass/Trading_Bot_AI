"""
telegram_alerts.py - Clean Telegram alerts with dollar PnL and balance.
"""
import requests
import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_LIMIT     = 4096

def send_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM] Missing credentials")
        return
    try:
        if len(text) > TELEGRAM_LIMIT:
            text = text[:TELEGRAM_LIMIT - 3] + "..."
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=5)
        if not resp.ok:
            print(f"[TELEGRAM] Failed: {resp.status_code}")
    except Exception as e:
        print(f"[TELEGRAM] Error: {e}")

def _bot_name():
    try:
        from config import BOT_NAME
        return BOT_NAME
    except ImportError:
        return "Gold Bot"

def _hr():
    return "━" * 20

def alert_bot_started(balance=0):
    from config import TRADE_CONFIG
    tf   = TRADE_CONFIG.get("timeframe", "5")
    mode = TRADE_CONFIG.get("conflict_mode", "conservative").upper()
    send_message(
        f"<b>🟢 {_bot_name()} Started</b>
"
        f"{_hr()}
"
        f"EMA {TRADE_CONFIG[chr(39)+chr(101)+chr(109)+chr(97)+chr(95)+chr(102)+chr(97)+chr(115)+chr(116)+chr(39)]}/{TRADE_CONFIG[chr(39)+chr(101)+chr(109)+chr(97)+chr(95)+chr(115)+chr(108)+chr(111)+chr(119)+chr(39)]} | ADX>={TRADE_CONFIG[chr(39)+chr(97)+chr(100)+chr(120)+chr(95)+chr(116)+chr(104)+chr(114)+chr(101)+chr(115)+chr(104)+chr(111)+chr(108)+chr(100)+chr(39)]} | {tf}min
"
        f"Session: 07:00-12:00 UTC | 13:30-17:00 UTC
"
        f"Balance: <b>${balance:,.2f}</b>"
    )

def alert_trade_opened(side, price, tp, sl, tp_dollar, sl_dollar, units, score, reasoning=""):
    emoji = "🟢 BUY" if side == "buy" else "🔴 SELL"
    send_message(
        f"{emoji} <b>Trade Opened</b> — {_bot_name()}
"
        f"{_hr()}
"
        f"Entry:  <b>${price:,.3f}</b>
"
        f"TP:     ${tp:,.3f}  (+${tp_dollar:.2f})
"
        f"SL:     ${sl:,.3f}  (-${sl_dollar:.2f})
"
        f"Units:  {units}
"
        f"Score:  {score}/8
"
        f"📰 {reasoning[:150] if reasoning else chr(39)+chr(39)}"
    )

def alert_trade_closed(side, entry, exit_price, result, pnl_dollar, balance):
    emoji     = "✅" if result == "TP" else "❌"
    pnl_str   = f"+${pnl_dollar:.2f}" if pnl_dollar >= 0 else f"-${abs(pnl_dollar):.2f}"
    pnl_emoji = "📈" if pnl_dollar >= 0 else "📉"
    send_message(
        f"{emoji} <b>Trade Closed — {result}</b> — {_bot_name()}
"
        f"{_hr()}
"
        f"Side:    {chr(39)+chr(66)+chr(85)+chr(89)+chr(39) if side == chr(39)+chr(98)+chr(117)+chr(121)+chr(39) else chr(39)+chr(83)+chr(69)+chr(76)+chr(76)+chr(39)}
"
        f"Entry:   ${entry:,.3f}
"
        f"Exit:    ${exit_price:,.3f}
"
        f"PnL:     <b>{pnl_emoji} {pnl_str}</b>
"
        f"Balance: <b>${balance:,.2f}</b>"
    )

def alert_standing_down(reason):
    send_message(
        f"⏸ <b>Standing Down</b> — {_bot_name()}
"
        f"{_hr()}
"
        f"{reason}"
    )

def alert_error(error_msg):
    send_message(f"⚠️ <b>Error</b> — {_bot_name()}
<code>{error_msg[:300]}</code>")

def alert_no_credits():
    send_message(f"💳 <b>Credits Exhausted</b> — {_bot_name()}
Top up at console.anthropic.com")
