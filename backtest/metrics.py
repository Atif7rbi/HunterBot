from __future__ import annotations

from math import inf
from statistics import mean

from backtest.models import BacktestTrade


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return default


def calculate_metrics(trades: list[BacktestTrade]) -> dict[str, float | int]:
    """Calculate basic backtest metrics from a trade list.

    This module is intentionally pure and has no DB/runtime side effects.
    """
    total = len(trades)
    if total == 0:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "net_pnl_usd": 0.0,
            "net_r": 0.0,
            "avg_r": 0.0,
            "best_r": 0.0,
            "worst_r": 0.0,
            "profit_factor": 0.0,
        }

    r_values = [_safe_float(t.pnl_r) for t in trades]
    pnl_values = [_safe_float(t.pnl_usd) for t in trades]

    wins = sum(1 for r in r_values if r > 0)
    losses = sum(1 for r in r_values if r < 0)

    gross_win_r = sum(r for r in r_values if r > 0)
    gross_loss_r = abs(sum(r for r in r_values if r < 0))

    if gross_loss_r == 0 and gross_win_r > 0:
        profit_factor = inf
    elif gross_loss_r == 0:
        profit_factor = 0.0
    else:
        profit_factor = gross_win_r / gross_loss_r

    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / total) * 100.0,
        "net_pnl_usd": sum(pnl_values),
        "net_r": sum(r_values),
        "avg_r": mean(r_values),
        "best_r": max(r_values),
        "worst_r": min(r_values),
        "profit_factor": profit_factor,
    }


def format_metric(value: object, suffix: str = "", digits: int = 2) -> str:
    try:
        v = float(value)
    except Exception:
        return "—"
    if v == inf:
        return "∞"
    return f"{v:.{digits}f}{suffix}"
