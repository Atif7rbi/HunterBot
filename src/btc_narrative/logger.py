"""Dedicated logger for BTC Narrative Strategy.

Writes only BTC Narrative messages to logs/btc_narrative.log.
This logger is intentionally separate from Hunter Original / bot log.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "btc_narrative"
LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "btc_narrative.log"


def _build_logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    return logger


btc_logger = _build_logger()


__all__ = ["btc_logger", "LOG_FILE", "LOGGER_NAME"]
