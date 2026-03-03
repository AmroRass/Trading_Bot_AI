"""
main.py - Gold trading bot v2.

Top-down strategy:
  Daily EMA50 bias -> 1H confirmation -> 5min entry
  AI scoring (8 point rubric) required
  Dynamic TP/SL based on volatility regime
  Cycle-based cooldown after trade close
  Re-entry checks after TP
"""

import time
import traceback
from datetime import datetime, timezone
import requests
import os

from config import ASSET_CONFIG, TRADE_CONFIG, validate_keys
from data import get_candles, get_news
from technicals import get_trend_signal
from ai_layer import get_news_sentiment, score_trade, get_economic_calendar
from signalgen import generate_signal
from execution import submit_order
from logger import init_log, log_decision, print_decision
from telegram_alerts import (
    alert_bot_started, alert_trade_opened,
    alert_trade_closed, alert_error, alert_no_credits, alert_standing_down
)

from dotenv import load_dotenv
load_dotenv()

OANDA_TOKEN   = os.getenv("OANDA_ACCESS_TOKEN")
OANDA_ACCOUNT = os.getenv("OANDA_ACCOUNT_ID")
OANDA_BASE    = "https://api-fxpractice.oanda.com/v3"
HEADERS       = {"Authorization": f"Bearer {OANDA_TOKEN}"}

# State
_tracked_trade = {
    "trade_id":    None,
    "side":        None,
    "entry_price": None,
    "tp_price":    None,
    "sl_price":    None,
    "units":       1,
    "reasoning":   "",
}
_cooldown_cycles    = 0   # cycles to wait after trade close
_sl_hits_today      = 0
_trades_today       = 0
_last_tp_price      = None   # for re-entry check
_last_tp_side       = None


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


def get_closed_trade(trade_id):
    try:
        resp = requests.get(
            f"{OANDA_BASE}/accounts/{OANDA_ACCOUNT}/trades/{trade_id}",
            headers=HEADERS, timeout=10
        )
        return resp.json().get("trade", {})
    except Exception:
        return {}


def get_account_balance():
    try:
        resp = requests.get(
            f"{OANDA_BASE}/accounts/{OANDA_ACCOUNT}/summary",
            headers=HEADERS, timeout=10
        )
        return float(resp.json().get("account", {}).get("balance", 0))
    except Exception:
        return 0.0


def monitor_position():
    global _tracked_trade, _cooldown_cycles, _sl_hits_today

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
            balance    = get_account_balance()

            if result == "SL":
                _sl_hits_today += 1

            alert_trade_closed(
                side=_tracked_trade["side"],
                entry=entry,
                exit_price=exit_price,
                result=result,
                pnl_dollar=round(pnl, 2),
                balance=balance,
            )
            print(f"[MONITOR] Trade closed {result} @ {exit_price} | PnL: ${pnl:.2f} | Balance: ${balance:.2f}")
        except Exception as e:
            print(f"[MONITOR] Error processing close: {e}")

    _tracked_trade = {k: None for k in _tracked_trade}
    _tracked_trade["units"] = 1
    _cooldown_cycles = 2
    print(f"[BOT] Cooldown started — waiting 2 cycles")
    return True


def check_reentry(current_price: float, trend: dict) -> tuple:
    """
    After TP, check if re-entry conditions are met.
    Returns (allowed: bool, reason: str)
    """
    global _last_tp_price, _last_tp_side

    if _last_tp_price is None:
        return True, "No previous TP"

    # Check trend still intact
    daily_ok = trend.get("daily_bias", {}).get("direction") != "unknown"
    htf_ok   = trend.get("htf_bias", {}).get("direction") != "unknown"

    if not daily_ok or not htf_ok:
        return False, "Trend unknown after TP"

    # Check price hasnt retraced 50% of previous move
    if _last_tp_side == "buy" and _last_tp_price:
        retracement = (_last_tp_price - current_price) / _last_tp_price
        if retracement > 0.003:  # more than 0.3% retraced
            _last_tp_price = None
            _last_tp_side  = None
            return True, "Retracement cleared — fresh entry allowed"

    return True, "Re-entry conditions met"


