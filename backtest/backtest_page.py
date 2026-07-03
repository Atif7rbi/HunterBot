from __future__ import annotations

from html import escape as esc

from nicegui import ui

from backtest.charts import render_equity_curve_placeholder
from backtest.config import MAX_RECENT_RUNS, RESULTS_DIR
from backtest.metrics import format_metric
from backtest.storage import load_recent_runs
from backtest.tables import render_runs_table
from ui.app import _page_shell


def _metric(run, key: str, default=0):
    return (run.metrics or {}).get(key, default) if run else default


def _run_status_label(has_runs: bool) -> str:
    return "READY" if has_runs else "NO RUNS"


def _run_status_class(has_runs: bool) -> str:
    return "green" if has_runs else "yellow"


def _is_skeleton_run(run) -> bool:
    if not run:
        return False
    cfg = getattr(run, "config", {}) or {}
    return str(cfg.get("engine_stage", "")).lower() == "skeleton"


def _has_real_equity_curve(run) -> bool:
    if not run or not getattr(run, "equity_curve", None):
        return False
    if _is_skeleton_run(run):
        return False
    return len(run.equity_curve) > 1


def _diagnostics(run) -> dict:
    if not run:
        return {}
    cfg = getattr(run, "config", {}) or {}
    diag = cfg.get("replay_diagnostics") or {}
    return diag if isinstance(diag, dict) else {}


def _cfg(run) -> dict:
    if not run:
        return {}
    cfg = getattr(run, "config", {}) or {}
    return cfg if isinstance(cfg, dict) else {}


def _render_decision_runner_status(run) -> str:
    cfg = _cfg(run)
    if not cfg:
        return ""

    enabled = bool(cfg.get("decision_runner_enabled"))
    decision_results = int(cfg.get("decision_results") or 0)
    decision_executed = int(cfg.get("decision_executed") or 0)
    decision_disabled = int(cfg.get("decision_disabled") or 0)
    decision_failed = int(cfg.get("decision_failed") or 0)
    decision_signals = int(cfg.get("decision_signals") or 0)

    status_text = "ENABLED" if enabled else "DISABLED"
    status_color = "#00f58c" if enabled else "#f6c453"
    status_desc = (
        "Live DecisionEngine execution is enabled for this replay run."
        if enabled
        else "DecisionRunner is present, but live DecisionEngine execution is disabled."
    )

    failed_color = "#ff5b61" if decision_failed else "#00f58c"
    signal_color = "#00f58c" if decision_signals else "#7c8aa3"

    return f"""
    <div class="card" style="margin-top:16px">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:14px">
        <div>
          <div class="card-title" style="margin:0 0 6px">Decision Runner</div>
          <div style="font-size:13px;color:var(--text-muted);line-height:1.7">{status_desc}</div>
        </div>
        <span style="font-family:var(--font-mono);font-size:12px;font-weight:900;color:{status_color};
                     background:rgba(255,255,255,.05);border:1px solid var(--border);
                     border-radius:999px;padding:6px 12px">RUNNER: {status_text}</span>
      </div>

      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px">
        <div style="border:1px solid var(--border);border-radius:10px;padding:14px;background:rgba(255,255,255,.03)">
          <div style="font-size:12px;color:var(--text-muted);text-transform:uppercase;font-weight:800">Decision Results</div>
          <div class="mono" style="font-size:24px;font-weight:900;color:#53a7ff;margin-top:6px">{decision_results}</div>
          <div style="font-size:12px;color:var(--text-muted);margin-top:4px">Runner outputs</div>
        </div>
        <div style="border:1px solid var(--border);border-radius:10px;padding:14px;background:rgba(255,255,255,.03)">
          <div style="font-size:12px;color:var(--text-muted);text-transform:uppercase;font-weight:800">Executed</div>
          <div class="mono" style="font-size:24px;font-weight:900;color:#00f58c;margin-top:6px">{decision_executed}</div>
          <div style="font-size:12px;color:var(--text-muted);margin-top:4px">Live DecisionEngine calls</div>
        </div>
        <div style="border:1px solid var(--border);border-radius:10px;padding:14px;background:rgba(255,255,255,.03)">
          <div style="font-size:12px;color:var(--text-muted);text-transform:uppercase;font-weight:800">Disabled</div>
          <div class="mono" style="font-size:24px;font-weight:900;color:#f6c453;margin-top:6px">{decision_disabled}</div>
          <div style="font-size:12px;color:var(--text-muted);margin-top:4px">Safe placeholder decisions</div>
        </div>
        <div style="border:1px solid var(--border);border-radius:10px;padding:14px;background:rgba(255,255,255,.03)">
          <div style="font-size:12px;color:var(--text-muted);text-transform:uppercase;font-weight:800">Failed</div>
          <div class="mono" style="font-size:24px;font-weight:900;color:{failed_color};margin-top:6px">{decision_failed}</div>
          <div style="font-size:12px;color:var(--text-muted);margin-top:4px">Import/execution failures</div>
        </div>
        <div style="border:1px solid var(--border);border-radius:10px;padding:14px;background:rgba(255,255,255,.03)">
          <div style="font-size:12px;color:var(--text-muted);text-transform:uppercase;font-weight:800">Signals</div>
          <div class="mono" style="font-size:24px;font-weight:900;color:{signal_color};margin-top:6px">{decision_signals}</div>
          <div style="font-size:12px;color:var(--text-muted);margin-top:4px">LONG / SHORT decisions</div>
        </div>
      </div>
    </div>
    """


