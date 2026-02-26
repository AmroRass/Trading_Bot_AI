"""
logger.py - Logs every decision cycle to CSV for analysis
"""
import csv
import os
from datetime import datetime

LOG_FILE = "trade_log.csv"
HEADERS = [
    "timestamp",
    "price",
    "tech_direction",
    "tech_strength",
    "tech_confirmed",
    "slope",
    "crossover_age",
    "reject_reason",
    "sentiment_direction",
    "sentiment_confidence",
    "sentiment_reasoning",
    "signals_agree",
    "action",
    "signal_type",
    "take_profit",
    "stop_loss",
    "reason",
    "execution_status",
]


def init_log():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=HEADERS)
            writer.writeheader()
        print(f"[LOG] Created {LOG_FILE}")


def log_decision(trend: dict, sentiment: dict, signal: dict, execution: dict):
    signals_agree = trend.get("direction") == sentiment.get("direction")
    row = {
        "timestamp":            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "price":                trend.get("close", ""),
        "tech_direction":       trend.get("direction", ""),
        "tech_strength":        trend.get("strength", ""),
        "tech_confirmed":       trend.get("confirmed", ""),
        "slope":                trend.get("slope", ""),
        "crossover_age":        trend.get("crossover_age", ""),
        "reject_reason":        trend.get("reject_reason", ""),
        "sentiment_direction":  sentiment.get("direction", ""),
        "sentiment_confidence": sentiment.get("confidence", ""),
        "sentiment_reasoning":  sentiment.get("reasoning", ""),
        "signals_agree":        signals_agree,
        "action":               signal.get("action", ""),
        "signal_type":          signal.get("signal_type", ""),
        "take_profit":          signal.get("take_profit", ""),
        "stop_loss":            signal.get("stop_loss", ""),
        "reason":               signal.get("reason", ""),
        "execution_status":     execution.get("status", ""),
    }
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS)
        writer.writerow(row)


def print_decision(trend: dict, sentiment: dict, signal: dict, execution: dict):
    signals_agree = trend.get("direction") == sentiment.get("direction")
    agree_str = "✅ AGREE" if signals_agree else "⚠️  CONFLICT"

    print("\n" + "="*60)
    print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] Gold @ {trend.get('close', '?')}")
    print(f"  TECH:      {trend.get('direction','?').upper()} | ADX={trend.get('strength','?')} | confirmed={trend.get('confirmed','?')}")
    print(f"  SLOPE:     {trend.get('slope','?')} | Crossover: {trend.get('crossover_age','?')} candles ago")
    if not trend.get("confirmed"):
        print(f"  REJECT:    {trend.get('reject_reason','?')}")
    print(f"  SENTIMENT: {sentiment.get('direction','?').upper()} | confidence={sentiment.get('confidence','?')} | {sentiment.get('reasoning','')}")
    print(f"  SIGNALS:   {agree_str}")
    action_str = (signal.get('action') or 'NO TRADE').upper()
    print(f"  ACTION:    {action_str} ({signal.get('signal_type','')}) → {signal.get('reason','')}")
    if signal.get("trade"):
        print(f"  TP={signal.get('take_profit')} | SL={signal.get('stop_loss')}")
    print(f"  EXECUTION: {execution.get('status','')}")
    print("="*60)
