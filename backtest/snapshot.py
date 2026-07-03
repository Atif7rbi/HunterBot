from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from backtest.replay import ReplayPoint


@dataclass(slots=True)
class HunterReplaySnapshot:
    """Backtest-side snapshot bridge.

    This is NOT the live exchange FullSnapshot yet. It is a stable replay
    snapshot that carries the Hunter inputs we need before connecting the real
    DecisionEngine.

    Purpose:
    ReplayPoint -> HunterReplaySnapshot -> future adapter -> Hunter logic

    It does not touch live trading, DB, or paper execution.
    """

    timestamp: str
    symbol: str
    price: float

    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0

    funding_rate: float | None = None
    open_interest: float | None = None
    open_interest_usd: float | None = None
    oi_change_4h_pct: float | None = None

    long_short_ratio_global: float | None = None
    long_short_ratio_top: float | None = None

    taker_buy_volume: float | None = None
    taker_sell_volume: float | None = None

    liquidity_zones_above: list[dict[str, Any]] = field(default_factory=list)
    liquidity_zones_below: list[dict[str, Any]] = field(default_factory=list)

    replay_index: int = 0
    replay_source: str = "unknown"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_candle(self) -> bool:
        return any(v > 0 for v in (self.open, self.high, self.low, self.close))

    @property
    def has_funding(self) -> bool:
        return self.funding_rate is not None

    @property
    def has_oi(self) -> bool:
        return self.open_interest is not None or self.open_interest_usd is not None

    @property
    def has_ls(self) -> bool:
        return self.long_short_ratio_global is not None or self.long_short_ratio_top is not None

    @property
    def has_taker_flow(self) -> bool:
        return self.taker_buy_volume is not None or self.taker_sell_volume is not None

    @property
    def has_liquidity(self) -> bool:
        return bool(self.liquidity_zones_above or self.liquidity_zones_below)

    @property
    def hunter_minimum_ready(self) -> bool:
        """Minimum useful context for Hunter strategy testing.

        Liquidity/taker flow can be optional for early replay, but candle,
        funding, LS, and OI are core context.
        """
        return self.has_candle and self.has_funding and self.has_ls and self.has_oi

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HunterSnapshotBuilder:
    """Converts ReplayPoint objects into HunterReplaySnapshot objects.

    This builder is intentionally separated from engine.py so future work can
    evolve the mapping without touching the replay loop or UI.
    """

    def from_replay_point(self, point: ReplayPoint) -> HunterReplaySnapshot:
        candle = point.candle
        ctx = point.context

        return HunterReplaySnapshot(
            timestamp=point.timestamp,
            symbol=point.symbol,
            price=float(point.price or 0.0),

            open=float(candle.open if candle else 0.0),
            high=float(candle.high if candle else 0.0),
            low=float(candle.low if candle else 0.0),
            close=float(candle.close if candle else point.price or 0.0),
            volume=float(candle.volume if candle else 0.0),

            funding_rate=ctx.funding_rate,
            open_interest=ctx.open_interest,
            open_interest_usd=ctx.open_interest_usd,
            oi_change_4h_pct=ctx.oi_change_4h_pct,

            long_short_ratio_global=ctx.long_short_ratio_global,
            long_short_ratio_top=ctx.long_short_ratio_top,

            taker_buy_volume=ctx.taker_buy_volume,
            taker_sell_volume=ctx.taker_sell_volume,

            liquidity_zones_above=list(ctx.liquidity_zones_above or []),
            liquidity_zones_below=list(ctx.liquidity_zones_below or []),

            replay_index=point.index,
            replay_source=point.source,
            raw={
                "replay_point": point.to_dict(),
            },
        )


def build_hunter_snapshot(point: ReplayPoint) -> HunterReplaySnapshot:
    return HunterSnapshotBuilder().from_replay_point(point)
