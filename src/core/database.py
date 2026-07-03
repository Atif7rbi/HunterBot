from __future__ import annotations

from datetime import datetime
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import (
    JSON, Boolean, DateTime, Enum, Float,
    ForeignKey, Index, Integer, String, func, text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.core.config import settings
from src.core.logger import logger


class Base(DeclarativeBase):
    pass


class TradeDirection(str, PyEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class TradeStatus(str, PyEnum):
    PENDING = "PENDING"
    TRIGGERED = "TRIGGERED"
    CLOSED_TP = "CLOSED_TP"
    CLOSED_SL = "CLOSED_SL"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class MarketState(str, PyEnum):
    CROWDED_LONG_TRAP = "CROWDED_LONG_TRAP"
    SHORT_SQUEEZE_SETUP = "SHORT_SQUEEZE_SETUP"
    SMART_MONEY_DIVERGENCE = "SMART_MONEY_DIVERGENCE"
    EXHAUSTION = "EXHAUSTION"
    ACCUMULATION = "ACCUMULATION"
    NO_SETUP = "NO_SETUP"


class MarketRegime(str, PyEnum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"


class RejectionCategory(str, PyEnum):
    EXECUTION = "EXECUTION"
    RISK = "RISK"
    POSITION_LIMIT = "POSITION_LIMIT"
    MARKET_STRUCTURE = "MARKET_STRUCTURE"
    SYSTEM_FILTER = "SYSTEM_FILTER"


class Symbol(Base):
    __tablename__ = "symbols"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    base_asset: Mapped[str] = mapped_column(String(16))
    quote_asset: Mapped[str] = mapped_column(String(16))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ScanSnapshot(Base):
    __tablename__ = "scan_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True, default=func.now())
    price: Mapped[float] = mapped_column(Float)
    volume_24h_usd: Mapped[float] = mapped_column(Float)
    open_interest_usd: Mapped[float] = mapped_column(Float)
    funding_rate: Mapped[float] = mapped_column(Float)
    long_short_ratio: Mapped[float] = mapped_column(Float)

    # Extra L/S sources for dashboard and telemetry
    # long_short_ratio  = global account ratio
    # ls_position_ratio = top trader position ratio
    # ls_account_ratio  = top trader account ratio
    ls_position_ratio: Mapped[float] = mapped_column(Float, default=1.0)
    ls_account_ratio: Mapped[float] = mapped_column(Float, default=1.0)

    oi_change_4h_pct: Mapped[float] = mapped_column(Float, default=0.0)
    passed_filters: Mapped[bool] = mapped_column(Boolean, default=False)
    extremity_score: Mapped[float] = mapped_column(Float, default=0.0)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True, default=func.now())
    price: Mapped[float] = mapped_column(Float)
    funding_rate: Mapped[float] = mapped_column(Float)
    open_interest: Mapped[float] = mapped_column(Float)
    open_interest_usd: Mapped[float] = mapped_column(Float)
    long_short_ratio_global: Mapped[float] = mapped_column(Float)
    long_short_ratio_top: Mapped[float] = mapped_column(Float)
    taker_buy_volume: Mapped[float] = mapped_column(Float)
    taker_sell_volume: Mapped[float] = mapped_column(Float)
    market_state: Mapped[Optional[str]] = mapped_column(Enum(MarketState), nullable=True)
    market_regime: Mapped[Optional[str]] = mapped_column(Enum(MarketRegime), nullable=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    raw_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    __table_args__ = (Index("ix_snapshot_symbol_time", "symbol", "timestamp"),)


class LiquidityZone(Base):
    __tablename__ = "liquidity_zones"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    price_level: Mapped[float] = mapped_column(Float)
    side: Mapped[str] = mapped_column(String(8))
    estimated_liquidations_usd: Mapped[float] = mapped_column(Float, default=0.0)
    distance_pct: Mapped[float] = mapped_column(Float)
    strength: Mapped[float] = mapped_column(Float, default=0.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(Enum(TradeDirection))
    status: Mapped[str] = mapped_column(Enum(TradeStatus), default=TradeStatus.PENDING, index=True)

    setup_score: Mapped[float] = mapped_column(Float)
    market_state: Mapped[str] = mapped_column(Enum(MarketState))
    market_regime: Mapped[str] = mapped_column(Enum(MarketRegime))
    trigger_description: Mapped[str] = mapped_column(String(512))
    trigger_confirmed_count: Mapped[int] = mapped_column(Integer, default=0)
    invalidation_condition: Mapped[str] = mapped_column(String(512))
    layer_scores: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    decision_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    entry_zone_low: Mapped[float] = mapped_column(Float)
    entry_zone_high: Mapped[float] = mapped_column(Float)
    actual_entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit_1: Mapped[float] = mapped_column(Float)
    take_profit_2: Mapped[float] = mapped_column(Float)
    take_profit_3: Mapped[float] = mapped_column(Float)
    risk_reward_ratio: Mapped[float] = mapped_column(Float)

    position_size_usd: Mapped[float] = mapped_column(Float)
    risk_amount_usd: Mapped[float] = mapped_column(Float)

    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fees_usd: Mapped[float] = mapped_column(Float, default=0.0)

    mae_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mfe_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    initial_sl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_migrated: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=False)
    tp1_hit: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=False)
    layer2_locked: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=False)
    trailing_active: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=False)
    trailing_anchor: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0)
    sl_layer2: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0)
    realized_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0)

    max_r_reached: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0)
    trailing_activated_at_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0)
    trailing_stop_at_activation: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0)

    # Trade management analytics v2 — BE/trailing audit + dynamic profit lock.
    be_activated_at_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0)
    be_activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    max_r_after_be: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0)
    max_r_after_trailing: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0)
    profit_locked_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0)
    trailing_moves: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=0)

    # Trade management analytics v1
    # These fields do not change execution behavior. They only record how well
    # the exit model captured the available R from each trade.
    management_profile: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, default="DEFAULT_FIXED")
    profile_tp1_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    profile_tp1_close_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    profile_break_even_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    profile_trailing_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    profile_trailing_distance_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tp1_hit_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    max_r_reached_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    time_to_tp1_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    time_to_max_r_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    profit_capture_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Trade quality classification v1
    # This is analytics-only. It does not change entry, SL, TP, trailing, or sizing.
    trade_quality_class: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    trade_quality_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trade_quality_reason: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    triggered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)