def _coverage(diag: dict, key: str) -> float:
    cov = diag.get("coverage") or {}
    try:
        return float(cov.get(key) or 0.0)
    except Exception:
        return 0.0


def _coverage_bar(label: str, pct: float, important: bool = True) -> str:
    if pct >= 90:
        color = "#00f58c"
        tone = "READY"
    elif pct > 0:
        color = "#f6c453"
        tone = "PARTIAL"
    else:
        color = "#ff5b61" if important else "#7c8aa3"
        tone = "MISSING" if important else "OPTIONAL"

    return f"""
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
      <div style="width:120px;font-size:12px;color:var(--text-muted);font-weight:800;text-transform:uppercase">{label}</div>
      <div style="flex:1;background:rgba(255,255,255,.06);border:1px solid var(--border);height:18px;border-radius:999px;overflow:hidden">
        <div style="height:100%;width:{max(0,min(pct,100)):.1f}%;background:{color};border-radius:999px"></div>
      </div>
      <div class="mono" style="width:58px;text-align:right;color:var(--text);font-size:12px">{pct:.1f}%</div>
      <div class="mono" style="width:62px;text-align:right;color:{color};font-size:10px;font-weight:900">{tone}</div>
    </div>
    """


def _feature_card(title: str, status: str, desc: str, tone: str = "muted") -> str:
    color = {
        "ready": "#00f58c",
        "partial": "#f6c453",
        "pending": "var(--text-muted)",
        "muted": "var(--text-muted)",
        "danger": "#ff5b61",
    }.get(tone, "var(--text-muted)")

    badge_bg = {
        "ready": "rgba(0,245,140,.12)",
        "partial": "rgba(246,199,83,.12)",
        "pending": "rgba(255,255,255,.05)",
        "muted": "rgba(255,255,255,.05)",
        "danger": "rgba(255,91,97,.12)",
    }.get(tone, "rgba(255,255,255,.05)")

    return f"""
    <div style="border:1px solid var(--border);border-radius:10px;padding:14px;background:rgba(255,255,255,.03)">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px">
        <div style="font-weight:800;color:var(--text)">{title}</div>
        <span style="font-family:var(--font-mono);font-size:11px;font-weight:800;color:{color};background:{badge_bg};border:1px solid var(--border);border-radius:999px;padding:3px 8px">{status}</span>
      </div>
      <div style="font-size:13px;color:var(--text-muted);line-height:1.6">{desc}</div>
    </div>
    """


