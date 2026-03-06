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
        resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=5)
        if not resp.ok:
            print(f"[TELEGRAM] Failed: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        print(f"[TELEGRAM] Error: {e}")

def _bot_name():
    try:
        from config import BOT_NAME
        return BOT_NAME
    except ImportError:
        return "Gold Bot"

def _hr():
    return "\u2501" * 20

def alert_bot_started(balance=0):
    from config import TRADE_CONFIG
    tf   = TRADE_CONFIG.get("timeframe", "5")
    mode = TRADE_CONFIG.get("conflict_mode", "conservative").upper()
    ema_fast  = TRADE_CONFIG.get("ema_fast", 9)
    ema_slow  = TRADE_CONFIG.get("ema_slow", 50)
    adx_threshold = TRADE_CONFIG.get("adx_threshold", 25)
    send_message(
        "<b>" + _bot_name() + " Started</b>\n" +
        _hr() + "\n" +
        "EMA " + str(ema_fast) + "/" + str(ema_slow) + " | ADX>=" + str(adx_threshold) + " | " + str(tf) + "min\n" +
        "Session: 07:00-12:00 UTC | 13:30-17:00 UTC\n" +
        "Balance: <b>$" + f"{balance:,.2f}" + "</b>"
    )

def alert_trade_opened(side, price, tp, sl, tp_dollar, sl_dollar, units, score, reasoning=""):
    emoji = "BUY" if side == "buy" else "SELL"
    send_message(
        "<b>" + emoji + " Trade Opened</b> - " + _bot_name() + "\n" +
        _hr() + "\n" +
        "Entry:  <b>$" + f"{price:,.3f}" + "</b>\n" +
        "TP:     $" + f"{tp:,.3f}" + "  (+$" + f"{tp_dollar:.2f}" + ")\n" +
        "SL:     $" + f"{sl:,.3f}" + "  (-$" + f"{sl_dollar:.2f}" + ")\n" +
        "Units:  " + str(units) + "\n" +
        "Score:  " + str(score) + "/8\n" +
        (reasoning[:150] if reasoning else "")
    )

def alert_trade_closed(side, entry, exit_price, result, pnl_dollar, balance):
    emoji   = "CLOSED TP" if result == "TP" else "CLOSED SL"
    pnl_str = "+$" + f"{pnl_dollar:.2f}" if pnl_dollar >= 0 else "-$" + f"{abs(pnl_dollar):.2f}"
    send_message(
        "<b>" + emoji + "</b> - " + _bot_name() + "\n" +
        _hr() + "\n" +
        "Side:    " + ("BUY" if side == "buy" else "SELL") + "\n" +
        "Entry:   $" + f"{entry:,.3f}" + "\n" +
        "Exit:    $" + f"{exit_price:,.3f}" + "\n" +
        "PnL:     <b>" + pnl_str + "</b>\n" +
        "Balance: <b>$" + f"{balance:,.2f}" + "</b>"
    )

def alert_standing_down(reason):
    send_message("<b>Standing Down</b> - " + _bot_name() + "\n" + _hr() + "\n" + reason)

def alert_error(error_msg):
    send_message("<b>Error</b> - " + _bot_name() + "\n<code>" + error_msg[:300] + "</code>")

def alert_no_credits():
    send_message("<b>Credits Exhausted</b> - " + _bot_name() + "\nTop up at console.anthropic.com")