def run_cycle():
    global _tracked_trade, _cooldown_cycles, _trades_today, _last_tp_price, _last_tp_side

    symbol    = ASSET_CONFIG["oanda_instrument"]
    keywords  = ASSET_CONFIG["news_keywords"]
    timeframe = TRADE_CONFIG["timeframe"]

    monitor_position()

    if _cooldown_cycles > 0:
        _cooldown_cycles -= 1
        print(f"[BOT] Cooldown — {_cooldown_cycles} cycles remaining, skipping")
        return

    open_trade   = get_open_trade()
    has_position = open_trade is not None

    if has_position and not _tracked_trade["trade_id"]:
        _tracked_trade["trade_id"]    = open_trade.get("id")
        _tracked_trade["side"]        = "buy" if float(open_trade.get("currentUnits", 0)) > 0 else "sell"
        _tracked_trade["entry_price"] = float(open_trade.get("price", 0))
        print(f"[MONITOR] Synced: {_tracked_trade['side']} @ {_tracked_trade['entry_price']}")

    df_5m    = get_candles(symbol, timeframe, lookback_bars=500)
    df_1h    = get_candles(symbol, "60",      lookback_bars=200)
    df_daily = get_candles(symbol, "D",       lookback_bars=100)

    if df_5m.empty:
        print("[WARN] No 5min data, skipping")
        return

    trend = get_trend_signal(df_5m, df_1h if not df_1h.empty else None, df_daily if not df_daily.empty else None)

    # Fetch news and score only if trend confirmed
    if trend["confirmed"]:
        articles  = get_news(keywords, lookback_hours=TRADE_CONFIG["news_lookback_hours"])
        sentiment = get_news_sentiment(articles)
        ai_score  = score_trade(trend, sentiment, _sl_hits_today)
    else:
        sentiment = {"direction": "neutral", "confidence": 0.0, "reasoning": "Trend not confirmed"}
        ai_score  = {"score": 0, "tradeable": False, "reasoning": trend.get("reject_reason", ""), "breakdown": {}, "event": {"blocked": False}}

    signal = generate_signal(trend, sentiment, ai_score)

    if not has_position:
        # Re-entry check
        reentry_ok, reentry_reason = check_reentry(trend["close"], trend)
        if not reentry_ok:
            print(f"[BOT] Re-entry blocked: {reentry_reason}")
            signal = {"trade": False, "reason": reentry_reason, "action": None}

        if signal.get("trade"):
            execution = submit_order(signal)

            if execution.get("status") == "submitted":
                side = signal["action"]
                try:
                    entry_price = float(execution.get("fill_price", trend["close"]))
                except (ValueError, TypeError):
                    entry_price = float(trend["close"])

                tp = signal["take_profit"]
                sl = signal["stop_loss"]

                _tracked_trade.update({
                    "trade_id":    execution.get("order_id"),
                    "side":        side,
                    "entry_price": entry_price,
                    "tp_price":    tp,
                    "sl_price":    sl,
                    "units":       signal["units"],
                    "reasoning":   sentiment.get("reasoning", ""),
                })
                _trades_today += 1

                # Track for re-entry check
                if side == "buy":
                    _last_tp_price = tp
                    _last_tp_side  = side

                balance = get_account_balance()
                alert_trade_opened(
                    side=side,
                    price=entry_price,
                    tp=tp,
                    sl=sl,
                    tp_dollar=signal["tp_dollar"],
                    sl_dollar=signal["sl_dollar"],
                    units=signal["units"],
                    score=signal["score"],
                    reasoning=sentiment.get("reasoning", ""),
                )
        else:
            execution = {"status": "skipped", "reason": signal.get("reason", "")}
            print(f"[BOT] No trade: {signal.get('reason', '')}")
    else:
        execution = {"status": "skipped", "reason": "Position already open"}
        print(f"[BOT] Position open (trade {_tracked_trade['trade_id']}) — skipping")

    log_decision(trend, sentiment, signal, execution)
    print_decision(trend, sentiment, signal, execution)


def main():
    global _sl_hits_today, _trades_today

    validate_keys()
    balance = get_account_balance()

    print("\n Gold AI Trading Bot v2")
    print(f"   Top-Down | Daily+1H+5min | ADX>={TRADE_CONFIG[chr(39)+'adx_threshold'+chr(39)]}")
    print(f"   Session: 07:00-12:00 | 13:30-17:00 UTC")
    print(f"   Balance: ${balance:,.2f}")
    print("="*60)

    init_log()
    get_economic_calendar()
    alert_bot_started(balance)

    last_day = None

    while True:
        try:
            today = datetime.now(timezone.utc).date()
            if today != last_day:
                _sl_hits_today = 0
                _trades_today  = 0
                last_day       = today
                if last_day is not None:
                    get_economic_calendar()

            run_cycle()

        except KeyboardInterrupt:
            print("\n[BOT] Stopped.")
            break
        except Exception as e:
            err = str(e)
            print(f"[ERROR] {err}")
            traceback.print_exc()
            if "credit balance is too low" in err:
                alert_no_credits()
            else:
                alert_error(err)

        print(f"\n[BOT] Sleeping {TRADE_CONFIG[chr(39)+'poll_interval_seconds'+chr(39)]}s...")
        time.sleep(TRADE_CONFIG[chr(39)+'poll_interval_seconds'+chr(39)])


if __name__ == "__main__":
    main()