def _timeline_step(num: int, title: str, status: str, tone: str, desc: str, last: bool = False) -> str:
    color = {
        "ready": "#00f58c",
        "partial": "#f6c453",
        "pending": "#7c8aa3",
    }.get(tone, "#7c8aa3")

    connector = "" if last else """
      <div style="position:absolute;left:14px;top:34px;bottom:-18px;width:2px;background:rgba(255,255,255,.08)"></div>
    """

    return f"""
    <div style="position:relative;display:flex;gap:14px;align-items:flex-start;padding-bottom:{'0' if last else '18px'}">
      {connector}
      <div style="width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;
                  background:rgba(255,255,255,.05);border:1px solid var(--border);color:{color};
                  font-family:var(--font-mono);font-weight:900;flex-shrink:0;z-index:1">{num}</div>
      <div style="flex:1;border:1px solid var(--border);border-radius:10px;padding:12px 14px;background:rgba(255,255,255,.025)">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <div style="font-weight:900;color:var(--text)">{title}</div>
          <span style="font-family:var(--font-mono);font-size:10px;color:{color};letter-spacing:.05em">{status}</span>
        </div>
        <div style="font-size:12px;color:var(--text-muted);line-height:1.6;margin-top:4px">{desc}</div>
      </div>
    </div>
    """


def _render_engine_status(runs_count: int) -> str:
    results_dir = str(RESULTS_DIR)
    return f"""
    <div class="card" style="margin-top:16px">
      <div class="card-title">Engine Status</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px">
        {_feature_card("Storage", "READY", f"Result reader is active and watches <code>{results_dir}/*.json</code>.", "ready")}
        {_feature_card("Saved Runs", str(runs_count), "Recent JSON runs will appear automatically after engine output exists.", "partial" if runs_count else "pending")}
        {_feature_card("Execution Engine", "SKELETON", "engine.py can create placeholder runs only; no strategy logic is executed yet.", "partial")}
        {_feature_card("Live Trading Safety", "ISOLATED", "Backtest foundation reads files only; it does not touch live trades or DB state.", "ready")}
      </div>
    </div>
    """


