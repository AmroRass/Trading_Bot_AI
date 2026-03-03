"""
ai_layer.py - AI scoring for gold trading signals.

Two functions:
  1. get_economic_calendar() -- Claude scans web for today's high impact events
  2. score_trade_setup()     -- Claude scores setup 0-8 using strict rubric

Scoring rubric (1 point each, direction-agnostic):
  1. Daily trend agrees with proposed direction?
  2. 1H trend agrees with proposed direction?
  3. 5min signal agrees with proposed direction?
  4. ADX >= 25?
  5. News supports or neutral?
  6. No high impact event within 60 minutes?
  7. Volatility normal (not elevated or extreme)?
  8. Fewer than 3 SL hits today?
"""

import anthropic
import hashlib
import json
from datetime import datetime, timezone, date
from config import ANTHROPIC_API_KEY, ASSET_CONFIG

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

_sentiment_cache = {
    "articles_hash": None,
    "result":        None,
}
_calendar_cache = {
    "date":   None,
    "events": [],
}


def get_economic_calendar() -> list:
    global _calendar_cache
    today = date.today().isoformat()
    if _calendar_cache["date"] == today and _calendar_cache["events"] is not None:
        print(f"[AI] Using cached calendar ({len(_calendar_cache['events'])} events)")
        return _calendar_cache["events"]

    print("[AI] Fetching today's economic calendar via Claude...")
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": f"""Search for today's economic calendar {today}.
Find ALL high impact events that affect gold (XAU/USD):
- FOMC decisions, minutes, Fed speeches
- US CPI, PPI, PCE
- US NFP, unemployment claims
- US GDP
- Major geopolitical developments

Return ONLY a valid JSON array, no other text:
[{{"time_utc": "14:00", "event": "FOMC Minutes", "impact": "high"}}]

If no high impact events today return exactly: []"""}]
        )

        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text

        text  = text.strip()
        start = text.find("[")
        end   = text.rfind("]") + 1
        if start >= 0 and end > start:
            events = json.loads(text[start:end])
        else:
            events = []

        _calendar_cache["date"]   = today
        _calendar_cache["events"] = events
        print(f"[AI] Calendar: {len(events)} high impact events today")
        return events

    except Exception as e:
        print(f"[AI] Calendar fetch failed: {e}")
        _calendar_cache["date"]   = today
        _calendar_cache["events"] = []
        return []


def has_upcoming_event(minutes_ahead: int = 60) -> dict:
    events = get_economic_calendar()
    now    = datetime.now(timezone.utc)

    for event in events:
        try:
            time_str     = event.get("time_utc", "")
            hour, minute = map(int, time_str.split(":"))
            event_dt     = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            diff_mins    = (event_dt - now).total_seconds() / 60

            if -30 <= diff_mins <= minutes_ahead:
                return {
                    "blocked":      True,
                    "event":        event.get("event", "Unknown"),
                    "minutes_away": int(diff_mins),
                }
        except Exception:
            continue

    return {"blocked": False, "event": None, "minutes_away": None}


def _hash_articles(articles: list) -> str:
    headlines = "".join([a.get("headline", "") for a in articles[:5]])
    return hashlib.md5(headlines.encode()).hexdigest()


def get_news_sentiment(articles: list) -> dict:
    global _sentiment_cache

    if not articles:
        return {"direction": "neutral", "confidence": 0.0, "reasoning": "No news articles available"}

    current_hash = _hash_articles(articles)
    if current_hash == _sentiment_cache["articles_hash"] and _sentiment_cache["result"]:
        cached = _sentiment_cache["result"]
        print(f"[AI] Using cached sentiment: {cached['direction']}")
        return cached

    print("[AI] New articles -- analyzing sentiment...")
    news_text = "\n\n".join([
        f"Headline: {a['headline']}\nSummary: {a['summary']}"
        for a in articles[:5]
    ])

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": f"""Analyze these gold (XAU/USD) news articles.

{news_text}

Respond in exactly this format:
SENTIMENT: [BULLISH or BEARISH or NEUTRAL]
CONFIDENCE: [0.0 to 1.0]
REASONING: [one sentence max]"""}]
        )

        raw    = response.content[0].text.strip()
        result = {"direction": "neutral", "confidence": 0.0, "reasoning": raw}

        for line in raw.split("\n"):
            if line.startswith("SENTIMENT:"):
                result["direction"] = line.split(":", 1)[1].strip().lower()
            elif line.startswith("CONFIDENCE:"):
                try:
                    result["confidence"] = float(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("REASONING:"):
                result["reasoning"] = line.split(":", 1)[1].strip()

        _sentiment_cache["articles_hash"] = current_hash
        _sentiment_cache["result"]        = result
        return result

    except Exception as e:
        print(f"[AI] Sentiment error: {e}")
        return {"direction": "neutral", "confidence": 0.0, "reasoning": f"Claude unavailable: {str(e)[:60]}"}


def score_trade(trend: dict, sentiment: dict, sl_hits_today: int) -> dict:
    direction  = trend.get("trade_bias")
    daily      = trend.get("daily_bias", {}).get("direction", "unknown")
    htf        = trend.get("htf_bias", {}).get("direction", "unknown")
    five_min   = trend.get("direction", "neutral")
    adx        = trend.get("strength", 0)
    volatility = trend.get("volatility", {}).get("regime", "normal")
    sent_dir   = sentiment.get("direction", "neutral")
    sent_conf  = sentiment.get("confidence", 0.0)

    expected    = "bullish" if direction == "buy" else "bearish"
    event_check = has_upcoming_event(minutes_ahead=60)

    c1 = daily    == expected
    c2 = htf      == expected
    c3 = five_min == expected
    c4 = adx      >= 25
    c5 = not (sent_dir == ("bearish" if direction == "buy" else "bullish") and sent_conf >= 0.5)
    c6 = not event_check["blocked"]
    c7 = volatility == "normal"
    c8 = sl_hits_today < 3

    breakdown = {
        "daily_agrees": c1,
        "htf_agrees":   c2,
        "5min_agrees":  c3,
        "adx_ok":       c4,
        "sentiment_ok": c5,
        "no_event":     c6,
        "vol_normal":   c7,
        "sl_limit_ok":  c8,
    }

    score     = sum(breakdown.values())
    tradeable = score >= 7

    failed    = [k for k, v in breakdown.items() if not v]
    if not failed:
        reasoning = f"Score {score}/8 -- all conditions met"
    else:
        reasoning = f"Score {score}/8 -- failed: {', '.join(failed)}"

    if event_check["blocked"]:
        reasoning += f" | WARNING: {event_check['event']} in {event_check['minutes_away']}min"

    return {
        "score":     score,
        "breakdown": breakdown,
        "reasoning": reasoning,
        "tradeable": tradeable,
        "event":     event_check,
    }
