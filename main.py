"""
main.py - The main bot loop with Telegram alerts and position monitoring.

Key fixes:
- Position tracking checks OANDA directly on every cycle
- 60 second cooldown after trade close prevents immediate re-entry stacking
- Startup message reads config accurately
"""

import time
import traceback
from datetime import datetime, timezone
import requests
import os

from config import ASSET_CONFIG, TRADE_CONFIG, SESSION_CONFIG, validate_keys
from data import get_candles, get_news
from technicals import get_trend_signal
from ai_layer import get_combined_sentiment
from signalgen import generate_signal
from execution import submit_order
from logger import init_log, log_decision, print_decision
from telegram_alerts import (
    alert_bot_started, alert_trade_opened,
    alert_trade_closed, alert_error, alert_no_credits
)

from dotenv import load_dotenv
load_dotenv()

OANDA_TOKEN   = os.getenv("OANDA_ACCESS_TOKEN")
OANDA_ACCOUNT = os.getenv("OANDA_ACCOUNT_ID")
OANDA_BASE    = "https://api-fxpractice.oanda.com/v3"
HEADERS       = {"Authorization": f"Bearer {OANDA_TOKEN}"}

TRADE_COOLDOWN_SECONDS = 60

_tracked_trade = {
    "trade_id":    None,
    "side":        None,
    "entry_price": None,
    "tp_price":    None,
    "sl_price":    None,
    "reasoning":   "",
}
_last_trade_close_time = None


def get_open_trade():
    try:
        resp = requests.get(
            f"{OANDA_BASE}/accounts/{OANDA_ACCOUNT}/openTrades",
            headers=HEADERS, timeout=10
        )
        trades = resp.json().get("trades", [])
        for t in trades:
            if t.get("instrument") == ASSET_CONFIG["oanda_instrument"]:
                return t
        return None
    except Exception as e:
        print(f"[MONITOR] Error fetching open trades: {e}")
        return None


def get_closed_trade(trade_id: str) -> dict:
    try:
        resp = requests.get(
            f"{OANDA_BASE}/accounts/{OANDA_ACCOUNT}/trades/{trade_id}",
            headers=HEADERS, timeout=10
        )
        return resp.json().get("trade", {})
    except Exception:
        return {}


def monitor_position():
    global _tracked_trade, _last_trade_close_time

    if not _tracked_trade["trade_id"]:
        return False

    open_trade = get_open_trade()

    if open_trade is not None:
        return False

    trade = get_closed_trade(_tracked_trade["trade_id"])
    if trade:
        try:
            exit_price = float(trade.get("averageClosePrice", _tracked_trade["entry_price"]))
            pnl        = float(trade.get("realizedPL", 0))
            result     = "TP" if pnl > 0 else "SL"
            entry      = float(_tracked_trade["entry_price"])
            pnl_pct    = (exit_price - entry) / entry * 100
            if _tracked_trade["side"] == "sell":
                pnl_pct = -pnl_pct

            alert_trade_closed(
                side=_tracked_trade["side"],
                entry=entry,
                exit_price=exit_price,
                result=result,
                pnl_pct=pnl_pct
            )
            print(f"[MONITOR] Trade closed — {result} @ {exit_price} | P&L: {pnl_pct:.3f}%")
        except Exception as e:
            print(f"[MONITOR] Error processing close: {e}")

    _tracked_trade = {k: None for k in _tracked_trade}
    _last_trade_close_time = datetime.now(timezone.utc)
    print(f"[BOT] Cooldown started — waiting {TRADE_COOLDOWN_SECONDS}s before next trade")
    return True


