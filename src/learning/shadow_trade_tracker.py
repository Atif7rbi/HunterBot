from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import requests
from sqlalchemy import select

from src.core.database import AsyncSessionLocal, ShadowTrade
from src.core.logger import logger

try:
    from src.layers.trade_quality_classifier import classify_trade_quality
except Exception:  # pragma: no cover
    classify_trade_quality = None


TRACK_HOURS = 48
DEDUP_MINUTES = 20


def _raw(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "")


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def calc_r(entry: float, sl: float, price: float, direction: str) -> float:
    risk = abs(entry - sl)
    if risk <= 1e-12:
        return 0.0
    if str(direction).upper() == "LONG":
        return (price - entry) / risk
    return (entry - price) / risk


def _hit_level(direction: str, price: float, level: float | None, *, target: bool) -> bool:
    if level is None:
        return False
    direction = direction.upper()
    if target:
        return price >= level if direction == "LONG" else price <= level
    return price <= level if direction == "LONG" else price >= level


class ShadowTradeTracker:
    """Learning-only engine for rejected setups.

    It never submits trades and never changes real trade management. It only
    records high-quality rejected setups and updates their hypothetical outcome
    using live Binance futures prices.
    """

    def __init__(self, min_score: float = 70.0) -> None:
        self.min_score = float(min_score)

    async def create_from_plan(
        self,
        *,
        symbol: str,
        direction: str,
        source: str,
        category: str | None,
        rejection_reason: str,
        rejection_details: str | None,
        setup_score: float,
        market_state: Any,
        market_regime: Any,
        entry_price: float,
        virtual_sl: float,
        virtual_tp1: float | None,
        virtual_tp2: float | None,
        virtual_tp3: float | None,
        risk_reward_ratio: float | None,
        decision_snapshot: dict | None = None,
        plan_snapshot: dict | None = None,
        card: Any | None = None,
    ) -> int | None:
        symbol = str(symbol or "").upper()
        direction = str(direction or "").upper()
        setup_score = float(setup_score or 0.0)

        if not symbol or direction not in {"LONG", "SHORT"}:
            return None
        if setup_score < self.min_score:
            return None

        entry_price = float(entry_price or 0.0)
        virtual_sl = float(virtual_sl or 0.0)
        if entry_price <= 0 or virtual_sl <= 0 or abs(entry_price - virtual_sl) <= 1e-12:
            return None

        now = datetime.utcnow()
        cutoff = now - timedelta(minutes=DEDUP_MINUTES)

        quality_class = None
        quality_score = None
        quality_reason = None
        if card is not None and classify_trade_quality is not None:
            try:
                q = classify_trade_quality(card)
                quality_class = q.quality_class
                quality_score = float(q.quality_score)
                quality_reason = q.reason
            except Exception:
                pass

        ds = decision_snapshot or {}
        positioning = ds.get("positioning") or {}
        liquidity = ds.get("liquidity") or {}
        score = _float(ds.get("score"), setup_score)

        async with AsyncSessionLocal() as s:
            existing = await s.scalar(
                select(ShadowTrade).where(
                    ShadowTrade.symbol == symbol,
                    ShadowTrade.direction == direction,
                    ShadowTrade.rejection_reason == rejection_reason,
                    ShadowTrade.created_at >= cutoff,
                ).order_by(ShadowTrade.created_at.desc())
            )
            if existing is not None:
                return int(existing.id)

            row = ShadowTrade(
                symbol=symbol,
                direction=direction,
                source=source,
                category=category,
                rejection_reason=rejection_reason,
                rejection_details=rejection_details,
                status="ACTIVE",
                created_at=now,
                decision_score=score,
                setup_score=setup_score,
                market_state=_raw(market_state),
                market_regime=_raw(market_regime),
                trade_quality_class=quality_class,
                trade_quality_score=quality_score,
                trade_quality_reason=quality_reason,
                entry_price=entry_price,
                virtual_sl=virtual_sl,
                virtual_tp1=virtual_tp1,
                virtual_tp2=virtual_tp2,
                virtual_tp3=virtual_tp3,
                risk_reward_ratio=risk_reward_ratio,
                risk_unit=abs(entry_price - virtual_sl),
                best_price=entry_price,
                worst_price=entry_price,
                current_price=entry_price,
                funding_rate=positioning.get("funding_rate"),
                ls_ratio_global=positioning.get("ls_ratio_global"),
                ls_ratio_top=positioning.get("ls_ratio_top"),
                open_interest_usd=positioning.get("open_interest_usd"),
                liquidity_imbalance=liquidity.get("imbalance"),
                liquidity_dominant_side=liquidity.get("dominant_side"),
                liquidity_primary_target=liquidity.get("primary_target"),
                decision_snapshot=decision_snapshot,
                plan_snapshot=plan_snapshot,
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)

        logger.info(
            "🧠 SHADOW created %s %s reason=%s score=%.1f entry=%.8f sl=%.8f rr=%s quality=%s",
            symbol,
            direction,
            rejection_reason,
            setup_score,
            entry_price,
            virtual_sl,
            f"{risk_reward_ratio:.2f}" if risk_reward_ratio is not None else "n/a",
            quality_class or "—",
        )
        return int(row.id)

    async def create_from_card_rejection(
        self,
        *,
        card: Any,
        rejection_reason: str,
        rejection_details: str | None = None,
        source: str = "PAPER_EXECUTOR",
        category: str | None = "SYSTEM_FILTER",
    ) -> int | None:
        entry = (float(card.entry_zone_low) + float(card.entry_zone_high)) / 2.0
        return await self.create_from_plan(
            symbol=card.symbol,
            direction=card.direction,
            source=source,
            category=category,
            rejection_reason=rejection_reason,
            rejection_details=rejection_details,
            setup_score=float(card.setup_score or 0.0),
            market_state=card.market_state,
            market_regime=card.market_regime,
            entry_price=entry,
            virtual_sl=float(card.stop_loss or 0.0),
            virtual_tp1=float(card.tp1 or 0.0),
            virtual_tp2=float(card.tp2 or 0.0),
            virtual_tp3=float(card.tp3 or 0.0),
            risk_reward_ratio=float(card.risk_reward or 0.0),
            decision_snapshot=card.decision_snapshot,
            plan_snapshot={
                "entry_zone_low": float(card.entry_zone_low or 0.0),
                "entry_zone_high": float(card.entry_zone_high or 0.0),
                "stop_loss": float(card.stop_loss or 0.0),
                "tp1": float(card.tp1 or 0.0),
                "tp2": float(card.tp2 or 0.0),
                "tp3": float(card.tp3 or 0.0),
                "risk_reward": float(card.risk_reward or 0.0),
                "reasoning": list(card.reasoning or []),
            },
            card=card,
        )

    async def _fetch_prices(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        try:
            data = requests.get("https://fapi.binance.com/fapi/v1/ticker/price", timeout=10).json()
            all_prices = {x["symbol"]: float(x["price"]) for x in data}
            return {s: all_prices[s] for s in symbols if s in all_prices}
        except Exception as e:
            logger.exception(f"[ShadowTradeTracker] price fetch failed: {e}")
            return {}

    async def update_outcomes(self, prices: dict[str, float] | None = None) -> dict[str, int]:
        now = datetime.utcnow()
        async with AsyncSessionLocal() as s:
            res = await s.execute(select(ShadowTrade).where(ShadowTrade.status == "ACTIVE"))
            rows = list(res.scalars().all())

        if not rows:
            return {"checked": 0, "updated": 0, "finalized": 0, "missing_price": 0}

        symbols = sorted({r.symbol for r in rows})
        price_map = dict(prices or {})
        missing = [sym for sym in symbols if sym not in price_map]
        if missing:
            price_map.update(await self._fetch_prices(missing))

        stats = {"checked": len(rows), "updated": 0, "finalized": 0, "missing_price": 0}

        async with AsyncSessionLocal() as s:
            for old in rows:
                row = await s.get(ShadowTrade, old.id)
                if row is None or row.status != "ACTIVE":
                    continue
                price = price_map.get(row.symbol)
                if price is None:
                    stats["missing_price"] += 1
                    continue

                price = float(price)
                age_hours = (now - row.created_at).total_seconds() / 3600.0 if row.created_at else 0.0
                r_now = calc_r(row.entry_price, row.virtual_sl, price, row.direction)

                row.current_price = price
                row.last_checked_at = now
                row.age_hours = age_hours
                row.final_r = r_now

                # Best/worst price by favorable R, not absolute price.
                best_r_old = calc_r(row.entry_price, row.virtual_sl, row.best_price or row.entry_price, row.direction)
                worst_r_old = calc_r(row.entry_price, row.virtual_sl, row.worst_price or row.entry_price, row.direction)
                if r_now > best_r_old:
                    row.best_price = price
                    row.max_r_reached = r_now
                else:
                    row.max_r_reached = max(float(row.max_r_reached or 0.0), best_r_old)
                if r_now < worst_r_old:
                    row.worst_price = price
                    row.min_r_reached = r_now
                else:
                    row.min_r_reached = min(float(row.min_r_reached or 0.0), worst_r_old)

                hit_sl = _hit_level(row.direction, price, row.virtual_sl, target=False)
                hit_tp1 = _hit_level(row.direction, price, row.virtual_tp1, target=True)
                hit_tp2 = _hit_level(row.direction, price, row.virtual_tp2, target=True)
                hit_tp3 = _hit_level(row.direction, price, row.virtual_tp3, target=True)

                for flag, value, label in [
                    ("would_hit_sl", hit_sl, "SL"),
                    ("would_hit_tp1", hit_tp1, "TP1"),
                    ("would_hit_tp2", hit_tp2, "TP2"),
                    ("would_hit_tp3", hit_tp3, "TP3"),
                ]:
                    if value and not getattr(row, flag):
                        setattr(row, flag, True)
                        if not row.first_hit:
                            row.first_hit = label
                            row.first_hit_at = now

                if age_hours >= 1 and row.r_1h is None:
                    row.r_1h = r_now
                if age_hours >= 4 and row.r_4h is None:
                    row.r_4h = r_now
                if age_hours >= 24 and row.r_24h is None:
                    row.r_24h = r_now
                if age_hours >= 48 and row.r_48h is None:
                    row.r_48h = r_now
                    row.status = "FINALIZED"
                    row.finalized_at = now
                    stats["finalized"] += 1

                stats["updated"] += 1

            await s.commit()

        if stats["updated"]:
            logger.info(
                "🧠 SHADOW outcomes updated checked=%d updated=%d finalized=%d missing=%d",
                stats["checked"], stats["updated"], stats["finalized"], stats["missing_price"],
            )
        return stats
