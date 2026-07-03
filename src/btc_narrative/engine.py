"""BTC Narrative Strategy engine.

Runs independently from Hunter Original:
- Collect BTC driver snapshot.
- Analyze BTC narrative using BTC LS_POSIT/LS_RATIO/LS_ACCOUNT only.
- Collect/log additional BTC context for future research.
- Select ETH/SOL/DOGE/XRP followers in dry-run mode.
- Does not submit trades by itself.
"""
from __future__ import annotations

from .analyzer import BtcNarrativeAnalyzer
from .config import BtcNarrativeConfig, DEFAULT_CONFIG
from .follower_selector import FollowerSelector
from .logger import btc_logger as logger
from .models import BtcNarrativeCycleResult


class BtcNarrativeEngine:
    def __init__(self, data_collector, cfg: BtcNarrativeConfig = DEFAULT_CONFIG) -> None:
        self.data_collector = data_collector
        self.cfg = cfg
        self.analyzer = BtcNarrativeAnalyzer(cfg)
        self.selector = FollowerSelector(cfg)
        self.last_result: BtcNarrativeCycleResult | None = None
        logger.info(
            "Engine initialized: enabled=%s dry_run=%s strategy=%s driver=%s followers=%s",
            cfg.enabled,
            cfg.dry_run,
            cfg.strategy_name,
            cfg.driver_symbol,
            list(cfg.follower_symbols),
        )

    async def run_analysis_only(self, *, liquidity_map=None) -> BtcNarrativeCycleResult:
        symbols = [self.cfg.driver_symbol, *list(self.cfg.follower_symbols)]
        errors: list[str] = []

        logger.info("BTC Narrative analysis cycle started: symbols=%s", symbols)

        try:
            snapshots = await self.data_collector.collect(symbols)
            logger.info(
                "Data collected: requested=%d received=%d symbols=%s",
                len(symbols),
                len(snapshots),
                sorted(snapshots.keys()),
            )
        except Exception as exc:
            logger.exception("BTC Narrative collect failed: %s", exc)
            result = BtcNarrativeCycleResult(ok=False, signal=None, errors=[str(exc)])
            self.last_result = result
            return result

        btc_snapshot = snapshots.get(self.cfg.driver_symbol)
        if btc_snapshot is None:
            msg = f"missing BTC driver snapshot: {self.cfg.driver_symbol}"
            logger.warning(msg)
            result = BtcNarrativeCycleResult(ok=False, signal=None, errors=[msg])
            self.last_result = result
            return result

        signal = self.analyzer.analyze(btc_snapshot, liquidity_map=liquidity_map)
        followers = {
            s: snapshots.get(s)
            for s in self.cfg.follower_symbols
            if snapshots.get(s) is not None
        }
        candidates = self.selector.select(signal, followers)

        result = BtcNarrativeCycleResult(ok=True, signal=signal, candidates=candidates, errors=errors)
        self.last_result = result

        logger.info(
            "BTC Narrative analysis cycle finished: ok=%s state=%s direction=%s "
            "score=%.1f confidence=%.2f candidates=%d errors=%d",
            result.ok,
            signal.state.value,
            signal.direction.value,
            signal.score,
            signal.confidence,
            len(candidates),
            len(errors),
        )
        return result
