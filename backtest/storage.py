from __future__ import annotations

import json
from pathlib import Path

from backtest.config import RESULTS_DIR
from backtest.metrics import calculate_metrics
from backtest.models import BacktestRun


def ensure_results_dir() -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    return RESULTS_DIR


def list_result_files() -> list[Path]:
    root = ensure_results_dir()
    return sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def load_run(path: Path) -> BacktestRun | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        run = BacktestRun.from_dict(raw)
        if not run.metrics:
            run.metrics = calculate_metrics(run.trades)
        return run
    except Exception:
        return None


def load_recent_runs(limit: int = 20) -> list[BacktestRun]:
    runs: list[BacktestRun] = []
    for path in list_result_files()[:limit]:
        run = load_run(path)
        if run is not None:
            runs.append(run)
    return runs


def save_run(run: BacktestRun) -> Path:
    root = ensure_results_dir()
    if not run.metrics:
        run.metrics = calculate_metrics(run.trades)
    safe_id = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in run.run_id)
    path = root / f"{safe_id}.json"
    path.write_text(json.dumps(run.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path
