"""Main Orchestrator — wires all layers together and runs the bot loop.

Two modes:
- `bot`      : run the live (paper) trading loop
- `backtest` : run backtest on a symbol
- `scan`     : single scan cycle, print results

Usage:
python -m src.main bot
python -m src.main backtest --symbol BTCUSDT --days 60
python -m src.main scan
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from typing import Optional

from src.alerts.telegram_bot import TelegramAlerter
from src.backtest.engine import BacktestEngine
from src.core.config import settings
from src.core.database import init_db
from src.core.logger import logger
from src.layers.context import ContextLayer
from src.layers.data_collector import DataCollector
from src.layers.decision_engine import DecisionEngine
from src.layers.liquidity_engine import LiquidityEngine
from src.layers.paper_executor import PaperExecutor
from src.layers.positioning import PositioningAnalyzer
from src.layers.regime_detector import RegimeDetector
from src.layers.scanner import Scanner
from src.layers.trade_generator import TradeGenerator
from src.layers.trigger_confirm import TriggerConfirmation
from src.learning.performance_analyzer import PerformanceAnalyzer
from src.learning.adaptive_weights import AdaptiveWeightsEngine
from src.learning.shadow_trade_tracker import ShadowTradeTracker


# Paper-trading price protection:
# Ensures PENDING/TRIGGERED trades outside scanner shortlist still receive prices.
# NOTE:
# LivePriceTracker lives under src/core/live_price.py, not src/live_price.py.
# If this import fails, open trades outside the scanner shortlist will not receive
# prices, and PaperExecutor.update_positions() will not manage TP/SL/trailing.
from src.core.live_price import LivePriceTracker



# Display-only BTC Narrative feed.
# These symbols are collected every cycle for Dashboard/Narrative rendering only.
# They are NOT added to the scanner shortlist and do NOT go through TradeGenerator here.
BTC_NARRATIVE_SYMBOLS = ["BTCUSDT", "SOLUSDT", "ETHUSDT", "DOGEUSDT", "XRPUSDT"]


class LiquidityHunterBot:
    def __init__(self) -> None:
        self.scanner = Scanner()
        self.data_collector = DataCollector()
        self.liquidity_engine = LiquidityEngine()
        self.positioning = PositioningAnalyzer()
        self.context = ContextLayer()
        self.regime = RegimeDetector()
        self.decision = DecisionEngine()
        self.trigger = TriggerConfirmation()
        self.trade_gen = TradeGenerator()
        self.executor = PaperExecutor()
        self.alerter = TelegramAlerter()
        self.live_price = LivePriceTracker()
        self.shadow_tracker = ShadowTradeTracker()


        self.last_decisions: dict[str, dict] = {}
        self.last_scan_results: list[dict] = []

        # Dashboard-only BTC Narrative feed.
        # Populated independently from Final Shortlist so BTC/followers never disappear
        # from ui/dash.html when they are filtered out of Hunter Original scanner.
        self.last_narrative_results: list[dict] = []
        self.last_narrative_decisions: dict[str, dict] = {}

        self.last_cycle_at: Optional[datetime] = None
        self.last_scan_diagnostics: dict = {}
        self._closed_since_adapt: int = 0


    def _snapshot_to_dashboard_row(self, snap) -> dict:
        """Convert a FullSnapshot into the same shape used by dashboard scans.

        This is display-only telemetry for BTC Narrative symbols. It does not
        affect scanner ranking, final shortlist, decision authority, or trading.
        """
        return {
            "symbol": snap.symbol,
            "price": snap.price,
            "volume_24h_usd": 0.0,  # FullSnapshot does not carry 24h quote volume.
            "open_interest_usd": snap.open_interest_usd,
            "funding_rate": snap.funding_rate,
            "long_short_ratio": snap.ls_ratio_global,
            "ls_position_ratio": getattr(snap.ls_top_position, "ratio", 1.0),
            "ls_account_ratio": getattr(snap.ls_top_account, "ratio", 1.0),
            "ls_top_position_long_pct": getattr(snap.ls_top_position, "long_pct", 50.0),
            "ls_top_position_short_pct": getattr(snap.ls_top_position, "short_pct", 50.0),
            "ls_top_account_long_pct": getattr(snap.ls_top_account, "long_pct", 50.0),
            "ls_top_account_short_pct": getattr(snap.ls_top_account, "short_pct", 50.0),
            "oi_change_4h_pct": getattr(snap, "oi_change_4h_pct", 0.0),
            "extremity_score": 0.0,
            "reasons": ["display_only_btc_narrative"],
        }

    async def _refresh_btc_narrative_display(self) -> dict[str, object]:
        """Refresh BTC + followers for Dashboard display only.

        Important:
        - Does not append symbols to scan_results.
        - Does not call TriggerConfirmation.
        - Does not call TradeGenerator.
        - Does not submit trades.
        """
        try:
            narrative_snaps = await self.data_collector.collect(BTC_NARRATIVE_SYMBOLS)
        except Exception as e:
            logger.exception(f"BTC Narrative display collect failed: {e}")
            return {"ok": False, "count": 0, "error": str(e)}

        rows: list[dict] = []
        decisions: dict[str, dict] = {}

        for sym, snap in narrative_snaps.items():
            try:
                row = self._snapshot_to_dashboard_row(snap)

                lmap = self.liquidity_engine.build_map(snap)
                oi_change = getattr(snap, "oi_change_4h_pct", 0.0)

                pos = self.positioning.analyze(snap, oi_change)
                ctx = self.context.analyze(snap, oi_change)
                regime = self.regime.detect(snap)
                dec = self.decision.evaluate(snap, lmap, pos, ctx, regime, oi_change)

                # Let dashboard heat use the real display decision score.
                row["extremity_score"] = dec.score
                rows.append(row)

                decisions[sym] = {
                    "symbol": sym,
                    "price": snap.price,
                    "score": dec.score,
                    "direction": dec.direction,
                    "size_factor": dec.size_factor,
                    "components": dec.components,
                    "reasoning": dec.reasoning,
                    "state": pos.state.value,
                    "regime": regime.regime.value,
                    "imbalance": lmap.imbalance,
                    "dominant_side": lmap.dominant_side,
                    "primary_target": (
                        lmap.primary_target.price_level if lmap.primary_target else None
                    ),
                    "funding_rate": snap.funding_rate,
                    "ls_ratio": snap.ls_ratio_global,
                    "long_short_ratio": snap.ls_ratio_global,
                    "ls_position_ratio": getattr(snap.ls_top_position, "ratio", 1.0),
                    "ls_account_ratio": getattr(snap.ls_top_account, "ratio", 1.0),
                    "ls_top_position_long_pct": getattr(snap.ls_top_position, "long_pct", 50.0),
                    "ls_top_position_short_pct": getattr(snap.ls_top_position, "short_pct", 50.0),
                    "ls_top_account_long_pct": getattr(snap.ls_top_account, "long_pct", 50.0),
                    "ls_top_account_short_pct": getattr(snap.ls_top_account, "short_pct", 50.0),
                    "oi_usd": snap.open_interest_usd,
                    "confirmation": False,
                    "trigger_summary": "display_only_btc_narrative",
                    "signal_status": "DISPLAY_ONLY",
                    "checked_at": datetime.utcnow().isoformat(),
                }

            except Exception as e:
                logger.exception(f"BTC Narrative display error for {sym}: {e}")

        self.last_narrative_results = rows
        self.last_narrative_decisions = decisions

        logger.info(
            "BTC Narrative display feed refreshed: "
            f"{len(rows)}/{len(BTC_NARRATIVE_SYMBOLS)} symbols"
        )

        return {"ok": True, "count": len(rows)}

    async def _get_active_trade_symbols(self) -> set[str]:
        """Return symbols that already have PENDING/TRIGGERED paper trades.

        Used to skip new setup analysis for symbols that are already managed
        by PaperExecutor. This does NOT stop price updates or trade management.
        """
        try:
            open_trades = await self.executor.get_open_trades()
        except Exception as e:
            logger.exception(f"Failed to load active trade symbols: {e}")
            return set()

        return {
            str(t.symbol).upper()
            for t in open_trades
            if getattr(t, "symbol", None)
        }

    async def _filter_active_trade_symbols(self, scan_results: list) -> list:
        """Remove symbols with active PENDING/TRIGGERED trades from scan results.

        This avoids wasting a full analysis cycle only to reject later with
        active_trade_exists.
        """
        active_symbols = await self._get_active_trade_symbols()

        if not active_symbols:
            return scan_results

        before = len(scan_results)
        filtered = [
            r for r in scan_results
            if str(getattr(r, "symbol", "")).upper() not in active_symbols
        ]
        removed = before - len(filtered)

        if removed:
            logger.info(
                "Active-symbol pre-filter: skipped "
                f"{removed}/{before} symbols already in open trades: "
                + ", ".join(sorted(active_symbols))
            )

        return filtered


    async def _build_position_price_map(self, snapshots: dict) -> dict[str, float]:
        """
        Build the price map used by PaperExecutor.update_positions().

        Why this exists:
        scanner/data_collector only returns prices for the current shortlist.
        But paper trades can remain PENDING/TRIGGERED even after their symbol
        leaves the shortlist. Without this merge, TP/SL/BE/Trailing will stop
        updating for those open trades.

        Priority:
        1) snapshot prices from current scanner cycle
        2) live prices for open PENDING/TRIGGERED trades not in snapshots
        """
        prices: dict[str, float] = {
            sym: snap.price
            for sym, snap in snapshots.items()
            if getattr(snap, "price", None) is not None
        }

        try:
            open_trades = await self.executor.get_open_trades()
        except Exception as e:
            logger.exception(f"Failed to load open trades for price merge: {e}")
            return prices

        open_symbols = {
            t.symbol
            for t in open_trades
            if getattr(t, "symbol", None)
        }

        missing_symbols = sorted(sym for sym in open_symbols if sym not in prices)

        if not missing_symbols:
            return prices

        if self.live_price is None:
            logger.warning(
                "LivePriceTracker unavailable; missing open trade prices for: "
                + ", ".join(missing_symbols)
            )
            return prices

        try:
            await self.live_price.update_prices()
            added = 0

            for sym in missing_symbols:
                p = self.live_price.get_price(sym)
                if p is None:
                    logger.warning(f"No live price found for open trade symbol {sym}")
                    continue

                prices[sym] = float(p)
                added += 1

            if added:
                logger.info(
                    f"Paper price merge: added {added}/{len(missing_symbols)} "
                    "open-trade prices outside scanner shortlist"
                )

        except Exception as e:
            logger.exception(f"Live open-trade price merge failed: {e}")

        return prices

    async def run_cycle(self) -> dict:
        """One full pipeline cycle: scan → analyze → decide → confirm → submit."""
        cycle_start = datetime.utcnow()
        logger.info("=" * 60)
        logger.info(f"Cycle start: {cycle_start.isoformat()}")

        scan_results = await self.scanner.scan()

        # Always refresh BTC Narrative display data independently from Final Shortlist.
        # This keeps BTC/SOL/ETH/DOGE/XRP visible on the dashboard even when Hunter
        # Original filters them out or they are not part of the current shortlist.
        await self._refresh_btc_narrative_display()


        if hasattr(self.scanner, "last_diagnostics") and self.scanner.last_diagnostics:
            d = self.scanner.last_diagnostics
            self.last_scan_diagnostics = {
                "total_symbols": d.total_symbols,
                "excluded_quality": d.excluded_stablecoins,
                "passed_volume": d.passed_volume,
                "passed_oi": d.passed_oi,
                "passed_extremity": d.passed_extremity,
                "final_shortlist": d.final_shortlist,
            }

        if not scan_results:
            logger.warning("Scanner returned no shortlist")

            # Even when scanner finds no setups, keep managing existing paper trades.
            prices = await self._build_position_price_map({})
            await self.executor.update_positions(prices)
            await self.shadow_tracker.update_outcomes(prices)
            await self.executor.take_portfolio_snapshot()

            self.last_cycle_at = datetime.utcnow()
            return {"status": "no_setups", "shortlist": []}

        scan_results = await self._filter_active_trade_symbols(scan_results)

        if not scan_results:
            logger.info(
                "All scanner shortlist symbols already have active trades; "
                "skipping new setup analysis for this cycle"
            )

            prices = await self._build_position_price_map({})
            await self.executor.update_positions(prices)
            await self.shadow_tracker.update_outcomes(prices)
            await self.executor.take_portfolio_snapshot()

            self.last_scan_results = []
            self.last_cycle_at = datetime.utcnow()
            return {"status": "active_symbols_filtered", "shortlist": []}

        self.last_scan_results = [
            {
                "symbol": r.symbol,
                "price": r.price,
                "volume_24h_usd": r.volume_24h_usd,
                "open_interest_usd": r.open_interest_usd,
                "funding_rate": r.funding_rate,

                # Dashboard L/S sources:
                # long_short_ratio  = global account ratio
                # ls_position_ratio = top trader position ratio
                # ls_account_ratio  = top trader account ratio
                "long_short_ratio": r.long_short_ratio,
                "ls_position_ratio": getattr(r, "ls_position_ratio", 1.0),
                "ls_account_ratio": getattr(r, "ls_account_ratio", 1.0),

                "oi_change_4h_pct": r.oi_change_4h_pct,
                "extremity_score": r.extremity_score,
                "reasons": r.reasons,
            }
            for r in scan_results
        ]

        symbols = [r.symbol for r in scan_results]
        snapshots = await self.data_collector.collect(symbols)

        decisions = []
        for sym, snap in snapshots.items():
            try:
                lmap = self.liquidity_engine.build_map(snap)
                await self.liquidity_engine.persist(lmap)

                oi_change = next(
                    (r.oi_change_4h_pct for r in scan_results if r.symbol == sym),
                    0.0,
                )

                pos = self.positioning.analyze(snap, oi_change)
                ctx = self.context.analyze(snap, oi_change)
                regime = self.regime.detect(snap)

                dec = self.decision.evaluate(snap, lmap, pos, ctx, regime, oi_change)

                if dec.direction == "WAIT":
                    self.last_decisions[sym] = {
                        "symbol": sym,
                        "price": snap.price,
                        "score": dec.score,
                        "direction": dec.direction,
                        "size_factor": dec.size_factor,
                        "components": dec.components,
                        "reasoning": dec.reasoning,
                        "state": pos.state.value,
                        "regime": regime.regime.value,
                        "imbalance": lmap.imbalance,
                        "dominant_side": lmap.dominant_side,
                        "primary_target": (
                            lmap.primary_target.price_level if lmap.primary_target else None
                        ),
                        "funding_rate": snap.funding_rate,
                        "ls_ratio": snap.ls_ratio_global,
                        "long_short_ratio": snap.ls_ratio_global,
                        "ls_position_ratio": getattr(snap.ls_top_position, "ratio", 1.0),
                        "ls_account_ratio": getattr(snap.ls_top_account, "ratio", 1.0),
                        "ls_top_position_long_pct": getattr(snap.ls_top_position, "long_pct", 50.0),
                        "ls_top_position_short_pct": getattr(snap.ls_top_position, "short_pct", 50.0),
                        "ls_top_account_long_pct": getattr(snap.ls_top_account, "long_pct", 50.0),
                        "ls_top_account_short_pct": getattr(snap.ls_top_account, "short_pct", 50.0),
                        "oi_usd": snap.open_interest_usd,
                        "confirmation": False,
                        "trigger_summary": "decision=WAIT",
                        "signal_status": "WAIT",
                        "checked_at": datetime.utcnow().isoformat(),
                    }
                    continue

                trig = self.trigger.check(snap, dec.direction, oi_change_5m=None, decision=dec)
                dec.confirmation = trig.confirmed
                dec.reasoning.extend([f"Trigger/{c.name}: {c.detail}" for c in trig.checks])

                signal_status = "EXECUTION_READY" if trig.confirmed else "WAIT_TRIGGER"

                self.last_decisions[sym] = {
                    "symbol": sym,
                    "price": snap.price,
                    "score": dec.score,
                    "direction": dec.direction,
                    "size_factor": dec.size_factor,
                    "components": dec.components,
                    "reasoning": dec.reasoning,
                    "state": pos.state.value,
                    "regime": regime.regime.value,
                    "imbalance": lmap.imbalance,
                    "dominant_side": lmap.dominant_side,
                    "primary_target": (
                        lmap.primary_target.price_level if lmap.primary_target else None
                    ),
                    "funding_rate": snap.funding_rate,
                    "ls_ratio": snap.ls_ratio_global,
                    "long_short_ratio": snap.ls_ratio_global,
                    "ls_position_ratio": getattr(snap.ls_top_position, "ratio", 1.0),
                    "ls_account_ratio": getattr(snap.ls_top_account, "ratio", 1.0),
                    "ls_top_position_long_pct": getattr(snap.ls_top_position, "long_pct", 50.0),
                    "ls_top_position_short_pct": getattr(snap.ls_top_position, "short_pct", 50.0),
                    "ls_top_account_long_pct": getattr(snap.ls_top_account, "long_pct", 50.0),
                    "ls_top_account_short_pct": getattr(snap.ls_top_account, "short_pct", 50.0),
                    "oi_usd": snap.open_interest_usd,
                    "confirmation": trig.confirmed,
                    "trigger_summary": trig.summary,
                    "signal_status": signal_status,
                    "checked_at": datetime.utcnow().isoformat(),
                }

                if not trig.confirmed:
                    logger.info(
                        f"{sym}: setup allowed (score={dec.score:.1f}) but trigger not confirmed "
                        f"({trig.summary}) — kept as WAIT_TRIGGER"
                    )
                    continue

                card = await self.trade_gen.generate(
                    snap, lmap, dec, pos.state, regime.regime
                )

                if card is None:
                    continue

                trade_id = await self.executor.submit(card)
                if trade_id:
                    decisions.append({"symbol": sym, "trade_id": trade_id, "card": card})
                    await self.alerter.setup_alert(card)

            except Exception as e:
                logger.exception(f"Pipeline error for {sym}: {e}")

        prices = await self._build_position_price_map(snapshots)
        await self.executor.update_positions(prices)
        await self.shadow_tracker.update_outcomes(prices)
        await self.executor.take_portfolio_snapshot()

        self.last_cycle_at = datetime.utcnow()
        elapsed = (self.last_cycle_at - cycle_start).total_seconds()
        logger.info(f"Cycle done in {elapsed:.1f}s — {len(decisions)} new setups")

        if settings.section("learning").get("enabled", False):
            from sqlalchemy import func as sqlfunc
            from sqlalchemy import select
            from src.core.database import AsyncSessionLocal, TradeOutcome

            async with AsyncSessionLocal() as _s:
                _res = await _s.execute(select(sqlfunc.count()).select_from(TradeOutcome))
                _total = _res.scalar() or 0

            adapt_every = settings.section("learning").get("adapt_every_n_trades", 10)
            if _total > 0 and _total % adapt_every == 0:
                logger.info(f"🧠 Learning Loop: running adapt() at {_total} outcomes")
                _matrix = await PerformanceAnalyzer().analyze()
                AdaptiveWeightsEngine().adapt(_matrix)

        return {
            "status": "ok",
            "shortlist_count": len(scan_results),
            "snapshots_count": len(snapshots),
            "new_setups": len(decisions),
        }

    async def recover_open_trades(self) -> dict:
        """Run PaperExecutor startup recovery before the live loop starts."""
        logger.info("[Recovery v1] starting startup recovery check")

        prices = await self._build_position_price_map({})

        if not prices:
            logger.info("[Recovery v1] no open-trade prices available")
            return {
                "checked": 0,
                "closed_tp": 0,
                "closed_sl": 0,
                "cancelled": 0,
                "missing_price": 0,
                "errors": 0,
            }

        stats = await self.executor.startup_recovery_v1(prices)
        await self.executor.take_portfolio_snapshot()
        return stats

    async def run_forever(self) -> None:
        """Background loop. Scan interval from config."""
        interval = settings.scanner["scan_interval_seconds"]
        logger.info(f"Bot loop started — interval {interval}s")
        await self.alerter.info("HunterBot Bot started.")
        while True:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.exception(f"Cycle exception: {e}")
            await asyncio.sleep(interval)


async def main_cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["bot", "backtest", "scan", "init"])
    parser.add_argument("--symbol", type=str, default="BTCUSDT")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--timeframe", type=str, default="1h")
    args = parser.parse_args()

    await init_db()

    if args.mode == "init":
        logger.info("Database initialized.")
        return

    if args.mode == "scan":
        bot = LiquidityHunterBot()
        result = await bot.run_cycle()
        logger.info(f"Scan result: {result}")
        return

    if args.mode == "backtest":
        engine = BacktestEngine()
        stats = await engine.run(args.symbol, days=args.days, timeframe=args.timeframe)
        logger.info(f"Stats: {stats}")
        return

    if args.mode == "bot":
        bot = LiquidityHunterBot()
        await bot.recover_open_trades()
        await bot.run_forever()


if __name__ == "__main__":
    asyncio.run(main_cli())