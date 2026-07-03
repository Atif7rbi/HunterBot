from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal


ReplaySourceType = Literal["empty", "jsonl", "json", "csv"]


@dataclass(slots=True)
class ReplayCandle:
    """OHLCV candle used by the replay engine."""

    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ReplayCandle":
        return cls(
            open=float(raw.get("open") or raw.get("o") or 0.0),
            high=float(raw.get("high") or raw.get("h") or 0.0),
            low=float(raw.get("low") or raw.get("l") or 0.0),
            close=float(raw.get("close") or raw.get("c") or raw.get("price") or 0.0),
            volume=float(raw.get("volume") or raw.get("v") or 0.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ReplayMarketContext:
    """Non-candle context needed by Hunter Bot."""

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

    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ReplayMarketContext":
        context = raw.get("context") if isinstance(raw.get("context"), dict) else raw

        return cls(
            funding_rate=_maybe_float(context.get("funding_rate")),
            open_interest=_maybe_float(context.get("open_interest")),
            open_interest_usd=_maybe_float(context.get("open_interest_usd")),
            oi_change_4h_pct=_maybe_float(context.get("oi_change_4h_pct")),
            long_short_ratio_global=_maybe_float(
                context.get("long_short_ratio_global")
                or context.get("ls_ratio_global")
                or context.get("long_short_ratio")
            ),
            long_short_ratio_top=_maybe_float(
                context.get("long_short_ratio_top")
                or context.get("ls_ratio_top")
            ),
            taker_buy_volume=_maybe_float(context.get("taker_buy_volume")),
            taker_sell_volume=_maybe_float(context.get("taker_sell_volume")),
            liquidity_zones_above=list(
                context.get("liquidity_zones_above")
                or context.get("zones_above")
                or []
            ),
            liquidity_zones_below=list(
                context.get("liquidity_zones_below")
                or context.get("zones_below")
                or []
            ),
            raw=dict(context),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ReplayPoint:
    """One chronological market step for the replay engine."""

    timestamp: str
    symbol: str
    price: float
    candle: ReplayCandle | None = None
    context: ReplayMarketContext = field(default_factory=ReplayMarketContext)
    source: str = "unknown"
    index: int = 0

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, Any],
        *,
        default_symbol: str = "UNKNOWN",
        index: int = 0,
        source: str = "dict",
    ) -> "ReplayPoint":
        symbol = str(raw.get("symbol") or default_symbol).upper()
        timestamp = _normalize_timestamp(
            raw.get("timestamp")
            or raw.get("time")
            or raw.get("open_time")
            or raw.get("datetime")
        )

        candle_raw = raw.get("candle") if isinstance(raw.get("candle"), dict) else raw
        candle = ReplayCandle.from_dict(candle_raw)

        price = _maybe_float(
            raw.get("price")
            or raw.get("close")
            or raw.get("c")
            or candle.close
        ) or 0.0

        return cls(
            timestamp=timestamp,
            symbol=symbol,
            price=price,
            candle=candle,
            context=ReplayMarketContext.from_dict(raw),
            source=source,
            index=index,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "symbol": self.symbol,
            "price": self.price,
            "candle": self.candle.to_dict() if self.candle else None,
            "context": self.context.to_dict(),
            "source": self.source,
            "index": self.index,
        }


class ReplayDataError(RuntimeError):
    pass


class ReplayDataSource:
    """Local-file replay source.

    Supported formats:
    - JSONL: one ReplayPoint-compatible JSON object per line
    - JSON : list of objects, or {"points": [...]}, {"data": [...]}, {"rows": [...]}
    - CSV  : timestamp,symbol,open,high,low,close,volume + optional context columns
    """

    def __init__(
        self,
        *,
        path: str | Path | None = None,
        source_type: ReplaySourceType | None = None,
        default_symbol: str = "UNKNOWN",
    ) -> None:
        self.path = Path(path) if path else None
        self.source_type = source_type or self._infer_type(self.path)
        self.default_symbol = default_symbol.upper()

    @staticmethod
    def _infer_type(path: Path | None) -> ReplaySourceType:
        if path is None:
            return "empty"
        suffix = path.suffix.lower()
        if suffix == ".jsonl":
            return "jsonl"
        if suffix == ".json":
            return "json"
        if suffix == ".csv":
            return "csv"
        raise ReplayDataError(f"Unsupported replay source extension: {suffix}")

    def load(self) -> list[ReplayPoint]:
        if self.source_type == "empty":
            return []

        if self.path is None:
            raise ReplayDataError("ReplayDataSource path is required for non-empty source.")

        if not self.path.exists():
            raise ReplayDataError(f"Replay source does not exist: {self.path}")

        if self.source_type == "jsonl":
            return self._load_jsonl(self.path)
        if self.source_type == "json":
            return self._load_json(self.path)
        if self.source_type == "csv":
            return self._load_csv(self.path)

        raise ReplayDataError(f"Unsupported replay source type: {self.source_type}")

    def _load_jsonl(self, path: Path) -> list[ReplayPoint]:
        points: list[ReplayPoint] = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise ReplayDataError(f"Invalid JSONL row {line_no}: expected object")
            points.append(
                ReplayPoint.from_dict(
                    raw,
                    default_symbol=self.default_symbol,
                    index=len(points),
                    source=str(path),
                )
            )
        return _sort_points(points)

    def _load_json(self, path: Path) -> list[ReplayPoint]:
        raw = json.loads(path.read_text(encoding="utf-8"))

        if isinstance(raw, dict):
            items = raw.get("points") or raw.get("data") or raw.get("rows") or []
        elif isinstance(raw, list):
            items = raw
        else:
            raise ReplayDataError("Invalid JSON replay file: expected list or object")

        if not isinstance(items, list):
            raise ReplayDataError("Invalid JSON replay file: points/data/rows must be a list")

        points = [
            ReplayPoint.from_dict(
                dict(item),
                default_symbol=self.default_symbol,
                index=i,
                source=str(path),
            )
            for i, item in enumerate(items)
            if isinstance(item, dict)
        ]
        return _sort_points(points)

    def _load_csv(self, path: Path) -> list[ReplayPoint]:
        points: list[ReplayPoint] = []
        with path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                points.append(
                    ReplayPoint.from_dict(
                        dict(row),
                        default_symbol=self.default_symbol,
                        index=len(points),
                        source=str(path),
                    )
                )
        return _sort_points(points)


class MarketReplay:
    """Replay adapter used by BacktestEngine.

    It loads local replay files when provided and never touches live trading
    or database state.
    """

    def __init__(
        self,
        *,
        symbol: str,
        timeframe: str,
        days: int,
        source_path: str | Path | None = None,
    ) -> None:
        self.symbol = symbol.upper()
        self.timeframe = timeframe
        self.days = int(days)
        self.source_path = Path(source_path) if source_path else None

    def load(self) -> list[ReplayPoint]:
        source = ReplayDataSource(
            path=self.source_path,
            default_symbol=self.symbol,
        )
        points = source.load()

        if self.symbol and self.symbol != "UNKNOWN":
            points = [p for p in points if p.symbol == self.symbol]

        return points

    def iter_points(self) -> Iterable[ReplayPoint]:
        yield from self.load()


def build_placeholder_equity_curve(
    *,
    initial_capital_usd: float,
    points: int = 1,
) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    return [
        {
            "timestamp": (now + timedelta(minutes=i)).isoformat(),
            "equity_usd": float(initial_capital_usd),
            "note": "placeholder",
        }
        for i in range(max(1, int(points)))
    ]


def _maybe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _normalize_timestamp(value: Any) -> str:
    if value is None or value == "":
        return datetime.now(timezone.utc).isoformat()

    if isinstance(value, (int, float)):
        v = float(value)
        if v > 10_000_000_000:
            return datetime.fromtimestamp(v / 1000, tz=timezone.utc).isoformat()
        return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()

    s = str(value).strip()
    if not s:
        return datetime.now(timezone.utc).isoformat()

    if s.replace(".", "", 1).isdigit():
        try:
            v = float(s)
            if v > 10_000_000_000:
                return datetime.fromtimestamp(v / 1000, tz=timezone.utc).isoformat()
            return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
        except Exception:
            pass

    return s


def _sort_points(points: list[ReplayPoint]) -> list[ReplayPoint]:
    ordered = sorted(points, key=lambda p: p.timestamp)
    for i, point in enumerate(ordered):
        point.index = i
    return ordered