class RejectedSignal(Base):
    __tablename__ = "rejected_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(8))
    setup_score: Mapped[float] = mapped_column(Float, index=True)
    market_state: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    market_regime: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    category: Mapped[Optional[str]] = mapped_column(
        Enum(RejectionCategory),
        nullable=True,
        index=True,
    )

    rejection_reason: Mapped[str] = mapped_column(String(64), index=True)
    rejection_details: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), index=True)

    __table_args__ = (
        Index("ix_rejected_signal_symbol_time", "symbol", "created_at"),
    )




class ShadowTrade(Base):
    """Rejected-signal shadow trade for learning only.

    This table never affects real execution. It records high-quality rejected
    setups and tracks their hypothetical outcome over time so Hunter can learn
    whether each rejection filter is helping or blocking good opportunities.
    """
    __tablename__ = "shadow_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(8), index=True)
    source: Mapped[str] = mapped_column(String(32), default="TRADE_GENERATOR", index=True)
    category: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    rejection_reason: Mapped[str] = mapped_column(String(64), index=True)
    rejection_details: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), index=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    age_hours: Mapped[float] = mapped_column(Float, default=0.0)

    decision_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    setup_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    market_state: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    market_regime: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)

    trade_quality_class: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, index=True)
    trade_quality_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trade_quality_reason: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    entry_price: Mapped[float] = mapped_column(Float)
    virtual_sl: Mapped[float] = mapped_column(Float)
    virtual_tp1: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    virtual_tp2: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    virtual_tp3: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    risk_reward_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    risk_unit: Mapped[float] = mapped_column(Float, default=0.0)

    current_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    best_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    worst_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_r_reached: Mapped[float] = mapped_column(Float, default=0.0)
    min_r_reached: Mapped[float] = mapped_column(Float, default=0.0)
    final_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    would_hit_sl: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    would_hit_tp1: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    would_hit_tp2: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    would_hit_tp3: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    first_hit: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)
    first_hit_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    r_1h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    r_4h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    r_24h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    r_48h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    funding_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ls_ratio_global: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ls_ratio_top: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ls_account_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    open_interest_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    oi_change_4h_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    liquidity_imbalance: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    liquidity_dominant_side: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    liquidity_primary_target: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    decision_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    plan_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_shadow_symbol_time", "symbol", "created_at"),
        Index("ix_shadow_reason_time", "rejection_reason", "created_at"),
        Index("ix_shadow_quality_time", "trade_quality_class", "created_at"),
    )


class WatchZone(Base):
    __tablename__ = "watch_zones"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=func.now(), index=True)
    score: Mapped[float] = mapped_column(Float)
    direction: Mapped[str] = mapped_column(String(8))
    market_state: Mapped[str] = mapped_column(String(64))
    regime: Mapped[str] = mapped_column(String(32))
    funding_rate: Mapped[float] = mapped_column(Float, default=0.0)
    components: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    outcome: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    max_move_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    __table_args__ = (Index("ix_watchzone_symbol_time", "symbol", "timestamp"),)


