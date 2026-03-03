"""
telegram_alerts.py - Sends trade alerts and bot status to Telegram.
"""

import requests
import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_LIMIT     = 4096


def send_message(text: str):
    """Send a message to Telegram. Truncates if over 4096 chars."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM] Missing credentials — skipping")
        return
    try:
        if len(text) > TELEGRAM_LIMIT:
            text = text[:TELEGRAM_LIMIT - 3] + "..."
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       text,
            "parse_mode": "HTML"
        }, timeout=5)
        if not resp.ok:
            print(f"[TELEGRAM] Failed: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        print(f"[TELEGRAM] Error: {e}")


def alert_bot_started():
    from config import TRADE_CONFIG, SESSION_CONFIG
    htf     = "ON" if TRADE_CONFIG.get("htf_confirmation") else "OFF"
    session = (
        f"{SESSION_CONFIG['start_hour_utc']:02d}:00-{SESSION_CONFIG['end_hour_utc']:02d}:00 UTC"
        if SESSION_CONFIG["enabled"] else "24/7"
    )
    tf = TRADE_CONFIG.get("timeframe", "15")
    send_message(
        f"🟢 <b>Gold Bot Started</b>\n"
        f"EMA {TRADE_CONFIG['ema_fast']}/{TRADE_CONFIG['ema_slow']} | "
        f"ADX≥{TRADE_CONFIG['adx_threshold']} | "
        f"HTF {htf} | "
        f"{tf}min candles | "
        f"Session Gold Market Hours (Sun 22:00 - Fri 22:00 UTC)"
    )


def alert_trade_opened(side: str, price: float, tp: float, sl: float, reasoning: str = ""):
    emoji = "🟢 BUY" if side == "buy" else "🔴 SELL"
    try:
        msg = (
            f"{emoji} <b>Trade Opened</b>\n"
            f"Entry: <b>${price:,.3f}</b>\n"
            f"TP:    ${tp:,.3f}\n"
            f"SL:    ${sl:,.3f}\n"
        )
    except (ValueError, TypeError):
        msg = (
            f"{emoji} <b>Trade Opened</b>\n"
            f"Entry: <b>{price}</b>\n"
            f"TP:    {tp}\n"
            f"SL:    {sl}\n"
        )
    if reasoning:
        msg += f"📰 {reasoning[:150]}"
    send_message(msg)


def alert_trade_closed(side: str, entry: float, exit_price: float, result: str, pnl_pct: float):
    emoji   = "✅" if result == "TP" else "❌"
    pnl_str = f"+{pnl_pct:.3f}%" if pnl_pct > 0 else f"{pnl_pct:.3f}%"
    send_message(
        f"{emoji} <b>Trade Closed — {result}</b>\n"
        f"Side:  {'BUY' if side == 'buy' else 'SELL'}\n"
        f"Entry: ${entry:,.3f}\n"
        f"Exit:  ${exit_price:,.3f}\n"
        f"P&L:   <b>{pnl_str}</b>"
    )


def alert_error(error_msg: str):
    send_message(
        f"⚠️ <b>Bot Error</b>\n"
        f"<code>{error_msg[:300]}</code>"
    )


def alert_no_credits():
    send_message(
        f"💳 <b>Anthropic Credits Exhausted</b>\n"
        f"Bot running on technical signals only.\n"
        f"Top up at console.anthropic.com"
    )
