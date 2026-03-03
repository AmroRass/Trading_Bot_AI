"""
daily_summary.py - Daily summary for both bots via Telegram.
Run via cron at 17:00 UTC: 0 17 * * 1-5 cd /home/ec2-user && python3 daily_summary.py
"""

import os
import requests
import anthropic
from datetime import datetime, timezone, date
from dotenv import load_dotenv

load_dotenv()

OANDA_TOKEN   = os.getenv("OANDA_ACCESS_TOKEN")
OANDA_BASE    = "https://api-fxpractice.oanda.com/v3"
HEADERS       = {"Authorization": f"Bearer {OANDA_TOKEN}"}
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID")

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

BOTS = {
    "Conservative": "101-004-37417354-005",
    "Risky":        "101-004-37417354-006",
}


def send_message(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT, "text": text[:4093], "parse_mode": "HTML"}, timeout=5)
    except Exception as e:
        print(f"[TELEGRAM] {e}")


def get_account(account_id):
    try:
        resp = requests.get(f"{OANDA_BASE}/accounts/{account_id}/summary", headers=HEADERS, timeout=10)
        acc = resp.json().get("account", {})
        return {"balance": float(acc.get("balance", 0)), "nav": float(acc.get("NAV", 0))}
    except Exception:
        return {"balance": 0, "nav": 0}


def get_todays_trades(account_id):
    try:
        resp = requests.get(
            f"{OANDA_BASE}/accounts/{account_id}/trades?state=CLOSED&count=100",
            headers=HEADERS, timeout=10
        )
        trades = resp.json().get("trades", [])
        today  = date.today().isoformat()
        result = []
        for t in trades:
            if not t.get("closeTime", "").startswith(today):
                continue
            pnl   = float(t.get("realizedPL", 0))
            entry = float(t.get("price", 0))
            exit_ = float(t.get("averageClosePrice", 0))
            side  = "buy" if float(t.get("initialUnits", 1)) > 0 else "sell"
            result.append({
                "side":   side,
                "entry":  entry,
                "exit":   exit_,
                "pnl":    pnl,
                "result": "TP" if pnl > 0 else "SL",
                "time":   t.get("closeTime", "")[:16],
            })
        return result
    except Exception as e:
        print(f"[SUMMARY] Error: {e}")
        return []


def get_claude_analysis(bot_name, trades, stats):
    if not trades:
        return "No trades today."
    trade_lines = chr(10).join([
        f"  {t['time']} | {t['side'].upper()} | {t['result']} | ${t['pnl']:+.2f}"
        for t in trades
    ])
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": f"""Gold bot daily analysis for {bot_name}:

{trade_lines}

Stats: {stats['total']} trades | {stats['wins']}W {stats['losses']}L | ${stats['total_pnl']:+.2f} total

In 2 sentences max: what pattern do you see and one concrete suggestion."""}]
        )
        return resp.content[0].text.strip()
    except Exception as e:
        return f"Analysis unavailable: {str(e)[:60]}"


def format_bot_section(name, account_id):
    acc    = get_account(account_id)
    trades = get_todays_trades(account_id)
    bal    = acc["balance"]

    if not trades:
        return (
            f"<b>{'Blue' if name == 'Conservative' else 'Red'} {name}</b>\n"
            f"  Balance: ${bal:,.2f}\n"
            f"  No trades today"
        )

    wins      = len([t for t in trades if t["result"] == "TP"])
    losses    = len([t for t in trades if t["result"] == "SL"])
    total     = len(trades)
    total_pnl = sum(t["pnl"] for t in trades)
    best      = max(t["pnl"] for t in trades)
    worst     = min(t["pnl"] for t in trades)
    wr        = wins / total * 100
    pnl_emoji = "Up" if total_pnl >= 0 else "Down"
    pnl_str   = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"

    stats    = {"total": total, "wins": wins, "losses": losses, "total_pnl": total_pnl}
    analysis = get_claude_analysis(name, trades, stats)

    return (
        f"<b>{'Blue' if name == 'Conservative' else 'Red'} {name}</b>\n"
        f"  Trades: {total} | {wins}W {losses}L | {wr:.0f}% WR\n"
        f"  PnL: <b>{pnl_emoji} {pnl_str}</b>\n"
        f"  Best: +${best:.2f} | Worst: -${abs(worst):.2f}\n"
        f"  Balance: <b>${bal:,.2f}</b>\n"
        f"  Analysis: {analysis}"
    )


def send_daily_summary():
    today    = date.today().strftime("%b %d, %Y")
    sections = []
    for name, account_id in BOTS.items():
        sections.append(format_bot_section(name, account_id))

    msg = (
        f"Daily Summary - {today}\n"
        f"{'=' * 20}\n\n"
        + "\n\n".join(sections)
    )
    send_message(msg)
    print(f"[SUMMARY] Sent for {today}")


if __name__ == "__main__":
    send_daily_summary()
