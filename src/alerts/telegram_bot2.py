"""Telegram alerter v1.4

- Rich HTML formatting
- watch_zone_alert: only sends if score >= WATCH_ALERT_MIN_SCORE (55)
- datetime.utcnow() replaced with timezone-aware _now()
"""
from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import select, func as sqlfunc

from src.core.config import settings
from src.core.database import (
    AlertLog, AsyncSessionLocal, Trade, TradeOutcome, TradeStatus, WatchZone,
)
from src.core.logger import logger
from src.layers.trade_generator import TradeCard

# Minimum score to send a Watch Zone alert.
# Scores below this are silently ignored (no Telegram message sent).
# 50 -> alert if score 50-69
# 55 -> alert if score 55-69  (recommended)
# 60 -> alert if score 60-69
# 70 -> disable Watch Zone alerts entirely
WATCH_ALERT_MIN_SCORE: float = 55.0

SEP = chr(0x2500) * 13


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _score_stars(score: float) -> str:
    if score >= 80:   return chr(0x2B50)*5
    elif score >= 70: return chr(0x2B50)*4
    elif score >= 60: return chr(0x2B50)*3
    elif score >= 50: return chr(0x2B50)*2
    else:             return chr(0x2B50)


def _dir_emoji(direction: str) -> str:
    return "🔴 SHORT" if direction == "SHORT" else "🟢 LONG"


def _dir_arrow(direction: str) -> str:
    return "⬇️" if direction == "SHORT" else "⬆️"


def _regime_label(regime: str) -> str:
    m = {
        "TRENDING_UP":   "📈 Trending Up",
        "TRENDING_DOWN": "📉 Trending Down",
        "RANGING":       "↔️ Ranging",
        "VOLATILE":      "⚡ Volatile",
    }
    return m.get(regime, regime)


def _state_label(state: str) -> str:
    m = {
        "CROWDED_LONG_TRAP":      "🩤 Crowded Long Trap",
        "SHORT_SQUEEZE_SETUP":    "🧨 Short Squeeze",
        "SMART_MONEY_DIVERGENCE": "🧠 Smart Money Div.",
        "EXHAUSTION":             "😮 Exhaustion",
        "ACCUMULATION":           "🏗 Accumulation",
        "NO_SETUP":               "⚪ No Setup",
    }
    return m.get(state, state)


