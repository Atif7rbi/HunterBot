
# 🎯 Liquidity Hunter Bot — PRO v2.0

> **Philosophy**: Don't chase price — hunt liquidity.
> The market always seeks the trapped side. This bot analyzes where pending orders cluster and where leveraged positions will be forcibly liquidated, then trades *with* those flows — not against them.

---

## 📋 Overview

**Liquidity Hunter Bot** is a quantitative trading bot operating on Binance USDT-M Perpetual Futures in full **Paper Trading** mode (simulated execution, no real money). It uses a **10-layer pipeline** to analyze the market and generate high-conviction trade setups based on:

- **Liquidity maps** (where liquidation fuel clusters)
- **Positioning analysis** (who is trapped — Longs or Shorts?)
- **Funding rate** (who is paying to hold?)
- **Open Interest** (accumulating or unwinding?)
- **Long/Short ratio** (smart money diverging from retail?)

> ⚠️ **Disclaimer**: This system is for learning and paper testing only. Do not use it with real money until you have verified 100+ documented trades with a positive expectancy.

---

## 🏗️ Architecture — 10 Layers

```
Market (Binance Futures API)
         │
         ▼
┌─────────────────────────────────────────────────┐
│  Layer 1 — Scanner                              │
│  Filter all USDT-M Perpetuals by volume,        │
│  OI, and extremity signals                      │
└────────────────────┬────────────────────────────┘
                     │ shortlist (top-N symbols)
                     ▼
┌─────────────────────────────────────────────────┐
│  Layer 2 — Data Collector                       │
│  Pull full snapshot: OHLCV, OI, LS Ratio,       │
│  Funding, Liquidations, Taker Buy/Sell          │
└────────────────────┬────────────────────────────┘
                     │
         ┌───────────┴───────────┐
         ▼                       ▼
┌────────────────┐     ┌─────────────────────────┐
│  Layer 3       │     │  Layer 4                │
│  Liquidity     │     │  Positioning Analyzer   │
│  Engine        │     │  6 market states:       │
│  Liquidation   │     │  Crowded Long/Short,    │
│  cluster map   │     │  Smart Money Div.,      │
│                │     │  OI Exhaustion/Accum.   │
└───────┬────────┘     └──────────┬──────────────┘
        │                         │
        └─────────────┬───────────┘
                      ▼
┌─────────────────────────────────────────────────┐
│  Layer 5 — Context Layer                        │
│  24h memory — has this signal repeated?         │
└────────────────────┬────────────────────────────┘
                     ▼
┌─────────────────────────────────────────────────┐
│  Layer 6 — Regime Detector                      │
│  Trending / Range / Volatile (ADX-based)        │
└────────────────────┬────────────────────────────┘
                     ▼
┌─────────────────────────────────────────────────┐
│  Layer 7 — Decision Engine                      │
│  Scores 0–100 across 5 weighted components:     │
│  liquidity 30% + positioning 25% + OI 20%       │
│  + funding 15% + price action 10%               │
└────────────────────┬────────────────────────────┘
                     │ score ≥ 60 → setup
                     ▼
┌─────────────────────────────────────────────────┐
│  Layer 8 — Trigger Confirmation                 │
│  Validates: volume spike + OI reaction          │
│  + rejection wick (requires 2 of 3)             │
└────────────────────┬────────────────────────────┘
                     │ confirmed
                     ▼
┌─────────────────────────────────────────────────┐
│  Layer 9 — Trade Generator                      │
│  Builds TradeCard: entry zone, SL, TP1/2/3, RR  │
│  Rejected if RR < 2.0                           │
└────────────────────┬────────────────────────────┘
                     ▼
┌─────────────────────────────────────────────────┐
│  Layer 10 — Paper Executor                      │
│  Simulated fill: slippage + spread + kill-switch│
│  PENDING → TRIGGERED → CLOSED_TP / CLOSED_SL   │
└─────────────────────────────────────────────────┘
         │                    │
         ▼                    ▼
┌──────────────┐    ┌──────────────────────────┐
│  Backtest    │    │  Learning Loop           │
│  Engine      │    │  PerformanceAnalyzer +   │
│  Walk-forward│    │  AdaptiveWeightsEngine   │
└──────────────┘    └──────────────────────────┘
```

---

## ⚙️ Full Scan Cycle

Every **N seconds** (default: 300s) the following cycle runs:

