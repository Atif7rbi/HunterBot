from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import func, select

from src.core.config import settings
from src.core.database import (
    AsyncSessionLocal,
    MarketRegime,
    MarketState,
    RejectedSignal,
    RejectionCategory,
    Trade,
    TradeStatus,
)

from src.core.logger import logger
from src.exchange.data_fetcher import FullSnapshot
from src.layers.decision_engine import DecisionResult
from src.layers.liquidity_engine import LiquidityMap, Zone
from src.learning.shadow_trade_tracker import ShadowTradeTracker


# All tunable TradeGenerator thresholds are loaded from config.yaml.
# Defaults below are only safety fallbacks if a key is missing.
DEFAULT_MAX_OPEN_PER_SYMBOL: int = 1
DEFAULT_MIN_DECISION_SCORE: float = 60.0
DEFAULT_MIN_RISK_REWARD: float = 1.7
DEFAULT_RR_SOFT_FLOOR: float = 1.5
DEFAULT_RR_HIGH_SCORE_THRESHOLD: float = 85.0
DEFAULT_MIN_SL_DISTANCE_PCT: float = 0.004
DEFAULT_MIN_ZONE_DISTANCE_PCT: float = 0.003
DEFAULT_MAX_ZONE_DISTANCE_PCT: float = 0.04
DEFAULT_REJECTION_DEDUP_WINDOW_SEC: int = 300
DEFAULT_MAX_SL_DISTANCE_PCT: float = 0.07
DEFAULT_MIN_EXECUTION_QUALITY_SCORE: float = 55.0
DEFAULT_BREATHING_BUFFER_MULTIPLIER: float = 0.50


@dataclass
class TradeCard:
    symbol: str
    direction: str
    setup_score: float
    market_state: MarketState
    market_regime: MarketRegime
    trigger_description: str
    entry_zone_low: float
    entry_zone_high: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    risk_reward: float
    invalidation_condition: str
    estimated_success: float
    size_factor: float
    reasoning: list[str]
    decision_snapshot: dict | None
    created_at: datetime


