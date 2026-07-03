"""Management Profiles v1.

Maps Trade Quality Class (A/B/C) to exit-management parameters.
This module is intentionally config-driven and does not decide direction,
entry, SL, or sizing. It only controls post-entry management behavior.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.core.config import settings


@dataclass(frozen=True)
class ManagementProfileSpec:
    name: str
    tp1_r: float
    tp1_close_pct: float
    break_even_r: float
    trailing_r: float
    trailing_distance_r: float

    @property
    def tp1_enabled(self) -> bool:
        return self.tp1_r > 0.0 and self.tp1_close_pct > 0.0


DEFAULT_PROFILE_BY_QUALITY = {
    "A": "AGGRESSIVE",
    "B": "BALANCED",
    "C": "CONSERVATIVE",
}

DEFAULT_PROFILES: dict[str, dict[str, float]] = {
    # V2.1: Profit Capture / Anti-Leakage Mode.
    # Audit baseline after 83h: Profit Leakage ~91%, Avg Capture ~8%.
    # The fix intentionally changes ONLY post-entry management behavior:
    # no scanner, direction, entry, SL, sizing, RR, cooldown, or filters.
    #
    # Principle:
    # - take a small partial early enough to pay for risk,
    # - move to BE before large giveback,
    # - activate trailing near 1R,
    # - use a tighter distance because prior Trailing Audit showed
    #   Avg Lost After Trail > 2R.
    "CONSERVATIVE": {
        "tp1_r": 0.30,
        "tp1_close_pct": 0.50,
        "break_even_r": 0.50,
        "trailing_r": 0.80,
        "trailing_distance_r": 0.30,
    },
    "BALANCED": {
        "tp1_r": 0.40,
        "tp1_close_pct": 0.35,
        "break_even_r": 0.60,
        "trailing_r": 0.90,
        "trailing_distance_r": 0.35,
    },
    "AGGRESSIVE": {
        "tp1_r": 0.50,
        "tp1_close_pct": 0.25,
        "break_even_r": 0.70,
        "trailing_r": 1.00,
        "trailing_distance_r": 0.40,
    },
}


def _cfg_dict() -> dict[str, Any]:
    raw = getattr(settings, "management_profiles", None)
    if isinstance(raw, dict):
        return raw
    return {}


def _to_float(value: Any, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _paper_default(name: str, default: float) -> float:
    try:
        return float(settings.paper_executor.get(name, default))
    except Exception:
        return default


def default_fixed_profile() -> ManagementProfileSpec:
    return ManagementProfileSpec(
        name="DEFAULT_FIXED",
        tp1_r=_paper_default("tp1_r", 0.4),
        tp1_close_pct=_paper_default("tp1_close_pct", 0.5),
        break_even_r=_paper_default("break_even_r", 1.0),
        trailing_r=_paper_default("trailing_r", 1.5),
        trailing_distance_r=_paper_default("trailing_distance_r", 0.6),
    )


def _profile_dict(name: str) -> dict[str, Any]:
    cfg = _cfg_dict()
    profiles = cfg.get("profiles") if isinstance(cfg.get("profiles"), dict) else {}
    raw = profiles.get(name, {}) if isinstance(profiles, dict) else {}
    base = DEFAULT_PROFILES.get(name, {})
    merged = dict(base)
    if isinstance(raw, dict):
        merged.update(raw)
    return merged


def get_profile(name: str | None) -> ManagementProfileSpec:
    profile_name = str(name or "DEFAULT_FIXED").upper()
    if profile_name == "DEFAULT_FIXED":
        return default_fixed_profile()
    if profile_name not in DEFAULT_PROFILES:
        profile_name = "BALANCED"

    raw = _profile_dict(profile_name)
    return ManagementProfileSpec(
        name=profile_name,
        tp1_r=_to_float(raw.get("tp1_r"), DEFAULT_PROFILES[profile_name]["tp1_r"]),
        tp1_close_pct=_to_float(raw.get("tp1_close_pct"), DEFAULT_PROFILES[profile_name]["tp1_close_pct"]),
        break_even_r=_to_float(raw.get("break_even_r"), DEFAULT_PROFILES[profile_name]["break_even_r"]),
        trailing_r=_to_float(raw.get("trailing_r"), DEFAULT_PROFILES[profile_name]["trailing_r"]),
        trailing_distance_r=_to_float(raw.get("trailing_distance_r"), DEFAULT_PROFILES[profile_name]["trailing_distance_r"]),
    )


def profile_for_quality(quality_class: str | None) -> ManagementProfileSpec:
    cfg = _cfg_dict()
    mapping = DEFAULT_PROFILE_BY_QUALITY.copy()
    raw_map = cfg.get("quality_map")
    if isinstance(raw_map, dict):
        mapping.update({str(k).upper(): str(v).upper() for k, v in raw_map.items()})

    q = str(quality_class or "B").upper()
    return get_profile(mapping.get(q, "BALANCED"))


def profile_from_trade(trade: Any) -> ManagementProfileSpec:
    """Return frozen profile parameters for a trade.

    New trades store profile_* columns at creation. For older trades we fall
    back to current config using management_profile.
    """
    name = str(getattr(trade, "management_profile", None) or "DEFAULT_FIXED").upper()
    base = get_profile(name)

    return ManagementProfileSpec(
        name=name,
        tp1_r=_to_float(getattr(trade, "profile_tp1_r", None), base.tp1_r),
        tp1_close_pct=_to_float(getattr(trade, "profile_tp1_close_pct", None), base.tp1_close_pct),
        break_even_r=_to_float(getattr(trade, "profile_break_even_r", None), base.break_even_r),
        trailing_r=_to_float(getattr(trade, "profile_trailing_r", None), base.trailing_r),
        trailing_distance_r=_to_float(getattr(trade, "profile_trailing_distance_r", None), base.trailing_distance_r),
    )