def run_cycle():
    global _tracked_trade, _last_trade_close_time

    symbol    = ASSET_CONFIG["oanda_instrument"]
    keywords  = ASSET_CONFIG["news_keywords"]
    timeframe = TRADE_CONFIG["timeframe"]

    monitor_position()

    if _last_trade_close_time:
        elapsed = (datetime.now(timezone.utc) - _last_trade_close_time).total_seconds()
        if elapsed < TRADE_COOLDOWN_SECONDS:
            remaining = int(TRADE_COOLDOWN_SECONDS - elapsed)
            print(f"[BOT] Cooldown active — {remaining}s remaining, skipping cycle")
            return

    open_trade   = get_open_trade()
    has_position = open_trade is not None

    if has_position and not _tracked_trade["trade_id"]:
        _tracked_trade["trade_id"]    = open_trade.get("id")
        _tracked_trade["side"]        = "buy" if float(open_trade.get("currentUnits", 0)) > 0 else "sell"
        _tracked_trade["entry_price"] = float(open_trade.get("price", 0))
        print(f"[MONITOR] Synced open position from OANDA: {_tracked_trade['side']} @ {_tracked_trade['entry_price']}")

    df_15m = get_candles(symbol, timeframe, lookback_bars=200)
    if df_15m.empty:
        print("[WARN] No price data, skipping cycle")
        return

    df_1h = get_candles(symbol, "60", lookback_bars=100)
    if df_1h.empty:
        df_1h = None

    trend = get_trend_signal(df_15m, df_1h)

    if trend["confirmed"] and trend["in_session"]:
        articles  = get_news(keywords, lookback_hours=TRADE_CONFIG["news_lookback_hours"])
        sentiment = get_combined_sentiment(articles)
    else:
        sentiment = {
            "direction": "neutral", "confidence": 0.0,
            "reasoning": "Outside session or ADX not confirmed — skipping news fetch",
            "source": "skipped"
        }

    signal = generate_signal(trend, sentiment)

    if not has_position:
        execution = submit_order(signal)

        if execution.get("status") == "submitted":
            side = signal.get("action")
            fill_price = execution.get("fill_price", "?")
            try:
                entry_price = float(fill_price)
            except (ValueError, TypeError):
                entry_price = float(trend["close"])

            tp = signal.get("take_profit")
            sl = signal.get("stop_loss")

            _tracked_trade["trade_id"]    = execution.get("order_id")
            _tracked_trade["side"]        = side
            _tracked_trade["entry_price"] = entry_price
            _tracked_trade["tp_price"]    = tp
            _tracked_trade["sl_price"]    = sl
            _tracked_trade["reasoning"]   = sentiment.get("reasoning", "")

            alert_trade_opened(side, entry_price, tp, sl, sentiment.get("reasoning", ""))
    else:
        execution = {"status": "skipped", "reason": "Position already open on OANDA"}
        print(f"[BOT] Position already open (trade {_tracked_trade['trade_id']}) — skipping")

    log_decision(trend, sentiment, signal, execution)
    print_decision(trend, sentiment, signal, execution)


def main():
    validate_keys()

    htf_status     = "ON" if TRADE_CONFIG.get("htf_confirmation") else "OFF"
    session_status = f"{SESSION_CONFIG['start_hour_utc']:02d}:00-{SESSION_CONFIG['end_hour_utc']:02d}:00 UTC" if SESSION_CONFIG["enabled"] else "24/7"

    print("\n🏅 Gold AI Trading Bot")
    print(f"   EMA: {TRADE_CONFIG['ema_fast']}/{TRADE_CONFIG['ema_slow']} | ADX≥{TRADE_CONFIG['adx_threshold']} | HTF {htf_status}")
    print(f"   TP: {TRADE_CONFIG['take_profit_pct']*100}% | SL: {TRADE_CONFIG['stop_loss_pct']*100}%")
    print(f"   Timeframe: {TRADE_CONFIG['timeframe']}min | Session: {session_status}")
    print("="*60)

    init_log()
    alert_bot_started()

    while True:
        try:
            run_cycle()
        except KeyboardInterrupt:
            print("\n[BOT] Stopped by user.")
            break
        except Exception as e:
            err = str(e)
            print(f"[ERROR] {err}")
            traceback.print_exc()
            if "credit balance is too low" in err:
                alert_no_credits()
            else:
                alert_error(err)

        print(f"\n[BOT] Sleeping {TRADE_CONFIG['poll_interval_seconds']}s...")
        time.sleep(TRADE_CONFIG["poll_interval_seconds"])


if __name__ == "__main__":
    main()