class TradeGenerator:
    def __init__(self) -> None:
        self.cfg = settings.trade_generator

        # Config-driven execution filters.
        # These preserve the existing runtime defaults if config.yaml is missing a key.
        self.max_open_per_symbol = int(
            self.cfg.get("max_open_per_symbol", DEFAULT_MAX_OPEN_PER_SYMBOL)
        )
        self.min_decision_score = float(
            self.cfg.get("min_decision_score", DEFAULT_MIN_DECISION_SCORE)
        )
        self.min_risk_reward = float(
            self.cfg.get("min_risk_reward_ratio", DEFAULT_MIN_RISK_REWARD)
        )
        self.rr_soft_floor = float(
            self.cfg.get("rr_soft_floor", DEFAULT_RR_SOFT_FLOOR)
        )
        self.rr_high_score_threshold = float(
            self.cfg.get("rr_high_score_threshold", DEFAULT_RR_HIGH_SCORE_THRESHOLD)
        )
        self.min_sl_distance_pct = float(
            self.cfg.get("min_sl_distance_pct", DEFAULT_MIN_SL_DISTANCE_PCT)
        )
        self.min_zone_distance_pct = float(
            self.cfg.get("min_zone_distance_pct", DEFAULT_MIN_ZONE_DISTANCE_PCT)
        )
        self.max_zone_distance_pct = float(
            self.cfg.get("max_zone_distance_pct", DEFAULT_MAX_ZONE_DISTANCE_PCT)
        )
        self.rejection_dedup_window_sec = int(
            self.cfg.get("rejection_dedup_window_sec", DEFAULT_REJECTION_DEDUP_WINDOW_SEC)
        )
        self.max_sl_distance_pct = float(
            self.cfg.get("max_sl_distance_pct", DEFAULT_MAX_SL_DISTANCE_PCT)
        )
        self.min_execution_quality_score = float(
            self.cfg.get("min_execution_quality_score", DEFAULT_MIN_EXECUTION_QUALITY_SCORE)
        )
        self.breathing_buffer_multiplier = float(
            self.cfg.get("breathing_buffer_multiplier", DEFAULT_BREATHING_BUFFER_MULTIPLIER)
        )

    def _zone_strength(self, z: Zone) -> float:
        return (
            getattr(z, "liquidity_strength", None)
            or getattr(z, "strength", None)
            or getattr(z, "volume_score", None)
            or getattr(z, "weight", None)
            or 1.0
        )

    def _score_zone(self, z: Zone) -> float:
        strength = self._zone_strength(z)
        distance_penalty = abs(z.distance_pct) * 4.0
        return strength - distance_penalty

    def _select_best_zone(self, candidates: list[Zone]) -> Zone | None:
        if not candidates:
            return None
        return max(candidates, key=self._score_zone)

    async def _log_rejected_signal(
            self,
            *,
            symbol: str,
            direction: str,
            setup_score: float,
            market_state: MarketState | None,
            market_regime: MarketRegime | None,
            category: RejectionCategory,
            rejection_reason: str,
            rejection_details: str | None = None,
    ) -> None:
        """Insert or update a rejected signal.

        Upsert identity:
            symbol + direction + category + rejection_reason

        This keeps the Rejected Signals table clean. For example:
            RAVEUSDT + SHORT + Risk + sl_too_wide

        stays one live row that gets updated every time the same rejection
        happens again, instead of creating duplicate rows.
        """
        if (setup_score or 0.0) < 70:
            return

        try:
            async with AsyncSessionLocal() as s:
                now = datetime.now(UTC)

                symbol_value = str(symbol or "").upper()
                direction_value = str(direction or "")
                category_value = getattr(category, "value", category)

                existing_row = await s.scalar(
                    select(RejectedSignal).where(
                        RejectedSignal.symbol == symbol_value,
                        RejectedSignal.direction == direction_value,
                        RejectedSignal.category == category_value,
                        RejectedSignal.rejection_reason == rejection_reason,
                    ).order_by(
                        RejectedSignal.created_at.desc(),
                        RejectedSignal.id.desc(),
                    )
                )

                if existing_row is not None:
                    existing_row.setup_score = float(setup_score or 0.0)
                    existing_row.market_state = getattr(market_state, "value", market_state)
                    existing_row.market_regime = getattr(market_regime, "value", market_regime)
                    existing_row.category = category_value
                    existing_row.rejection_reason = rejection_reason
                    existing_row.rejection_details = rejection_details
                    existing_row.created_at = now
                    await s.commit()
                    return

                row = RejectedSignal(
                    symbol=symbol_value,
                    direction=direction_value,
                    setup_score=float(setup_score or 0.0),
                    market_state=getattr(market_state, "value", market_state),
                    market_regime=getattr(market_regime, "value", market_regime),
                    category=category_value,
                    rejection_reason=rejection_reason,
                    rejection_details=rejection_details,
                    created_at=now,
                )
                s.add(row)
                await s.commit()

        except Exception as e:
            logger.exception(f"failed to log rejected signal for {symbol}: {e}")

    async def _is_overexposed(
            self,
            symbol: str,
            setup_score: float = 0.0,
            market_state: MarketState | None = None,
            market_regime: MarketRegime | None = None,
            direction: str = "",
    ) -> bool:
        async with AsyncSessionLocal() as s:
            statement = select(func.count()).where(
                Trade.symbol == symbol,
                Trade.status.in_([TradeStatus.PENDING, TradeStatus.TRIGGERED]),
            )
            count = await s.scalar(statement)

        if (count or 0) >= self.max_open_per_symbol:
            logger.info(f"⛔ {symbol} overexposed")
            await self._log_rejected_signal(
                symbol=symbol,
                direction=direction,
                setup_score=setup_score,
                market_state=market_state,
                market_regime=market_regime,
                category=RejectionCategory.POSITION_LIMIT,
                rejection_reason="active_trade_exists",
                rejection_details=f"symbol already has {(count or 0)} active PENDING/TRIGGERED trade",
            )
            return True

        return False

    def _find_invalidation(
            self,
            lmap: LiquidityMap,
            direction: str,
            entry_reference: float,
    ) -> float:
        if direction == "SHORT":
            candidates = [z for z in lmap.zones_above if z.price_level > entry_reference]
        else:
            candidates = [z for z in lmap.zones_below if z.price_level < entry_reference]

        if not candidates:
            return entry_reference * (1.015 if direction == "SHORT" else 0.985)

        structural = [z for z in candidates if abs(z.distance_pct) <= 0.03]
        pool = structural if structural else candidates

        best = max(pool, key=self._zone_strength)
        return best.price_level

    def _find_targets(
            self,
            lmap: LiquidityMap,
            direction: str,
            price: float,
            entry_reference: float,
    ) -> tuple[float, float, float]:
        raw_zones = lmap.zones_below if direction == "SHORT" else lmap.zones_above

        if direction == "SHORT":
            candidates = [z for z in raw_zones if z.price_level < entry_reference]
        else:
            candidates = [z for z in raw_zones if z.price_level > entry_reference]

        structural = [z for z in candidates if abs(z.distance_pct) <= 0.08]
        pool = structural if structural else candidates

        # Targets must be ordered by execution distance from entry:
        #   LONG : Entry < TP1 < TP2 < TP3
        #   SHORT: Entry > TP1 > TP2 > TP3
        #
        # Liquidity strength is useful for choosing good zones, but TP labels
        # should always represent nearest -> farthest target. Otherwise a far
        # liquidity pool can incorrectly become TP1 while a closer target becomes
        # TP2, which breaks trade monitoring and trailing/TP sequencing.
        pool = sorted(
            pool,
            key=lambda z: abs(z.price_level - entry_reference),
        )

        targets = [z.price_level for z in pool[:3]]

        while len(targets) < 3:
            n = len(targets) + 1
            fallback = (
                price * (1 - 0.01 * n)
                if direction == "SHORT"
                else price * (1 + 0.01 * n)
            )
            targets.append(fallback)

        return targets[0], targets[1], targets[2]



    def _calc_execution_quality(
            self,
            *,
            rr: float,
            sl_distance_pct: float,
            entry_distance_pct: float,
            zone_span_pct: float,
            decision_score: float,
    ) -> tuple[float, list[str]]:
        """Score execution quality separately from decision quality.

        Decision score answers:
            "Is the setup idea strong?"

        Execution quality answers:
            "Is this a good place to execute the idea?"

        Conservative v1:
        - Does not change direction.
        - Does not lower RR requirements.
        - Only rejects extremely poor execution quality.
        """
        score = 100.0
        reasons: list[str] = []

        # RR penalty
        if rr < 0.50:
            score -= 45.0
            reasons.append(f"RR critically low ({rr:.2f})")
        elif rr < 1.00:
            score -= 30.0
            reasons.append(f"RR poor ({rr:.2f})")
        elif rr < self.rr_soft_floor:
            score -= 15.0
            reasons.append(f"RR below soft floor ({rr:.2f} < {self.rr_soft_floor:.2f})")

        # SL distance penalty
        if sl_distance_pct > self.max_sl_distance_pct * 2.0:
            score -= 45.0
            reasons.append(f"SL extremely wide ({sl_distance_pct*100:.1f}%)")
        elif sl_distance_pct > self.max_sl_distance_pct:
            score -= 30.0
            reasons.append(f"SL wide ({sl_distance_pct*100:.1f}% > {self.max_sl_distance_pct*100:.1f}%)")
        elif sl_distance_pct > self.max_sl_distance_pct * 0.75:
            score -= 12.0
            reasons.append(f"SL moderately wide ({sl_distance_pct*100:.1f}%)")

        # Entry distance penalty
        if entry_distance_pct > self.max_zone_distance_pct:
            score -= 20.0
            reasons.append(f"entry far from zone ({entry_distance_pct*100:.2f}%)")
        elif entry_distance_pct > self.max_zone_distance_pct * 0.50:
            score -= 8.0
            reasons.append(f"entry not ideal ({entry_distance_pct*100:.2f}%)")

        # Wide entry zone penalty
        if zone_span_pct > 0.015:
            score -= 8.0
            reasons.append(f"entry zone wide ({zone_span_pct*100:.2f}%)")

        # High conviction gets small forgiveness, not a free pass.
        if decision_score >= 90.0:
            score += 5.0
            reasons.append("high decision score cushion (+5)")

        score = max(0.0, min(100.0, score))
        return score, reasons

    def _adaptive_stop_buffer(
            self,
            *,
            base_buffer: float,
            zone_span_pct: float,
            distance_pct: float,
    ) -> float:
        """Add small breathing space based on execution geometry.

        This is intentionally conservative. It adds a tiny buffer when the
        entry zone is wider or the price is not perfectly inside the zone.
        """
        adaptive = (
            abs(zone_span_pct) * self.breathing_buffer_multiplier
            + abs(distance_pct) * 0.25
        )
        return max(base_buffer, base_buffer + adaptive)


    def _extract_heat_density(self, reasoning: list[str]) -> dict:
        """Extract Heat Density telemetry from decision reasoning."""
        heat = {
            "class": None,
            "ratio": None,
            "boost": 0.0,
            "raw": None,
        }

        for line in reasoning or []:
            s = str(line or "").strip()
            if not s.startswith("Heat Density:"):
                continue

            heat["raw"] = s

            try:
                after = s.split("Heat Density:", 1)[1].strip()
                cls = after.split("(", 1)[0].strip()
                if cls:
                    heat["class"] = cls

                if "ratio=" in s:
                    ratio_part = s.split("ratio=", 1)[1].split(",", 1)[0].split(")", 1)[0]
                    heat["ratio"] = float(ratio_part.strip())

                if "boost=" in s:
                    boost_part = s.split("boost=", 1)[1].split(")", 1)[0].split(",", 1)[0]
                    heat["boost"] = float(boost_part.strip())

            except Exception:
                pass

            break

        return heat

    def _build_decision_snapshot(
            self,
            *,
            snap: FullSnapshot,
            lmap: LiquidityMap,
            decision: DecisionResult,
            market_state: MarketState,
            market_regime: MarketRegime,
            direction: str,
            entry_mid: float,
            sl: float,
            tp1: float,
            tp2: float,
            tp3: float,
            rr: float,
            risk: float,
    ) -> dict:
        """Build immutable decision snapshot saved with future trades.

        This is UI/analytics metadata only.
        It does not affect trade execution.
        """
        reasoning = list(decision.reasoning or [])
        heat = self._extract_heat_density(reasoning)

        return {
            "version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "symbol": snap.symbol,
            "direction": direction,
            "score": float(decision.score or 0.0),
            "raw_score": float(decision.raw_score or 0.0),
            "size_factor": float(decision.size_factor or 0.0),
            "components": dict(decision.components or {}),
            "market": {
                "state": getattr(market_state, "value", market_state),
                "regime": getattr(market_regime, "value", market_regime),
            },
            "positioning": {
                "funding_rate": float(getattr(snap, "funding_rate", 0.0) or 0.0),
                "ls_ratio_global": float(getattr(snap, "ls_ratio_global", 0.0) or 0.0),
                "ls_ratio_top": float(getattr(snap, "ls_ratio_top", 0.0) or 0.0),
                "open_interest_usd": float(getattr(snap, "open_interest_usd", 0.0) or 0.0),
            },
            "liquidity": {
                "imbalance": float(getattr(lmap, "imbalance", 0.0) or 0.0),
                "dominant_side": str(getattr(lmap, "dominant_side", "") or ""),
                "primary_target": (
                    float(lmap.primary_target.price_level)
                    if getattr(lmap, "primary_target", None)
                    else None
                ),
            },
            "heat": heat,
            "risk_plan": {
                "entry_mid": float(entry_mid or 0.0),
                "stop_loss": float(sl or 0.0),
                "tp1": float(tp1 or 0.0),
                "tp2": float(tp2 or 0.0),
                "tp3": float(tp3 or 0.0),
                "risk": float(risk or 0.0),
                "risk_reward": float(rr or 0.0),
            },
            "reasoning": reasoning,
        }


    async def generate(
            self,
            snap: FullSnapshot,
            lmap: LiquidityMap,
            decision: DecisionResult,
            market_state: MarketState,
            market_regime: MarketRegime,
    ) -> Optional[TradeCard]:
        if decision.direction == "WAIT":
            return None

        if decision.score < self.min_decision_score:
            logger.info(
                f"⛔ SKIP {snap.symbol} — score too low ({decision.score:.1f})"
            )
            return None

        if await self._is_overexposed(
                snap.symbol,
                setup_score=decision.score,
                market_state=market_state,
                market_regime=market_regime,
                direction=decision.direction,
        ):
            return None

        direction = decision.direction
        price = snap.price

        zones = lmap.zones_above if direction == "SHORT" else lmap.zones_below

        filtered_zones = [
            z for z in zones
            if self.min_zone_distance_pct <= abs(z.distance_pct) <= self.max_zone_distance_pct
        ]

        if not filtered_zones and zones:
            filtered_zones = sorted(zones, key=lambda z: abs(z.distance_pct))[:1]

        best_zone = self._select_best_zone(filtered_zones)

        if best_zone:
            sweep_target = best_zone.price_level
        else:
            sweep_target = price * (1.005 if direction == "SHORT" else 0.995)

        zone_width = self.cfg["entry_zone_width_pct"]

        if direction == "SHORT":
            entry_high = sweep_target
            entry_low = sweep_target * (1 - zone_width)
        else:
            entry_low = sweep_target
            entry_high = sweep_target * (1 + zone_width)

        entry_mid = (entry_low + entry_high) / 2

        zone_span_pct = abs(entry_high - entry_low) / entry_mid if entry_mid else 0.0
        max_distance = float(self.cfg.get("max_entry_distance_pct", 0.02) or 0.02)

        if direction == "LONG":
            if price > entry_high:
                distance_pct = (price - entry_high) / entry_high
            elif price < entry_low:
                distance_pct = (entry_low - price) / entry_low
            else:
                distance_pct = 0.0
        else:
            if price < entry_low:
                distance_pct = (entry_low - price) / entry_low
            elif price > entry_high:
                distance_pct = (price - entry_high) / entry_high
            else:
                distance_pct = 0.0

        max_distance = max(max_distance, zone_span_pct * 1.25)

        if distance_pct > max_distance:
            logger.info(
                f"⛔ SKIP {snap.symbol} — entry too far "
                f"(dist={distance_pct:.3f} > {max_distance:.3f})"
            )
            await self._log_rejected_signal(
                symbol=snap.symbol,
                direction=direction,
                setup_score=decision.score,
                market_state=market_state,
                market_regime=market_regime,
                category=RejectionCategory.EXECUTION,
                rejection_reason="entry_too_far",
                rejection_details=f"dist={distance_pct:.6f} > max={max_distance:.6f}",
            )
            return None

        invalidation = self._find_invalidation(lmap, direction, sweep_target)
        base_buffer = float(self.cfg["stop_buffer_pct"])
        buffer = self._adaptive_stop_buffer(
            base_buffer=base_buffer,
            zone_span_pct=zone_span_pct,
            distance_pct=distance_pct,
        )

        if direction == "SHORT":
            sl = invalidation * (1 + buffer)
            risk = sl - entry_mid
        else:
            sl = invalidation * (1 - buffer)
            risk = entry_mid - sl

        if risk <= 0:
            logger.info(f"⛔ SKIP {snap.symbol} — invalid risk ({risk:.6f})")
            await self._log_rejected_signal(
                symbol=snap.symbol,
                direction=direction,
                setup_score=decision.score,
                market_state=market_state,
                market_regime=market_regime,
                category=RejectionCategory.RISK,
                rejection_reason="invalid_risk",
                rejection_details=f"risk={risk:.10f}",
            )
            return None

        tp1, tp2, tp3 = self._find_targets(lmap, direction, price, entry_mid)

        best_tp = max(
            [tp1, tp2, tp3],
            key=lambda tp: abs(tp - entry_mid)
        )
        reward = abs(best_tp - entry_mid)
        rr = reward / risk if risk else 0.0
        sl_distance = abs(entry_mid - sl) / entry_mid if entry_mid else 0.0

        execution_quality_score, execution_quality_reasons = self._calc_execution_quality(
            rr=rr,
            sl_distance_pct=sl_distance,
            entry_distance_pct=distance_pct,
            zone_span_pct=zone_span_pct,
            decision_score=decision.score,
        )

        async def _shadow_rejection(
                rejection_reason: str,
                rejection_details: str | None,
                category: RejectionCategory,
        ) -> None:
            """Record rejected executable plans for Shadow Intelligence.

            This is learning-only and never changes real trading behavior.
            """
            try:
                ds = self._build_decision_snapshot(
                    snap=snap,
                    lmap=lmap,
                    decision=decision,
                    market_state=market_state,
                    market_regime=market_regime,
                    direction=direction,
                    entry_mid=entry_mid,
                    sl=sl,
                    tp1=tp1,
                    tp2=tp2,
                    tp3=tp3,
                    rr=rr,
                    risk=risk,
                )
                await ShadowTradeTracker().create_from_plan(
                    symbol=snap.symbol,
                    direction=direction,
                    source="TRADE_GENERATOR",
                    category=getattr(category, "value", str(category)),
                    rejection_reason=rejection_reason,
                    rejection_details=rejection_details,
                    setup_score=float(decision.score or 0.0),
                    market_state=market_state,
                    market_regime=market_regime,
                    entry_price=float(entry_mid or 0.0),
                    virtual_sl=float(sl or 0.0),
                    virtual_tp1=float(tp1 or 0.0),
                    virtual_tp2=float(tp2 or 0.0),
                    virtual_tp3=float(tp3 or 0.0),
                    risk_reward_ratio=float(rr or 0.0),
                    decision_snapshot=ds,
                    plan_snapshot={
                        "entry_zone_low": float(entry_low or 0.0),
                        "entry_zone_high": float(entry_high or 0.0),
                        "entry_mid": float(entry_mid or 0.0),
                        "sweep_target": float(sweep_target or 0.0),
                        "invalidation": float(invalidation or 0.0),
                        "stop_loss": float(sl or 0.0),
                        "tp1": float(tp1 or 0.0),
                        "tp2": float(tp2 or 0.0),
                        "tp3": float(tp3 or 0.0),
                        "risk": float(risk or 0.0),
                        "reward": float(reward or 0.0),
                        "risk_reward": float(rr or 0.0),
                        "sl_distance_pct": float(sl_distance or 0.0),
                        "entry_distance_pct": float(distance_pct or 0.0),
                        "zone_span_pct": float(zone_span_pct or 0.0),
                        "execution_quality_score": float(execution_quality_score or 0.0),
                        "execution_quality_reasons": list(execution_quality_reasons or []),
                    },
                )
            except Exception as e:
                logger.warning(f"Shadow rejection log failed for {snap.symbol}: {e}")

        if sl_distance > self.max_sl_distance_pct:
            logger.info(
                f"⛔ SKIP {snap.symbol} — SL too wide "
                f"({sl_distance:.4f} > {self.max_sl_distance_pct:.4f})"
            )
            await self._log_rejected_signal(
                symbol=snap.symbol,
                direction=direction,
                setup_score=decision.score,
                market_state=market_state,
                market_regime=market_regime,
                category=RejectionCategory.RISK,
                rejection_reason="sl_too_wide",
                rejection_details=(
                    f"sl_distance_pct={sl_distance:.6f}; "
                    f"max_sl_distance_pct={self.max_sl_distance_pct:.6f}; "
                    f"execution_quality={execution_quality_score:.1f}; "
                    f"rr={rr:.4f}; "
                    f"entry_mid={entry_mid:.10f}; "
                    f"sweep_target={sweep_target:.10f}; "
                    f"invalidation={invalidation:.10f}; "
                    f"sl={sl:.10f}; "
                    f"risk={risk:.10f}; "
                    f"tp1={tp1:.10f}; "
                    f"tp2={tp2:.10f}; "
                    f"tp3={tp3:.10f}; "
                    f"best_tp={best_tp:.10f}; "
                    f"reward={reward:.10f}; "
                    f"direction={direction}; "
                    f"liquidity_bias={getattr(lmap, 'dominant_side', 'NA')}; "
                    f"primary_target={getattr(getattr(lmap, 'primary_target', None), 'price_level', None)}; "
                    f"quality_reasons={', '.join(execution_quality_reasons)}"
                ),
            )
            await _shadow_rejection(
                rejection_reason="sl_too_wide",
                rejection_details=None,
                category=RejectionCategory.RISK,
            )
            return None

        if execution_quality_score < self.min_execution_quality_score:
            logger.info(
                f"⛔ SKIP {snap.symbol} — execution quality too low "
                f"({execution_quality_score:.1f} < {self.min_execution_quality_score:.1f})"
            )
            await self._log_rejected_signal(
                symbol=snap.symbol,
                direction=direction,
                setup_score=decision.score,
                market_state=market_state,
                market_regime=market_regime,
                category=RejectionCategory.EXECUTION,
                rejection_reason="execution_quality_low",
                rejection_details=(
                    f"execution_quality={execution_quality_score:.1f}; "
                    f"min_execution_quality={self.min_execution_quality_score:.1f}; "
                    f"rr={rr:.4f}; "
                    f"sl_distance_pct={sl_distance:.6f}; "
                    f"entry_distance_pct={distance_pct:.6f}; "
                    f"zone_span_pct={zone_span_pct:.6f}; "
                    f"direction={direction}; "
                    f"quality_reasons={', '.join(execution_quality_reasons)}"
                ),
            )
            await _shadow_rejection(
                rejection_reason="execution_quality_low",
                rejection_details=None,
                category=RejectionCategory.EXECUTION,
            )
            return None

        # Adaptive RR protection:
        # - Normal acceptance: RR >= min_risk_reward_ratio
        # - Exception zone  : rr_soft_floor <= RR < min_risk_reward_ratio
        #                     only when setup_score >= rr_high_score_threshold
        rr_pass_normal = rr >= self.min_risk_reward
        rr_pass_exception = (
            rr >= self.rr_soft_floor
            and decision.score >= self.rr_high_score_threshold
        )

        if not (rr_pass_normal or rr_pass_exception):
            logger.info(
                f"⛔ SKIP {snap.symbol} — RR too low "
                f"({rr:.2f}; min={self.min_risk_reward:.2f}, "
                f"soft={self.rr_soft_floor:.2f}@score>={self.rr_high_score_threshold:.1f})"
            )
            await self._log_rejected_signal(
                symbol=snap.symbol,
                direction=direction,
                setup_score=decision.score,
                market_state=market_state,
                market_regime=market_regime,
                category=RejectionCategory.RISK,
                rejection_reason="rr_too_low",
                rejection_details=(
                    f"rr={rr:.4f} < min={self.min_risk_reward:.4f}; "
                    f"soft_floor={self.rr_soft_floor:.4f}; "
                    f"score={decision.score:.2f}; "
                    f"required_score={self.rr_high_score_threshold:.2f}; "
                    f"entry_mid={entry_mid:.10f}; "
                    f"sweep_target={sweep_target:.10f}; "
                    f"invalidation={invalidation:.10f}; "
                    f"sl={sl:.10f}; "
                    f"risk={risk:.10f}; "
                    f"tp1={tp1:.10f}; "
                    f"tp2={tp2:.10f}; "
                    f"tp3={tp3:.10f}; "
                    f"best_tp={best_tp:.10f}; "
                    f"reward={reward:.10f}; "
                    f"sl_distance_pct={sl_distance:.6f}; "
                    f"execution_quality={execution_quality_score:.1f}; "
                    f"quality_reasons={', '.join(execution_quality_reasons)}; "
                    f"direction={direction}; "
                    f"liquidity_bias={getattr(lmap, 'dominant_side', 'NA')}; "
                    f"primary_target={getattr(getattr(lmap, 'primary_target', None), 'price_level', None)}"
                ),
            )
            await _shadow_rejection(
                rejection_reason="rr_too_low",
                rejection_details=None,
                category=RejectionCategory.RISK,
            )
            return None

        if rr_pass_exception and not rr_pass_normal:
            logger.info(
                f"✅ {snap.symbol} — RR soft-pass "
                f"(rr={rr:.2f} >= {self.rr_soft_floor:.2f}, "
                f"score={decision.score:.1f} >= {self.rr_high_score_threshold:.1f})"
            )

        if sl_distance < self.min_sl_distance_pct:
            logger.info(
                f"⛔ SKIP {snap.symbol} — SL too tight ({sl_distance:.4f})"
            )
            await self._log_rejected_signal(
                symbol=snap.symbol,
                direction=direction,
                setup_score=decision.score,
                market_state=market_state,
                market_regime=market_regime,
                category=RejectionCategory.RISK,
                rejection_reason="sl_too_tight",
                rejection_details=(
                    f"sl_distance={sl_distance:.6f} < min={self.min_sl_distance_pct}"
                ),
            )
            await _shadow_rejection(
                rejection_reason="sl_too_tight",
                rejection_details=None,
                category=RejectionCategory.RISK,
            )
            return None

        estimated_success = 0.40 + (decision.score / 100) * 0.40

        decision.reasoning.append(
            f"Execution Quality: {execution_quality_score:.1f}/100 "
            f"({'; '.join(execution_quality_reasons) if execution_quality_reasons else 'clean execution'})"
        )

        logger.info(
            f"✅ TRADE {snap.symbol} {direction} RR={rr:.2f} "
            f"risk={risk:.4f} execQ={execution_quality_score:.1f}"
        )

        decision_snapshot = self._build_decision_snapshot(
            snap=snap,
            lmap=lmap,
            decision=decision,
            market_state=market_state,
            market_regime=market_regime,
            direction=direction,
            entry_mid=entry_mid,
            sl=sl,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            rr=rr,
            risk=risk,
        )

        return TradeCard(
            symbol=snap.symbol,
            direction=direction,
            setup_score=decision.score,
            market_state=market_state,
            market_regime=market_regime,
            trigger_description="Liquidity sweep + reclaim",
            entry_zone_low=entry_low,
            entry_zone_high=entry_high,
            stop_loss=sl,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            risk_reward=rr,
            invalidation_condition="Structure break",
            estimated_success=estimated_success,
            size_factor=decision.size_factor,
            reasoning=decision.reasoning,
            decision_snapshot=decision_snapshot,
            created_at=datetime.now(UTC),
        )