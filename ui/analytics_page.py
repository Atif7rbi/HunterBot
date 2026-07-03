from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nicegui import ui
from sqlalchemy import desc, select

from src.core.database import AsyncSessionLocal, RejectedSignal, Trade, TradeStatus
from ui.app import _auto_refresh_on_scan, _page_shell, _safe_notify

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


