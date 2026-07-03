from __future__ import annotations

from src.core.config import settings
from src.layers.liquidity_engine import LiquidityMap


class LiquidityDistance:
    """
    Execution-quality helper.

    Reads nearest liquidity from LiquidityMap.
    Returns metadata only:
    - no direction logic
    - no filtering
    - no veto
    """

    @staticmethod
    def evaluate(lmap: LiquidityMap) -> dict:
        cfg = settings.section("liquidity_distance")

        enabled = bool(cfg.get("enabled", True))

        if not enabled:
            return {
                "nearest_level": None,
                "distance_pct": None,
                "distance_score": 0,
                "execution_quality": "DISABLED",
            }

        if not lmap.primary_target:
            return {
                "nearest_level": None,
                "distance_pct": None,
                "distance_score": 0,
                "execution_quality": "UNKNOWN",
            }

        high_distance = float(cfg.get("high_distance_pct", 0.5))
        normal_distance = float(cfg.get("normal_distance_pct", 1.0))
        low_distance = float(cfg.get("low_distance_pct", 2.0))

        high_score = float(cfg.get("high_score", 8))
        normal_score = float(cfg.get("normal_score", 5))
        low_score = float(cfg.get("low_score", 2))
        poor_score = float(cfg.get("poor_score", 0))

        distance_pct = abs(lmap.primary_target.distance_pct) * 100

        if distance_pct <= high_distance:
            score = high_score
            quality = "HIGH"
        elif distance_pct <= normal_distance:
            score = normal_score
            quality = "NORMAL"
        elif distance_pct <= low_distance:
            score = low_score
            quality = "LOW"
        else:
            score = poor_score
            quality = "POOR"

        return {
            "nearest_level": lmap.primary_target.price_level,
            "distance_pct": round(distance_pct, 2),
            "distance_score": score,
            "execution_quality": quality,
        }
