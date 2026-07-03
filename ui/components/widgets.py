"""Reusable UI widgets — v1.3.

New in v1.3:
  - scan_progress_bar(): animated 5-step progress bar for scan cycles
  - All tables now wrapped with .table-wrap for horizontal scroll
  - Table headers use explicit col-* classes for min-width enforcement
  - score_bar_cell(): combined score number + bar in one TD
  - empty_state(): consistent empty placeholder with icon + message
"""
from __future__ import annotations

from nicegui import ui


# ---------------------------------------------------------------------------
# KPI
# ---------------------------------------------------------------------------

def kpi_card(label: str, value: str, delta: str = "", delta_class: str = "flat") -> None:
    with ui.element("div").classes("kpi"):
        ui.html(f"<div class='kpi-label'>{label}</div>")
        ui.html(f"<div class='kpi-value'>{value}</div>")
        if delta:
            ui.html(f"<div class='kpi-delta {delta_class}'>{delta}</div>")


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------

def score_bar(score: float) -> str:
    """Standalone score bar HTML (for embedding inside table cells)."""
    pct = max(0.0, min(score, 100.0))
    cls = "score-low" if score < 60 else ("score-mid" if score < 75 else "score-high")
    return (
        f"<div class='score-bar'>"
        f"<div class='score-bar-fill {cls}' style='width:{pct}%'></div>"
        f"</div>"
    )


def score_bar_cell(score: float) -> str:
    """Score number + bar in a single <td>-ready HTML block."""
    return (
        f"<div style='display:flex;align-items:center;gap:10px;min-width:130px;'>"
        f"<span style='min-width:36px;text-align:right;'>{score:.0f}</span>"
        f"{score_bar(score)}"
        f"</div>"
    )


# ---------------------------------------------------------------------------
# Pills
# ---------------------------------------------------------------------------

def direction_pill(direction: str) -> str:
    cls_map = {"LONG": "pill-long", "SHORT": "pill-short", "WAIT": "pill-wait"}
    return f"<span class='pill {cls_map.get(direction, 'pill-muted')}'>{direction}</span>"


def state_pill(state: str) -> str:
    short = state.replace("_", " ").title()
    return f"<span class='pill pill-muted' title='{state}'>{short}</span>"


def regime_pill(regime: str) -> str:
    cls = "pill-info"
    if "RANGING" in regime:
        cls = "pill-muted"
    elif "VOLATILE" in regime:
        cls = "pill-wait"
    label = regime.replace("_", " ").title()
    return f"<span class='pill {cls}'>{label}</span>"


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------

def fmt_price(p: float) -> str:
    if p >= 1000: return f"{p:,.2f}"
    if p >= 10:   return f"{p:,.3f}"
    if p >= 1:    return f"{p:.4f}"
    return f"{p:.6f}"


def fmt_money_short(v: float) -> str:
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.1f}K"
    return f"${v:.0f}"


def fmt_pct(v: float, plus_sign: bool = True) -> str:
    sign = "+" if v >= 0 and plus_sign else ""
    return f"{sign}{v*100:.2f}%"


# ---------------------------------------------------------------------------
# Section header
# ---------------------------------------------------------------------------

def section_header(title: str, action_html: str = "") -> None:
    with ui.element("div").classes("card-header"):
        ui.html(f"<div class='card-title'>{title}</div>")
        if action_html:
            ui.html(action_html)


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------

def empty_state(icon: str = "📭", message: str = "No data yet.", sub: str = "") -> None:
    html = (
        f"<div style='padding:32px 0;text-align:center;'>"
        f"<div style='font-size:28px;margin-bottom:10px;'>{icon}</div>"
        f"<div style='color:var(--text-muted);font-size:13px;'>{message}</div>"
    )
    if sub:
        html += f"<div style='color:var(--text-faint);font-size:11px;margin-top:4px;'>{sub}</div>"
    html += "</div>"
    ui.html(html)


# ---------------------------------------------------------------------------
# SCAN PROGRESS BAR  (new in v1.3)
# ---------------------------------------------------------------------------

_SCAN_STEPS = [
    ("Fetch", "Fetching tickers"),
    ("Quality", "Symbol filter"),
    ("Volume", "Volume check"),
    ("OI", "OI & L/S enrich"),
    ("Extremity", "Signal check"),
]


def scan_progress_bar(
    current_step: int,        # 0-5  (0=idle, 5=done)
    counts: dict | None = None,  # optional: {"total":528,"excluded":89,"vol":30,...}
) -> None:
    """
    Renders an animated 5-step scan progress bar.

    Usage:
        scan_progress_bar(current_step=3, counts={"total": 528, "vol": 30})

    Steps:  0=idle, 1=Fetch, 2=Quality, 3=Volume, 4=OI+LS, 5=Done
    """
    counts = counts or {}
    pct = int((current_step / len(_SCAN_STEPS)) * 100)
    is_running = 0 < current_step < len(_SCAN_STEPS)

    # Title
    title_spin = "<span class='spin'></span>" if is_running else ""
    status_label = (
        "Idle — waiting for next scan"
        if current_step == 0
        else ("✅ Scan complete" if current_step >= len(_SCAN_STEPS)
              else f"Step {current_step}/{len(_SCAN_STEPS)}: {_SCAN_STEPS[current_step-1][1]}")
    )

    # Build step dots HTML
    steps_html = "<div class='scan-steps'>"
    for i, (label, _) in enumerate(_SCAN_STEPS, start=1):
        if i < current_step:
            dot_cls = "done"; lbl_cls = "done"
        elif i == current_step:
            dot_cls = "active"; lbl_cls = "active"
        else:
            dot_cls = ""; lbl_cls = ""
        steps_html += (
            f"<div class='scan-step'>"
            f"<div class='scan-step-dot {dot_cls}'></div>"
            f"<span class='scan-step-label {lbl_cls}'>{label}</span>"
            f"</div>"
        )
    steps_html += "</div>"

    # Counter line
    parts = []
    if counts.get("total"):     parts.append(f"Total: <b>{counts['total']}</b>")
    if counts.get("excluded"):  parts.append(f"Excluded: <b>{counts['excluded']}</b>")
    if counts.get("vol"):       parts.append(f"Vol✓: <b>{counts['vol']}</b>")
    if counts.get("oi"):        parts.append(f"OI✓: <b>{counts['oi']}</b>")
    if counts.get("final"):     parts.append(f"Final: <b style='color:var(--accent)'>{counts['final']}</b>")
    counter_html = (
        f"<div class='scan-counter'>{' · '.join(parts)}</div>" if parts else ""
    )

    html = f"""
<div class='scan-progress-wrap'>
  <div class='scan-progress-title'>
    {title_spin}
    <span>{status_label}</span>
  </div>
  <div class='scan-bar-track'>
    <div class='scan-bar-fill' style='width:{pct}%'></div>
  </div>
  {steps_html}
  {counter_html}
</div>
"""
    ui.html(html)
