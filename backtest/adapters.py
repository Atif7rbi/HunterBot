from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import SimpleNamespace
from typing import Any

from backtest.snapshot import HunterReplaySnapshot


class ReplayMarketState(str, Enum):
    NEUTRAL = "NEUTRAL"
    CROWDED_LONG_TRAP = "CROWDED_LONG_TRAP"
    CROWDED_SHORT_TRAP = "CROWDED_SHORT_TRAP"


class ReplayMarketRegime(str, Enum):
    RANGING = "RANGING"
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"


@dataclass(slots=True)
class ReplayAdapterBundle:
    """Adapter output expected by the future DecisionEngine bridge.

    These objects are intentionally attribute-compatible instead of being
    tightly coupled to live exchange/data/database classes. This keeps
    backtest replay isolated from live trading state.
    """

    snap: Any
    lmap: Any
    pos: Any
    ctx: Any
    regime: Any
    oi_change_4h: float
    warnings: list[str] = field(default_factory=list)
    ready_for_decision_engine: bool = False


class ReplayDecisionAdapter:
    """Build DecisionEngine-compatible inputs from HunterReplaySnapshot.

    Safety note:
    This file intentionally does NOT import src.core.database or any live DB
    module. Backtest must remain independent from runtime database state.
    """

    def build(self, snapshot: HunterReplaySnapshot) -> ReplayAdapterBundle:
        warnings: list[str] = []

        snap = self.build_full_snapshot(snapshot)
        lmap = self.build_liquidity_map(snapshot, warnings)
        pos = self.build_positioning_result(snapshot, warnings)
        ctx = self.build_context_result(snapshot)
        regime = self.build_regime_result(snapshot)

        oi_change_4h = float(snapshot.oi_change_4h_pct or 0.0)

        ready = (
            snapshot.hunter_minimum_ready
            and pos.bias_direction in ("BULLISH", "BEARISH")
        )

        if not snapshot.has_liquidity:
            warnings.append("No replay liquidity zones; LiquidityMap uses neutral/fallback values.")

        if not snapshot.has_taker_flow:
            warnings.append("No replay taker flow; CVD/taker-flow checks will be unavailable.")

        return ReplayAdapterBundle(
            snap=snap,
            lmap=lmap,
            pos=pos,
            ctx=ctx,
            regime=regime,
            oi_change_4h=oi_change_4h,
            warnings=warnings,
            ready_for_decision_engine=ready,
        )

    def build_full_snapshot(self, snapshot: HunterReplaySnapshot) -> Any:
        return SimpleNamespace(
            symbol=snapshot.symbol,
            price=float(snapshot.price or snapshot.close or 0.0),
            open=snapshot.open,
            high=snapshot.high,
            low=snapshot.low,
            close=snapshot.close,
            volume=snapshot.volume,

            funding_rate=snapshot.funding_rate or 0.0,
            open_interest=snapshot.open_interest,
            open_interest_usd=snapshot.open_interest_usd,
            oi_change_4h_pct=snapshot.oi_change_4h_pct or 0.0,

            ls_ratio_global=snapshot.long_short_ratio_global,
            ls_ratio_top=snapshot.long_short_ratio_top,
            long_short_ratio=snapshot.long_short_ratio_global,

            taker_buy_volume=snapshot.taker_buy_volume,
            taker_sell_volume=snapshot.taker_sell_volume,

            spread_pct=0.0,
            timestamp=snapshot.timestamp,

            replay_index=snapshot.replay_index,
            replay_source=snapshot.replay_source,
        )

    def build_liquidity_map(
        self,
        snapshot: HunterReplaySnapshot,
        warnings: list[str],
    ) -> Any:
        zones_above = [
            self._zone_from_dict(z, side="above", price=snapshot.price)
            for z in snapshot.liquidity_zones_above
        ]
        zones_below = [
            self._zone_from_dict(z, side="below", price=snapshot.price)
            for z in snapshot.liquidity_zones_below
        ]

        if zones_above and zones_below:
            above_strength = sum(float(getattr(z, "liquidity_strength", 1.0) or 1.0) for z in zones_above)
            below_strength = sum(float(getattr(z, "liquidity_strength", 1.0) or 1.0) for z in zones_below)
            total = max(above_strength + below_strength, 1e-9)
            imbalance = (above_strength - below_strength) / total
            dominant_side = "ABOVE" if above_strength >= below_strength else "BELOW"
        elif zones_above:
            imbalance = 1.0
            dominant_side = "ABOVE"
        elif zones_below:
            imbalance = -1.0
            dominant_side = "BELOW"
        else:
            imbalance = 0.0
            dominant_side = "NEUTRAL"

        all_zones = zones_above + zones_below
        primary_target = min(
            all_zones,
            key=lambda z: abs(float(getattr(z, "distance_pct", 0.0) or 0.0)),
            default=None,
        )

        if primary_target is None:
            warnings.append("No primary liquidity target available.")

        return SimpleNamespace(
            zones_above=zones_above,
            zones_below=zones_below,
            imbalance=imbalance,
            dominant_side=dominant_side,
            primary_target=primary_target,
        )

    def build_positioning_result(
        self,
        snapshot: HunterReplaySnapshot,
        warnings: list[str],
    ) -> Any:
        funding = snapshot.funding_rate
        ls_ratio = snapshot.long_short_ratio_top or snapshot.long_short_ratio_global

        bias_direction = "NEUTRAL"
        crowding_side = "UNKNOWN"
        squeeze_type = "NONE"
        bias_strength = 0.0
        state = ReplayMarketState.NEUTRAL

        if funding is None or ls_ratio is None:
            warnings.append("Insufficient funding/LS data to derive positioning bias.")
        else:
            ls = float(ls_ratio)
            f = float(funding)

            if ls > 1.15:
                crowding_side = "LONG"
            elif ls < 0.85:
                crowding_side = "SHORT"
            else:
                crowding_side = "BALANCED"

            if f > 0 and crowding_side == "LONG":
                bias_direction = "BEARISH"
                squeeze_type = "LONG_TRAP"
                state = ReplayMarketState.CROWDED_LONG_TRAP
                bias_strength = min(1.0, abs(ls - 1.0) / 1.0 + min(abs(f) / 0.001, 1.0) * 0.25)

            elif f < 0 and crowding_side == "SHORT":
                bias_direction = "BULLISH"
                squeeze_type = "SHORT_TRAP"
                state = ReplayMarketState.CROWDED_SHORT_TRAP
                bias_strength = min(1.0, abs(1.0 - ls) / 1.0 + min(abs(f) / 0.001, 1.0) * 0.25)

            else:
                bias_direction = "NEUTRAL"
                state = ReplayMarketState.NEUTRAL
                bias_strength = 0.0

        return SimpleNamespace(
            bias_direction=bias_direction,
            bias_strength=float(bias_strength),
            state=state,

            vwap_alignment=None,
            crowding_side=crowding_side,
            squeeze_type=squeeze_type,

            vwap_distance_15m_pct=None,
            vwap_distance_1h_pct=None,
            vwap_distance_4h_pct=None,

            reasoning=[
                "Replay adapter positioning bridge.",
                f"funding={funding}",
                f"ls_ratio={ls_ratio}",
                f"bias={bias_direction}",
                f"strength={bias_strength:.2f}",
            ],
        )

    def build_context_result(self, snapshot: HunterReplaySnapshot) -> Any:
        return SimpleNamespace(
            is_recently_swept=False,
            is_extended=False,
            contextual_modifier=1.0,
            notes=[
                "Replay adapter context bridge: neutral context.",
            ],
        )

    def build_regime_result(self, snapshot: HunterReplaySnapshot) -> Any:
        return SimpleNamespace(
            regime=ReplayMarketRegime.RANGING,
            adx=0.0,
            notes=[
                "Replay adapter regime bridge: default RANGING.",
            ],
        )

    def _zone_from_dict(self, raw: dict[str, Any], *, side: str, price: float) -> Any:
        level = (
            raw.get("price_level")
            or raw.get("price")
            or raw.get("level")
            or raw.get("liquidation_price")
            or 0.0
        )
        price_level = float(level or 0.0)
        ref_price = float(price or 0.0)
        distance_pct = 0.0
        if ref_price > 0 and price_level > 0:
            distance_pct = (price_level - ref_price) / ref_price

        strength = float(
            raw.get("liquidity_strength")
            or raw.get("strength")
            or raw.get("volume_score")
            or raw.get("weight")
            or 1.0
        )

        return SimpleNamespace(
            price_level=price_level,
            distance_pct=distance_pct,
            liquidity_strength=strength,
            strength=strength,
            volume_score=strength,
            weight=strength,
            side=side,
            raw=dict(raw),
        )


def build_decision_inputs(snapshot: HunterReplaySnapshot) -> ReplayAdapterBundle:
    return ReplayDecisionAdapter().build(snapshot)
