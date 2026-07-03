"""Layer 2 — Data Collector.

For each shortlisted symbol, pull detailed market data:
  - OHLCV at 15m / 1h / 4h
  - Funding rate, OI, L/S ratios (global + top traders)
  - Taker buy/sell volume
  - Order book depth

Stored in MarketSnapshot with the raw_data JSON column for full traceability.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from src.core.database import AsyncSessionLocal, MarketSnapshot
from src.core.logger import logger
from src.exchange.binance_client import BinanceFuturesClient
from src.exchange.data_fetcher import FullSnapshot, fetch_full_snapshot


class DataCollector:
    async def collect(self, symbols: list[str]) -> dict[str, FullSnapshot]:
        """Pulls detailed snapshots for all symbols in parallel (throttled)."""
        if not symbols:
            return {}

        async with BinanceFuturesClient() as client:
            sem = asyncio.Semaphore(8)

            async def bounded(sym: str):
                async with sem:
                    return await fetch_full_snapshot(client, sym)

            results = await asyncio.gather(*[bounded(s) for s in symbols])

        out: dict[str, FullSnapshot] = {}
        for sym, snap in zip(symbols, results):
            if snap is not None:
                out[sym] = snap

        logger.info(f"DataCollector: {len(out)}/{len(symbols)} full snapshots fetched")
        await self._persist(out)
        return out

    async def _persist(self, snapshots: dict[str, FullSnapshot]) -> None:
        async with AsyncSessionLocal() as session:
            for sym, s in snapshots.items():
                row = MarketSnapshot(
                    symbol=sym,
                    timestamp=s.timestamp,
                    price=s.price,
                    funding_rate=s.funding_rate,
                    open_interest=s.open_interest,
                    open_interest_usd=s.open_interest_usd,
                    long_short_ratio_global=s.ls_ratio_global,
                    long_short_ratio_top=s.ls_ratio_top,
                    taker_buy_volume=s.taker_buy_volume,
                    taker_sell_volume=s.taker_sell_volume,
                )
                session.add(row)
            await session.commit()
