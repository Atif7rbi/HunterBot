"""Layer 12 — Performance Analyzer."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from sqlalchemy import select
from src.core.database import AsyncSessionLocal, TradeOutcome

MIN_SAMPLE = 50
ROLLING_WINDOW = 50

def _decay(index: int) -> float:
    if index < 30:
        return 1.0
    if index < 50:
        return 0.6
    return 0.3

@dataclass
class CellStats:
    setup: str
    regime: str
    trades: int = 0
    wins: int = 0
    gross_profit_r: float = 0.0
    gross_loss_r: float = 0.0
    total_r: float = 0.0
    weighted_r: float = 0.0
    equity_curve: list = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def avg_r(self) -> float:
        return self.total_r / self.trades if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        if self.gross_loss_r == 0:
            return 99.0 if self.gross_profit_r > 0 else 0.0
        return self.gross_profit_r / self.gross_loss_r

    @property
    def max_drawdown_r(self) -> float:
        peak, max_dd = 0.0, 0.0
        for x in self.equity_curve:
            peak = max(peak, x)
            max_dd = max(max_dd, peak - x)
        return max_dd

    def verdict(self) -> str:
        if self.trades < MIN_SAMPLE:
            return 'INSUFFICIENT_DATA'
        if self.profit_factor >= 1.8 and self.avg_r >= 0.8 and self.win_rate >= 0.60:
            return 'STRONG_EDGE'
        if self.profit_factor < 1.0 or self.avg_r < 0 or self.win_rate < 0.40:
            return 'UNDERPERFORMING'
        if self.win_rate >= 0.50 and self.avg_r >= 0.5:
            return 'VALID'
        return 'NEUTRAL'

class PerformanceAnalyzer:
    async def analyze(self, rolling_window: int = ROLLING_WINDOW) -> dict:
        async with AsyncSessionLocal() as s:
            res = await s.execute(select(TradeOutcome).order_by(TradeOutcome.closed_at.desc()).limit(rolling_window))
            outcomes = res.scalars().all()
        matrix = {}
        for idx, o in enumerate(outcomes):
            key = (o.setup_type, o.regime)
            if key not in matrix:
                matrix[key] = CellStats(setup=o.setup_type, regime=o.regime)
            cell = matrix[key]
            decay = _decay(idx)
            cell.trades += 1
            if o.result == 'WIN':
                cell.wins += 1
                cell.gross_profit_r += max(o.pnl_r, 0)
            if o.pnl_r < 0:
                cell.gross_loss_r += abs(o.pnl_r)
            cell.total_r += o.pnl_r
            cell.weighted_r += o.pnl_r * decay
            prev = cell.equity_curve[-1] if cell.equity_curve else 0.0
            cell.equity_curve.append(prev + o.pnl_r * decay)
        return matrix


# ---------------------------------------------------------------------------
# Management Audit v1
# ---------------------------------------------------------------------------
# Analytics-only layer. It does not change execution, TP, SL, BE, trailing,
# sizing, scanner, or Direction Authority. It only measures how much of the
# available R was captured by the current trade management model.

from collections import defaultdict
from typing import Any


def _enum_value(value: Any, default: str = "UNKNOWN") -> str:
    if value is None:
        return default
    return str(getattr(value, "value", value) or default)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


class ManagementAuditAnalyzer:
    async def analyze(self, limit: int | None = None, exclude_unknown: bool = True) -> dict:
        async with AsyncSessionLocal() as s:
            stmt = select(TradeOutcome).order_by(TradeOutcome.closed_at.desc())
            if limit is not None:
                stmt = stmt.limit(int(limit))
            res = await s.execute(stmt)
            raw_rows = list(res.scalars().all())

        # V2: keep old/unclassified outcomes visible in metadata, but exclude them
        # from profile/quality/capture calculations by default. Old trades with
        # UNKNOWN profile/class distort the management audit because they were not
        # managed by the current profile engine.
        rows = []
        excluded_unknown = 0
        for o in raw_rows:
            profile_raw = _enum_value(getattr(o, "management_profile", None), "UNKNOWN")
            quality_raw = _enum_value(getattr(o, "trade_quality_class", None), "UNKNOWN")
            if exclude_unknown and (profile_raw == "UNKNOWN" or quality_raw == "UNKNOWN"):
                excluded_unknown += 1
                continue
            rows.append(o)

        trades = []
        profiles: dict[str, dict] = defaultdict(lambda: {
            "trades": 0,
            "wins": 0,
            "total_max_r": 0.0,
            "total_final_r": 0.0,
            "total_capture": 0.0,
            "capture_count": 0,
        })
        qualities: dict[str, dict] = defaultdict(lambda: {
            "trades": 0,
            "wins": 0,
            "total_max_r": 0.0,
            "total_final_r": 0.0,
            "total_capture": 0.0,
            "capture_count": 0,
        })
        exits: dict[str, int] = defaultdict(int)
        trailing_rows = []
        be_rows = []

        for o in rows:
            final_r = _safe_float(getattr(o, "final_r", None), _safe_float(getattr(o, "pnl_r", None)))
            max_r = _safe_float(getattr(o, "max_r_reached", None))
            capture = getattr(o, "profit_capture_ratio", None)
            capture_f = None if capture is None else max(0.0, _safe_float(capture))
            if capture_f is None and max_r > 0:
                capture_f = max(0.0, final_r / max_r)

            profile = _enum_value(getattr(o, "management_profile", None), "UNKNOWN")
            quality = _enum_value(getattr(o, "trade_quality_class", None), "UNKNOWN")
            exit_reason = _enum_value(getattr(o, "exit_reason", None), _enum_value(getattr(o, "result", None), "UNKNOWN"))
            win = final_r > 0
            lost_r = max(0.0, max_r - final_r)
            leakage = 1.0 - capture_f if capture_f is not None else None
            be_r = _safe_float(getattr(o, "be_activated_at_r", None))
            max_after_be = _safe_float(getattr(o, "max_r_after_be", None))
            trail_r = _safe_float(getattr(o, "trailing_activated_at_r", None))
            max_after_trailing = _safe_float(getattr(o, "max_r_after_trailing", None))
            profit_locked_r = _safe_float(getattr(o, "profit_locked_r", None))
            trailing_moves = int(_safe_float(getattr(o, "trailing_moves", None), 0.0))

            # Backward-compatible inference for older outcomes that do not have
            # v2 audit columns yet. This keeps the report useful immediately,
            # while new trades will be more accurate.
            if trail_r <= 0:
                trail_r = _safe_float(getattr(o, "profile_trailing_r", None)) if "Trailing" in exit_reason else 0.0
            if max_after_trailing <= 0 and trail_r > 0:
                max_after_trailing = max_r
            if be_r <= 0:
                be_r = _safe_float(getattr(o, "profile_break_even_r", None)) if ("BE" in exit_reason or trail_r > 0) else 0.0
            if max_after_be <= 0 and be_r > 0:
                max_after_be = max_r

            lost_after_trailing = max(0.0, max_after_trailing - final_r) if trail_r > 0 else None
            lost_after_be = max(0.0, max_after_be - final_r) if be_r > 0 else None

            item = {
                "symbol": getattr(o, "symbol", "—"),
                "direction": _enum_value(getattr(o, "direction", None), "—"),
                "profile": profile,
                "quality": quality,
                "exit_reason": exit_reason,
                "max_r": max_r,
                "final_r": final_r,
                "capture": capture_f,
                "leakage": leakage,
                "lost_r": lost_r,
                "be_activated_at_r": be_r,
                "max_r_after_be": max_after_be,
                "lost_after_be": lost_after_be,
                "trailing_activated_at_r": trail_r,
                "max_r_after_trailing": max_after_trailing,
                "profit_locked_r": profit_locked_r,
                "trailing_moves": trailing_moves,
                "lost_after_trailing": lost_after_trailing,
                "closed_at": getattr(o, "closed_at", None),
            }
            trades.append(item)
            if trail_r > 0:
                trailing_rows.append(item)
            if be_r > 0:
                be_rows.append(item)

            for bucket, key in ((profiles, profile), (qualities, quality)):
                b = bucket[key]
                b["trades"] += 1
                b["wins"] += 1 if win else 0
                b["total_max_r"] += max_r
                b["total_final_r"] += final_r
                if capture_f is not None:
                    b["total_capture"] += capture_f
                    b["capture_count"] += 1
            exits[exit_reason] += 1

        def finalize_bucket(bucket: dict[str, dict]) -> list[dict]:
            out = []
            for name, b in bucket.items():
                n = b["trades"] or 1
                cc = b["capture_count"] or 0
                avg_capture = (b["total_capture"] / cc) if cc else None
                out.append({
                    "name": name,
                    "trades": b["trades"],
                    "win_rate": b["wins"] / n,
                    "avg_max_r": b["total_max_r"] / n,
                    "avg_final_r": b["total_final_r"] / n,
                    "avg_capture": avg_capture,
                    "avg_leakage": (1.0 - avg_capture) if avg_capture is not None else None,
                })
            return sorted(out, key=lambda x: (x["trades"], x["avg_max_r"]), reverse=True)

        count = len(trades)
        avg_max_r = sum(t["max_r"] for t in trades) / count if count else 0.0
        avg_final_r = sum(t["final_r"] for t in trades) / count if count else 0.0
        capture_values = [t["capture"] for t in trades if t["capture"] is not None]
        avg_capture = sum(capture_values) / len(capture_values) if capture_values else None
        avg_leakage = (1.0 - avg_capture) if avg_capture is not None else None

        def avg(items, key):
            vals = [x.get(key) for x in items if x.get(key) is not None]
            return (sum(vals) / len(vals)) if vals else None

        trailing_summary = {
            "count": len(trailing_rows),
            "avg_activation_r": avg(trailing_rows, "trailing_activated_at_r"),
            "avg_max_after_trailing": avg(trailing_rows, "max_r_after_trailing"),
            "avg_final_r": avg(trailing_rows, "final_r"),
            "avg_lost_after_trailing": avg(trailing_rows, "lost_after_trailing"),
            "avg_profit_locked_r": avg(trailing_rows, "profit_locked_r"),
            "avg_moves": avg(trailing_rows, "trailing_moves"),
            "worst": sorted(trailing_rows, key=lambda t: (t.get("lost_after_trailing") or 0.0), reverse=True)[:8],
        }
        be_summary = {
            "count": len(be_rows),
            "avg_activation_r": avg(be_rows, "be_activated_at_r"),
            "avg_max_after_be": avg(be_rows, "max_r_after_be"),
            "avg_final_r": avg(be_rows, "final_r"),
            "avg_lost_after_be": avg(be_rows, "lost_after_be"),
        }

        return {
            "summary": {
                "trades": count,
                "raw_trades": len(raw_rows),
                "excluded_unknown": excluded_unknown,
                "avg_max_r": avg_max_r,
                "avg_final_r": avg_final_r,
                "avg_capture": avg_capture,
                "avg_leakage": avg_leakage,
            },
            "profiles": finalize_bucket(profiles),
            "qualities": finalize_bucket(qualities),
            "exits": sorted([{"reason": k, "count": v} for k, v in exits.items()], key=lambda x: x["count"], reverse=True),
            "lost_winners": sorted(trades, key=lambda t: t["lost_r"], reverse=True)[:10],
            "trailing_audit": trailing_summary,
            "be_audit": be_summary,
            "trades": trades,
        }
