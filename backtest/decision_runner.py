from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from backtest.adapters import ReplayAdapterBundle
from backtest.snapshot import HunterReplaySnapshot


@dataclass(slots=True)
class BacktestDecision:
    """Backtest-safe representation of a DecisionEngine result.

    This is intentionally separate from src.layers.decision_engine.DecisionResult
    so the backtest module can remain isolated from live DB/runtime side effects.
    """

    timestamp: str
    symbol: str
    engine_status: str
    direction: str = "WAIT"
    score: float = 0.0
    raw_score: float = 0.0
    size_factor: float = 0.0
    is_watch_zone: bool = False
    veto_reason: str = ""
    reasoning: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BacktestDecisionRunner:
    """Safe DecisionEngine runner for replay mode.

    Default behavior is disabled. This is deliberate.

    Why:
    - The live DecisionEngine imports src.core.database and schedules persistence.
    - Backtest should not touch runtime DB or write WatchZone/PositioningBiasLog.
    - We first need a safe/no-persist DecisionEngine path before enabling it fully.

    This class therefore provides:
    - Disabled mode: returns clear placeholder decisions.
    - Optional experimental mode: attempts to import and execute DecisionEngine,
      catches failures, and never crashes the backtest pipeline.
    """

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = bool(enabled)

    def evaluate(
        self,
        *,
        snapshot: HunterReplaySnapshot,
        bundle: ReplayAdapterBundle,
    ) -> BacktestDecision:
        if not self.enabled:
            return BacktestDecision(
                timestamp=snapshot.timestamp,
                symbol=snapshot.symbol,
                engine_status="DISABLED",
                direction="WAIT",
                score=0.0,
                raw_score=0.0,
                size_factor=0.0,
                reasoning=[
                    "DecisionEngine replay execution is disabled.",
                    "Decision-compatible inputs were built successfully.",
                    "Enable only after DB persistence side effects are isolated.",
                ],
            )

        try:
            # Lazy import on purpose. Do not import live strategy modules at
            # backtest package import time.
            from src.layers.decision_engine import DecisionEngine  # noqa: WPS433
        except Exception as e:
            return BacktestDecision(
                timestamp=snapshot.timestamp,
                symbol=snapshot.symbol,
                engine_status="IMPORT_FAILED",
                direction="WAIT",
                error=f"{type(e).__name__}: {e}",
                reasoning=[
                    "Could not import live DecisionEngine.",
                    "This usually means environment/DB dependencies are not available.",
                    "Backtest pipeline remains safe and no live DB writes occurred.",
                ],
            )

        try:
            engine = DecisionEngine()
            result = engine.evaluate(
                snap=bundle.snap,
                lmap=bundle.lmap,
                pos=bundle.pos,
                ctx=bundle.ctx,
                regime=bundle.regime,
                oi_change_4h=bundle.oi_change_4h,
            )

            return BacktestDecision(
                timestamp=snapshot.timestamp,
                symbol=snapshot.symbol,
                engine_status="EXECUTED",
                direction=getattr(result, "direction", "WAIT"),
                score=float(getattr(result, "score", 0.0) or 0.0),
                raw_score=float(getattr(result, "raw_score", 0.0) or 0.0),
                size_factor=float(getattr(result, "size_factor", 0.0) or 0.0),
                is_watch_zone=bool(getattr(result, "is_watch_zone", False)),
                veto_reason=str(getattr(result, "veto_reason", "") or ""),
                reasoning=list(getattr(result, "reasoning", []) or []),
                components=dict(getattr(result, "components", {}) or {}),
            )

        except Exception as e:
            return BacktestDecision(
                timestamp=snapshot.timestamp,
                symbol=snapshot.symbol,
                engine_status="EXECUTION_FAILED",
                direction="WAIT",
                error=f"{type(e).__name__}: {e}",
                reasoning=[
                    "DecisionEngine execution failed during replay.",
                    "No trade execution occurred.",
                    "Inspect adapter compatibility before enabling strategy replay.",
                ],
            )
