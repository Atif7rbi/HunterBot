"""Backtest Engine — replays historical data through the same decision pipeline.

Foundational principle: backtest uses the SAME functions as live — no logic
duplication. We just feed historical data instead of live data, and walk forward
candle by candle.

Output: list of simulated trades + statistics (win rate, avg R, max DD, sharpe).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from src.core.config import settings
from src.core.logger import logger
from src.exchange.binance_client import BinanceFuturesClient
from src.exchange.data_fetcher import FullSnapshot, klines_to_df


@dataclass
class BacktestTrade:
    symbol: str
    direction: str
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    sl: float
    tp: float
    pnl_pct: float
    pnl_r: float
    setup_score: float
    outcome: str  # "WIN" / "LOSS"


@dataclass
class BacktestStats:
    symbol: str
    period_days: int
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    avg_win_r: float
    avg_loss_r: float
    expectancy_r: float
    max_drawdown_r: float
    sharpe: float
    profit_factor: float
    trades: list[BacktestTrade] = field(default_factory=list)


class BacktestEngine:
    """Simplified backtest — uses positioning + price action.

    For full fidelity we'd need historical funding/OI/LS series for every candle,
    which Binance provides at 5min granularity but with strict rate limits.
    This engine focuses on the price-action piece using OHLCV — which is the
    most-shared component between v1 and live signals.
    """

    def __init__(self) -> None:
        self.cfg = settings.backtest

    async def fetch_history(
        self, symbol: str, days: int = 180, interval: str = "1h"
    ) -> pd.DataFrame:
        """Pull up to N days of historical candles."""
        async with BinanceFuturesClient() as client:
            # Each call returns max 1500 candles
            all_klines: list = []
            end_ms = int(datetime.utcnow().timestamp() * 1000)
            interval_ms = {"15m": 15 * 60_000, "1h": 60 * 60_000, "4h": 4 * 60 * 60_000}[interval]
            target_count = days * 24 * (60_000 * 60 // interval_ms)
            while len(all_klines) < target_count:
                # Klines endpoint walks forward; use endTime to walk back
                # For simplicity we just take the last min(target,1500) from now
                k = await client.klines(symbol, interval, limit=min(1500, target_count))
                all_klines = k
                break  # MVP: one batch (~62 days at 1h)
            return klines_to_df(all_klines)

    def _walk_forward(
        self, df: pd.DataFrame, symbol: str, lookback: int = 50
    ) -> list[BacktestTrade]:
        """Walk candle by candle, generate simulated trades using simplified rules.

        For each candle i:
          - Look at last `lookback` candles
          - Detect swing high/low (recent pivot)
          - If current candle sweeps above prior swing high THEN closes back below → SHORT
          - If current candle sweeps below prior swing low THEN closes back above → LONG
          - SL = beyond the swept level; TP = 2× risk
          - Walk forward simulating until SL or TP hit
        """
        trades: list[BacktestTrade] = []
        if len(df) < lookback + 10:
            return trades

        i = lookback
        while i < len(df) - 50:
            window = df.iloc[i - lookback : i]
            swing_high = window["high"].max()
            swing_low = window["low"].min()
            current = df.iloc[i]

            direction: Optional[str] = None
            entry: float = 0
            sl: float = 0
            tp: float = 0
            swept_level: float = 0

            # Sweep above + reject (SHORT)
            if current["high"] > swing_high and current["close"] < swing_high:
                direction = "SHORT"
                swept_level = swing_high
                entry = current["close"]
                sl = current["high"] * 1.002
                risk = sl - entry
                tp = entry - risk * 2

            # Sweep below + reject (LONG)
            elif current["low"] < swing_low and current["close"] > swing_low:
                direction = "LONG"
                swept_level = swing_low
                entry = current["close"]
                sl = current["low"] * 0.998
                risk = entry - sl
                tp = entry + risk * 2

            if direction is None:
                i += 1
                continue

            # Walk forward to find exit
            exit_price: Optional[float] = None
            exit_idx: Optional[int] = None
            outcome: Optional[str] = None
            for j in range(i + 1, min(i + 100, len(df))):
                bar = df.iloc[j]
                if direction == "LONG":
                    if bar["low"] <= sl:
                        exit_price = sl
                        outcome = "LOSS"
                        exit_idx = j
                        break
                    if bar["high"] >= tp:
                        exit_price = tp
                        outcome = "WIN"
                        exit_idx = j
                        break
                else:
                    if bar["high"] >= sl:
                        exit_price = sl
                        outcome = "LOSS"
                        exit_idx = j
                        break
                    if bar["low"] <= tp:
                        exit_price = tp
                        outcome = "WIN"
                        exit_idx = j
                        break

            if exit_price and exit_idx and outcome:
                pnl = (exit_price - entry) / entry if direction == "LONG" else (entry - exit_price) / entry
                # Apply fees
                pnl -= 2 * self.cfg["fee_pct"]
                # R measure
                risk_pct = abs(entry - sl) / entry
                pnl_r = pnl / risk_pct if risk_pct else 0

                trades.append(
                    BacktestTrade(
                        symbol=symbol,
                        direction=direction,
                        entry_time=current["close_time"],
                        entry_price=entry,
                        exit_time=df.iloc[exit_idx]["close_time"],
                        exit_price=exit_price,
                        sl=sl,
                        tp=tp,
                        pnl_pct=pnl,
                        pnl_r=pnl_r,
                        setup_score=70.0,  # placeholder for v1
                        outcome=outcome,
                    )
                )
                i = exit_idx + 1
            else:
                i += 1

        return trades

    def compute_stats(
        self, trades: list[BacktestTrade], symbol: str, period_days: int
    ) -> BacktestStats:
        if not trades:
            return BacktestStats(
                symbol=symbol, period_days=period_days, total_trades=0,
                wins=0, losses=0, win_rate=0, avg_win_r=0, avg_loss_r=0,
                expectancy_r=0, max_drawdown_r=0, sharpe=0, profit_factor=0,
            )

        wins = [t for t in trades if t.outcome == "WIN"]
        losses = [t for t in trades if t.outcome == "LOSS"]

        win_rate = len(wins) / len(trades)
        avg_win_r = np.mean([t.pnl_r for t in wins]) if wins else 0.0
        avg_loss_r = np.mean([t.pnl_r for t in losses]) if losses else 0.0
        expectancy = win_rate * avg_win_r + (1 - win_rate) * avg_loss_r

        # Equity curve in R
        rs = [t.pnl_r for t in trades]
        cum = np.cumsum(rs)
        peak = np.maximum.accumulate(cum)
        dd = peak - cum
        max_dd = float(np.max(dd)) if len(dd) > 0 else 0.0

        # Sharpe (over per-trade R, annualized assuming N trades/year ≈ 250)
        if np.std(rs) > 0:
            sharpe = float(np.mean(rs) / np.std(rs) * np.sqrt(250))
        else:
            sharpe = 0.0

        gross_profit = sum(t.pnl_r for t in wins)
        gross_loss = abs(sum(t.pnl_r for t in losses))
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        return BacktestStats(
            symbol=symbol,
            period_days=period_days,
            total_trades=len(trades),
            wins=len(wins),
            losses=len(losses),
            win_rate=win_rate,
            avg_win_r=avg_win_r,
            avg_loss_r=avg_loss_r,
            expectancy_r=expectancy,
            max_drawdown_r=max_dd,
            sharpe=sharpe,
            profit_factor=pf,
            trades=trades,
        )

    async def run(self, symbol: str, days: int = 60, timeframe: str = "1h") -> BacktestStats:
        logger.info(f"Backtest: {symbol} over {days}d @ {timeframe}")
        df = await self.fetch_history(symbol, days=days, interval=timeframe)
        if df.empty:
            return self.compute_stats([], symbol, days)
        trades = self._walk_forward(df, symbol)
        stats = self.compute_stats(trades, symbol, days)
        logger.info(
            f"Backtest {symbol}: {stats.total_trades} trades | WR {stats.win_rate*100:.1f}% | "
            f"Expectancy {stats.expectancy_r:+.2f}R | Sharpe {stats.sharpe:.2f} | "
            f"MaxDD {stats.max_drawdown_r:.1f}R"
        )
        return stats
