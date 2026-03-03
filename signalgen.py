"""
signalgen.py - Generates trade signals with dynamic position sizing and TP/SL.

Rules:
  - Only trade in direction of daily bias
  - Score >= 7/8 required to trade
  - Buys: TP 0.6%, SL 0.3% (or dynamic in elevated volatility)
  - Sells: TP 0.4%, SL 0.2% (or dynamic in elevated volatility)
  - Position size: 2 units if score 8/8 + normal volatility, else 1 unit
"""

from config import TRADE_CONFIG


def generate_signal(trend: dict, sentiment: dict, score: dict) -> dict:
    trade_bias = trend.get("trade_bias")   # "buy", "sell", or None
    volatility = trend.get("volatility", {})
    regime     = volatility.get("regime", "normal")
    dynamic_sl = volatility.get("dynamic_sl", TRADE_CONFIG["stop_loss_pct"])
    price      = trend.get("close", 0)

    # No trade if technicals not confirmed
    if not trade_bias:
        return _no_trade(trend.get("reject_reason", "Technical conditions not met"))

    # No trade if score too low
    if not score.get("tradeable", False):
        return _no_trade(score.get("reasoning", "Score too low"))

    action = trade_bias

    # TP/SL based on direction and volatility
    if action == "buy":
        if regime == "normal":
            sl_pct = TRADE_CONFIG["stop_loss_pct"]        # 0.3%
            tp_pct = TRADE_CONFIG["take_profit_pct"]      # 0.6%
        else:
            sl_pct = dynamic_sl                            # wider dynamic SL
            tp_pct = dynamic_sl * 2                       # maintains 2:1
    else:
        if regime == "normal":
            sl_pct = 0.002                                 # 0.2%
            tp_pct = 0.004                                 # 0.4%
        else:
            sl_pct = dynamic_sl
            tp_pct = dynamic_sl * 2

    tp = round(price * (1 + tp_pct) if action == "buy" else price * (1 - tp_pct), 4)
    sl = round(price * (1 - sl_pct) if action == "buy" else price * (1 + sl_pct), 4)

    # Dollar P&L estimates (per unit)
    tp_dollar = round(abs(tp - price), 2)
    sl_dollar = round(abs(sl - price), 2)

    # Position sizing
    units = 2 if (score["score"] == 8 and regime == "normal") else 1

    return {
        "action":     action,
        "entry_price": price,
        "take_profit": tp,
        "stop_loss":   sl,
        "tp_dollar":   tp_dollar,
        "sl_dollar":   sl_dollar,
        "units":       units,
        "sl_pct":      sl_pct,
        "tp_pct":      tp_pct,
        "score":       score["score"],
        "reason":      score["reasoning"],
        "trade":       True,
    }


def _no_trade(reason: str) -> dict:
    return {
        "action":      None,
        "entry_price": None,
        "take_profit": None,
        "stop_loss":   None,
        "tp_dollar":   None,
        "sl_dollar":   None,
        "units":       0,
        "score":       0,
        "reason":      reason,
        "trade":       False,
    }
