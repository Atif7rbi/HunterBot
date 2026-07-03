"""Layer 11 — Outcome Logger."""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import select
from src.core.database import AsyncSessionLocal, Trade, TradeOutcome, TradeStatus
from src.core.logger import logger

class OutcomeLogger:
    async def log(
        self,
        trade: Trade,
        funding_at_entry: float = 0.0,
        ls_global_at_entry: float = 1.0,
        oi_change_4h_at_entry: float = 0.0,
    ) -> None:
        if trade.status not in (TradeStatus.CLOSED_TP.value, TradeStatus.CLOSED_SL.value):
            return
        pnl_r = trade.pnl_r or 0.0
        result = 'WIN' if pnl_r > 0 else ('BREAKEVEN' if pnl_r == 0 else 'LOSS')
        duration = 0
        if trade.triggered_at and trade.closed_at:
            duration = int((trade.closed_at - trade.triggered_at).total_seconds() / 60)
        async with AsyncSessionLocal() as s:
            existing = await s.execute(select(TradeOutcome).where(TradeOutcome.trade_id == trade.id))
            if existing.scalars().first():
                return
            row = TradeOutcome(
                trade_id=trade.id,
                symbol=trade.symbol,
                setup_type=trade.market_state,
                regime=trade.market_regime,
                direction=trade.direction,
                setup_score=trade.setup_score or 0.0,
                funding_at_entry=funding_at_entry,
                ls_global_at_entry=ls_global_at_entry,
                oi_change_4h_at_entry=oi_change_4h_at_entry,
                result=result,
                pnl_r=pnl_r,
                duration_minutes=duration,
                closed_at=trade.closed_at or datetime.utcnow(),
                max_r_reached=getattr(trade, "max_r_reached", None),
                final_r=getattr(trade, "pnl_r", None),
                profit_capture_ratio=getattr(trade, "profit_capture_ratio", None),
                time_to_tp1_min=getattr(trade, "time_to_tp1_min", None),
                time_to_max_r_min=getattr(trade, "time_to_max_r_min", None),
                exit_reason=getattr(trade, "exit_reason", None),
                management_profile=getattr(trade, "management_profile", None),
                profile_tp1_r=getattr(trade, "profile_tp1_r", None),
                profile_tp1_close_pct=getattr(trade, "profile_tp1_close_pct", None),
                profile_break_even_r=getattr(trade, "profile_break_even_r", None),
                profile_trailing_r=getattr(trade, "profile_trailing_r", None),
                profile_trailing_distance_r=getattr(trade, "profile_trailing_distance_r", None),
                be_activated_at_r=getattr(trade, "be_activated_at_r", None),
                max_r_after_be=getattr(trade, "max_r_after_be", None),
                trailing_activated_at_r=getattr(trade, "trailing_activated_at_r", None),
                max_r_after_trailing=getattr(trade, "max_r_after_trailing", None),
                profit_locked_r=getattr(trade, "profit_locked_r", None),
                trailing_moves=getattr(trade, "trailing_moves", None),
                trade_quality_class=getattr(trade, "trade_quality_class", None),
                trade_quality_score=getattr(trade, "trade_quality_score", None),
                trade_quality_reason=getattr(trade, "trade_quality_reason", None),
            )
            s.add(row)
            await s.commit()
        logger.info(f"📝 Outcome #{trade.id}: {result} {pnl_r:+.2f}R [{trade.market_state} × {trade.market_regime}]")
