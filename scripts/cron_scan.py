"""Cron runner: execute one HunterBot scan cycle.

Use from cPanel cron, for example:
cd /home/USER/HunterBot && /home/USER/virtualenv/HunterBot/3.11/bin/python scripts/cron_scan.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from src.core.database import init_db  # noqa: E402
from src.core.logger import logger  # noqa: E402
from src.main import LiquidityHunterBot  # noqa: E402


async def main() -> None:
    os.environ.setdefault("HUNTER_RUNTIME_MODE", "cron_scan")
    await init_db()
    bot = LiquidityHunterBot()
    result = await bot.run_cycle()
    logger.info(f"[cron_scan] result={result}")


if __name__ == "__main__":
    asyncio.run(main())
