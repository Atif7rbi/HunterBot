"""Layer 10 — Paper Executor v3.4
Progressive Exit Engine + Instrumentation + Cooldown Guard + Management V2.1

Exit model:
TP1 @ 0.4R → partial close 50%
BE  @ 1.0R → SL → entry
Trail @ 1.5R → trailing SL ON, distance 0.6R from anchor

v3.2:
- Cooldown guard: skip new trade on symbol if cancelled within last 30min
- Management timing repair: delay BE to 1.0R, delay trailing to 1.5R

v3.3 — التعديل الوحيد:
- BUG FIX: أضفنا cooldown بعد CLOSED_SL (كان مفقوداً تماماً)
  في v3.2 كان الـ cooldown يعمل فقط بعد CANCELLED
  النتيجة: بعد كل خسارة SL كان البوت يعيد الدخول فوراً على نفس الرمز
  المثال: AIGENSYNUSDT خسر 9 مرات متتالية لأن cooldown لم يمنعه
  الحل: SL_COOLDOWN_MINUTES = 60 دقيقة بعد CLOSED_SL
         CANCEL_COOLDOWN_MINUTES = 30 دقيقة بعد CANCELLED (لم يتغير)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, text

from src.core.config import settings
from src.core.database import (
    AsyncSessionLocal,
    PortfolioSnapshot,
    RejectedSignal,
    Trade,
    TradeDirection,
    TradeStatus,
)
from src.core.logger import logger
from src.layers.trade_generator import TradeCard
from src.layers.trade_quality_classifier import classify_trade_quality
from src.layers.management_profiles import profile_for_quality, profile_from_trade
from src.learning.outcome_logger import OutcomeLogger
from src.learning.shadow_trade_tracker import ShadowTradeTracker

# ── Cooldown constants ────────────────────────────────────────────────────────
# Read from config.yaml to keep test behavior controlled from one place.
# Defaults preserve the old hardcoded behavior if keys are missing.
CANCEL_COOLDOWN_MINUTES: int = int(
    settings.paper_executor.get("cooldown_after_cancel_minutes", 30)
)
SL_COOLDOWN_MINUTES: int = int(
    settings.paper_executor.get("cooldown_after_sl_minutes", 60)
)
TP_COOLDOWN_MINUTES: int = int(
    settings.paper_executor.get("cooldown_after_tp_minutes", 0)
)

# للتوافق مع أي كود خارجي يستخدم الاسم القديم
COOLDOWN_MINUTES: int = CANCEL_COOLDOWN_MINUTES

# ── Exit model constants ──────────────────────────────────────────────────────
# Read from config.yaml to keep exit behavior controlled from one place.
# Defaults preserve the old hardcoded behavior if keys are missing.
TP1_R: float = float(settings.paper_executor.get("tp1_r", 0.4))
TP1_CLOSE_PCT: float = float(settings.paper_executor.get("tp1_close_pct", 0.5))
BREAK_EVEN_R: float = float(settings.paper_executor.get("break_even_r", 1.0))
TRAILING_R: float = float(settings.paper_executor.get("trailing_r", 1.5))
TRAIL_DISTANCE_R: float = float(settings.paper_executor.get("trailing_distance_r", 0.6))


def calc_r(entry: float, initial_sl: float, price: float, direction: str) -> float:
    risk_unit = abs(entry - initial_sl)
    if risk_unit < 1e-9:
        return 0.0
    if direction == TradeDirection.LONG.value:
        return (price - entry) / risk_unit
    return (entry - price) / risk_unit


def price_from_r(entry: float, initial_sl: float, lock_r: float, direction: str) -> float:
    """Convert a locked profit in R into an SL price.

    This makes the trailing stop explicitly protect part of the achieved move,
    instead of relying only on the last anchor price.
    """
    risk_unit = abs(entry - initial_sl)
    if risk_unit < 1e-9:
        return entry
    if direction == TradeDirection.LONG.value:
        return entry + lock_r * risk_unit
    return entry - lock_r * risk_unit


def dynamic_profit_lock_r(max_r: float, trailing_distance_r: float) -> float:
    """Dynamic profit lock used by Management V2.1.

    Base rule: protect (max_r - trailing_distance_r).
    The audit showed winners reaching 2R–5R and closing near BE/small profit.
    These floors make the trailing stop explicitly lock a minimum amount of R
    once the move expands, while still allowing some breathing room.
    """
    if max_r <= 0:
        return 0.0

    lock = max(0.0, max_r - trailing_distance_r)

    # Expansion floors: once the trade proves itself, do not let it fall back
    # to a tiny winner. These are intentionally conservative and can be tuned
    # from audit results later.
    if max_r >= 5.0:
        lock = max(lock, 3.50)
    elif max_r >= 4.0:
        lock = max(lock, 2.75)
    elif max_r >= 3.0:
        lock = max(lock, 2.00)
    elif max_r >= 2.0:
        lock = max(lock, 1.20)
    elif max_r >= 1.5:
        lock = max(lock, 0.80)
    elif max_r >= 1.0:
        lock = max(lock, 0.40)

    return max(0.0, lock)


async def migrate_initial_sl() -> None:
    columns = [
        ("initial_sl",                  "FLOAT DEFAULT 0"),
        ("is_migrated",                 "BOOLEAN DEFAULT 0"),
        ("tp1_hit",                     "BOOLEAN DEFAULT 0"),
        ("layer2_locked",               "BOOLEAN DEFAULT 0"),
        ("trailing_active",             "BOOLEAN DEFAULT 0"),
        ("trailing_anchor",             "FLOAT DEFAULT 0"),
        ("sl_layer2",                   "FLOAT DEFAULT 0"),
        ("realized_pnl",                "FLOAT DEFAULT 0"),
        ("max_r_reached",               "FLOAT DEFAULT 0"),
        ("trailing_activated_at_r",     "FLOAT DEFAULT 0"),
        ("trailing_stop_at_activation", "FLOAT DEFAULT 0"),
        ("be_activated_at_r",           "FLOAT DEFAULT 0"),
        ("be_activated_at",             "DATETIME"),
        ("max_r_after_be",              "FLOAT DEFAULT 0"),
        ("max_r_after_trailing",        "FLOAT DEFAULT 0"),
        ("profit_locked_r",             "FLOAT DEFAULT 0"),
        ("trailing_moves",              "INTEGER DEFAULT 0"),
        ("management_profile",          "VARCHAR(32) DEFAULT 'DEFAULT_FIXED'"),
        ("exit_reason",                 "VARCHAR(128)"),
        ("tp1_hit_at",                  "DATETIME"),
        ("max_r_reached_at",            "DATETIME"),
        ("time_to_tp1_min",             "INTEGER"),
        ("time_to_max_r_min",           "INTEGER"),
        ("profit_capture_ratio",        "FLOAT"),
        ("trade_quality_class",       "VARCHAR(8)"),
        ("trade_quality_score",       "FLOAT"),
        ("trade_quality_reason",      "VARCHAR(512)"),
        ("profile_tp1_r",              "FLOAT"),
        ("profile_tp1_close_pct",      "FLOAT"),
        ("profile_break_even_r",       "FLOAT"),
        ("profile_trailing_r",         "FLOAT"),
        ("profile_trailing_distance_r","FLOAT"),
        ("decision_snapshot",             "JSON"),
    ]
    async with AsyncSessionLocal() as s:
        result = await s.execute(text("PRAGMA table_info(trades)"))
        existing = [row[1] for row in result.fetchall()]
        for col_name, col_def in columns:
            if col_name not in existing:
                await s.execute(text(f"ALTER TABLE trades ADD COLUMN {col_name} {col_def}"))
                logger.info(f"Added column {col_name}")
            else:
                logger.info(f"Column exists: {col_name}")
        await s.commit()
    logger.info("migrate_initial_sl complete")


class PaperExecutor:
    def __init__(self) -> None:
        self.cfg             = settings.paper_executor
        self.initial_capital = self.cfg["initial_capital_usd"]
        self.risk_pct        = self.cfg["risk_per_trade_pct"]
        self.slippage_entry  = self.cfg["slippage_entry_pct"]
        self.slippage_stop   = self.cfg["slippage_stop_pct"]
        self.spread          = self.cfg["spread_pct"]
        self.fee_pct         = settings.backtest["fee_pct"]

    # ── Equity / counts ───────────────────────────────────────────────────────

    async def get_equity(self) -> float:
        async with AsyncSessionLocal() as s:
            res = await s.execute(
                select(Trade).where(
                    Trade.status.in_([TradeStatus.CLOSED_TP.value, TradeStatus.CLOSED_SL.value])
                )
            )
            closed   = res.scalars().all()
            realized = sum((t.pnl_usd or 0) - (t.fees_usd or 0) for t in closed)
            return self.initial_capital + realized

    async def get_open_count(self) -> int:
        async with AsyncSessionLocal() as s:
            res = await s.execute(
                select(Trade).where(
                    Trade.status.in_([TradeStatus.PENDING.value, TradeStatus.TRIGGERED.value])
                )
            )
            return len(res.scalars().all())

    async def get_open_trades(self) -> list:
        async with AsyncSessionLocal() as s:
            res = await s.execute(
                select(Trade)
                .where(Trade.status.in_([TradeStatus.PENDING.value, TradeStatus.TRIGGERED.value]))
                .order_by(Trade.created_at.desc())
            )
            trades = res.scalars().all()
            for t in trades:
                t.entry_price = t.actual_entry_price or (t.entry_zone_low + t.entry_zone_high) / 2
                t.tp1         = t.take_profit_1
                t.tp2         = t.take_profit_2
                t.opened_at   = t.triggered_at or t.created_at
            return trades

    # ── Kill switch ───────────────────────────────────────────────────────────

    async def is_kill_switch_active(self) -> bool:
        async with AsyncSessionLocal() as s:
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            res = await s.execute(
                select(Trade).where(
                    Trade.closed_at >= today_start,
                    Trade.status.in_([TradeStatus.CLOSED_TP.value, TradeStatus.CLOSED_SL.value]),
                )
            )
            today_closed = sorted(res.scalars().all(), key=lambda t: t.closed_at)
        equity    = await self.get_equity()
        daily_pnl = sum(t.pnl_usd or 0 for t in today_closed)
        if equity > 0 and daily_pnl < 0:
            if abs(daily_pnl) >= self.initial_capital * self.cfg["daily_max_loss_pct"]:
                logger.warning(f"KILL SWITCH: daily loss {daily_pnl:.2f}")
                return True
        recent = sorted(today_closed, key=lambda t: t.closed_at, reverse=True)
        consec  = 0
        for t in recent:
            if (t.pnl_usd or 0) < 0:
                consec += 1
            else:
                break
        if consec >= self.cfg["daily_max_consecutive_losses"]:
            logger.warning(f"KILL SWITCH: {consec} consecutive losses")
            return True
        return False

    # ── Sizing ────────────────────────────────────────────────────────────────

    def compute_size(
        self, equity: float, entry: float, sl: float, size_factor: float
    ) -> tuple[float, float]:
        """Return notional size and the *actual* dollar risk.

        The requested risk can be capped by the max notional rule:
        ``notional = min(requested_notional, equity * 0.20)``.

        Older versions returned the requested/theoretical risk even when the
        notional was capped. That made pnl_r, capture ratio, leakage, and
        management health look worse than reality because final PnL was divided
        by a risk amount the trade never actually used.
        """
        requested_risk = equity * self.risk_pct * size_factor
        risk_per_unit = abs(entry - sl)
        if risk_per_unit <= 0 or entry <= 0:
            return 0.0, 0.0

        requested_units = requested_risk / risk_per_unit
        requested_notional = requested_units * entry
        notional = min(requested_notional, equity * 0.20)

        actual_units = notional / entry
        actual_risk = actual_units * risk_per_unit

        return notional, actual_risk

    # ── Rejected signal logger ────────────────────────────────────────────────

    async def _log_rejected_signal(
        self,
        *,
        card: TradeCard,
        rejection_reason: str,
        rejection_details: str | None = None,
    ) -> None:
        if (card.setup_score or 0.0) < 70:
            return
        try:
            async with AsyncSessionLocal() as s:
                row = RejectedSignal(
                    symbol            = card.symbol,
                    direction         = str(card.direction or ""),
                    setup_score       = float(card.setup_score or 0.0),
                    market_state      = getattr(card.market_state,  "value", card.market_state),
                    market_regime     = getattr(card.market_regime, "value", card.market_regime),
                    rejection_reason  = rejection_reason,
                    rejection_details = rejection_details,
                    created_at        = card.created_at,
                )
                s.add(row)
                await s.commit()
        except Exception as e:
            logger.exception(f"failed to log rejected signal for {card.symbol}: {e}")

        try:
            await ShadowTradeTracker().create_from_card_rejection(
                card=card,
                rejection_reason=rejection_reason,
                rejection_details=rejection_details,
                source="PAPER_EXECUTOR",
                category="SYSTEM_FILTER",
            )
        except Exception as e:
            logger.warning(f"Shadow executor rejection log failed for {card.symbol}: {e}")

    # ── Submit ────────────────────────────────────────────────────────────────

    async def submit(self, card: TradeCard) -> Optional[int]:
        # ── 1. Kill switch ───────────────────────────────────────────────────
        if await self.is_kill_switch_active():
            await self._log_rejected_signal(
                card=card,
                rejection_reason="kill_switch_active",
                rejection_details="daily kill switch active",
            )
            logger.warning(f"Kill switch active, skipping {card.symbol}")
            return None

        # ── 2. Get equity ────────────────────────────────────────────────────
        equity = await self.get_equity()

        # ── 3. Max concurrent trades ─────────────────────────────────────────
        if await self.get_open_count() >= self.cfg["max_concurrent_trades"]:
            await self._log_rejected_signal(
                card=card,
                rejection_reason="max_concurrent_reached",
                rejection_details=f"max_concurrent_trades={self.cfg['max_concurrent_trades']}",
            )
            logger.info(f"Max concurrent trades reached, skipping {card.symbol}")
            return None

        # ── 4. Cooldown checks — session واحد للفحصين ────────────────────────
        now           = datetime.utcnow()
        sl_cutoff     = now - timedelta(minutes=SL_COOLDOWN_MINUTES)
        cancel_cutoff = now - timedelta(minutes=CANCEL_COOLDOWN_MINUTES)

        async with AsyncSessionLocal() as s:
            # 4a. ✅ v3.3 — cooldown بعد CLOSED_SL (كان مفقوداً في v3.2)
            sl_res = await s.execute(
                select(Trade).where(
                    Trade.symbol    == card.symbol,
                    Trade.status    == TradeStatus.CLOSED_SL.value,
                    Trade.closed_at >= sl_cutoff,
                )
            )
            if sl_res.scalars().first():
                await self._log_rejected_signal(
                    card=card,
                    rejection_reason="cooldown_after_sl",
                    rejection_details=f"CLOSED_SL within last {SL_COOLDOWN_MINUTES} minutes",
                )
                logger.info(
                    f"⏳ {card.symbol} SL cooldown active "
                    f"(CLOSED_SL < {SL_COOLDOWN_MINUTES}min), skipping"
                )
                return None

            # 4b. cooldown بعد CANCELLED — كما كان في v3.2
            cancel_res = await s.execute(
                select(Trade).where(
                    Trade.symbol    == card.symbol,
                    Trade.status    == TradeStatus.CANCELLED.value,
                    Trade.closed_at >= cancel_cutoff,
                )
            )
            if cancel_res.scalars().first():
                await self._log_rejected_signal(
                    card=card,
                    rejection_reason="cooldown_active",
                    rejection_details=f"CANCELLED within last {CANCEL_COOLDOWN_MINUTES} minutes",
                )
                logger.info(
                    f"⏳ {card.symbol} cooldown active "
                    f"(CANCELLED < {CANCEL_COOLDOWN_MINUTES}min), skipping"
                )
                return None

        # ── 5. Compute size ──────────────────────────────────────────────────
        entry_mid      = (card.entry_zone_low + card.entry_zone_high) / 2
        notional, risk = self.compute_size(equity, entry_mid, card.stop_loss, card.size_factor)
        if notional <= 0:
            await self._log_rejected_signal(
                card=card,
                rejection_reason="invalid_position_size",
                rejection_details=f"notional={notional:.10f}, risk={risk:.10f}",
            )
            return None

        # ── 6. Active trade check ────────────────────────────────────────────
        async with AsyncSessionLocal() as s:
            res = await s.execute(
                select(Trade).where(
                    Trade.symbol == card.symbol,
                    Trade.status.in_([TradeStatus.PENDING.value, TradeStatus.TRIGGERED.value]),
                )
            )
            if res.scalars().first():
                await self._log_rejected_signal(
                    card=card,
                    rejection_reason="active_trade_exists",
                    rejection_details="symbol already has PENDING/TRIGGERED trade",
                )
                logger.info(f"{card.symbol} trade already active, skipping")
                return None

        # ── 7. Create PENDING trade ──────────────────────────────────────────
        quality = classify_trade_quality(card)
        profile = profile_for_quality(quality.quality_class)

        async with AsyncSessionLocal() as s:
            trade = Trade(
                symbol                      = card.symbol,
                direction                   = TradeDirection.LONG.value if card.direction == "LONG" else TradeDirection.SHORT.value,
                status                      = TradeStatus.PENDING.value,
                setup_score                 = card.setup_score,
                market_state                = card.market_state.value,
                market_regime               = card.market_regime.value,
                trigger_description         = card.trigger_description,
                trigger_confirmed_count     = 0,
                invalidation_condition      = card.invalidation_condition,
                entry_zone_low              = card.entry_zone_low,
                entry_zone_high             = card.entry_zone_high,
                stop_loss                   = card.stop_loss,
                initial_sl                  = card.stop_loss,
                take_profit_1               = card.tp1,
                take_profit_2               = card.tp2,
                take_profit_3               = card.tp3,
                risk_reward_ratio           = card.risk_reward,
                position_size_usd           = notional,
                risk_amount_usd             = risk,
                created_at                  = card.created_at,
                notes                       = "\n".join(card.reasoning[:5]),
                decision_snapshot           = card.decision_snapshot,
                is_migrated                 = False,
                tp1_hit                     = False,
                layer2_locked               = False,
                trailing_active             = False,
                trailing_anchor             = 0.0,
                sl_layer2                   = 0.0,
                realized_pnl                = 0.0,
                max_r_reached               = 0.0,
                trailing_activated_at_r     = 0.0,
                trailing_stop_at_activation = 0.0,
                be_activated_at_r           = 0.0,
                be_activated_at             = None,
                max_r_after_be              = 0.0,
                max_r_after_trailing        = 0.0,
                profit_locked_r             = 0.0,
                trailing_moves              = 0,
                management_profile          = profile.name,
                profile_tp1_r              = profile.tp1_r,
                profile_tp1_close_pct      = profile.tp1_close_pct,
                profile_break_even_r       = profile.break_even_r,
                profile_trailing_r         = profile.trailing_r,
                profile_trailing_distance_r= profile.trailing_distance_r,
                exit_reason                 = None,
                tp1_hit_at                  = None,
                max_r_reached_at            = None,
                time_to_tp1_min             = None,
                time_to_max_r_min           = None,
                profit_capture_ratio        = None,
                trade_quality_class         = quality.quality_class,
                trade_quality_score         = quality.quality_score,
                trade_quality_reason        = quality.reason,
            )
            s.add(trade)
            await s.commit()
            await s.refresh(trade)

        logger.info(
            f"📋 PENDING {card.direction} {card.symbol} size={notional:.0f} risk={risk:.2f} "
            f"entry=[{card.entry_zone_low:.4g},{card.entry_zone_high:.4g}] SL={card.stop_loss:.4g} "
            f"TP2={card.tp2:.4g} R:R={card.risk_reward:.2f} "
            f"quality={quality.quality_class}({quality.quality_score:.1f}) "
            f"profile={profile.name}"
        )
        return trade.id

    # ── Trigger ───────────────────────────────────────────────────────────────

    async def trigger(self, trade_id: int, fill_price: float, confirmed_count: int) -> None:
        async with AsyncSessionLocal() as s:
            t = await s.get(Trade, trade_id)
            if t is None or t.status != TradeStatus.PENDING.value:
                return
            slip   = 1 + self.slippage_entry + self.spread / 2
            actual = fill_price * slip if t.direction == TradeDirection.LONG.value else fill_price / slip
            t.actual_entry_price      = actual
            t.status                  = TradeStatus.TRIGGERED.value
            t.triggered_at            = datetime.utcnow()
            t.trigger_confirmed_count = confirmed_count
            t.fees_usd                = (t.position_size_usd or 0) * self.fee_pct
            if not t.initial_sl:
                t.initial_sl = t.stop_loss
            await s.commit()
        logger.info(f"⚡ TRIGGERED {t.symbol} @ {actual:.4g}")

    # ── Partial close ─────────────────────────────────────────────────────────

    async def partial_close(self, t: Trade, pct: float, price: float, reason: str) -> None:
        if pct <= 0 or pct >= 1:
            return
        entry        = t.actual_entry_price or 0
        closed_size  = (t.position_size_usd or 0) * pct
        units_closed = closed_size / entry if entry else 0
        pnl = (
            (price - entry) * units_closed
            if t.direction == TradeDirection.LONG.value
            else (entry - price) * units_closed
        )
        t.position_size_usd = (t.position_size_usd or 0) * (1 - pct)
        t.realized_pnl      = (t.realized_pnl or 0) + pnl
        t.fees_usd          = (t.fees_usd or 0) + closed_size * self.fee_pct
        t.notes = f"{t.notes or ''} | PARTIAL {pct*100:.0f}% @ {price:.4g} pnl={pnl:.2f} ({reason})"
        logger.info(
            f"📤 PARTIAL {t.symbol} {pct*100:.0f}% @ {price:.4g} "
            f"pnl={pnl:.2f} remaining={t.position_size_usd:.0f}"
        )

    # ── Close ─────────────────────────────────────────────────────────────────

    async def close(
        self, trade_id: int, exit_price: float, status: TradeStatus, reason: str
    ) -> None:
        async with AsyncSessionLocal() as s:
            t = await s.get(Trade, trade_id)
            if t is None or t.status != TradeStatus.TRIGGERED.value:
                return
            actual_exit = exit_price
            if status == TradeStatus.CLOSED_SL:
                slip        = 1 + self.slippage_stop + self.spread / 2
                actual_exit = (
                    exit_price * (1 / slip)
                    if t.direction == TradeDirection.LONG.value
                    else exit_price * slip
                )
            entry     = t.actual_entry_price or 0
            units     = (t.position_size_usd or 0) / entry if entry else 0
            pnl       = (
                (actual_exit - entry) * units
                if t.direction == TradeDirection.LONG.value
                else (entry - actual_exit) * units
            )
            total_pnl = pnl + (t.realized_pnl or 0)
            risk      = t.risk_amount_usd or 1
            pnl_r     = total_pnl / risk

            max_r = float(t.max_r_reached or 0.0)
            # Capture ratio answers: how much of the best available R did we
            # actually realize by the time the trade closed?
            if max_r > 1e-9:
                capture = max(0.0, pnl_r / max_r)
            else:
                capture = None

            if t.triggered_at and t.max_r_reached_at and t.time_to_max_r_min is None:
                t.time_to_max_r_min = int((t.max_r_reached_at - t.triggered_at).total_seconds() // 60)

            t.exit_price = actual_exit
            t.pnl_usd    = total_pnl
            t.pnl_r      = pnl_r
            t.profit_capture_ratio = capture
            t.exit_reason = reason
            if not t.management_profile:
                t.management_profile = "DEFAULT_FIXED"
            t.fees_usd   = (t.fees_usd or 0) + (t.position_size_usd or 0) * self.fee_pct
            t.status     = status.value
            t.closed_at  = datetime.utcnow()
            if reason:
                t.notes = f"{t.notes or ''} | EXIT {reason}"
            await s.commit()

            try:
                from src.alerts.telegram_bot import TelegramAlerter

                await TelegramAlerter().exit_alert(t, reason)

            except Exception as e:
                logger.warning(
                    f"Telegram exit alert failed for {t.symbol}: {e}"
                )

        max_r    = t.max_r_reached or 0.0
        trail_on = bool(t.trailing_active)
        trail_r  = t.trailing_activated_at_r or 0.0
        locked_r = t.profit_locked_r or 0.0
        capture_txt = f"{(t.profit_capture_ratio or 0.0) * 100:.1f}%" if t.profit_capture_ratio is not None else "n/a"
        logger.info(
            f"{'✅' if total_pnl >= 0 else '❌'} EXIT {t.symbol} "
            f"max_r={max_r:.2f}R close_r={pnl_r:.2f}R capture={capture_txt} "
            f"profile={t.management_profile or 'DEFAULT_FIXED'} "
            f"trail_on={trail_on}" + (f" trail_r={trail_r:.2f} locked_r={locked_r:.2f}" if trail_on else "") +
            f" reason={reason} PnL={total_pnl:.2f}"
        )
        try:
            try:
                await OutcomeLogger.log(t)
            except TypeError:
                await OutcomeLogger().log(t)
        except Exception as e:
            logger.warning(f"OutcomeLogger failed for {t.symbol}: {e}")

    # ── Startup Recovery v1 ──────────────────────────────────────────────────

    async def startup_recovery_v1(self, prices: dict[str, float]) -> dict:
        """Recover open paper trades once at bot startup.

        v1 is intentionally conservative:
        - PENDING:
          * cancel if entry was clearly missed
          * cancel if pending expired
        - TRIGGERED:
          * close if current price already hit SL
          * close if current price already reached TP2

        This prevents a trade from staying TRIGGERED after the machine was off
        while price moved beyond a decisive exit level.
        """
        stats = {
            "checked": 0,
            "closed_tp": 0,
            "closed_sl": 0,
            "cancelled": 0,
            "missing_price": 0,
            "errors": 0,
        }

        actions: list[tuple[str, int, float | None, TradeStatus | None, str]] = []

        async with AsyncSessionLocal() as s:
            res = await s.execute(
                select(Trade).where(
                    Trade.status.in_([TradeStatus.PENDING.value, TradeStatus.TRIGGERED.value])
                )
            )
            active = res.scalars().all()
            now = datetime.utcnow()
            max_age = timedelta(hours=8)

            for t in active:
                stats["checked"] += 1
                price = prices.get(t.symbol)

                if price is None:
                    stats["missing_price"] += 1
                    logger.warning(f"[Recovery v1] missing price for {t.symbol}")
                    continue

                price = float(price)

                try:
                    if t.status == TradeStatus.PENDING.value:
                        missed = self.cfg["missed_entry_max_pct"]
                        buffer = max(missed * 2, 0.003)

                        should_cancel = False
                        reason = ""

                        if t.direction == TradeDirection.LONG.value:
                            if price > t.entry_zone_high * (1 + buffer):
                                should_cancel = True
                                reason = (
                                    f"startup missed entry {price:.4g} > "
                                    f"zone {t.entry_zone_high:.4g}"
                                )
                        else:
                            if price < t.entry_zone_low * (1 - buffer):
                                should_cancel = True
                                reason = (
                                    f"startup missed entry {price:.4g} < "
                                    f"zone {t.entry_zone_low:.4g}"
                                )

                        if not should_cancel and t.created_at and (now - t.created_at) > max_age:
                            should_cancel = True
                            reason = "startup pending expired 8h"

                        if should_cancel:
                            actions.append(("cancel", int(t.id), None, None, reason))
                        continue

                    if t.status != TradeStatus.TRIGGERED.value:
                        continue

                    if t.direction == TradeDirection.LONG.value:
                        if price <= float(t.stop_loss or 0.0):
                            actions.append(
                                ("close", int(t.id), float(t.stop_loss), TradeStatus.CLOSED_SL, "startup recovery SL hit")
                            )
                        elif t.take_profit_2 and price >= float(t.take_profit_2):
                            actions.append(
                                ("close", int(t.id), float(t.take_profit_2), TradeStatus.CLOSED_TP, "startup recovery TP2 hit")
                            )
                    else:
                        if price >= float(t.stop_loss or 0.0):
                            actions.append(
                                ("close", int(t.id), float(t.stop_loss), TradeStatus.CLOSED_SL, "startup recovery SL hit")
                            )
                        elif t.take_profit_2 and price <= float(t.take_profit_2):
                            actions.append(
                                ("close", int(t.id), float(t.take_profit_2), TradeStatus.CLOSED_TP, "startup recovery TP2 hit")
                            )

                except Exception as e:
                    stats["errors"] += 1
                    logger.exception(f"[Recovery v1] failed while checking {t.symbol}: {e}")

        for action, trade_id, exit_price, status, reason in actions:
            try:
                if action == "cancel":
                    await self.cancel(trade_id, reason)
                    stats["cancelled"] += 1
                elif action == "close" and exit_price is not None and status is not None:
                    await self.close(trade_id, exit_price, status, reason)
                    if status == TradeStatus.CLOSED_TP:
                        stats["closed_tp"] += 1
                    elif status == TradeStatus.CLOSED_SL:
                        stats["closed_sl"] += 1
            except Exception as e:
                stats["errors"] += 1
                logger.exception(f"[Recovery v1] action failed trade_id={trade_id}: {e}")

        logger.info(
            "[Recovery v1] checked={checked} closed_tp={closed_tp} "
            "closed_sl={closed_sl} cancelled={cancelled} "
            "missing_price={missing_price} errors={errors}".format(**stats)
        )
        return stats

    # ── Cancel ────────────────────────────────────────────────────────────────

    async def cancel(self, trade_id: int, reason: str) -> None:
        async with AsyncSessionLocal() as s:
            t = await s.get(Trade, trade_id)
            if t is None or t.status != TradeStatus.PENDING.value:
                return
            t.status    = TradeStatus.CANCELLED.value
            t.closed_at = datetime.utcnow()
            t.notes     = f"{t.notes or ''} | CANCELLED {reason}"
            await s.commit()
        logger.info(f"🚫 CANCELLED {t.symbol} — {reason}")

    # ── Update positions (tick loop) ──────────────────────────────────────────

    async def update_positions(self, prices: dict[str, float]) -> None:
        async with AsyncSessionLocal() as s:
            res = await s.execute(
                select(Trade).where(
                    Trade.status.in_([TradeStatus.PENDING.value, TradeStatus.TRIGGERED.value])
                )
            )
            active  = res.scalars().all()
            max_age = timedelta(hours=8)
            now     = datetime.utcnow()

            for t in active:
                price = prices.get(t.symbol)
                if price is None:
                    continue

                # ── PENDING management ───────────────────────────────────────
                if t.status == TradeStatus.PENDING.value:
                    missed = self.cfg["missed_entry_max_pct"]
                    buffer = max(missed * 2, 0.003)
                    if t.direction == TradeDirection.LONG.value:
                        if price > t.entry_zone_high * (1 + buffer):
                            await self.cancel(t.id, f"missed entry {price:.4g} > zone {t.entry_zone_high:.4g}")
                            continue
                    else:
                        if price < t.entry_zone_low * (1 - buffer):
                            await self.cancel(t.id, f"missed entry {price:.4g} < zone {t.entry_zone_low:.4g}")
                            continue
                    if t.created_at and (now - t.created_at) > max_age:
                        await self.cancel(t.id, "pending expired 8h")
                        continue
                    if t.entry_zone_low <= price <= t.entry_zone_high:
                        await self.trigger(t.id, price, confirmed_count=1)
                        logger.info(f"⚡ AUTO-TRIGGERED {t.symbol} @ {price:.4g}")
                    continue

                # ── TRIGGERED management ─────────────────────────────────────
                entry      = t.actual_entry_price or 0
                initial_sl = t.initial_sl or t.stop_loss
                risk_unit  = abs(entry - initial_sl)

                if risk_unit < 1e-6:
                    if t.direction == TradeDirection.LONG.value:
                        if price <= t.stop_loss:
                            await self.close(t.id, t.stop_loss, TradeStatus.CLOSED_SL, "SL hit")
                        elif price >= t.take_profit_2:
                            await self.close(t.id, t.take_profit_2, TradeStatus.CLOSED_TP, "TP2 hit")
                    else:
                        if price >= t.stop_loss:
                            await self.close(t.id, t.stop_loss, TradeStatus.CLOSED_SL, "SL hit")
                        elif price <= t.take_profit_2:
                            await self.close(t.id, t.take_profit_2, TradeStatus.CLOSED_TP, "TP2 hit")
                    continue

                r = calc_r(entry, initial_sl, price, t.direction)
                profile = profile_from_trade(t)
                if r > (t.max_r_reached or 0.0):
                    t.max_r_reached = r
                    t.max_r_reached_at = now
                    if t.layer2_locked:
                        t.max_r_after_be = max(float(t.max_r_after_be or 0.0), r)
                    if t.trailing_active:
                        t.max_r_after_trailing = max(float(t.max_r_after_trailing or 0.0), r)
                    if t.triggered_at:
                        t.time_to_max_r_min = int((now - t.triggered_at).total_seconds() // 60)

                # TP1 — profile-driven partial close. Aggressive profiles can disable TP1.
                if profile.tp1_enabled and not t.tp1_hit and r >= profile.tp1_r:
                    await self.partial_close(t, profile.tp1_close_pct, price, f"TP1/{profile.name}")
                    t.tp1_hit = True
                    t.tp1_hit_at = now
                    if t.triggered_at:
                        t.time_to_tp1_min = int((now - t.triggered_at).total_seconds() // 60)
                    logger.info(
                        f"🎯 TP1 {t.symbol} profile={profile.name} r={r:.2f}R | "
                        f"{profile.tp1_close_pct*100:.0f}% closed @ {price:.4g}"
                    )

                # Break-even is independent from TP1 in V2.1.
                # Old behavior could delay BE until TP1 was hit. That is dangerous
                # if the profile has BE below/near TP1 or if price jumps/reverses
                # between ticks. BE should protect risk as soon as its R threshold
                # is reached.
                if not t.layer2_locked and r >= profile.break_even_r:
                    t.stop_loss         = entry
                    t.sl_layer2         = entry
                    t.layer2_locked     = True
                    t.be_activated_at_r = r
                    t.be_activated_at   = now
                    t.max_r_after_be    = max(float(t.max_r_after_be or 0.0), r)
                    t.profit_locked_r   = max(float(t.profit_locked_r or 0.0), 0.0)
                    logger.info(
                        f"🟰 BE {t.symbol} profile={profile.name} r={r:.2f}R "
                        f"SL→entry @ {entry:.4g}"
                    )

                # Trailing activation — Management V2:
                # Do not require BE to have happened in an earlier tick. If price
                # jumps directly into trailing territory, activate BE + trailing
                # in the same update so the move is not left unprotected.
                if not t.trailing_active and r >= profile.trailing_r:
                    if not t.layer2_locked:
                        t.stop_loss         = entry
                        t.sl_layer2         = entry
                        t.layer2_locked     = True
                        t.be_activated_at_r = r
                        t.be_activated_at   = now
                        t.max_r_after_be    = max(float(t.max_r_after_be or 0.0), r)
                    t.trailing_active             = True
                    t.trailing_anchor             = price
                    t.trailing_activated_at_r     = r
                    t.trailing_stop_at_activation = t.stop_loss
                    t.max_r_after_trailing        = max(float(t.max_r_after_trailing or 0.0), r)
                    logger.info(
                        f"🚀 TRAIL ON {t.symbol} profile={profile.name} r={r:.2f}R "
                        f"anchor={price:.4g} sl_snapshot={t.stop_loss:.4g}"
                    )

                # Trailing update — dynamic profit lock.
                # Old model used only anchor ± fixed distance. V2 also converts
                # the achieved MaxR into a minimum locked-R floor, reducing the
                # chance that a 3R–5R winner closes near BE or small profit.
                if t.trailing_active:
                    max_r_now = max(float(t.max_r_reached or 0.0), r)
                    lock_r = dynamic_profit_lock_r(max_r_now, profile.trailing_distance_r)
                    profit_lock_sl = price_from_r(entry, initial_sl, lock_r, t.direction)
                    if t.direction == TradeDirection.LONG.value:
                        t.trailing_anchor = max(t.trailing_anchor, price)
                        anchor_sl = t.trailing_anchor - profile.trailing_distance_r * risk_unit
                        new_sl = max(anchor_sl, profit_lock_sl)
                        if new_sl > t.stop_loss:
                            t.stop_loss = new_sl
                            t.profit_locked_r = max(float(t.profit_locked_r or 0.0), lock_r)
                            t.trailing_moves = int(t.trailing_moves or 0) + 1
                            logger.info(
                                f"📈 TRAIL MOVE {t.symbol} profile={profile.name} "
                                f"anchor={t.trailing_anchor:.4g} sl={new_sl:.4g} locked={lock_r:.2f}R"
                            )
                    else:
                        t.trailing_anchor = min(t.trailing_anchor, price)
                        anchor_sl = t.trailing_anchor + profile.trailing_distance_r * risk_unit
                        new_sl = min(anchor_sl, profit_lock_sl)
                        if new_sl < t.stop_loss:
                            t.stop_loss = new_sl
                            t.profit_locked_r = max(float(t.profit_locked_r or 0.0), lock_r)
                            t.trailing_moves = int(t.trailing_moves or 0) + 1
                            logger.info(
                                f"📉 TRAIL MOVE {t.symbol} profile={profile.name} "
                                f"anchor={t.trailing_anchor:.4g} sl={new_sl:.4g} locked={lock_r:.2f}R"
                            )

                # SL check
                if t.direction == TradeDirection.LONG.value:
                    if price <= t.stop_loss:
                        label = (
                            "Trailing SL" if t.trailing_active else
                            "BE SL"       if t.layer2_locked  else
                            "TP1 partial / base SL" if t.tp1_hit else
                            "SL hit"
                        )
                        await s.commit()
                        await self.close(t.id, t.stop_loss, TradeStatus.CLOSED_SL, label)
                else:
                    if price >= t.stop_loss:
                        label = (
                            "Trailing SL" if t.trailing_active else
                            "BE SL"       if t.layer2_locked  else
                            "TP1 partial / base SL" if t.tp1_hit else
                            "SL hit"
                        )
                        await s.commit()
                        await self.close(t.id, t.stop_loss, TradeStatus.CLOSED_SL, label)

            await s.commit()

    # ── Portfolio snapshot ────────────────────────────────────────────────────

    async def take_portfolio_snapshot(self) -> None:
        async with AsyncSessionLocal() as s:
            equity     = await self.get_equity()
            open_count = await self.get_open_count()
            kill       = await self.is_kill_switch_active()
            snap = PortfolioSnapshot(
                equity_usd         = equity,
                open_positions     = open_count,
                daily_pnl_usd      = 0.0,
                daily_pnl_pct      = 0.0,
                consecutive_losses = 0,
                kill_switch_active = kill,
            )
            s.add(snap)
            await s.commit()