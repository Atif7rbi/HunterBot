from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from backtest.adapters import ReplayAdapterBundle, build_decision_inputs
from backtest.config import (
    DEFAULT_DAYS,
    DEFAULT_FEE_PCT,
    DEFAULT_INITIAL_CAPITAL_USD,
    DEFAULT_SYMBOL,
    DEFAULT_TIMEFRAME,
)
from backtest.decision_runner import BacktestDecision, BacktestDecisionRunner
from backtest.metrics import calculate_metrics
from backtest.models import BacktestRun, BacktestTrade
from backtest.replay import MarketReplay, ReplayPoint, build_placeholder_equity_curve
from backtest.snapshot import HunterReplaySnapshot, build_hunter_snapshot
from backtest.storage import save_run
from backtest.strategy_runner import BacktestStrategyRunner, BacktestStrategySignal


@dataclass(slots=True)
class StrategyEvent:
    timestamp: str
    symbol: str
    event_type: str
    direction: str = ""
    score: float = 0.0
    price: float = 0.0
    reason: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ReplayDiagnostics:
    total_points: int = 0
    candle_points: int = 0
    funding_points: int = 0
    open_interest_points: int = 0
    open_interest_usd_points: int = 0
    oi_change_points: int = 0
    ls_global_points: int = 0
    ls_top_points: int = 0
    taker_flow_points: int = 0
    liquidity_points: int = 0
    price_points: int = 0
    hunter_snapshot_points: int = 0
    hunter_minimum_ready_points: int = 0
    decision_input_points: int = 0
    decision_ready_points: int = 0
    adapter_warning_points: int = 0
    decision_executed_points: int = 0
    decision_disabled_points: int = 0
    decision_failed_points: int = 0
    decision_signal_points: int = 0
    strategy_signal_points: int = 0
    strategy_watch_points: int = 0
    strategy_wait_points: int = 0
    strategy_long_points: int = 0
    strategy_short_points: int = 0
    first_timestamp: str | None = None
    last_timestamp: str | None = None

    @property
    def ls_points(self) -> int:
        return max(self.ls_global_points, self.ls_top_points)

    @property
    def oi_points(self) -> int:
        return max(self.open_interest_points, self.open_interest_usd_points)

    def coverage_pct(self, value: int) -> float:
        if self.total_points <= 0:
            return 0.0
        return (value / self.total_points) * 100.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_points": self.total_points,
            "candle_points": self.candle_points,
            "funding_points": self.funding_points,
            "open_interest_points": self.open_interest_points,
            "open_interest_usd_points": self.open_interest_usd_points,
            "oi_change_points": self.oi_change_points,
            "ls_global_points": self.ls_global_points,
            "ls_top_points": self.ls_top_points,
            "ls_points": self.ls_points,
            "taker_flow_points": self.taker_flow_points,
            "liquidity_points": self.liquidity_points,
            "price_points": self.price_points,
            "hunter_snapshot_points": self.hunter_snapshot_points,
            "hunter_minimum_ready_points": self.hunter_minimum_ready_points,
            "decision_input_points": self.decision_input_points,
            "decision_ready_points": self.decision_ready_points,
            "adapter_warning_points": self.adapter_warning_points,
            "decision_executed_points": self.decision_executed_points,
            "decision_disabled_points": self.decision_disabled_points,
            "decision_failed_points": self.decision_failed_points,
            "decision_signal_points": self.decision_signal_points,
            "strategy_signal_points": self.strategy_signal_points,
            "strategy_watch_points": self.strategy_watch_points,
            "strategy_wait_points": self.strategy_wait_points,
            "strategy_long_points": self.strategy_long_points,
            "strategy_short_points": self.strategy_short_points,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "coverage": {
                "candles_pct": self.coverage_pct(self.candle_points),
                "funding_pct": self.coverage_pct(self.funding_points),
                "oi_pct": self.coverage_pct(self.oi_points),
                "oi_change_pct": self.coverage_pct(self.oi_change_points),
                "ls_pct": self.coverage_pct(self.ls_points),
                "taker_flow_pct": self.coverage_pct(self.taker_flow_points),
                "liquidity_pct": self.coverage_pct(self.liquidity_points),
                "price_pct": self.coverage_pct(self.price_points),
                "hunter_snapshot_pct": self.coverage_pct(self.hunter_snapshot_points),
                "hunter_minimum_ready_pct": self.coverage_pct(self.hunter_minimum_ready_points),
                "decision_input_pct": self.coverage_pct(self.decision_input_points),
                "decision_ready_pct": self.coverage_pct(self.decision_ready_points),
                "decision_executed_pct": self.coverage_pct(self.decision_executed_points),
                "decision_signal_pct": self.coverage_pct(self.decision_signal_points),
                "strategy_signal_pct": self.coverage_pct(self.strategy_signal_points),
                "strategy_watch_pct": self.coverage_pct(self.strategy_watch_points),
                "strategy_wait_pct": self.coverage_pct(self.strategy_wait_points),
            },
            "hunter_ready": self.is_hunter_ready(),
            "decision_bridge_ready": self.is_decision_bridge_ready(),
            "strategy_runner_ready": self.strategy_signal_points > 0 or self.strategy_watch_points > 0 or self.strategy_wait_points > 0,
            "warnings": self.warnings(),
        }

    def is_hunter_ready(self) -> bool:
        if self.total_points <= 0:
            return False
        return (
            self.candle_points > 0
            and self.funding_points > 0
            and self.ls_points > 0
            and self.oi_points > 0
            and self.hunter_snapshot_points > 0
        )

    def is_decision_bridge_ready(self) -> bool:
        if self.total_points <= 0:
            return False
        return self.decision_ready_points > 0

    def warnings(self) -> list[str]:
        warnings: list[str] = []
        if self.total_points <= 0:
            return ["No replay points loaded."]

        if self.candle_points == 0:
            warnings.append("No candle data found.")
        if self.funding_points == 0:
            warnings.append("No funding data found.")
        if self.ls_points == 0:
            warnings.append("No long/short ratio data found.")
        if self.oi_points == 0:
            warnings.append("No open interest data found.")
        if self.hunter_snapshot_points == 0:
            warnings.append("No Hunter replay snapshots were built.")
        if self.decision_input_points == 0:
            warnings.append("No DecisionEngine adapter inputs were built.")
        if self.decision_ready_points == 0:
            warnings.append("No snapshots are ready for DecisionEngine evaluation.")
        if self.decision_disabled_points > 0:
            warnings.append("DecisionEngine replay execution is disabled.")
        if self.decision_failed_points > 0:
            warnings.append(f"DecisionEngine failed on {self.decision_failed_points} replay point(s).")
        if self.liquidity_points == 0:
            warnings.append("No liquidity zone data found; liquidity-aware replay will be limited.")
        if self.taker_flow_points == 0:
            warnings.append("No taker flow data found; CVD/divergence replay will be limited.")
        if self.adapter_warning_points > 0:
            warnings.append(f"Adapter warnings present on {self.adapter_warning_points} replay point(s).")

        return warnings


