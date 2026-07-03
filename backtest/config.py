from __future__ import annotations

from pathlib import Path


# Backtest module settings.
# These are UI/backtest-only defaults and do not affect live trading.
RESULTS_DIR = Path("backtest/results")

DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_TIMEFRAME = "1h"
DEFAULT_DAYS = 60
DEFAULT_INITIAL_CAPITAL_USD = 1000.0
DEFAULT_FEE_PCT = 0.0004
DEFAULT_WARMUP_CANDLES = 100

MAX_RECENT_RUNS = 20
LATEST_TRADES_LIMIT = 50

SUPPORTED_TIMEFRAMES = ["15m", "1h", "4h"]

# Engine skeleton controls.
# This stays disabled until a real historical data source is wired.
ALLOW_PLACEHOLDER_RUNS = True
