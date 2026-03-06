"""
hourly_update.py - Hourly status for both bots.
Cron: 0 * * * * cd /home/ec2-user && python3 hourly_update.py
"""

import requests
import os
from datetime import datetime, timezone, date
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID")
OANDA_TOKEN    = os.getenv("OANDA_ACCESS_TOKEN")
OANDA_BASE     = "https://api-fxpractice.oanda.com/v3"
HEADERS        = {"Authorization": f"Bearer {OANDA_TOKEN}"}

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


def get_summary(account_id):
    try:
        resp = requests.get(f"{OANDA_BASE}/accounts/{account_id}/summary", headers=HEADERS, timeout=10)
        acc  = resp.json().get("account", {})
        return {"balance": float(acc.get("balance", 0)), "nav": float(acc.get("NAV", 0))}
    except Exception:
        return {"balance": 0, "nav": 0}


def get_open_trade(account_id):
    try:
        resp   = requests.get(f"{OANDA_BASE}/accounts/{account_id}/openTrades", headers=HEADERS, timeout=10)
        trades = resp.json().get("trades", [])
        for t in trades:
            if t.get("instrument") == "XAU_USD":
                units = float(t.get("currentUnits", 0))
                return {
                    "side":       "BUY" if units > 0 else "SELL",
                    "entry":      float(t.get("price", 0)),
                    "unrealized": float(t.get("unrealizedPL", 0)),
                }
        return {}
    except Exception:
        return {}


def get_today(account_id):
    try:
        resp   = requests.get(f"{OANDA_BASE}/accounts/{account_id}/trades?state=CLOSED&count=100", headers=HEADERS, timeout=10)
        trades = resp.json().get("trades", [])
        today  = date.today().isoformat()
        wins = losses = 0
        pnl  = 0.0
        for t in trades:
            if not t.get("closeTime", "").startswith(today):
                continue
            p = float(t.get("realizedPL", 0))
            pnl += p
            if p > 0: wins += 1
            elif p < 0: losses += 1
        return {"wins": wins, "losses": losses, "pnl": pnl}
    except Exception:
        return {"wins": 0, "losses": 0, "pnl": 0}


def format_section(name, account_id):
    emoji   = "Blue" if name == "Conservative" else "Red"
    summary = get_summary(account_id)
    trade   = get_open_trade(account_id)
    today   = get_today(account_id)
    bal     = summary["balance"]
    total   = today["wins"] + today["losses"]
    pnl_str = f"+${today['pnl']:.2f}" if today["pnl"] >= 0 else f"-${abs(today['pnl']):.2f}"
    pnl_e   = "Up" if today["pnl"] >= 0 else "Down"

    lines = [f"<b>{emoji} {name}</b>  ${bal:,.2f}"]
    if total > 0:
        wr = today["wins"] / total * 100
        lines.append(f"  Today: {today['wins']}W {today['losses']}L ({wr:.0f}%) {pnl_e} {pnl_str}")
    else:
        lines.append(f"  Today: No closed trades")

    if trade:
        unr = trade["unrealized"]
        unr_str = f"+${unr:.2f}" if unr >= 0 else f"-${abs(unr):.2f}"
        unr_e   = "Up" if unr >= 0 else "Down"
        lines.append(f"  Open: {trade['side']} @ ${trade['entry']:,.3f} {unr_e} {unr_str}")
    else:
        lines.append(f"  Open: No position")

    return "\n".join(lines)


def send_hourly():
    now      = datetime.now(timezone.utc).strftime("%H:%M UTC")
    sections = [format_section(n, a) for n, a in BOTS.items()]
    msg      = f"Hourly Update - {now}\n{'=' * 20}\n\n" + "\n\n".join(sections)
    send_message(msg)
    print(f"[HOURLY] Sent at {now}")


if __name__ == "__main__":
    send_hourly()
