from __future__ import annotations

from collections import Counter
from datetime import datetime
from html import escape as esc
from typing import Any

from nicegui import ui
from sqlalchemy import desc, select

from src.core.config import settings
from src.core.database import AsyncSessionLocal, Trade, TradeStatus, ShadowTrade
from src.learning.performance_analyzer import ManagementAuditAnalyzer
from ui.components.widgets import fmt_money_short, fmt_price
from ui.mission_control.market_heatmap import render_market_heatmap

from pathlib import Path


def _load_css() -> str:
    try:
        return Path(__file__).with_name("styles.css").read_text(encoding="utf-8")
    except Exception:
        return ""


def _raw(value: Any) -> str:
    raw = getattr(value, "value", value)
    raw = str(raw or "")
    if "." in raw:
        raw = raw.rsplit(".", 1)[-1]
    return raw


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _decision_direction(d: dict) -> str:
    return str(d.get("direction") or "WAIT")


def _distance_quality(decision: dict) -> str:
    for line in decision.get("reasoning") or []:
        s = str(line)
        if s.startswith("Liquidity Distance:"):
            parts = s.split(":", 1)[1].strip().split()
            return parts[0] if parts else "UNKNOWN"
    return "UNKNOWN"


def _quality_counts(decisions: list[dict]) -> dict[str, int]:
    counts = {"HIGH": 0, "NORMAL": 0, "LOW": 0, "POOR": 0, "UNKNOWN": 0}
    for d in decisions:
        q = _distance_quality(d)
        counts[q if q in counts else "UNKNOWN"] += 1
    return counts


def _trade_entry_price(t: Trade) -> float:
    if getattr(t, "actual_entry_price", None) is not None:
        return float(t.actual_entry_price or 0.0)
    return float(((t.entry_zone_low or 0.0) + (t.entry_zone_high or 0.0)) / 2)


def _live_pnl(t: Trade, live_prices: Any) -> tuple[float | None, float | None]:
    entry = _trade_entry_price(t)
    current = None
    try:
        current = live_prices.get_price(t.symbol)
    except Exception:
        current = None

    if not current or not entry:
        return None, None

    direction = _raw(t.direction)
    if direction == "SHORT":
        pnl_pct = ((entry - float(current)) / entry) * 100.0
    else:
        pnl_pct = ((float(current) - entry) / entry) * 100.0

    pnl_usd = float(t.position_size_usd or 0.0) * (pnl_pct / 100.0)
    return pnl_usd, pnl_pct


def _pill(label: str, cls: str = "") -> str:
    return f'<span class="mc-pill {cls}">{esc(label)}</span>'