```
1. Scanner.scan()
   └─ Fetches all USDT-M perpetuals from Binance
   └─ Filters: 24h volume ≥ 100M$ + OI ≥ threshold + high extremity score
   └─ Returns: shortlist (top N symbols)

2. DataCollector.collect(symbols)
   └─ Full snapshot per symbol in the shortlist

3. Per symbol (parallel):
   a. LiquidityEngine.build_map(snap)    → liquidation cluster map
   b. PositioningAnalyzer.analyze(snap)  → market state
   c. ContextLayer.analyze(snap)         → historical context
   d. RegimeDetector.detect(snap)        → market regime

4. DecisionEngine.evaluate(...)
   └─ Computes score 0–100
   └─ score < 60       → ignore
   └─ 60 ≤ score < threshold → WATCH ZONE (Telegram alert only)
   └─ score ≥ threshold + trigger confirmed → SETUP

5. TriggerConfirmation.check()
   └─ Validates 3 conditions:
      -  Volume Spike  (volume > X× average)
      -  OI Reaction   (OI change > threshold)
      -  Rejection Wick (tail on candle)
   └─ Requires 2 of 3

6. TradeGenerator.generate()
   └─ Calculates: entry_zone, SL, TP1/TP2/TP3
   └─ Rejected if RR < 2.0

7. PaperExecutor.submit()
   └─ Saves trade as PENDING in DB
   └─ Monitors price on every tick:
      -  Price enters zone  → TRIGGERED  ⚡
      -  Price runs > 3%    → CANCELLED
      -  Age > 8 hours      → CANCELLED (expired)
      -  SL hit             → CLOSED_SL  ❌
      -  TP2 hit            → CLOSED_TP  ✅

8. Learning Loop (every 5 closed trades)
   └─ PerformanceAnalyzer reviews last 30 trades
   └─ AdaptiveWeightsEngine adjusts the 5 component weights
```

---

## 🧠 Self-Learning System

### How It Works
After every **5 closed trades** (TP or SL), the bot analyzes recent performance and automatically adjusts Decision Engine weights:

```python
# Example: if high funding_extreme trades win more often
# AdaptiveWeightsEngine raises funding weight: 0.15 → 0.18
```

### Current Settings

| Parameter | Value | Meaning |
|---|---|---|
| `enabled` | `true` | Learning is active |
| `min_sample_size` | `10` | Starts after 10 closed trades |
| `adapt_every_n_trades` | `5` | Re-adapts every 5 trades |
| `rolling_window` | `30` | Only studies the last 30 trades |

---

## 📊 Database Schema

### Main Tables

| Table | Description |
|---|---|
| `Trade` | Every trade: PENDING → TRIGGERED → CLOSED |
| `TradeOutcome` | Closed trade results (used for learning) |
| `ScanSnapshot` | Results of each scan cycle |
| `PortfolioSnapshot` | Equity curve over time |
| `LiquidityMap` | Persisted liquidation cluster maps |

### Trade Lifecycle

```
PENDING      → Price has not entered the entry zone yet
    ├── TRIGGERED   → Price entered zone, entry filled
    │       ├── CLOSED_TP   → TP2 hit ✅
    │       └── CLOSED_SL   → SL hit  ❌
    └── CANCELLED   → Missed zone (>3%) or expired (>8h)
```

---

## 🛡️ Risk Management — Hard Rules

| Rule | Value |
|---|---|
| Paper capital | $10,000 |
| Risk per trade | 1% of equity |
| Max concurrent positions | 3 |
| Max daily loss | 5% |
| Max consecutive losses | 3 |
| Minimum Risk/Reward | 2.0 (preferred 2.5) |
| Entry slippage | 0.05% |
| Stop slippage | 0.10% |
| Spread | 0.02% |

---

## 🖥️ Dashboard

Web UI built with **NiceGUI** on port `8090`:

| Page | Content |
|---|---|
| **Overview** | KPIs (Equity, PnL, Win Rate, Open Positions, Kill Switch) + recent signals + recent closed trades |
| **Scanner** | Latest shortlist with all metrics |
| **Signals** | Decision Engine breakdown per symbol (5 component scores) |
| **Trades** | Open positions + full trade history |
| **Backtest** | Launch backtest from the UI |
| **Settings** | Live view of all `config.yaml` parameters |

### Dashboard Features
- **Countdown timer** to next scan (turns red at < 15 seconds)
- **Auto-refresh** when a new scan cycle completes
- **Live uptime counter**
- **Full dark mode**
- **Live progress bar** during scan execution

---

## 📱 Telegram Alerts

| Alert Type | Trigger |
|---|---|
| 🎯 Setup Alert | New trade submitted |
| 👁️ Watch Zone | High-score symbol, not yet confirmed |
| ✅ TP Hit | Trade closed with profit |
| ❌ SL Hit | Trade closed at stop loss |
| 📊 Weekly Report | Every Sunday automatically |
| 🚨 Kill Switch | Daily loss limit reached |

---

## 🔧 Installation & Running

### Requirements

```
Python 3.11+
SQLite (default) or PostgreSQL
Telegram account (for alerts)
```

### Install

```bash
git clone <repo>
cd liquidity_hunter
python -m venv .venv
source .venv/bin/activate   # Linux/Mac
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your Telegram credentials
python -m src.main init
```

### Run

```bash
# Dashboard (recommended)
python -m ui.app
# Open http://localhost:8090

# CLI only
python -m src.main bot

# Single scan
python -m src.main scan

# Backtest
python -m src.main backtest --symbol BTCUSDT --days 60 --timeframe 1h
```

