from __future__ import annotations
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import re
from nicegui import ui, app
from sqlalchemy import case, desc, func, select
from pathlib import Path
from fastapi.responses import HTMLResponse, RedirectResponse
from src.core.database import AsyncSessionLocal, RejectedSignal, Trade, TradeStatus, TradeDirection
from src.core.logger import logger
from src.learning.performance_analyzer import ManagementAuditAnalyzer

from ui.app import (
    bot,
    executor,
    live_prices,
    _cfg_section,
    _dir_text,
    _details_link,
    _event_time_label,
    _generate_and_send_pdf_report,
    _generate_and_send_rejected_pdf_report,
    _generate_main_pdf_file,
    _build_main_report_preview_html,
    _build_rejected_report_preview_html,
    _build_analytics_report_preview_html,
    _normalize_report_trade_limit,
    _page_shell,
    _rejection_category_text,
    _safe_notify,
    _status_pill,
    _status_text,
    _trade_details_html,
    _trade_entry_price,
    _trade_outcome_class,
    _trigger_scan_now,
    _auto_refresh_on_scan,
    _report_trade_limit,
    _rejected_report_limit,
)
from ui.components.widgets import (
    direction_pill,
    empty_state,
    fmt_money_short,
    fmt_price,
    regime_pill,
    score_bar_cell,
    state_pill,
)
from ui.mission_control.dash import render_mission_control
from backtest.backtest_page import page_backtest


# ─────────────────────────────────────────────────────────────────────────────
# Report helpers (Trades + Rejected) — unchanged from original
# ─────────────────────────────────────────────────────────────────────────────


SA_TZ = ZoneInfo("Asia/Riyadh")

# Reports Center UI state. Keeps the selected preview after page refresh/scan reload.
_reports_active_preview = {
    "kind": "main",
    "limit": "20",
    "range": {"mode": "Since Risk Fix", "from_date": None, "to_date": None},
    "rejected_limit": "50",
    "analytics_period": "30d",
    "management_limit": "All",
}


def _saudi_dt(dt):
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


def _saudi_time(dt, fmt: str = "%m/%d %H:%M") -> str:
    converted = _saudi_dt(dt)
    if not converted:
        return "—"

    try:
        return converted.strftime(fmt)
    except Exception:
        return str(dt)


async def _send_report_clicked(status_label, selected_limit=None, report_range: object | None = None) -> None:
    status_label.set_text("Generating and sending report...")
    ok, msg = await _generate_and_send_pdf_report(
        _normalize_report_trade_limit(selected_limit)
        if selected_limit is not None
        else _report_trade_limit,
        report_range=report_range,
    )
    status_label.set_text("" if ok else msg)
    if ok:
        _safe_notify("Report sent to Telegram", type="positive")
    else:
        _safe_notify(msg or "Send failed", type="negative")


async def _send_rejected_report_clicked(status_label, selected_limit=None) -> None:
    status_label.set_text("Generating and sending rejected report...")
    ok, msg = await _generate_and_send_rejected_pdf_report(
        _normalize_report_trade_limit(selected_limit)
        if selected_limit is not None
        else _rejected_report_limit
    )
    status_label.set_text("" if ok else msg)
    if ok:
        _safe_notify("Rejected report sent", type="positive")
    else:
        _safe_notify(msg or "Send failed", type="negative")



def _compact_rejection_details(details: str | None) -> str:
    """Compact long rejection diagnostics for the Rejected Signals table.

    The full raw details remain stored in DB/reports. This helper only keeps
    the UI table readable.
    """
    if not details:
        return "-"

    raw = str(details).strip()
    parts: dict[str, str] = {}

    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        parts[k.strip()] = v.strip()

    def _first_float(value: str | None, default: float = 0.0) -> float:
        if not value:
            return default
        m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", str(value))
        if not m:
            return default
        try:
            return float(m.group(0))
        except Exception:
            return default

    if "rr" in parts or raw.startswith("rr="):
        rr = _first_float(parts.get("rr"))
        risk_pct = _first_float(parts.get("sl_distance_pct")) * 100

        direction = parts.get("direction", "")
        bias = parts.get("liquidity_bias", "")

        label = "SL Wide" if risk_pct >= 3.0 else "RR Low"
        extra = f" | {direction}" if direction else ""
        if bias:
            extra += f" | bias {bias}"

        return f"RR {rr:.2f} | Risk {risk_pct:.1f}% | {label}{extra}"

    if len(raw) > 120:
        return raw[:117] + "..."

    return raw



# ─────────────────────────────────────────────────────────────────────────────
# Analytics PDF — Telegram sender
# ─────────────────────────────────────────────────────────────────────────────

