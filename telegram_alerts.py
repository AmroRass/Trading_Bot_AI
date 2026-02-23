"""
telegram_alerts.py - Sends trade alerts and bot status to Telegram.

Alerts sent:
  - Trade opened (direction, price, TP, SL)
  - Trade closed (TP or SL hit, P&L)
  - Bot error / crash
  - Bot started
"""

import requests
import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")


def send_message(text: str):
    """Send a message to Telegram. Fails silently if unavailable."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       text,
            "parse_mode": "HTML"
        }, timeout=5)
    except Exception as e:
        print(f"[TELEGRAM] Failed to send message: {e}")


def alert_bot_started():
    send_message(
        "🟢 <b>Gold Bot Started</b>\n"
        "EMA 9/50 | ADX≥25 | HTF ON | Session 07-17 UTC"
    )


def alert_trade_opened(side: str, price: float, tp: float, sl: float, reasoning: str = ""):
    emoji = "🟢 BUY" if side == "buy" else "🔴 SELL"
    msg = (
        f"{emoji} <b>Trade Opened</b>\n"
        f"Entry: <b>${price:,.3f}</b>\n"
        f"TP:    ${tp:,.3f}\n"
        f"SL:    ${sl:,.3f}\n"
    )
    if reasoning:
        msg += f"📰 {reasoning[:100]}"
    send_message(msg)


def alert_trade_closed(side: str, entry: float, exit_price: float, result: str, pnl_pct: float):
    emoji = "✅" if result == "TP" else "❌"
    pnl_str = f"+{pnl_pct:.3f}%" if pnl_pct > 0 else f"{pnl_pct:.3f}%"
    msg = (
        f"{emoji} <b>Trade Closed — {result}</b>\n"
        f"Side:  {'BUY' if side == 'buy' else 'SELL'}\n"
        f"Entry: ${entry:,.3f}\n"
        f"Exit:  ${exit_price:,.3f}\n"
        f"P&L:   <b>{pnl_str}</b>"
    )
    send_message(msg)


def alert_error(error_msg: str):
    send_message(
        f"⚠️ <b>Bot Error</b>\n"
        f"<code>{error_msg[:200]}</code>"
    )


def alert_no_credits():
    send_message(
        "💳 <b>Anthropic Credits Exhausted</b>\n"
        "Bot running on technical signals only.\n"
        "Top up at console.anthropic.com"
    )
