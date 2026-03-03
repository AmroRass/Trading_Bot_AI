"""
Asset configuration - swap this out to trade a different instrument.
Everything else in the system stays the same.
"""

ASSET_CONFIG = {
    "name": "Gold",
    "finnhub_symbol": "OANDA:XAU_USD",
    "oanda_instrument": "XAU_USD",
    "news_keywords": ["gold", "XAU", "fed", "inflation", "dollar", "interest rate", "central bank", "safe haven", "geopolitical", "treasury", "tariff", "silver", "commodity"],
    "description": "Gold Spot vs USD"
}

# Trading parameters — validated via rigorous walk-forward backtest (15 months)
TRADE_CONFIG = {
    "timeframe": "5",           # minutes
    "ema_fast": 9,
    "ema_slow": 50,              # changed from 21 — confirmed better via backtest
    "adx_period": 14,
    "adx_threshold": 25,         # changed from 20 — filters weak trends
    "take_profit_pct": 0.004,    # 0.4%
    "stop_loss_pct": 0.002,      # changed from 0.003 — better risk/reward ratio
    "htf_confirmation": False,    # NEW — only trade when 1H EMA50 agrees with 15min signal
    "conflict_mode": "risky",
    "news_lookback_hours": 24,
    "poll_interval_seconds": 300,
    "oanda_units": 1,
}

# Session filter — only trade London + NY overlap (7am-5pm UTC)
SESSION_CONFIG = {
    "enabled": True,
    "start_hour_utc": 7,
    "end_hour_utc": 20,
}

# Sentiment thresholds
SENTIMENT_CONFIG = {
    "bullish_threshold": 0.2,
    "bearish_threshold": -0.2,
    "high_impact_score": 0.6,
}

# API Keys - loaded from .env file (never hardcode keys here)
import os
from dotenv import load_dotenv
load_dotenv()

FINNHUB_API_KEY    = os.getenv("FINNHUB_API_KEY")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")
OANDA_ACCESS_TOKEN = os.getenv("OANDA_ACCESS_TOKEN")
OANDA_ACCOUNT_ID   = os.getenv("OANDA_ACCOUNT_ID")
OANDA_ENVIRONMENT  = os.getenv("OANDA_ENVIRONMENT", "practice")

def validate_keys():
    missing = [k for k, v in {
        "FINNHUB_API_KEY":    FINNHUB_API_KEY,
        "ANTHROPIC_API_KEY":  ANTHROPIC_API_KEY,
        "OANDA_ACCESS_TOKEN": OANDA_ACCESS_TOKEN,
        "OANDA_ACCOUNT_ID":   OANDA_ACCOUNT_ID,
    }.items() if not v]
    if missing:
        raise EnvironmentError(f"Missing required env vars: {', '.join(missing)}\nCopy .env.example to .env and fill in your keys.")