async def render_mission_control(
    *,
    container,
    bot: Any,
    executor: Any,
    live_prices: Any,
    details_link=None,
) -> None:
    """Render Hunter Mission Control.

    Display-only dashboard. No strategy, scoring, execution, or config mutation.
    """
    ui.add_head_html(
        f"""
        <style>
        {_load_css()}
        body:has(.mc-wrap) .kpi-row {{
          display: none !important;
        }}
        </style>
        """
    )

    equity = await executor.get_equity() if hasattr(executor, "get_equity") else 0.0
    open_count = await executor.get_open_count() if hasattr(executor, "get_open_count") else 0
    kill = await executor.is_kill_switch_active() if hasattr(executor, "is_kill_switch_active") else False
    initial = float(getattr(executor, "initial_capital", 0) or 0.0)
    eq_pct = ((equity - initial) / initial * 100.0) if initial else 0.0

    async with AsyncSessionLocal() as s:
        open_res = await s.execute(
            select(Trade)
            .where(Trade.status.in_([TradeStatus.PENDING.value, TradeStatus.TRIGGERED.value]))
            .order_by(desc(Trade.created_at))
            .limit(8)
        )
        open_trades = list(open_res.scalars().all())

        closed_res = await s.execute(
            select(Trade)
            .where(Trade.status.in_([TradeStatus.CLOSED_TP.value, TradeStatus.CLOSED_SL.value]))
            .order_by(desc(Trade.closed_at), desc(Trade.created_at))
            .limit(100)
        )
        closed = list(closed_res.scalars().all())

        try:
            shadow_res = await s.execute(
                select(ShadowTrade)
                .order_by(desc(ShadowTrade.created_at))
                .limit(500)
            )
            shadow_rows = list(shadow_res.scalars().all())
        except Exception:
            shadow_rows = []

    wins = [t for t in closed if (t.pnl_usd or 0) > 0]
    total_pnl = sum(float(t.pnl_usd or 0.0) for t in closed)
    win_rate = (len(wins) / len(closed) * 100.0) if closed else 0.0
    avg_r = (sum(float(t.pnl_r or 0.0) for t in closed) / len(closed)) if closed else 0.0
    avg_max_r = (sum(float(getattr(t, "max_r_reached", 0.0) or 0.0) for t in closed) / len(closed)) if closed else 0.0
    capture_values = [
        float(getattr(t, "profit_capture_ratio", 0.0) or 0.0)
        for t in closed
        if getattr(t, "profit_capture_ratio", None) is not None
    ]
    avg_capture = (sum(capture_values) / len(capture_values) * 100.0) if capture_values else 0.0

    winner_max_r = (
        sum(float(getattr(t, "max_r_reached", 0.0) or 0.0) for t in wins) / len(wins)
    ) if wins else 0.0
    winner_final_r = (
        sum(float(getattr(t, "pnl_r", 0.0) or 0.0) for t in wins) / len(wins)
    ) if wins else 0.0
    winner_capture_values = [
        float(getattr(t, "profit_capture_ratio", 0.0) or 0.0)
        for t in wins
        if getattr(t, "profit_capture_ratio", None) is not None
    ]
    winner_capture = (
        sum(winner_capture_values) / len(winner_capture_values) * 100.0
    ) if winner_capture_values else 0.0

    shadow_total = len(shadow_rows)
    shadow_active = sum(1 for r in shadow_rows if str(getattr(r, "status", "") or "").upper() == "ACTIVE")
    shadow_finalized = [r for r in shadow_rows if str(getattr(r, "status", "") or "").upper() == "FINALIZED"]
    shadow_evaluated = shadow_finalized or shadow_rows
    shadow_avg_max_r = (
        sum(float(getattr(r, "max_r_reached", 0.0) or 0.0) for r in shadow_evaluated) / len(shadow_evaluated)
    ) if shadow_evaluated else 0.0
    shadow_avg_final_r = (
        sum(float(getattr(r, "final_r", 0.0) or 0.0) for r in shadow_finalized if getattr(r, "final_r", None) is not None)
        / len([r for r in shadow_finalized if getattr(r, "final_r", None) is not None])
    ) if [r for r in shadow_finalized if getattr(r, "final_r", None) is not None] else 0.0
    shadow_tp1_hits = sum(1 for r in shadow_evaluated if bool(getattr(r, "would_hit_tp1", False)) or float(getattr(r, "max_r_reached", 0.0) or 0.0) >= 1.0)
    shadow_hit_rate = (shadow_tp1_hits / len(shadow_evaluated) * 100.0) if shadow_evaluated else 0.0
    shadow_reason_counts = Counter(str(getattr(r, "rejection_reason", "UNKNOWN") or "UNKNOWN") for r in shadow_rows)
    shadow_top_reason = shadow_reason_counts.most_common(1)[0][0] if shadow_reason_counts else "—"
    shadow_quality_counts = Counter(str(getattr(r, "trade_quality_class", "—") or "—") for r in shadow_rows)
    shadow_quality_mix = " / ".join(f"{k}:{v}" for k, v in shadow_quality_counts.most_common(3)) if shadow_quality_counts else "—"

    decisions = list((getattr(bot, "last_decisions", {}) or {}).values())
    scans = list(getattr(bot, "last_scan_results", []) or [])

    diag = getattr(getattr(bot, "scanner", None), "last_diagnostics", None)
    scanned = getattr(diag, "total_symbols", 0) if diag else (len(scans) or 0)
    final = getattr(diag, "final_shortlist", 0) if diag else len(scans)

    exec_count = sum(1 for d in decisions if _decision_direction(d) in {"LONG", "SHORT"} and float(d.get("score", 0) or 0) >= float(settings.decision_engine.get("min_score_to_signal", 55)))
    watch_count = sum(1 for d in decisions if _decision_direction(d) in {"LONG", "SHORT"} and float(d.get("score", 0) or 0) < float(settings.decision_engine.get("min_score_to_signal", 55)))
    wait_count = max(0, len(decisions) - exec_count - watch_count)

    avg_funding = (sum(float(r.get("funding_rate", 0) or 0) for r in scans) / len(scans) * 100.0) if scans else 0.0
    avg_oi = (sum(float(r.get("oi_change_4h_pct", 0) or 0) for r in scans) / len(scans) * 100.0) if scans else 0.0
    avg_ls = (sum(float(r.get("long_short_ratio", 0) or 0) for r in scans) / len(scans)) if scans else 0.0

    regime_counts = Counter(str(d.get("regime") or "UNKNOWN") for d in decisions)
    top_regime = regime_counts.most_common(1)[0][0] if regime_counts else "UNKNOWN"

    short_bias = sum(1 for d in decisions if _decision_direction(d) == "SHORT")
    long_bias = sum(1 for d in decisions if _decision_direction(d) == "LONG")
    top_bias = "SHORT" if short_bias > long_bias else "LONG" if long_bias > short_bias else "NEUTRAL"

    crowding = "LONGS DOMINANT" if avg_ls >= 1.2 else "SHORTS DOMINANT" if avg_ls <= 0.8 and avg_ls > 0 else "BALANCED"

    q_counts = _quality_counts(decisions)
    q_total = max(1, sum(q_counts.values()))
    high_pct = (q_counts["HIGH"] / q_total) * 100
    normal_pct = (q_counts["NORMAL"] / q_total) * 100
    low_pct = (q_counts["LOW"] / q_total) * 100
    poor_pct = (q_counts["POOR"] / q_total) * 100

    conic = (
        f"#22c55e 0 {high_pct:.1f}%, "
        f"#facc15 {high_pct:.1f}% {high_pct + normal_pct:.1f}%, "
        f"#f59e0b {high_pct + normal_pct:.1f}% {high_pct + normal_pct + low_pct:.1f}%, "
        f"#ef4444 {high_pct + normal_pct + low_pct:.1f}% 100%"
    )

    active_rows = ""
    for t in open_trades:
        pnl_usd, pnl_pct = _live_pnl(t, live_prices)
        try:
            current_price = live_prices.get_price(t.symbol)
        except Exception:
            current_price = None

        pnl_cls = "pos" if (pnl_usd or 0) >= 0 else "neg"
        pnl_txt = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "—"
        price_txt = fmt_price(current_price or 0) if current_price else "—"
        r_txt = "—"
        if pnl_usd is not None and getattr(t, "risk_amount_usd", None):
            risk = float(t.risk_amount_usd or 0.0)
            if risk:
                r_txt = f"{(pnl_usd / risk):+.2f}R"

        link = details_link(t.symbol) if details_link else ""
        active_rows += f"""
        <tr>
          <td class="mc-sym">{esc(t.symbol)}</td>
          <td>{_pill(_raw(t.direction), 'short' if _raw(t.direction) == 'SHORT' else 'long')}</td>
          <td class="mono">{fmt_price(_trade_entry_price(t))}</td>
          <td class="mono blue">{price_txt}</td>
          <td class="mono {pnl_cls}">{pnl_txt}</td>
          <td class="mono {pnl_cls}">{r_txt}</td>
          <td>{_pill(_raw(t.status), 'open')}</td>
          <td>{link}</td>
        </tr>
        """

    if not active_rows:
        active_rows = '<tr><td colspan="8" class="mc-muted" style="text-align:center;padding:22px">No open positions.</td></tr>'

    top_signals = sorted(decisions, key=lambda d: float(d.get("score", 0) or 0), reverse=True)[:5]
    events = []
    for d in top_signals:
        direction = _decision_direction(d)
        score = float(d.get("score", 0) or 0)
        if direction in {"LONG", "SHORT"}:
            events.append(f'<div class="mc-event"><span class="dot green"></span><span>{esc(str(d.get("symbol", "—")))} {direction} score {score:.1f}</span></div>')
        else:
            events.append(f'<div class="mc-event"><span class="dot blue"></span><span>{esc(str(d.get("symbol", "—")))} WAIT score {score:.1f}</span></div>')

    events_html = "".join(events) or '<div class="mc-muted">No recent events.</div>'

    live_prices_ok = True
    try:
        live_prices_ok = bool(live_prices.get_all()) or open_count == 0
    except Exception:
        live_prices_ok = False


    # Trade Management Health — dashboard-only KPI.
    # Uses ManagementAuditAnalyzer data already used by PDF reports.
    # Does not affect strategy, execution, scanner, scoring, or database state.
    mgmt_trades = 0
    mgmt_capture = None
    mgmt_leakage = None
    mgmt_trail_loss = None
    mgmt_be_loss = None
    mgmt_target_leakage = 0.70
    mgmt_recovery_needed = None
    mgmt_status = "NO DATA"
    mgmt_status_cls = "blue"
    mgmt_note = "Waiting for audited closed trades"

    try:
        audit = await ManagementAuditAnalyzer().analyze(limit=100)
        summary = audit.get("summary", {}) or {}
        trailing_audit = audit.get("trailing_audit", {}) or {}
        be_audit = audit.get("be_audit", {}) or {}

        mgmt_trades = int(summary.get("trades", 0) or 0)
        mgmt_capture = summary.get("avg_capture")
        mgmt_leakage = summary.get("avg_leakage")
        mgmt_trail_loss = trailing_audit.get("avg_lost_after_trailing")
        mgmt_be_loss = be_audit.get("avg_lost_after_be")

        if mgmt_leakage is not None:
            mgmt_recovery_needed = max(0.0, (float(mgmt_leakage) - mgmt_target_leakage) * 100.0)

        leakage_pct = (float(mgmt_leakage) * 100.0) if mgmt_leakage is not None else None
        if leakage_pct is None or mgmt_trades <= 0:
            mgmt_status = "NO DATA"
            mgmt_status_cls = "blue"
            mgmt_note = "Need closed audited trades"
        elif leakage_pct > 80.0:
            mgmt_status = "CRITICAL"
            mgmt_status_cls = "neg"
            mgmt_note = "Profit leakage is very high"
        elif leakage_pct >= 60.0:
            mgmt_status = "WARNING"
            mgmt_status_cls = "yellow"
            mgmt_note = "Management still leaking profit"
        else:
            mgmt_status = "SAFE"
            mgmt_status_cls = "pos"
            mgmt_note = "Profit capture improving"
    except Exception:
        mgmt_status = "CHECK"
        mgmt_status_cls = "yellow"
        mgmt_note = "Management audit unavailable"

    def _pct_or_dash(value: Any) -> str:
        if value is None:
            return "—"
        try:
            return f"{float(value) * 100.0:.1f}%"
        except Exception:
            return "—"

    def _r_or_dash(value: Any) -> str:
        if value is None:
            return "—"
        try:
            return f"{float(value):+.2f}R"
        except Exception:
            return "—"

    def _plain_pct_or_dash(value: Any) -> str:
        if value is None:
            return "—"
        try:
            return f"{float(value):.1f}%"
        except Exception:
            return "—"

    with container:
        ui.html(f"""
        <div class="mc-wrap">
         
          <div class="mc-kpi-grid">
            <div class="mc-card kpi"><div class="label">BOT STATUS</div><div class="value {'neg' if kill else 'pos'}">{'ARMED' if kill else 'LIVE'}</div><div class="hint">{'Kill switch active' if kill else 'All systems operational'}</div></div>
            <div class="mc-card kpi"><div class="label">SCANNED</div><div class="value blue">{scanned}</div><div class="hint">Markets scanned</div></div>
            <div class="mc-card kpi"><div class="label">FINAL</div><div class="value purple">{final}</div><div class="hint">After scanner filters</div></div>
            <div class="mc-card kpi"><div class="label">SIGNALS</div><div class="value yellow">{exec_count + watch_count}</div><div class="hint">Execute / Watch</div></div>
            <div class="mc-card kpi"><div class="label">OPEN TRADES</div><div class="value cyan">{open_count}</div><div class="hint">Active positions</div></div>
            <div class="mc-card kpi"><div class="label">EQUITY</div><div class="value {'pos' if eq_pct >= 0 else 'neg'}">${equity:,.0f}</div><div class="hint">{eq_pct:+.2f}% from initial</div></div>
          </div>
        """)
        render_market_heatmap(bot=bot)
        ui.html(f"""

          <div class="mc-grid-3">
            <div class="mc-card">
              <div class="mc-section-title">MARKET PULSE</div>
              <div class="mc-list">
                <div><span>Funding Avg</span><strong class="{'pos' if avg_funding >= 0 else 'neg'}">{avg_funding:+.3f}%</strong></div>
                <div><span>Crowding</span><strong class="{'neg' if 'LONGS' in crowding else 'pos' if 'SHORTS' in crowding else 'blue'}">{crowding}</strong></div>
                <div><span>Dominant Regime</span><strong class="yellow">{esc(top_regime)}</strong></div>
                <div><span>Top Bias</span><strong class="{'neg' if top_bias == 'SHORT' else 'pos' if top_bias == 'LONG' else 'blue'}">{top_bias}</strong></div>
                <div><span>OI Change Avg</span><strong class="{'pos' if avg_oi >= 0 else 'neg'}">{avg_oi:+.2f}%</strong></div>
                <div><span>L/S Ratio Avg</span><strong>{avg_ls:.2f}</strong></div>
              </div>
            </div>

            <div class="mc-card">
              <div class="mc-section-title">EXECUTION QUALITY <span>(Distance to Liquidity)</span></div>
              <div class="mc-quality">
                <div class="mc-donut" style="background:conic-gradient({conic})">
                  <div><strong>{sum(q_counts.values())}</strong><span>Total</span></div>
                </div>
                <div class="mc-quality-list">
                  <div><span class="dot green"></span>HIGH ≤0.5% <b>{q_counts['HIGH']}</b></div>
                  <div><span class="dot yellow"></span>NORMAL 0.5–1% <b>{q_counts['NORMAL']}</b></div>
                  <div><span class="dot orange"></span>LOW 1–2% <b>{q_counts['LOW']}</b></div>
                  <div><span class="dot red"></span>POOR >2% <b>{q_counts['POOR']}</b></div>
                </div>
              </div>
            </div>

            <div class="mc-card">
              <div class="mc-section-title">LIVE PERFORMANCE</div>
              <div class="mc-list">
                <div><span>Equity</span><strong class="pos">${equity:,.2f}</strong></div>
                <div><span>Realized P/L</span><strong class="{'pos' if total_pnl >= 0 else 'neg'}">{_money(total_pnl)}</strong></div>
                <div><span>Win Rate</span><strong class="{'pos' if win_rate >= 50 else 'yellow'}">{win_rate:.1f}%</strong></div>
                <div><span>Avg R</span><strong class="{'pos' if avg_r >= 0 else 'neg'}">{avg_r:+.2f}R</strong></div>
                <div><span>Avg Max R</span><strong class="blue">{avg_max_r:+.2f}R</strong></div>
                <div><span>Avg Capture</span><strong class="{'pos' if avg_capture >= 50 else 'yellow' if avg_capture >= 25 else 'neg'}">{avg_capture:.1f}%</strong></div>
                <div><span>Winner Avg MaxR</span><strong class="blue">{winner_max_r:+.2f}R</strong></div>
                <div><span>Winner Avg FinalR</span><strong class="{'pos' if winner_final_r >= 0 else 'neg'}">{winner_final_r:+.2f}R</strong></div>
                <div><span>Winner Capture</span><strong class="{'pos' if winner_capture >= 50 else 'yellow' if winner_capture >= 25 else 'neg'}">{winner_capture:.1f}%</strong></div>
                <div><span>Closed Trades</span><strong>{len(closed)}</strong></div>
              </div>
            </div>
          </div>

          <div class="mc-grid-2">
            <div class="mc-card">
              <div class="mc-section-title">SIGNALS BREAKDOWN</div>
              <div class="mc-signal-boxes">
                <div class="exec"><span>EXECUTE</span><strong>{exec_count}</strong></div>
                <div class="watch"><span>WATCH</span><strong>{watch_count}</strong></div>
                <div class="wait"><span>WAIT</span><strong>{wait_count}</strong></div>
              </div>
              <div class="mc-stack">
                <span class="exec" style="width:{(exec_count/max(1,len(decisions))*100):.1f}%"></span>
                <span class="watch" style="width:{(watch_count/max(1,len(decisions))*100):.1f}%"></span>
                <span class="wait" style="width:{(wait_count/max(1,len(decisions))*100):.1f}%"></span>
              </div>
            </div>

            <div class="mc-card">
              <div class="mc-section-title">ACTIVE TRADES</div>
              <div class="mc-table-wrap">
                <table class="mc-table">
                  <thead><tr><th>Symbol</th><th>Dir</th><th>Entry</th><th>Price</th><th>P/L</th><th>R</th><th>Status</th><th></th></tr></thead>
                  <tbody>{active_rows}</tbody>
                </table>
              </div>
            </div>
          </div>

          <div class="mc-grid-2 bottom">
            <div class="mc-card">
              <div class="mc-section-title">SHADOW INTELLIGENCE <span>Rejected setups learning</span></div>
              <div class="mc-list">
                <div><span>Shadow Cases</span><strong class="blue">{shadow_total}</strong></div>
                <div><span>Active Tracking</span><strong class="cyan">{shadow_active}</strong></div>
                <div><span>Evaluated</span><strong>{len(shadow_finalized)}</strong></div>
                <div><span>Hit Rate ≥1R</span><strong class="{'pos' if shadow_hit_rate >= 50 else 'yellow' if shadow_hit_rate >= 25 else 'neg'}">{shadow_hit_rate:.1f}%</strong></div>
                <div><span>Avg Shadow MaxR</span><strong class="blue">{shadow_avg_max_r:+.2f}R</strong></div>
                <div><span>Avg Shadow FinalR</span><strong class="{'pos' if shadow_avg_final_r >= 0 else 'neg'}">{shadow_avg_final_r:+.2f}R</strong></div>
                <div><span>Top Rejection</span><strong class="yellow">{esc(shadow_top_reason)}</strong></div>
                <div><span>Quality Mix</span><strong>{esc(shadow_quality_mix)}</strong></div>
              </div>
            </div>

            <div class="mc-card">
              <div class="mc-section-title">TRADE MANAGEMENT HEALTH <span>Profit capture monitor</span></div>
              <div class="mc-health-grid">
                <div><span>Capture</span><strong class="{'pos' if (mgmt_capture or 0) >= 0.50 else 'yellow' if (mgmt_capture or 0) >= 0.25 else 'neg'}">{_pct_or_dash(mgmt_capture)}</strong></div>
                <div><span>Leakage</span><strong class="{mgmt_status_cls}">{_pct_or_dash(mgmt_leakage)}</strong></div>
                <div><span>Trail Loss</span><strong class="{'neg' if (mgmt_trail_loss or 0) >= 1.0 else 'yellow' if (mgmt_trail_loss or 0) >= 0.5 else 'pos'}">{_r_or_dash(mgmt_trail_loss)}</strong></div>
                <div><span>BE Loss</span><strong class="{'neg' if (mgmt_be_loss or 0) >= 1.0 else 'yellow' if (mgmt_be_loss or 0) >= 0.5 else 'pos'}">{_r_or_dash(mgmt_be_loss)}</strong></div>
                <div><span>Target</span><strong class="pos">&lt;{mgmt_target_leakage * 100:.0f}%</strong></div>
                <div><span>Recovery</span><strong class="{'pos' if (mgmt_recovery_needed or 0) <= 0 else 'yellow' if (mgmt_recovery_needed or 0) <= 20 else 'neg'}">{_plain_pct_or_dash(mgmt_recovery_needed)}</strong></div>
              </div>
              <div style="margin-top:12px;border-top:1px solid rgba(148,163,184,.12);padding-top:10px;display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap">
                <div><span style="color:var(--text-muted);font-size:12px">Status</span> <strong class="{mgmt_status_cls}" style="font-family:var(--font-mono);font-size:15px">{mgmt_status}</strong></div>
                <div style="color:var(--text-muted);font-size:12px">{mgmt_note} · Trades {mgmt_trades}</div>
                <div style="color:var(--text-muted);font-size:11px">DB OK | Feed {'OK' if live_prices_ok else 'CHECK'} | Kill {'ARMED' if kill else 'SAFE'}</div>
              </div>
            </div>

            <div class="mc-card">
              <div class="mc-section-title">RECENT EVENTS</div>
              <div class="mc-events">{events_html}</div>
            </div>
          </div>
        </div>
        """)
