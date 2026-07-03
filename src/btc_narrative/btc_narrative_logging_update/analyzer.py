"""BTC-only narrative analyzer.

BTC Narrative is intentionally separate from Hunter Original.

Current active decision logic:
- Collect/log all BTC context for future research.
- Use ONLY BTC LS_POSIT, BTC LS_RATIO, and BTC LS_ACCOUNT for active decision.
- If BTC long crowding > short crowding -> SHORT followers.
- If BTC short crowding > long crowding -> LONG followers.

Important:
- Funding/OI/VWAP/liquidity are logged for later analysis only.
- Funding + LS_POSIT agreement is NOT required here; that belongs to Hunter Original.
"""
from __future__ import annotations

from .config import BtcNarrativeConfig, DEFAULT_CONFIG
from .logger import btc_logger as logger
from .models import BtcNarrativeSignal, BtcNarrativeState, FollowerAction


class BtcNarrativeAnalyzer:
    def __init__(self, cfg: BtcNarrativeConfig = DEFAULT_CONFIG) -> None:
        self.cfg = cfg
        logger.info(
            "Analyzer initialized: strategy=%s driver=%s weights(pos/ratio/account)=%.2f/%.2f/%.2f "
            "min_score=%.2f min_spread=%.2f min_votes=%d",
            cfg.strategy_name,
            cfg.driver_symbol,
            cfg.ls_position_weight,
            cfg.ls_ratio_weight,
            cfg.ls_account_weight,
            cfg.min_crowding_score,
            cfg.min_crowding_spread,
            cfg.min_vote_agreement,
        )

    @staticmethod
    def _ratio_to_pcts(ratio: float) -> tuple[float, float]:
        ratio = float(ratio or 1.0)
        if ratio <= 0:
            ratio = 1.0
        long_pct = (ratio / (1.0 + ratio)) * 100.0
        short_pct = 100.0 - long_pct
        return long_pct, short_pct

    @staticmethod
    def _extract_ls_source(source, fallback_ratio: float = 1.0) -> dict:
        ratio = float(getattr(source, "ratio", fallback_ratio) or fallback_ratio or 1.0)

        long_pct = getattr(source, "long_pct", None)
        short_pct = getattr(source, "short_pct", None)

        if long_pct is None or short_pct is None:
            long_pct, short_pct = BtcNarrativeAnalyzer._ratio_to_pcts(ratio)
        else:
            long_pct = float(long_pct or 50.0)
            short_pct = float(short_pct or 50.0)

        return {
            "ratio": ratio,
            "long_pct": long_pct,
            "short_pct": short_pct,
            "long_edge": max(long_pct - 50.0, 0.0),
            "short_edge": max(short_pct - 50.0, 0.0),
        }

    def _weights(self) -> dict[str, float]:
        raw = {
            "ls_position": float(self.cfg.ls_position_weight),
            "ls_ratio": float(self.cfg.ls_ratio_weight),
            "ls_account": float(self.cfg.ls_account_weight),
        }
        total = sum(max(v, 0.0) for v in raw.values())
        if total <= 0:
            return {"ls_position": 0.45, "ls_ratio": 0.35, "ls_account": 0.20}
        return {k: max(v, 0.0) / total for k, v in raw.items()}

    def analyze(self, btc_snapshot, *, btc_decision=None, liquidity_map=None) -> BtcNarrativeSignal:
        symbol = getattr(btc_snapshot, "symbol", self.cfg.driver_symbol)
        price = float(getattr(btc_snapshot, "price", 0.0) or 0.0)

        # Collected/logged for future analysis only.
        funding = float(getattr(btc_snapshot, "funding_rate", 0.0) or 0.0)
        oi_change = float(getattr(btc_snapshot, "oi_change_4h_pct", 0.0) or 0.0)
        open_interest_usd = float(getattr(btc_snapshot, "open_interest_usd", 0.0) or 0.0)

        # Active decision inputs only.
        ls_ratio_global = float(getattr(btc_snapshot, "ls_ratio_global", 1.0) or 1.0)
        ls_ratio = self._extract_ls_source(None, fallback_ratio=ls_ratio_global)
        ls_position = self._extract_ls_source(
            getattr(btc_snapshot, "ls_top_position", None),
            fallback_ratio=1.0,
        )
        ls_account = self._extract_ls_source(
            getattr(btc_snapshot, "ls_top_account", None),
            fallback_ratio=1.0,
        )

        # Future-analysis context only.
        vwap_15m = getattr(btc_snapshot, "vwap_15m", None)
        vwap_1h = getattr(btc_snapshot, "vwap_1h", None)
        vwap_4h = getattr(btc_snapshot, "vwap_4h", None)

        liquidity_imbalance = getattr(liquidity_map, "imbalance", None) if liquidity_map is not None else None
        liquidity_dominant_side = getattr(liquidity_map, "dominant_side", None) if liquidity_map is not None else None
        primary_target = (
            getattr(getattr(liquidity_map, "primary_target", None), "price_level", None)
            if liquidity_map is not None
            else None
        )

        logger.info(
            "BTC full context collected: symbol=%s price=%.8f funding=%.6f oi_usd=%.2f "
            "oi_change_4h=%.2f vwap_15m=%s vwap_1h=%s vwap_4h=%s "
            "liquidity_imbalance=%s liquidity_dominant_side=%s primary_target=%s",
            symbol,
            price,
            funding,
            open_interest_usd,
            oi_change,
            vwap_15m,
            vwap_1h,
            vwap_4h,
            liquidity_imbalance,
            liquidity_dominant_side,
            primary_target,
        )

        logger.info(
            "BTC active LS inputs: LS_RATIO ratio=%.3f long=%.2f short=%.2f | "
            "LS_POSIT ratio=%.3f long=%.2f short=%.2f | "
            "LS_ACCOUNT ratio=%.3f long=%.2f short=%.2f",
            ls_ratio["ratio"],
            ls_ratio["long_pct"],
            ls_ratio["short_pct"],
            ls_position["ratio"],
            ls_position["long_pct"],
            ls_position["short_pct"],
            ls_account["ratio"],
            ls_account["long_pct"],
            ls_account["short_pct"],
        )

        weights = self._weights()
        min_edge = float(self.cfg.min_source_edge_pct)

        sources = {
            "ls_position": ls_position,
            "ls_ratio": ls_ratio,
            "ls_account": ls_account,
        }

        long_crowding_score = 0.0
        short_crowding_score = 0.0
        long_votes = 0
        short_votes = 0

        for name, item in sources.items():
            long_edge = item["long_edge"] if item["long_edge"] >= min_edge else 0.0
            short_edge = item["short_edge"] if item["short_edge"] >= min_edge else 0.0

            long_crowding_score += long_edge * weights[name] * 2.0
            short_crowding_score += short_edge * weights[name] * 2.0

            if long_edge > short_edge and long_edge > 0:
                long_votes += 1
            elif short_edge > long_edge and short_edge > 0:
                short_votes += 1

        spread = abs(long_crowding_score - short_crowding_score)
        score = max(long_crowding_score, short_crowding_score)
        confidence = min(score / 100.0, 1.0)

        components: dict[str, float] = {
            "btc_long_crowding_score": long_crowding_score,
            "btc_short_crowding_score": short_crowding_score,
            "btc_crowding_spread": spread,
            "btc_long_votes": float(long_votes),
            "btc_short_votes": float(short_votes),
            "ls_position_ratio": ls_position["ratio"],
            "ls_position_long_pct": ls_position["long_pct"],
            "ls_position_short_pct": ls_position["short_pct"],
            "ls_ratio_global": ls_ratio["ratio"],
            "ls_ratio_long_pct": ls_ratio["long_pct"],
            "ls_ratio_short_pct": ls_ratio["short_pct"],
            "ls_account_ratio": ls_account["ratio"],
            "ls_account_long_pct": ls_account["long_pct"],
            "ls_account_short_pct": ls_account["short_pct"],
            # Future-analysis context only:
            "funding_rate": funding,
            "oi_change_4h_pct": oi_change,
            "open_interest_usd": open_interest_usd,
        }

        reasons: list[str] = [
            f"ls_position={ls_position['long_pct']:.1f}L/{ls_position['short_pct']:.1f}S",
            f"ls_ratio={ls_ratio['long_pct']:.1f}L/{ls_ratio['short_pct']:.1f}S",
            f"ls_account={ls_account['long_pct']:.1f}L/{ls_account['short_pct']:.1f}S",
        ]

        state = BtcNarrativeState.NEUTRAL
        direction = FollowerAction.WAIT

        min_score = float(self.cfg.min_crowding_score)
        min_spread = float(self.cfg.min_crowding_spread)
        min_votes = int(self.cfg.min_vote_agreement)

        if score >= min_score and spread >= min_spread:
            if long_crowding_score > short_crowding_score and long_votes >= min_votes:
                state = BtcNarrativeState.BTC_LONG_CROWDED
                direction = FollowerAction.SHORT
                reasons.append("btc_long_crowding_gt_short_crowding")
            elif short_crowding_score > long_crowding_score and short_votes >= min_votes:
                state = BtcNarrativeState.BTC_SHORT_CROWDED
                direction = FollowerAction.LONG
                reasons.append("btc_short_crowding_gt_long_crowding")
            else:
                reasons.append("insufficient_ls_vote_agreement")
        else:
            reasons.append(
                f"crowding_threshold_not_met score={score:.1f}/{min_score:.1f} "
                f"spread={spread:.1f}/{min_spread:.1f}"
            )

        logger.info(
            "BTC narrative result: state=%s follower_direction=%s score=%.2f confidence=%.2f "
            "long_score=%.2f short_score=%.2f spread=%.2f long_votes=%d short_votes=%d reasons=%s",
            state.value,
            direction.value,
            score,
            confidence,
            long_crowding_score,
            short_crowding_score,
            spread,
            long_votes,
            short_votes,
            reasons,
        )

        return BtcNarrativeSignal(
            strategy_name=self.cfg.strategy_name,
            state=state,
            driver_symbol=self.cfg.driver_symbol,
            driver_price=price,
            direction=direction,
            score=score,
            confidence=confidence,
            reasons=reasons,
            components=components,
        )
