# Trading Bot AI

AI-assisted paper-trading system for OANDA instruments, currently focused on:

- `XAU_USD` / Gold
- `EUR_JPY`

The project is built around one main idea:

> Do not use AI to blindly predict price. Use Python to translate market data into mathematical market conditions, then use Claude as a human-style trade reviewer.

This project is for educational and paper-trading research purposes only. It is not financial advice.

---

## Current Structure

At this baseline, the bot uses two standalone monitor files:

```text
gold_monitor.py      # Standalone Gold / XAU_USD monitor
eurjpy_monitor.py    # Standalone EUR/JPY monitor
requirements.txt     # Python dependencies
```

The current files are intentionally simple and self-contained before the next architecture upgrade.

---

## Core Philosophy

The goal is not to predict exact future prices.

The goal is to recognise market conditions mathematically.

Instead of asking:

```text
Will gold go to 4750?
```

the bot should ask:

```text
What condition is the market in right now?
Is this range, trend ignition, pullback continuation, exhaustion, or chop?
```

Then each market condition gets its own trading behaviour.

---

## Python vs Claude

The bot should separate responsibilities clearly.

### Python = Calculator, Analyst, and Referee

Python handles objective facts:

- Did price actually break the level?
- Is the candle close above or below support/resistance?
- Is EMA alignment bullish or bearish?
- Is volume expanding?
- Is the move extended from EMA9/EMA50?
- Is the stop distance valid?
- Is the risk-to-reward valid?
- Is this a late chase?
- Is the bot using the practice account?
- Is there already an open position?
- Should the trade be blocked?

Python should not allow Claude to invent arithmetic or fake breakouts.

### Claude = Human-Style Trade Reviewer

Claude acts like a trader reviewing a clean setup sheet.

Claude should judge:

- Is the setup clean or messy?
- Does the trade idea make sense in context?
- Is this a valid continuation or a late chase?
- Should the bot enter now, wait for a pullback, or skip?
- Is the explanation logically consistent?

Claude should review facts. Python should verify facts.

---

## Market-State Logic

The long-term goal is to move beyond simple labels like:

```text
BULLISH
BEARISH
RANGING
```

and classify more useful market states:

```text
RANGE
BULLISH_TREND_IGNITION
BEARISH_TREND_IGNITION
BULLISH_PULLBACK_CONTINUATION
BEARISH_PULLBACK_CONTINUATION
EXHAUSTION
CHOP
NO_TRADE
```

This matters because range days and trend days need different behaviour.

On a range day:

```text
Buy support.
Short resistance.
Do not chase the middle.
Use nearer targets.
```

On a trend day:

```text
Do not treat every new high as automatic resistance.
Look for ignition, acceptance, pullbacks, and continuation.
Use meaningful trend targets, not tiny nearby levels.
```

---

## Lesson From Previous Bot Behaviour

The older bot appeared to have edge because it was more momentum-aware and AI-driven.

However, it also had weaknesses:

- It sometimes generated random signals on strong trend days.
- It allowed Claude to describe breakouts that were not mathematically true.
- It sometimes identified trend continuation too late.
- It often used targets that were too close, causing poor risk-to-reward.
- It could confuse movement with a valid trade.

The newer logic is safer, but it became too defensive.

The intended direction is a hybrid:

```text
Old bot momentum awareness
+
New bot safety filters
+
Mathematical market-state detection
+
Claude as a focused reviewer
```

---

## Target Architecture

The intended bot architecture is:

```text
Market candles
  ↓
Python feature engine
  ↓
Market-state classifier
  ↓
Claude trade review
  ↓
Python execution validation
  ↓
Paper execution / Telegram alert
  ↓
Decision logging
  ↓
Outcome tracking
  ↓
Trade memory and lessons
```

Claude should receive a small, connected case file, not a huge disconnected prompt.

Example:

```text
Market state: BULLISH_TREND_IGNITION

Evidence:
- 5 of last 6 candles green
- Price broke the 2-hour range high
- EMA9 > EMA26
- Price above EMA50
- Volume expansion detected
- Extension from EMA9 is high
- Nearest target gives poor R:R

Task:
Choose ENTER_NOW, WAIT_PULLBACK, or NO_TRADE.
```

---

## Machine Learning Direction

Machine learning should not be used as a direct price predictor.

The better use is pattern recognition:

```text
Candles → features → market condition → bot behaviour
```

Possible features:

- Candle body size
- Candle range
- Wick size
- Body-to-range ratio
- Consecutive green/red candles
- Volume expansion
- ATR expansion
- EMA9/EMA26/EMA50 alignment
- EMA slope
- Distance from EMA
- Breakout distance
- Recent range high/low
- Pullback depth
- Extension score

Later, the system can learn from past examples:

```text
When candles looked like this, did price usually continue enough before reversing?
```

This is classification and pattern recognition, not price prediction.

---

## Trade Memory

Claude itself does not permanently learn from experience through the normal Anthropic API.

Instead, the bot should build its own memory system.

Planned memory design:

```text
SQLite database
  ↓
Store every market snapshot
  ↓
Store every Claude decision
  ↓
Store every Python validation result
  ↓
Store every trade outcome
  ↓
Generate short lessons
  ↓
Retrieve only relevant lessons for future Claude calls
```

The full history stays in SQLite.

Claude only receives the top 2–3 relevant lessons for the current setup.

---

## Planned Next Improvements

1. Add decision audit logging
2. Add SQLite database storage
3. Build a feature engine
4. Add market-state classification
5. Add trend ignition detection
6. Add pullback continuation detection
7. Improve Claude prompt structure
8. Store skipped trades and executed trades
9. Track outcomes after signals
10. Retrieve similar past setups before Claude review
11. Backtest market states on historical data
12. Optimise each stage separately

---

## Environment Variables

API keys and private settings should be stored in a local `.env` file.

Do not commit `.env` to GitHub.

Typical values may include:

```env
OANDA_ACCESS_TOKEN=your_oanda_token_here
OANDA_ACCOUNT_ID=your_oanda_account_id_here
OANDA_ENVIRONMENT=practice

ANTHROPIC_API_KEY=your_anthropic_key_here

TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here
```

Check the monitor scripts for the exact variable names used.

---

## Installation

Create and activate a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## Running the Bots

Run Gold monitor:

```bash
python3 gold_monitor.py
```

Run EUR/JPY monitor:

```bash
python3 eurjpy_monitor.py
```

Run in the background on EC2:

```bash
nohup python3 gold_monitor.py > gold.log 2>&1 &
nohup python3 eurjpy_monitor.py > eurjpy.log 2>&1 &
```

Check running bots:

```bash
pgrep -af "gold_monitor|eurjpy_monitor"
```

Stop bots:

```bash
pkill -f gold_monitor.py
pkill -f eurjpy_monitor.py
```

View logs:

```bash
tail -f gold.log
tail -f eurjpy.log
```

---

## Repository Hygiene

Do not commit:

- `.env`
- API keys
- `.pem` files
- logs
- databases
- CSV outputs
- runtime state
- cache files

The repository should track source code and documentation only.

---

## Baseline

This baseline represents the cleaned standalone version of the Gold and EUR/JPY monitor bots.

From this point forward, all major changes should be committed through Git so the project can be reviewed, tested, and rolled back safely.
