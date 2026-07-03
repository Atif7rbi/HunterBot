"""HunterBot Bot — NiceGUI Dashboard v2.0 (Sniper Radar Style)"""
from __future__ import annotations
import sys
import asyncio
import httpx
import os
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from urllib.parse import quote
from nicegui import app, ui
from pathlib import Path

# When started with `python -m ui.app`, this file is loaded as `__main__`.
# Keep `ui.app` pointing to the same module instance so ui.pages imports the
# live bot/helpers from here instead of creating a second duplicate app module.
sys.modules.setdefault("ui.app", sys.modules[__name__])

app.add_static_file(
    local_file=str(Path(__file__).resolve().parent / 'Dashboard' / 'dashboard.html'),
    url_path='/dashboard.html',
)


from sqlalchemy import case, desc, func, select, true
from src.core.config import settings
from src.core.database import (
    AsyncSessionLocal,
    PortfolioSnapshot,
    RejectedSignal,
    ScanSnapshot,
    Trade,
    TradeStatus,
    init_db,
)
from src.core.logger import logger
from src.core.live_price import LivePriceTracker
from src.learning.performance_analyzer import ManagementAuditAnalyzer
from src.alerts.telegram_bot import TelegramAlerter
from src.layers.paper_executor import PaperExecutor
from src.main import LiquidityHunterBot
from ui.components.widgets import (
    direction_pill,
    empty_state,
    fmt_money_short,
    fmt_pct,
    fmt_price,
    kpi_card,
    regime_pill,
    scan_progress_bar,
    score_bar,
    score_bar_cell,
    section_header,
    state_pill,
)
from ui.theme import COLORS, GLOBAL_CSS

_report_trade_limit: str = "20"
_rejected_report_limit: str = "50"
bot = LiquidityHunterBot()
executor = PaperExecutor()
live_prices = LivePriceTracker(refresh_seconds=15)
_running_lock = asyncio.Lock()
_uptime_start = datetime.now()
_scan_state: dict = {"step": 0, "counts": {}}
_scan_version: int = 0
SA_TZ = ZoneInfo("Asia/Riyadh")
#_report_trade_limit: str = "20"


def _cfg_section(name: str, default: dict | None = None) -> dict:
    default = default or {}
    try:
        section = getattr(settings, name)
        if isinstance(section, dict):
            return section
        return dict(section) if section else default
    except Exception:
        pass
    try:
        section = settings.section(name)
        if isinstance(section, dict):
            return section
        return dict(section) if section else default
    except Exception:
        return default


TRADE_STATUS_LABELS = {
    "PENDING": "Pending",
    "TRIGGERED": "Open",
    "CANCELLED": "Cancelled",
    "EXPIRED": "Expired",
    "CLOSED_TP": "TP",
    "CLOSED_SL": "SL",
}


def _safe_notify(message: str, **kwargs) -> None:
    try:
        ui.notify(message, **kwargs)
    except Exception:
        pass


