from __future__ import annotations

from html import escape as esc
from typing import Sequence

from backtest.config import LATEST_TRADES_LIMIT
from backtest.metrics import format_metric
from backtest.models import BacktestRun


def _metric(run: BacktestRun | None, key: str, default=0):
    if run is None:
        return default
    return (run.metrics or {}).get(key, default)


def render_runs_table(runs: Sequence[BacktestRun]) -> str:
    if not runs:
        return """
        <div style="color:var(--text-muted);font-size:14px;padding:28px 0;text-align:center">
          No backtest result files found yet.<br>
          Future results should be saved as JSON inside <code>backtest/results/</code>.
        </div>
        """

    rows = ""
    for run in runs:
        pf = _metric(run, "profit_factor", 0)
        pf_text = "∞" if pf == float("inf") else format_metric(pf)
        rows += f"""
        <tr>
          <td class="mono">{esc(run.run_id)}</td>
          <td class="sym-cell">{esc(run.symbol)}</td>
          <td class="mono">{esc(run.timeframe)}</td>
          <td class="mono tabular-nums">{run.days}</td>
          <td class="mono tabular-nums">{int(_metric(run, "total_trades", len(run.trades)))}</td>
          <td class="mono tabular-nums">{format_metric(_metric(run, "win_rate", 0), "%")}</td>
          <td class="mono tabular-nums">{format_metric(_metric(run, "avg_r", 0))}R</td>
          <td class="mono tabular-nums">{format_metric(_metric(run, "net_r", 0))}R</td>
          <td class="mono tabular-nums">{pf_text}</td>
          <td class="mono text-muted">{esc(str(run.created_at or "—")[:19])}</td>
        </tr>
        """

    return f"""
    <div class="table-wrap">
      <table class="lh-table">
        <thead>
          <tr>
            <th>Run ID</th>
            <th>Symbol</th>
            <th>TF</th>
            <th>Days</th>
            <th>Trades</th>
            <th>Win Rate</th>
            <th>Avg R</th>
            <th>Net R</th>
            <th>PF</th>
            <th>Created</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    """


def render_latest_trade_table(runs: Sequence[BacktestRun]) -> str:
    if not runs:
        return ""

    latest = runs[0]
    trades = latest.trades[-LATEST_TRADES_LIMIT:]
    if not trades:
        return """
        <div class="card" style="margin-top:16px">
          <div class="card-title">Latest Run Trades</div>
          <div style="color:var(--text-muted);font-size:14px;padding:22px 0;text-align:center">
            Latest run has no trade list.
          </div>
        </div>
        """

    rows = ""
    for t in trades:
        pnl_cls = "text-success" if (t.pnl_r or 0) >= 0 else "text-danger"
        rows += f"""
        <tr>
          <td class="sym-cell">{esc(t.symbol)}</td>
          <td>{esc(t.direction)}</td>
          <td class="mono">{t.entry_price:.6g}</td>
          <td class="mono">{t.exit_price:.6g}</td>
          <td class="mono tabular-nums {pnl_cls}">{t.pnl_usd:+.2f}</td>
          <td class="mono tabular-nums {pnl_cls}">{t.pnl_r:+.2f}R</td>
          <td style="color:var(--text-muted);font-size:12px">{esc(t.exit_reason or "—")}</td>
        </tr>
        """

    return f"""
    <div class="card" style="margin-top:16px">
      <div class="card-title">Latest Run Trades <span class="pill pill-info-soft" style="margin-left:6px">{len(trades)}</span></div>
      <div class="table-wrap">
        <table class="lh-table">
          <thead>
            <tr>
              <th>Symbol</th><th>Dir</th><th>Entry</th><th>Exit</th><th>PnL $</th><th>R</th><th>Reason</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </div>
    """
