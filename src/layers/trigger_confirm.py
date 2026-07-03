"""Layer 8 — Trigger Confirmation.

Setup score says:
    "there may be an opportunity here."

Trigger confirmation answers:
    "is the move actually starting now?"

Checks:
  1. Volume spike
  2. OI reaction
  3. Rejection candle
  4. CVD divergence (optional / skipped when not applicable)

Rules:
  - required_confirmations is taken from config
  - confirmation is evaluated against AVAILABLE checks only
  - skipped checks do not count against confirmation
  - this layer does not decide direction or score; it only confirms execution timing
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.core.config import settings
from src.core.logger import logger
from src.exchange.data_fetcher import FullSnapshot


@dataclass
class TriggerCheck:
    name: str
    confirmed: bool
    detail: str


@dataclass
class TriggerResult:
    symbol: str
    confirmed: bool
    confirmed_count: int
    required_count: int
    checks: list[TriggerCheck]
    summary: str


class TriggerConfirmation:
    def __init__(self) -> None:
        self.cfg = settings.trigger_confirmation

    def _is_skipped(self, check: TriggerCheck) -> bool:
        return "skipped" in check.detail.lower()

    def _volume_spike(self, df: pd.DataFrame) -> TriggerCheck:
        if df is None or len(df) < 21:
            return TriggerCheck("volume_spike", False, "insufficient data — skipped")

        avg20 = float(df["volume"].iloc[-21:-1].mean())
        cur = float(df["volume"].iloc[-1])

        if avg20 <= 0:
            return TriggerCheck("volume_spike", False, "zero baseline — skipped")

        ratio = cur / avg20
        target = float(self.cfg["volume_spike_multiplier"])
        ok = ratio >= target

        return TriggerCheck(
            "volume_spike",
            ok,
            f"vol={ratio:.2f}× avg (target {target:.2f}×)",
        )

    def _oi_reaction(self, oi_change_5m: float | None) -> TriggerCheck:
        if oi_change_5m is None or oi_change_5m == 0.0:
            return TriggerCheck("oi_reaction", False, "data unavailable — skipped")

        target = float(self.cfg["oi_reaction_threshold"])
        ok = abs(oi_change_5m) >= target

        return TriggerCheck(
            "oi_reaction",
            ok,
            f"ΔOI(5m)={oi_change_5m*100:+.2f}% (target ±{target*100:.2f}%)",
        )

    def _rejection_candle(self, df: pd.DataFrame, direction: str) -> TriggerCheck:
        if df is None or len(df) < 1:
            return TriggerCheck("rejection_candle", False, "insufficient data — skipped")

        last = df.iloc[-1]

        open_ = float(last["open"])
        close = float(last["close"])
        high = float(last["high"])
        low = float(last["low"])

        upper_wick = high - max(close, open_)
        lower_wick = min(close, open_) - low
        rng = high - low
        body = abs(close - open_)

        if rng <= 0:
            return TriggerCheck("rejection_candle", False, "no range — skipped")

        wick = upper_wick if direction == "SHORT" else lower_wick
        wick_range_ratio = wick / rng
        wick_body_ratio = wick / max(body, 1e-9)

        range_target = float(self.cfg["rejection_wick_ratio"])
        body_target = float(self.cfg.get("rejection_wick_body_ratio", 1.5))

        ok = (
            wick_range_ratio >= range_target
            and wick_body_ratio >= body_target
        )

        wick_name = "upper" if direction == "SHORT" else "lower"
        return TriggerCheck(
            "rejection_candle",
            ok,
            (
                f"{wick_name} wick/range={wick_range_ratio:.0%} "
                f"(target {range_target:.0%}), "
                f"wick/body={wick_body_ratio:.2f} "
                f"(target {body_target:.2f})"
            ),
        )

    def _cvd_divergence(self, snap: FullSnapshot, direction: str) -> TriggerCheck:
        """
        CVD divergence is optional confirmation.

        Ratio meaning:
          ratio > 1.0 => buyers dominant
          ratio < 1.0 => sellers dominant

        Confirmation cases:
          LONG  + sellers dominant => divergence confirmation
          SHORT + buyers dominant  => divergence confirmation

          LONG  + buyers dominant  => flow-alignment confirmation
          SHORT + sellers dominant => flow-alignment confirmation

        Neutral / unavailable data is skipped.
        """

        buy_vol = float(getattr(snap, "taker_buy_volume", 1.0) or 1.0)
        sell_vol = float(getattr(snap, "taker_sell_volume", 1.0) or 1.0)

        if buy_vol == 1.0 and sell_vol == 1.0:
            return TriggerCheck("cvd_divergence", False, "data unavailable — skipped")

        ratio = buy_vol / max(sell_vol, 1e-9)

        if direction == "LONG" and ratio < 1.0:
            return TriggerCheck(
                "cvd_divergence",
                True,
                f"CVD divergence: LONG signal but sell dominance ratio={ratio:.3f} (<1.0)",
            )

        if direction == "SHORT" and ratio > 1.0:
            return TriggerCheck(
                "cvd_divergence",
                True,
                f"CVD divergence: SHORT signal but buy dominance ratio={ratio:.3f} (>1.0)",
            )

        if direction == "LONG" and ratio > 1.0:
            return TriggerCheck(
                "cvd_flow_alignment",
                True,
                f"CVD alignment: LONG signal with buy dominance ratio={ratio:.3f} (>1.0)",
            )

        if direction == "SHORT" and ratio < 1.0:
            return TriggerCheck(
                "cvd_flow_alignment",
                True,
                f"CVD alignment: SHORT signal with sell dominance ratio={ratio:.3f} (<1.0)",
            )

        return TriggerCheck(
            "cvd_neutral",
            False,
            f"CVD neutral for {direction} (ratio={ratio:.3f}) — skipped",
        )


    def _has_strong_heat(self, decision, direction: str) -> bool:
        """Detect strong aligned Heat Density from DecisionEngine reasoning."""
        direction = str(direction or "").upper()
        reasoning = list(getattr(decision, "reasoning", []) or [])

        for line in reasoning:
            s = str(line or "")

            if not s.startswith("Heat Density:"):
                continue

            if direction == "SHORT" and "STRONG_SHORT_HEAT" in s and "boost=+" in s:
                return True

            if direction == "LONG" and "STRONG_LONG_HEAT" in s and "boost=+" in s:
                return True

        return False

    def _has_cvd_confirmation(self, checks: list[TriggerCheck]) -> bool:
        """CVD divergence/alignment can act as elite timing confirmation."""
        for check in checks or []:
            name = str(getattr(check, "name", "") or "")
            if name.startswith("cvd_") and bool(getattr(check, "confirmed", False)):
                return True
        return False

    def _apply_elite_trigger(
        self,
        *,
        decision,
        direction: str,
        checks: list[TriggerCheck],
        confirmed: bool,
        summary: str,
    ) -> tuple[bool, str]:
        """Elite trigger rule.

        Allows execution when:
        - classic trigger is not already confirmed
        - decision.score >= elite_min_score
        - strong aligned Heat Density exists
        - CVD confirms divergence or flow alignment

        This does not change direction or score.
        """
        if confirmed:
            return confirmed, summary

        if decision is None:
            return confirmed, summary

        direction = str(direction or "").upper()
        if direction not in {"LONG", "SHORT"}:
            return confirmed, summary

        elite_cfg = self.cfg.get("elite_trigger", {}) if hasattr(self.cfg, "get") else {}
        enabled = bool(elite_cfg.get("enabled", True))
        if not enabled:
            return confirmed, summary

        min_score = float(elite_cfg.get("min_score", 70.0))
        score = float(getattr(decision, "score", 0.0) or 0.0)

        if score < min_score:
            return confirmed, summary

        strong_heat = self._has_strong_heat(decision, direction)
        cvd_ok = self._has_cvd_confirmation(checks)

        if not (strong_heat and cvd_ok):
            return confirmed, summary

        summary = (
            f"{summary} | ELITE_TRIGGER=PASS "
            f"(score={score:.1f}, strong_heat=True, cvd=True)"
        )

        try:
            decision.reasoning.append(
                f"Elite Trigger: PASS (score={score:.1f}, strong heat + CVD confirmation)"
            )
        except Exception:
            pass

        return True, summary


    def check(
        self,
        snap: FullSnapshot,
        direction: str,
        oi_change_5m: float | None = None,
        decision=None,
    ) -> TriggerResult:
        df_15m = snap.klines_15m

        checks = [
            self._volume_spike(df_15m),
            self._oi_reaction(oi_change_5m),
            self._rejection_candle(df_15m, direction),
            self._cvd_divergence(snap, direction),
        ]

        confirmed_count = sum(1 for c in checks if c.confirmed)
        skipped_count = sum(1 for c in checks if self._is_skipped(c))
        available_count = len(checks) - skipped_count

        required_cfg = int(self.cfg["required_confirmations"])
        required = max(1, min(required_cfg, available_count)) if available_count > 0 else 1

        confirmed = confirmed_count >= required

        passed = [c.name for c in checks if c.confirmed]
        skipped = [c.name for c in checks if self._is_skipped(c)]
        failed = [
            c.name
            for c in checks
            if (not c.confirmed and not self._is_skipped(c))
        ]

        summary = (
            f"{confirmed_count}/{available_count} confirmations "
            f"(need {required} from {available_count} available)"
        )
        if skipped_count:
            summary += f" [{skipped_count} skipped]"

        summary += (
            f" | PASS={','.join(passed) if passed else '-'}"
            f" | FAIL={','.join(failed) if failed else '-'}"
            f" | SKIP={','.join(skipped) if skipped else '-'}"
        )

        confirmed, summary = self._apply_elite_trigger(
            decision=decision,
            direction=direction,
            checks=checks,
            confirmed=confirmed,
            summary=summary,
        )

        try:
            details = " ; ".join(
                f"{c.name}={'PASS' if c.confirmed else 'SKIP' if self._is_skipped(c) else 'FAIL'}"
                f" [{c.detail}]"
                for c in checks
            )
            logger.info(
                f"TRIGGER {snap.symbol} {direction}: {summary} :: {details}"
            )
        except Exception:
            pass

        return TriggerResult(
            symbol=snap.symbol,
            confirmed=confirmed,
            confirmed_count=confirmed_count,
            required_count=required,
            checks=checks,
            summary=summary,
        )