def _render_replay_quality(run) -> str:
    diag = _diagnostics(run)
    if not diag:
        return """
        <div class="card" style="margin-top:16px">
          <div class="card-title">Replay Data Quality</div>
          <div style="color:var(--text-muted);font-size:14px;padding:24px 0;text-align:center">
            No replay diagnostics available for the latest run.
          </div>
        </div>
        """

    hunter_ready = bool(diag.get("hunter_ready"))
    warnings = diag.get("warnings") or []
    warnings_html = "".join(
        f'<div style="font-size:12px;color:#f6c453;line-height:1.7">⚠ {esc(str(w))}</div>'
        for w in warnings
    ) or '<div style="font-size:12px;color:#00f58c;line-height:1.7">No warnings.</div>'

    ready_color = "#00f58c" if hunter_ready else "#ff5b61"
    ready_text = "YES" if hunter_ready else "NO"

    total = int(diag.get("total_points") or 0)
    decision_inputs = int(diag.get("decision_input_points") or 0)
    decision_ready = int(diag.get("decision_ready_points") or 0)
    adapter_warnings = int(diag.get("adapter_warning_points") or 0)
    decision_bridge_ready = bool(diag.get("decision_bridge_ready"))
    first_ts = esc(str(diag.get("first_timestamp") or "—"))
    last_ts = esc(str(diag.get("last_timestamp") or "—"))

    bars = ""
    bars += _coverage_bar("Candles", _coverage(diag, "candles_pct"), True)
    bars += _coverage_bar("Funding", _coverage(diag, "funding_pct"), True)
    bars += _coverage_bar("OI", _coverage(diag, "oi_pct"), True)
    bars += _coverage_bar("OI Δ", _coverage(diag, "oi_change_pct"), False)
    bars += _coverage_bar("LS", _coverage(diag, "ls_pct"), True)
    bars += _coverage_bar("Decision Inputs", _coverage(diag, "decision_input_pct"), True)
    bars += _coverage_bar("Decision Ready", _coverage(diag, "decision_ready_pct"), True)
    bars += _coverage_bar("Taker Flow", _coverage(diag, "taker_flow_pct"), False)
    bars += _coverage_bar("Liquidity", _coverage(diag, "liquidity_pct"), False)
    bars += _coverage_bar("Price", _coverage(diag, "price_pct"), True)

    return f"""
    <div class="card" style="margin-top:16px">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:14px">
        <div class="card-title" style="margin:0">Replay Data Quality</div>
        <span style="font-family:var(--font-mono);font-size:12px;font-weight:900;color:{ready_color};
                     background:rgba(255,255,255,.05);border:1px solid var(--border);
                     border-radius:999px;padding:6px 12px">HUNTER READY: {ready_text}</span>
      </div>

      <div style="display:grid;grid-template-columns:minmax(320px,1.4fr) minmax(280px,.8fr);gap:18px;align-items:start">
        <div>
          {bars}
        </div>
        <div style="border:1px solid var(--border);border-radius:12px;padding:14px;background:rgba(255,255,255,.025)">
          <div style="font-weight:900;color:var(--text);margin-bottom:10px">Latest Run Diagnostics</div>
          <table class="lh-table" style="width:100%">
            <tbody>
              <tr><td style="color:var(--text-muted)">Replay Points</td><td class="mono" style="text-align:right">{total}</td></tr>
              <tr><td style="color:var(--text-muted)">Decision Inputs</td><td class="mono" style="text-align:right">{decision_inputs}</td></tr>
              <tr><td style="color:var(--text-muted)">Decision Ready</td><td class="mono" style="text-align:right">{decision_ready}</td></tr>
              <tr><td style="color:var(--text-muted)">Adapter Warnings</td><td class="mono" style="text-align:right">{adapter_warnings}</td></tr>
              <tr><td style="color:var(--text-muted)">Decision Bridge</td><td class="mono" style="text-align:right;color:{'#00f58c' if decision_bridge_ready else '#ff5b61'}">{'READY' if decision_bridge_ready else 'NOT READY'}</td></tr>
              <tr><td style="color:var(--text-muted)">First</td><td class="mono" style="text-align:right">{first_ts}</td></tr>
              <tr><td style="color:var(--text-muted)">Last</td><td class="mono" style="text-align:right">{last_ts}</td></tr>
            </tbody>
          </table>
          <div style="font-weight:900;color:var(--text);margin:14px 0 8px">Warnings</div>
          {warnings_html}
        </div>
      </div>
    </div>
    """


def _render_skeleton_warning(run) -> str:
    if not _is_skeleton_run(run):
        return ""
    return """
    <div class="card" style="margin-top:16px;border-color:rgba(246,199,83,.28);background:rgba(246,199,83,.045)">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
        <div>
          <div class="card-title" style="margin-bottom:6px">Skeleton Run Detected</div>
          <div style="color:var(--text-muted);font-size:14px;line-height:1.7">
            This run only validates the pipeline: Engine → JSON → Storage → UI.
            It does not contain real market replay, strategy signals, or execution simulation.
          </div>
        </div>
        <span style="font-family:var(--font-mono);font-size:12px;font-weight:900;color:#f6c453;
                     background:rgba(246,199,83,.12);border:1px solid rgba(246,199,83,.28);
                     border-radius:999px;padding:7px 12px">SKELETON RUN</span>
      </div>
    </div>
    """