class PositioningBiasLog(Base):
    __tablename__ = "positioning_bias_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=func.now(), index=True)

    bot_direction: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, index=True)
    positioning_bias: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)
    bias_strength: Mapped[float] = mapped_column(Float, default=0.0)
    vwap_alignment: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    crowding_side: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    squeeze_type: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)

    market_state: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    market_regime: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)

    decision_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    raw_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    match_state: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)
    action_taken: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)

    funding_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ls_ratio_global: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ls_ratio_top: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    oi_change_4h_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    vwap_distance_15m_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vwap_distance_1h_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vwap_distance_4h_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    reasoning: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    watch_zone_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("watch_zones.id"),
        nullable=True,
        index=True,
    )
    trade_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("trades.id"),
        nullable=True,
        index=True,
    )

    __table_args__ = (
        Index("ix_positioning_bias_symbol_time", "symbol", "timestamp"),
        Index("ix_positioning_bias_match_regime", "match_state", "market_regime"),
    )


class TradeOutcome(Base):
    __tablename__ = "trade_outcomes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_id: Mapped[int] = mapped_column(Integer, ForeignKey("trades.id"), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    setup_type: Mapped[str] = mapped_column(Enum(MarketState), index=True)
    regime: Mapped[str] = mapped_column(Enum(MarketRegime), index=True)
    direction: Mapped[str] = mapped_column(Enum(TradeDirection))
    setup_score: Mapped[float] = mapped_column(Float)
    funding_at_entry: Mapped[float] = mapped_column(Float, default=0.0)
    ls_global_at_entry: Mapped[float] = mapped_column(Float, default=1.0)
    oi_change_4h_at_entry: Mapped[float] = mapped_column(Float, default=0.0)
    result: Mapped[str] = mapped_column(String(12))
    pnl_r: Mapped[float] = mapped_column(Float)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=0)
    layer_scores: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    closed_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), index=True)

    mae_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mfe_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    time_in_trade_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    time_to_mfe_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sl_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tp_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    veto_reason: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    # Trade management analytics v1
    max_r_reached: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    final_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    profit_capture_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    time_to_tp1_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    time_to_max_r_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    exit_reason: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    management_profile: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    profile_tp1_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    profile_tp1_close_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    profile_break_even_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    profile_trailing_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    profile_trailing_distance_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Trade management analytics v2
    be_activated_at_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_r_after_be: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trailing_activated_at_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_r_after_trailing: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    profit_locked_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trailing_moves: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Trade quality classification v1
    trade_quality_class: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    trade_quality_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trade_quality_reason: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    __table_args__ = (Index("ix_outcome_setup_regime", "setup_type", "regime"),)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=func.now(), index=True)
    equity_usd: Mapped[float] = mapped_column(Float)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)
    daily_pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    daily_pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    kill_switch_active: Mapped[bool] = mapped_column(Boolean, default=False)


class AlertLog(Base):
    __tablename__ = "alert_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=func.now(), index=True)
    alert_type: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(String(2048))
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)


engine = create_async_engine(settings.env.database_url, echo=False, future=True)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def migrate_rejected_signals_category() -> None:
    async with AsyncSessionLocal() as s:
        result = await s.execute(text("PRAGMA table_info(rejected_signals)"))
        existing = [row[1] for row in result.fetchall()]

        if "category" not in existing:
            await s.execute(
                text("ALTER TABLE rejected_signals ADD COLUMN category VARCHAR(32)")
            )
            logger.info("Added column category to rejected_signals")
        else:
            logger.info("Column exists: rejected_signals.category")

        await s.commit()


async def migrate_trade_decision_snapshot() -> None:
    async with AsyncSessionLocal() as s:
        result = await s.execute(text("PRAGMA table_info(trades)"))
        existing = [row[1] for row in result.fetchall()]

        if "decision_snapshot" not in existing:
            await s.execute(
                text("ALTER TABLE trades ADD COLUMN decision_snapshot JSON")
            )
            logger.info("Added column decision_snapshot to trades")
        else:
            logger.info("Column exists: trades.decision_snapshot")

        await s.commit()