class TelegramAlerter:
    def __init__(self) -> None:
        self.token   = settings.env.telegram_bot_token
        self.chat_id = settings.env.telegram_chat_id
        self.enabled = (
            settings.alerts["telegram_enabled"]
            and bool(self.token) and bool(self.chat_id)
        )
        if not self.enabled:
            logger.warning("Telegram disabled (token or chat_id missing)")

    async def _send(self, text: str, alert_type: str, symbol: str) -> None:
        if not self.enabled:
            await self._log(alert_type, symbol, text, sent=False, error="disabled")
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json={
                    "chat_id":                  self.chat_id,
                    "text":                     text,
                    "parse_mode":               "HTML",
                    "disable_web_page_preview": True,
                })
                resp.raise_for_status()
            await self._log(alert_type, symbol, text, sent=True)
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            await self._log(alert_type, symbol, text, sent=False, error=str(e))

    async def _log(
        self, alert_type: str, symbol: str, message: str,
        sent: bool, error: Optional[str] = None,
    ) -> None:
        async with AsyncSessionLocal() as s:
            s.add(AlertLog(
                alert_type=alert_type, symbol=symbol,
                message=message[:2000], sent=sent, error=error,
            ))
            await s.commit()

    # ------------------------------------------------------------------
    async def setup_alert(self, card: TradeCard) -> None:
        if not settings.alerts["alert_on_setup"]:
            return

        size_label = "FULL SIZE 💯" if card.size_factor >= 1.0 else "HALF SIZE 🔸"
        stars      = _score_stars(card.setup_score)
        arrow      = _dir_arrow(card.direction)
        dir_txt    = card.direction
        sym        = card.symbol
        score      = card.setup_score
        state      = _state_label(card.market_state.value)
        regime     = _regime_label(card.market_regime.value)
        entry_lo   = card.entry_zone_low
        entry_hi   = card.entry_zone_high
        sl         = card.stop_loss
        tp1        = card.tp1
        tp2        = card.tp2
        tp3        = card.tp3
        rr         = card.risk_reward
        trigger    = html.escape(card.trigger_description)
        inval      = html.escape(card.invalidation_condition)
        win_pct    = card.estimated_success * 100
        head_emoji = "🔴" if dir_txt == "SHORT" else "🟢"
        ts         = _now().strftime("%Y-%m-%d %H:%M UTC")

        msg_lines = [
            f"{head_emoji} <b>{sym}</b>  {arrow}  <b>{dir_txt}</b>",
            SEP,
            f"🎯 <b>Quality:</b>  {stars}",
            f"📊 <b>Score:</b>  <code>{score:.1f} / 100</code>  ({size_label})",
            f"🧩 <b>Setup:</b>  {state}",
            f"🌊 <b>Regime:</b>  {regime}",
            "",
            SEP,
            "<b>📍 ENTRY / SL / TP</b>",
            SEP,
            f"🔵 <b>Entry Zone:</b>  <code>{entry_lo:.6g}</code> – <code>{entry_hi:.6g}</code>",
            f"🔴 <b>Stop Loss:</b>   <code>{sl:.6g}</code>",
            f"🟡 <b>TP1:</b>  <code>{tp1:.6g}</code>",
            f"🟢 <b>TP2:</b>  <code>{tp2:.6g}</code>",
            f"💎 <b>TP3:</b>  <code>{tp3:.6g}</code>",
            f"⚖️ <b>R:R:</b>  <code>1 : {rr:.1f}</code>",
            "",
            SEP,
            "<b>⚙️ DETAILS</b>",
            SEP,
            f"🔫 <b>Trigger:</b>  {trigger}",
            f"🚫 <b>Invalidation:</b>  {inval}",
            f"🎲 <b>Est. Win Rate:</b>  <code>{win_pct:.0f}%</code>",
            "",
            f"🕐 <i>{ts}</i>",
            SEP,
            "<i>HunterBot · Paper Mode</i>",
        ]
        await self._send("\n".join(msg_lines), "SETUP", card.symbol)

    # ------------------------------------------------------------------
    async def trigger_alert(self, card: TradeCard, fill_price: float) -> None:
        if not settings.alerts["alert_on_trigger"]:
            return
        arrow   = _dir_arrow(card.direction)
        sym     = card.symbol
        dir_txt = card.direction
        sl      = card.stop_loss
        tp2     = card.tp2
        tp3     = card.tp3
        msg_lines = [
            f"🎯 <b>TRIGGERED</b>  {arrow}  <b>{sym}</b>",
            SEP,
            f"✅ <b>{dir_txt}</b> filled at  <code>{fill_price:.6g}</code>",
            f"🔴 <b>SL:</b>   <code>{sl:.6g}</code>",
            f"🟢 <b>TP2:</b>  <code>{tp2:.6g}</code>",
            f"💎 <b>TP3:</b>  <code>{tp3:.6g}</code>",
            "",
            "<i>Position is now live — managing risk.</i>",
        ]
        await self._send("\n".join(msg_lines), "TRIGGER", card.symbol)

    # ------------------------------------------------------------------
    async def exit_alert(
        self, symbol: str, status: str,
        exit_price: float, pnl_r: float, pnl_usd: float,
    ) -> None:
        if not settings.alerts["alert_on_exit"]:
            return
        result_line = "✅ <b>WIN</b>  🎉" if pnl_usd > 0 else "❌ <b>LOSS</b>"
        sign = "+" if pnl_usd >= 0 else ""
        msg_lines = [
            f"🏁 <b>CLOSED</b>  ·  <b>{symbol}</b>",
            SEP,
            result_line,
            f"📌 <b>Status:</b>   {status}",
            f"💰 <b>Exit:</b>     <code>{exit_price:.6g}</code>",
            f"📈 <b>P/L:</b>      <code>{sign}{pnl_usd:.2f} USD</code>  (<code>{sign}{pnl_r:.2f}R</code>)",
            "",
            "<i>HunterBot · Paper Mode</i>",
        ]
        await self._send("\n".join(msg_lines), "EXIT", symbol)

    # ------------------------------------------------------------------
    async def watch_zone_alert(
        self, symbol: str, score: float, direction: str, state: str
    ) -> None:
        """Send Watch Zone alert only if score >= WATCH_ALERT_MIN_SCORE.
        Scores below the threshold are silently ignored to reduce noise.
        """
        if score < WATCH_ALERT_MIN_SCORE:
            logger.debug(
                f"Watch Zone suppressed for {symbol} "
                f"(score={score:.1f} < {WATCH_ALERT_MIN_SCORE})"
            )
            return

        stars      = _score_stars(score)
        dir_line   = _dir_emoji(direction)
        state_line = _state_label(state)
        msg_lines = [
            f"👀 <b>WATCH ZONE</b>  ·  <b>{symbol}</b>",
            SEP,
            f"📊 <b>Score:</b>    <code>{score:.1f} / 100</code>  {stars}",
            f"🧭 <b>Direction:</b> {dir_line}",
            f"🧩 <b>Setup:</b>    {state_line}",
            "",
            "⏳ <i>Below execution threshold — monitoring only.</i>",
            "<i>Will execute if score improves next cycle.</i>",
        ]
        await self._send("\n".join(msg_lines), "WATCH", symbol)

    # ------------------------------------------------------------------
    async def weekly_report(self) -> None:
        since = _now() - timedelta(days=7)

        async with AsyncSessionLocal() as s:
            res = await s.execute(
                select(Trade).where(
                    Trade.status.in_([TradeStatus.CLOSED_TP.value, TradeStatus.CLOSED_SL.value]),
                    Trade.closed_at >= since,
                )
            )
            trades = res.scalars().all()

            wz_res   = await s.execute(
                select(sqlfunc.count()).select_from(WatchZone)
                .where(WatchZone.timestamp >= since)
            )
            wz_count = wz_res.scalar() or 0

            out_res  = await s.execute(
                select(TradeOutcome).where(TradeOutcome.closed_at >= since)
            )
            outcomes = out_res.scalars().all()

        ts_start = since.strftime("%b %d")
        ts_end   = _now().strftime("%b %d, %Y")

        if not trades:
            msg_lines = [
                "📊 <b>Weekly Report — HunterBot</b>",
                SEP,
                f"🗓 <i>{ts_start} → {ts_end}</i>",
                "",
                "No trades closed this week.",
                f"👀 Watch-Zone signals monitored: <code>{wz_count}</code>",
            ]
            await self._send("\n".join(msg_lines), "REPORT", "system")
            return

        wins      = [t for t in trades if (t.pnl_usd or 0) > 0]
        losses    = [t for t in trades if (t.pnl_usd or 0) <= 0]
        win_rate  = len(wins) / len(trades) * 100
        total_pnl = sum((t.pnl_usd or 0) for t in trades)
        avg_win_r = sum((t.pnl_r or 0) for t in wins)   / len(wins)   if wins   else 0.0
        avg_los_r = sum((t.pnl_r or 0) for t in losses) / len(losses) if losses else 0.0
        best      = max(trades, key=lambda t: t.pnl_usd or 0)
        worst     = min(trades, key=lambda t: t.pnl_usd or 0)

        def _avg_layer(outs: list, key: str) -> float:
            vals = [(o.layer_scores or {}).get(key, 0.0) for o in outs if o.layer_scores]
            return sum(vals) / len(vals) if vals else 0.0

        win_out  = [o for o in outcomes if o.result == "WIN"]
        loss_out = [o for o in outcomes if o.result == "LOSS"]
        layer_map = {
            "liquidity_imbalance":     "Liquidity",
            "positioning_extremity":   "Positioning",
            "oi_behavior":             "OI Behavior",
            "funding_extreme":         "Funding",
            "price_action_confluence": "Price Action",
        }

        pnl_sign   = "+" if total_pnl >= 0 else ""
        best_sign  = "+" if (best.pnl_usd  or 0) >= 0 else ""
        worst_sign = "+" if (worst.pnl_usd or 0) >= 0 else ""

        msg_lines = [
            "📊 <b>Weekly Report — HunterBot</b>",
            SEP,
            f"🗓 <i>{ts_start} → {ts_end}</i>",
            "",
            "<b>📈 PERFORMANCE</b>",
            SEP,
            f"✅ <b>Wins:</b>      <code>{len(wins)}</code>",
            f"❌ <b>Losses:</b>    <code>{len(losses)}</code>",
            f"🎯 <b>Win Rate:</b>  <code>{win_rate:.1f}%</code>",
            f"💰 <b>Total P/L:</b> <code>{pnl_sign}{total_pnl:.2f} USD</code>",
            f"⚡ <b>Avg Win:</b>  <code>{avg_win_r:+.2f}R</code>  ·  <b>Avg Loss:</b> <code>{avg_los_r:+.2f}R</code>",
            "",
            f"🏆 <b>Best:</b>   {best.symbol}  <code>{best_sign}{(best.pnl_usd or 0):.2f} USD</code>  (<code>{(best.pnl_r or 0):+.2f}R</code>)",
            f"💀 <b>Worst:</b>  {worst.symbol}  <code>{worst_sign}{(worst.pnl_usd or 0):.2f} USD</code>  (<code>{(worst.pnl_r or 0):+.2f}R</code>)",
            f"👀 <b>Watch-Zone signals:</b>  <code>{wz_count}</code>",
        ]

        if win_out or loss_out:
            msg_lines += ["", "<b>🔬 Layer Attribution (Wins vs Losses)</b>", SEP]
            for k, label in layer_map.items():
                w     = _avg_layer(win_out, k)
                l     = _avg_layer(loss_out, k)
                arrow = "🟢" if w > l + 2 else ("🔴" if l > w + 2 else "⚪")
                msg_lines.append(
                    f"{arrow} <b>{label}:</b>  W=<code>{w:.0f}</code>  L=<code>{l:.0f}</code>"
                )

        msg_lines += ["", "<i>HunterBot · Paper Mode</i>"]
        await self._send("\n".join(msg_lines), "REPORT", "system")

    # ------------------------------------------------------------------
    async def info(self, message: str) -> None:
        msg_lines = [
            "ℹ️ <b>HunterBot</b>",
            SEP,
            html.escape(message),
        ]
        await self._send("\n".join(msg_lines), "INFO", "system")