---

## 🗂️ Project Structure

```
liquidity_hunter/
├── config.yaml              ← All settings (no magic numbers in code)
├── .env                     ← Secrets (Telegram, DB URL)
├── requirements.txt
├── data/
│   └── liquidity_hunter.db  ← SQLite database
├── logs/                    ← Rotating log files
├── src/
│   ├── core/
│   │   ├── config.py
│   │   ├── database.py
│   │   └── logger.py
│   ├── exchange/
│   │   └── binance_client.py
│   ├── layers/              ← Layers 1–10
│   ├── backtest/
│   │   └── engine.py
│   ├── learning/
│   │   ├── outcome_logger.py
│   │   ├── performance_analyzer.py
│   │   └── adaptive_weights.py
│   ├── alerts/
│   │   └── telegram_bot.py
│   └── main.py              ← Orchestrator + CLI
└── ui/
    ├── app.py               ← NiceGUI Dashboard
    ├── theme.py
    └── components/
        └── widgets.py
```

---

## 📈 Roadmap

| Version | Status | Content |
|---|---|---|
| **v1** (current) | ✅ Done | Paper Trading + Dashboard + Telegram + Backtest |
| **v2** | 🔄 Planned | Multi-timeframe confirmations + OI/Funding historical series in Backtest |
| **v3** | 📋 Future | Live execution via Binance API (after 100 paper trades + Sharpe ≥ 1.0) |
| **v4** | 📋 Future | VPS deployment + PostgreSQL + Grafana monitoring |

---

## 🐛 Fixes & Improvements Applied (v2.0)

### 🔴 Critical Fixes

| File | Bug | Fix |
|---|---|---|
| `paper_executor.py` | `pass` in trigger logic — all trades stayed PENDING then CANCELLED | Replaced with `await self.trigger()` when price enters the entry zone |
| `paper_executor.py` | `missed_entry_max_pct: 0.003` (0.3%) too tight for volatile alts | Raised to `0.03` (3%) |
| `paper_executor.py` | PENDING trades expired after only 4 hours | Extended to 8 hours |
| `app.py` | `TypeError: can't subtract offset-naive and offset-aware datetimes` → 500 error | Added `tzinfo is None` check before datetime subtraction |
| `engine.py` | `--timeframe` not supported in Backtest CLI | Added full `timeframe` parameter |
| `database.py` | `layer_scores` not persisted on Trade row | Post-submit patch applied in `main.py` |

### 🟡 Improvements

| File | Improvement |
|---|---|
| `app.py` | Next-scan countdown in topbar (green → yellow → red) |
| `app.py` | Fixed Closed Trades table (uses correct `actual_entry_price` column) |
| `telegram_bot.py` | Automatic weekly report every Sunday |
| `telegram_bot.py` | Watch Zone alerts separated from Setup alerts |
| `config.yaml` | Optimized Learning settings: `min_sample_size: 10`, `rolling_window: 30` |

---

## ⚖️ Strengths & Weaknesses

### ✅ Strengths

- **No retail indicators** (no RSI, no MACD) — analyzes real market structure
- **10 independent layers** — each testable and replaceable in isolation
- **Self-learning system** that adapts component weights every 5 trades
- **Automatic kill-switch** protects capital daily
- **Walk-forward backtest** — no curve fitting
- **All settings in `config.yaml`** — zero magic numbers in code
- **Full dashboard** + Telegram alerts

### ⚠️ Weaknesses

- **Single data source** (Binance only) — API outage = full stop
- **No multi-timeframe support** yet — analyzes one timeframe only
- **Backtest lacks historical OI/Funding series** — may overestimate results
- **No partial take profit** — closes fully at TP2
- **Self-learning requires closed trades** — won't start until 10 real closes
- **SQLite** not suitable for high concurrency (UI + Bot simultaneous writes)

---

## 🔑 Environment Variables (.env)

```env
# Database
DATABASE_URL=sqlite+aiosqlite:///data/liquidity_hunter.db
# PostgreSQL for VPS:
# DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/liquidity_hunter

# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here

# Environment
ENV=development
```

---

## ⚙️ Key Configuration (config.yaml)

```yaml
scanner:
  scan_interval_seconds: 300
  top_n_to_monitor: 15
  min_quote_volume_24h_usd: 100_000_000
  funding_extreme_threshold: 0.0005

decision_engine:
  min_score_to_signal: 60
  min_score_full_size: 75

paper_executor:
  initial_capital_usd: 10_000
  risk_per_trade_pct: 0.01
  max_concurrent_trades: 3
  missed_entry_max_pct: 0.03       # 3% — suitable for volatile alts
  daily_max_loss_pct: 0.05
  daily_max_consecutive_losses: 3

learning:
  enabled: true
  min_sample_size: 10
  adapt_every_n_trades: 5
  rolling_window: 30
```

---

*Last updated: May 2026 — Paper Trading Mode only*
```
