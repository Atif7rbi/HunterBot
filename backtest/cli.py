from __future__ import annotations

import argparse

from backtest.engine import run_backtest


def _fmt_pct(value: object) -> str:
    try:
        v = float(value or 0.0)
    except Exception:
        return "0.0%"
    return f"{v:.1f}%"


def _fmt_bool(value: object) -> str:
    return "True" if bool(value) else "False"


def _diagnostics(run) -> dict:
    cfg = run.config or {}
    diag = cfg.get("replay_diagnostics") or {}
    return diag if isinstance(diag, dict) else {}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hunter Bot Backtest Runner"
    )

    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--source-path", default=None, help="Replay source file (.json/.jsonl/.csv)")
    parser.add_argument("--enable-decision-engine", action="store_true", help="Experimental: attempt to run live DecisionEngine in replay mode.")
    parser.add_argument("--no-save", action="store_true")

    args = parser.parse_args()

    run = run_backtest(
        symbol=args.symbol,
        timeframe=args.timeframe,
        days=args.days,
        source_path=args.source_path,
        enable_decision_engine=args.enable_decision_engine,
        save=not args.no_save,
    )

    cfg = run.config or {}
    diag = _diagnostics(run)
    coverage = diag.get("coverage") or {}
    warnings = diag.get("warnings") or []

    print()
    print("=" * 60)
    print("Hunter Backtest")
    print("=" * 60)

    print(f"Run ID             : {run.run_id}")
    print(f"Symbol             : {run.symbol}")
    print(f"Timeframe          : {run.timeframe}")
    print(f"Days               : {run.days}")

    print("-" * 60)

    print(f"Engine Stage       : {cfg.get('engine_stage')}")
    print(f"Replay Points      : {cfg.get('replay_points', 0)}")
    print(f"Hunter Snapshots   : {cfg.get('hunter_snapshots', 0)}")
    print(f"Decision Inputs    : {cfg.get('decision_inputs', 0)}")
    print(f"Decision Ready     : {cfg.get('decision_ready', 0)}")
    print(f"Decision Results   : {cfg.get('decision_results', 0)}")
    print(f"Decision Executed  : {cfg.get('decision_executed', 0)}")
    print(f"Decision Disabled  : {cfg.get('decision_disabled', 0)}")
    print(f"Decision Failed    : {cfg.get('decision_failed', 0)}")
    print(f"Decision Signals   : {cfg.get('decision_signals', 0)}")
    print(f"Strategy Results   : {cfg.get('strategy_results', 0)}")
    print(f"Strategy Signals   : {cfg.get('strategy_signals', 0)}")
    print(f"Strategy LONG      : {cfg.get('strategy_long', 0)}")
    print(f"Strategy SHORT     : {cfg.get('strategy_short', 0)}")
    print(f"Strategy WATCH     : {cfg.get('strategy_watch', 0)}")
    print(f"Strategy WAIT      : {cfg.get('strategy_wait', 0)}")
    print(f"Strategy Events    : {cfg.get('strategy_events', 0)}")
    print(f"Strategy Connected : {cfg.get('strategy_connected')}")
    print(f"Execution Connected: {cfg.get('execution_connected')}")

    print("-" * 60)

    print(f"Hunter Ready       : {_fmt_bool(cfg.get('hunter_ready'))}")
    print(f"Decision Bridge    : {_fmt_bool(cfg.get('decision_bridge_ready'))}")
    print(f"Decision Runner    : {'ENABLED' if cfg.get('decision_runner_enabled') else 'DISABLED'}")
    print(f"Strategy Runner    : {'ENABLED' if cfg.get('backtest_strategy_runner') else 'DISABLED'}")
    print(f"Snapshot Coverage  : {_fmt_pct(coverage.get('hunter_snapshot_pct'))}")
    print(f"Minimum Ready      : {_fmt_pct(coverage.get('hunter_minimum_ready_pct'))}")
    print(f"Decision Inputs    : {_fmt_pct(coverage.get('decision_input_pct'))}")
    print(f"Decision Ready     : {_fmt_pct(coverage.get('decision_ready_pct'))}")
    print(f"Decision Executed  : {_fmt_pct(coverage.get('decision_executed_pct'))}")
    print(f"Decision Signals   : {_fmt_pct(coverage.get('decision_signal_pct'))}")
    print(f"Strategy Signals   : {_fmt_pct(coverage.get('strategy_signal_pct'))}")
    print(f"Strategy WATCH     : {_fmt_pct(coverage.get('strategy_watch_pct'))}")
    print(f"Strategy WAIT      : {_fmt_pct(coverage.get('strategy_wait_pct'))}")
    print(f"Candle Coverage    : {_fmt_pct(coverage.get('candles_pct'))}")
    print(f"Funding Coverage   : {_fmt_pct(coverage.get('funding_pct'))}")
    print(f"OI Coverage        : {_fmt_pct(coverage.get('oi_pct'))}")
    print(f"OI Δ Coverage      : {_fmt_pct(coverage.get('oi_change_pct'))}")
    print(f"LS Coverage        : {_fmt_pct(coverage.get('ls_pct'))}")
    print(f"Taker Flow Coverage: {_fmt_pct(coverage.get('taker_flow_pct'))}")
    print(f"Liquidity Coverage : {_fmt_pct(coverage.get('liquidity_pct'))}")
    print(f"Price Coverage     : {_fmt_pct(coverage.get('price_pct'))}")

    if diag:
        print(f"First Timestamp    : {diag.get('first_timestamp') or '—'}")
        print(f"Last Timestamp     : {diag.get('last_timestamp') or '—'}")

    print("-" * 60)

    if args.source_path:
        print(f"Replay Source      : {args.source_path}")
    else:
        print("Replay Source      : None")

    if warnings:
        print("-" * 60)
        print("Warnings:")
        for item in warnings:
            print(f"  - {item}")

    print("=" * 60)


if __name__ == "__main__":
    main()
