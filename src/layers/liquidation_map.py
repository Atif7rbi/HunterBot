from __future__ import annotations

"""Layer — Liquidation Map Provider.

Purpose
-------
Fetch and normalize REAL liquidation / liquidity-cluster data for dashboard
and later strategy layers.

Important:
    This file does NOT estimate liquidation zones from OI.
    If no real source is configured, it returns an unavailable result.
    That is intentional: fake liquidation data is worse than no data.

Expected external/custom provider payload
-----------------------------------------
The provider is intentionally flexible. It accepts any of these shapes:

1)
{
  "symbol": "BTCUSDT",
  "current_price": 73400,
  "levels_above": [{"price": 74200, "usd": 1840000000}],
  "levels_below": [{"price": 71800, "usd": 2310000000}]
}

2)
{
  "data": {
    "symbol": "BTCUSDT",
    "price": 73400,
    "above": [{"price": 74200, "amount_usd": 1840000000}],
    "below": [{"price": 71800, "amount_usd": 2310000000}]
  }
}

3)
{
  "liquidations": {
    "above": [{"level": 74200, "value": 1840000000}],
    "below": [{"level": 71800, "value": 2310000000}]
  }
}

Config example
--------------
liquidation_map:
  enabled: true
  provider: custom
  endpoint_url: "https://your-provider.example/liquidations?symbol={symbol}"
  api_key: "..."
  timeout_sec: 8
  cache_ttl_sec: 60
  top_levels: 5

The endpoint_url may include {symbol}. If not, the symbol is sent as a
query parameter named "symbol".

No trading logic is changed by this file.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src.core.config import settings
from src.core.logger import logger


@dataclass(slots=True)
class LiquidationLevel:
    price: float
    usd: float
    side: str  # ABOVE or BELOW
    distance_pct: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "price": self.price,
            "usd": self.usd,
            "side": self.side,
            "distance_pct": self.distance_pct,
        }


@dataclass(slots=True)
class LiquidationMap:
    symbol: str
    current_price: float = 0.0
    levels_above: list[LiquidationLevel] = field(default_factory=list)
    levels_below: list[LiquidationLevel] = field(default_factory=list)
    source: str = "unavailable"
    fetched_at: datetime | None = None
    available: bool = False
    error: str | None = None

    @property
    def total_above_usd(self) -> float:
        return sum(float(x.usd or 0.0) for x in self.levels_above)

    @property
    def total_below_usd(self) -> float:
        return sum(float(x.usd or 0.0) for x in self.levels_below)

    @property
    def nearest_above(self) -> LiquidationLevel | None:
        if not self.levels_above:
            return None
        return min(self.levels_above, key=lambda x: abs(x.price - self.current_price))

    @property
    def nearest_below(self) -> LiquidationLevel | None:
        if not self.levels_below:
            return None
        return min(self.levels_below, key=lambda x: abs(x.price - self.current_price))

    @property
    def bias(self) -> str:
        above = self.total_above_usd
        below = self.total_below_usd

        if above <= 0 and below <= 0:
            return "UNKNOWN"

        if above > below * 1.25:
            return "ABOVE"
        if below > above * 1.25:
            return "BELOW"
        return "BALANCED"

    @property
    def density_score(self) -> float:
        """Display-only 0..100 density score.

        This scores how meaningful the liquidation map is by total liquidity
        relative to the nearest clusters. It is NOT a trade signal.
        """
        total = self.total_above_usd + self.total_below_usd
        if total <= 0:
            return 0.0

        nearest_total = 0.0
        if self.nearest_above:
            nearest_total += self.nearest_above.usd
        if self.nearest_below:
            nearest_total += self.nearest_below.usd

        score = (nearest_total / total) * 100.0
        return max(0.0, min(100.0, score))

    def as_decision_patch(self) -> dict[str, Any]:
        """Keys compatible with dashboard decision dictionaries."""
        nearest_above = self.nearest_above
        nearest_below = self.nearest_below

        return {
            "liquidation_map_available": self.available,
            "liquidation_source": self.source,
            "liquidity_above_usd": self.total_above_usd,
            "liquidity_below_usd": self.total_below_usd,
            "liquidity_bias": self.bias,
            "liquidity_density_score": self.density_score,
            "nearest_liquidity_above_price": nearest_above.price if nearest_above else None,
            "nearest_liquidity_above_usd": nearest_above.usd if nearest_above else None,
            "nearest_liquidity_above_distance_pct": nearest_above.distance_pct if nearest_above else None,
            "nearest_liquidity_below_price": nearest_below.price if nearest_below else None,
            "nearest_liquidity_below_usd": nearest_below.usd if nearest_below else None,
            "nearest_liquidity_below_distance_pct": nearest_below.distance_pct if nearest_below else None,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "current_price": self.current_price,
            "available": self.available,
            "source": self.source,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "error": self.error,
            "total_above_usd": self.total_above_usd,
            "total_below_usd": self.total_below_usd,
            "bias": self.bias,
            "density_score": self.density_score,
            "levels_above": [x.as_dict() for x in self.levels_above],
            "levels_below": [x.as_dict() for x in self.levels_below],
        }


@dataclass(slots=True)
class _CacheItem:
    value: LiquidationMap
    expires_at: datetime


def _cfg() -> dict[str, Any]:
    block = getattr(settings, "liquidation_map", None)
    if block is None:
        return {}

    if isinstance(block, dict):
        return block

    try:
        return dict(block)
    except Exception:
        return {
            k: getattr(block, k)
            for k in dir(block)
            if not k.startswith("_") and not callable(getattr(block, k))
        }


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _pick(d: dict[str, Any], keys: list[str], default: Any = None) -> Any:
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return default


def _unwrap_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}

    for key in ("data", "result", "payload"):
        inner = payload.get(key)
        if isinstance(inner, dict):
            payload = inner
            break

    liquidations = payload.get("liquidations")
    if isinstance(liquidations, dict):
        merged = dict(payload)
        merged.update(liquidations)
        return merged

    return payload


def _extract_levels(raw: Any, side: str, current_price: float, top_levels: int) -> list[LiquidationLevel]:
    if raw is None:
        return []

    if isinstance(raw, dict):
        # Some APIs return {"levels": [...]} or {"items": [...]}
        raw = raw.get("levels") or raw.get("items") or raw.get("data") or []

    if not isinstance(raw, list):
        return []

    levels: list[LiquidationLevel] = []

    for item in raw:
        if not isinstance(item, dict):
            continue

        price = _to_float(
            _pick(item, ["price", "level", "liq_price", "liquidation_price", "p"]),
            0.0,
        )
        usd = _to_float(
            _pick(item, ["usd", "amount_usd", "value_usd", "value", "notional", "size_usd", "liquidity"]),
            0.0,
        )

        if price <= 0 or usd <= 0:
            continue

        distance_pct = 0.0
        if current_price > 0:
            distance_pct = abs(price - current_price) / current_price * 100.0

        levels.append(
            LiquidationLevel(
                price=price,
                usd=usd,
                side=side,
                distance_pct=distance_pct,
            )
        )

    if side == "ABOVE":
        levels = [x for x in levels if current_price <= 0 or x.price >= current_price]
        levels.sort(key=lambda x: (x.price - current_price if current_price else x.price))
    else:
        levels = [x for x in levels if current_price <= 0 or x.price <= current_price]
        levels.sort(key=lambda x: (current_price - x.price if current_price else -x.price))

    return levels[: max(1, int(top_levels or 5))]


class LiquidationMapProvider:
    """Fetch normalized real liquidation maps.

    Supported provider mode:
      - custom HTTP endpoint controlled by config.

    The class is intentionally conservative. It will never invent liquidation
    levels from OI or funding.
    """

    def __init__(self) -> None:
        self._cache: dict[str, _CacheItem] = {}

    def _unavailable(self, symbol: str, reason: str) -> LiquidationMap:
        return LiquidationMap(
            symbol=symbol,
            source="unavailable",
            available=False,
            error=reason,
            fetched_at=datetime.now(UTC),
        )

    async def get(self, symbol: str, *, current_price: float | None = None) -> LiquidationMap:
        symbol = str(symbol or "").upper()
        cfg = _cfg()

        enabled = bool(cfg.get("enabled", False))
        if not enabled:
            return self._unavailable(symbol, "liquidation_map disabled")

        provider = str(cfg.get("provider", "custom") or "custom").lower()
        if provider != "custom":
            return self._unavailable(symbol, f"unsupported liquidation_map provider: {provider}")

        endpoint_url = str(cfg.get("endpoint_url") or "").strip()
        if not endpoint_url:
            return self._unavailable(symbol, "liquidation_map.endpoint_url missing")

        ttl = int(cfg.get("cache_ttl_sec", 60) or 60)
        now = datetime.now(UTC)

        cached = self._cache.get(symbol)
        if cached and cached.expires_at > now:
            return cached.value

        try:
            result = await self._fetch_custom(
                symbol=symbol,
                endpoint_url=endpoint_url,
                api_key=str(cfg.get("api_key") or ""),
                timeout_sec=float(cfg.get("timeout_sec", 8) or 8),
                top_levels=int(cfg.get("top_levels", 5) or 5),
                current_price=_to_float(current_price, 0.0),
            )

            self._cache[symbol] = _CacheItem(
                value=result,
                expires_at=now + timedelta(seconds=max(5, ttl)),
            )
            return result

        except Exception as e:
            logger.warning(f"liquidation map fetch failed for {symbol}: {e}")
            return self._unavailable(symbol, str(e))

    async def _fetch_custom(
        self,
        *,
        symbol: str,
        endpoint_url: str,
        api_key: str,
        timeout_sec: float,
        top_levels: int,
        current_price: float,
    ) -> LiquidationMap:
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["X-API-KEY"] = api_key

        if "{symbol}" in endpoint_url:
            url = endpoint_url.format(symbol=symbol)
            params = None
        else:
            url = endpoint_url
            params = {"symbol": symbol}

        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            payload = resp.json()

        data = _unwrap_payload(payload)

        price = _to_float(
            _pick(data, ["current_price", "price", "mark_price", "last_price"]),
            current_price,
        )

        above_raw = (
            data.get("levels_above")
            or data.get("above")
            or data.get("short_liquidations_above")
            or data.get("shorts_above")
            or data.get("resistance_liquidity")
        )
        below_raw = (
            data.get("levels_below")
            or data.get("below")
            or data.get("long_liquidations_below")
            or data.get("longs_below")
            or data.get("support_liquidity")
        )

        levels_above = _extract_levels(above_raw, "ABOVE", price, top_levels)
        levels_below = _extract_levels(below_raw, "BELOW", price, top_levels)

        available = bool(levels_above or levels_below)

        return LiquidationMap(
            symbol=symbol,
            current_price=price,
            levels_above=levels_above,
            levels_below=levels_below,
            source="custom",
            fetched_at=datetime.now(UTC),
            available=available,
            error=None if available else "provider returned no levels",
        )


# Shared instance for app-wide reuse.
liquidation_map_provider = LiquidationMapProvider()
