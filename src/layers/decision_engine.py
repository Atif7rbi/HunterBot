"""Layer 7 — Decision Engine v2.9

Role:
- Receives allowed market direction from PositioningResult.bias_direction
- Applies execution vetoes only
- Computes execution-quality score
- Classifies setup into WAIT / WATCH / EXECUTABLE

Clean isolation:
- New strategy controls direction through bias_direction only.
- Funding is used ONLY inside positioning strict gate.
- No funding veto in DecisionEngine.
- Legacy state remains visible in telemetry/reasoning only.
- pos.state is NOT used to boost OI score or regime behavior.
- Trigger timing is handled later by Layer 8 in the main pipeline.

v2.8 changes:
- Removed artificial positioning score floor.
- Price action score is less inflated and more explainable.
- Added explicit weighted score breakdown to reasoning.
- WATCH_ONLY no longer collapses into unexplainable WAIT.
- Scores below WATCH_ZONE_MIN still return WAIT.
- Scores between WATCH_ZONE_MIN and min_score_to_signal keep their candidate direction
  for visibility, but size_factor remains 0.0 and is_watch_zone=True.
- Added Distance to Liquidity execution boost after regime/context adjustments.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from src.core.config import settings
from src.core.database import (
    AsyncSessionLocal,
    MarketRegime,
    PositioningBiasLog,
    WatchZone,
)
from src.core.logger import logger
from src.exchange.data_fetcher import FullSnapshot
from src.layers.context import ContextResult
from src.layers.liquidity_engine import LiquidityMap
from src.layers.positioning import PositioningResult
from src.layers.regime_detector import RegimeResult
from src.market.liquidity_distance import LiquidityDistance
from src.layers.liquidation_heat_density import LiquidationHeatDensity

WATCH_ZONE_MIN = 45.0


@dataclass
class VetoResult:
    rejected: bool
    reason: str = ""


@dataclass
class DecisionResult:
    symbol: str
    score: float
    raw_score: float
    direction: str
    size_factor: float
    components: dict[str, float] = field(default_factory=dict)
    reasoning: list[str] = field(default_factory=list)
    is_watch_zone: bool = False
    veto_reason: str = ""
    confirmation: bool = False


class DecisionEngine:
    def __init__(self) -> None:
        self.cfg = settings.decision_engine
        self.weights = self.cfg["weights"]
        self.heat_density = LiquidationHeatDensity()

    def _veto_check(
        self,
        snap: FullSnapshot,
        lmap: LiquidityMap,
        pos: PositioningResult,
        regime: RegimeResult,
        direction: str,
    ) -> VetoResult:
        cfg_v = self.cfg.get("veto", {})

        max_spread = cfg_v.get("max_spread_pct", 0.001)
        current_spread = getattr(snap, "spread_pct", 0.0) or 0.0
        if current_spread > max_spread:
            return VetoResult(
                rejected=True,
                reason=(
                    "EXECUTION_VETO: spread "
                    f"{current_spread*100:.3f}% > max {max_spread*100:.3f}%"
                ),
            )

        already_ran_pct = cfg_v.get("already_ran_pct", 0.015)
        if lmap.primary_target:
            if (
                direction == "SHORT"
                and lmap.primary_target.distance_pct < 0
                and abs(lmap.primary_target.distance_pct) > already_ran_pct
            ):
                return VetoResult(
                    rejected=True,
                    reason=(
                        "EXECUTION_VETO: price already ran "
                        f"{abs(lmap.primary_target.distance_pct)*100:.1f}% past target (SHORT)"
                    ),
                )

            if (
                direction == "LONG"
                and lmap.primary_target.distance_pct > 0
                and abs(lmap.primary_target.distance_pct) > already_ran_pct
            ):
                return VetoResult(
                    rejected=True,
                    reason=(
                        "EXECUTION_VETO: price already ran "
                        f"{abs(lmap.primary_target.distance_pct)*100:.1f}% past target (LONG)"
                    ),
                )

        return VetoResult(rejected=False)

    def _liquidity_imbalance_score(self, lmap: LiquidityMap) -> float:
        return min(abs(lmap.imbalance) * 100, 100.0)

    def _positioning_extremity_score(self, pos: PositioningResult) -> float:
        if getattr(pos, "bias_direction", "NEUTRAL") == "NEUTRAL":
            return 0.0

        authority_score = float(getattr(pos, "authority_score", 0.0) or 0.0)
        if authority_score > 0:
            return max(0.0, min(authority_score, 100.0))

        strength = float(getattr(pos, "bias_strength", 0.0) or 0.0)
        return max(0.0, min(strength * 100.0, 100.0))

    def _oi_behavior_score(
        self,
        snap: FullSnapshot,
        oi_change_4h: float,
        pos: PositioningResult,
    ) -> float:
        if getattr(pos, "bias_direction", "NEUTRAL") == "NEUTRAL":
            return 0.0

        magnitude = min(abs(oi_change_4h) / 0.15, 1.0) * 100
        return magnitude

    def _price_action_score(self, ctx: ContextResult, lmap: LiquidityMap) -> float:
        score = 0.0

        if ctx.is_recently_swept:
            score += 40

        if ctx.is_extended:
            score += 25

        if lmap.primary_target and abs(lmap.primary_target.distance_pct) < 0.015:
            score += 20

        return min(score, 100.0)

    def _determine_direction(self, pos: PositioningResult) -> str:
        if pos.bias_direction == "BULLISH":
            return "LONG"
        if pos.bias_direction == "BEARISH":
            return "SHORT"
        return "WAIT"

    def _apply_regime_adjustment(
        self,
        raw_score: float,
        direction: str,
        pos: PositioningResult,
        regime: RegimeResult,
    ) -> tuple[float, list[str]]:
        notes: list[str] = []
        is_reversal = pos.bias_direction in ("BULLISH", "BEARISH")

        score = raw_score

        if regime.regime == MarketRegime.TRENDING_UP and direction == "SHORT" and is_reversal:
            p = self.cfg["trending_market_penalty_on_reversal"]
            score *= (1 - p)
            notes.append(f"-{p*100:.0f}% (counter-trend in uptrend)")

        elif regime.regime == MarketRegime.TRENDING_DOWN and direction == "LONG" and is_reversal:
            p = self.cfg["trending_market_penalty_on_reversal"]
            score *= (1 - p)
            notes.append(f"-{p*100:.0f}% (counter-trend in downtrend)")

        elif regime.regime == MarketRegime.RANGING and is_reversal:
            b = self.cfg["range_market_bonus_on_reversal"]
            score *= (1 + b)
            notes.append(f"+{b*100:.0f}% (reversal play in range)")

        return min(score, 100.0), notes

    def _score_breakdown_notes(self, comp: dict[str, float]) -> list[str]:
        notes: list[str] = ["---- SCORE BREAKDOWN ----"]

        weighted_total = 0.0
        for key, value in comp.items():
            weight = float(self.weights.get(key, 0.0))
            contribution = value * weight
            weighted_total += contribution
            notes.append(
                f"{key}: raw={value:.1f}, weight={weight:.2f}, contribution={contribution:.1f}"
            )

        notes.append(f"Raw weighted score={weighted_total:.1f}")
        return notes

    def _resolve_match_state(self, bot_direction: str, pos: PositioningResult) -> str:
        pos_dir = getattr(pos, "bias_direction", "NEUTRAL") or "NEUTRAL"
        if bot_direction == "WAIT" or pos_dir == "NEUTRAL":
            return "NEUTRAL"
        mapped_dir = "LONG" if pos_dir == "BULLISH" else "SHORT"
        return "MATCH" if mapped_dir == bot_direction else "CONFLICT"

    def _resolve_action_taken(self, result: DecisionResult) -> str:
        if result.veto_reason:
            return "IGNORE"
        if result.is_watch_zone:
            return "WATCH_ONLY"
        if result.direction == "WAIT":
            return "IGNORE"
        return "EXECUTE"

    async def _save_positioning_bias_log(
        self,
        result: DecisionResult,
        snap: FullSnapshot,
        pos: PositioningResult,
        regime: RegimeResult,
        oi_change_4h: float,
        watch_zone_id: int | None = None,
        trade_id: int | None = None,
    ) -> None:
        try:
            bot_direction = result.direction if result.direction in ("LONG", "SHORT") else "WAIT"
            async with AsyncSessionLocal() as s:
                s.add(
                    PositioningBiasLog(
                        symbol=result.symbol,
                        bot_direction=bot_direction,
                        positioning_bias=getattr(pos, "bias_direction", "NEUTRAL"),
                        bias_strength=float(getattr(pos, "bias_strength", 0.0) or 0.0),
                        vwap_alignment=getattr(pos, "vwap_alignment", None),
                        crowding_side=getattr(pos, "crowding_side", None),
                        squeeze_type=getattr(pos, "squeeze_type", None),
                        market_state=pos.state.value if getattr(pos, "state", None) else None,
                        market_regime=regime.regime.value if getattr(regime, "regime", None) else None,
                        decision_score=result.score,
                        raw_score=result.raw_score,
                        match_state=self._resolve_match_state(bot_direction, pos),
                        action_taken=self._resolve_action_taken(result),
                        funding_rate=getattr(snap, "funding_rate", None),
                        ls_ratio_global=getattr(snap, "ls_ratio_global", None),
                        ls_ratio_top=getattr(snap, "ls_ratio_top", None),
                        oi_change_4h_pct=oi_change_4h,
                        vwap_distance_15m_pct=getattr(pos, "vwap_distance_15m_pct", None),
                        vwap_distance_1h_pct=getattr(pos, "vwap_distance_1h_pct", None),
                        vwap_distance_4h_pct=getattr(pos, "vwap_distance_4h_pct", None),
                        reasoning={"items": list(result.reasoning)},
                        watch_zone_id=watch_zone_id,
                        trade_id=trade_id,
                    )
                )
                await s.commit()
        except Exception as e:
            logger.warning(f"PositioningBiasLog persist failed for {result.symbol}: {e}")

    async def _save_watch_zone(
        self,
        result: DecisionResult,
        snap: FullSnapshot,
        regime_str: str,
    ) -> int | None:
        try:
            async with AsyncSessionLocal() as s:
                watch = WatchZone(
                    symbol=result.symbol,
                    score=result.score,
                    direction=result.direction,
                    market_state=result.components.get("_state", "UNKNOWN"),
                    regime=regime_str,
                    funding_rate=snap.funding_rate,
                    components={
                        k: v for k, v in result.components.items() if not k.startswith("_")
                    },
                )
                s.add(watch)
                await s.commit()
                await s.refresh(watch)
                return watch.id
        except Exception as e:
            logger.warning(f"WatchZone persist failed for {result.symbol}: {e}")
            return None

    async def _persist_decision_context(
        self,
        result: DecisionResult,
        snap: FullSnapshot,
        pos: PositioningResult,
        regime: RegimeResult,
        oi_change_4h: float,
        should_save_watch_zone: bool,
    ) -> None:
        watch_zone_id: int | None = None
        if should_save_watch_zone:
            watch_zone_id = await self._save_watch_zone(result, snap, regime.regime.value)

        await self._save_positioning_bias_log(
            result=result,
            snap=snap,
            pos=pos,
            regime=regime,
            oi_change_4h=oi_change_4h,
            watch_zone_id=watch_zone_id,
            trade_id=None,
        )

    def _schedule_persistence(
        self,
        result: DecisionResult,
        snap: FullSnapshot,
        pos: PositioningResult,
        regime: RegimeResult,
        oi_change_4h: float,
        should_save_watch_zone: bool,
    ) -> None:
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(
                self._persist_decision_context(
                    result=result,
                    snap=snap,
                    pos=pos,
                    regime=regime,
                    oi_change_4h=oi_change_4h,
                    should_save_watch_zone=should_save_watch_zone,
                )
            )
        except RuntimeError:
            pass

    def evaluate(
        self,
        snap: FullSnapshot,
        lmap: LiquidityMap,
        pos: PositioningResult,
        ctx: ContextResult,
        regime: RegimeResult,
        oi_change_4h: float,
    ) -> DecisionResult:
        direction = self._determine_direction(pos)

        if direction == "WAIT":
            reasoning = [
                f"Positioning gate direction=WAIT (bias={pos.bias_direction}, state={pos.state.value})",
                f"Regime: {regime.regime.value} (ADX={regime.adx:.1f})",
                f"OI 4h: {oi_change_4h*100:+.1f}%",
                *ctx.notes,
            ]
            if getattr(pos, "reasoning", None):
                reasoning.extend(pos.reasoning)

            result = DecisionResult(
                symbol=snap.symbol,
                score=0.0,
                raw_score=0.0,
                direction="WAIT",
                size_factor=0.0,
                components={},
                reasoning=reasoning,
                is_watch_zone=False,
                confirmation=False,
            )
            self._schedule_persistence(
                result=result,
                snap=snap,
                pos=pos,
                regime=regime,
                oi_change_4h=oi_change_4h,
                should_save_watch_zone=False,
            )
            return result

        veto = self._veto_check(snap, lmap, pos, regime, direction)
        if veto.rejected:
            logger.info(f"🚫 VETO {snap.symbol}: {veto.reason}")
            result = DecisionResult(
                symbol=snap.symbol,
                score=0.0,
                raw_score=0.0,
                direction="WAIT",
                size_factor=0.0,
                reasoning=[veto.reason],
                veto_reason=veto.reason,
                confirmation=False,
            )
            self._schedule_persistence(
                result=result,
                snap=snap,
                pos=pos,
                regime=regime,
                oi_change_4h=oi_change_4h,
                should_save_watch_zone=False,
            )
            return result

        distance = LiquidityDistance.evaluate(lmap)
        heat = self.heat_density.analyze(lmap, direction)

        comp = {
            "liquidity_imbalance": self._liquidity_imbalance_score(lmap),
            "positioning_extremity": self._positioning_extremity_score(pos),
            "oi_behavior": self._oi_behavior_score(snap, oi_change_4h, pos),
            "price_action_confluence": self._price_action_score(ctx, lmap),
        }

        raw_score = sum(comp[k] * self.weights[k] for k in comp)

        distance_level = distance.get("nearest_level")
        distance_pct = distance.get("distance_pct")
        distance_score = distance.get("distance_score", 0)
        distance_quality = distance.get("execution_quality", "UNKNOWN")

        distance_level_text = (
            f"{distance_level}"
            if distance_level is not None
            else "None"
        )
        distance_pct_text = (
            f"{distance_pct:.2f}%"
            if isinstance(distance_pct, (int, float))
            else "None"
        )

        reasoning = [
            f"Liquidity dominant: {lmap.dominant_side} (imb={lmap.imbalance:+.2f})",
            (
                "Liquidity Distance: "
                f"{distance_quality} "
                f"(distance={distance_pct_text}, score=+{distance_score})"
            ),
            f"Nearest liquidity: {distance_level_text}",
            f"Positioning: {pos.state.value} ({pos.bias_direction}, strength={pos.bias_strength:.2f})",
            (
                "Authority Scores: "
                f"total={float(getattr(pos, 'authority_score', 0.0) or 0.0):.1f}, "
                f"funding={float(getattr(pos, 'funding_score', 0.0) or 0.0):.1f}, "
                f"ls_pos={float(getattr(pos, 'ls_position_score', 0.0) or 0.0):.1f}, "
                f"oi={float(getattr(pos, 'oi_score', 0.0) or 0.0):.1f}, "
                f"vwap={float(getattr(pos, 'vwap_score', 0.0) or 0.0):.1f}, "
                f"ls_account={float(getattr(pos, 'ls_account_score', 0.0) or 0.0):.1f}"
            ),
            f"Regime: {regime.regime.value} (ADX={regime.adx:.1f})",
            f"OI 4h: {oi_change_4h*100:+.1f}%",
            *ctx.notes,
        ]

        reasoning.extend(self._score_breakdown_notes(comp))

        if getattr(pos, "reasoning", None):
            reasoning.extend(pos.reasoning)

        score, regime_notes = self._apply_regime_adjustment(
            raw_score,
            direction,
            pos,
            regime,
        )
        reasoning.extend(regime_notes)

        if ctx.contextual_modifier != 1.0:
            reasoning.append(f"Context modifier: ×{ctx.contextual_modifier:.2f}")
            score *= ctx.contextual_modifier

        distance_cfg = settings.section("liquidity_distance")
        boost_multiplier = float(
            distance_cfg.get("execution_boost_multiplier", 0.50)
        )
        distance_boost = float(distance_score or 0.0) * boost_multiplier

        if distance_boost > 0:
            score += distance_boost
            reasoning.append(
                f"Liquidity execution boost: +{distance_boost:.1f} "
                f"(distance score {float(distance_score or 0.0):.1f} × {boost_multiplier:.2f})"
            )

        heat_boost = float(getattr(heat, "score_boost", 0.0) or 0.0)
        if heat_boost != 0:
            score += heat_boost
            reasoning.append(
                f"Heat Density: {heat.classification} "
                f"(ratio={heat.density_ratio:.2f}, boost={heat_boost:+.1f})"
            )
        else:
            reasoning.append(
                f"Heat Density: {heat.classification} "
                f"(ratio={heat.density_ratio:.2f}, boost=+0.0)"
            )

        ls_account_boost = float(getattr(pos, "ls_account_score", 0.0) or 0.0)
        if ls_account_boost > 0:
            score += ls_account_boost
            reasoning.append(f"LS Account execution bonus: +{ls_account_boost:.1f}")

        score = min(score, 100.0)

        min_signal = self.cfg["min_score_to_signal"]
        min_full = self.cfg["min_score_full_size"]
        is_watch_zone = False
        should_save_watch_zone = False
        size_factor: float

        if score < WATCH_ZONE_MIN:
            reasoning.append(
                f"Decision: WAIT because score {score:.1f} < watch minimum {WATCH_ZONE_MIN:.1f}"
            )
            size_factor = 0.0
            direction = "WAIT"

        elif score < min_signal:
            reasoning.append(
                f"Decision: WATCH_ONLY because score {score:.1f} < signal threshold {min_signal:.1f}"
            )
            is_watch_zone = True
            should_save_watch_zone = True
            comp["_state"] = pos.state.value
            size_factor = 0.0

        elif score < min_full:
            reasoning.append(
                f"Decision: EXECUTE half size because {min_signal:.1f} <= score {score:.1f} < full size {min_full:.1f}"
            )
            size_factor = 0.5

        else:
            reasoning.append(
                f"Decision: EXECUTE full size because score {score:.1f} >= full size {min_full:.1f}"
            )
            size_factor = 1.0

        result_components = dict(comp)
        result_components.update({
            "authority_score": float(getattr(pos, "authority_score", 0.0) or 0.0),
            "funding_score": float(getattr(pos, "funding_score", 0.0) or 0.0),
            "ls_position_score": float(getattr(pos, "ls_position_score", 0.0) or 0.0),
            "oi_score": float(getattr(pos, "oi_score", 0.0) or 0.0),
            "vwap_score": float(getattr(pos, "vwap_score", 0.0) or 0.0),
            "ls_account_score": float(getattr(pos, "ls_account_score", 0.0) or 0.0),
        })
        result_components.pop("_state", None)

        result = DecisionResult(
            symbol=snap.symbol,
            score=score,
            raw_score=raw_score,
            direction=direction,
            size_factor=size_factor,
            components=result_components,
            reasoning=reasoning,
            is_watch_zone=is_watch_zone,
            confirmation=False,
        )

        should_log_bias = (
            result.is_watch_zone
            or result.direction in ("LONG", "SHORT")
            or bool(result.veto_reason)
        )

        if should_log_bias:
            self._schedule_persistence(
                result=result,
                snap=snap,
                pos=pos,
                regime=regime,
                oi_change_4h=oi_change_4h,
                should_save_watch_zone=should_save_watch_zone,
            )

        return result