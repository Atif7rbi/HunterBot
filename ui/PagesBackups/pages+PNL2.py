from __future__ import annotations
from datetime import datetime, timedelta, timezone
from nicegui import ui
from sqlalchemy import case, desc, func, select
from pathlib import Path
from fastapi.responses import HTMLResponse
from src.core.database import AsyncSessionLocal, RejectedSignal, Trade, TradeStatus

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


# ─────────────────────────────────────────────────────────────────────────────
# Report helpers (Trades + Rejected) — unchanged from original
# ─────────────────────────────────────────────────────────────────────────────

async def _send_report_clicked(status_label, selected_limit=None) -> None:
    status_label.set_text("Generating and sending report...")
    ok, msg = await _generate_and_send_pdf_report(
        _normalize_report_trade_limit(selected_limit)
        if selected_limit is not None
        else _report_trade_limit
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
    container = _page_shell("Overview", "PORTFOLIO • RECENT ACTIVITY")
    _auto_refresh_on_scan()

    with container:
        equity     = await executor.get_equity()
        open_count = await executor.get_open_count()
        kill       = await executor.is_kill_switch_active()

        async with AsyncSessionLocal() as s:
            res = await s.execute(
                select(Trade).where(
                    Trade.status.in_(
                        [TradeStatus.CLOSED_TP.value, TradeStatus.CLOSED_SL.value]
                    )
                )
            )
            closed = res.scalars().all()

        wins     = [t for t in closed if (t.pnl_usd or 0) > 0]
        win_rate = len(wins) / len(closed) if closed else 0.0
        total_pnl = sum(t.pnl_usd or 0 for t in closed)
        initial  = executor.initial_capital
        eq_pct   = ((equity - initial) / initial * 100) if initial else 0.0

        eq_cls   = "green" if eq_pct   >= 0 else "red"
        pnl_cls  = "green" if total_pnl >= 0 else "red"
        wr_cls   = "green" if win_rate  >= 0.5 else "yellow"
        kill_cls = "red"   if kill else "green"

        ui.html(
            f"""
            <div class="kpi-row" style="display:none">
              <div class="kpi"><div class="kpi-label">Equity</div><div class="kpi-val {eq_cls}">${equity:,.0f}</div><div class="kpi-change {'up' if eq_pct >= 0 else 'down'}">{abs(eq_pct):.2f}%</div></div>
              <div class="kpi"><div class="kpi-label">Realized P/L</div><div class="kpi-val {pnl_cls}">${total_pnl:,.2f}</div><div class="kpi-change {'up' if total_pnl >= 0 else 'down'}">{len(closed)} trades</div></div>
              <div class="kpi"><div class="kpi-label">Win Rate</div><div class="kpi-val {wr_cls}">{win_rate*100:.1f}%</div><div class="kpi-change">{len(wins)}/{len(closed)} wins</div></div>
              <div class="kpi"><div class="kpi-label">Open Positions</div><div class="kpi-val cyan">{open_count}</div><div class="kpi-change">ACTIVE</div></div>
              <div class="kpi"><div class="kpi-label">Kill Switch</div><div class="kpi-val {kill_cls}">{'ARMED' if kill else 'SAFE'}</div><div class="kpi-change {'down' if kill else 'up'}">{'Daily limit hit' if kill else 'Trading OK'}</div></div>
            </div>
            """
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
                        ]
                        summary_table_html = "".join(
                            f"""
                            <tr>
                              <td style="color:var(--text-muted)">{_esc(k)}</td>
                              <td class="mono" style="text-align:right">{_esc(str(v))}</td>
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


# ─────────────────────────────────────────────────────────────────────────────
# /trades
# ─────────────────────────────────────────────────────────────────────────────

@ui.page("/trades")
async def page_trades() -> None:
    global _rejected_report_limit
    container = _page_shell("Trades", "OPEN • CLOSED POSITIONS")

    async def _load_open_trades():
        async with AsyncSessionLocal() as s:
            open_res = await s.execute(
                select(Trade)
                .where(Trade.status.in_([TradeStatus.PENDING.value, TradeStatus.TRIGGERED.value]))
                .order_by(desc(Trade.created_at))
            )
            return open_res.scalars().all()

    def _open_positions_table(open_trades) -> str:
        tbl = """<div class="table-wrap"><table class="lh-table"><thead><tr>
        <th>Symbol</th>
        <th>Dir</th>
        <th>Status</th>
        <th>Entry</th><th>Price</th><th>P/L $</th><th>P/L %</th><th>SL</th><th>TP1</th><th>TP2</th><th>Size $</th><th>Opened</th><th>Details</th>
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
  <td class="sym-cell">{t.symbol.replace('USDT', '')}</td>
  <td>{direction_pill(direction_text)}</td>
  <td>{_status_pill(t)}</td>
  <td class="mono">{fmt_price(entry_price)}</td>
  <td class="mono" style="color:#5ab3ff;font-weight:700">{fmt_price(current_price)}</td>
  <td class="mono tabular-nums">{pnl_usd_html}</td>
  <td class="mono tabular-nums">{pnl_pct_html}</td>

  <td class="mono text-danger">
      {fmt_price(t.stop_loss or 0)}
  </td>

  <td class="mono"
      style="color:#f6c453;font-weight:700">
      {fmt_price(t.take_profit_1 or 0)}
  </td>

  <td class="mono"
      style="color:#a335c4;font-weight:700">
      {fmt_price(t.take_profit_2 or 0)}
  </td>

  <td class="mono tabular-nums">
      {fmt_money_short(t.position_size_usd or 0)}
  </td>

  <td class="mono text-muted" >
      {t.created_at.strftime("%m/%d %H:%M") if t.created_at else "—"}
  </td>

  <td>{_details_link(t.symbol)}</td>
</tr>"""

        tbl += "</tbody></table></div>"
        return tbl

    async def _render_open_positions(open_host) -> None:
        open_host.clear()
        open_trades = await _load_open_trades()

        with open_host:
            with ui.element("div").classes("card"):
                ui.html(
                    f'<div class="card-title">Open Positions '
                    f'<span class="pill pill-info-soft" style="margin-left:6px">{len(open_trades)}</span>'
                    #f'<span class="text-muted" style="font-size:12px;margin-left:8px">auto refresh: P/L table only</span>'
                    f'</div>'
                )
                if not open_trades:
                    empty_state("No open positions.")
                else:
                    ui.html(_open_positions_table(open_trades))

    with container:
        open_host = ui.element("div")
        await _render_open_positions(open_host)
        ui.timer(30.0, lambda: _render_open_positions(open_host))

        async with AsyncSessionLocal() as s:
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
                    closed_at = t.closed_at.strftime("%m/%d %H:%M") if t.closed_at else "—"
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
                    closed_at = t.closed_at.strftime("%m/%d %H:%M") if t.closed_at else "—"
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
                    created_at    = r.created_at.strftime("%m/%d %H:%M") if r.created_at else "—"
                    category_text = _rejection_category_text(r.category)
                    tbl += f"""<tr>
                      <td class="sym-cell">{r.symbol}</td>
                      <td>{direction_pill(str(r.direction or ""))}</td>
                      <td><span class="pill pill-info-soft" style="font-size:12px">{category_text}</span></td>
                      <td><span class="pill pill-info-soft" style="font-size:12px">{str(r.rejection_reason or "")}</span></td>
                      <td class="mono tabular-nums">{float(r.setup_score or 0):.1f}</td>
                      <td style="font-size:12px;color:var(--text-muted);white-space:normal">{str(r.market_state or "")}</td>
                      <td style="font-size:12px;color:var(--text-muted);white-space:normal">{str(r.market_regime or "")}</td>
                      <td style="color:var(--text-muted);white-space:normal;font-size:12px">{str(r.rejection_details or "-")}</td>
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
# /backtest
# ─────────────────────────────────────────────────────────────────────────────

@ui.page("/backtest")
async def page_backtest() -> None:
    container = _page_shell("Backtest", "HISTORICAL SIMULATION")

    with container:
        ui.html("""
        <div class="kpi-row">
          <div class="kpi"><div class="kpi-label">Mode</div><div class="kpi-val cyan">CLI BACKTEST</div><div class="kpi-change">Runs via terminal</div></div>
          <div class="kpi"><div class="kpi-label">Default Symbol</div><div class="kpi-val">BTCUSDT</div><div class="kpi-change">Edit below</div></div>
          <div class="kpi"><div class="kpi-label">Default Range</div><div class="kpi-val yellow">60 Days</div><div class="kpi-change">Historical candles</div></div>
          <div class="kpi"><div class="kpi-label">Fee</div><div class="kpi-val">0.04%</div><div class="kpi-change">Taker per config</div></div>
          <div class="kpi"><div class="kpi-label">Warmup</div><div class="kpi-val">100</div><div class="kpi-change">Candles</div></div>
        </div>
        """)

        with ui.element("div").classes("card"):
            ui.html('<div class="card-title">Backtest Launcher</div>')
            cmd_box = ui.html("")

            with ui.row().classes("items-end gap-4").style("flex-wrap:wrap;margin-bottom:16px"):
                symbol_inp = (
                    ui.input("Symbol", value="BTCUSDT", placeholder="e.g. ETHUSDT")
                    .props("outlined dense").style("min-width:180px")
                )
                days_inp = (
                    ui.number("Days", value=60, min=7, max=365, step=1, format="%.0f")
                    .props("outlined dense").style("min-width:120px")
                )
                tf_sel = (
                    ui.select(options=["15m", "1h", "4h"], value="1h", label="Timeframe")
                    .props("outlined dense").style("min-width:130px")
                )

            def refresh_cmd() -> None:
                sym  = (symbol_inp.value or "BTCUSDT").strip().upper()
                days = int(days_inp.value or 60)
                tf   = tf_sel.value or "1h"
                cmd_box.set_content(f"""
                    <div style="background:rgba(0,0,0,.35);border:1px solid var(--border);border-radius:10px;padding:18px 22px;margin-top:8px">
                      <div style="font-size:13px;color:var(--text-muted);margin-bottom:10px;letter-spacing:.04em;text-transform:uppercase">Command — copy & run in terminal</div>
                      <code style="font-family:var(--font-mono);font-size:15px;line-height:2;color:#7dd3fc;display:block;word-break:break-all">python -m src.main backtest --symbol {sym} --days {days} --timeframe {tf}</code>
                      <div style="margin-top:14px;padding:14px 18px;border-radius:8px;background:#111827; backdrop-filter:blur(14px);;border:1px solid var(--border);font-size:14px;color:var(--text-muted);line-height:2">
                        <span style="color:var(--accent);font-weight:700">Output</span><br>
                        <code style="font-family:var(--font-mono)">data/backtest_results.json</code><br>
                        <code style="font-family:var(--font-mono)">--days 180</code> لعينة أوسع
                      </div>
                    </div>
                """)

            symbol_inp.on("update:model-value", lambda e: refresh_cmd())
            days_inp.on("update:model-value", lambda e: refresh_cmd())
            tf_sel.on("update:model-value", lambda e: refresh_cmd())
            refresh_cmd()


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
                ui.html(
                    f'<details class="trade-disclosure"><summary>{summary}</summary>{_trade_details_html(t)}</details>'
                )
