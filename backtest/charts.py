from __future__ import annotations

from html import escape as esc
from typing import Any, Sequence


def render_equity_curve_placeholder(equity_curve: Sequence[dict[str, Any]] | None) -> str:
    """Render a lightweight placeholder until real chart rendering is added.

    This is intentionally HTML-only and side-effect free. It does not require
    any chart library and does not affect live trading.
    """
    if not equity_curve:
        return """
        <div class="card" style="margin-top:16px">
          <div class="card-title">Equity Curve</div>
          <div style="color:var(--text-muted);font-size:14px;padding:24px 0;text-align:center">
            No equity curve data available yet.
          </div>
        </div>
        """

    # Minimal text summary until chart implementation.
    start = equity_curve[0]
    end = equity_curve[-1]
    start_equity = start.get("equity", start.get("equity_usd", "—"))
    end_equity = end.get("equity", end.get("equity_usd", "—"))

    return f"""
    <div class="card" style="margin-top:16px">
      <div class="card-title">Equity Curve</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px">
        <div style="border:1px solid var(--border);border-radius:10px;padding:14px;background:rgba(255,255,255,.03)">
          <div style="font-size:12px;color:var(--text-muted);text-transform:uppercase">Points</div>
          <div class="mono" style="font-size:22px;font-weight:800;color:var(--text)">{len(equity_curve)}</div>
        </div>
        <div style="border:1px solid var(--border);border-radius:10px;padding:14px;background:rgba(255,255,255,.03)">
          <div style="font-size:12px;color:var(--text-muted);text-transform:uppercase">Start Equity</div>
          <div class="mono" style="font-size:22px;font-weight:800;color:var(--text)">{esc(str(start_equity))}</div>
        </div>
        <div style="border:1px solid var(--border);border-radius:10px;padding:14px;background:rgba(255,255,255,.03)">
          <div style="font-size:12px;color:var(--text-muted);text-transform:uppercase">End Equity</div>
          <div class="mono" style="font-size:22px;font-weight:800;color:var(--text)">{esc(str(end_equity))}</div>
        </div>
      </div>
      <div style="color:var(--text-muted);font-size:13px;margin-top:14px">
        Chart rendering will be connected after engine.py emits stable equity curve format.
      </div>
    </div>
    """


def render_drawdown_placeholder() -> str:
    return """
    <div class="card" style="margin-top:16px">
      <div class="card-title">Drawdown</div>
      <div style="color:var(--text-muted);font-size:14px;padding:24px 0;text-align:center">
        Drawdown chart will be enabled after equity curve support.
      </div>
    </div>
    """