class BacktestEngine:
    """Backtest engine v7 with backtest-only StrategyRunner.

    Current:
    - Build replay snapshots and decision-compatible inputs.
    - Keep live DecisionEngine disabled by default.
    - Run a backtest-only Hunter-style signal layer for LONG/SHORT/WATCH visibility.
    - No trades and no DB writes.
    """

    def __init__(
        self,
        *,
        symbol: str = DEFAULT_SYMBOL,
        timeframe: str = DEFAULT_TIMEFRAME,
        days: int = DEFAULT_DAYS,
        initial_capital_usd: float = DEFAULT_INITIAL_CAPITAL_USD,
        fee_pct: float = DEFAULT_FEE_PCT,
        source_path: str | None = None,
        enable_decision_engine: bool = False,
        run_id: str | None = None,
    ) -> None:
        self.symbol = symbol.upper()
        self.timeframe = timeframe
        self.days = int(days)
        self.initial_capital_usd = float(initial_capital_usd)
        self.fee_pct = float(fee_pct)
        self.source_path = source_path
        self.enable_decision_engine = bool(enable_decision_engine)
        self.run_id = run_id or self._new_run_id()
        self.decision_runner = BacktestDecisionRunner(enabled=self.enable_decision_engine)
        self.strategy_runner = BacktestStrategyRunner()

        self.replay = MarketReplay(
            symbol=self.symbol,
            timeframe=self.timeframe,
            days=self.days,
            source_path=self.source_path,
        )

    @staticmethod
    def _new_run_id() -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return f"run_{stamp}_{uuid4().hex[:8]}"

    def load_data(self) -> list[ReplayPoint]:
        return self.replay.load()

    def build_hunter_snapshots(self, replay_points: list[ReplayPoint]) -> list[HunterReplaySnapshot]:
        return [build_hunter_snapshot(point) for point in replay_points]

    def build_decision_bundles(self, hunter_snapshots: list[HunterReplaySnapshot]) -> list[ReplayAdapterBundle]:
        return [build_decision_inputs(snapshot) for snapshot in hunter_snapshots]

    def run_decisions(
        self,
        hunter_snapshots: list[HunterReplaySnapshot],
        decision_bundles: list[ReplayAdapterBundle],
    ) -> list[BacktestDecision]:
        return [
            self.decision_runner.evaluate(snapshot=snapshot, bundle=bundle)
            for snapshot, bundle in zip(hunter_snapshots, decision_bundles)
        ]

    def run_strategy_signals(
        self,
        hunter_snapshots: list[HunterReplaySnapshot],
        decision_bundles: list[ReplayAdapterBundle],
    ) -> list[BacktestStrategySignal]:
        return [
            self.strategy_runner.evaluate(snapshot=snapshot, bundle=bundle)
            for snapshot, bundle in zip(hunter_snapshots, decision_bundles)
        ]

    def diagnose_replay(
        self,
        replay_points: list[ReplayPoint],
        hunter_snapshots: list[HunterReplaySnapshot],
        decision_bundles: list[ReplayAdapterBundle],
        decisions: list[BacktestDecision],
        strategy_signals: list[BacktestStrategySignal],
    ) -> ReplayDiagnostics:
        diagnostics = ReplayDiagnostics(
            total_points=len(replay_points),
            hunter_snapshot_points=len(hunter_snapshots),
            hunter_minimum_ready_points=sum(1 for s in hunter_snapshots if s.hunter_minimum_ready),
            decision_input_points=len(decision_bundles),
            decision_ready_points=sum(1 for b in decision_bundles if b.ready_for_decision_engine),
            adapter_warning_points=sum(1 for b in decision_bundles if b.warnings),
            decision_executed_points=sum(1 for d in decisions if d.engine_status == "EXECUTED"),
            decision_disabled_points=sum(1 for d in decisions if d.engine_status == "DISABLED"),
            decision_failed_points=sum(1 for d in decisions if d.engine_status in ("IMPORT_FAILED", "EXECUTION_FAILED")),
            decision_signal_points=sum(1 for d in decisions if d.direction in ("LONG", "SHORT")),
            strategy_signal_points=sum(1 for s in strategy_signals if s.signal in ("LONG", "SHORT")),
            strategy_watch_points=sum(1 for s in strategy_signals if s.signal == "WATCH"),
            strategy_wait_points=sum(1 for s in strategy_signals if s.signal == "WAIT"),
            strategy_long_points=sum(1 for s in strategy_signals if s.signal == "LONG"),
            strategy_short_points=sum(1 for s in strategy_signals if s.signal == "SHORT"),
        )

        if replay_points:
            diagnostics.first_timestamp = replay_points[0].timestamp
            diagnostics.last_timestamp = replay_points[-1].timestamp

        for point in replay_points:
            ctx = point.context

            if point.price and point.price > 0:
                diagnostics.price_points += 1

            if point.candle is not None and (
                point.candle.open > 0
                or point.candle.high > 0
                or point.candle.low > 0
                or point.candle.close > 0
            ):
                diagnostics.candle_points += 1

            if ctx.funding_rate is not None:
                diagnostics.funding_points += 1

            if ctx.open_interest is not None:
                diagnostics.open_interest_points += 1

            if ctx.open_interest_usd is not None:
                diagnostics.open_interest_usd_points += 1

            if ctx.oi_change_4h_pct is not None:
                diagnostics.oi_change_points += 1

            if ctx.long_short_ratio_global is not None:
                diagnostics.ls_global_points += 1

            if ctx.long_short_ratio_top is not None:
                diagnostics.ls_top_points += 1

            if ctx.taker_buy_volume is not None or ctx.taker_sell_volume is not None:
                diagnostics.taker_flow_points += 1

            if ctx.liquidity_zones_above or ctx.liquidity_zones_below:
                diagnostics.liquidity_points += 1

        return diagnostics

    def strategy_to_events(
        self,
        strategy_signals: list[BacktestStrategySignal],
        decisions: list[BacktestDecision],
        decision_bundles: list[ReplayAdapterBundle],
    ) -> list[StrategyEvent]:
        events: list[StrategyEvent] = []

        for signal, decision, bundle in zip(strategy_signals, decisions, decision_bundles):
            if signal.signal in ("LONG", "SHORT"):
                event_type = "STRATEGY_SIGNAL"
            elif signal.signal == "WATCH":
                event_type = "STRATEGY_WATCH"
            else:
                event_type = "STRATEGY_WAIT"

            events.append(
                StrategyEvent(
                    timestamp=signal.timestamp,
                    symbol=signal.symbol,
                    event_type=event_type,
                    direction=signal.direction,
                    score=signal.score,
                    price=0.0,
                    reason="BacktestStrategyRunner processed replay snapshot.",
                    payload={
                        "strategy_signal": signal.to_dict(),
                        "decision": decision.to_dict(),
                        "adapter_ready": bundle.ready_for_decision_engine,
                        "adapter_warnings": list(bundle.warnings),
                    },
                )
            )

        return events

    def simulate_execution(self, strategy_events: list[StrategyEvent]) -> list[BacktestTrade]:
        return []

    def build_equity_curve(
        self,
        *,
        trades: list[BacktestTrade],
        replay_points: list[ReplayPoint],
    ) -> list[dict[str, Any]]:
        if not trades:
            return build_placeholder_equity_curve(
                initial_capital_usd=self.initial_capital_usd,
                points=1,
            )

        return build_placeholder_equity_curve(
            initial_capital_usd=self.initial_capital_usd,
            points=max(1, len(trades)),
        )

    def build_run(
        self,
        *,
        replay_points: list[ReplayPoint],
        hunter_snapshots: list[HunterReplaySnapshot],
        decision_bundles: list[ReplayAdapterBundle],
        decisions: list[BacktestDecision],
        strategy_signals: list[BacktestStrategySignal],
        strategy_events: list[StrategyEvent],
        trades: list[BacktestTrade],
        equity_curve: list[dict[str, Any]],
        diagnostics: ReplayDiagnostics,
    ) -> BacktestRun:
        metrics = calculate_metrics(trades)
        engine_stage = "strategy_runner" if replay_points else "skeleton"

        return BacktestRun(
            run_id=self.run_id,
            symbol=self.symbol,
            timeframe=self.timeframe,
            days=self.days,
            created_at=datetime.now(timezone.utc).isoformat(),
            config={
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "days": self.days,
                "initial_capital_usd": self.initial_capital_usd,
                "fee_pct": self.fee_pct,
                "source_path": self.source_path,
                "engine_stage": engine_stage,
                "replay_points": len(replay_points),
                "hunter_snapshots": len(hunter_snapshots),
                "decision_inputs": len(decision_bundles),
                "decision_ready": sum(1 for b in decision_bundles if b.ready_for_decision_engine),
                "decision_results": len(decisions),
                "decision_executed": sum(1 for d in decisions if d.engine_status == "EXECUTED"),
                "decision_disabled": sum(1 for d in decisions if d.engine_status == "DISABLED"),
                "decision_failed": sum(1 for d in decisions if d.engine_status in ("IMPORT_FAILED", "EXECUTION_FAILED")),
                "decision_signals": sum(1 for d in decisions if d.direction in ("LONG", "SHORT")),
                "strategy_results": len(strategy_signals),
                "strategy_signals": sum(1 for s in strategy_signals if s.signal in ("LONG", "SHORT")),
                "strategy_watch": sum(1 for s in strategy_signals if s.signal == "WATCH"),
                "strategy_wait": sum(1 for s in strategy_signals if s.signal == "WAIT"),
                "strategy_long": sum(1 for s in strategy_signals if s.signal == "LONG"),
                "strategy_short": sum(1 for s in strategy_signals if s.signal == "SHORT"),
                "strategy_events": len(strategy_events),
                "replay_diagnostics": diagnostics.to_dict(),
                "hunter_ready": diagnostics.is_hunter_ready(),
                "decision_bridge_ready": diagnostics.is_decision_bridge_ready(),
                "live_trading_affected": False,
                "strategy_connected": True,
                "execution_connected": False,
                "hunter_snapshot_bridge": True,
                "decision_input_bridge": True,
                "decision_runner_enabled": self.enable_decision_engine,
                "backtest_strategy_runner": True,
            },
            metrics=metrics,
            trades=trades,
            equity_curve=equity_curve,
            notes=(
                "Backtest engine v7. BacktestStrategyRunner emits LONG/SHORT/WATCH "
                "signals, but execution simulation is not connected yet."
            ),
        )

    def run(self, *, save: bool = True) -> BacktestRun:
        replay_points = self.load_data()
        hunter_snapshots = self.build_hunter_snapshots(replay_points)
        decision_bundles = self.build_decision_bundles(hunter_snapshots)
        decisions = self.run_decisions(hunter_snapshots, decision_bundles)
        strategy_signals = self.run_strategy_signals(hunter_snapshots, decision_bundles)
        diagnostics = self.diagnose_replay(
            replay_points,
            hunter_snapshots,
            decision_bundles,
            decisions,
            strategy_signals,
        )
        strategy_events = self.strategy_to_events(
            strategy_signals,
            decisions,
            decision_bundles,
        )
        trades = self.simulate_execution(strategy_events)
        equity_curve = self.build_equity_curve(
            trades=trades,
            replay_points=replay_points,
        )

        result = self.build_run(
            replay_points=replay_points,
            hunter_snapshots=hunter_snapshots,
            decision_bundles=decision_bundles,
            decisions=decisions,
            strategy_signals=strategy_signals,
            strategy_events=strategy_events,
            trades=trades,
            equity_curve=equity_curve,
            diagnostics=diagnostics,
        )

        if save:
            save_run(result)

        return result


def run_backtest(
    *,
    symbol: str = DEFAULT_SYMBOL,
    timeframe: str = DEFAULT_TIMEFRAME,
    days: int = DEFAULT_DAYS,
    source_path: str | None = None,
    enable_decision_engine: bool = False,
    save: bool = True,
) -> BacktestRun:
    engine = BacktestEngine(
        symbol=symbol,
        timeframe=timeframe,
        days=days,
        source_path=source_path,
        enable_decision_engine=enable_decision_engine,
    )
    return engine.run(save=save)
