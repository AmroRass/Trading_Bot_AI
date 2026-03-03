"""
daily_summary.py - Sends a daily trading summary to Telegram via Claude.

Fetches actual trade results directly from OANDA instead of reading CSV.

Run via cron at 17:00 UTC (session close):
  0 17 * * 1-5 cd /home/ec2-user/moneymaker && python3 daily_summary.py
"""

import os
import requests
import anthropic
from datetime import datetime, timezone, date, timedelta
from telegram_alerts import send_message
from config import ANTHROPIC_API_KEY
from dotenv import load_dotenv

load_dotenv()

OANDA_TOKEN   = os.getenv("OANDA_ACCESS_TOKEN")
OANDA_ACCOUNT = os.getenv("OANDA_ACCOUNT_ID")
OANDA_BASE    = "https://api-fxpractice.oanda.com/v3"
HEADERS       = {"Authorization": f"Bearer {OANDA_TOKEN}"}

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def get_todays_closed_trades() -> list:
    """Fetch today's closed trades directly from OANDA."""
    try:
        resp = requests.get(
            f"{OANDA_BASE}/accounts/{OANDA_ACCOUNT}/trades?state=CLOSED&count=100",
            headers=HEADERS, timeout=10
        )
        trades = resp.json().get("trades", [])

        today = date.today().isoformat()
        todays_trades = []

        for t in trades:
            close_time = t.get("closeTime", "")
            if close_time.startswith(today):
                pnl        = float(t.get("realizedPL", 0))
                entry      = float(t.get("price", 0))
                exit_price = float(t.get("averageClosePrice", 0))
                units      = float(t.get("currentUnits", 0))
                side       = "buy" if float(t.get("initialUnits", 1)) > 0 else "sell"

                if side == "buy":
                    pnl_pct = (exit_price - entry) / entry * 100
                else:
                    pnl_pct = (entry - exit_price) / entry * 100

                todays_trades.append({
                    "side":        side,
                    "entry":       entry,
                    "exit":        exit_price,
                    "pnl":         pnl,
                    "pnl_pct":     round(pnl_pct, 3),
                    "result":      "TP" if pnl > 0 else "SL",
                    "close_time":  close_time[:16],
                })

        return todays_trades

    except Exception as e:
        print(f"[SUMMARY] Error fetching trades: {e}")
        return []


def get_account_balance() -> dict:
    """Fetch current account balance and P&L."""
    try:
        resp = requests.get(
            f"{OANDA_BASE}/accounts/{OANDA_ACCOUNT}/summary",
            headers=HEADERS, timeout=10
        )
        account = resp.json().get("account", {})
        return {
            "balance":      float(account.get("balance", 0)),
            "nav":          float(account.get("NAV", 0)),
            "unrealized":   float(account.get("unrealizedPL", 0)),
        }
    except Exception:
        return {"balance": 0, "nav": 0, "unrealized": 0}


def get_claude_analysis(trades: list, stats: dict) -> str:
    """Ask Claude to analyse the day."""

    trade_lines = "\n".join([
        f"  {t['close_time']} | {t['side'].upper()} | entry={t['entry']} exit={t['exit']} | {t['result']} {t['pnl_pct']:+.3f}%"
        for t in trades
    ]) if trades else "No trades today"

    prompt = f"""You are analysing the daily performance of a gold (XAU/USD) algo trading bot.

Today's closed trades:
{trade_lines}

Summary:
- Total: {stats['total']} trades
- Wins: {stats['wins']} | Losses: {stats['losses']} | Win rate: {stats['win_rate']:.0f}%
- Total P&L: {stats['total_pnl']:+.3f}%
- Best trade: {stats['best']:+.3f}% | Worst trade: {stats['worst']:+.3f}%

The bot uses EMA 9/50 + ADX >= 25 + news sentiment on 5min gold candles.
TP is 0.4%, SL is 0.2%.

In 3 sentences maximum be brutally honest:
1. Was today profitable or not and why
2. Any pattern in the losses (time of day, size, clustering)
3. One concrete suggestion"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        return f"Analysis unavailable: {str(e)[:80]}"


def send_daily_summary():
    today    = date.today().strftime("%b %d, %Y")
    trades   = get_todays_closed_trades()
    balance  = get_account_balance()

    if not trades:
        send_message(
            f"📊 <b>Daily Summary — {today}</b>\n\n"
            f"No trades closed today.\n"
            f"Balance: ${balance['balance']:,.2f}"
        )
        return

    total     = len(trades)
    wins      = len([t for t in trades if t["result"] == "TP"])
    losses    = len([t for t in trades if t["result"] == "SL"])
    win_rate  = wins / total * 100
    total_pnl = sum(t["pnl_pct"] for t in trades)
    best      = max(t["pnl_pct"] for t in trades)
    worst     = min(t["pnl_pct"] for t in trades)

    stats = {
        "total":     total,
        "wins":      wins,
        "losses":    losses,
        "win_rate":  win_rate,
        "total_pnl": total_pnl,
        "best":      best,
        "worst":     worst,
    }

    analysis = get_claude_analysis(trades, stats)

    pnl_emoji = "📈" if total_pnl > 0 else "📉"

    msg = (
        f"📊 <b>Daily Summary — {today}</b>\n\n"
        f"Trades: {total}  |  W: {wins}  L: {losses}  |  WR: {win_rate:.0f}%\n"
        f"{pnl_emoji} Total P&L: <b>{total_pnl:+.3f}%</b>\n"
        f"Best: {best:+.3f}% | Worst: {worst:+.3f}%\n"
        f"Balance: ${balance['balance']:,.2f}\n\n"
        f"<b>Claude Analysis:</b>\n{analysis}"
    )

    send_message(msg)
    print(f"[SUMMARY] Sent daily summary for {today}")
    print(f"[SUMMARY] {total} trades | {wins}W {losses}L | {total_pnl:+.3f}% | Balance: ${balance['balance']:,.2f}")


if __name__ == "__main__":
    send_daily_summary()
