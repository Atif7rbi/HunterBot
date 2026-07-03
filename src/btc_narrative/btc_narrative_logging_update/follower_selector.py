"""Follower selector for BTC Narrative Strategy.

For now this is analysis-only:
- Receives BTC narrative signal.
- Converts it into candidate actions for configured followers.
- Does NOT generate entries, SL, TP, or submit trades.
"""
from __future__ import annotations

from .config import BtcNarrativeConfig, DEFAULT_CONFIG
from .logger import btc_logger as logger
from .models import (
    BtcNarrativeSignal,
    BtcNarrativeState,
    FollowerAction,
    FollowerCandidate,
)


class FollowerSelector:
    def __init__(self, cfg: BtcNarrativeConfig = DEFAULT_CONFIG) -> None:
        self.cfg = cfg
        logger.info(
            "FollowerSelector initialized: followers=%s min_follower_score=%.2f",
            list(cfg.follower_symbols),
            cfg.min_follower_score,
        )

    def select(self, signal: BtcNarrativeSignal, follower_snapshots: dict) -> list[FollowerCandidate]:
        candidates: list[FollowerCandidate] = []

        if signal.direction == FollowerAction.WAIT or signal.state == BtcNarrativeState.NEUTRAL:
            logger.info(
                "Follower selection skipped: BTC state=%s direction=%s score=%.2f",
                signal.state.value,
                signal.direction.value,
                signal.score,
            )
            return candidates

        for symbol, snap in follower_snapshots.items():
            if snap is None:
                logger.info("Follower skipped: %s missing snapshot", symbol)
                continue

            price = float(getattr(snap, "price", 0.0) or 0.0)
            if price <= 0:
                logger.info("Follower skipped: %s invalid price=%s", symbol, price)
                continue

            # For dry-run, follower score inherits BTC score.
            # Later we can add follower execution quality, distance, RR, etc.
            follower_score = signal.score
            if follower_score < self.cfg.min_follower_score:
                logger.info(
                    "Follower rejected: %s score %.2f < %.2f",
                    symbol,
                    follower_score,
                    self.cfg.min_follower_score,
                )
                continue

            reasons = [
                f"btc_state={signal.state.value}",
                f"btc_direction={signal.direction.value}",
                f"btc_score={signal.score:.1f}",
                "dry_run_candidate_only",
            ]

            candidate = FollowerCandidate(
                strategy_name=self.cfg.strategy_name,
                symbol=symbol,
                action=signal.direction,
                btc_state=signal.state,
                btc_score=signal.score,
                price=price,
                score=follower_score,
                reasons=reasons,
                raw={
                    "follower_price": price,
                    "btc_components": signal.components,
                    "btc_reasons": signal.reasons,
                },
            )
            candidates.append(candidate)

            logger.info(
                "Follower candidate: symbol=%s action=%s price=%.8f score=%.2f reasons=%s",
                symbol,
                signal.direction.value,
                price,
                follower_score,
                reasons,
            )

        logger.info("Follower selection finished: candidates=%d", len(candidates))
        return candidates
