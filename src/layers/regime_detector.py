"""Layer 6 — Regime Detector.

Classifies market regime: TRENDING vs RANGING vs VOLATILE.
Uses ADX-like calculation + Bollinger Band width — these are structural
measurements, NOT retail entry signals (consistent with bot philosophy).

Why it matters: same positioning signal works differently in different regimes.
A "Crowded Long Trap" reverts hard in a range, may grind higher in strong uptrend.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.core.config import settings
from src.core.database import MarketRegime
from src.exchange.data_fetcher import FullSnapshot


@dataclass
class RegimeResult:
    symbol: str
    regime: MarketRegime
    adx: float
    bb_width_pct: float
    direction_strength: float  # -1 (strong down) .. +1 (strong up)
    notes: list[str]


def _adx(df: pd.DataFrame, period: int = 14) -> float:
    """Compute ADX. Pure pandas/numpy implementation."""
    if len(df) < period * 2:
        return 0.0

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    plus_dm = high.diff()
    minus_dm = low.diff().mul(-1)
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan))
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan))
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)) * 100
    adx_series = dx.ewm(alpha=1 / period, adjust=False).mean()

    return float(adx_series.iloc[-1]) if not adx_series.empty else 0.0


def _bb_width(df: pd.DataFrame, period: int = 20) -> float:
    """Bollinger Band width as % of mid."""
    if len(df) < period:
        return 0.0
    close = df["close"].astype(float)
    mid = close.rolling(period).mean().iloc[-1]
    std = close.rolling(period).std().iloc[-1]
    if mid == 0 or pd.isna(mid):
        return 0.0
    return float((4 * std) / mid)


class RegimeDetector:
    def __init__(self) -> None:
        self.cfg = settings.regime_detector

    def detect(self, snap: FullSnapshot) -> RegimeResult:
        df = snap.klines_1h
        notes: list[str] = []

        if df.empty or len(df) < 30:
            return RegimeResult(
                symbol=snap.symbol, regime=MarketRegime.RANGING,
                adx=0.0, bb_width_pct=0.0, direction_strength=0.0, notes=["insufficient data"],
            )

        adx_val = _adx(df, period=self.cfg["adx_period"])
        bbw = _bb_width(df, period=self.cfg["bb_width_period"])

        # Direction
        ema_fast = df["close"].ewm(span=10).mean().iloc[-1]
        ema_slow = df["close"].ewm(span=30).mean().iloc[-1]
        direction = (ema_fast - ema_slow) / ema_slow if ema_slow else 0.0
        direction_strength = float(np.clip(direction * 50, -1.0, 1.0))

        # Volatile = wide BB width regardless of trend
        if bbw > 0.08:
            regime = MarketRegime.VOLATILE
            notes.append(f"BB width {bbw*100:.1f}% — high volatility")
        elif adx_val >= self.cfg["trending_threshold"]:
            regime = MarketRegime.TRENDING_UP if direction_strength > 0 else MarketRegime.TRENDING_DOWN
            notes.append(f"ADX {adx_val:.1f} — trending")
        elif adx_val <= self.cfg["ranging_threshold"]:
            regime = MarketRegime.RANGING
            notes.append(f"ADX {adx_val:.1f} — ranging")
        else:
            # Transitional zone — bias toward range (safer for liquidity sweep plays)
            regime = MarketRegime.RANGING
            notes.append(f"ADX {adx_val:.1f} — transitional, treat as range")

        return RegimeResult(
            symbol=snap.symbol,
            regime=regime,
            adx=adx_val,
            bb_width_pct=bbw,
            direction_strength=direction_strength,
            notes=notes,
        )
