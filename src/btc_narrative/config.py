"""BTC Narrative Strategy configuration.

This module is intentionally isolated from Hunter Original.

Current active decision logic uses ONLY BTC L/S sources:
- LS_POSIT  (top trader position)
- LS_RATIO  (global long/short ratio)
- LS_ACCOUNT (top trader account)

Other BTC context such as funding, OI, VWAP, and liquidity is collected/logged
for future research only and is NOT part of the active decision logic yet.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True)
class BtcNarrativeConfig:
    # Runtime
    enabled: bool = False
    dry_run: bool = True
    strategy_name: str = "BTC_NARRATIVE"

    # Universe
    driver_symbol: str = "BTCUSDT"
    follower_symbols: Sequence[str] = field(
        default_factory=lambda: ("ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT")
    )

    # ── Active BTC Narrative decision inputs ────────────────────────────────
    # Weights must sum close to 1.0. They are normalized defensively in analyzer.
    # LS_POSIT is most important, then LS_RATIO, then LS_ACCOUNT.
    ls_position_weight: float = 0.45
    ls_ratio_weight: float = 0.35
    ls_account_weight: float = 0.20

    # Minimum agreement across the three LS sources.
    # Example: 2 means at least two of LS_POSIT/LS_RATIO/LS_ACCOUNT
    # must point to the same crowded side.
    min_vote_agreement: int = 2

    # Crowding score threshold.
    # Score is 0-100. Higher means stronger BTC crowd imbalance.
    min_crowding_score: float = 15.0
    strong_crowding_score: float = 30.0

    # Optional edge gap between long crowding and short crowding.
    # Prevents tiny differences from creating a narrative.
    min_crowding_spread: float = 3.0

    # Ignore near-50/50 individual LS source values.
    # Example: 52/48 is weak; 56/44 starts to matter more.
    min_source_edge_pct: float = 2.0

    # ── Future research collection flags ────────────────────────────────────
    collect_funding: bool = True
    collect_oi: bool = True
    collect_vwap: bool = True
    collect_liquidity: bool = True

    # ── Follower selection / future execution filters ───────────────────────
    # These are not used for live trade submission yet. Router is dry-run first.
    min_follower_score: float = 0.0
    max_follower_entry_distance_pct: float = 0.05
    min_risk_reward_ratio: float = 1.5
    max_open_per_strategy: int = 3
    max_open_per_symbol: int = 1


DEFAULT_CONFIG = BtcNarrativeConfig()
