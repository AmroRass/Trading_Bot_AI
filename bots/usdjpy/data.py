"""
data.py - Price data from OANDA, news from Finnhub free tier
"""

import requests
import pandas as pd
from datetime import datetime, timedelta
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
from config import (
    FINNHUB_API_KEY, ASSET_CONFIG,
    OANDA_ACCESS_TOKEN, OANDA_ENVIRONMENT
)

FINNHUB_BASE = "https://finnhub.io/api/v1"

oanda_client = oandapyV20.API(
    access_token=OANDA_ACCESS_TOKEN,
    environment=OANDA_ENVIRONMENT
)

GRANULARITY_MAP = {
    "1":   "M1",
    "5":   "M5",
    "15":  "M15",
    "30":  "M30",
    "60":  "H1",
    "240": "H4",
    "D":   "D",
}


def get_candles(symbol: str, resolution: str, lookback_bars: int = 100) -> pd.DataFrame:
    """Fetch OHLCV candles from OANDA."""
    instrument  = ASSET_CONFIG["oanda_instrument"]
    granularity = GRANULARITY_MAP.get(resolution, "M15")

    params = {
        "count":       lookback_bars,
        "granularity": granularity,
        "price":       "M",
    }

    r  = instruments.InstrumentsCandles(instrument, params=params)
    rv = oanda_client.request(r)

    candles = rv.get("candles", [])
    if not candles:
        print(f"[DATA] No candles returned for {instrument}")
        return pd.DataFrame()

    rows = []
    for c in candles:
        if not c.get("complete"):
            continue
        mid = c.get("mid", {})
        rows.append({
            "timestamp": pd.to_datetime(c["time"]),
            "open":      float(mid.get("o", 0)),
            "high":      float(mid.get("h", 0)),
            "low":       float(mid.get("l", 0)),
            "close":     float(mid.get("c", 0)),
            "volume":    int(c.get("volume", 0)),
        })

    df = pd.DataFrame(rows)
    df.set_index("timestamp", inplace=True)
    return df


def get_news(keywords: list, lookback_hours: int = 2) -> list:
    """
    Fetch general market news from Finnhub free tier.
    Returns articles matching gold-relevant keywords.
    """
    url    = f"{FINNHUB_BASE}/news"
    params = {"category": "general", "token": FINNHUB_API_KEY}

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        articles = resp.json()
    except Exception as e:
        print(f"[DATA] News fetch failed: {e}")
        return []

    cutoff   = datetime.utcnow() - timedelta(hours=lookback_hours)
    filtered = []

    for article in articles:
        pub_time = datetime.utcfromtimestamp(article.get("datetime", 0))
        if pub_time < cutoff:
            continue
        text = (article.get("headline", "") + " " + article.get("summary", "")).lower()
        if any(kw.lower() in text for kw in keywords):
            filtered.append({
                "headline": article.get("headline", ""),
                "summary":  article.get("summary", ""),
                "url":      article.get("url", ""),
                "datetime": pub_time,
                "source":   article.get("source", ""),
            })

    print(f"[DATA] Found {len(filtered)} relevant articles")
    return filtered


def get_forex_sentiment(symbol: str = "XAUUSD") -> dict:
    """
    Fetch Finnhub's built-in sentiment score.
    Falls back gracefully if the symbol isn't supported on free tier.
    """
    url = f"{FINNHUB_BASE}/news-sentiment"
    params = {
        "symbol": symbol,
        "token":  FINNHUB_API_KEY
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        sentiment_score = data.get("sentiment", {}).get("bullishPercent", 0.5) - 0.5
        buzz_score      = data.get("buzz", {}).get("buzz", 0)

        return {
            "sentiment_score": round(sentiment_score, 3),
            "buzz":            buzz_score,
            "articles_in_week": data.get("buzz", {}).get("weeklyAverage", 0),
        }

    except Exception as e:
        print(f"[DATA] Sentiment fetch failed ({e}), defaulting to neutral")
        return {"sentiment_score": 0.0, "buzz": 0.0, "articles_in_week": 0}
