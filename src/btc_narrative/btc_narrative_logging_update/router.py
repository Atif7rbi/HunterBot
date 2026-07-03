"""Router for BTC Narrative Strategy.

The router is where BTC Narrative can later be connected to TradeGenerator/PaperExecutor.
For the first phase it runs in dry-run / analysis-only mode.
"""
from __future__ import annotations

from .config import BtcNarrativeConfig, DEFAULT_CONFIG
from .engine import BtcNarrativeEngine
from .logger import btc_logger as logger
from .models import BtcNarrativeCycleResult


class BtcNarrativeRouter:
    def __init__(self, data_collector, cfg: BtcNarrativeConfig = DEFAULT_CONFIG) -> None:
        self.cfg = cfg
        self.engine = BtcNarrativeEngine(data_collector=data_collector, cfg=cfg)
        self.last_result: BtcNarrativeCycleResult | None = None
        logger.info(
            "Router initialized: enabled=%s dry_run=%s strategy=%s",
            cfg.enabled,
            cfg.dry_run,
            cfg.strategy_name,
        )

    async def run_cycle(self) -> BtcNarrativeCycleResult:
        logger.info("Router cycle requested: enabled=%s dry_run=%s", self.cfg.enabled, self.cfg.dry_run)

        if not self.cfg.enabled:
            logger.info("BTC Narrative disabled; skipping cycle")
            result = BtcNarrativeCycleResult(ok=True, signal=None, candidates=[])
            self.last_result = result
            return result

        result = await self.engine.run_analysis_only()
        self.last_result = result

        if result.signal is None:
            logger.info("Router cycle finished without signal: ok=%s errors=%s", result.ok, result.errors)
            return result

        logger.info(
            "Router cycle result: ok=%s state=%s direction=%s score=%.1f candidates=%d dry_run=%s",
            result.ok,
            result.signal.state.value,
            result.signal.direction.value,
            result.signal.score,
            len(result.candidates),
            self.cfg.dry_run,
        )

        if self.cfg.dry_run:
            logger.info("BTC Narrative dry-run only; no trades will be submitted")
        else:
            logger.warning("BTC Narrative live routing is not implemented yet; no trades submitted")

        return result