def _render_pipeline() -> str:
    return f"""
    <div class="card" style="margin-top:16px">
      <div class="card-title">Hybrid Replay Pipeline</div>
      <div style="display:grid;grid-template-columns:minmax(280px,460px) 1fr;gap:18px;align-items:start">
        <div>
          {_timeline_step(1, "Historical Market Data", "PENDING", "pending", "Candles plus Funding, LS, OI, and liquidity context.")}
          {_timeline_step(2, "Replay Engine", "PENDING", "pending", "Rebuild market state candle-by-candle without touching live bot.")}
          {_timeline_step(3, "Strategy Logic", "PLANNED", "pending", "Reuse Hunter Bot logic carefully so tests match live behavior.")}
          {_timeline_step(4, "Execution Simulation", "PLANNED", "pending", "Simulate entry, SL, TP1, BE, trailing, fees, and slippage.")}
          {_timeline_step(5, "Metrics", "READY", "ready", "Win rate, Net R, Avg R, Best/Worst R, and Profit Factor foundation exists.")}
          {_timeline_step(6, "Storage + Viewer", "READY", "ready", "JSON result loader and UI viewer are active.", last=True)}
        </div>
        <div style="border:1px solid var(--border);border-radius:12px;padding:16px;background:rgba(83,167,255,.05)">
          <div style="font-weight:900;color:var(--text);font-size:15px;margin-bottom:8px">Why Hybrid?</div>
          <div style="font-size:13px;color:var(--text-muted);line-height:1.8">
            Hunter Bot is not a candle-only strategy. A useful backtest must replay the same context used by the live system:
            <br><br>
            <span class="mono" style="color:#7dd3fc">Candles + Funding + Long/Short + OI + Liquidity</span>
            <br><br>
            This avoids misleading results from a simple OHLC-only backtest.
          </div>
        </div>
      </div>
    </div>
    """


def _render_supported_features() -> str:
    return f"""
    <div class="card" style="margin-top:16px">
      <div class="card-title">Supported Features</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px">
        {_feature_card("BacktestRun Model", "READY", "Standard dataclass for run metadata, metrics, trades, and equity curve.", "ready")}
        {_feature_card("BacktestTrade Model", "READY", "Standard trade record format for future replay output.", "ready")}
        {_feature_card("Metrics Module", "READY", "Calculates total trades, win rate, net R, avg R, best/worst R, and PF.", "ready")}
        {_feature_card("Results Storage", "READY", "Loads/saves JSON result files in backtest/results.", "ready")}
        {_feature_card("Replay Diagnostics", "READY", "Shows data coverage before strategy replay is trusted.", "ready")}
        {_feature_card("Decision Bridge", "READY", "Builds DecisionEngine-compatible inputs without executing strategy.", "ready")}
        {_feature_card("Decision Runner", "READY", "Tracks disabled/executed/failed DecisionEngine replay attempts.", "ready")}
        {_feature_card("Equity Curve", "PLACEHOLDER", "Shown only when useful; skeleton runs are clearly labeled.", "partial")}
        {_feature_card("Replay Engine", "NOT BUILT", "Requires real historical source and Hunter snapshot reconstruction.", "pending")}
        {_feature_card("PDF / CSV Report", "LATER", "Will be added after run format stabilizes.", "pending")}
      </div>
    </div>
    """


def _render_getting_started() -> str:
    return """
    <div class="card" style="margin-top:16px">
      <div class="card-title">Getting Started</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px">
        <div style="border:1px solid var(--border);border-radius:10px;padding:14px;background:rgba(255,255,255,.03)">
          <div style="font-weight:900;color:var(--text)">1. Define Replay Source</div>
          <div style="font-size:13px;color:var(--text-muted);line-height:1.7;margin-top:6px">
            Decide where historical candles, funding, LS, OI, and liquidity context will come from.
          </div>
        </div>
        <div style="border:1px solid var(--border);border-radius:10px;padding:14px;background:rgba(255,255,255,.03)">
          <div style="font-weight:900;color:var(--text)">2. Build replay.py</div>
          <div style="font-size:13px;color:var(--text-muted);line-height:1.7;margin-top:6px">
            Convert historical context into chronological ReplayPoint objects.
          </div>
        </div>
        <div style="border:1px solid var(--border);border-radius:10px;padding:14px;background:rgba(255,255,255,.03)">
          <div style="font-weight:900;color:var(--text)">3. Build engine.py</div>
          <div style="font-size:13px;color:var(--text-muted);line-height:1.7;margin-top:6px">
            Generate real BacktestRun objects and write results into backtest/results.
          </div>
        </div>
      </div>
    </div>
    """


