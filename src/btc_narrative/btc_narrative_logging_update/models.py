"""Data models for BTC Narrative Strategy."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class BtcNarrativeState(str, Enum):
    NEUTRAL = "NEUTRAL"
    BTC_SHORT_CROWDED = "BTC_SHORT_CROWDED"  # BTC shorts more crowded -> LONG followers
    BTC_LONG_CROWDED = "BTC_LONG_CROWDED"    # BTC longs more crowded -> SHORT followers

    # Kept for future expansion / backward compatibility with earlier scaffold.
    BTC_SHORT_SQUEEZE = "BTC_SHORT_SQUEEZE"
    BTC_LONG_TRAP = "BTC_LONG_TRAP"
    BTC_EXPANSION_UP = "BTC_EXPANSION_UP"
    BTC_EXPANSION_DOWN = "BTC_EXPANSION_DOWN"


class FollowerAction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    WAIT = "WAIT"


@dataclass
class BtcNarrativeSignal:
    strategy_name: str
    state: BtcNarrativeState
    driver_symbol: str
    driver_price: float
    direction: FollowerAction
    score: float
    confidence: float
    reasons: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class FollowerCandidate:
    strategy_name: str
    symbol: str
    action: FollowerAction
    btc_state: BtcNarrativeState
    btc_score: float
    price: float
    score: float
    reasons: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class BtcNarrativeCycleResult:
    ok: bool
    signal: Optional[BtcNarrativeSignal]
    candidates: list[FollowerCandidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    checked_at: datetime = field(default_factory=datetime.utcnow)
