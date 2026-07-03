"""Binance Futures public API client.

Uses only public endpoints â€” no API keys required for paper trading.
Async via httpx for high throughput when scanning 200+ symbols.

Note: If geo-blocked on Binance, swap base_url to a mirror or use OKX fallback.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from src.core.config import settings
from src.core.logger import logger

DEFAULT_TIMEOUT = 15.0
MAX_RETRIES = 3


class BinanceFuturesClient:
    """Async client for Binance USDM-Futures public endpoints."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = base_url or settings.env.binance_futures_base_url
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "BinanceFuturesClient":
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=DEFAULT_TIMEOUT,
            headers={"User-Agent": "LiquidityHunter/1.0"},
        )
        return self

    async def __aexit__(self, *args) -> None:
        if self._client:
            await self._client.aclose()

    async def _get(self, path: str, params: Optional[dict] = None) -> Any:
        assert self._client is not None, "Use as async context manager"
        for attempt in range(MAX_RETRIES):
            try:
                resp = await self._client.get(path, params=params)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    wait = 2 ** attempt
                    logger.warning(f"Rate limit hit, sleeping {wait}s")
                    await asyncio.sleep(wait)
                    continue
                if e.response.status_code == 451:
                    logger.error("Geo-blocked. Consider OKX fallback.")
                    raise
                logger.error(f"HTTP {e.response.status_code} on {path}: {e.response.text[:200]}")
                if attempt == MAX_RETRIES - 1:
                    raise
            except httpx.RequestError as e:
                logger.warning(f"Request error on {path} attempt {attempt + 1}: {e}")
                if attempt == MAX_RETRIES - 1:
                    raise
                await asyncio.sleep(1)

    # ---------- Market Data ----------

    async def exchange_info(self) -> dict:
        """All trading rules / symbols on USDM Futures."""
        return await self._get("/fapi/v1/exchangeInfo")

    async def ticker_24h(self, symbol: Optional[str] = None) -> Any:
        """24h price + volume statistics. Pass no symbol = all symbols."""
        params = {"symbol": symbol} if symbol else None
        return await self._get("/fapi/v1/ticker/24hr", params=params)

    async def klines(self, symbol: str, interval: str, limit: int = 200) -> list:
        """OHLCV candles. Returns list of [open_time, o, h, l, c, v, close_time, ...]."""
        return await self._get(
            "/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )

    async def funding_rate(self, symbol: str, limit: int = 1) -> list:
        """Recent funding rate history."""
        return await self._get(
            "/fapi/v1/fundingRate",
            params={"symbol": symbol, "limit": limit},
        )

    async def premium_index(self, symbol: Optional[str] = None) -> Any:
        """Mark price + current funding rate. Pass no symbol = all."""
        params = {"symbol": symbol} if symbol else None
        return await self._get("/fapi/v1/premiumIndex", params=params)

    async def open_interest(self, symbol: str) -> dict:
        """Current open interest."""
        return await self._get("/fapi/v1/openInterest", params={"symbol": symbol})

    async def open_interest_hist(
        self, symbol: str, period: str = "5m", limit: int = 30
    ) -> list:
        """OI history. period: 5m/15m/30m/1h/2h/4h/6h/12h/1d."""
        return await self._get(
            "/futures/data/openInterestHist",
            params={"symbol": symbol, "period": period, "limit": limit},
        )

    async def long_short_ratio(
        self, symbol: str, period: str = "5m", limit: int = 30, scope: str = "global"
    ) -> list:
        """L/S ratio. scope: 'global' (all accounts) or 'top' (top traders by position)."""
        endpoint = {
            "global": "/futures/data/globalLongShortAccountRatio",
            "top": "/futures/data/topLongShortPositionRatio",
            "top_accounts": "/futures/data/topLongShortAccountRatio",
        }[scope]
        return await self._get(
            endpoint, params={"symbol": symbol, "period": period, "limit": limit}
        )

    async def taker_buy_sell_volume(
        self, symbol: str, period: str = "5m", limit: int = 30
    ) -> list:
        """Aggregated taker buy/sell volume â€” useful for aggression direction."""
        return await self._get(
            "/futures/data/takerlongshortRatio",
            params={"symbol": symbol, "period": period, "limit": limit},
        )

    async def order_book(self, symbol: str, limit: int = 100) -> dict:
        """Order book depth."""
        return await self._get(
            "/fapi/v1/depth", params={"symbol": symbol, "limit": limit}
        )

    # ---------- Helpers ----------

    async def all_perpetual_symbols(self) -> list[str]:
        """All active USDT-margined perpetual symbols."""
        info = await self.exchange_info()
        return [
            s["symbol"]
            for s in info.get("symbols", [])
            if s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
        ]
