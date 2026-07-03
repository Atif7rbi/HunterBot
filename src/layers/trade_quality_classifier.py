"""Trade Quality Classifier v1.

Purpose:
- Classify each accepted TradeCard as A / B / C at creation time.
- Store the classification for later analysis.
- Does NOT change entry, SL, TP, trailing, sizing, or order flow.

The goal is to discover whether high-quality setups actually produce higher
MaxR / FinalR / Capture before connecting these classes to Management Profiles.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True)
class TradeQualityResult:
    quality_class: str
    quality_score: float
    reason: str


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _find_number(pattern: str, text: str, default: float = 0.0) -> float:
    m = re.search(pattern, text, flags=re.IGNORECASE)
    if not m:
        return default
    return _to_float(m.group(1), default)


def _reasoning_text(card: Any) -> str:
    return "\n".join(str(x or "") for x in getattr(card, "reasoning", []) or [])


def _extract_authority_parts(card: Any) -> dict[str, float]:
    text = _reasoning_text(card)
    # Example:
    # Authority Scores: total=71.3, funding=25.0, ls_pos=16.3, oi=0.0, vwap=30.0, ls_account=4.0
    return {
        "authority_total": _find_number(r"Authority Scores:\s*total=([0-9.+-]+)", text, _to_float(getattr(card, "setup_score", 0.0))),
        "funding": _find_number(r"funding=([0-9.+-]+)", text, 0.0),
        "ls_position": _find_number(r"ls_pos=([0-9.+-]+)", text, 0.0),
        "oi": _find_number(r"oi=([0-9.+-]+)", text, 0.0),
        "vwap": _find_number(r"vwap=([0-9.+-]+)", text, 0.0),
        "ls_account": _find_number(r"ls_account=([0-9.+-]+)", text, 0.0),
        "execution_quality": _find_number(r"Execution Quality:\s*([0-9.+-]+)", text, 50.0),
    }


def _component(snapshot: dict | None, key: str, default: float = 0.0) -> float:
    if not isinstance(snapshot, dict):
        return default
    comps = snapshot.get("components") or {}
    if isinstance(comps, dict) and key in comps:
        return _to_float(comps.get(key), default)
    return default


def classify_trade_quality(card: Any) -> TradeQualityResult:
    """Return A/B/C quality for an accepted TradeCard.

    V1 is intentionally conservative and analytics-only.
    It combines:
    - Direction/authority strength from Hunter score and Authority Scores line.
    - Execution quality from TradeGenerator reasoning.
    - RR quality from TradeCard.risk_reward.

    The output should be studied later against Avg MaxR, FinalR, and Capture.
    """
    setup_score = _to_float(getattr(card, "setup_score", 0.0))
    rr = _to_float(getattr(card, "risk_reward", 0.0))
    parts = _extract_authority_parts(card)

    authority_total = max(parts["authority_total"], setup_score)
    execution_quality = parts["execution_quality"]

    funding = parts["funding"]
    ls_position = parts["ls_position"]
    oi = parts["oi"]
    vwap = parts["vwap"]
    ls_account = parts["ls_account"]

    # Component confirmations. These are not execution commands; only quality evidence.
    confirmations = 0
    confirmations += 1 if funding >= 20.0 else 0
    confirmations += 1 if ls_position >= 15.0 else 0
    confirmations += 1 if oi >= 10.0 else 0
    confirmations += 1 if vwap >= 20.0 else 0
    confirmations += 1 if ls_account >= 3.0 else 0
    confirmations += 1 if execution_quality >= 70.0 else 0
    confirmations += 1 if rr >= 2.0 else 0

    # 0-100 analytics score. It is deliberately simple and explainable.
    authority_score = min(100.0, max(0.0, authority_total))
    execution_score = min(100.0, max(0.0, execution_quality))
    rr_score = min(100.0, max(0.0, rr / 2.5 * 100.0))
    confirm_score = min(100.0, confirmations / 7.0 * 100.0)

    quality_score = (
        authority_score * 0.45
        + execution_score * 0.25
        + rr_score * 0.15
        + confirm_score * 0.15
    )

    if quality_score >= 80.0 and confirmations >= 5:
        quality_class = "A"
    elif quality_score >= 65.0 and confirmations >= 3:
        quality_class = "B"
    else:
        quality_class = "C"

    reason = (
        f"class={quality_class}; score={quality_score:.1f}; "
        f"authority={authority_total:.1f}; execQ={execution_quality:.1f}; rr={rr:.2f}; "
        f"confirmations={confirmations}/7; funding={funding:.1f}; "
        f"ls_pos={ls_position:.1f}; oi={oi:.1f}; vwap={vwap:.1f}; ls_account={ls_account:.1f}"
    )

    return TradeQualityResult(
        quality_class=quality_class,
        quality_score=round(quality_score, 4),
        reason=reason[:512],
    )
