"""BTC Narrative Strategy package.

This package is intentionally isolated from Hunter Original.

Active decision logic:
- BTC LS_POSIT
- BTC LS_RATIO
- BTC LS_ACCOUNT

Other BTC context is collected/logged for future research only.
"""

from .config import BtcNarrativeConfig, DEFAULT_CONFIG
from .engine import BtcNarrativeEngine
from .router import BtcNarrativeRouter
from .models import (
    BtcNarrativeCycleResult,
    BtcNarrativeSignal,
    BtcNarrativeState,
    FollowerAction,
    FollowerCandidate,
)

__all__ = [
    "BtcNarrativeConfig",
    "DEFAULT_CONFIG",
    "BtcNarrativeEngine",
    "BtcNarrativeRouter",
    "BtcNarrativeCycleResult",
    "BtcNarrativeSignal",
    "BtcNarrativeState",
    "FollowerAction",
    "FollowerCandidate",
]