async def _generate_and_send_analytics_pdf(days_label: str) -> tuple[bool, str]:
    """Build an Analytics PDF (rejected + cancelled/expired summary) and send to Telegram."""
    import io
    import os
    import httpx
    from reportlab.lib import colors as rlcolors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    try:
        # ── date filter ──────────────────────────────────────────────
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
            rej_res = await s.execute(rej_stmt)
            rejected_all = rej_res.scalars().all()

            canc_stmt = select(Trade).where(
                Trade.status.in_([TradeStatus.CANCELLED.value, TradeStatus.EXPIRED.value])
            ).order_by(desc(Trade.closed_at), desc(Trade.created_at))
            if cutoff:
                canc_stmt = canc_stmt.where(Trade.created_at >= cutoff.replace(tzinfo=None))
            canc_res = await s.execute(canc_stmt)
            cancelled_all = canc_res.scalars().all()

        total_rej = len(rejected_all)
        total_cancelled = sum(
            1 for t in cancelled_all
            if str(getattr(t.status, "value", t.status)).endswith("CANCELLED")
        )
        total_expired = sum(
            1 for t in cancelled_all
            if str(getattr(t.status, "value", t.status)).endswith("EXPIRED")
        )

        # ── aggregations ─────────────────────────────────────────────
        reason_counts: dict[str, int] = {}
        for r in rejected_all:
            reason = str(r.rejection_reason or "Unknown").strip()
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        reason_sorted = sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)[:15]

        rej_by_sym: dict[str, int] = {}
        for r in rejected_all:
            sym = str(r.symbol or "Unknown").replace("USDT", "")
            rej_by_sym[sym] = rej_by_sym.get(sym, 0) + 1
        rej_sym_sorted = sorted(rej_by_sym.items(), key=lambda x: x[1], reverse=True)[:10]

        canc_by_sym: dict[str, int] = {}
        for t in cancelled_all:
            sym = str(t.symbol or "Unknown").replace("USDT", "")
            canc_by_sym[sym] = canc_by_sym.get(sym, 0) + 1
        canc_sym_sorted = sorted(canc_by_sym.items(), key=lambda x: x[1], reverse=True)[:10]

        # ── PDF build ────────────────────────────────────────────────
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        period_label = f"Last {days_label}" if days_label != "All" else "All Time"
        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=A4,
            leftMargin=1.5 * cm, rightMargin=1.5 * cm,
            topMargin=1.5 * cm, bottomMargin=1.5 * cm,
        )
        styles = getSampleStyleSheet()
        title_s = ParagraphStyle("t", parent=styles["Title"], fontSize=18, spaceAfter=4)
        sub_s   = ParagraphStyle("s", parent=styles["Normal"], fontSize=10,
                                  textColor=rlcolors.gray, spaceAfter=10)
        hdr_s   = ParagraphStyle("h", parent=styles["Heading2"], fontSize=12,
                                  spaceBefore=12, spaceAfter=6)
        cell_s  = ParagraphStyle("c", parent=styles["Normal"], fontSize=7, leading=9)

        HDR_BG   = rlcolors.HexColor("#1a1a2e")
        ROW_A    = rlcolors.HexColor("#f9f9f9")
        ROW_B    = rlcolors.white
        GRID_CLR = rlcolors.HexColor("#cccccc")

        def _tbl_style(has_header=True):
            s = [
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("ROWBACKGROUNDS", (0, 1 if has_header else 0), (-1, -1), [ROW_A, ROW_B]),
                ("GRID", (0, 0), (-1, -1), 0.3, GRID_CLR),
                ("LEFTPADDING",  (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING",    (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
            if has_header:
                s += [
                    ("BACKGROUND", (0, 0), (-1, 0), HDR_BG),
                    ("TEXTCOLOR",  (0, 0), (-1, 0), rlcolors.white),
                    ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE",   (0, 0), (-1, 0), 9),
                ]
            return TableStyle(s)

        story = [
            Paragraph("HunterBot Analytics Report", title_s),
            Paragraph(f"Generated {now_str}  |  Period: {period_label}", sub_s),
            Spacer(1, 0.3 * cm),
        ]

        # KPI summary table
        story.append(Paragraph("Summary", hdr_s))
        kpi_data = [
            ["Metric", "Value"],
            ["Period",           period_label],
            ["Rejected Signals", str(total_rej)],
            ["Cancelled Trades", str(total_cancelled)],
            ["Expired Trades",   str(total_expired)],
            ["Total Canc+Exp",   str(total_cancelled + total_expired)],
        ]
        kpi_tbl = Table(kpi_data, colWidths=[7 * cm, 9 * cm])
        kpi_tbl.setStyle(_tbl_style())
        story += [kpi_tbl, Spacer(1, 0.3 * cm)]

        # Rejection Reasons
        if reason_sorted:
            story.append(Paragraph("Top Rejection Reasons", hdr_s))
            r_data = [["Reason", "Count"]] + [[r, str(c)] for r, c in reason_sorted]
            r_tbl = Table(r_data, colWidths=[13 * cm, 3 * cm])
            r_tbl.setStyle(_tbl_style())
            story += [r_tbl, Spacer(1, 0.3 * cm)]

        # Rejections by Symbol
        if rej_sym_sorted:
            story.append(Paragraph("Rejections by Symbol", hdr_s))
            rs_data = [["Symbol", "Count"]] + [[s, str(c)] for s, c in rej_sym_sorted]
            rs_tbl = Table(rs_data, colWidths=[13 * cm, 3 * cm])
            rs_tbl.setStyle(_tbl_style())
            story += [rs_tbl, Spacer(1, 0.3 * cm)]

        # Cancelled/Expired by Symbol
        if canc_sym_sorted:
            story.append(Paragraph("Cancelled / Expired by Symbol", hdr_s))
            cs_data = [["Symbol", "Count"]] + [[s, str(c)] for s, c in canc_sym_sorted]
            cs_tbl = Table(cs_data, colWidths=[13 * cm, 3 * cm])
            cs_tbl.setStyle(_tbl_style())
            story.append(cs_tbl)

        doc.build(story)
        buf.seek(0)

        # ── Telegram send ─────────────────────────────────────────────
        from ui.app import _cfg_section as _cs
        env_cfg   = getattr(__import__("src.core.config", fromlist=["settings"]), "settings", None)
        env_block = getattr(env_cfg, "env", None) if env_cfg else None
        token     = getattr(env_block, "telegram_bot_token", None)
        chat_id   = getattr(env_block, "telegram_chat_id", None)
        alerts_cfg = _cs("alerts")
        if not chat_id:
            chat_id = (
                alerts_cfg.get("telegram_chat_id")
                or alerts_cfg.get("chat_id")
                or alerts_cfg.get("telegram_channel_id")
                or os.getenv("TELEGRAM_CHAT_ID")
                or os.getenv("TG_CHAT_ID")
            )
        if not token:
            return False, "Telegram bot token not configured"
        if not chat_id:
            return False, "Telegram chat id not configured"

        fname = f"HunterBot_Analytics_{datetime.now().strftime('%Y%m%d%H%M')}.pdf"
        url   = f"https://api.telegram.org/bot{token}/sendDocument"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                data={"chat_id": str(chat_id), "caption": f"HunterBot Analytics Report\n{now_str} | {period_label}"},
                files={"document": (fname, buf.getvalue(), "application/pdf")},
            )
            resp.raise_for_status()
        return True, "Analytics report sent"

    except Exception as e:
        return False, str(e)


# ─────────────────────────────────────────────────────────────────────────────
# /  Overview
# ─────────────────────────────────────────────────────────────────────────────

@ui.page("/")
async def page_overview() -> None:
    container = _page_shell("Overview", "MISSION CONTROL • REAL-TIME BOT MONITOR")
    _auto_refresh_on_scan()

    await render_mission_control(
        container=container,
        bot=bot,
        executor=executor,
        live_prices=live_prices,
        details_link=_details_link,
    )


# ─────────────────────────────────────────────────────────────────────────────
# /scanner
# ─────────────────────────────────────────────────────────────────────────────

@ui.page("/scanner")
async def page_scanner() -> None:
    container = _page_shell("Scanner", "LAST SCAN • SHORTLIST")
    _auto_refresh_on_scan()

    with container:
        with ui.element("div").classes("card"):
            ui.html('<div class="card-title">Scanner</div>')
            if not bot.last_scan_results:
                empty_state("No scan results yet. Run a scan first.")
            else:
                rows = sorted(
                    bot.last_scan_results,
                    key=lambda r: r["extremity_score"],
                    reverse=True,
                )
                tbl = """
                <div class="table-wrap"><table class="lh-table">
                <thead><tr>
                <th>Symbol</th><th>Price</th><th>24h Volume</th><th>OI</th><th>Funding</th><th>L/S</th><th>OI Δ4h</th><th>Score</th><th>Reasons</th>
                </tr></thead><tbody>
                """
                for r in rows:
                    reasons = ", ".join(r["reasons"]) if r["reasons"] else "—"
                    tbl += f"""
                    <tr>
                      <td class="sym-cell">{r["symbol"]}</td>
                      <td class="mono">{fmt_price(r["price"])}</td>
                      <td class="mono tabular-nums">{fmt_money_short(r["volume_24h_usd"])}</td>
                      <td class="mono tabular-nums">{fmt_money_short(r["open_interest_usd"])}</td>
                      <td class="mono tabular-nums">{r["funding_rate"]*100:+.3f}%</td>
                      <td class="mono tabular-nums">{r["long_short_ratio"]:.2f}</td>
                      <td class="mono tabular-nums">{r["oi_change_4h_pct"]*100:+.1f}%</td>
                      <td>{score_bar_cell(r["extremity_score"])}</td>
                      <td style="color:var(--text-muted);font-size:14px;max-width:220px;white-space:normal">{reasons}</td>
                    </tr>
                    """
                tbl += "</tbody></table></div>"
                ui.html(tbl)


# ─────────────────────────────────────────────────────────────────────────────
# /signals
# ─────────────────────────────────────────────────────────────────────────────

@ui.page("/signals")
async def page_signals() -> None:
    container = _page_shell("Signals", "DECISION ENGINE • SCORE BREAKDOWN")
    _auto_refresh_on_scan()

    # Local escaping only for text rendered from runtime data.
    # This does not change trading logic; it only keeps the UI safe/clean.
    from html import escape as _esc

    with container:
        if not bot.last_decisions:
            with ui.element("div").classes("card"):
                empty_state("No decisions computed yet. Run a scan first.")
            return

        # UI display only: show all computed decisions instead of hiding WAIT / low-score rows.
        # This does not change trading logic, score, entry, trigger, or execution.
        visible = list(bot.last_decisions.values())

        if not visible:
            with ui.element("div").classes("card"):
                empty_state("No decisions computed yet.")
            return

        def score_theme(score: float) -> tuple[str, str, str, str]:
            if score >= 80:
                return ("signal-card signal-card-green", "#00f58c", "rgba(0,245,140,.14)", "rgba(0,245,140,.26)")
            if score >= 60:
                return ("signal-card signal-card-blue", "#53a7ff", "rgba(83,167,255,.14)", "rgba(83,167,255,.26)")
            return ("signal-card signal-card-red", "#ff5b61", "rgba(255,91,97,.14)", "rgba(255,91,97,.24)")

        def component_color(val: float) -> str:
            if val >= 80:
                return "#19f58d"
            if val >= 60:
                return "#53a7ff"
            if val >= 40:
                return "#f6b73c"
            return "#ff5b61"

        def _trigger_display_value(decision: dict) -> float:
            """UI-only value. Not part of decision score."""
            if decision.get("confirmation") is True:
                return 100.0
            if decision.get("direction") in ("LONG", "SHORT"):
                return 50.0
            return 0.0

        def _compact_trigger_summary(summary: str) -> str:
            """Keep trigger display short in cards/dialog header.

            Example:
            "0/2 confirmations (need 2 from 2 available) [2 skipped]"
            -> "0/2 confirmations"
            """
            s = str(summary or "—").strip()
            if not s or s == "—":
                return "—"
            if " confirmations" in s:
                return s.split(" confirmations", 1)[0].strip() + " confirmations"
            return s

        def _extract_heat_density(reasoning: list[str]) -> dict:
            """Extract Heat Density telemetry from DecisionEngine reasoning.

            Expected line example:
            Heat Density: STRONG_SHORT_HEAT (ratio=2.30, boost=+8.0)
            """
            data = {
                "classification": "—",
                "ratio": None,
                "boost": 0.0,
                "line": "Heat Density: —",
            }

            for line in reasoning:
                s = str(line or "").strip()
                if not s.startswith("Heat Density:"):
                    continue

                data["line"] = s

                try:
                    after = s.split("Heat Density:", 1)[1].strip()
                    cls = after.split("(", 1)[0].strip()
                    if cls:
                        data["classification"] = cls

                    if "ratio=" in s:
                        ratio_part = s.split("ratio=", 1)[1].split(",", 1)[0].split(")", 1)[0]
                        data["ratio"] = float(ratio_part.strip())

                    if "boost=" in s:
                        boost_part = s.split("boost=", 1)[1].split(")", 1)[0].split(",", 1)[0]
                        data["boost"] = float(boost_part.strip())

                except Exception:
                    pass

                break

            return data

        def _heat_color(classification: str, boost: float) -> str:
            cls = str(classification or "")
            if "CONFLICT" in cls or boost < 0:
                return "#ff5b61"
            if "STRONG" in cls and boost > 0:
                return "#19f58d"
            if boost > 0:
                return "#53a7ff"
            return "var(--text-muted)"

        def _heat_pill(classification: str, boost: float) -> str:
            color = _heat_color(classification, boost)
            label = _esc(str(classification or "—"))
            return (
                f'<span class="pill" style="border-color:{color};'
                f'color:{color};background:rgba(255,255,255,.04);'
                f'font-size:11px">{label}</span>'
            )

        def _render_component(label: str, val: float, note: str = "") -> str:
            c = component_color(val)
            note_html = f'<div style="font-size:10px;color:var(--text-muted);margin-top:2px">{_esc(note)}</div>' if note else ""
            return f"""
            <div class="signal-comp">
              <div class="signal-comp-label">{_esc(label)}</div>
              <div class="signal-comp-value" style="color:{c}">{val:.0f}</div>
              <div class="signal-comp-bar"><div class="signal-comp-fill" style="width:{max(0,min(val,100))}%;background:{c}"></div></div>
              {note_html}
            </div>
            """

        with ui.element("div").classes("signals-grid"):
            for d in sorted(visible, key=lambda x: x.get("score", 0), reverse=True):
                score = float(d.get("score", 0) or 0)
                card_cls, score_fg, score_bg, score_border = score_theme(score)
                price = fmt_price(d.get("price", 0))
                direction_html = direction_pill(d.get("direction"))
                state_html = state_pill(d.get("state"))
                regime_html = regime_pill(d.get("regime"))
                comps = d.get("components", {}) or {}
                raw_trigger_summary = str(d.get("trigger_summary") or "—")
                trigger_summary = _compact_trigger_summary(raw_trigger_summary)
                trigger_val = _trigger_display_value(d)

                # Score components: the first 4 are the real DecisionEngine components.
                # TRIGGER is displayed for timing visibility only; it is not part of score.
                comp_html = ""
                comp_html += _render_component("LIQUIDITY", float(comps.get("liquidity_imbalance", 0) or 0))
                comp_html += _render_component("POSITIONING", float(comps.get("positioning_extremity", 0) or 0))
                comp_html += _render_component("OI BEHAVIOR", float(comps.get("oi_behavior", 0) or 0))
                comp_html += _render_component("PRICE ACTION", float(comps.get("price_action_confluence", 0) or 0))
                comp_html += _render_component("TRIGGER", trigger_val, "display only")

                reasoning = [str(r) for r in (d.get("reasoning") or [])]
                heat = _extract_heat_density(reasoning)
                heat_class = str(heat.get("classification") or "—")
                heat_ratio = heat.get("ratio")
                heat_boost = float(heat.get("boost") or 0.0)
                heat_pill_html = _heat_pill(heat_class, heat_boost)
                heat_ratio_text = f"{heat_ratio:.2f}" if isinstance(heat_ratio, (int, float)) else "—"
                heat_boost_text = f"{heat_boost:+.1f}"
                reasoning_preview = reasoning[:5]
                reasoning_html = "".join(f"<li>{_esc(r)}</li>" for r in reasoning_preview)
                full_reasoning_html = "".join(f"<li>{_esc(r)}</li>" for r in reasoning)

                ctx_html = f"""
                <div class="signal-context-grid">
                  <div><div class="signal-mini-label">FUNDING</div><div class="signal-mini-value mono">{d.get("funding_rate",0)*100:+.3f}%</div></div>
                  <div><div class="signal-mini-label">L/S RATIO</div><div class="signal-mini-value mono">{d.get("ls_ratio",0):.2f}</div></div>
                  <div><div class="signal-mini-label">OPEN INTEREST</div><div class="signal-mini-value mono">{fmt_money_short(d.get("oi_usd",0))}</div></div>
                  <div><div class="signal-mini-label">LIQ. BIAS</div><div class="signal-mini-value mono">{_esc(str(d.get("dominant_side","—")))}</div></div>
                  <div><div class="signal-mini-label">TRIGGER</div><div class="signal-mini-value mono">{_esc(trigger_summary)}</div></div>
                  <div><div class="signal-mini-label">HEAT</div><div class="signal-mini-value mono">{heat_pill_html}</div></div>
                  <div><div class="signal-mini-label">HEAT RATIO</div><div class="signal-mini-value mono">{_esc(heat_ratio_text)}</div></div>
                  <div><div class="signal-mini-label">HEAT BOOST</div><div class="signal-mini-value mono" style="color:{_heat_color(heat_class, heat_boost)}">{_esc(heat_boost_text)}</div></div>
                </div>
                """

                symbol = _esc(str(d.get("symbol", "—")))

                with ui.element("div").classes(card_cls):
                    ui.html(f"""
                      <div class="signal-top">
                        <div class="signal-head-left">
                          <div class="signal-symbol-row"><span class="signal-symbol">{symbol}</span><span class="signal-price mono">{price}</span></div>
                          <div class="signal-tags">{direction_html}{state_html}{regime_html}</div>
                        </div>
                        <div class="signal-score-box" style="color:{score_fg};background:{score_bg};border-color:{score_border}">
                          <div class="signal-score-label">SCORE</div>
                          <div class="signal-score-value mono">{score:.1f}</div>
                        </div>
                      </div>
                      <div class="signal-divider"></div>
                      <div class="signal-comp-grid">{comp_html}</div>
                      <div class="signal-divider"></div>
                      {ctx_html}
                      <div class="signal-divider"></div>
                      <div class="signal-reasoning-title">REASONING</div>
                      <ul class="signal-reasoning-list">{reasoning_html}</ul>
                    """)

                    if len(reasoning) > len(reasoning_preview):
                        score_rows = [
                            ("Liquidity", float(comps.get("liquidity_imbalance", 0) or 0), "Execution score"),
                            ("Positioning", float(comps.get("positioning_extremity", 0) or 0), "Gate strength"),
                            ("OI Behavior", float(comps.get("oi_behavior", 0) or 0), "Execution score"),
                            ("Price Action", float(comps.get("price_action_confluence", 0) or 0), "Execution score"),
                            ("Trigger", trigger_val, "Display only"),
                            ("Heat Density", max(0.0, min(100.0, 50.0 + heat_boost * 5.0)), f"{heat_class} | boost {heat_boost_text}"),
                        ]
                        score_table_html = "".join(
                            f"""
                            <tr>
                              <td>{_esc(name)}</td>
                              <td class="mono" style="text-align:right;color:{component_color(val)}">{val:.0f}</td>
                              <td style="color:var(--text-muted)">{_esc(note)}</td>
                            </tr>
                            """
                            for name, val, note in score_rows
                        )

                        trigger_lines = [r for r in reasoning if r.startswith("Trigger/")]
                        other_lines = [r for r in reasoning if not r.startswith("Trigger/")]
                        trigger_detail_html = "".join(
                            f"""
                            <tr>
                              <td class="mono">{_esc(line.split(':', 1)[0].replace('Trigger/', ''))}</td>
                              <td style="color:var(--text-muted)">{_esc(line.split(':', 1)[1].strip() if ':' in line else line)}</td>
                            </tr>
                            """
                            for line in trigger_lines
                        ) or '<tr><td colspan="2" style="color:var(--text-muted)">No trigger details available.</td></tr>'
                        other_reasoning_html = "".join(f"<li>{_esc(r)}</li>" for r in other_lines)

                        summary_rows = [
                            ("Symbol", symbol),
                            ("Score", f"{score:.1f}"),
                            ("Direction", str(d.get("direction", "—"))),
                            ("State", str(d.get("state", "—"))),
                            ("Regime", str(d.get("regime", "—"))),
                            ("Trigger", trigger_summary),
                            ("Heat", f"{heat_class} | ratio {heat_ratio_text} | boost {heat_boost_text}"),
                        ]
                        summary_table_html = "".join(
                            f"""
                            <tr>
                              <td style="
                                    color:var(--text-muted);
                                    width:120px;
                                    min-width:120px;
                                    vertical-align:top;
                                    white-space:nowrap;
                                ">
                                {_esc(k)}
                              </td>
                              <td class="mono" style="
                                    text-align:right;
                                    white-space:normal;
                                    word-break:break-word;
                                    overflow-wrap:anywhere;
                                    max-width:260px;
                                    vertical-align:top;
                                    line-height:1.45;
                                ">
                                {_esc(str(v))}
                              </td>
                            </tr>
                            """
                            for k, v in summary_rows
                        )

                        context_rows = [
                            ("Funding", f"{d.get('funding_rate', 0)*100:+.3f}%"),
                            ("L/S Ratio", f"{d.get('ls_ratio', 0):.2f}"),
                            ("Open Interest", fmt_money_short(d.get("oi_usd", 0))),
                            ("Liquidity Bias", str(d.get("dominant_side", "—"))),
                            ("Primary Target", fmt_price(d.get("primary_target") or 0) if d.get("primary_target") else "—"),
                            ("Heat Density", heat_class),
                            ("Heat Ratio", heat_ratio_text),
                            ("Heat Boost", heat_boost_text),
                        ]
                        context_table_html = "".join(
                            f"""
                            <tr>
                              <td style="color:var(--text-muted)">{_esc(k)}</td>
                              <td class="mono" style="text-align:right">{_esc(str(v))}</td>
                            </tr>
                            """
                            for k, v in context_rows
                        )

                        with ui.dialog() as details_dialog, ui.card().style(
    "width:1150px;"
    "max-width:96vw;"
    "max-height:90vh;"
    "overflow:auto;"
    "background:#0b1220;"
    "border:1px solid rgba(255,255,255,.12);"
    "border-radius:16px;"
    "box-shadow:0 25px 80px rgba(0,0,0,.80);"
    "padding:22px 28px;"
):
                            ui.html(f"""
                                <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:18px;margin-bottom:14px">
                                  <div>
                                    <div style="font-size:20px;font-weight:900;color:var(--text);letter-spacing:.02em">
                                      {symbol} — Decision Details
                                    </div>
                                    <div style="font-size:13px;color:var(--text-muted);margin-top:4px">
                                      Score {score:.1f} • Direction {_esc(str(d.get("direction", "—")))} • Trigger {_esc(trigger_summary)}
                                    </div>
                                  </div>
                                </div>

                                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;margin-bottom:14px">
                                  <div style="border:1px solid var(--border);border-radius:12px;padding:12px;background:#111827; backdrop-filter:blur(14px);">
                                    <div style="font-size:12px;font-weight:800;color:var(--text-muted);letter-spacing:.08em;text-transform:uppercase;margin-bottom:8px">Summary</div>
                                    <table class="lh-table" style="width:100%"><tbody>{summary_table_html}</tbody></table>
                                  </div>

                                  <div style="border:1px solid var(--border);border-radius:12px;padding:12px;background:#111827; backdrop-filter:blur(14px);">
                                    <div style="font-size:12px;font-weight:800;color:var(--text-muted);letter-spacing:.08em;text-transform:uppercase;margin-bottom:8px">Market Context</div>
                                    <table class="lh-table" style="width:100%"><tbody>{context_table_html}</tbody></table>
                                  </div>
                                </div>

                                <div style="border:1px solid var(--border);border-radius:12px;padding:12px;background:#111827; backdrop-filter:blur(14px);;margin-bottom:14px">
                                  <div style="font-size:12px;font-weight:800;color:var(--text-muted);letter-spacing:.08em;text-transform:uppercase;margin-bottom:8px">Score Breakdown</div>
                                  <table class="lh-table" style="width:100%">
                                    <thead><tr><th>Component</th><th style="text-align:right">Value</th><th>Role</th></tr></thead>
                                    <tbody>{score_table_html}</tbody>
                                  </table>
                                </div>

                                <div style="border:1px solid var(--border);border-radius:12px;padding:12px;background:#111827; backdrop-filter:blur(14px);;margin-bottom:14px">
                                  <div style="font-size:12px;font-weight:800;color:var(--text-muted);letter-spacing:.08em;text-transform:uppercase;margin-bottom:8px">Heat Density</div>
                                  <table class="lh-table" style="width:100%">
                                    <tbody>
                                      <tr><td style="color:var(--text-muted)">Classification</td><td class="mono" style="text-align:right;color:{_heat_color(heat_class, heat_boost)}">{_esc(heat_class)}</td></tr>
                                      <tr><td style="color:var(--text-muted)">Ratio</td><td class="mono" style="text-align:right">{_esc(heat_ratio_text)}</td></tr>
                                      <tr><td style="color:var(--text-muted)">Boost</td><td class="mono" style="text-align:right;color:{_heat_color(heat_class, heat_boost)}">{_esc(heat_boost_text)}</td></tr>
                                      <tr><td style="color:var(--text-muted)">Raw Line</td><td class="mono" style="text-align:right;white-space:normal">{_esc(str(heat.get("line", "—")))}</td></tr>
                                    </tbody>
                                  </table>
                                </div>

                                <div style="border:1px solid var(--border);border-radius:12px;padding:12px;background:#111827; backdrop-filter:blur(14px);;margin-bottom:14px">
                                  <div style="font-size:12px;font-weight:800;color:var(--text-muted);letter-spacing:.08em;text-transform:uppercase;margin-bottom:8px">Trigger Details</div>
                                  <table class="lh-table" style="width:100%">
                                    <thead><tr><th>Check</th><th>Detail</th></tr></thead>
                                    <tbody>{trigger_detail_html}</tbody>
                                  </table>
                                </div>

                                <div style="border:1px solid var(--border);border-radius:12px;padding:12px;background:#111827; backdrop-filter:blur(14px);">
                                  <div style="font-size:12px;font-weight:800;color:var(--text-muted);letter-spacing:.08em;text-transform:uppercase;margin-bottom:8px">Reasoning Notes</div>
                                  <ul class="signal-reasoning-list" style="max-height:none;line-height:1.75;margin:0">{other_reasoning_html}</ul>
                                </div>
                            """)
                            ui.button("Close", on_click=details_dialog.close).props("flat").style(
                                "margin-top:12px;background:rgba(255,255,255,.06);"
                                "color:var(--text-muted);border:1px solid var(--border);"
                            )

                        ui.button(
                            f"More ({len(reasoning) - len(reasoning_preview)} lines)",
                            on_click=details_dialog.open,
                        ).props("flat dense").style(
                            "margin-top:8px;background:rgba(255,255,255,.06);"
                            "color:var(--text-muted);border:1px solid var(--border);"
                            "border-radius:5px;font-size:12px;padding:3px 8px"
                        )




def _trade_journey_html(t: Trade) -> str:
    """Visual trade journey for symbol trade details.

    UI-only helper. It does not change trading logic or database state.
    """
    from html import escape as _esc

    direction = _dir_text(t.direction)
    status = _status_text(str(t.status), t.pnl_usd or 0.0)
    entry = _trade_entry_price(t)
    exit_price = float(t.exit_price or 0.0)
    pnl = float(t.pnl_usd or 0.0)
    pnl_r = float(t.pnl_r or 0.0)
    realized = float(getattr(t, "realized_pnl", 0.0) or 0.0)
    notes = str(getattr(t, "notes", "") or "")

    created = _saudi_time(t.created_at) if getattr(t, "created_at", None) else "—"
    triggered = _saudi_time(t.triggered_at) if getattr(t, "triggered_at", None) else "—"
    closed = _saudi_time(t.closed_at) if getattr(t, "closed_at", None) else "—"

    pnl_cls = "text-success" if pnl >= 0 else "text-danger"

    def _yes_no(flag) -> str:
        return "✓" if bool(flag) else "—"

    def _step(title: str, value: str, state: str = "done") -> str:
        if state == "good":
            color = "#19f58d"
            mark = "✓"
        elif state == "bad":
            color = "#ff5b61"
            mark = "✕"
        elif state == "warn":
            color = "#f6c453"
            mark = "!"
        else:
            color = "var(--text-muted)"
            mark = "•"

        return f"""
        <div style="display:flex;gap:10px;align-items:flex-start;padding:9px 0;border-bottom:1px solid rgba(255,255,255,.06)">
          <div style="width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;
                      border:1px solid {color};color:{color};font-size:12px;font-weight:900;flex-shrink:0">{mark}</div>
          <div style="flex:1">
            <div style="font-size:13px;font-weight:800;color:var(--text)">{_esc(title)}</div>
            <div class="mono" style="font-size:12px;color:var(--text-muted);margin-top:2px;white-space:normal">{_esc(value)}</div>
          </div>
        </div>
        """

    tp1_hit = bool(getattr(t, "tp1_hit", False))
    be_locked = bool(getattr(t, "layer2_locked", False))
    trailing = bool(getattr(t, "trailing_active", False))

    result_state = "good" if pnl >= 0 else "bad"
    if status in {"CANCELLED", "EXPIRED"}:
        result_state = "warn"

    steps = []
    steps.append(_step(
        "Signal Created",
        f"{created} | score={float(getattr(t, 'setup_score', 0.0) or 0.0):.1f} | {direction} | {str(getattr(t, 'market_state', '—') or '—')}",
        "done",
    ))

    if str(getattr(t, "status", "")) in {TradeStatus.PENDING.value}:
        steps.append(_step(
            "Waiting For Entry",
            f"Entry zone {fmt_price(getattr(t, 'entry_zone_low', 0.0) or 0.0)} → {fmt_price(getattr(t, 'entry_zone_high', 0.0) or 0.0)}",
            "warn",
        ))
    else:
        steps.append(_step(
            "Position Triggered",
            f"{triggered} | entry={fmt_price(entry)} | size={fmt_money_short(getattr(t, 'position_size_usd', 0.0) or 0.0)}",
            "good",
        ))

    steps.append(_step(
        "Risk Plan",
        (
            f"SL={fmt_price(getattr(t, 'stop_loss', 0.0) or 0.0)} | "
            f"TP1={fmt_price(getattr(t, 'take_profit_1', 0.0) or 0.0)} | "
            f"TP2={fmt_price(getattr(t, 'take_profit_2', 0.0) or 0.0)} | "
            f"TP3={fmt_price(getattr(t, 'take_profit_3', 0.0) or 0.0)}"
        ),
        "done",
    ))

    steps.append(_step(
        "TP1 / Partial",
        f"{_yes_no(tp1_hit)} TP1 hit | realized={realized:+.2f}",
        "good" if tp1_hit else "done",
    ))

    steps.append(_step(
        "Break Even",
        f"{_yes_no(be_locked)} BE locked | SL layer2={fmt_price(getattr(t, 'sl_layer2', 0.0) or 0.0)}",
        "good" if be_locked else "done",
    ))

    steps.append(_step(
        "Trailing",
        (
            f"{_yes_no(trailing)} trailing active | "
            f"max_r={float(getattr(t, 'max_r_reached', 0.0) or 0.0):.2f}R | "
            f"anchor={fmt_price(getattr(t, 'trailing_anchor', 0.0) or 0.0)}"
        ),
        "good" if trailing else "done",
    ))

    steps.append(_step(
        "Result",
        f"{closed} | status={status} | exit={fmt_price(exit_price)} | pnl=${pnl:+,.2f} | {pnl_r:+.2f}R",
        result_state,
    ))

    # Try to surface Heat Density if it was saved in notes/reasoning text.
    heat_lines = [x.strip() for x in notes.split("|") if "Heat Density:" in x]
    heat_html = ""
    if heat_lines:
        heat_html = f"""
        <div style="margin-top:12px;border:1px solid var(--border);border-radius:12px;padding:12px;background:#111827">
          <div style="font-size:12px;font-weight:900;color:var(--text-muted);letter-spacing:.08em;text-transform:uppercase;margin-bottom:8px">Heat Density</div>
          <div class="mono" style="font-size:12px;color:var(--text);white-space:normal">{_esc(heat_lines[-1])}</div>
        </div>
        """

    return f"""
    <div style="border:1px solid var(--border);border-radius:12px;padding:12px;background:#0f172a;margin:10px 0 12px 0">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:8px">
        <div>
          <div style="font-size:13px;font-weight:900;color:var(--text-muted);letter-spacing:.08em;text-transform:uppercase">Trade Journey</div>
          <div style="font-size:12px;color:var(--text-muted);margin-top:2px">Lifecycle from signal to exit</div>
        </div>
        <div class="mono {pnl_cls}" style="font-size:13px;font-weight:900">${pnl:+,.2f} / {pnl_r:+.2f}R</div>
      </div>
      {''.join(steps)}
      {heat_html}
    </div>
    """


async def _manual_close_trade(trade_id: int) -> tuple[bool, str]:
    """Manual Paper Trading close.

    - PENDING trades are cancelled.
    - TRIGGERED trades are closed at latest live price.
    - Profitable close -> CLOSED_TP.
    - Losing close -> CLOSED_SL.

    This does NOT call executor.close(), because some local versions of
    OutcomeLogger.log() are instance methods and can raise:
    OutcomeLogger.log() missing 1 required positional argument: 'trade'
    """
    try:
        async with AsyncSessionLocal() as s:
            t = await s.get(Trade, int(trade_id))

            if t is None:
                return False, "Trade not found"

            status = str(getattr(t.status, "value", t.status))
            symbol = str(t.symbol or "")

            if status == TradeStatus.PENDING.value:
                t.status = TradeStatus.CANCELLED.value
                t.closed_at = datetime.utcnow()
                t.notes = f"{t.notes or ''} | MANUAL CANCEL"
                await s.commit()
                return True, f"{symbol} pending trade cancelled"

            if status != TradeStatus.TRIGGERED.value:
                return False, f"Trade is not open: {status}"

            price = live_prices.get_price(symbol)

            if price is None:
                updater = getattr(live_prices, "update_prices", None)
                if callable(updater):
                    maybe_result = updater()
                    if hasattr(maybe_result, "__await__"):
                        await maybe_result
                price = live_prices.get_price(symbol)

            if price is None:
                return False, f"No live price available for {symbol}"

            price = float(price)
            entry = float(t.actual_entry_price or _trade_entry_price(t) or 0.0)
            if entry <= 0:
                return False, f"Invalid entry price for {symbol}"

            position_size = float(t.position_size_usd or 0.0)
            units = position_size / entry if entry else 0.0

            direction = str(getattr(t.direction, "value", t.direction))
            if direction == TradeDirection.SHORT.value:
                open_pnl = (entry - price) * units
            else:
                open_pnl = (price - entry) * units

            total_pnl = open_pnl + float(t.realized_pnl or 0.0)
            risk = float(t.risk_amount_usd or 1.0)
            pnl_r = total_pnl / risk if risk else 0.0

            fee_pct = float(getattr(executor, "fee_pct", 0.0) or 0.0)

            t.exit_price = price
            t.pnl_usd = total_pnl
            t.pnl_r = pnl_r
            t.fees_usd = float(t.fees_usd or 0.0) + position_size * fee_pct
            t.status = TradeStatus.CLOSED_TP.value if total_pnl >= 0 else TradeStatus.CLOSED_SL.value
            t.closed_at = datetime.utcnow()
            t.notes = f"{t.notes or ''} | MANUAL CLOSE @ {price:.8g}"

            await s.commit()
            await s.refresh(t)

        # Outcome logging is optional here. It must never break manual close.
        try:
            from src.learning.outcome_logger import OutcomeLogger

            try:
                await OutcomeLogger.log(t)
            except TypeError:
                await OutcomeLogger().log(t)

        except Exception as e:
            logger.warning(f"Manual close outcome log skipped for {symbol}: {e}")

        return True, f"{symbol} manually closed at {fmt_price(price)}"

    except Exception as e:
        return False, f"Manual close failed: {e}"


@app.get("/manual-close/{trade_id}")
async def manual_close_trade_endpoint(trade_id: int):
    await _manual_close_trade(int(trade_id))
    return RedirectResponse(url="/trades", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# /trades
# ─────────────────────────────────────────────────────────────────────────────

@ui.page("/trades")
async def page_trades() -> None:
    global _rejected_report_limit
    container = _page_shell("Trades", "OPEN • CLOSED POSITIONS")
    _auto_refresh_on_scan()

    with container:
        async with AsyncSessionLocal() as s:
            open_res = await s.execute(
                select(Trade)
                .where(Trade.status.in_([TradeStatus.PENDING.value, TradeStatus.TRIGGERED.value]))
                .order_by(desc(Trade.created_at))
            )
            open_trades = open_res.scalars().all()

            closed_res = await s.execute(
                select(Trade)
                .where(Trade.status.in_([TradeStatus.CLOSED_TP.value, TradeStatus.CLOSED_SL.value]))
                .order_by(desc(Trade.closed_at), desc(Trade.created_at))
                .limit(50)
            )
            closed = closed_res.scalars().all()

            cancelled_res = await s.execute(
                select(Trade)
                .where(Trade.status.in_([TradeStatus.CANCELLED.value, TradeStatus.EXPIRED.value]))
                .order_by(desc(Trade.closed_at), desc(Trade.created_at))
                .limit(50)
            )
            cancelled = cancelled_res.scalars().all()

            rejected_res = await s.execute(
                select(RejectedSignal)
                .order_by(desc(RejectedSignal.created_at), desc(RejectedSignal.id))
                .limit(50)
            )
            rejected_rows = rejected_res.scalars().all()

        # Open
        with ui.element("div").classes("card"):
            ui.html(f'<div class="card-title">Open Positions <span class="pill pill-info-soft" style="margin-left:6px">{len(open_trades)}</span></div>')
            if not open_trades:
                empty_state("No open positions.")
            else:
                tbl = """<div class="table-wrap"><table class="lh-table"><thead><tr>
                <th>Symbol</th><th>Dir</th><th>Status</th><th>Entry</th><th>Price</th><th>P/L $</th><th>P/L %</th><th>SL</th><th>TP1</th><th>TP2</th><th>Size $</th><th>Opened</th><th>Details</th><th>Action</th>
                </tr></thead><tbody>"""
                for t in open_trades:
                    entry_price = _trade_entry_price(t)
                    current_price = live_prices.get_price(t.symbol)
                    direction_text = _dir_text(t.direction)
                    is_triggered = _status_text(str(t.status)) == "TRIGGERED"

                    pnl_usd_html = '<span class="text-muted">—</span>'
                    pnl_pct_html = '<span class="text-muted">—</span>'

                    if is_triggered and current_price and entry_price:
                        if direction_text == "SHORT":
                            pnl_pct = ((entry_price - current_price) / entry_price) * 100
                        else:
                            pnl_pct = ((current_price - entry_price) / entry_price) * 100

                        pnl_usd = float(t.position_size_usd or 0.0) * (pnl_pct / 100.0)
                        pnl_cls = "text-success" if pnl_usd >= 0 else "text-danger"
                        pnl_usd_html = f'<span class="{pnl_cls}">${pnl_usd:+,.2f}</span>'
                        pnl_pct_html = f'<span class="{pnl_cls}">{pnl_pct:+.2f}%</span>'

                    tbl += f"""<tr>
                      <td class="sym-cell">{t.symbol}</td>
                      <td>{direction_pill(direction_text)}</td>
                      <td>{_status_pill(t)}</td>
                      <td class="mono">{fmt_price(entry_price)}</td>
                      <td class="mono" style="color:#5ab3ff;font-weight:700">{fmt_price(current_price or 0)}</td>
                      <td class="mono tabular-nums">{pnl_usd_html}</td>
                      <td class="mono tabular-nums">{pnl_pct_html}</td>
                      <td class="mono text-danger">{fmt_price(t.stop_loss or 0)}</td>
                      <td class="mono" style="color:#f6c453;font-weight:700">{fmt_price(t.take_profit_1 or 0)}</td>
                      <td class="mono" style="color:#5ab3ff;font-weight:700">{fmt_price(t.take_profit_2 or 0)}</td>
                      <td class="mono tabular-nums">{fmt_money_short(t.position_size_usd or 0)}</td>
                      <td class="mono text-muted">{_saudi_time(t.created_at) if t.created_at else "—"}</td>
                      <td>{_details_link(t.symbol)}</td>
                      <td>
                        <a
                          href="/manual-close/{t.id}"
                          class="details-link"
                          style="color:#ff5b61;font-weight:800"
                          onclick="return confirm('Close {t.symbol} manually?')"
                        >Close</a>
                      </td>
                    </tr>"""
                tbl += "</tbody></table></div>"
                ui.html(tbl)

        # Closed
        with ui.element("div").classes("card"):
            ui.html(f'<div class="card-title">Closed Trades <span class="pill pill-muted" style="margin-left:6px">{len(closed)}</span></div>')
            if not closed:
                empty_state("No closed trades yet.")
            else:
                tbl = """<div class="table-wrap"><table class="lh-table"><thead><tr>
                <th>Symbol</th><th>Dir</th><th>Status</th><th>Entry</th><th>Exit</th><th>P/L USD</th><th>R</th><th>Closed</th><th>Details</th>
                </tr></thead><tbody>"""
                for t in closed:
                    pnl = t.pnl_usd or 0
                    cls = "text-success" if pnl >= 0 else "text-danger"
                    closed_at = _saudi_time(t.closed_at) if t.closed_at else "—"
                    tbl += f"""<tr>
                      <td class="sym-cell">{t.symbol}</td>
                      <td>{direction_pill(_dir_text(t.direction))}</td>
                      <td>{_status_pill(t)}</td>
                      <td class="mono">{fmt_price(_trade_entry_price(t))}</td>
                      <td class="mono">{fmt_price(t.exit_price or 0)}</td>
                      <td class="mono tabular-nums {cls}">${pnl:+,.2f}</td>
                      <td class="mono tabular-nums {cls}">{(t.pnl_r or 0):+.2f}R</td>
                      <td class="mono text-muted">{closed_at}</td>
                      <td>{_details_link(t.symbol)}</td>
                    </tr>"""
                tbl += "</tbody></table></div>"
                ui.html(tbl)

        # Cancelled
        with ui.element("div").classes("card"):
            ui.html(f'<div class="card-title">Cancelled Trades <span class="pill pill-info-soft" style="margin-left:6px">{len(cancelled)}</span></div>')
            if not cancelled:
                empty_state("No cancelled trades yet.")
            else:
                tbl = """<div class="table-wrap"><table class="lh-table"><thead><tr>
                <th>Symbol</th><th>Dir</th><th>Status</th><th>Entry</th><th>Exit</th><th>P/L USD</th><th>R</th><th>Closed</th><th>Details</th>
                </tr></thead><tbody>"""
                for t in cancelled:
                    pnl = t.pnl_usd or 0
                    cls = "text-success" if pnl >= 0 else "text-danger"
                    closed_at = _saudi_time(t.closed_at) if t.closed_at else "—"
                    tbl += f"""<tr>
                      <td class="sym-cell">{t.symbol}</td>
                      <td>{direction_pill(_dir_text(t.direction))}</td>
                      <td><span class="pill pill-info-soft">{_status_text(str(t.status))}</span></td>
                      <td class="mono">{fmt_price(_trade_entry_price(t))}</td>
                      <td class="mono">{fmt_price(t.exit_price or 0)}</td>
                      <td class="mono tabular-nums {cls}">${pnl:+,.2f}</td>
                      <td class="mono tabular-nums {cls}">{(t.pnl_r or 0):+.2f}R</td>
                      <td class="mono text-muted">{closed_at}</td>
                      <td>{_details_link(t.symbol)}</td>
                    </tr>"""
                tbl += "</tbody></table></div>"
                ui.html(tbl)

        # Rejected
        with ui.element("div").classes("card"):
            with ui.row().classes("items-center justify-between").style("margin-bottom:12px;width:100%"):
                ui.html(f'<div class="card-title">Rejected Signals <span class="pill pill-info-soft" style="margin-left:6px">{len(rejected_rows)}</span></div>')
                with ui.row().classes("items-center").style("gap:8px"):
                    rejected_limit_select = ui.select(
                        options=["20", "50", "100", "All"],
                        value=_normalize_report_trade_limit(_rejected_report_limit),
                    ).props("outlined dense").style("width:90px")
                    rejected_report_status = ui.label().classes("text-muted").style("font-size:12px")
                    ui.button(
                        "Send Report",
                        on_click=lambda: _send_rejected_report_clicked(
                            rejected_report_status, rejected_limit_select.value
                        ),
                    ).props("flat").style(
                        "background:rgba(255,255,255,.06);color:var(--text-muted);"
                        "border:1px solid var(--border);border-radius:5px;font-size:14px;padding:4px 10px"
                    )
            if not rejected_rows:
                empty_state("No rejected signals yet.")
            else:
                tbl = """<div class="table-wrap"><table class="lh-table">
                <thead><tr>
                <th>Symbol</th><th>Dir</th><th>Category</th><th>Reason</th><th>Score</th><th>State</th><th>Regime</th><th>Details</th><th>Time</th>
                </tr></thead><tbody>"""
                for r in rejected_rows:
                    created_at    = _saudi_time(r.created_at) if r.created_at else "—"
                    category_text = _rejection_category_text(r.category)
                    tbl += f"""<tr>
                      <td class="sym-cell">{r.symbol}</td>
                      <td>{direction_pill(str(r.direction or ""))}</td>
                      <td><span class="pill pill-info-soft" style="font-size:12px">{category_text}</span></td>
                      <td><span class="pill pill-info-soft" style="font-size:12px">{str(r.rejection_reason or "")}</span></td>
                      <td class="mono tabular-nums">{float(r.setup_score or 0):.1f}</td>
                      <td style="font-size:12px;color:var(--text-muted);white-space:normal">{str(r.market_state or "")}</td>
                      <td style="font-size:12px;color:var(--text-muted);white-space:normal">{str(r.market_regime or "")}</td>
                      <td style="color:var(--text-muted);white-space:normal;font-size:12px">{_compact_rejection_details(r.rejection_details)}</td>
                      <td class="mono text-muted">{created_at}</td>
                    </tr>"""
                tbl += "</tbody></table></div>"
                ui.html(tbl)


# ─────────────────────────────────────────────────────────────────────────────
# /analytics  ← NEW — with date filter + PDF Telegram export
# ─────────────────────────────────────────────────────────────────────────────

@ui.page("/analytics")
async def page_analytics() -> None:
    container = _page_shell("Analytics", "REJECTED SIGNALS • CANCELLED / EXPIRED TRADES")
    _auto_refresh_on_scan()

    # ── Period filter state (client-side reactive) ────────────────────
    # We use a NiceGUI button-group so no page reload needed — just re-render charts
    PERIODS = ["7d", "30d", "90d", "All"]
    selected_period = {"value": "30d"}   # mutable dict so closures can mutate it

    async def _load_and_render(period: str, charts_host) -> None:
        """Fetch DB data for given period and re-render charts+KPIs inside charts_host."""
        now_utc = datetime.now(timezone.utc)
        cutoff_map = {"7d": timedelta(days=7), "30d": timedelta(days=30), "90d": timedelta(days=90)}
        cutoff = (now_utc - cutoff_map[period]).replace(tzinfo=None) if period in cutoff_map else None

        async with AsyncSessionLocal() as s:
            rej_stmt = select(RejectedSignal).order_by(desc(RejectedSignal.created_at))
            if cutoff:
                rej_stmt = rej_stmt.where(RejectedSignal.created_at >= cutoff)
            rej_res = await s.execute(rej_stmt)
            rejected_all = rej_res.scalars().all()

            canc_stmt = select(Trade).where(
                Trade.status.in_([TradeStatus.CANCELLED.value, TradeStatus.EXPIRED.value])
            ).order_by(desc(Trade.closed_at), desc(Trade.created_at))
            if cutoff:
                canc_stmt = canc_stmt.where(Trade.created_at >= cutoff)
            canc_res = await s.execute(canc_stmt)
            cancelled_all = canc_res.scalars().all()

        total_rej = len(rejected_all)
        total_cancelled = sum(
            1 for t in cancelled_all
            if str(getattr(t.status, "value", t.status)).endswith("CANCELLED")
        )
        total_expired = sum(
            1 for t in cancelled_all
            if str(getattr(t.status, "value", t.status)).endswith("EXPIRED")
        )

        reason_counts: dict[str, int] = {}
        for r in rejected_all:
            k = str(r.rejection_reason or "Unknown").strip()
            reason_counts[k] = reason_counts.get(k, 0) + 1
        reason_sorted = sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)[:12]

        rej_by_sym: dict[str, int] = {}
        for r in rejected_all:
            sym = str(r.symbol or "Unknown").replace("USDT", "")
            rej_by_sym[sym] = rej_by_sym.get(sym, 0) + 1
        rej_sym_sorted = sorted(rej_by_sym.items(), key=lambda x: x[1], reverse=True)[:10]

        canc_by_sym: dict[str, int] = {}
        for t in cancelled_all:
            sym = str(t.symbol or "Unknown").replace("USDT", "")
            canc_by_sym[sym] = canc_by_sym.get(sym, 0) + 1
        canc_sym_sorted = sorted(canc_by_sym.items(), key=lambda x: x[1], reverse=True)[:10]

        # ── Helper: bar chart HTML ────────────────────────────────────
        def _bar_chart(title: str, data: list, bar_color: str, subtitle: str = "") -> str:
            if not data:
                return (
                    f'<div class="card" style="flex:1;min-width:300px">' 
                    f'<div class="card-title">{title}</div>'
                    f'<div style="color:var(--text-muted);font-size:14px;padding:24px 0;text-align:center">No data for this period</div>'
                    f'</div>'
                )
            max_val = max(v for _, v in data) or 1
            bars = ""
            for label, val in data:
                pct   = (val / max_val) * 100
                short = label[:22] + "…" if len(label) > 22 else label
                bars += (
                    f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">' 
                    f'<div style="width:130px;font-size:12px;color:var(--text-muted);text-align:right;' 
                    f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex-shrink:0" title="{label}">{short}</div>' 
                    f'<div style="flex:1;background:rgba(255,255,255,.06);border-radius:3px;height:18px;overflow:hidden">' 
                    f'<div style="height:100%;width:{pct:.1f}%;background:{bar_color};border-radius:3px;transition:width .4s ease"></div>' 
                    f'</div>' 
                    f'<div style="width:36px;font-size:13px;font-family:var(--font-mono);color:var(--text);text-align:right;flex-shrink:0">{val}</div>' 
                    f'</div>'
                )
            sub = f'<div style="font-size:12px;color:var(--text-muted);margin-bottom:14px">{subtitle}</div>' if subtitle else ""
            return (
                f'<div class="card" style="flex:1;min-width:300px">' 
                f'<div class="card-title">{title}</div>' 
                f'{sub}<div style="margin-top:8px">{bars}</div>' 
                f'</div>'
            )

        # ── Helper: donut SVG ─────────────────────────────────────────
        def _donut_chart(cancelled: int, expired: int) -> str:
            total  = (cancelled + expired) or 1
            radius = 54
            circ   = 2 * 3.14159 * radius
            d1     = (cancelled / total) * circ
            d2     = (expired   / total) * circ
            rot2   = (cancelled / total) * 360 - 90
            seg1 = (
                f'<circle cx="70" cy="70" r="{radius}" fill="none" stroke="#f59e0b" stroke-width="16" ' 
                f'stroke-dasharray="{d1:.2f} {circ-d1:.2f}" ' 
                f'stroke-dashoffset="0" transform="rotate(-90 70 70)"/>' 
            )
            seg2 = (
                f'<circle cx="70" cy="70" r="{radius}" fill="none" stroke="#53a7ff" stroke-width="16" ' 
                f'stroke-dasharray="{d2:.2f} {circ-d2:.2f}" ' 
                f'stroke-dashoffset="0" transform="rotate({rot2:.1f} 70 70)"/>' 
            ) if expired > 0 else ""
            return (
                f'<div class="card" style="flex:1;min-width:260px;max-width:360px">' 
                f'<div class="card-title">Cancelled vs Expired</div>' 
                f'<div style="display:flex;align-items:center;gap:24px;margin-top:12px">' 
                f'<svg width="140" height="140" viewBox="0 0 140 140">' 
                f'<circle cx="70" cy="70" r="{radius}" fill="none" stroke="rgba(255,255,255,.06)" stroke-width="16"/>' 
                f'{seg1}{seg2}' 
                f'<text x="70" y="67" text-anchor="middle" dominant-baseline="middle" ' 
                f'style="font-size:18px;font-weight:700;fill:var(--text);font-family:var(--font-mono)">{cancelled+expired}</text>' 
                f'<text x="70" y="84" text-anchor="middle" dominant-baseline="middle" ' 
                f'style="font-size:10px;fill:var(--text-muted)">TOTAL</text>' 
                f'</svg>' 
                f'<div>' 
                f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">' 
                f'<div style="width:12px;height:12px;border-radius:50%;background:#f59e0b;flex-shrink:0"></div>' 
                f'<span style="font-size:13px;color:var(--text-muted)">Cancelled</span>' 
                f'<span style="margin-left:12px;font-family:var(--font-mono);font-size:13px;color:var(--text)">{cancelled}</span>' 
                f'</div>' 
                f'<div style="display:flex;align-items:center;gap:8px">' 
                f'<div style="width:12px;height:12px;border-radius:50%;background:#53a7ff;flex-shrink:0"></div>' 
                f'<span style="font-size:13px;color:var(--text-muted)">Expired</span>' 
                f'<span style="margin-left:12px;font-family:var(--font-mono);font-size:13px;color:var(--text)">{expired}</span>' 
                f'</div>' 
                f'</div>' 
                f'</div>' 
                f'</div>'
            )

        # ── Render into charts_host ───────────────────────────────────
        charts_host.clear()
        with charts_host:
            # KPI row
            ui.html(f"""
            <div class="kpi-row">
              <div class="kpi">
                <div class="kpi-label">Rejected Signals</div>
                <div class="kpi-val red">{total_rej}</div>
                <div class="kpi-change down">Total rejections</div>
              </div>
              <div class="kpi">
                <div class="kpi-label">Cancelled Trades</div>
                <div class="kpi-val yellow">{total_cancelled}</div>
                <div class="kpi-change">Auto / manual cancelled</div>
              </div>
              <div class="kpi">
                <div class="kpi-label">Expired Trades</div>
                <div class="kpi-val yellow">{total_expired}</div>
                <div class="kpi-change">Timed out without trigger</div>
              </div>
              <div class="kpi">
                <div class="kpi-label">Canc + Expired</div>
                <div class="kpi-val cyan">{total_cancelled + total_expired}</div>
                <div class="kpi-change">Combined</div>
              </div>
            </div>
            """)

            # Row 1: reason + rej-by-sym
            ui.html(
                f'<div style="display:flex;gap:16px;flex-wrap:wrap;width:100%">' 
                + _bar_chart("Rejection Reasons", reason_sorted, "#ff5b61", "Top 12 rejection reasons") 
                + _bar_chart("Rejections by Symbol", rej_sym_sorted, "#53a7ff", "Top 10 symbols") 
                + f'</div>'
            )

            # Row 2: canc-by-sym + donut
            ui.html(
                f'<div style="display:flex;gap:16px;flex-wrap:wrap;width:100%;margin-top:0">' 
                + _bar_chart("Cancelled / Expired by Symbol", canc_sym_sorted, "#f59e0b", "Top 10 symbols") 
                + _donut_chart(total_cancelled, total_expired) 
                + f'</div>'
            )

    # ── Page shell ────────────────────────────────────────────────────
    with container:
        # Top toolbar: period buttons + Send PDF button
        with ui.row().classes("items-center justify-between").style(
            "margin-bottom:16px;width:100%;flex-wrap:wrap;gap:12px"
        ):
            # Period selector
            period_btns: dict[str, ui.html] = {}

            def _btn_style(active: bool) -> str:
                if active:
                    return (
                        "background:rgba(255,255,255,.06);color:var(--accent);font-weight:700;"
                        "border:1px solid var(--border);border-radius:5px;font-size:14px;padding:4px 10px"
                    )
                return (
                    "background:rgba(255,255,255,.06);color:var(--text-muted);"
                    "border:1px solid var(--border);border-radius:5px;"
                    "font-size:14px;padding:4px 10px"
                )

            def _make_period_handler(p: str):
                async def _handler():
                    selected_period["value"] = p
                    for lbl, btn in period_btns.items():
                        btn.props('flat unelevated').style(_btn_style(lbl == p))
                    await _load_and_render(p, charts_area)
                return _handler

            with ui.row().classes("items-center").style("gap:6px"):
                for p in PERIODS:
                    btn = ui.button(
                        p,
                        on_click=_make_period_handler(p),
                    ).props("flat unelevated").style(_btn_style(p == selected_period["value"]))
                    period_btns[p] = btn

            # PDF send button
            with ui.row().classes("items-center").style("gap:10px"):
                pdf_status = ui.label().classes("text-muted").style("font-size:12px")

                async def _send_analytics_pdf():
                    pdf_status.set_text("Generating PDF…")
                    ok, msg = await _generate_and_send_analytics_pdf(selected_period["value"])
                    pdf_status.set_text("" if ok else msg)
                    if ok:
                        _safe_notify("Analytics report sent to Telegram ✓", type="positive")
                    else:
                        _safe_notify(msg or "Send failed", type="negative")

                ui.button(
                    "📤 Send Analytics PDF",
                    on_click=_send_analytics_pdf,
                ).props("flat unelevated").style(
                    "background:rgba(255,255,255,.06);color:var(--text-muted);"
                    "border:1px solid var(--border);border-radius:5px;"
                    "font-size:14px;padding:4px 10px"
                )

        # Charts host — re-rendered on period change
        charts_area = ui.element("div").style("width:100%;display:flex;flex-direction:column;gap:16px")

    # Initial load
    await _load_and_render(selected_period["value"], charts_area)





# ─────────────────────────────────────────────────────────────────────────────
# Reports Center detailed preview helpers
# ─────────────────────────────────────────────────────────────────────────────

def _reports_escape(value: object) -> str:
    from html import escape
    return escape(str(value if value is not None else ""))


def _reports_clean_date(value: object) -> str | None:
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


def _reports_range(report_range: object | None) -> tuple[str, str | None, str | None, bool]:
    mode = "Count Limit"
    from_date = None
    to_date = None
    if isinstance(report_range, dict):
        mode = str(report_range.get("mode") or "Count Limit")
        from_date = _reports_clean_date(report_range.get("from_date"))
        to_date = _reports_clean_date(report_range.get("to_date"))
    elif report_range:
        mode = str(report_range)
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


def _reports_apply_date_filter(stmt, column, from_date: str | None, to_date: str | None):
    if from_date:
        stmt = stmt.where(func.date(column) >= from_date)
    if to_date:
        stmt = stmt.where(func.date(column) <= to_date)
    return stmt


async def _build_main_report_detailed_preview_html(trade_limit: str | int | None, report_range: object | None) -> str:
    selected_limit = _normalize_report_trade_limit(trade_limit if trade_limit is not None else _report_trade_limit)
    limit_value = None if str(selected_limit).lower() == "all" else int(selected_limit)
    period_label, from_date, to_date, use_count_limit = _reports_range(report_range)
    async with AsyncSessionLocal() as s:
        open_stmt = select(Trade).where(Trade.status.in_([TradeStatus.PENDING.value, TradeStatus.TRIGGERED.value])).order_by(desc(Trade.created_at))
        closed_stmt = select(Trade).where(Trade.status.in_([TradeStatus.CLOSED_TP.value, TradeStatus.CLOSED_SL.value])).order_by(desc(Trade.closed_at), desc(Trade.created_at))
        cancelled_stmt = select(Trade).where(Trade.status.in_([TradeStatus.CANCELLED.value, TradeStatus.EXPIRED.value])).order_by(desc(Trade.closed_at), desc(Trade.created_at))
        if use_count_limit:
            if limit_value is not None:
                open_stmt = open_stmt.limit(limit_value)
                closed_stmt = closed_stmt.limit(limit_value)
                cancelled_stmt = cancelled_stmt.limit(limit_value)
            shown_label = "All Time" if limit_value is None else f"Last {limit_value} trades"
        else:
            open_stmt = _reports_apply_date_filter(open_stmt, Trade.created_at, from_date, to_date)
            closed_stmt = _reports_apply_date_filter(closed_stmt, Trade.closed_at, from_date, to_date)
            cancelled_stmt = _reports_apply_date_filter(cancelled_stmt, Trade.closed_at, from_date, to_date)
            shown_label = period_label
        open_rows = list((await s.execute(open_stmt)).scalars().all())
        closed_rows = list((await s.execute(closed_stmt)).scalars().all())
        cancelled_rows = list((await s.execute(cancelled_stmt)).scalars().all())
    wins = [t for t in closed_rows if float(t.pnl_usd or 0) > 0]
    losses = [t for t in closed_rows if float(t.pnl_usd or 0) <= 0]
    total_pnl = sum(float(t.pnl_usd or 0) for t in closed_rows)
    avg_r = (sum(float(t.pnl_r or 0) for t in closed_rows) / len(closed_rows)) if closed_rows else 0.0
    win_rate = (len(wins) / len(closed_rows) * 100.0) if closed_rows else 0.0
    pnl_cls = "green" if total_pnl >= 0 else "red"
    wr_cls = "green" if win_rate >= 50 else "yellow"
    kpis = f"""
    <div class=\"kpi-row\" style=\"margin-top:12px\">
      <div class=\"kpi\"><div class=\"kpi-label\">Period</div><div class=\"kpi-val cyan\" style=\"font-size:18px\">{_reports_escape(shown_label)}</div><div class=\"kpi-change\">Main trades report</div></div>
      <div class=\"kpi\"><div class=\"kpi-label\">Open</div><div class=\"kpi-val cyan\">{len(open_rows)}</div><div class=\"kpi-change\">Pending / triggered</div></div>
      <div class=\"kpi\"><div class=\"kpi-label\">Closed</div><div class=\"kpi-val {pnl_cls}\">{len(closed_rows)}</div><div class=\"kpi-change\">{len(wins)}W / {len(losses)}L</div></div>
      <div class=\"kpi\"><div class=\"kpi-label\">Win Rate</div><div class=\"kpi-val {wr_cls}\">{win_rate:.1f}%</div><div class=\"kpi-change\">Preview only</div></div>
      <div class=\"kpi\"><div class=\"kpi-label\">P/L</div><div class=\"kpi-val {pnl_cls}\">${total_pnl:+,.2f}</div><div class=\"kpi-change\">{avg_r:+.2f}R avg</div></div>
      <div class=\"kpi\"><div class=\"kpi-label\">Cancelled</div><div class=\"kpi-val yellow\">{len(cancelled_rows)}</div><div class=\"kpi-change\">Cancelled / expired</div></div>
    </div>"""
    closed_body = ""
    for t in closed_rows[:80]:
        pnl = float(t.pnl_usd or 0)
        cls = "text-success" if pnl >= 0 else "text-danger"
        closed_body += f"""
        <tr><td class=\"sym-cell\">{_reports_escape(t.symbol)}</td><td>{direction_pill(_dir_text(t.direction))}</td><td>{_status_pill(t)}</td><td class=\"mono\">{fmt_price(_trade_entry_price(t))}</td><td class=\"mono\">{fmt_price(t.exit_price or 0)}</td><td class=\"mono tabular-nums {cls}\">${pnl:+,.2f}</td><td class=\"mono tabular-nums {cls}\">{float(t.pnl_r or 0):+.2f}R</td><td class=\"mono text-muted\">{_saudi_time(t.closed_at) if t.closed_at else '—'}</td></tr>"""
    if not closed_body:
        closed_body = '<tr><td colspan="8" class="text-muted">No closed trades in this report range.</td></tr>'
    cancelled_body = ""
    for t in cancelled_rows[:80]:
        reason = _reports_escape(str(t.notes or "—"))
        if len(reason) > 220:
            reason = reason[:217] + "..."
        cancelled_body += f"""
        <tr><td class=\"sym-cell\">{_reports_escape(t.symbol)}</td><td>{direction_pill(_dir_text(t.direction))}</td><td><span class=\"pill pill-info-soft\">{_reports_escape(_status_text(str(t.status)))}</span></td><td style=\"white-space:normal;color:var(--text-muted);font-size:12px\">{reason}</td><td class=\"mono text-muted\">{_saudi_time(t.closed_at) if t.closed_at else '—'}</td></tr>"""
    if not cancelled_body:
        cancelled_body = '<tr><td colspan="5" class="text-muted">No cancelled/expired trades in this report range.</td></tr>'
    closed_table = f"""
    <div class=\"card\" style=\"margin-top:16px\"><div class=\"card-title\">Closed Trades Preview <span class=\"pill pill-muted\" style=\"margin-left:6px\">{len(closed_rows)}</span></div><div class=\"table-wrap\"><table class=\"lh-table\"><thead><tr><th>Symbol</th><th>Dir</th><th>Status</th><th>Entry</th><th>Exit</th><th>P/L USD</th><th>R</th><th>Closed</th></tr></thead><tbody>{closed_body}</tbody></table></div></div>"""
    cancelled_table = f"""
    <div class=\"card\" style=\"margin-top:16px\"><div class=\"card-title\">Cancelled / Expired Preview <span class=\"pill pill-info-soft\" style=\"margin-left:6px\">{len(cancelled_rows)}</span></div><div class=\"table-wrap\"><table class=\"lh-table\"><thead><tr><th>Symbol</th><th>Dir</th><th>Status</th><th>Reason</th><th>Closed</th></tr></thead><tbody>{cancelled_body}</tbody></table></div></div>"""
    return kpis + closed_table + cancelled_table


async def _build_analytics_report_detailed_preview_html(days_label: str = "30d") -> str:
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
    reason_counts = {}
    for r in rejected_all:
        reason = str(r.rejection_reason or "Unknown").strip()
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    reason_rows = sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)[:20]
    sym_counts = {}
    for r in rejected_all:
        sym = str(r.symbol or "Unknown").replace("USDT", "")
        sym_counts[sym] = sym_counts.get(sym, 0) + 1
    sym_rows = sorted(sym_counts.items(), key=lambda x: x[1], reverse=True)[:20]
    def _table(title, rows):
        body = "".join(f'<tr><td>{_reports_escape(k)}</td><td class="mono tabular-nums" style="text-align:right">{v}</td></tr>' for k, v in rows)
        if not body:
            body = '<tr><td colspan="2" class="text-muted">No data for this period.</td></tr>'
        return f'<div class="card" style="min-width:320px;flex:1"><div class="card-title">{_reports_escape(title)}</div><div class="table-wrap"><table class="lh-table"><thead><tr><th>Name</th><th style="text-align:right">Count</th></tr></thead><tbody>{body}</tbody></table></div></div>'
    kpis = f"""
    <div class=\"kpi-row\" style=\"margin-top:12px\"><div class=\"kpi\"><div class=\"kpi-label\">Period</div><div class=\"kpi-val cyan\">{_reports_escape(period)}</div><div class=\"kpi-change\">Analytics preview</div></div><div class=\"kpi\"><div class=\"kpi-label\">Rejected Signals</div><div class=\"kpi-val red\">{len(rejected_all)}</div><div class=\"kpi-change\">Total rejections</div></div><div class=\"kpi\"><div class=\"kpi-label\">Cancelled</div><div class=\"kpi-val yellow\">{total_cancelled}</div><div class=\"kpi-change\">Cancelled trades</div></div><div class=\"kpi\"><div class=\"kpi-label\">Expired</div><div class=\"kpi-val yellow\">{total_expired}</div><div class=\"kpi-change\">Expired trades</div></div><div class=\"kpi\"><div class=\"kpi-label\">Total</div><div class=\"kpi-val cyan\">{total_cancelled + total_expired}</div><div class=\"kpi-change\">Canc + expired</div></div></div>"""
    return kpis + f'<div style="display:flex;gap:16px;flex-wrap:wrap;width:100%;margin-top:16px">{_table("Top Rejection Reasons", reason_rows)}{_table("Rejections by Symbol", sym_rows)}</div>'





async def _build_management_report_preview_html(limit_value: str | int | None = "50") -> str:
    # Detailed management audit preview for Reports Center. UI-only.
    selected = _normalize_report_trade_limit(limit_value if limit_value is not None else "50")
    limit_n = None if str(selected).lower() == "all" else int(selected)
    try:
        audit = await ManagementAuditAnalyzer().analyze(limit_n)
    except TypeError:
        audit = await ManagementAuditAnalyzer().analyze(limit=limit_n)

    # Recent closed trade sample used for Top Winners/Losers and leakage by exit reason.
    async with AsyncSessionLocal() as s:
        trade_stmt = (
            select(Trade)
            .where(Trade.status.in_([TradeStatus.CLOSED_TP.value, TradeStatus.CLOSED_SL.value]))
            .order_by(desc(Trade.closed_at), desc(Trade.created_at))
        )
        if limit_n is not None:
            trade_stmt = trade_stmt.limit(limit_n)
        recent_closed = list((await s.execute(trade_stmt)).scalars().all())

    summary = audit.get("summary", {}) or {}
    trades = int(summary.get("trades", 0) or 0)
    raw_trades = int(summary.get("raw_trades", trades) or trades)
    excluded_unknown = int(summary.get("excluded_unknown", 0) or 0)
    avg_max_r = float(summary.get("avg_max_r", 0) or 0)
    avg_final_r = float(summary.get("avg_final_r", 0) or 0)
    avg_capture = summary.get("avg_capture")
    avg_leakage = summary.get("avg_leakage")

    capture_pct = (float(avg_capture) * 100.0) if avg_capture is not None else None
    leakage_pct = (float(avg_leakage) * 100.0) if avg_leakage is not None else None
    capture_cls = "green" if (capture_pct or 0) >= 45 else "yellow" if (capture_pct or 0) >= 25 else "red"
    leakage_cls = "green" if (leakage_pct or 0) <= 35 else "yellow" if (leakage_pct or 0) <= 60 else "red"

    trailing = audit.get("trailing_audit", {}) or {}
    be = audit.get("be_audit", {}) or {}
    trail_loss = trailing.get("avg_lost_after_trailing")
    be_loss = be.get("avg_lost_after_be")
    trail_loss_val = float(trail_loss or 0)
    be_loss_val = float(be_loss or 0)

    def pct_text(v) -> str:
        return f"{float(v) * 100.0:.1f}%" if v is not None else "n/a"

    def r_text(v) -> str:
        return f"{float(v or 0):+.2f}R"

    def _exit_reason(t: Trade) -> str:
        raw = str(getattr(t, "exit_reason", None) or "").strip()
        if raw:
            return raw
        return _status_text(str(getattr(t, "status", "")), float(getattr(t, "pnl_usd", 0) or 0))

    def _trade_metric(t: Trade) -> dict:
        max_r = float(getattr(t, "max_r_reached", 0.0) or 0.0)
        final_r = float(getattr(t, "pnl_r", 0.0) or 0.0)
        lost_r = max(0.0, max_r - final_r)
        if max_r > 0:
            capture = max(0.0, min(final_r / max_r, 1.0))
            leakage = max(0.0, min(lost_r / max_r, 1.0))
        else:
            capture = None
            leakage = None
        return {"max_r": max_r, "final_r": final_r, "lost_r": lost_r, "capture": capture, "leakage": leakage, "exit": _exit_reason(t)}

    trade_metrics = [(t, _trade_metric(t)) for t in recent_closed]
    top_winners = sorted(trade_metrics, key=lambda x: x[1]["final_r"], reverse=True)[:10]
    top_losers = sorted(trade_metrics, key=lambda x: x[1]["final_r"])[:10]

    exit_groups: dict[str, dict] = {}
    for t, m in trade_metrics:
        reason = m["exit"] or "Unknown"
        g = exit_groups.setdefault(reason, {"count": 0, "max_r": 0.0, "final_r": 0.0, "lost_r": 0.0, "leakages": [], "captures": []})
        g["count"] += 1
        g["max_r"] += m["max_r"]
        g["final_r"] += m["final_r"]
        g["lost_r"] += m["lost_r"]
        if m["leakage"] is not None:
            g["leakages"].append(m["leakage"])
        if m["capture"] is not None:
            g["captures"].append(m["capture"])

    limit_label = "All managed outcomes" if limit_n is None else f"Last {limit_n} managed outcomes"
    kpis = f'''
    <div class="kpi-row" style="margin-top:12px">
      <div class="kpi"><div class="kpi-label">Period</div><div class="kpi-val cyan" style="font-size:18px">{_reports_escape(limit_label)}</div><div class="kpi-change">Management audit</div></div>
      <div class="kpi"><div class="kpi-label">Trades</div><div class="kpi-val cyan">{trades}</div><div class="kpi-change">raw {raw_trades} / excluded {excluded_unknown}</div></div>
      <div class="kpi"><div class="kpi-label">Capture</div><div class="kpi-val {capture_cls}">{pct_text(avg_capture)}</div><div class="kpi-change">target &gt; 70%</div></div>
      <div class="kpi"><div class="kpi-label">Leakage</div><div class="kpi-val {leakage_cls}">{pct_text(avg_leakage)}</div><div class="kpi-change">lower is better</div></div>
      <div class="kpi"><div class="kpi-label">Avg MaxR</div><div class="kpi-val cyan">{avg_max_r:+.2f}R</div><div class="kpi-change">available move</div></div>
      <div class="kpi"><div class="kpi-label">Avg FinalR</div><div class="kpi-val {'green' if avg_final_r >= 0 else 'red'}">{avg_final_r:+.2f}R</div><div class="kpi-change">captured result</div></div>
    </div>'''

    health = f'''
    <div class="card" style="margin-top:16px">
      <div class="card-title">Management Health</div>
      <div class="kpi-row" style="margin-top:10px">
        <div class="kpi"><div class="kpi-label">Trailing Trades</div><div class="kpi-val cyan">{int(trailing.get('count', 0) or 0)}</div><div class="kpi-change">avg activation {r_text(trailing.get('avg_activation_r'))}</div></div>
        <div class="kpi"><div class="kpi-label">Trail Loss</div><div class="kpi-val {'red' if trail_loss_val > 0.75 else 'yellow' if trail_loss_val > 0.35 else 'green'}">{r_text(trail_loss_val)}</div><div class="kpi-change">after trailing activation</div></div>
        <div class="kpi"><div class="kpi-label">BE Trades</div><div class="kpi-val cyan">{int(be.get('count', 0) or 0)}</div><div class="kpi-change">avg activation {r_text(be.get('avg_activation_r'))}</div></div>
        <div class="kpi"><div class="kpi-label">BE Loss</div><div class="kpi-val {'red' if be_loss_val > 0.75 else 'yellow' if be_loss_val > 0.35 else 'green'}">{r_text(be_loss_val)}</div><div class="kpi-change">after BE activation</div></div>
      </div>
    </div>'''

    def simple_table(title: str, headers: list[str], rows_html: str, empty_cols: int) -> str:
        if not rows_html:
            rows_html = f'<tr><td colspan="{empty_cols}" class="text-muted">No data available.</td></tr>'
        head = ''.join(f'<th>{_reports_escape(h)}</th>' for h in headers)
        return f'<div class="card" style="margin-top:16px;flex:1;min-width:360px"><div class="card-title">{_reports_escape(title)}</div><div class="table-wrap"><table class="lh-table"><thead><tr>{head}</tr></thead><tbody>{rows_html}</tbody></table></div></div>'

    def pct_metric(v) -> str:
        return pct_text(v) if v is not None else "n/a"

    def trade_row(t: Trade, m: dict, include_lost: bool = True) -> str:
        pnl_cls = "green" if m["final_r"] >= 0 else "red"
        lost_cell = f'<td class="mono red">{r_text(m["lost_r"])}</td>' if include_lost else ""
        return f'''
        <tr>
          <td class="sym-cell">{_reports_escape(getattr(t, 'symbol', '—'))}</td>
          <td>{direction_pill(_dir_text(getattr(t, 'direction', '—')))}</td>
          <td class="mono">{r_text(m['max_r'])}</td>
          <td class="mono {pnl_cls}">{r_text(m['final_r'])}</td>
          {lost_cell}
          <td class="mono">{pct_metric(m['capture'])}</td>
          <td class="mono">{pct_metric(m['leakage'])}</td>
          <td>{_reports_escape(m['exit'])}</td>
        </tr>'''

    lost_rows = ""
    for t in (audit.get("lost_winners", []) or [])[:10]:
        cap = t.get("capture")
        leak = t.get("leakage")
        lost_rows += f'''
        <tr>
          <td class="sym-cell">{_reports_escape(t.get('symbol', '—'))}</td>
          <td>{direction_pill(str(t.get('direction', '—')))}</td>
          <td class="mono">{r_text(t.get('max_r'))}</td>
          <td class="mono {'green' if float(t.get('final_r') or 0) >= 0 else 'red'}">{r_text(t.get('final_r'))}</td>
          <td class="mono red">{r_text(t.get('lost_r'))}</td>
          <td class="mono">{pct_text(cap) if cap is not None else 'n/a'}</td>
          <td class="mono">{pct_text(leak) if leak is not None else 'n/a'}</td>
          <td>{_reports_escape(t.get('exit_reason', '—'))}</td>
        </tr>'''

    winner_rows = ''.join(trade_row(t, m, include_lost=False) for t, m in top_winners)
    loser_rows = ''.join(trade_row(t, m, include_lost=False) for t, m in top_losers)

    exit_leak_rows = ""
    for reason, g in sorted(exit_groups.items(), key=lambda item: (sum(item[1]["leakages"]) / len(item[1]["leakages"])) if item[1]["leakages"] else -1, reverse=True)[:12]:
        avg_leak = (sum(g["leakages"]) / len(g["leakages"])) if g["leakages"] else None
        avg_cap = (sum(g["captures"]) / len(g["captures"])) if g["captures"] else None
        avg_max = g["max_r"] / g["count"] if g["count"] else 0.0
        avg_final = g["final_r"] / g["count"] if g["count"] else 0.0
        avg_lost = g["lost_r"] / g["count"] if g["count"] else 0.0
        exit_leak_rows += f'''
        <tr>
          <td>{_reports_escape(reason)}</td>
          <td class="mono">{int(g['count'])}</td>
          <td class="mono">{r_text(avg_max)}</td>
          <td class="mono {'green' if avg_final >= 0 else 'red'}">{r_text(avg_final)}</td>
          <td class="mono red">{r_text(avg_lost)}</td>
          <td class="mono">{pct_metric(avg_cap)}</td>
          <td class="mono">{pct_metric(avg_leak)}</td>
        </tr>'''

    profile_rows = ""
    for r in (audit.get("profiles", []) or [])[:8]:
        cap = r.get("avg_capture")
        leak = r.get("avg_leakage")
        profile_rows += f'''
        <tr>
          <td class="sym-cell">{_reports_escape(r.get('name', '—'))}</td>
          <td class="mono">{int(r.get('trades', 0) or 0)}</td>
          <td class="mono">{float(r.get('win_rate', 0) or 0)*100:.1f}%</td>
          <td class="mono">{r_text(r.get('avg_max_r'))}</td>
          <td class="mono {'green' if float(r.get('avg_final_r', 0) or 0) >= 0 else 'red'}">{r_text(r.get('avg_final_r'))}</td>
          <td class="mono">{pct_text(cap) if cap is not None else 'n/a'}</td>
          <td class="mono">{pct_text(leak) if leak is not None else 'n/a'}</td>
        </tr>'''

    exit_rows = ""
    for r in (audit.get("exits", []) or [])[:10]:
        exit_rows += f'<tr><td>{_reports_escape(r.get("reason", "—"))}</td><td class="mono" style="text-align:right">{int(r.get("count", 0) or 0)}</td></tr>'

    lost_table = simple_table("Top 10 Leaked Trades", ["Symbol", "Dir", "MaxR", "FinalR", "LostR", "Capture", "Leakage", "Exit"], lost_rows, 8)
    exit_leak_table = simple_table("Leakage By Exit Reason", ["Exit", "Trades", "Avg MaxR", "Avg FinalR", "Avg LostR", "Capture", "Leakage"], exit_leak_rows, 7)
    winner_table = simple_table("Top Winners", ["Symbol", "Dir", "MaxR", "FinalR", "Capture", "Leakage", "Exit"], winner_rows, 7)
    loser_table = simple_table("Top Losers", ["Symbol", "Dir", "MaxR", "FinalR", "Capture", "Leakage", "Exit"], loser_rows, 7)
    profile_table = simple_table("Profile Performance", ["Profile", "Trades", "Win%", "Avg MaxR", "Avg FinalR", "Capture", "Leakage"], profile_rows, 7)
    exit_table = simple_table("Exit Reasons", ["Reason", "Count"], exit_rows, 2)

    return (
        kpis + health
        + f'<div style="display:flex;gap:16px;flex-wrap:wrap;width:100%;margin-top:0">{lost_table}{exit_leak_table}</div>'
        + f'<div style="display:flex;gap:16px;flex-wrap:wrap;width:100%;margin-top:0">{winner_table}{loser_table}</div>'
        + f'<div style="display:flex;gap:16px;flex-wrap:wrap;width:100%;margin-top:0">{profile_table}{exit_table}</div>'
    )


# ─────────────────────────────────────────────────────────────────────────────
# /reports
# ─────────────────────────────────────────────────────────────────────────────

@ui.page("/reports")
async def page_reports() -> None:
    global _report_trade_limit, _rejected_report_limit
    container = _page_shell("Reports", "REPORTS CENTER • PREVIEW • TELEGRAM")
    _auto_refresh_on_scan()

    report_range_options = [
        "Count Limit",
        "Today",
        "Yesterday",
        "Last 7 Days",
        "Last 30 Days",
        "Since Risk Fix",
        "Custom Date Range",
        "All Time",
    ]

    with container:
        ui.html("""
        <div class="card" style="margin-bottom:16px">
          <div class="card-title">Reports Center</div>
          <div class="text-muted" style="font-size:13px;line-height:1.7">
            مركز التقارير الجديد. التقرير الرئيسي يدعم التاريخ والتقويم ويرسل إلى Telegram.
            تقرير المرفوضة وتحليلاتها تعمل بشكل مستقل عن فلتر التقرير الرئيسي.
          </div>
        </div>
        """)

        preview_area = None

        with ui.element("div").classes("report-grid").style("display:grid;grid-template-columns:repeat(4,minmax(250px,1fr));gap:16px;width:100%;align-items:stretch"):
            with ui.element("div").classes("report-action-card").style("border:1px solid rgba(255,255,255,.10);background:rgba(15,23,42,.72);border-radius:16px;padding:16px;min-height:245px"):
                ui.html('<div class="card-title">Main Trades Report</div>')
                ui.label("Open + Closed + Cancelled/Expired only").classes("text-muted")

                ui.label("Report Range").classes("detail-label").style("margin-top:12px")
                range_select = ui.select(options=report_range_options, value=(_reports_active_preview.get("range") or {}).get("mode", "Since Risk Fix")).props("outlined dense").style("width:230px")

                ui.label("Count Limit").classes("detail-label").style("margin-top:10px")
                limit_select = ui.select(
                    options=["20", "30", "50", "100", "All"],
                    value=_normalize_report_trade_limit(_reports_active_preview.get("limit") or _report_trade_limit),
                ).props("outlined dense").style("width:160px")
                limit_select.on(
                    "update:model-value",
                    lambda e: globals().__setitem__("_report_trade_limit", _normalize_report_trade_limit(getattr(e, "value", None))),
                )

                with ui.row().classes("items-center").style("gap:10px;margin-top:10px;flex-wrap:wrap"):
                    from_input = ui.input(label="From Date", value=(_reports_active_preview.get("range") or {}).get("from_date") or None, placeholder="YYYY-MM-DD").props("outlined dense type=date").style("width:170px")
                    to_input = ui.input(label="To Date", value=(_reports_active_preview.get("range") or {}).get("to_date") or None, placeholder="YYYY-MM-DD").props("outlined dense type=date").style("width:170px")

                ui.label("Custom dates use date only, no hour.").classes("text-muted").style("font-size:12px;margin-top:6px")
                status = ui.label().classes("text-muted").style("font-size:12px;margin-top:10px")

                def _range_payload() -> dict:
                    return {"mode": range_select.value, "from_date": from_input.value, "to_date": to_input.value}

                async def _preview_main() -> None:
                    _reports_active_preview.update({"kind": "main", "limit": limit_select.value, "range": _range_payload()})
                    html = await _build_main_report_detailed_preview_html(limit_select.value, _range_payload())
                    preview_area.set_content(html)
                    preview_area.update()
                    _safe_notify("Main report preview updated", type="positive")

                async def _send_main() -> None:
                    await _send_report_clicked(status, limit_select.value, _range_payload())

                async def _save_main() -> None:
                    status.set_text("Saving PDF...")
                    ok, msg, path = await _generate_main_pdf_file(limit_select.value, _range_payload())
                    status.set_text("" if ok else msg)
                    if ok:
                        _safe_notify(msg, type="positive")
                    else:
                        _safe_notify(msg or "Save failed", type="negative")

                with ui.row().classes("items-center").style("gap:8px;margin-top:10px;flex-wrap:wrap"):
                    ui.button("Preview", on_click=_preview_main).props("flat").style("background:rgba(255,255,255,.06);color:var(--text);border:1px solid var(--border);border-radius:8px")
                    ui.button("📤 Send Telegram", on_click=_send_main).style("background:var(--accent);color:#000;font-weight:900;border:none;border-radius:8px")
                    ui.button("Save PDF", on_click=_save_main).props("flat").style("background:rgba(255,255,255,.04);color:var(--text-muted);border:1px solid var(--border);border-radius:8px")

            with ui.element("div").classes("report-action-card").style("border:1px solid rgba(255,255,255,.10);background:rgba(15,23,42,.72);border-radius:16px;padding:16px;min-height:245px"):
                ui.html('<div class="card-title">Rejected Signals Report</div>')
                ui.label("Separate report. Not affected by main report date filters.").classes("text-muted")

                ui.label("Rows").classes("detail-label").style("margin-top:12px")
                rej_limit_select = ui.select(options=["20", "50", "100", "All"], value=_normalize_report_trade_limit(_rejected_report_limit)).props("outlined dense").style("width:160px")
                rej_status = ui.label().classes("text-muted").style("font-size:12px;margin-top:10px")

                async def _preview_rejected() -> None:
                    _reports_active_preview.update({"kind": "rejected", "rejected_limit": rej_limit_select.value})
                    html = await _build_rejected_report_preview_html(rej_limit_select.value)
                    preview_area.set_content(html)
                    preview_area.update()
                    _safe_notify("Rejected preview updated", type="positive")

                with ui.row().classes("items-center").style("gap:8px;margin-top:10px;flex-wrap:wrap"):
                    ui.button("📤 Send Telegram", on_click=lambda: _send_rejected_report_clicked(rej_status, rej_limit_select.value)).style("background:rgba(83,167,255,.95);color:#000;font-weight:900;border:none;border-radius:8px")
                    ui.button("Preview", on_click=_preview_rejected).props("flat").style("background:rgba(255,255,255,.04);color:var(--text-muted);border:1px solid var(--border);border-radius:8px")

            with ui.element("div").classes("report-action-card").style("border:1px solid rgba(255,255,255,.10);background:rgba(15,23,42,.72);border-radius:16px;padding:16px;min-height:245px"):
                ui.html('<div class="card-title">Analytics Report</div>')
                ui.label("Rejected + Cancelled/Expired analytics.").classes("text-muted")

                ui.label("Period").classes("detail-label").style("margin-top:12px")
                analytics_period = ui.select(options=["7d", "30d", "90d", "All"], value=_reports_active_preview.get("analytics_period", "30d")).props("outlined dense").style("width:160px")
                analytics_status = ui.label().classes("text-muted").style("font-size:12px;margin-top:10px")

                async def _send_analytics_from_reports() -> None:
                    analytics_status.set_text("Generating Analytics PDF...")
                    ok, msg = await _generate_and_send_analytics_pdf(analytics_period.value)
                    analytics_status.set_text("" if ok else msg)
                    if ok:
                        _safe_notify("Analytics report sent to Telegram", type="positive")
                    else:
                        _safe_notify(msg or "Send failed", type="negative")

                async def _preview_analytics() -> None:
                    _reports_active_preview.update({"kind": "analytics", "analytics_period": analytics_period.value})
                    html = await _build_analytics_report_detailed_preview_html(analytics_period.value)
                    preview_area.set_content(html)
                    preview_area.update()
                    _safe_notify("Analytics preview updated", type="positive")

                with ui.row().classes("items-center").style("gap:8px;margin-top:10px;flex-wrap:wrap"):
                    ui.button("📤 Send Telegram", on_click=_send_analytics_from_reports).style("background:rgba(246,183,60,.95);color:#000;font-weight:900;border:none;border-radius:8px")
                    ui.button("Preview", on_click=_preview_analytics).props("flat").style("background:rgba(255,255,255,.04);color:var(--text-muted);border:1px solid var(--border);border-radius:8px")

            with ui.element("div").classes("report-action-card").style("border:1px solid rgba(255,255,255,.10);background:rgba(15,23,42,.72);border-radius:16px;padding:16px;min-height:245px"):
                ui.html('<div class="card-title">Trade Management Report</div>')
                ui.label("Profit capture, leakage, BE and trailing audit.").classes("text-muted")

                ui.label("Audit Sample").classes("detail-label").style("margin-top:12px")
                management_limit = ui.select(options=["20", "30", "50", "100", "All"], value=_reports_active_preview.get("management_limit", "50")).props("outlined dense").style("width:160px")
                management_status = ui.label().classes("text-muted").style("font-size:12px;margin-top:10px")

                async def _preview_management() -> None:
                    management_status.set_text("Loading management audit...")
                    _reports_active_preview.update({"kind": "management", "management_limit": management_limit.value})
                    html = await _build_management_report_preview_html(management_limit.value)
                    preview_area.set_content(html)
                    preview_area.update()
                    management_status.set_text("")
                    _safe_notify("Management preview updated", type="positive")

                with ui.row().classes("items-center").style("gap:8px;margin-top:10px;flex-wrap:wrap"):
                    ui.button("Preview", on_click=_preview_management).props("flat").style("background:rgba(255,255,255,.06);color:var(--text);border:1px solid var(--border);border-radius:8px")

        preview_area = ui.html("").classes("reports-preview-area").style("width:100%;display:block;margin-top:16px")
        active_kind = str(_reports_active_preview.get("kind") or "main")
        if active_kind == "management":
            html = await _build_management_report_preview_html(_reports_active_preview.get("management_limit", "50"))
        elif active_kind == "analytics":
            html = await _build_analytics_report_detailed_preview_html(_reports_active_preview.get("analytics_period", "30d"))
        elif active_kind == "rejected":
            html = await _build_rejected_report_preview_html(_reports_active_preview.get("rejected_limit") or _rejected_report_limit)
        else:
            html = await _build_main_report_detailed_preview_html(
                _reports_active_preview.get("limit") or _report_trade_limit,
                _reports_active_preview.get("range") or {"mode": "Since Risk Fix"},
            )
        preview_area.set_content(html)


# ─────────────────────────────────────────────────────────────────────────────
# /settings
# ─────────────────────────────────────────────────────────────────────────────

@ui.page("/settings")
async def page_settings() -> None:
    global _report_trade_limit
    container = _page_shell("Settings", "CONFIGURATION OVERVIEW")
    from src.core.config import settings as cfg

    def cfg_table(title: str, rows: list[tuple[str, str]]) -> None:
        html = f'<div class="card-title">{title}</div><table class="lh-table"><tbody>'
        for k, v in rows:
            html += f'<tr><td style="color:var(--text-muted);padding:10px 14px;font-size:14px">{k}</td><td class="mono" style="padding:10px 14px;font-size:14px">{v}</td></tr>'
        html += "</tbody></table>"
        with ui.element("div").classes("card"):
            ui.html(html)

    pe     = _cfg_section("paper_executor")
    de     = _cfg_section("decision_engine")
    sc     = _cfg_section("scanner")
    bt     = _cfg_section("backtest")
    ui_cfg = _cfg_section("ui")
    tr     = _cfg_section("trigger_confirmation")
    tg_cfg = _cfg_section("alerts")

    with container:
        with ui.element("div").style(
            "display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:16px;width:100%"
        ):
            cfg_table("Environment", [
                ("Mode",            str(cfg.env.env)),
                ("Database URL",    str(cfg.env.database_url)),
                ("Telegram",        "Configured" if cfg.env.telegram_bot_token else "Not set"),
                ("Telegram Alerts", "On" if tg_cfg.get("telegram_enabled") else "Off"),
                ("UI Host",         str(ui_cfg.get("host", "0.0.0.0"))),
                ("UI Port",         str(ui_cfg.get("port", 8082))),
            ])
            cfg_table("Scanner", [
                ("Scan Interval",   f'{sc.get("scan_interval_seconds", 40)} s'),
                ("Top N Monitor",   str(sc.get("top_n_to_monitor"))),
                ("Min Quote Vol 24h", fmt_money_short(sc.get("min_quote_volume_24h_usd", 0))),
                ("Min Open Interest", fmt_money_short(sc.get("min_open_interest_usd", 0))),
                ("Funding Extreme",   f'{sc.get("funding_extreme_threshold", 0)*100:.3f}%'),
                ("OI Change 4h Thr.", f'{sc.get("oi_change_4h_threshold", 0)*100:.0f}%'),
            ])
            cfg_table("Execution Risk", [
                ("Initial Capital",       f'${pe.get("initial_capital_usd", 0):,.0f}'),
                ("Risk Per Trade",        f'{pe.get("risk_per_trade_pct", 0)*100:.1f}%'),
                ("Max Concurrent Trades", str(pe.get("max_concurrent_trades"))),
                ("Daily Max Loss",        f'{pe.get("daily_max_loss_pct", 0)*100:.1f}%'),
                ("Max Consecutive Losses",str(pe.get("daily_max_consecutive_losses"))),
                ("Slippage Entry",        f'{pe.get("slippage_entry_pct", 0)*100:.3f}%'),
                ("Spread",                f'{pe.get("spread_pct", 0)*100:.3f}%'),
            ])
            cfg_table("Decision Engine", [
                ("Min Score to Signal",   str(de.get("min_score_to_signal"))),
                ("Min Score Full Size",   str(de.get("min_score_full_size"))),
                ("Trend Reversal Penalty",f'{de.get("trending_market_penalty_on_reversal", 0)*100:.0f}%'),
                ("Range Reversal Bonus",  f'{de.get("range_market_bonus_on_reversal", 0)*100:.0f}%'),
            ])
            cfg_table("Trigger Confirmation", [
                ("Required Confirmations", str(tr.get("required_confirmations"))),
                ("Volume Spike Multiplier",f'{tr.get("volume_spike_multiplier", 0):.1f}x'),
                ("OI Reaction Threshold",  f'{tr.get("oi_reaction_threshold", 0)*100:.2f}%'),
                ("Rejection Wick Ratio",   f'{tr.get("rejection_wick_ratio", 0)*100:.0f}%'),
            ])
            cfg_table("Backtest", [
                ("Default Lookback Days", str(bt.get("default_lookback_days"))),
                ("Warmup Candles",        str(bt.get("warmup_candles"))),
                ("Fee taker",             f'{bt.get("fee_pct", 0)*100:.3f}%'),
            ])

        with ui.element("div").classes("card").style("margin-top:16px;max-width:560px"):
            ui.label("Telegram Report").classes("card-title")
            ui.label("Generate a full PDF report and send it to Telegram.").classes("text-muted")
            ui.label("Report Trades").classes("detail-label")
            trade_limit_select = (
                ui.select(options=["20", "30", "50", "100", "All"],
                          value=_normalize_report_trade_limit(_report_trade_limit))
                .props("outlined dense")
            )
            trade_limit_select.style("width:180px;margin-top:6px")
            trade_limit_select.on(
                "update:model-value",
                lambda e: globals().__setitem__(
                    "_report_trade_limit",
                    _normalize_report_trade_limit(getattr(e, "value", None)),
                ),
            )
            report_status = ui.label().classes("text-muted").style("margin-top:8px")
            ui.button(
                "Send Report to Telegram",
                on_click=lambda: _send_report_clicked(report_status, trade_limit_select.value),
            ).style(
                "background:var(--accent);color:#000;font-weight:800;border:none;"
                "border-radius:8px;padding:10px 14px;margin-top:10px"
            )


# ─────────────────────────────────────────────────────────────────────────────
# /symbol/{symbol}
# ─────────────────────────────────────────────────────────────────────────────

@ui.page("/symbol/{symbol}")
async def page_symbol_details(symbol: str) -> None:
    symbol    = symbol.upper()
    container = _page_shell("Trades", f"{symbol} • FULL TRADE LOG")

    async with AsyncSessionLocal() as s:
        rows = await s.execute(
            select(Trade)
            .where(Trade.symbol == symbol)
            .order_by(desc(Trade.created_at), desc(Trade.closed_at))
        )
        trades = rows.scalars().all()

        await s.execute(
            select(
                func.count(Trade.id),
                func.sum(case((Trade.status == TradeStatus.TRIGGERED.value, 1), else_=0)),
            ).where(Trade.symbol == symbol)
        )

    with container:
        with ui.row().classes("items-center justify-between").style("margin-bottom:4px"):
            ui.html(f'<div class="card-title" style="margin:0;font-size:16px;color:var(--text)">{symbol} Trade Log</div>')
            ui.html('<a href="/trades" class="details-link">Back to Trades</a>')

        if not trades:
            with ui.element("div").classes("card"):
                empty_state(f"No trades recorded yet for {symbol}.")
            return

        total      = len(trades)
        open_n     = sum(1 for t in trades if _status_text(str(t.status)) in {"PENDING", "TRIGGERED"})
        cancelled_n= sum(1 for t in trades if _status_text(str(t.status)) in {"CANCELLED", "EXPIRED"})
        tp_n       = sum(1 for t in trades if _status_text(str(t.status)) == "TP")
        sl_n       = sum(1 for t in trades if _status_text(str(t.status), t.pnl_usd or 0.0).startswith("SL"))
        net_pnl    = sum(t.pnl_usd or 0 for t in trades)

        ui.html(f"""
        <div class="kpi-row symbol-kpis">
          <div class="kpi"><div class="kpi-label">Trades</div><div class="kpi-val cyan">{total}</div><div class="kpi-change">All records</div></div>
          <div class="kpi"><div class="kpi-label">Open</div><div class="kpi-val yellow">{open_n}</div><div class="kpi-change">Pending / Triggered</div></div>
          <div class="kpi"><div class="kpi-label">Cancelled</div><div class="kpi-val red">{cancelled_n}</div><div class="kpi-change">Cancelled / Expired</div></div>
          <div class="kpi"><div class="kpi-label">Take Profit</div><div class="kpi-val green">{tp_n}</div><div class="kpi-change">Closed in profit</div></div>
          <div class="kpi"><div class="kpi-label">Stop Loss Net</div><div class="kpi-val {'green' if net_pnl >= 0 else 'red'}">{sl_n}</div><div class="kpi-change {'up' if net_pnl >= 0 else 'down'}">${net_pnl:,.2f}</div></div>
        </div>
        """)

        with ui.element("div").classes("card"):
            ui.html('<div class="card-title">Full Symbol Timeline</div>')
            for t in trades:
                pnl     = t.pnl_usd or 0.0
                pnl_cls = _trade_outcome_class(t)
                summary = f"""
                <div class="trade-summary-row">
                  <div class="trade-summary-main">
                    <span class="trade-symbol">{t.id}</span>
                    {direction_pill(_dir_text(t.direction))}
                    {_status_pill(t)}
                    <span class="trade-time">{_event_time_label(t)}</span>
                  </div>
                  <div class="trade-summary-side">
                    <span class="mono">Entry {fmt_price(_trade_entry_price(t))}</span>
                    <span class="mono">Exit {fmt_price(t.exit_price or 0)}</span>
                    <span class="mono {pnl_cls}">${pnl:,.2f}</span>
                    <span class="mono {pnl_cls}">{(t.pnl_r or 0):.2f}R</span>
                  </div>
                </div>
                """
                journey_html = _trade_journey_html(t)
                ui.html(
                    f'<details class="trade-disclosure"><summary>{summary}</summary>{journey_html}{_trade_details_html(t)}</details>'
                )