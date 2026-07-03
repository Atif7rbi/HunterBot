"""Liquidation Heat Density Layer.

Reads Hunter's existing LiquidityMap and estimates where liquidation pressure
is denser.

Important:
- This is NOT a standalone signal generator.
- This does NOT decide LONG/SHORT.
- It only returns score_boost + classification for an already planned direction.

Meaning:
- zones_above = short-liquidation heat above price -> supports LONG.
- zones_below = long-liquidation heat below price  -> supports SHORT.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


Direction = Literal["LONG", "SHORT", "WAIT"]
HeatClass = Literal[
    "NO_DATA",
    "NEUTRAL",
    "LONG_HEAT",
    "SHORT_HEAT",
    "STRONG_LONG_HEAT",
    "STRONG_SHORT_HEAT",
    "CONFLICT",
]


@dataclass(frozen=True)
class HeatDensityResult:
    symbol: str
    classification: HeatClass

    above_heat_usd: float
    below_heat_usd: float
    total_heat_usd: float
    density_ratio: float

    dominant_side: str
    direction_alignment: bool
    score_boost: float

    nearest_above_price: Optional[float]
    nearest_below_price: Optional[float]
    strongest_above_price: Optional[float]
    strongest_below_price: Optional[float]

    reasoning: list[str]


class LiquidationHeatDensity:
    """Calculate heat density from LiquidityMap zones."""

    def __init__(
        self,
        *,
        max_distance_pct: float = 0.08,
        strong_ratio: float = 2.0,
        moderate_ratio: float = 1.35,
        min_total_heat_usd: float = 50_000.0,
        strong_boost: float = 8.0,
        moderate_boost: float = 4.0,
        conflict_penalty: float = -5.0,
    ) -> None:
        self.max_distance_pct = float(max_distance_pct)
        self.strong_ratio = float(strong_ratio)
        self.moderate_ratio = float(moderate_ratio)
        self.min_total_heat_usd = float(min_total_heat_usd)
        self.strong_boost = float(strong_boost)
        self.moderate_boost = float(moderate_boost)
        self.conflict_penalty = float(conflict_penalty)

    def _zone_heat(self, z) -> float:
        """Weighted heat for a liquidity zone.

        Uses existing LiquidityEngine fields:
        estimated_liquidations_usd × strength × distance_weight

        Distance is weighted again here because this layer focuses on
        executable heat density near current price.
        """
        est = float(getattr(z, "estimated_liquidations_usd", 0.0) or 0.0)
        strength = float(getattr(z, "strength", 1.0) or 1.0)
        distance = abs(float(getattr(z, "distance_pct", 0.0) or 0.0))

        if est <= 0 or distance > self.max_distance_pct:
            return 0.0

        distance_weight = max(0.0, 1.0 - (distance / self.max_distance_pct))
        return est * strength * distance_weight

    def _nearest_price(self, zones: list) -> Optional[float]:
        valid = [z for z in zones if getattr(z, "price_level", 0)]
        if not valid:
            return None
        z = min(valid, key=lambda x: abs(float(getattr(x, "distance_pct", 999.0) or 999.0)))
        return float(z.price_level)

    def _strongest_price(self, zones: list) -> Optional[float]:
        scored = [(self._zone_heat(z), z) for z in zones]
        scored = [(h, z) for h, z in scored if h > 0]
        if not scored:
            return None
        _, z = max(scored, key=lambda x: x[0])
        return float(z.price_level)

    def analyze_map(self, lmap, direction: Direction) -> HeatDensityResult:
        symbol = str(getattr(lmap, "symbol", "UNKNOWN"))
        direction_value = str(direction or "WAIT").upper()

        zones_above = list(getattr(lmap, "zones_above", []) or [])
        zones_below = list(getattr(lmap, "zones_below", []) or [])

        above_heat = sum(self._zone_heat(z) for z in zones_above)
        below_heat = sum(self._zone_heat(z) for z in zones_below)
        total = above_heat + below_heat

        nearest_above = self._nearest_price(zones_above)
        nearest_below = self._nearest_price(zones_below)
        strongest_above = self._strongest_price(zones_above)
        strongest_below = self._strongest_price(zones_below)

        if total <= 0:
            return HeatDensityResult(
                symbol=symbol,
                classification="NO_DATA",
                above_heat_usd=0.0,
                below_heat_usd=0.0,
                total_heat_usd=0.0,
                density_ratio=0.0,
                dominant_side="NONE",
                direction_alignment=False,
                score_boost=0.0,
                nearest_above_price=nearest_above,
                nearest_below_price=nearest_below,
                strongest_above_price=strongest_above,
                strongest_below_price=strongest_below,
                reasoning=["No usable liquidity heat zones."],
            )

        if total < self.min_total_heat_usd:
            return HeatDensityResult(
                symbol=symbol,
                classification="NEUTRAL",
                above_heat_usd=above_heat,
                below_heat_usd=below_heat,
                total_heat_usd=total,
                density_ratio=1.0,
                dominant_side="NONE",
                direction_alignment=False,
                score_boost=0.0,
                nearest_above_price=nearest_above,
                nearest_below_price=nearest_below,
                strongest_above_price=strongest_above,
                strongest_below_price=strongest_below,
                reasoning=[f"Total heat too low: {total:,.0f} < {self.min_total_heat_usd:,.0f}."],
            )

        bigger = max(above_heat, below_heat)
        smaller = max(min(above_heat, below_heat), 1.0)
        ratio = bigger / smaller

        dominant_side = "ABOVE" if above_heat > below_heat else "BELOW"

        # ABOVE heat = short liquidations = supports LONG
        # BELOW heat = long liquidations  = supports SHORT
        if direction_value == "LONG":
            aligned = dominant_side == "ABOVE"
        elif direction_value == "SHORT":
            aligned = dominant_side == "BELOW"
        else:
            aligned = False

        classification: HeatClass = "NEUTRAL"
        boost = 0.0

        if ratio >= self.strong_ratio:
            classification = "STRONG_LONG_HEAT" if dominant_side == "ABOVE" else "STRONG_SHORT_HEAT"
            boost = self.strong_boost if aligned else self.conflict_penalty
        elif ratio >= self.moderate_ratio:
            classification = "LONG_HEAT" if dominant_side == "ABOVE" else "SHORT_HEAT"
            boost = self.moderate_boost if aligned else self.conflict_penalty / 2
        else:
            classification = "NEUTRAL"
            boost = 0.0

        if direction_value in {"LONG", "SHORT"} and not aligned and classification != "NEUTRAL":
            classification = "CONFLICT"

        reasoning = [
            f"Above heat={above_heat:,.0f}, below heat={below_heat:,.0f}, ratio={ratio:.2f}.",
            f"Dominant side={dominant_side}; planned direction={direction_value}; aligned={aligned}.",
            f"Score boost={boost:+.1f}.",
        ]

        return HeatDensityResult(
            symbol=symbol,
            classification=classification,
            above_heat_usd=above_heat,
            below_heat_usd=below_heat,
            total_heat_usd=total,
            density_ratio=ratio,
            dominant_side=dominant_side,
            direction_alignment=aligned,
            score_boost=boost,
            nearest_above_price=nearest_above,
            nearest_below_price=nearest_below,
            strongest_above_price=strongest_above,
            strongest_below_price=strongest_below,
            reasoning=reasoning,
        )

    # Backward-compatible alias.
    def analyze(self, lmap, direction: Direction) -> HeatDensityResult:
        return self.analyze_map(lmap, direction)