def _render_replay_activity(run) -> str:
    if not run:
        return ""

    cfg = getattr(run, "config", {}) or {}
    stage = str(cfg.get("engine_stage") or "").lower()
    replay_points = int(cfg.get("replay_points") or 0)
    strategy_events = int(cfg.get("strategy_events") or 0)

    if stage != "replay_loop" or replay_points <= 0:
        return ""

    tick_dots = ""
    visible_points = min(replay_points, 24)
    for i in range(visible_points):
        tick_dots += """
        <div style="width:10px;height:10px;border-radius:50%;background:#53a7ff;
                    box-shadow:0 0 10px rgba(83,167,255,.45);flex-shrink:0"></div>
        """
        if i < visible_points - 1:
            tick_dots += """
            <div style="height:2px;min-width:18px;flex:1;background:rgba(83,167,255,.28)"></div>
            """

    if replay_points > visible_points:
        tick_dots += f"""
        <div class="mono" style="font-size:12px;color:var(--text-muted);margin-left:8px">
          +{replay_points - visible_points} more
        </div>
        """

    return f"""
    <div class="card" style="margin-top:16px">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:14px">
        <div class="card-title" style="margin:0">Replay Activity</div>
        <span style="font-family:var(--font-mono);font-size:12px;font-weight:900;color:#53a7ff;
                     background:rgba(83,167,255,.12);border:1px solid rgba(83,167,255,.28);
                     border-radius:999px;padding:6px 12px">REPLAY LOOP ACTIVE</span>
      </div>

      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:16px">
        <div style="border:1px solid var(--border);border-radius:10px;padding:14px;background:rgba(255,255,255,.03)">
          <div style="font-size:12px;color:var(--text-muted);text-transform:uppercase;font-weight:800">Replay Points</div>
          <div class="mono" style="font-size:24px;font-weight:900;color:#53a7ff;margin-top:6px">{replay_points}</div>
          <div style="font-size:12px;color:var(--text-muted);margin-top:4px">Market steps processed</div>
        </div>
        <div style="border:1px solid var(--border);border-radius:10px;padding:14px;background:rgba(255,255,255,.03)">
          <div style="font-size:12px;color:var(--text-muted);text-transform:uppercase;font-weight:800">Strategy Events</div>
          <div class="mono" style="font-size:24px;font-weight:900;color:#f6c453;margin-top:6px">{strategy_events}</div>
          <div style="font-size:12px;color:var(--text-muted);margin-top:4px">Currently REPLAY_TICK only</div>
        </div>
        <div style="border:1px solid var(--border);border-radius:10px;padding:14px;background:rgba(255,255,255,.03)">
          <div style="font-size:12px;color:var(--text-muted);text-transform:uppercase;font-weight:800">Decision Bridge</div>
          <div class="mono" style="font-size:20px;font-weight:900;color:#00f58c;margin-top:8px">READY</div>
          <div style="font-size:12px;color:var(--text-muted);margin-top:4px">Inputs built; strategy not executed</div>
        </div>
      </div>

      <div style="border:1px solid var(--border);border-radius:12px;padding:14px;background:rgba(83,167,255,.045)">
        <div style="font-weight:900;color:var(--text);margin-bottom:12px">Replay Timeline</div>
        <div style="display:flex;align-items:center;gap:0;width:100%;overflow:hidden">
          {tick_dots}
        </div>
        <div style="font-size:12px;color:var(--text-muted);margin-top:12px;line-height:1.7">
          This confirms the engine is processing historical replay points. Trades remain disabled until Hunter snapshot reconstruction and execution simulation are connected.
        </div>
      </div>
    </div>
    """


