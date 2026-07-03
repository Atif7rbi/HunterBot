"""Layer 3 — Liquidity Engine.

Identifies liquidity pools (zones where stop-losses cluster):
  - Above price: previous swing highs, round numbers, resistance levels
  - Below price: previous swing lows, support levels

Estimates "liquidation magnitude" using L/S positioning + OI as proxy.
Maps the imbalance: which side has more accumulated liquidity?

FIX v1.1:
  - Added distance_weight to estimated_liquidations_usd: closer zones weigh more
    (inner_liq = oi * share * strength * 0.15 * (1 / (1 + distance * 10)))
    This fixes near-zero imbalance bug where both sides scored almost equally.
  - Added multi-timeframe swing detection (15m + 1h + 4h) for richer zone map
  - Equal Highs/Lows detection: if 2+ swing points within 0.2%, mark as engineered liquidity
  - Strength now also accounts for multi-tf confluence
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from src.core.config import settings
from src.core.database import AsyncSessionLocal, LiquidityZone
from src.core.logger import logger
from src.exchange.data_fetcher import FullSnapshot

Side = Literal["ABOVE", "BELOW"]


@dataclass
class Zone:
    symbol: str
    price_level: float
    side: Side
    distance_pct: float
    strength: float            # 0..1
    estimated_liquidations_usd: float
    description: str


@dataclass
class LiquidityMap:
    symbol: str
    current_price: float
    zones_above: list[Zone]
    zones_below: list[Zone]
    imbalance: float           # -1 (all below) .. +1 (all above)
    dominant_side: Side
    primary_target: Zone | None


class LiquidityEngine:
    def __init__(self) -> None:
        self.cfg = settings.liquidity_engine

    def _find_swing_points(
        self, df: pd.DataFrame, lookback: int = 5
    ) -> tuple[list[float], list[float]]:
        """Pivot highs/lows using N-bar rule."""
        if len(df) < lookback * 2 + 1:
            return [], []
        highs = df["high"].values
        lows  = df["low"].values
        swing_highs: list[float] = []
        swing_lows:  list[float] = []
        for i in range(lookback, len(df) - lookback):
            window_h = highs[i - lookback : i + lookback + 1]
            window_l = lows[i  - lookback : i + lookback + 1]
            if highs[i] == window_h.max():
                swing_highs.append(float(highs[i]))
            if lows[i] == window_l.min():
                swing_lows.append(float(lows[i]))
        return swing_highs, swing_lows

    def _cluster_levels(
        self, levels: list[float], proximity_pct: float
    ) -> list[tuple[float, int]]:
        """Group nearby levels. Returns [(avg_price, count), ...]."""
        if not levels:
            return []
        sorted_l = sorted(levels)
        clusters: list[list[float]] = [[sorted_l[0]]]
        for lvl in sorted_l[1:]:
            last_avg = sum(clusters[-1]) / len(clusters[-1])
            if abs(lvl - last_avg) / last_avg <= proximity_pct:
                clusters[-1].append(lvl)
            else:
                clusters.append([lvl])
        return [(sum(c) / len(c), len(c)) for c in clusters]

    def _detect_equal_levels(
        self, levels: list[float], tolerance_pct: float = 0.002
    ) -> list[float]:
        """Find Equal Highs/Lows — engineered liquidity. Returns levels with 2+ touches."""
        equal: list[float] = []
        sorted_l = sorted(levels)
        i = 0
        while i < len(sorted_l):
            group = [sorted_l[i]]
            j = i + 1
            while j < len(sorted_l):
                if abs(sorted_l[j] - sorted_l[i]) / sorted_l[i] <= tolerance_pct:
                    group.append(sorted_l[j])
                    j += 1
                else:
                    break
            if len(group) >= 2:
                equal.append(sum(group) / len(group))
            i = j if j > i + 1 else i + 1
        return equal

    def _round_number_levels(
        self, current_price: float, range_pct: float = 0.05
    ) -> list[float]:
        """Find psychological round-number levels within range_pct of price."""
        if current_price > 10_000:
            step = 1000.0
        elif current_price > 1_000:
            step = 100.0
        elif current_price > 100:
            step = 10.0
        elif current_price > 10:
            step = 1.0
        elif current_price > 1:
            step = 0.1
        else:
            step = 0.01
        low  = current_price * (1 - range_pct)
        high = current_price * (1 + range_pct)
        levels: list[float] = []
        n = int(low / step)
        while n * step <= high:
            lvl = n * step
            if low <= lvl <= high:
                levels.append(lvl)
            n += 1
        return levels

    def build_map(self, snap: FullSnapshot) -> LiquidityMap:
        """Build a liquidity map for one symbol using multi-timeframe swings."""
        price = snap.price

        all_highs: list[float] = []
        all_lows:  list[float] = []

        # FIX: collect swing points from 3 timeframes for richer zone map
        for df, lookback in [
            (snap.klines_15m, 3),
            (snap.klines_1h,  5),
            (snap.klines_4h,  3),
        ]:
            if df is not None and not df.empty:
                recent = df.tail(self.cfg["swing_lookback_candles"])
                h, l = self._find_swing_points(recent, lookback=lookback)
                all_highs.extend(h)
                all_lows.extend(l)

        if not all_highs and not all_lows:
            return LiquidityMap(
                symbol=snap.symbol, current_price=price,
                zones_above=[], zones_below=[],
                imbalance=0.0, dominant_side="ABOVE", primary_target=None,
            )

        # Detect Equal Highs/Lows — high-probability engineered liquidity
        equal_highs = self._detect_equal_levels([h for h in all_highs if h > price])
        equal_lows  = self._detect_equal_levels([l for l in all_lows  if l < price])

        # Add round numbers
        round_levels = self._round_number_levels(price, range_pct=0.04)
        all_highs.extend([r for r in round_levels if r > price])
        all_lows.extend( [r for r in round_levels if r < price])

        # Cluster
        proximity = self.cfg["liquidity_zone_proximity_pct"]
        clusters_above = self._cluster_levels([h for h in all_highs if h > price], proximity)
        clusters_below = self._cluster_levels([l for l in all_lows  if l < price], proximity)

        oi_usd    = snap.open_interest_usd
        ls        = snap.ls_ratio_global
        long_share  = ls / (1 + ls)
        short_share = 1.0 - long_share

        def build_zones(clusters, side: Side, share: float) -> list[Zone]:
            zones: list[Zone] = []
            for lvl, count in clusters:
                if side == "ABOVE":
                    distance = (lvl - price) / price
                else:
                    distance = (price - lvl) / price
                if distance <= 0:
                    continue

                base_strength = min(count / 3, 1.0)

                # Boost strength for Equal High/Low zones
                ref_list = equal_highs if side == "ABOVE" else equal_lows
                is_equal = any(abs(lvl - eq) / eq < 0.003 for eq in ref_list)
                if is_equal:
                    base_strength = min(base_strength + 0.3, 1.0)
                    desc_extra = " [EQUAL — engineered liquidity]"
                else:
                    desc_extra = ""

                # FIX: distance_weight — closer zones get higher liquidation estimate
                # Formula: 1 / (1 + distance_pct * 10)
                # Example: 1% away → weight=0.91, 3% away → weight=0.77, 5% → 0.67
                distance_weight = 1.0 / (1.0 + distance * 10)

                est_liq = oi_usd * share * base_strength * 0.15 * distance_weight

                zones.append(Zone(
                    symbol=snap.symbol,
                    price_level=lvl,
                    side=side,
                    distance_pct=distance,
                    strength=base_strength,
                    estimated_liquidations_usd=est_liq,
                    description=f"{count} swing/round confluence{desc_extra}",
                ))
            return zones

        zones_above = build_zones(clusters_above, "ABOVE", short_share)
        zones_below = build_zones(clusters_below, "BELOW", long_share)

        # Sort by proximity
        zones_above.sort(key=lambda z: z.distance_pct)
        zones_below.sort(key=lambda z: z.distance_pct)

        # Imbalance
        total_above = sum(z.estimated_liquidations_usd for z in zones_above)
        total_below = sum(z.estimated_liquidations_usd for z in zones_below)
        denom = total_above + total_below
        imbalance = (total_above - total_below) / denom if denom > 0 else 0.0
        dominant: Side = "ABOVE" if imbalance >= 0 else "BELOW"

        target_pool = zones_above if dominant == "ABOVE" else zones_below
        primary = target_pool[0] if target_pool else None

        return LiquidityMap(
            symbol=snap.symbol,
            current_price=price,
            zones_above=zones_above,
            zones_below=zones_below,
            imbalance=imbalance,
            dominant_side=dominant,
            primary_target=primary,
        )

    async def persist(self, lmap: LiquidityMap) -> None:
        async with AsyncSessionLocal() as session:
            for z in lmap.zones_above + lmap.zones_below:
                session.add(LiquidityZone(
                    symbol=z.symbol,
                    price_level=z.price_level,
                    side=z.side,
                    estimated_liquidations_usd=z.estimated_liquidations_usd,
                    distance_pct=z.distance_pct,
                    strength=z.strength,
                ))
            await session.commit()
