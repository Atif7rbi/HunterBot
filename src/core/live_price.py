from __future__ import annotations

import asyncio
from typing import Dict, List

from sqlalchemy import select

from src.core.database import AsyncSessionLocal, Trade, TradeStatus
from src.core.logger import logger

# نستخدم requests مباشرة بدون أي BinanceClient
import requests


class LivePriceTracker:

    def __init__(self, refresh_seconds: int = 60):
        self.refresh_seconds = refresh_seconds
        self.prices: Dict[str, float] = {}
        self.running = False

    async def get_open_symbols(self) -> List[str]:

        try:
            async with AsyncSessionLocal() as session:

                result = await session.execute(
                    select(Trade.symbol).where(
                        Trade.status.in_(
                            [
                                TradeStatus.PENDING.value,
                                TradeStatus.TRIGGERED.value,
                            ]
                        )
                    )
                )

                symbols = result.scalars().all()

            return list(set(symbols))

        except Exception as e:
            logger.exception(
                f"[LivePriceTracker] symbol read failed: {e}"
            )
            return []

    async def update_prices(self):

        symbols = await self.get_open_symbols()

        if not symbols:
            self.prices.clear()
            return

        try:

            url = (
                "https://fapi.binance.com/fapi/v1/ticker/price"
            )

            data = requests.get(
                url,
                timeout=10
            ).json()

            price_map = {
                x["symbol"]: float(x["price"])
                for x in data
            }

            for s in symbols:

                if s in price_map:
                    self.prices[s] = price_map[s]

        except Exception as e:

            logger.exception(
                f"[LivePriceTracker] update failed: {e}"
            )

    async def loop(self):

        if self.running:
            return

        self.running = True

        logger.info(
            "[LivePriceTracker] started"
        )

        while True:

            try:
                await self.update_prices()

            except Exception as e:

                logger.exception(
                    f"[LivePriceTracker] loop error: {e}"
                )

            await asyncio.sleep(
                self.refresh_seconds
            )

    def get_price(self, symbol):

        return self.prices.get(symbol)
