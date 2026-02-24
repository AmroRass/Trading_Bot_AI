"""
main.py - The main bot loop with Telegram alerts and position monitoring.
"""

import time
import traceback
from datetime import datetime, timezone

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

import requests
import os

OANDA_TOKEN   = os.getenv("OANDA_ACCESS_TOKEN")
OANDA_ACCOUNT = os.getenv("OANDA_ACCOUNT_ID")
OANDA_BASE    = "https://api-fxpractice.oanda.com/v3"
HEADERS       = {"Authorization": f"Bearer {OANDA_TOKEN}"}

# Track open position for monitoring
_open_position = {
    "active":       False,
    "side":         None,
    "entry_price":  None,
    "tp_price":     None,
    "sl_price":     None,
    "trade_id":     None,
    "reasoning":    "",
}


def get_open_trades():
    """Fetch currently open trades from OANDA."""
    try:
        resp = requests.get(
            f"{OANDA_BASE}/accounts/{OANDA_ACCOUNT}/openTrades",
            headers=HEADERS, timeout=10
        )
        return resp.json().get("trades", [])
    except Exception:
        return []


def monitor_position():
    """
    Check if our tracked position has closed (TP or SL hit).
    Sends Telegram alert if it has.
    """
    global _open_position

    if not _open_position["active"]:
        return

    trades = get_open_trades()
    trade_ids = [t["id"] for t in trades]

    if _open_position["trade_id"] and _open_position["trade_id"] not in trade_ids:
        # Position closed — figure out if TP or SL
        try:
            resp = requests.get(
                f"{OANDA_BASE}/accounts/{OANDA_ACCOUNT}/trades/{_open_position['trade_id']}",
                headers=HEADERS, timeout=10
            )
            trade = resp.json().get("trade", {})
            exit_price = float(trade.get("averageClosePrice", _open_position["entry_price"]))
            pnl        = float(trade.get("realizedPL", 0))
            result     = "TP" if pnl > 0 else "SL"
            pnl_pct    = (exit_price - _open_position["entry_price"]) / _open_position["entry_price"] * 100
            if _open_position["side"] == "sell":
                pnl_pct = -pnl_pct

            alert_trade_closed(
                side=_open_position["side"],
                entry=_open_position["entry_price"],
                exit_price=exit_price,
                result=result,
                pnl_pct=pnl_pct
            )
        except Exception:
            pass

        # Reset tracking
        _open_position = {k: None for k in _open_position}
        _open_position["active"] = False


def run_cycle():
    global _open_position

    symbol    = ASSET_CONFIG["oanda_instrument"]
    keywords  = ASSET_CONFIG["news_keywords"]
    timeframe = TRADE_CONFIG["timeframe"]

    # Monitor existing position first
    monitor_position()

    # 1. Fetch 15min candles
    df_15m = get_candles(symbol, timeframe, lookback_bars=100)
    if df_15m.empty:
        print("[WARN] No 15min price data, skipping cycle")
        return

    # 2. Fetch 1H candles for HTF confirmation
    df_1h = get_candles(symbol, "60", lookback_bars=100)
    if df_1h.empty:
        df_1h = None

    # 3. Technical analysis
    trend = get_trend_signal(df_15m, df_1h)

    # 4. News + sentiment (only if in session and ADX confirmed)
    if trend["in_session"] and trend["confirmed"]:
        articles  = get_news(keywords, lookback_hours=TRADE_CONFIG["news_lookback_hours"])
        sentiment = get_combined_sentiment(articles)
    else:
        sentiment = {
            "direction": "neutral", "confidence": 0.0,
            "reasoning": "Outside session or ADX not confirmed — skipping news fetch",
            "source": "skipped"
        }

    # 5. Generate signal
    signal = generate_signal(trend, sentiment)

    # 6. Execute
    if not _open_position["active"]:
        execution = submit_order(signal)

        # Track if order was placed
        if execution.get("status") == "submitted":
            price = trend["close"]
            side  = signal.get("action")
            tp    = signal.get("take_profit")
            sl    = signal.get("stop_loss")

            _open_position["active"]      = True
            _open_position["side"]        = side
            _open_position["entry_price"] = price
            _open_position["tp_price"]    = tp
            _open_position["sl_price"]    = sl
            _open_position["trade_id"]    = execution.get("trade_id")
            _open_position["reasoning"]   = sentiment.get("reasoning", "")

            alert_trade_opened(side, price, tp, sl, sentiment.get("reasoning", ""))
    else:
        execution = {"status": "skipped", "reason": "Position already open"}
        print("[BOT] Position already open — skipping new signal")

    # 7. Log
    log_decision(trend, sentiment, signal, execution)
    print_decision(trend, sentiment, signal, execution)


def main():
    validate_keys()

    print("\n🏅 Gold AI Trading Bot")
    print(f"   EMA: {TRADE_CONFIG['ema_fast']}/{TRADE_CONFIG['ema_slow']} | ADX≥{TRADE_CONFIG['adx_threshold']}")
    print(f"   TP: {TRADE_CONFIG['take_profit_pct']*100}% | SL: {TRADE_CONFIG['stop_loss_pct']*100}%")
    print(f"   Session: 07:00-17:00 UTC | Interval: {TRADE_CONFIG['poll_interval_seconds']}s")
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
