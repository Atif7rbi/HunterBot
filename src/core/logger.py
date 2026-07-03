"""Centralized logger using loguru. Writes to logs/bot.log + stderr."""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from src.core.config import PROJECT_ROOT, settings

LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)


def setup_logger() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.env.log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
        "<cyan>{name}:{function}:{line}</cyan> - <level>{message}</level>",
    )
    logger.add(
        LOG_DIR / "bot.log",
        level=settings.env.log_level,
        rotation="50 MB",
        retention="14 days",
        compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    )


setup_logger()

__all__ = ["logger"]