@ui.page("/backtest")
async def page_backtest() -> None:
    container = _page_shell("Backtest", "REPLAY ENGINE • RESULTS FOUNDATION")
    runs = load_recent_runs(limit=MAX_RECENT_RUNS)
    latest = runs[0] if runs else None
    has_runs = bool(runs)

    with container:
        ui.html(f"""
        <div class="kpi-row">
          <div class="kpi">
            <div class="kpi-label">Backtest Status</div>
            <div class="kpi-val {_run_status_class(has_runs)}">{_run_status_label(has_runs)}</div>
            <div class="kpi-change">Reads backtest/results/*.json</div>
          </div>
          <div class="kpi">
            <div class="kpi-label">Saved Runs</div>
            <div class="kpi-val cyan">{len(runs)}</div>
            <div class="kpi-change">Recent result files</div>
          </div>
          <div class="kpi">
            <div class="kpi-label">Latest Trades</div>
            <div class="kpi-val">{int(_metric(latest, "total_trades", len(latest.trades)) if latest else 0)}</div>
            <div class="kpi-change">From newest run</div>
          </div>
          <div class="kpi">
            <div class="kpi-label">Latest Net R</div>
            <div class="kpi-val {'green' if latest and _metric(latest, 'net_r', 0) >= 0 else 'red'}">{format_metric(_metric(latest, "net_r", 0) if latest else 0)}R</div>
            <div class="kpi-change">Not live trading</div>
          </div>
          <div class="kpi">
            <div class="kpi-label">Latest Win Rate</div>
            <div class="kpi-val yellow">{format_metric(_metric(latest, "win_rate", 0) if latest else 0, "%")}</div>
            <div class="kpi-change">Closed simulated trades</div>
          </div>
        </div>
        """)

        ui.html(_render_engine_status(len(runs)))
        ui.html(_render_pipeline())
        ui.html(_render_supported_features())

        if not has_runs:
            ui.html(_render_getting_started())
            with ui.element("div").classes("card").style("margin-top:16px"):
                ui.html("""
                <div class="card-title">Backtest Runs</div>
                <div style="color:var(--text-muted);font-size:14px;padding:28px 0;text-align:center">
                  No backtest result files found yet.<br>
                  Future results should be saved as JSON inside <code>backtest/results/</code>.
                </div>
                """)
            return

        ui.html(_render_skeleton_warning(latest))
        ui.html(_render_replay_quality(latest))
        ui.html(_render_decision_runner_status(latest))

        with ui.element("div").classes("card").style("margin-top:16px"):
            ui.html("""
            <div class="card-title">Backtest Runs</div>
            <div style="color:var(--text-muted);font-size:14px;line-height:1.8;margin-bottom:14px">
              Future engine output should be written to <code>backtest/results/&lt;run_id&gt;.json</code>.
              This viewer updates after restart or refresh.
            </div>
            """)
            ui.html(render_runs_table(runs))

        if _has_real_equity_curve(latest):
            ui.html(render_equity_curve_placeholder(latest.equity_curve))
        else:
            activity_html = _render_replay_activity(latest)
            if activity_html:
                ui.html(activity_html)
            elif latest:
                with ui.element("div").classes("card").style("margin-top:16px"):
                    ui.html("""
                    <div class="card-title">Equity Curve</div>
                    <div style="color:var(--text-muted);font-size:14px;padding:22px 0;text-align:center">
                      Equity curve is hidden for skeleton runs to avoid confusing placeholder data with real backtest results.
                    </div>
                    """)
