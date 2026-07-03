from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from backtest.adapters import ReplayAdapterBundle
from backtest.snapshot import HunterReplaySnapshot


@dataclass(slots=True)
class BacktestStrategySignal:
    """Backtest-only strategy signal.

    This is NOT a trade and does not touch live Hunter Bot execution.

    Purpose:
    - Give replay runs a first strategy-level signal layer.
    - Validate that Funding + LS + OI context can produce LONG/SHORT/WATCH.
    - Keep execution simulation disabled until the signal layer is trusted.
    """

    timestamp: str
    symbol: str
    signal: str = "WAIT"  # WAIT / WATCH / LONG / SHORT
    score: float = 0.0
    direction: str = "WAIT"
    reasons: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_actionable(self) -> bool:
        return self.signal in ("LONG", "SHORT")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BacktestStrategyRunner:
    """Initial Hunter-style replay signal runner.

    This is a backtest-only first pass, not the final live DecisionEngine.

    v1 logic:
    SHORT candidate:
      - Positive funding
      - Long crowding
      - OI rising

    LONG candidate:
      - Negative funding
      - Short crowding
      - OI rising

    Outputs:
      - LONG / SHORT when score >= signal_threshold
      - WATCH when score >= watch_threshold
      - WAIT otherwise

    No DB writes. No trades. No execution.
    """

    def __init__(
        self,
        *,
        watch_threshold: float = 55.0,
        signal_threshold: float = 70.0,
    ) -> None:
        self.watch_threshold = float(watch_threshold)
        self.signal_threshold = float(signal_threshold)

    def evaluate(
        self,
        *,
        snapshot: HunterReplaySnapshot,
        bundle: ReplayAdapterBundle,
    ) -> BacktestStrategySignal:
        funding = snapshot.funding_rate
        ls_ratio = snapshot.long_short_ratio_top or snapshot.long_short_ratio_global
        oi_change = snapshot.oi_change_4h_pct

        reasons: list[str] = []
        components: dict[str, float] = {}

        if funding is None:
            reasons.append("Missing funding.")
        if ls_ratio is None:
            reasons.append("Missing long/short ratio.")
        if oi_change is None:
            reasons.append("Missing OI change.")

        if funding is None or ls_ratio is None or oi_change is None:
            return BacktestStrategySignal(
                timestamp=snapshot.timestamp,
                symbol=snapshot.symbol,
                signal="WAIT",
                direction="WAIT",
                score=0.0,
                reasons=reasons,
                metadata={
                    "ready": False,
                    "adapter_ready": bundle.ready_for_decision_engine,
                },
            )

        funding = float(funding)
        ls_ratio = float(ls_ratio)
        oi_change = float(oi_change)

        # Component scores are intentionally simple and explainable.
        funding_score = min(abs(funding) / 0.001, 1.0) * 100.0
        ls_score = min(abs(ls_ratio - 1.0) / 1.0, 1.0) * 100.0
        oi_score = min(max(oi_change, 0.0) / 0.05, 1.0) * 100.0

        components["funding_pressure"] = funding_score
        components["ls_crowding"] = ls_score
        components["oi_expansion"] = oi_score

        score = (
            funding_score * 0.30
            + ls_score * 0.45
            + oi_score * 0.25
        )

        direction = "WAIT"

        if funding > 0 and ls_ratio > 1.15 and oi_change > 0:
            direction = "SHORT"
            reasons.extend(
                [
                    "Positive funding.",
                    "Long crowding detected.",
                    "OI expansion supports long-trap pressure.",
                ]
            )

        elif funding < 0 and ls_ratio < 0.85 and oi_change > 0:
            direction = "LONG"
            reasons.extend(
                [
                    "Negative funding.",
                    "Short crowding detected.",
                    "OI expansion supports short-trap pressure.",
                ]
            )

        else:
            reasons.append("Funding/LS/OI are not aligned enough for a Hunter replay signal.")

        signal = "WAIT"
        if direction in ("LONG", "SHORT") and score >= self.signal_threshold:
            signal = direction
            reasons.append(f"Signal accepted: score {score:.1f} >= {self.signal_threshold:.1f}.")
        elif direction in ("LONG", "SHORT") and score >= self.watch_threshold:
            signal = "WATCH"
            reasons.append(f"Watch only: score {score:.1f} >= {self.watch_threshold:.1f}.")
        else:
            reasons.append(f"Wait: score {score:.1f} below signal/watch requirements.")

        return BacktestStrategySignal(
            timestamp=snapshot.timestamp,
            symbol=snapshot.symbol,
            signal=signal,
            direction=direction,
            score=score,
            reasons=reasons,
            components=components,
            metadata={
                "ready": True,
                "adapter_ready": bundle.ready_for_decision_engine,
                "funding": funding,
                "ls_ratio": ls_ratio,
                "oi_change_4h_pct": oi_change,
                "watch_threshold": self.watch_threshold,
                "signal_threshold": self.signal_threshold,
            },
        )