def _uptime_str() -> str:
    elapsed = datetime.now() - _uptime_start
    h, r = divmod(int(elapsed.total_seconds()), 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"



def _saudi_dt(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert a datetime to Saudi Arabia time for UI display only."""
    if not dt:
        return None

    try:
        from datetime import timezone

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(SA_TZ)

    except Exception:
        return dt


def _saudi_time(dt: Optional[datetime], fmt: str = "%m/%d %H:%M") -> str:
    converted = _saudi_dt(dt)
    if not converted:
        return "—"

    try:
        return converted.strftime(fmt)
    except Exception:
        return str(dt)


def _saudi_now(fmt: str = "%Y-%m-%d %H:%M") -> str:
    return datetime.now(SA_TZ).strftime(fmt)


# def _status_text(status: str | None) -> str:
#     raw = getattr(status, 'value', status)
#     raw = str(raw or '')
#     if raw.startswith('TradeStatus.'):
#         raw = raw.split('.', 1)[1]
#
#     if raw == "CLOSED_TP":
#         return "TP"
#     if raw == "CLOSED_SL":
#         return "SL"
#
#     return raw
#
#
# def _status_pill(status: str | None) -> str:
#     raw = str(status or "")
#     if raw.startswith("TradeStatus."):
#         raw = raw.split(".", 1)[1]
#     label = _status_text(raw)
#     if raw == "CLOSED_TP":
#         cls = "pill pill-long"
#     elif raw == "CLOSED_SL":
#         cls = "pill pill-short"
#     elif raw in {"CANCELLED", "EXPIRED"}:
#         cls = "pill pill-info-soft"
#     elif raw == "TRIGGERED":
#         cls = "pill pill-open"
#     elif raw == "PENDING":
#         cls = "pill pill-pending"
#     else:
#         cls = "pill pill-muted"
#     return f'<span class="{cls}">{label}</span>'


def _status_text(status: str | None, pnl: float = 0.0) -> str:
    raw = getattr(status, 'value', status)
    raw = str(raw or '')
    if raw.startswith('TradeStatus.'):
        raw = raw.split('.', 1)[1]

    if raw == "CLOSED_TP":
        return "TP"
    if raw == "CLOSED_SL":
        if pnl > 0:
            return "SL+"
        if pnl == 0:
            return "SL BE"
        return "SL"

    return raw


def _status_pill(t: Trade) -> str:
    raw = getattr(t.status, 'value', t.status)
    raw = str(raw or '')
    if raw.startswith("TradeStatus."):
        raw = raw.split(".", 1)[1]

    label = _status_text(raw, t.pnl_usd or 0.0)

    if raw == "CLOSED_TP":
        cls = "pill pill-long"
    elif raw == "CLOSED_SL" and (t.pnl_usd or 0.0) > 0:
        cls = "pill pill-open"
    elif raw == "CLOSED_SL" and (t.pnl_usd or 0.0) == 0:
        cls = "pill pill-info-soft"
    elif raw == "CLOSED_SL":
        cls = "pill pill-short"
    elif raw in {"CANCELLED", "EXPIRED"}:
        cls = "pill pill-info-soft"
    elif raw == "TRIGGERED":
        cls = "pill pill-open"
    elif raw == "PENDING":
        cls = "pill pill-pending"
    else:
        cls = "pill pill-muted"

    return f'<span class="{cls}">{label}</span>'

def _event_time_label(t: Trade) -> str:
    if t.closed_at:
        return _saudi_time(t.closed_at)
    if t.triggered_at:
        return _saudi_time(t.triggered_at)
    if t.created_at:
        return _saudi_time(t.created_at)
    return "—"


def _dir_text(direction: object) -> str:
    raw = getattr(direction, 'value', direction)
    raw = str(raw or '')
    if raw.startswith('TradeDirection.'):
        raw = raw.split('.', 1)[1]
    return raw

def _rejection_category_text(category: object) -> str:
    raw = getattr(category, 'value', category)
    raw = str(raw or '')
    if raw.startswith('RejectionCategory.'):
        raw = raw.split('.', 1)[1]
    return raw.replace('_', ' ').title() if raw else '—'

def _trade_entry_price(t: Trade) -> float:
    if t.actual_entry_price is not None:
        return t.actual_entry_price
    return (t.entry_zone_low + t.entry_zone_high) / 2


def _trade_outcome_class(t: Trade) -> str:
    pnl = t.pnl_usd or 0.0
    if str(t.status) == "CLOSED_TP" or pnl > 0:
        return "text-success"
    if str(t.status) in {"CLOSED_SL", "CANCELLED", "EXPIRED"} or pnl < 0:
        return "text-danger"
    return "text-muted"


def _trade_stage_text(t: Trade) -> str:
    parts: list[str] = []
    if t.created_at:
        parts.append(f"Created {_saudi_time(t.created_at)}")
    if t.triggered_at:
        parts.append(f"Triggered {_saudi_time(t.triggered_at)}")
    if t.closed_at:
        parts.append(f"Closed {_saudi_time(t.closed_at)}")
    return "  →  ".join(parts) if parts else "No timestamps"


def _timeline_event(title: str, ts: Optional[datetime], cls: str, detail: str) -> str:
    if not ts:
        return ""
    stamp = _saudi_time(ts, "%Y-%m-%d %H:%M:%S")
    return (
        '<div class="timeline-item">'
        f'<div class="timeline-dot {cls}"></div>'
        '<div class="timeline-content">'
        f'<div class="timeline-head"><span class="timeline-title">{title}</span>'
        f'<span class="timeline-time">{stamp}</span></div>'
        f'<div class="timeline-detail">{detail}</div>'
        '</div></div>'
    )


def _trade_details_html(t: Trade) -> str:
    notes = t.notes or "—"
    layer_scores = t.layer_scores or {}
    layer_html = ""
    if layer_scores:
        items = "".join(
            f'<div class="detail-chip"><span>{k}</span><strong>{v}</strong></div>'
            for k, v in layer_scores.items()
        )
        layer_html = f'<div class="detail-block"><div class="detail-label">Layer Scores</div><div class="detail-chip-row">{items}</div></div>'

    timeline_html = "".join(
        x for x in [
            _timeline_event("Created", t.created_at, "dot-created", f"Setup score {t.setup_score:.1f}"),
            _timeline_event("Triggered", t.triggered_at, "dot-open", f"Entry {fmt_price(_trade_entry_price(t))}"),
            _timeline_event("Closed", t.closed_at, "dot-closed", f"Status {_status_text(str(t.status))} · Exit {fmt_price(t.exit_price or 0)}"),
        ]
        if x
    ) or '<div class="text-muted" style="font-size:12px;">No timeline events recorded.</div>'

    return f'''
    <div class="trade-detail-grid">
      <div class="detail-block">
        <div class="detail-label">Lifecycle</div>
        <div class="detail-value">{_trade_stage_text(t)}</div>
      </div>
      <div class="detail-block">
        <div class="detail-label">Setup</div>
        <div class="detail-value">{t.trigger_description}</div>
      </div>
      <div class="detail-block">
        <div class="detail-label">Invalidation</div>
        <div class="detail-value">{t.invalidation_condition}</div>
      </div>
      <div class="detail-block">
        <div class="detail-label">Market Context</div>
        <div class="detail-value">{str(t.market_state)} · {str(t.market_regime)}</div>
      </div>
      <div class="detail-block">
        <div class="detail-label">Entry Zone</div>
        <div class="detail-value">{fmt_price(t.entry_zone_low)} → {fmt_price(t.entry_zone_high)}</div>
      </div>
      <div class="detail-block">
        <div class="detail-label">Execution</div>
        <div class="detail-value">Entry {fmt_price(_trade_entry_price(t))} · Exit {fmt_price(t.exit_price or 0)}</div>
      </div>
      <div class="detail-block">
        <div class="detail-label">Risk</div>
        <div class="detail-value">SL {fmt_price(t.stop_loss)} · RR {t.risk_reward_ratio:.2f} · Risk ${t.risk_amount_usd:,.2f}</div>
      </div>
      <div class="detail-block">
        <div class="detail-label">Targets</div>
        <div class="detail-value">TP1 {fmt_price(t.take_profit_1)} · TP2 {fmt_price(t.take_profit_2)} · TP3 {fmt_price(t.take_profit_3)}</div>
      </div>
      <div class="detail-block">
        <div class="detail-label">Performance</div>
        <div class="detail-value">PnL ${(t.pnl_usd or 0):+,.2f} · {(t.pnl_r or 0):+.2f}R · Fees ${(t.fees_usd or 0):,.2f}</div>
      </div>
      <div class="detail-block">
        <div class="detail-label">Excursion</div>
        <div class="detail-value">MAE {fmt_pct(t.mae_pct or 0)} · MFE {fmt_pct(t.mfe_pct or 0)} · Max R {(t.max_r_reached or 0):.2f}</div>
      </div>
      <div class="detail-block">
        <div class="detail-label">Management</div>
        <div class="detail-value">TP1 hit {'Yes' if t.tp1_hit else 'No'} · Trailing {'On' if t.trailing_active else 'Off'} · Migrated {'Yes' if t.is_migrated else 'No'}</div>
      </div>
      <div class="detail-block">
        <div class="detail-label">Notes</div>
        <div class="detail-value">{notes}</div>
      </div>
      {layer_html}
      <div class="detail-block detail-block-wide">
        <div class="detail-label">Timeline</div>
        <div class="timeline-wrap">{timeline_html}</div>
      </div>
    </div>
    '''


def _details_link(symbol: str) -> str:
    return (
        f'<a href="/symbol/{quote(symbol)}" class="details-link">'
        'More details'
        '</a>'
    )


async def generate_and_send_pdf_report(trade_limit: str | int | None = None) -> tuple[bool, str]:
    """Build a PDF summary of current performance and send it via Telegram."""
    import io
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    try:
        equity = await executor.get_equity() if hasattr(executor, 'get_equity') else 0.0
        open_count = await executor.get_open_count() if hasattr(executor, 'get_open_count') else 0
        kill = await executor.is_kill_switch_active() if hasattr(executor, 'is_kill_switch_active') else False
        initial = getattr(executor, 'initial_capital', 0) or 0
        eq_pct = (equity - initial) / initial * 100 if initial else 0

        selected_limit = _normalize_report_trade_limit(trade_limit if trade_limit is not None else _report_trade_limit)
        limit_value = None if selected_limit.lower() == 'all' else int(selected_limit)

        async with AsyncSessionLocal() as s:
            closed_stmt = (
                select(Trade)
                .where(Trade.status.in_([TradeStatus.CLOSED_TP, TradeStatus.CLOSED_SL]))
                .order_by(desc(Trade.closed_at))
            )
            cancelled_stmt = (
                select(Trade)
                .where(Trade.status.in_([TradeStatus.CANCELLED, TradeStatus.EXPIRED]))
                .order_by(desc(Trade.closed_at), desc(Trade.created_at))
            )

            if limit_value is not None:
                closed_stmt = closed_stmt.limit(limit_value)
                cancelled_stmt = cancelled_stmt.limit(limit_value)

            closed_res = await s.execute(closed_stmt)
            closed = list(closed_res.scalars().all())

            cancelled_res = await s.execute(cancelled_stmt)
            cancelled = list(cancelled_res.scalars().all())

        wins = [t for t in closed if (t.pnl_usd or 0) > 0]
        losses = [t for t in closed if (t.pnl_usd or 0) <= 0]
        win_rate = len(wins) / len(closed) * 100 if closed else 0.0
        total_pnl = sum((t.pnl_usd or 0) for t in closed)
        now_str = _saudi_now('%Y-%m-%d %H:%M')

        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf,
            pagesize=A4,
            leftMargin=2 * cm,
            rightMargin=2 * cm,
            topMargin=2 * cm,
            bottomMargin=2 * cm,
        )
        styles = getSampleStyleSheet()
        title_s = ParagraphStyle('title', parent=styles['Title'], fontSize=18, spaceAfter=6)
        sub_s = ParagraphStyle('sub', parent=styles['Normal'], fontSize=10, textColor=rl_colors.gray, spaceAfter=12)
        hdr_s = ParagraphStyle('hdr', parent=styles['Heading2'], fontSize=12, spaceBefore=14, spaceAfter=6)

        story = [
            Paragraph('HunterBot — Report', title_s),
            Paragraph(f'Generated: {now_str}  |  Paper Mode', sub_s),
            Spacer(1, 0.3 * cm),
            Paragraph('Portfolio Summary', hdr_s),
        ]

        eq_sign = '+' if eq_pct >= 0 else ''
        pnl_sign = '+' if total_pnl >= 0 else ''
        kpi_data = [
            ['Metric', 'Value'],
            ['Equity', f'${equity:,.0f}  ({eq_sign}{eq_pct:.2f}%)'],
            ['Realized P/L', f'${pnl_sign}{total_pnl:,.2f}'],
            ['Win Rate', f'{win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)'],
            ['Open Positions', str(open_count)],
            ['Kill Switch', 'ARMED' if kill else 'SAFE'],
        ]
        kpi_tbl = Table(kpi_data, colWidths=[6 * cm, 10 * cm])
        kpi_tbl.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), rl_colors.HexColor('#1a1a2e')),
            ('TEXTCOLOR', (0, 0), (-1, 0), rl_colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [rl_colors.HexColor('#f9f9f9'), rl_colors.white]),
            ('GRID', (0, 0), (-1, -1), 0.4, rl_colors.HexColor('#cccccc')),
            ('LEFTPADDING', (0, 0), (-1, -1), 5),
            ('RIGHTPADDING', (0, 0), (-1, -1), 5),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ]))
        story.append(kpi_tbl)
        story.append(Spacer(1, 0.4 * cm))

        # Management Audit — analytics-only section
        try:
            audit = await ManagementAuditAnalyzer().analyze(limit_value)
            summary = audit.get("summary", {})
            audit_trades = int(summary.get("trades", 0) or 0)
            if audit_trades:
                story.append(Paragraph('Management Audit', hdr_s))
                avg_capture = summary.get("avg_capture")
                avg_leakage = summary.get("avg_leakage")
                audit_data = [
                    ['Metric', 'Value'],
                    ['Analyzed Trades', str(audit_trades)],
                    ['Avg MaxR', f'{float(summary.get("avg_max_r", 0) or 0):+.2f}R'],
                    ['Avg FinalR', f'{float(summary.get("avg_final_r", 0) or 0):+.2f}R'],
                    ['Avg Capture', f'{avg_capture * 100:.1f}%' if avg_capture is not None else 'n/a'],
                    ['Profit Leakage', f'{avg_leakage * 100:.1f}%' if avg_leakage is not None else 'n/a'],
                ]
                audit_tbl = Table(audit_data, colWidths=[6 * cm, 10 * cm])
                audit_tbl.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), rl_colors.HexColor('#1a1a2e')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), rl_colors.white),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, -1), 9),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [rl_colors.HexColor('#f9f9f9'), rl_colors.white]),
                    ('GRID', (0, 0), (-1, -1), 0.3, rl_colors.HexColor('#cccccc')),
                    ('LEFTPADDING', (0, 0), (-1, -1), 5),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 5),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ]))
                story.append(audit_tbl)
                story.append(Spacer(1, 0.25 * cm))

                profile_rows = audit.get("profiles", [])[:6]
                if profile_rows:
                    story.append(Paragraph('Management Profile Performance', hdr_s))
                    rows = [['Profile', 'Trades', 'Win%', 'Avg MaxR', 'Avg FinalR', 'Capture', 'Leakage']]
                    for r in profile_rows:
                        cap = r.get("avg_capture")
                        leak = r.get("avg_leakage")
                        rows.append([
                            str(r.get("name", '—')),
                            str(r.get("trades", 0)),
                            f'{float(r.get("win_rate", 0) or 0) * 100:.1f}%',
                            f'{float(r.get("avg_max_r", 0) or 0):+.2f}R',
                            f'{float(r.get("avg_final_r", 0) or 0):+.2f}R',
                            f'{cap * 100:.1f}%' if cap is not None else 'n/a',
                            f'{leak * 100:.1f}%' if leak is not None else 'n/a',
                        ])
                    p_tbl = Table(rows, colWidths=[3.0 * cm, 1.6 * cm, 1.7 * cm, 2.2 * cm, 2.2 * cm, 2.0 * cm, 2.0 * cm])
                    p_tbl.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (-1, 0), rl_colors.HexColor('#1a1a2e')),
                        ('TEXTCOLOR', (0, 0), (-1, 0), rl_colors.white),
                        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                        ('FONTSIZE', (0, 0), (-1, -1), 7.5),
                        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [rl_colors.HexColor('#f9f9f9'), rl_colors.white]),
                        ('GRID', (0, 0), (-1, -1), 0.3, rl_colors.HexColor('#cccccc')),
                    ]))
                    story.append(p_tbl)
                    story.append(Spacer(1, 0.25 * cm))

                quality_rows = audit.get("qualities", [])[:6]
                if quality_rows:
                    story.append(Paragraph('Quality Class Performance', hdr_s))
                    rows = [['Class', 'Trades', 'Win%', 'Avg MaxR', 'Avg FinalR', 'Capture']]
                    for r in quality_rows:
                        cap = r.get("avg_capture")
                        rows.append([
                            str(r.get("name", '—')),
                            str(r.get("trades", 0)),
                            f'{float(r.get("win_rate", 0) or 0) * 100:.1f}%',
                            f'{float(r.get("avg_max_r", 0) or 0):+.2f}R',
                            f'{float(r.get("avg_final_r", 0) or 0):+.2f}R',
                            f'{cap * 100:.1f}%' if cap is not None else 'n/a',
                        ])
                    q_tbl = Table(rows, colWidths=[2.4 * cm, 1.8 * cm, 2.0 * cm, 2.4 * cm, 2.4 * cm, 2.4 * cm])
                    q_tbl.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (-1, 0), rl_colors.HexColor('#1a1a2e')),
                        ('TEXTCOLOR', (0, 0), (-1, 0), rl_colors.white),
                        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                        ('FONTSIZE', (0, 0), (-1, -1), 8),
                        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [rl_colors.HexColor('#f9f9f9'), rl_colors.white]),
                        ('GRID', (0, 0), (-1, -1), 0.3, rl_colors.HexColor('#cccccc')),
                    ]))
                    story.append(q_tbl)
                    story.append(Spacer(1, 0.25 * cm))

                exit_rows = audit.get("exits", [])[:8]
                if exit_rows:
                    story.append(Paragraph('Exit Reasons', hdr_s))
                    rows = [['Reason', 'Count']]
                    for r in exit_rows:
                        rows.append([str(r.get("reason", '—')), str(r.get("count", 0))])
                    e_tbl = Table(rows, colWidths=[10 * cm, 3 * cm])
                    e_tbl.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (-1, 0), rl_colors.HexColor('#1a1a2e')),
                        ('TEXTCOLOR', (0, 0), (-1, 0), rl_colors.white),
                        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                        ('FONTSIZE', (0, 0), (-1, -1), 8),
                        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [rl_colors.HexColor('#f9f9f9'), rl_colors.white]),
                        ('GRID', (0, 0), (-1, -1), 0.3, rl_colors.HexColor('#cccccc')),
                    ]))
                    story.append(e_tbl)
                    story.append(Spacer(1, 0.25 * cm))

                trailing_audit = audit.get("trailing_audit", {}) or {}
                if int(trailing_audit.get("count", 0) or 0):
                    story.append(Paragraph('Trailing Audit', hdr_s))
                    rows = [
                        ['Metric', 'Value'],
                        ['Trailing Trades', str(int(trailing_audit.get("count", 0) or 0))],
                        ['Avg Activation', f'{float(trailing_audit.get("avg_activation_r", 0) or 0):+.2f}R'],
                        ['Avg Max After Trail', f'{float(trailing_audit.get("avg_max_after_trailing", 0) or 0):+.2f}R'],
                        ['Avg FinalR', f'{float(trailing_audit.get("avg_final_r", 0) or 0):+.2f}R'],
                        ['Avg Lost After Trail', f'{float(trailing_audit.get("avg_lost_after_trailing", 0) or 0):+.2f}R'],
                        ['Avg Locked Profit', f'{float(trailing_audit.get("avg_profit_locked_r", 0) or 0):+.2f}R'],
                    ]
                    tr_tbl = Table(rows, colWidths=[6 * cm, 10 * cm])
                    tr_tbl.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (-1, 0), rl_colors.HexColor('#1a1a2e')),
                        ('TEXTCOLOR', (0, 0), (-1, 0), rl_colors.white),
                        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                        ('FONTSIZE', (0, 0), (-1, -1), 8),
                        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [rl_colors.HexColor('#f9f9f9'), rl_colors.white]),
                        ('GRID', (0, 0), (-1, -1), 0.3, rl_colors.HexColor('#cccccc')),
                    ]))
                    story.append(tr_tbl)
                    story.append(Spacer(1, 0.20 * cm))

                    worst_trailing = trailing_audit.get("worst", [])[:6]
                    if worst_trailing:
                        rows = [['Symbol', 'Profile', 'Trail@', 'MaxAfter', 'FinalR', 'Lost']]
                        for r in worst_trailing:
                            rows.append([
                                str(r.get("symbol", '—')),
                                str(r.get("profile", '—')),
                                f'{float(r.get("trailing_activated_at_r", 0) or 0):+.2f}R',
                                f'{float(r.get("max_r_after_trailing", 0) or 0):+.2f}R',
                                f'{float(r.get("final_r", 0) or 0):+.2f}R',
                                f'{float(r.get("lost_after_trailing", 0) or 0):+.2f}R',
                            ])
                        wt_tbl = Table(rows, colWidths=[2.6 * cm, 3.0 * cm, 1.8 * cm, 2.1 * cm, 1.8 * cm, 1.8 * cm])
                        wt_tbl.setStyle(TableStyle([
                            ('BACKGROUND', (0, 0), (-1, 0), rl_colors.HexColor('#1a1a2e')),
                            ('TEXTCOLOR', (0, 0), (-1, 0), rl_colors.white),
                            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                            ('FONTSIZE', (0, 0), (-1, -1), 7.5),
                            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [rl_colors.HexColor('#f9f9f9'), rl_colors.white]),
                            ('GRID', (0, 0), (-1, -1), 0.3, rl_colors.HexColor('#cccccc')),
                        ]))
                        story.append(wt_tbl)
                        story.append(Spacer(1, 0.25 * cm))

                be_audit = audit.get("be_audit", {}) or {}
                if int(be_audit.get("count", 0) or 0):
                    story.append(Paragraph('Break-Even Audit', hdr_s))
                    rows = [
                        ['Metric', 'Value'],
                        ['BE Trades', str(int(be_audit.get("count", 0) or 0))],
                        ['Avg BE Activation', f'{float(be_audit.get("avg_activation_r", 0) or 0):+.2f}R'],
                        ['Avg Max After BE', f'{float(be_audit.get("avg_max_after_be", 0) or 0):+.2f}R'],
                        ['Avg FinalR', f'{float(be_audit.get("avg_final_r", 0) or 0):+.2f}R'],
                        ['Avg Lost After BE', f'{float(be_audit.get("avg_lost_after_be", 0) or 0):+.2f}R'],
                    ]
                    be_tbl = Table(rows, colWidths=[6 * cm, 10 * cm])
                    be_tbl.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (-1, 0), rl_colors.HexColor('#1a1a2e')),
                        ('TEXTCOLOR', (0, 0), (-1, 0), rl_colors.white),
                        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                        ('FONTSIZE', (0, 0), (-1, -1), 8),
                        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [rl_colors.HexColor('#f9f9f9'), rl_colors.white]),
                        ('GRID', (0, 0), (-1, -1), 0.3, rl_colors.HexColor('#cccccc')),
                    ]))
                    story.append(be_tbl)
                    story.append(Spacer(1, 0.25 * cm))

                lost_rows = audit.get("lost_winners", [])[:8]
                if lost_rows:
                    story.append(Paragraph('Biggest Lost Winners', hdr_s))
                    rows = [['Symbol', 'Dir', 'Profile', 'MaxR', 'FinalR', 'LostR', 'Capture']]
                    for r in lost_rows:
                        cap = r.get("capture")
                        rows.append([
                            str(r.get("symbol", '—')),
                            str(r.get("direction", '—')),
                            str(r.get("profile", '—')),
                            f'{float(r.get("max_r", 0) or 0):+.2f}R',
                            f'{float(r.get("final_r", 0) or 0):+.2f}R',
                            f'{float(r.get("lost_r", 0) or 0):+.2f}R',
                            f'{cap * 100:.1f}%' if cap is not None else 'n/a',
                        ])
                    l_tbl = Table(rows, colWidths=[2.5 * cm, 1.2 * cm, 3.0 * cm, 1.8 * cm, 1.8 * cm, 1.8 * cm, 2.0 * cm])
                    l_tbl.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (-1, 0), rl_colors.HexColor('#1a1a2e')),
                        ('TEXTCOLOR', (0, 0), (-1, 0), rl_colors.white),
                        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                        ('FONTSIZE', (0, 0), (-1, -1), 7.5),
                        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [rl_colors.HexColor('#f9f9f9'), rl_colors.white]),
                        ('GRID', (0, 0), (-1, -1), 0.3, rl_colors.HexColor('#cccccc')),
                    ]))
                    story.append(l_tbl)
                    story.append(Spacer(1, 0.35 * cm))
        except Exception as audit_error:
            logger.exception(f'Management Audit report section failed: {audit_error}')
            story.append(Paragraph('Management Audit', hdr_s))
            story.append(Paragraph('Management Audit unavailable. Check bot.log for details.', styles['Normal']))
            story.append(Spacer(1, 0.25 * cm))

        # جدول صفقات SL/TP
        if closed:
            story.append(Paragraph(f"Closed Trades ({'all' if limit_value is None else 'last ' + str(limit_value)})", hdr_s))
            trade_data = [['Symbol', 'Status', 'P/L', 'R', 'Closed', 'Dir']]
            for t in closed:
                pnl = t.pnl_usd or 0
                status = getattr(t.status, 'value', t.status)
                direction = getattr(t.direction, 'value', t.direction)
                trade_data.append([
                    t.symbol,
                    str(status).replace('CLOSED_', ''),
                    f'${pnl:+,.2f}',
                    f'{(t.pnl_r or 0):+.2f}R',
                    _saudi_time(t.closed_at) if t.closed_at else '—',
                    direction or '—',
                ])
            t_tbl = Table(trade_data, colWidths=[3.5 * cm, 2 * cm, 2 * cm, 2 * cm, 2.5 * cm, 3.8 * cm])
            t_tbl.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), rl_colors.HexColor('#1a1a2e')),
                ('TEXTCOLOR', (0, 0), (-1, 0), rl_colors.white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [rl_colors.HexColor('#f9f9f9'), rl_colors.white]),
                ('GRID', (0, 0), (-1, -1), 0.3, rl_colors.HexColor('#cccccc')),
                ('LEFTPADDING', (0, 0), (-1, -1), 6),
                ('RIGHTPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ]))
            story.append(t_tbl)

        # جدول الصفقات الملغاة مع سبب الإلغاء
        if cancelled:
            story.append(Spacer(1, 0.35 * cm))
            story.append(Paragraph('Cancelled Trades', hdr_s))

            cancelled_data = [['Symbol', 'Status', 'Reason', 'Closed', 'Dir']]

            for t in cancelled:
                status = getattr(t.status, 'value', t.status)
                direction = getattr(t.direction, 'value', t.direction)

                # نقرأ السبب من notes ونقصّه لو طويل
                reason = (t.notes or '—').strip()
                # if len(reason) > 60:
                #     reason = reason[:57] + '...'

                cancelled_data.append([
                    t.symbol,
                    str(status),
                    reason,
                    _saudi_time(t.closed_at) if t.closed_at else '—',
                    direction or '—',
                ])

            c_tbl = Table(
                cancelled_data,
                colWidths=[2.4 * cm, 1.8 * cm, 8.8 * cm, 1.6 * cm, 1.1 * cm],
                repeatRows=2,
            )
            c_tbl.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), rl_colors.HexColor('#1a1a2e')),
                ('TEXTCOLOR', (0, 0), (-1, 0), rl_colors.white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 8),
                ('FONTSIZE', (0, 1), (-1, -1), 7),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [rl_colors.HexColor('#f9f9f9'), rl_colors.white]),
                ('GRID', (0, 0), (-1, -1), 0.3, rl_colors.HexColor('#cccccc')),
                ('LEFTPADDING', (0, 0), (-1, -1), 4),
                ('RIGHTPADDING', (0, 0), (-1, -1), 4),
                ('TOPPADDING', (0, 0), (-1, -1), 3),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                ('LEFTPADDING', (2, 1), (2, -1), 6),
                ('RIGHTPADDING', (2, 1), (2, -1), 6),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ]))
            story.append(c_tbl)

        doc.build(story)
        buf.seek(0)

        env_cfg = getattr(settings, 'env', None)
        token = getattr(env_cfg, 'telegram_bot_token', None) if env_cfg else None
        chat_id = getattr(env_cfg, 'telegram_chat_id', None) if env_cfg else None
        alerts_cfg = _cfg_section('alerts')
        if not chat_id:
            chat_id = (
                alerts_cfg.get('telegram_chat_id')
                or alerts_cfg.get('chat_id')
                or alerts_cfg.get('telegram_channel_id')
                or os.getenv('TELEGRAM_CHAT_ID')
                or os.getenv('TG_CHAT_ID')
                or os.getenv('TELEGRAM_BOT_CHAT_ID')
            )
        if not token:
            return (False, 'Telegram bot token not configured')
        if not chat_id:
            return (False, 'Telegram chat id not configured')

        fname = f"HunterBot — Report_{datetime.now(SA_TZ).strftime('%Y%m%d_%H%M')}.pdf"
        url = f'https://api.telegram.org/bot{token}/sendDocument'
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                data={'chat_id': str(chat_id), 'caption': 'HunterBot Report\n' + now_str},
                files={'document': (fname, buf.getvalue(), 'application/pdf')},
            )
            resp.raise_for_status()
        logger.info(f'PDF report sent to Telegram: {fname}')
        return (True, 'Report sent to Telegram')

    except Exception as e:
        logger.exception(f'generate_and_send_pdf_report failed: {e}')
        return (False, str(e))

async def _send_report_clicked(status_label, selected_limit=None) -> None:
    status_label.set_text('⏳ Generating and sending report...')
    ok, msg = await generate_and_send_pdf_report(_normalize_report_trade_limit(selected_limit if selected_limit is not None else _report_trade_limit))
    status_label.set_text(('✅ ' if ok else '❌ ') + msg)


async def generate_and_send_rejected_pdf_report(limit_value: str | int | None = None) -> tuple[bool, str]:
    import io
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    try:
        selected_limit = _normalize_report_trade_limit(limit_value if limit_value is not None else _rejected_report_limit)
        limit_n = None if selected_limit.lower() == 'all' else int(selected_limit)

        async with AsyncSessionLocal() as s:
            stmt = (
                select(RejectedSignal)
                .order_by(desc(RejectedSignal.created_at), desc(RejectedSignal.id))
            )
            if limit_n is not None:
                stmt = stmt.limit(limit_n)

            res = await s.execute(stmt)
            rows = list(res.scalars().all())

        if not rows:
            return False, 'No rejected signals found'

        now_str = _saudi_now('%Y-%m-%d %H:%M')

        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf,
            pagesize=A4,
            leftMargin=1.5 * cm,
            rightMargin=1.5 * cm,
            topMargin=1.5 * cm,
            bottomMargin=1.5 * cm,
        )

        styles = getSampleStyleSheet()
        title_s = ParagraphStyle('title', parent=styles['Title'], fontSize=18, spaceAfter=6)
        sub_s = ParagraphStyle('sub', parent=styles['Normal'], fontSize=10, textColor=rl_colors.gray, spaceAfter=10)
        hdr_s = ParagraphStyle('hdr', parent=styles['Heading2'], fontSize=12, spaceBefore=10, spaceAfter=6)
        cell_s = ParagraphStyle('cell', parent=styles['Normal'], fontSize=7, leading=9)

        story = []
        story.append(Paragraph('HunterBot Rejected Signals Report', title_s))
        story.append(Paragraph(f'Generated {now_str}', sub_s))
        story.append(Paragraph(f'Rows: {len(rows)}', sub_s))
        story.append(Spacer(1, 0.2 * cm))
        story.append(Paragraph('Rejected Signals', hdr_s))

        # عناوين الأعمدة المختصرة
        table_data = [[
            "Sym",  # BTC بدل BTCUSDT
            "D",  # L / S
            "%",
            "Context",  # STATE · REGIME في عمود واحد
            "Category",
            "Reason",
            "Details",
            "Created",
        ]]

        for r in rows:
            # اختصار الرمز
            symbol = str(r.symbol or "")
            base = symbol.replace("USDT", "").replace("USDC", "")

            # اختصار الاتجاه
            raw_dir = str(r.direction or "")
            short_dir = "L" if "LONG" in raw_dir else "S" if "SHORT" in raw_dir else raw_dir

            # دمج state + regime في عمود واحد قصير
            state = str(r.market_state or "")
            regime = str(r.market_regime or "")
            if state and regime:
                context = f"{state} · {regime}"
            else:
                context = state or regime or "-"

            reason = str(r.rejection_reason or "")
            details = str(r.rejection_details or "-")
            created = _saudi_time(r.created_at) if r.created_at else ""
            category_text = _rejection_category_text(getattr(r, "category", None))

            table_data.append([
                base,
                short_dir,
                f"{float(r.setup_score or 0):.1f}",
                Paragraph(context, cell_s),
                category_text,
                reason,
                Paragraph(details, cell_s),
                created,
            ])

        tbl = Table(
            table_data,
            colWidths=[1.3 * cm, 0.5 * cm, 0.9 * cm, 4.0 * cm, 2.0 * cm, 2.7 * cm, 3.5 * cm, 1.6 * cm],
            repeatRows=1,
        )

        # tbl = Table(
        #     table_data,
        #     colWidths=[1.5*cm, 0.5*cm, 0.9*cm, 4.5*cm, 2.7*cm, 3.5*cm, 1.7*cm],
        #     repeatRows=1,
        # )
        tbl.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), rl_colors.HexColor('#1a1a2e')),
            ('TEXTCOLOR', (0, 0), (-1, 0), rl_colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 8),
            ('FONTSIZE', (0, 1), (-1, -1), 7),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [rl_colors.HexColor('#f9f9f9'), rl_colors.white]),
            ('GRID', (0, 0), (-1, -1), 0.3, rl_colors.HexColor('#cccccc')),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        story.append(tbl)

        doc.build(story)
        buf.seek(0)

        env_cfg = getattr(settings, 'env', None)
        token = getattr(env_cfg, 'telegram_bot_token', None) if env_cfg else None
        chat_id = getattr(env_cfg, 'telegram_chat_id', None) if env_cfg else None

        alerts_cfg = _cfg_section('alerts')
        if not chat_id:
            chat_id = (
                alerts_cfg.get('telegram_chat_id')
                or alerts_cfg.get('chat_id')
                or alerts_cfg.get('telegram_channel_id')
                or os.getenv('TELEGRAM_CHAT_ID')
                or os.getenv('TG_CHAT_ID')
                or os.getenv('TELEGRAM_BOT_CHAT_ID')
            )

        if not token:
            return False, 'Telegram bot token not configured'
        if not chat_id:
            return False, 'Telegram chat id not configured'

        fname = f"HunterBot_RejectedSignals_{datetime.now(SA_TZ).strftime('%Y%m%d_%H%M')}.pdf"
        url = f'https://api.telegram.org/bot{token}/sendDocument'

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                data={
                    'chat_id': str(chat_id),
                    'caption': f'HunterBot Rejected Signals Report — {now_str}',
                },
                files={
                    'document': (fname, buf.getvalue(), 'application/pdf'),
                },
            )
            resp.raise_for_status()

        logger.info(f'Rejected signals PDF report sent to Telegram: {fname}')
        return True, 'sent Success'

    except Exception as e:
        logger.exception(f'generate_and_send_rejected_pdf_report failed: {e}')
        return False, str(e)


async def send_rejected_report_clicked(status_label, selected_limit=None) -> None:
    status_label.set_text('⏳ Generating and sending rejected report...')
    ok, msg = await generate_and_send_rejected_pdf_report(
        _normalize_report_trade_limit(selected_limit if selected_limit is not None else _rejected_report_limit)
    )
    status_label.set_text(('✅ ' if ok else '❌ ') + msg)


def _normalize_report_trade_limit(value: object) -> str:
    if isinstance(value, dict):
        value = value.get('value') or value.get('args') or value
    value = str(value or '20').strip()
    return 'All' if value.lower() == 'all' else value


def _build_progress_html() -> str:
    step = _scan_state.get("step", 0)
    counts = _scan_state.get("counts", {})
    steps = ["Fetch", "Quality", "Volume", "OI", "Done"]
    pct = int((step / 5) * 100)
    if step == 0:
        label, dot_color, animating = "Idle", "rgba(255,255,255,.25)", False
    elif step >= 5:
        label, dot_color, animating = "Complete ✓", "var(--accent)", False
    else:
        label, dot_color, animating = f"{steps[step-1]}…", "var(--info)", True
    anim = "animation:pulse 1s infinite;" if animating else ""
    counts_html = ""
    if counts:
        total = counts.get("total", 0)
        final = counts.get("final", 0)
        counts_html = (
            f'<div style="font-size:13px;color:var(--text-muted);font-family:var(--font-mono);margin-top:2px;">'
            f'{total} scanned • <span style="color:var(--accent)">{final} final</span>'
            '</div>'
        )
    return (
        '<div style="display:flex;flex-direction:column;gap:6px;padding:4px 0;">'
        '<div style="display:flex;align-items:center;gap:6px;width:100%;">'
        f'<span style="width:7px;height:7px;border-radius:50%;background:{dot_color};flex-shrink:0;{anim}"></span>'
        f'<span style="font-size:13px;color:var(--text-muted);">{label}</span>'
        f'<span style="margin-left:auto;font-family:var(--font-mono);font-size:13px;color:var(--info);">{pct}%</span>'
        '</div>'
        '<div style="width:100%;height:4px;background:rgba(255,255,255,.08);border-radius:2px;overflow:hidden;">'
        f'<div style="height:100%;width:{pct}%;background:var(--accent);border-radius:2px;transition:width .4s ease;"></div>'
        '</div>'
        f'{counts_html}'
        '</div>'
    )


async def _trigger_scan_now() -> None:
    global _scan_version
    if _running_lock.locked():
        _safe_notify('A cycle is already running', type='warning')
        return
    async with _running_lock:
        _scan_state.update({"step": 1, "counts": {}})
        _safe_notify('Cycle started', type='info')
        try:
            result = await bot.run_cycle()
            diag = getattr(bot.scanner, 'last_diagnostics', None)
            _scan_state["step"] = 5
            _scan_state["counts"] = {
                "total": getattr(diag, 'total_symbols', 0),
                "excluded": getattr(diag, 'excluded_quality', 0),
                "vol": getattr(diag, 'passed_volume', 0),
                "oi": getattr(diag, 'passed_oi', 0),
                "final": getattr(diag, 'final_shortlist', 0),
            } if diag else {}
            _scan_version += 1
            new = result.get('new_setups', 0)
            _safe_notify(f'Scan done • {new} new setup{"s" if new != 1 else ""}', type='positive')
        except Exception as e:
            _scan_state["step"] = 0
            logger.exception('Manual cycle failed')
            _safe_notify(f'Error: {e}', type='negative', multiline=True)

# ─────────────────────────────────────────────────────────────────────────────
# Reports Center helpers/fixes
# ─────────────────────────────────────────────────────────────────────────────

def _clean_report_date(value: object) -> str | None:
    """Return YYYY-MM-DD or None. Date-only to avoid timezone/hour bugs."""
    if value is None:
        return None
    raw = str(value).strip()[:10]
    if not raw:
        return None
    try:
        datetime.strptime(raw, "%Y-%m-%d")
        return raw
    except Exception:
        return None


def _resolve_report_range(report_range: object | None) -> tuple[str, str | None, str | None, bool]:
    """Resolve report range into (label, from_date, to_date, use_count_limit)."""
    from datetime import timedelta
    if not report_range:
        return "Last selected trades", None, None, True
    if isinstance(report_range, str):
        mode = report_range
        from_date = None
        to_date = None
    else:
        report_range = report_range or {}
        mode = str(report_range.get("mode") or "Count Limit")
        from_date = _clean_report_date(report_range.get("from_date"))
        to_date = _clean_report_date(report_range.get("to_date"))
    today_dt = datetime.now(SA_TZ)
    today = today_dt.strftime("%Y-%m-%d")
    if mode in {"Count Limit", "Last Trades", "Limit"}:
        return "Last selected trades", None, None, True
    if mode == "All Time":
        return "All Time", None, None, False
    if mode == "Today":
        return f"Today ({today})", today, today, False
    if mode == "Yesterday":
        d = (today_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        return f"Yesterday ({d})", d, d, False
    if mode == "Last 7 Days":
        start = (today_dt - timedelta(days=6)).strftime("%Y-%m-%d")
        return f"Last 7 Days ({start} → {today})", start, today, False
    if mode == "Last 30 Days":
        start = (today_dt - timedelta(days=29)).strftime("%Y-%m-%d")
        return f"Last 30 Days ({start} → {today})", start, today, False
    if mode == "Since Risk Fix":
        return f"Since Risk Fix (2026-06-18 → {today})", "2026-06-18", today, False
    if mode == "Custom Date Range":
        return f"Custom ({from_date or '...'} → {to_date or '...'})", from_date, to_date, False
    return "Last selected trades", None, None, True


def _apply_report_date_filter(stmt, column, from_date: str | None, to_date: str | None):
    if from_date:
        stmt = stmt.where(func.date(column) >= from_date)
    if to_date:
        stmt = stmt.where(func.date(column) <= to_date)
    return stmt


async def _load_main_report_rows(trade_limit: str | int | None = None, report_range: object | None = None) -> dict:
    selected_limit = _normalize_report_trade_limit(trade_limit if trade_limit is not None else _report_trade_limit)
    limit_value = None if selected_limit.lower() == "all" else int(selected_limit)
    period_label, from_date, to_date, use_count_limit = _resolve_report_range(report_range)
    async with AsyncSessionLocal() as s:
        open_stmt = select(Trade).where(Trade.status.in_([TradeStatus.PENDING.value, TradeStatus.TRIGGERED.value])).order_by(desc(Trade.created_at))
        closed_stmt = select(Trade).where(Trade.status.in_([TradeStatus.CLOSED_TP.value, TradeStatus.CLOSED_SL.value])).order_by(desc(Trade.closed_at), desc(Trade.created_at))
        cancelled_stmt = select(Trade).where(Trade.status.in_([TradeStatus.CANCELLED.value, TradeStatus.EXPIRED.value])).order_by(desc(Trade.closed_at), desc(Trade.created_at))
        if use_count_limit:
            if limit_value is not None:
                open_stmt = open_stmt.limit(limit_value)
                closed_stmt = closed_stmt.limit(limit_value)
                cancelled_stmt = cancelled_stmt.limit(limit_value)
        else:
            open_stmt = _apply_report_date_filter(open_stmt, Trade.created_at, from_date, to_date)
            closed_stmt = _apply_report_date_filter(closed_stmt, Trade.closed_at, from_date, to_date)
            cancelled_stmt = _apply_report_date_filter(cancelled_stmt, Trade.closed_at, from_date, to_date)
        open_rows = list((await s.execute(open_stmt)).scalars().all())
        closed_rows = list((await s.execute(closed_stmt)).scalars().all())
        cancelled_rows = list((await s.execute(cancelled_stmt)).scalars().all())
    wins = [t for t in closed_rows if (t.pnl_usd or 0) > 0]
    losses = [t for t in closed_rows if (t.pnl_usd or 0) <= 0]
    total_pnl = sum((t.pnl_usd or 0) for t in closed_rows)
    avg_r = (sum((t.pnl_r or 0) for t in closed_rows) / len(closed_rows)) if closed_rows else 0.0
    win_rate = (len(wins) / len(closed_rows) * 100.0) if closed_rows else 0.0
    return {"selected_limit": selected_limit, "limit_value": limit_value, "period_label": period_label if not use_count_limit else ("All Time" if limit_value is None else f"Last {limit_value} trades"), "from_date": from_date, "to_date": to_date, "use_count_limit": use_count_limit, "open": open_rows, "closed": closed_rows, "cancelled": cancelled_rows, "wins": wins, "losses": losses, "total_pnl": total_pnl, "avg_r": avg_r, "win_rate": win_rate}


async def build_main_report_preview_html(trade_limit: str | int | None = None, report_range: object | None = None) -> str:
    data = await _load_main_report_rows(trade_limit, report_range)
    pnl_cls = "green" if data["total_pnl"] >= 0 else "red"
    wr_cls = "green" if data["win_rate"] >= 50 else "yellow"
    return f'''
    <div class="kpi-row" style="margin-top:12px">
      <div class="kpi"><div class="kpi-label">Period</div><div class="kpi-val cyan" style="font-size:18px">{data["period_label"]}</div><div class="kpi-change">Main trades report</div></div>
      <div class="kpi"><div class="kpi-label">Open</div><div class="kpi-val cyan">{len(data["open"])}</div><div class="kpi-change">Pending / triggered</div></div>
      <div class="kpi"><div class="kpi-label">Closed</div><div class="kpi-val {pnl_cls}">{len(data["closed"])}</div><div class="kpi-change">{len(data["wins"])}W / {len(data["losses"])}L</div></div>
      <div class="kpi"><div class="kpi-label">Win Rate</div><div class="kpi-val {wr_cls}">{data["win_rate"]:.1f}%</div><div class="kpi-change">Preview only</div></div>
      <div class="kpi"><div class="kpi-label">P/L</div><div class="kpi-val {pnl_cls}">${data["total_pnl"]:+,.2f}</div><div class="kpi-change">{data["avg_r"]:+.2f}R avg</div></div>
      <div class="kpi"><div class="kpi-label">Cancelled</div><div class="kpi-val yellow">{len(data["cancelled"])}</div><div class="kpi-change">Cancelled / expired</div></div>
    </div>
    '''


async def build_rejected_report_preview_html(limit_value: str | int | None = None) -> str:
    from html import escape as _esc
    selected_limit = _normalize_report_trade_limit(limit_value if limit_value is not None else _rejected_report_limit)
    limit_n = None if selected_limit.lower() == "all" else int(selected_limit)
    async with AsyncSessionLocal() as s:
        stmt = select(RejectedSignal).order_by(desc(RejectedSignal.created_at), desc(RejectedSignal.id))
        if limit_n is not None:
            stmt = stmt.limit(limit_n)
        rows = list((await s.execute(stmt)).scalars().all())
    body = ""
    for r in rows[:30]:
        body += f"""
        <tr><td class="sym-cell">{_esc(str(r.symbol or ''))}</td><td>{direction_pill(str(r.direction or ''))}</td><td><span class="pill pill-info-soft" style="font-size:12px">{_esc(_rejection_category_text(getattr(r, 'category', None)))}</span></td><td><span class="pill pill-info-soft" style="font-size:12px">{_esc(str(r.rejection_reason or ''))}</span></td><td class="mono tabular-nums">{float(r.setup_score or 0):.1f}</td><td class="mono text-muted">{_saudi_time(r.created_at) if r.created_at else '—'}</td></tr>
        """
    if not body:
        body = '<tr><td colspan="6" class="text-muted">No rejected signals found.</td></tr>'
    return f'''<div class="card" style="margin-top:16px"><div class="card-title">Rejected Signals Preview <span class="pill pill-info-soft" style="margin-left:6px">{len(rows)}</span></div><div class="table-wrap"><table class="lh-table"><thead><tr><th>Symbol</th><th>Dir</th><th>Category</th><th>Reason</th><th>Score</th><th>Time</th></tr></thead><tbody>{body}</tbody></table></div></div>'''


async def build_analytics_report_preview_html(days_label: str = "30d") -> str:
    from datetime import timezone, timedelta
    now_utc = datetime.now(timezone.utc)
    if days_label == "7d":
        cutoff = now_utc - timedelta(days=7)
    elif days_label == "30d":
        cutoff = now_utc - timedelta(days=30)
    elif days_label == "90d":
        cutoff = now_utc - timedelta(days=90)
    else:
        cutoff = None
    async with AsyncSessionLocal() as s:
        rej_stmt = select(RejectedSignal).order_by(desc(RejectedSignal.created_at))
        if cutoff:
            rej_stmt = rej_stmt.where(RejectedSignal.created_at >= cutoff.replace(tzinfo=None))
        rejected_all = list((await s.execute(rej_stmt)).scalars().all())
        canc_stmt = select(Trade).where(Trade.status.in_([TradeStatus.CANCELLED.value, TradeStatus.EXPIRED.value])).order_by(desc(Trade.closed_at), desc(Trade.created_at))
        if cutoff:
            canc_stmt = canc_stmt.where(Trade.created_at >= cutoff.replace(tzinfo=None))
        cancelled_all = list((await s.execute(canc_stmt)).scalars().all())
    total_cancelled = sum(1 for t in cancelled_all if str(getattr(t.status, "value", t.status)).endswith("CANCELLED"))
    total_expired = sum(1 for t in cancelled_all if str(getattr(t.status, "value", t.status)).endswith("EXPIRED"))
    period = f"Last {days_label}" if days_label != "All" else "All Time"
    return f'''<div class="kpi-row" style="margin-top:12px"><div class="kpi"><div class="kpi-label">Period</div><div class="kpi-val cyan">{period}</div><div class="kpi-change">Analytics preview</div></div><div class="kpi"><div class="kpi-label">Rejected Signals</div><div class="kpi-val red">{len(rejected_all)}</div><div class="kpi-change">Total rejections</div></div><div class="kpi"><div class="kpi-label">Cancelled</div><div class="kpi-val yellow">{total_cancelled}</div><div class="kpi-change">Cancelled trades</div></div><div class="kpi"><div class="kpi-label">Expired</div><div class="kpi-val yellow">{total_expired}</div><div class="kpi-change">Expired trades</div></div><div class="kpi"><div class="kpi-label">Total</div><div class="kpi-val cyan">{total_cancelled + total_expired}</div><div class="kpi-change">Canc + expired</div></div></div>'''


async def _build_main_report_pdf_bytes(trade_limit: str | int | None = None, report_range: object | None = None) -> tuple[bytes, str, str]:
    import io
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    data = await _load_main_report_rows(trade_limit, report_range)
    equity = await executor.get_equity() if hasattr(executor, "get_equity") else 0.0
    open_count = await executor.get_open_count() if hasattr(executor, "get_open_count") else 0
    kill = await executor.is_kill_switch_active() if hasattr(executor, "is_kill_switch_active") else False
    initial = getattr(executor, "initial_capital", 0) or 0
    eq_pct = (equity - initial) / initial * 100 if initial else 0
    now_str = _saudi_now("%Y-%m-%d %H:%M")
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=1.5 * cm, rightMargin=1.5 * cm, topMargin=1.5 * cm, bottomMargin=1.5 * cm)
    styles = getSampleStyleSheet()
    title_s = ParagraphStyle("title", parent=styles["Title"], fontSize=18, spaceAfter=6)
    sub_s = ParagraphStyle("sub", parent=styles["Normal"], fontSize=10, textColor=rl_colors.gray, spaceAfter=12)
    hdr_s = ParagraphStyle("hdr", parent=styles["Heading2"], fontSize=12, spaceBefore=12, spaceAfter=6)
    cell_s = ParagraphStyle("cell", parent=styles["Normal"], fontSize=7, leading=9)
    def tbl_style(font_size=8):
        return TableStyle([("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#1a1a2e")), ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), font_size), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [rl_colors.HexColor("#f9f9f9"), rl_colors.white]), ("GRID", (0, 0), (-1, -1), 0.3, rl_colors.HexColor("#cccccc")), ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("VALIGN", (0, 0), (-1, -1), "TOP")])
    story = [Paragraph("HunterBot — Main Trades Report", title_s), Paragraph(f"Generated: {now_str} | Paper Mode | Period: {data['period_label']}", sub_s), Spacer(1, 0.2 * cm), Paragraph("Portfolio Summary", hdr_s)]
    eq_sign = "+" if eq_pct >= 0 else ""
    pnl_sign = "+" if data["total_pnl"] >= 0 else ""
    kpi_data = [["Metric", "Value"], ["Report Period", data["period_label"]], ["Equity", f"${equity:,.2f} ({eq_sign}{eq_pct:.2f}%)"], ["Realized P/L", f"${pnl_sign}{data['total_pnl']:,.2f}"], ["Win Rate", f"{data['win_rate']:.1f}% ({len(data['wins'])}W / {len(data['losses'])}L)"], ["Open Positions", f"{open_count} live / {len(data['open'])} in report"], ["Cancelled/Expired", str(len(data["cancelled"]))], ["Kill Switch", "ARMED" if kill else "SAFE"]]
    kpi_tbl = Table(kpi_data, colWidths=[5.5 * cm, 10.5 * cm]); kpi_tbl.setStyle(tbl_style(9)); story += [kpi_tbl, Spacer(1, 0.25 * cm)]
    try:
        audit = await ManagementAuditAnalyzer().analyze(data["limit_value"] if data["use_count_limit"] else None, from_date=data["from_date"], to_date=data["to_date"])
        summary = audit.get("summary", {})
        if int(summary.get("trades", 0) or 0):
            cap = summary.get("avg_capture"); leak = summary.get("avg_leakage")
            rows = [["Metric", "Value"], ["Analyzed Trades", str(summary.get("trades", 0))], ["Avg MaxR", f"{float(summary.get('avg_max_r', 0) or 0):+.2f}R"], ["Avg FinalR", f"{float(summary.get('avg_final_r', 0) or 0):+.2f}R"], ["Avg Capture", f"{cap*100:.1f}%" if cap is not None else "n/a"], ["Profit Leakage", f"{leak*100:.1f}%" if leak is not None else "n/a"]]
            audit_tbl = Table(rows, colWidths=[5.5 * cm, 10.5 * cm]); audit_tbl.setStyle(tbl_style(8)); story += [Paragraph("Management Audit", hdr_s), audit_tbl, Spacer(1, 0.25 * cm)]
    except Exception as audit_error:
        logger.exception(f"Management Audit PDF section failed: {audit_error}")
    def _trade_table(title, rows, is_cancelled=False):
        if not rows: return
        story.append(Paragraph(f"{title} ({len(rows)})", hdr_s))
        if is_cancelled:
            table_data = [["Symbol", "Status", "Reason", "Closed", "Dir"]]
            for t in rows:
                status = getattr(t.status, "value", t.status); direction = getattr(t.direction, "value", t.direction); reason = str(t.notes or "—")
                table_data.append([t.symbol, str(status), Paragraph(reason[:260], cell_s), _saudi_time(t.closed_at) if t.closed_at else "—", direction or "—"])
            tbl = Table(table_data, colWidths=[2.1 * cm, 2.0 * cm, 7.7 * cm, 2.1 * cm, 1.2 * cm], repeatRows=1)
        else:
            table_data = [["Symbol", "Status", "P/L", "R", "Closed", "Dir"]]
            for t in rows:
                status = getattr(t.status, "value", t.status); direction = getattr(t.direction, "value", t.direction)
                table_data.append([t.symbol, str(status).replace("CLOSED_", ""), f"${float(t.pnl_usd or 0):+,.2f}", f"{float(t.pnl_r or 0):+.2f}R", _saudi_time(t.closed_at) if t.closed_at else "—", direction or "—"])
            tbl = Table(table_data, colWidths=[2.6 * cm, 2.0 * cm, 2.0 * cm, 1.7 * cm, 2.2 * cm, 1.4 * cm], repeatRows=1)
        tbl.setStyle(tbl_style(7)); story.extend([tbl, Spacer(1, 0.25 * cm)])
    _trade_table("Closed Trades", data["closed"], False)
    _trade_table("Cancelled / Expired Trades", data["cancelled"], True)
    doc.build(story); buf.seek(0)
    fname = f"HunterBot_Report_{datetime.now(SA_TZ).strftime('%Y%m%d_%H%M')}.pdf"
    caption = f"HunterBot Report\n{now_str} | {data['period_label']}"
    return buf.getvalue(), fname, caption


async def generate_and_send_pdf_report(trade_limit: str | int | None = None, report_range: object | None = None) -> tuple[bool, str]:
    """Build the main trades PDF and send it via Telegram."""
    try:
        pdf_bytes, fname, caption = await _build_main_report_pdf_bytes(trade_limit, report_range)
        env_cfg = getattr(settings, "env", None)
        token = getattr(env_cfg, "telegram_bot_token", None) if env_cfg else None
        chat_id = getattr(env_cfg, "telegram_chat_id", None) if env_cfg else None
        alerts_cfg = _cfg_section("alerts")
        if not chat_id:
            chat_id = alerts_cfg.get("telegram_chat_id") or alerts_cfg.get("chat_id") or alerts_cfg.get("telegram_channel_id") or os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TG_CHAT_ID") or os.getenv("TELEGRAM_BOT_CHAT_ID")
        if not token: return False, "Telegram bot token not configured"
        if not chat_id: return False, "Telegram chat id not configured"
        url = f"https://api.telegram.org/bot{token}/sendDocument"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, data={"chat_id": str(chat_id), "caption": caption}, files={"document": (fname, pdf_bytes, "application/pdf")})
            resp.raise_for_status()
        logger.info(f"PDF report sent to Telegram: {fname}")
        return True, "Report sent to Telegram"
    except Exception as e:
        logger.exception(f"generate_and_send_pdf_report failed: {e}")
        return False, str(e)


async def generate_main_pdf_file(trade_limit: str | int | None = None, report_range: object | None = None) -> tuple[bool, str, str | None]:
    """Build main report PDF and save it locally under data/reports."""
    try:
        pdf_bytes, fname, _ = await _build_main_report_pdf_bytes(trade_limit, report_range)
        out_dir = Path("data") / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / fname
        path.write_bytes(pdf_bytes)
        return True, f"Saved: {path}", str(path)
    except Exception as e:
        logger.exception(f"generate_main_pdf_file failed: {e}")
        return False, str(e), None

# ── Aliases for pages.py compatibility ──────────────────────────────
_generate_and_send_pdf_report = generate_and_send_pdf_report
_generate_and_send_rejected_pdf_report = generate_and_send_rejected_pdf_report
_generate_main_pdf_file = generate_main_pdf_file
_build_main_report_preview_html = build_main_report_preview_html
_build_rejected_report_preview_html = build_rejected_report_preview_html
_build_analytics_report_preview_html = build_analytics_report_preview_html

def _render_sidebar(active: str) -> None:
    """Render the sidebar as stable DOM.

    Important: do not split opening/closing <div> tags across multiple ui.html()
    calls. NiceGUI wraps each ui.html in its own element, and fragmented HTML can
    corrupt the DOM after refresh/auto-refresh, which caused duplicated sidebar
    blocks and broken Home layout.
    """
    routes = [
        ('Home', '/', '🏠'),
        ('Scanner', '/scanner', '🛰️'),
        ('Signals', '/signals', '📡'),
        ('Trades', '/trades', '📘'),
        ('Reports', '/reports', '📑'),
        ('Analytics', '/analytics', '📊'),
        ('Backtest', '/backtest', '🧪'),
        ('Settings', '/settings', '⚙️'),
    ]

    nav_html = ''.join(
        f'<a href="{path}" class="nav-link">'
        f'<div class="{"nav-item active" if label == active else "nav-item"}">'
        f'<span class="nav-icon">{icon}</span>'
        f'<span class="nav-label">{label}</span>'
        f'</div></a>'
        for label, path, icon in routes
    )

    with ui.element('aside').classes('sidebar').props('id=hunter-sidebar'):
        ui.html(f"""
        <div class="sidebar-inner">
          <div class="logo-block hunter-logo-card">
            <div class="hunter-logo-orb">🎯</div>
            <div class="logo-title">HUNTER PRO</div>
            <div class="logo-sub">Liquidity Hunter OS</div>
            <div class="logo-badge">Paper Mode · LIVE</div>
          </div>

          <div class="sidebar-section">
            <div class="sidebar-label">Navigation</div>
            {nav_html}
          </div>
        </div>
        """)

        with ui.element('div').classes('sidebar-section scan-side-card'):
            ui.html('<div class="sidebar-label">Last Scan</div>')
            prog_el = ui.html(_build_progress_html())
            ui.timer(1.0, lambda: prog_el.set_content(_build_progress_html()))

        ui.html('<div class="sidebar-footer"><span class="side-dot"></span><span>State saved locally</span></div>')

def _render_topbar(subtitle: str) -> None:
    with ui.element('div').classes('topbar'):
        ui.html(
            '<button onclick="toggleSidebar()" class="sidebar-toggle-btn" title="Sidebar">&#9776;</button>'
        )
        ui.html(
            f'<div><div class="topbar-title">HUNTER PRO</div>'
            f'<div class="topbar-sub">BINANCE FUTURES · {subtitle}</div></div>'
        )
        ui.html('<div style="display:flex;align-items:center;justify-content:center;min-width:90px;"><div class="status-pill" style="margin:0;"><div class="status-dot"></div>LIVE</div></div>')

        with ui.row().classes('items-center').style('gap:22px;margin-left:auto;'):
            with ui.row().classes('items-center no-wrap').style('gap:22px;align-items:center;'):
                uptime_el = ui.html('')

                def refresh_uptime() -> None:
                    uptime_el.set_content(
                        '<div style="border:1px solid var(--border);text-align:center;min-width:90px">'
                        '<div style="font-size:13px;color:#a0aec0;letter-spacing:.06em;text-transform:uppercase">UPTIME</div>'
                        f'<div class="uptime">{_uptime_str()}</div>'
                        '</div>'
                    )

                refresh_uptime()
                ui.timer(1.0, refresh_uptime)

                from src.core.config import settings as scfg
                from datetime import timezone as tz
                scan_interval = int(_cfg_section('ui', _cfg_section('scanner', {})).get('scan_interval_seconds', _cfg_section('scanner', {}).get('scan_interval_seconds', 45)))
                countdown_el = ui.html('')

            def refresh_countdown() -> None:
                now = datetime.now(tz.utc)
                if bot.last_cycle_at:
                    last = bot.last_cycle_at
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=tz.utc)
                    elapsed = (now - last).total_seconds()
                    remaining = max(0, scan_interval - int(elapsed))
                else:
                    remaining = scan_interval
                m2, s2 = divmod(remaining, 60)
                if remaining > 60:
                    clr = 'var(--accent)'
                elif remaining > 15:
                    clr = '#f59e0b'
                else:
                    clr = '#ef4444'
                countdown_el.set_content(
                    '<div style="border:1px solid var(--border);text-align:center;min-width:88px">'
                    '<div style="font-size:11px;color:#a0aec0;letter-spacing:.06em;text-transform:uppercase">NEXT SCAN</div>'
                    f'<div style="font-size:14px;font-weight:700;color:{clr};font-family:var(--font-mono)">{m2:02d}:{s2:02d}</div>'
                    '</div>'
                )

            refresh_countdown()
            ui.timer(1.0, refresh_countdown)
            ui.button('Refresh', on_click=lambda: ui.navigate.reload()).props('flat').style(
                'background:rgba(255,255,255,.06);color:var(--text-muted);border:1px solid var(--border);'
                'border-radius:5px;font-size:14px;padding:4px 10px;'
            )
            ui.button('Run Scan', on_click=_trigger_scan_now).style(
                #'background:#22c55e;color:#000;font-weight:700;font-size:14px;border-radius:5px;padding:4px 12px;border:none;'
                'background:rgba(255,255,255,.06) !important;color:#fff !important;font-weight:700 !important;font-size:16px;border:1px solid var(--border);'
            )




async def _render_global_summary_strip(target) -> None:
    """Update the global KPI strip in-place.

    Equity is live:
        initial capital + realized closed P/L + open unrealized P/L

    Realized P/L and Win Rate remain based on closed trades only.
    """
    base_equity = await executor.get_equity() if hasattr(executor, 'get_equity') else 0.0
    open_count = await executor.get_open_count() if hasattr(executor, 'get_open_count') else 0
    kill = await executor.is_kill_switch_active() if hasattr(executor, 'is_kill_switch_active') else False

    async with AsyncSessionLocal() as s:
        closed_res = await s.execute(
            select(Trade).where(
                Trade.status.in_([TradeStatus.CLOSED_TP.value, TradeStatus.CLOSED_SL.value])
            )
        )
        closed = list(closed_res.scalars().all())

        open_res = await s.execute(
            select(Trade).where(
                Trade.status.in_([TradeStatus.PENDING.value, TradeStatus.TRIGGERED.value])
            )
        )
        open_trades = list(open_res.scalars().all())

    live_open_pnl = 0.0

    for t in open_trades:
        try:
            status = _status_text(t.status)
            if status != 'TRIGGERED':
                continue

            current_price = live_prices.get_price(t.symbol)
            entry_price = _trade_entry_price(t)

            if not current_price or not entry_price:
                continue

            direction = _dir_text(t.direction)

            if direction == 'SHORT':
                pnl_pct = ((entry_price - current_price) / entry_price) * 100.0
            else:
                pnl_pct = ((current_price - entry_price) / entry_price) * 100.0

            live_open_pnl += float(t.position_size_usd or 0.0) * (pnl_pct / 100.0)

        except Exception:
            continue

    equity = float(base_equity or 0.0) + live_open_pnl

    wins = [t for t in closed if (t.pnl_usd or 0) > 0]
    win_rate = len(wins) / len(closed) if closed else 0.0
    total_pnl = sum((t.pnl_usd or 0) - (t.fees_usd or 0) for t in closed)

    initial = getattr(executor, 'initial_capital', 0) or 0
    eq_pct = ((equity - initial) / initial * 100) if initial else 0.0

    eq_cls = 'green' if eq_pct >= 0 else 'red'
    pnl_cls = 'green' if total_pnl >= 0 else 'red'
    wr_cls = 'green' if win_rate >= 0.5 else 'yellow'
    kill_cls = 'red' if kill else 'green'

    open_pnl_cls = 'up' if live_open_pnl >= 0 else 'down'
    open_pnl_sign = '+' if live_open_pnl >= 0 else '-'
    eq_sign = '+' if eq_pct >= 0 else '-'

    html = (
        f'<div class="kpi-row header-kpi-row" style="width:100%;margin:0;">'
        f'<div class="kpi"><div class="kpi-label">Equity</div><div class="kpi-val {eq_cls}">${equity:,.2f}</div><div class="kpi-change {"up" if eq_pct>=0 else "down"}">{eq_sign}{abs(eq_pct):.2f}%</div></div>'
        f'<div class="kpi"><div class="kpi-label">Realized P/L</div><div class="kpi-val {pnl_cls}">${total_pnl:,.2f}</div><div class="kpi-change {"up" if total_pnl>=0 else "down"}">{len(closed)} trades</div></div>'
        f'<div class="kpi"><div class="kpi-label">Win Rate</div><div class="kpi-val {wr_cls}">{win_rate*100:.1f}%</div><div class="kpi-change">{len(wins)}/{len(closed)} wins</div></div>'
        f'<div class="kpi"><div class="kpi-label">Open Positions</div><div class="kpi-val cyan">{open_count}</div><div class="kpi-change {open_pnl_cls}">Open P/L {open_pnl_sign}${abs(live_open_pnl):,.2f}</div></div>'
        f'<div class="kpi"><div class="kpi-label">Kill Switch</div><div class="kpi-val {kill_cls}">{"ARMED" if kill else "SAFE"}</div><div class="kpi-change {"down" if kill else "up"}">{"Daily limit hit" if kill else "Trading OK"}</div></div>'
        '</div>'
    )

    try:
        target.set_content(html)
    except Exception:
        pass



def _page_shell(
    page_title: str,
    subtitle: str,
    *,
    show_topbar: bool = True,
    show_summary: bool = True,
):
    ui.add_head_html(f'<style>{GLOBAL_CSS}</style>')
    ui.add_head_html("""
    <style>
    .hunter-page-shell { position:relative; width:100%; min-height:100vh; overflow-x:hidden; }
    .sidebar {
        box-sizing: border-box !important;
        width: 240px !important;
        min-width: 240px !important;
        background:
            radial-gradient(circle at 22% 0%, rgba(0,245,140,.13), transparent 34%),
            linear-gradient(180deg, rgba(15,23,42,.98), rgba(2,6,23,.98)) !important;
        border-right: 1px solid rgba(148,163,184,.16) !important;
        box-shadow: 12px 0 45px rgba(0,0,0,.34) !important;
        padding: 16px 14px !important;
        transition: transform .25s ease, opacity .25s ease !important;
        z-index: 1000 !important;
        position: fixed !important;
        left: 0 !important;
        top: 0 !important;
        bottom: 0 !important;
        height: 100vh !important;
        overflow-y: auto !important;
    }
    .sidebar.sidebar--hidden { transform: translateX(-252px) !important; opacity:0 !important; pointer-events:none !important; }
    .main-content { box-sizing:border-box !important; margin-left:240px; width:calc(100% - 240px); max-width:calc(100% - 240px); min-height:100vh; transition:margin-left .25s ease,width .25s ease,max-width .25s ease; }
    .hunter-logo-card { position:relative; overflow:hidden; border:1px solid rgba(255,255,255,.10); border-radius:18px; padding:16px 14px; background:linear-gradient(135deg,rgba(0,245,140,.11),rgba(83,167,255,.08)),rgba(15,23,42,.72); box-shadow:inset 0 1px 0 rgba(255,255,255,.07),0 20px 50px rgba(0,0,0,.20); margin-bottom:18px; }
    .hunter-logo-card:after { content:""; position:absolute; width:110px; height:110px; right:-44px; top:-46px; border-radius:999px; background:rgba(0,245,140,.11); filter:blur(2px); }
    .hunter-logo-orb { width:34px; height:34px; border-radius:12px; display:flex; align-items:center; justify-content:center; background:rgba(0,245,140,.12); border:1px solid rgba(0,245,140,.24); margin-bottom:10px; font-size:17px; }
    .logo-title { font-size:18px !important; letter-spacing:.12em !important; font-weight:950 !important; }
    .logo-sub { color:var(--text-muted) !important; font-size:11px !important; letter-spacing:.06em !important; margin-top:3px; }
    .logo-badge { display:inline-flex; margin-top:10px; padding:4px 8px; border-radius:999px; color:var(--accent); border:1px solid rgba(0,245,140,.18); background:rgba(0,245,140,.08); font-size:10px; font-weight:800; letter-spacing:.06em; text-transform:uppercase; }
    .sidebar-section { margin-top:14px !important; padding:0 !important; }
    .sidebar-label { margin:0 4px 8px 4px !important; color:rgba(148,163,184,.82) !important; font-size:11px !important; letter-spacing:.12em !important; text-transform:uppercase; font-weight:900; }
    .nav-link { text-decoration:none !important; display:block; margin:6px 0; }
    .nav-item { height:42px; display:flex !important; align-items:center !important; gap:11px !important; padding:0 12px !important; border-radius:13px !important; color:rgba(226,232,240,.78) !important; border:1px solid transparent !important; background:transparent !important; transition:all .18s ease !important; font-weight:750 !important; }
    .nav-item:hover { background:rgba(255,255,255,.055) !important; border-color:rgba(255,255,255,.08) !important; color:#fff !important; transform:translateX(2px); }
    .nav-item.active { color:#08111f !important; background:linear-gradient(135deg,var(--accent),#7dd3fc) !important; box-shadow:0 12px 30px rgba(0,245,140,.16) !important; border-color:rgba(255,255,255,.14) !important; }
    .nav-icon { width:24px; height:24px; border-radius:9px; display:inline-flex; align-items:center; justify-content:center; background:rgba(255,255,255,.06); flex-shrink:0; font-size:13px; }
    .nav-item.active .nav-icon { background:rgba(0,0,0,.12); }
    .nav-label { font-size:14px; letter-spacing:.01em; }
    .scan-side-card { margin-top:18px !important; padding:12px !important; border-radius:16px; border:1px solid rgba(255,255,255,.08); background:rgba(15,23,42,.58); }
    .sidebar-footer { position:absolute; bottom:16px; left:16px; right:16px; display:flex; align-items:center; gap:8px; color:rgba(148,163,184,.70); font-size:11px; letter-spacing:.04em; }
    .side-dot { width:7px; height:7px; border-radius:999px; background:var(--accent); box-shadow:0 0 16px rgba(0,245,140,.85); }
    .sidebar-toggle-btn { width:38px; height:38px; border-radius:12px; border:1px solid rgba(255,255,255,.10); background:rgba(255,255,255,.055); color:var(--text); cursor:pointer; font-size:21px; line-height:1; margin-right:6px; flex-shrink:0; transition:all .18s ease; }
    .sidebar-toggle-btn:hover { border-color:rgba(0,245,140,.30); color:var(--accent); background:rgba(0,245,140,.08); }
    .report-grid { display:grid !important; grid-template-columns:repeat(3,minmax(280px,1fr)); gap:16px; width:100%; align-items:stretch; }
    .report-action-card { border:1px solid rgba(255,255,255,.10); background:rgba(15,23,42,.72); border-radius:16px; padding:16px; min-height:245px; }
    .reports-preview-area { width:100%; margin-top:16px; }
    @media (max-width:1250px) { .report-grid { grid-template-columns:1fr !important; } }
    @media (max-width:900px) { .main-content { margin-left:0 !important; width:100% !important; } .sidebar { width:230px !important; min-width:230px !important; } }
    </style>
    <script>
    const HUNTER_SIDEBAR_KEY = 'hunter_sidebar_hidden';
    let hunterSidebarObserverStarted = false;

    function cleanupHunterSidebarDom() {
        // NiceGUI can briefly keep old page fragments around during reload/navigation.
        // Keep a single shell/sidebar only, otherwise the fixed sidebar gets duplicated
        // and Home collapses into a very long broken column.
        const sidebars = Array.from(document.querySelectorAll('aside.sidebar, .sidebar'));
        sidebars.forEach((el, idx) => {
            if (idx > 0 && el.parentNode) el.parentNode.removeChild(el);
        });

        const s = document.querySelector('aside.sidebar, .sidebar');
        if (s) {
            const keepFirst = (selector) => {
                const nodes = Array.from(s.querySelectorAll(selector));
                nodes.forEach((node, idx) => {
                    if (idx > 0 && node.parentNode) node.parentNode.removeChild(node);
                });
            };
            keepFirst('.sidebar-inner');
            keepFirst('.scan-side-card');
            keepFirst('.sidebar-footer');
        }

        const mains = Array.from(document.querySelectorAll('.main-content'));
        mains.forEach((el, idx) => {
            if (idx > 0 && el.parentNode) el.parentNode.removeChild(el);
        });
    }

    function applySidebarState() {
        cleanupHunterSidebarDom();
        const s = document.querySelector('aside.sidebar, .sidebar');
        const m = document.querySelector('.main-content');
        if (!s || !m) return;
        const hidden = localStorage.getItem(HUNTER_SIDEBAR_KEY) === 'true';
        s.classList.toggle('sidebar--hidden', hidden);
        if (hidden) {
            m.style.marginLeft = '0px';
            m.style.width = '100%';
            m.style.maxWidth = '100%';
        } else {
            m.style.marginLeft = '240px';
            m.style.width = 'calc(100% - 240px)';
            m.style.maxWidth = 'calc(100% - 240px)';
        }
    }

    function toggleSidebar() {
        const s = document.querySelector('aside.sidebar, .sidebar');
        if (!s) return;
        const hidden = !s.classList.contains('sidebar--hidden');
        localStorage.setItem(HUNTER_SIDEBAR_KEY, hidden ? 'true' : 'false');
        applySidebarState();
    }

    function startHunterSidebarObserver() {
        if (hunterSidebarObserverStarted) return;
        hunterSidebarObserverStarted = true;
        const observer = new MutationObserver(() => {
            window.requestAnimationFrame(applySidebarState);
        });
        observer.observe(document.body, { childList: true, subtree: true });
    }

    document.addEventListener('DOMContentLoaded', () => {
        applySidebarState();
        startHunterSidebarObserver();
    });
    window.addEventListener('load', applySidebarState);
    window.addEventListener('pageshow', applySidebarState);
    </script>
    """)

    with ui.element('div').classes('hunter-page-shell').style('width:100%;min-height:100vh;overflow-x:hidden'):
        _render_sidebar(page_title)

        with ui.element('div').classes('main-content'):
            if show_topbar:
                _render_topbar(subtitle)

            if show_summary:
                summary_host = ui.html('').style('padding:0 14px 0 14px;width:100%;display:block')
                ui.timer(0.1, lambda: _render_global_summary_strip(summary_host), once=True)

            content = ui.element('div').classes('page-body')

    return content

# def _page_shell(page_title: str, subtitle: str):
#     ui.add_head_html(f'<style>{GLOBAL_CSS}</style>')
#     ui.add_head_html('''
#     <script>
#     function toggleSidebar() {
#       var s = document.querySelector('.sidebar');
#       var m = document.querySelector('.main-content');
#       if (!s) return;
#       var hidden = s.classList.toggle('sidebar--hidden');
#       if (m) {
#         m.style.marginLeft = hidden ? '0px' : '200px';
#         m.style.width = hidden ? '100%' : 'calc(100% - 200px)';
#         m.style.transition = 'margin-left 0.25s ease, width 0.25s ease';
#       }
#     }
#     </script>
#     ''')
#     with ui.element('div').style('display:flex;width:100%;min-height:100vh;overflow:hidden;'):
#         _render_sidebar(page_title)
#         with ui.element('div').classes('main-content'):
#             _render_topbar(subtitle)
#             summary_host = ui.element('div').style('padding:0 14px 0 14px;')
#             ui.timer(0.0, lambda: _render_global_summary_strip(summary_host), once=True)
#             content = ui.element('div').classes('page-body')
#     return content


def _auto_refresh_on_scan(interval: float = 6.0) -> None:
    seen_version = {'v': _scan_version}

    def check() -> None:
        if _scan_version != seen_version['v']:
            seen_version['v'] = _scan_version
            ui.navigate.reload()

    ui.timer(interval, check)

# ─────────────────────────────────────────────────────────────────────────────
# Page routes
# ─────────────────────────────────────────────────────────────────────────────
# All dashboard tabs live in ui/pages.py.
# Keep ui/app.py as the app bootstrap + shared helpers only.
from ui import pages  # noqa: E402,F401


async def _background_loop() -> None:
    from src.core.config import settings as s
    global _scan_version
    await asyncio.sleep(5)
    while True:
        try:
            async with _running_lock:
                _scan_state.update({"step": 1, "counts": {}})
                await bot.run_cycle()
                diag = getattr(bot.scanner, 'last_diagnostics', None)
                _scan_state["step"] = 5
                _scan_state["counts"] = {
                    "total": getattr(diag, 'total_symbols', 0),
                    "excluded": getattr(diag, 'excluded_quality', 0),
                    "vol": getattr(diag, 'passed_volume', 0),
                    "oi": getattr(diag, 'passed_oi', 0),
                    "final": getattr(diag, 'final_shortlist', 0),
                } if diag else {}
                _scan_version += 1
        except Exception as e:
            _scan_state["step"] = 0
            logger.exception(f'Background cycle failed: {e}')
        interval = int(_cfg_section('scanner').get('scan_interval_seconds', 45))
        await asyncio.sleep(interval)


@app.on_startup
async def startup() -> None:
    await init_db()
    logger.info('Database initialised')
    asyncio.create_task(_background_loop())
    asyncio.create_task(live_prices.loop())


def main() -> None:
    import os
    from src.core.config import settings as s
    uicfg = _cfg_section('ui')
    port = int(os.environ.get('LH_UI_PORT', uicfg.get('port', 8082)))
    host = os.environ.get('LH_UI_HOST', uicfg.get('host', '0.0.0.0'))
    print(f'NiceGUI binding to {host}:{port}')
    ui.run(
        title='HunterBot',
        host=host,
        port=port,
        dark=True,
        reload=False,
        favicon='🎯',
        show=False,
    )


if __name__ in {'__main__', '__mp_main__'}:
    main()




# @ui.page('/symbol/{symbol}')
# async def page_symbol_details(symbol: str) -> None:
#     symbol = symbol.upper()
#     container = _page_shell('Trades', f'{symbol} · FULL TRADE LOG')
#     async with AsyncSessionLocal() as s:
#         rows = await s.execute(
#             select(Trade)
#             .where(Trade.symbol == symbol)
#             .order_by(desc(Trade.created_at), desc(Trade.closed_at))
#         )
#         trades = rows.scalars().all()
#
#         stats = await s.execute(
#             select(
#                 func.count(Trade.id),
#                 func.sum(case((Trade.status == TradeStatus.TRIGGERED.value, 1), else_=0)),
#             ).where(Trade.symbol == symbol)
#         )
#         counts_row = stats.one_or_none()
#
#     with container:
#         with ui.row().classes('items-center justify-between').style('margin-bottom:4px;'):
#             ui.html(f'<div class="card-title" style="margin:0;font-size:16px;color:var(--text);">{symbol} Trade Log</div>')
#             ui.html('<a href="/trades" class="details-link">← Back to Trades</a>')
#
#         if not trades:
#             with ui.element('div').classes('card'):
#                 empty_state('🪙', f'No trades recorded yet for {symbol}.')
#             return
#
#         total = len(trades)
#         open_n = sum(1 for t in trades if _status_text(t.status) in {'PENDING', 'TRIGGERED'})
#         cancelled_n = sum(1 for t in trades if _status_text(t.status) in {'CANCELLED', 'EXPIRED'})
#         tp_n = sum(1 for t in trades if _status_text(t.status) == 'CLOSED_TP')
#         sl_n = sum(1 for t in trades if _status_text(t.status) == 'CLOSED_SL')
#         net_pnl = sum((t.pnl_usd or 0) for t in trades)
#
#         ui.html(
#             f'<div class="kpi-row symbol-kpis">'
#             f'<div class="kpi"><div class="kpi-label">Trades</div><div class="kpi-val cyan">{total}</div><div class="kpi-change">All records</div></div>'
#             f'<div class="kpi"><div class="kpi-label">Open</div><div class="kpi-val yellow">{open_n}</div><div class="kpi-change">Pending + triggered</div></div>'
#             f'<div class="kpi"><div class="kpi-label">Cancelled</div><div class="kpi-val red">{cancelled_n}</div><div class="kpi-change">Cancelled + expired</div></div>'
#             f'<div class="kpi"><div class="kpi-label">Take Profit</div><div class="kpi-val green">{tp_n}</div><div class="kpi-change">Closed in profit</div></div>'
#             f'<div class="kpi"><div class="kpi-label">Stop Loss / Net</div><div class="kpi-val {"green" if net_pnl >= 0 else "red"}">{sl_n}</div><div class="kpi-change {"up" if net_pnl >= 0 else "down"}">${net_pnl:+,.2f}</div></div>'
#             '</div>'
#         )
#
#         with ui.element('div').classes('card'):
#             ui.html('<div class="card-title">Full Symbol Timeline</div>')
#             for t in trades:
#                 pnl = t.pnl_usd or 0.0
#                 pnl_cls = _trade_outcome_class(t)
#                 summary = (
#                     f'<div class="trade-summary-row">'
#                     f'<div class="trade-summary-main">'
#                     f'<span class="trade-symbol">#{t.id}</span>'
#                     f'{direction_pill(_dir_text(t.direction))}'
#                     f'{_status_pill(t)}'
#                     f'<span class="trade-time">{_event_time_label(t)}</span>'
#                     '</div>'
#                     f'<div class="trade-summary-side">'
#                     f'<span class="mono">Entry {fmt_price(_trade_entry_price(t))}</span>'
#                     f'<span class="mono">Exit {fmt_price(t.exit_price or 0)}</span>'
#                     f'<span class="mono {pnl_cls}">${pnl:+,.2f}</span>'
#                     f'<span class="mono {pnl_cls}">{(t.pnl_r or 0):+.2f}R</span>'
#                     '</div>'
#                     '</div>'
#                 )
#                 ui.html(
#                     f'<details class="trade-disclosure">'
#                     f'<summary>{summary}</summary>'
#                     f'{_trade_details_html(t)}'
#                     '</details>'
#                 )
#

# @ui.page('/backtest')
# async def page_backtest() -> None:
#     container = _page_shell('Backtest', 'HISTORICAL SIMULATION')
#     with container:
#         ui.html(
#             '<div class="kpi-row">'
#             '<div class="kpi"><div class="kpi-label">Mode</div><div class="kpi-val cyan">CLI BACKTEST</div><div class="kpi-change">Runs via terminal</div></div>'
#             '<div class="kpi"><div class="kpi-label">Default Symbol</div><div class="kpi-val">BTCUSDT</div><div class="kpi-change">Edit below</div></div>'
#             '<div class="kpi"><div class="kpi-label">Default Range</div><div class="kpi-val yellow">60 Days</div><div class="kpi-change">Historical candles</div></div>'
#             '<div class="kpi"><div class="kpi-label">Fee</div><div class="kpi-val">0.04%</div><div class="kpi-change">Taker per config</div></div>'
#             '<div class="kpi"><div class="kpi-label">Warmup</div><div class="kpi-val">100</div><div class="kpi-change">Candles</div></div>'
#             '</div>'
#         )
#         with ui.element('div').classes('card'):
#             ui.html('<div class="card-title">Backtest Launcher</div>')
#             cmd_box = ui.html('')
#             with ui.row().classes('items-end gap-4').style('flex-wrap:wrap;margin-bottom:16px;'):
#                 symbol_inp = ui.input(label='Symbol', value='BTCUSDT', placeholder='e.g. ETHUSDT').props('outlined dense').style('min-width:180px;')
#                 days_inp = ui.number(label='Days', value=60, min=7, max=365, step=1, format='%.0f').props('outlined dense').style('min-width:120px;')
#                 tf_sel = ui.select(label='Timeframe', options=['15m', '1h', '4h'], value='1h').props('outlined dense').style('min-width:130px;')
#
#             def refresh_cmd() -> None:
#                 sym = (symbol_inp.value or 'BTCUSDT').strip().upper()
#                 days = int(days_inp.value or 60)
#                 tf = tf_sel.value or '1h'
#                 cmd_box.set_content(
#                     '<div style="background:rgba(0,0,0,.35);border:1px solid var(--border);border-radius:10px;padding:18px 22px;margin-top:8px;">'
#                     '<div style="font-size:13px;color:var(--text-muted);margin-bottom:10px;letter-spacing:.04em;text-transform:uppercase;">Command — copy & run in terminal</div>'
#                     f'<code style="font-family:var(--font-mono);font-size:15px;line-height:2;color:#7dd3fc;display:block;word-break:break-all;">python -m src.main backtest --symbol {sym} --days {days} --timeframe {tf}</code>'
#                     '<div style="margin-top:14px;padding:14px 18px;border-radius:8px;background:rgba(255,255,255,.03);border:1px solid var(--border);font-size:14px;color:var(--text-muted);line-height:2;">'
#                     '<span style="color:var(--accent);font-weight:700;">Output:</span><br><code style="font-family:var(--font-mono);">data/backtest_results.json</code><br><code style="font-family:var(--font-mono);">--days 180</code> لعينة أوسع</div></div>'
#                 )
#
#             symbol_inp.on('update:model-value', lambda e: refresh_cmd())
#             days_inp.on('update:model-value', lambda e: refresh_cmd())
#             tf_sel.on('update:model-value', lambda e: refresh_cmd())
#             refresh_cmd()