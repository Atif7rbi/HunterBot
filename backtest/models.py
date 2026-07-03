from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class BacktestTrade:
    symbol: str
    direction: str
    entry_time: str | None = None
    exit_time: str | None = None
    entry_price: float = 0.0
    exit_price: float = 0.0
    pnl_usd: float = 0.0
    pnl_r: float = 0.0
    exit_reason: str = ""
    setup_score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BacktestTrade":
        return cls(
            symbol=str(raw.get("symbol", "")),
            direction=str(raw.get("direction", "")),
            entry_time=raw.get("entry_time") or raw.get("opened_at") or raw.get("created_at"),
            exit_time=raw.get("exit_time") or raw.get("closed_at"),
            entry_price=float(raw.get("entry_price") or raw.get("entry") or 0.0),
            exit_price=float(raw.get("exit_price") or raw.get("exit") or 0.0),
            pnl_usd=float(raw.get("pnl_usd") or raw.get("pnl") or 0.0),
            pnl_r=float(raw.get("pnl_r") or raw.get("r") or 0.0),
            exit_reason=str(raw.get("exit_reason") or raw.get("reason") or ""),
            setup_score=float(raw.get("setup_score") or raw.get("score") or 0.0),
            metadata=dict(raw.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BacktestRun:
    run_id: str
    symbol: str = "UNKNOWN"
    timeframe: str = "UNKNOWN"
    days: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    config: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BacktestRun":
        trades_raw = raw.get("trades") or []
        trades = [
            t if isinstance(t, BacktestTrade) else BacktestTrade.from_dict(dict(t))
            for t in trades_raw
            if isinstance(t, (dict, BacktestTrade))
        ]
        return cls(
            run_id=str(raw.get("run_id") or raw.get("id") or "unknown"),
            symbol=str(raw.get("symbol") or raw.get("config", {}).get("symbol") or "UNKNOWN"),
            timeframe=str(raw.get("timeframe") or raw.get("config", {}).get("timeframe") or "UNKNOWN"),
            days=int(raw.get("days") or raw.get("config", {}).get("days") or 0),
            created_at=str(raw.get("created_at") or ""),
            config=dict(raw.get("config") or {}),
            metrics=dict(raw.get("metrics") or {}),
            trades=trades,
            equity_curve=list(raw.get("equity_curve") or []),
            notes=str(raw.get("notes") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["trades"] = [t.to_dict() for t in self.trades]
        return data