async def migrate_trade_management_metrics() -> None:
    """Add analytics columns for adaptive trade management.

    Safe for existing SQLite DBs. These columns are informational only and do
    not change trade execution behavior.
    """
    trade_columns = [
        ("management_profile", "VARCHAR(32) DEFAULT 'DEFAULT_FIXED'"),
        ("profile_tp1_r", "FLOAT"),
        ("profile_tp1_close_pct", "FLOAT"),
        ("profile_break_even_r", "FLOAT"),
        ("profile_trailing_r", "FLOAT"),
        ("profile_trailing_distance_r", "FLOAT"),
        ("exit_reason", "VARCHAR(128)"),
        ("tp1_hit_at", "DATETIME"),
        ("max_r_reached_at", "DATETIME"),
        ("time_to_tp1_min", "INTEGER"),
        ("time_to_max_r_min", "INTEGER"),
        ("profit_capture_ratio", "FLOAT"),
        ("be_activated_at_r", "FLOAT DEFAULT 0"),
        ("be_activated_at", "DATETIME"),
        ("max_r_after_be", "FLOAT DEFAULT 0"),
        ("max_r_after_trailing", "FLOAT DEFAULT 0"),
        ("profit_locked_r", "FLOAT DEFAULT 0"),
        ("trailing_moves", "INTEGER DEFAULT 0"),
        ("trade_quality_class", "VARCHAR(8)"),
        ("trade_quality_score", "FLOAT"),
        ("trade_quality_reason", "VARCHAR(512)"),
    ]

    outcome_columns = [
        ("max_r_reached", "FLOAT"),
        ("final_r", "FLOAT"),
        ("profit_capture_ratio", "FLOAT"),
        ("time_to_tp1_min", "INTEGER"),
        ("time_to_max_r_min", "INTEGER"),
        ("exit_reason", "VARCHAR(128)"),
        ("management_profile", "VARCHAR(32)"),
        ("profile_tp1_r", "FLOAT"),
        ("profile_tp1_close_pct", "FLOAT"),
        ("profile_break_even_r", "FLOAT"),
        ("profile_trailing_r", "FLOAT"),
        ("profile_trailing_distance_r", "FLOAT"),
        ("be_activated_at_r", "FLOAT"),
        ("max_r_after_be", "FLOAT"),
        ("trailing_activated_at_r", "FLOAT"),
        ("max_r_after_trailing", "FLOAT"),
        ("profit_locked_r", "FLOAT"),
        ("trailing_moves", "INTEGER"),
        ("trade_quality_class", "VARCHAR(8)"),
        ("trade_quality_score", "FLOAT"),
        ("trade_quality_reason", "VARCHAR(512)"),
    ]

    async with AsyncSessionLocal() as s:
        result = await s.execute(text("PRAGMA table_info(trades)"))
        existing = [row[1] for row in result.fetchall()]
        for col_name, col_def in trade_columns:
            if col_name not in existing:
                await s.execute(text(f"ALTER TABLE trades ADD COLUMN {col_name} {col_def}"))
                logger.info(f"Added column trades.{col_name}")
            else:
                logger.info(f"Column exists: trades.{col_name}")

        result = await s.execute(text("PRAGMA table_info(trade_outcomes)"))
        existing = [row[1] for row in result.fetchall()]
        for col_name, col_def in outcome_columns:
            if col_name not in existing:
                await s.execute(text(f"ALTER TABLE trade_outcomes ADD COLUMN {col_name} {col_def}"))
                logger.info(f"Added column trade_outcomes.{col_name}")
            else:
                logger.info(f"Column exists: trade_outcomes.{col_name}")

        await s.commit()


async def migrate_scan_snapshot_ls_sources() -> None:
    async with AsyncSessionLocal() as s:
        result = await s.execute(text("PRAGMA table_info(scan_snapshots)"))
        existing = [row[1] for row in result.fetchall()]

        if "ls_position_ratio" not in existing:
            await s.execute(
                text("ALTER TABLE scan_snapshots ADD COLUMN ls_position_ratio FLOAT DEFAULT 1.0")
            )
            logger.info("Added column scan_snapshots.ls_position_ratio")
        else:
            logger.info("Column exists: scan_snapshots.ls_position_ratio")

        if "ls_account_ratio" not in existing:
            await s.execute(
                text("ALTER TABLE scan_snapshots ADD COLUMN ls_account_ratio FLOAT DEFAULT 1.0")
            )
            logger.info("Added column scan_snapshots.ls_account_ratio")
        else:
            logger.info("Column exists: scan_snapshots.ls_account_ratio")

        await s.commit()


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await migrate_rejected_signals_category()
    await migrate_trade_decision_snapshot()
    await migrate_trade_management_metrics()
    await migrate_scan_snapshot_ls_sources()


async def get_session() -> AsyncSession:
    return AsyncSessionLocal()