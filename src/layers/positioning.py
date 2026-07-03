from __future__ import annotations

from dataclasses import dataclass, field

from src.core.config import settings
from src.core.database import MarketState
from src.exchange.data_fetcher import FullSnapshot


@dataclass
class PositioningResult:
    symbol: str
    state: MarketState
    confidence: float
    description: str
    bias_direction: str
    bias_strength: float = 0.0
    crowding_side: str = "BALANCED"
    squeeze_type: str = "NONE"
    vwap_alignment: str = "NEUTRAL"
    vwap_distance_15m_pct: float = 0.0
    vwap_distance_1h_pct: float = 0.0
    vwap_distance_4h_pct: float = 0.0
    reasoning: list[str] = field(default_factory=list)

    # Telemetry only — old logic must never drive final decision
    old_bias_direction: str = "NEUTRAL"
    old_bias_strength: float = 0.0
    old_state: str = "NO_SETUP"
    old_description: str = ""

    ls_global_long_pct: float = 50.0
    ls_global_short_pct: float = 50.0
    ls_top_position_long_pct: float = 50.0
    ls_top_position_short_pct: float = 50.0
    ls_top_account_long_pct: float = 50.0
    ls_top_account_short_pct: float = 50.0

    authority_score: float = 0.0
    funding_score: float = 0.0
    ls_position_score: float = 0.0
    oi_score: float = 0.0
    vwap_score: float = 0.0
    ls_account_score: float = 0.0


class PositioningAnalyzer:
    def __init__(self) -> None:
        self.cfg = settings.positioning

        # New configurable authority-score mode.
        # If strict_gate_enabled=True, old Hunter strict gate remains active.
        self.strict_gate_enabled = bool(self.cfg.get("strict_gate_enabled", True))
        self.authority_weights = self.cfg.get("authority_weights", {}) or {}
        self.vwap_weights = self.cfg.get("vwap_weights", {}) or {}
        self.thresholds = self.cfg.get("thresholds", {}) or {}

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self.cfg.get(key, default))
        except (TypeError, ValueError, AttributeError):
            return default

    def _calc_vwap(self, df) -> float | None:
        if df is None or df.empty:
            return None

        required = {"high", "low", "close", "volume"}
        if not required.issubset(df.columns):
            return None

        work = df.dropna(subset=["high", "low", "close", "volume"]).copy()
        if work.empty:
            return None

        volume_sum = float(work["volume"].sum())
        if volume_sum <= 0:
            return None

        typical_price = (work["high"] + work["low"] + work["close"]) / 3.0
        return float((typical_price * work["volume"]).sum() / volume_sum)

    def _price_vs_vwap(
        self,
        price: float,
        vwap: float | None,
        near_pct: float,
    ) -> tuple[str, float]:
        if vwap is None or vwap <= 0:
            return "NEUTRAL", 0.0

        distance_pct = (price - vwap) / vwap

        if abs(distance_pct) <= near_pct:
            return "PENDING", distance_pct * 100.0

        return ("LONG" if price > vwap else "SHORT"), distance_pct * 100.0

    def _resolve_vwap_alignment(self, alignments: list[str]) -> str:
        long_count = sum(1 for x in alignments if x == "LONG")
        short_count = sum(1 for x in alignments if x == "SHORT")
        pending_count = sum(1 for x in alignments if x == "PENDING")

        if long_count >= 2:
            return "LONG"

        if short_count >= 2:
            return "SHORT"

        if pending_count >= 1:
            return "PENDING"

        return "NEUTRAL"

    def _build_base_reasoning(
        self,
        funding: float,
        tf_alignments: dict[str, str],
        snap: FullSnapshot,
    ) -> list[str]:
        return [
            f"Funding {funding * 100:.4f}%",
            (
                "LS Global Account "
                f"L={snap.ls_global_account.long_pct:.1f}% "
                f"S={snap.ls_global_account.short_pct:.1f}% "
                f"(ratio={snap.ls_global_account.ratio:.2f})"
            ),
            (
                "LS Top Position "
                f"L={snap.ls_top_position.long_pct:.1f}% "
                f"S={snap.ls_top_position.short_pct:.1f}% "
                f"(ratio={snap.ls_top_position.ratio:.2f})"
            ),
            (
                "LS Top Account "
                f"L={snap.ls_top_account.long_pct:.1f}% "
                f"S={snap.ls_top_account.short_pct:.1f}% "
                f"(ratio={snap.ls_top_account.ratio:.2f})"
            ),
            (
                "VWAP alignment "
                f"15m={tf_alignments['15m']}, "
                f"1h={tf_alignments['1h']}, "
                f"4h={tf_alignments['4h']}"
            ),
        ]

    def _legacy_telemetry(
        self,
        snap: FullSnapshot,
        oi_change_4h_pct: float,
        vwap_alignment: str,
    ) -> tuple[MarketState, float, str, str, float, str, str, list[str]]:
        """
        Legacy logic is telemetry only.
        Its output must never decide final bias_direction/state/confidence.
        """
        funding = snap.funding_rate
        ls_g = snap.ls_ratio_global
        ls_t = snap.ls_ratio_top

        f_high = self._cfg_float("funding_high_threshold", 0.0003)
        f_low = self._cfg_float("funding_low_threshold", -0.0003)
        ls_long_crowded = self._cfg_float("ls_crowded_long", 2.0)
        ls_short_crowded = self._cfg_float("ls_crowded_short", 0.6)

        oi_rising = oi_change_4h_pct > 0.02
        oi_falling = oi_change_4h_pct < -0.02

        if (ls_g > 1.5 and ls_t < 0.8) or (ls_g < 0.7 and ls_t > 1.3):
            old_bias = "BEARISH" if ls_g > ls_t else "BULLISH"
            crowding_side = "LONG" if ls_g > ls_t else "SHORT"
            squeeze_type = (
                "LONG_LIQUIDATION"
                if old_bias == "BEARISH"
                else "SHORT_SQUEEZE"
            )
            return (
                MarketState.SMART_MONEY_DIVERGENCE,
                0.70,
                f"Retail LS={ls_g:.2f} vs Top-traders LS={ls_t:.2f} — fade retail",
                old_bias,
                0.70,
                crowding_side,
                squeeze_type,
                ["Legacy: retail positioning diverges from top traders → fade crowded side"],
            )

        is_extreme_long = funding >= f_high * 1.5 and ls_g >= ls_long_crowded * 1.1
        if funding >= f_high and ls_g >= ls_long_crowded and (oi_rising or is_extreme_long):
            confidence = min(0.6 + max((funding / f_high - 1), 0) * 0.1, 0.95)
            return (
                MarketState.CROWDED_LONG_TRAP,
                confidence,
                (
                    f"Funding {funding*100:.3f}% | LS {ls_g:.2f} | "
                    f"OI {oi_change_4h_pct*100:+.1f}%. Longs paying to hold."
                ),
                "BEARISH",
                min(confidence + 0.05, 0.98),
                "LONG",
                "LONG_LIQUIDATION",
                ["Legacy: positive funding + long crowding → downside liquidation risk"],
            )

        is_extreme_short = (
            abs(funding) >= abs(f_low) * 1.5
            and ls_g <= (ls_short_crowded * 0.9)
            and funding < 0
        )
        if funding <= f_low and ls_g <= ls_short_crowded and (oi_rising or is_extreme_short):
            confidence = min(0.6 + max((abs(funding) / abs(f_low) - 1), 0) * 0.1, 0.95)
            return (
                MarketState.SHORT_SQUEEZE_SETUP,
                confidence,
                (
                    f"Funding {funding*100:.3f}% | LS {ls_g:.2f} | "
                    f"OI {oi_change_4h_pct*100:+.1f}%. Shorts loaded."
                ),
                "BULLISH",
                min(confidence + 0.05, 0.98),
                "SHORT",
                "SHORT_SQUEEZE",
                ["Legacy: negative funding + short crowding → squeeze probability higher"],
            )

        if abs(funding) >= abs(f_high) * 1.5 and oi_falling:
            old_bias = "BEARISH" if funding > 0 else "BULLISH"
            crowding_side = "LONG" if funding > 0 else "SHORT"
            squeeze_type = (
                "LONG_LIQUIDATION"
                if funding > 0
                else "SHORT_SQUEEZE"
            )
            return (
                MarketState.EXHAUSTION,
                0.70,
                f"Extreme funding {funding*100:.3f}% + OI dropping → unwinding phase",
                old_bias,
                0.72,
                crowding_side,
                squeeze_type,
                ["Legacy: extreme funding + OI decline → crowded move may be unwinding"],
            )

        if abs(funding) < abs(f_high) * 0.3 and oi_rising and 0.6 < ls_g < 1.5:
            return (
                MarketState.ACCUMULATION,
                0.55,
                f"Calm funding, OI building {oi_change_4h_pct*100:+.1f}% — accumulation phase",
                "NEUTRAL",
                0.45,
                "BALANCED",
                "NONE",
                [f"Legacy: accumulation / build-up phase, VWAP context={vwap_alignment}"],
            )

        return (
            MarketState.NO_SETUP,
            0.0,
            "No clear legacy positioning signal",
            "NEUTRAL",
            0.0,
            "BALANCED",
            "NONE",
            ["Legacy: no clean funding/LS/OI positioning narrative detected"],
        )


    def _weight(self, group: dict, key: str, default: float) -> float:
        try:
            return float(group.get(key, default))
        except (TypeError, ValueError, AttributeError):
            return default

    def _threshold(self, key: str, default: float) -> float:
        try:
            return float(self.thresholds.get(key, default))
        except (TypeError, ValueError, AttributeError):
            return default

    def _funding_component(
        self,
        funding: float,
        target_direction: str,
    ) -> tuple[float, list[str]]:
        """Funding score.

        LONG setup wants negative funding => shorts paying.
        SHORT setup wants positive funding => longs paying.
        """
        max_points = self._weight(self.authority_weights, "funding", 25.0)
        threshold = abs(self._cfg_float("funding_high_threshold", 0.0003))

        if threshold <= 0:
            threshold = 0.0003

        if target_direction == "LONG":
            aligned_value = max(0.0, -funding)
            label = "Funding < 0"
        else:
            aligned_value = max(0.0, funding)
            label = "Funding > 0"

        ratio = min(aligned_value / threshold, 1.0)
        points = max_points * ratio

        return points, [
            f"{label}: {funding*100:+.4f}% => {points:.1f}/{max_points:.1f}"
        ]

    def _ls_position_component(
        self,
        snap: FullSnapshot,
        target_direction: str,
    ) -> tuple[float, list[str]]:
        """Top position L/S score.

        This is the closest LS source to contract-size crowding.
        LONG setup wants shorts crowded.
        SHORT setup wants longs crowded.
        """
        max_points = self._weight(self.authority_weights, "ls_position", 20.0)

        min_short_pct_for_long = self._cfg_float("min_short_pct_for_long", 70.0)
        min_long_pct_for_short = self._cfg_float("min_long_pct_for_short", 70.0)

        if target_direction == "LONG":
            pct = float(getattr(snap.ls_top_position, "short_pct", 50.0) or 50.0)
            threshold = min_short_pct_for_long
            side = "Top position SHORT"
        else:
            pct = float(getattr(snap.ls_top_position, "long_pct", 50.0) or 50.0)
            threshold = min_long_pct_for_short
            side = "Top position LONG"

        if threshold <= 50:
            threshold = 70.0

        ratio = max(0.0, min((pct - 50.0) / (threshold - 50.0), 1.0))
        points = max_points * ratio

        return points, [
            f"{side}: {pct:.1f}% threshold={threshold:.1f}% => {points:.1f}/{max_points:.1f}"
        ]

    def _oi_component(
        self,
        oi_change_4h_pct: float,
    ) -> tuple[float, list[str]]:
        """OI expansion score.

        Only rising OI supports crowding buildup.
        Falling OI means unwind and gets zero authority points.
        """
        max_points = self._weight(self.authority_weights, "oi_expansion", 15.0)
        full_threshold = self._cfg_float("oi_expansion_full_score_pct", 0.08)

        if full_threshold <= 0:
            full_threshold = 0.08

        rising = max(0.0, float(oi_change_4h_pct or 0.0))
        ratio = min(rising / full_threshold, 1.0)
        points = max_points * ratio

        return points, [
            f"OI expansion 4H: {oi_change_4h_pct*100:+.1f}% => {points:.1f}/{max_points:.1f}"
        ]

    def _vwap_component(
        self,
        tf_alignments: dict[str, str],
        target_direction: str,
    ) -> tuple[float, list[str]]:
        """VWAP context score per timeframe."""
        mapping = {
            "15m": ("vwap_15m", self._weight(self.vwap_weights, "vwap_15m", 10.0)),
            "1h": ("vwap_1h", self._weight(self.vwap_weights, "vwap_1h", 10.0)),
            "4h": ("vwap_4h", self._weight(self.vwap_weights, "vwap_4h", 10.0)),
        }

        points = 0.0
        notes: list[str] = []

        for tf, (key, max_points) in mapping.items():
            align = tf_alignments.get(tf, "NEUTRAL")
            add = max_points if align == target_direction else 0.0
            points += add
            notes.append(
                f"VWAP {tf}: {align} vs target={target_direction} => {add:.1f}/{max_points:.1f}"
            )

        return points, notes

    def _authority_score_for_direction(
        self,
        snap: FullSnapshot,
        oi_change_4h_pct: float,
        tf_alignments: dict[str, str],
        target_direction: str,
    ) -> tuple[float, dict[str, float], list[str]]:
        funding_points, funding_notes = self._funding_component(
            snap.funding_rate,
            target_direction,
        )
        ls_points, ls_notes = self._ls_position_component(
            snap,
            target_direction,
        )
        oi_points, oi_notes = self._oi_component(oi_change_4h_pct)
        vwap_points, vwap_notes = self._vwap_component(
            tf_alignments,
            target_direction,
        )

        total = funding_points + ls_points + oi_points + vwap_points
        components = {
            "funding_score": funding_points,
            "ls_position_score": ls_points,
            "oi_score": oi_points,
            "vwap_score": vwap_points,
            "authority_score": total,
        }

        notes = [
            f"---- AUTHORITY SCORE {target_direction} ----",
            *funding_notes,
            *ls_notes,
            *oi_notes,
            *vwap_notes,
            f"Authority total {target_direction}: {total:.1f}",
        ]

        return total, components, notes

    def _ls_account_component(
        self,
        snap: FullSnapshot,
        target_direction: str,
    ) -> tuple[float, list[str]]:
        execution_cfg = settings.decision_engine.get("execution_weights", {}) or {}
        try:
            max_points = float(execution_cfg.get("ls_account", 4.0))
        except (TypeError, ValueError, AttributeError):
            max_points = 4.0

        min_short_pct_for_long = self._cfg_float("min_short_pct_for_long", 66.0)
        min_long_pct_for_short = self._cfg_float("min_long_pct_for_short", 66.0)

        if target_direction == "LONG":
            pct = float(getattr(snap.ls_top_account, "short_pct", 50.0) or 50.0)
            threshold = min_short_pct_for_long
            side = "Top account SHORT"
        else:
            pct = float(getattr(snap.ls_top_account, "long_pct", 50.0) or 50.0)
            threshold = min_long_pct_for_short
            side = "Top account LONG"

        if threshold <= 50:
            threshold = 66.0

        ratio = max(0.0, min((pct - 50.0) / (threshold - 50.0), 1.0))
        points = max_points * ratio

        return points, [
            f"{side}: {pct:.1f}% threshold={threshold:.1f}% => LS_ACCOUNT bonus {points:.1f}/{max_points:.1f}"
        ]


    def _apply_authority_score_strategy(
        self,
        snap: FullSnapshot,
        result: PositioningResult,
        oi_change_4h_pct: float,
        tf_alignments: dict[str, str],
    ) -> PositioningResult:
        long_score, long_comp, long_notes = self._authority_score_for_direction(
            snap=snap,
            oi_change_4h_pct=oi_change_4h_pct,
            tf_alignments=tf_alignments,
            target_direction="LONG",
        )
        short_score, short_comp, short_notes = self._authority_score_for_direction(
            snap=snap,
            oi_change_4h_pct=oi_change_4h_pct,
            tf_alignments=tf_alignments,
            target_direction="SHORT",
        )

        signal_threshold = self._threshold("signal", 60.0)
        watch_threshold = self._threshold("watch", 45.0)
        strong_threshold = self._threshold("strong", 80.0)

        result.reasoning.append("---- AUTHORITY SCORE MODE ----")
        result.reasoning.append(f"strict_gate_enabled={self.strict_gate_enabled}")
        result.reasoning.extend(long_notes)
        result.reasoning.extend(short_notes)
        result.reasoning.append(
            f"Thresholds: watch={watch_threshold:.1f}, signal={signal_threshold:.1f}, strong={strong_threshold:.1f}"
        )

        if long_score >= short_score:
            best_direction = "LONG"
            best_score = long_score
            best_comp = long_comp
        else:
            best_direction = "SHORT"
            best_score = short_score
            best_comp = short_comp

        ls_account_points, ls_account_notes = self._ls_account_component(
            snap=snap,
            target_direction=best_direction,
        )
        result.reasoning.extend(ls_account_notes)

        result.authority_score = float(best_comp.get("authority_score", best_score))
        result.funding_score = float(best_comp.get("funding_score", 0.0))
        result.ls_position_score = float(best_comp.get("ls_position_score", 0.0))
        result.oi_score = float(best_comp.get("oi_score", 0.0))
        result.vwap_score = float(best_comp.get("vwap_score", 0.0))
        result.ls_account_score = float(ls_account_points)

        if best_score < signal_threshold:
            result.bias_direction = "NEUTRAL"
            result.state = MarketState.NO_SETUP
            result.description = (
                f"Authority score not passed: best={best_direction} {best_score:.1f} < signal {signal_threshold:.1f}"
            )
            result.bias_strength = 0.0
            result.confidence = 0.0
            result.crowding_side = "BALANCED"
            result.squeeze_type = "NONE"
            return result

        if best_direction == "LONG":
            result.bias_direction = "BULLISH"
            result.state = MarketState.SHORT_SQUEEZE_SETUP
            result.description = f"Authority score passed LONG: {best_score:.1f}"
            result.crowding_side = "SHORT"
            result.squeeze_type = "SHORT_SQUEEZE"
        else:
            result.bias_direction = "BEARISH"
            result.state = MarketState.CROWDED_LONG_TRAP
            result.description = f"Authority score passed SHORT: {best_score:.1f}"
            result.crowding_side = "LONG"
            result.squeeze_type = "LONG_LIQUIDATION"

        denom = max(strong_threshold, 1.0)
        result.bias_strength = max(0.0, min(best_score / denom, 1.0))
        result.confidence = result.bias_strength

        result.reasoning.append(
            f"Authority selected {best_direction}: score={best_score:.1f}, "
            f"strength={result.bias_strength:.2f}, ls_account_bonus={result.ls_account_score:.1f}"
        )
        return result



    def _apply_strict_strategy_gate(
        self,
        snap: FullSnapshot,
        result: PositioningResult,
        tf_alignments: dict[str, str],
    ) -> PositioningResult:
        """
        The only decision-authority gate.

        LONG:
          funding < 0
          shorts >= configured threshold in all 3 LS sources
          price > VWAP in at least 2 of 3 timeframes

        SHORT:
          funding > 0
          longs >= configured threshold in all 3 LS sources
          price < VWAP in at least 2 of 3 timeframes
        """
        min_short_pct_for_long = self._cfg_float("min_short_pct_for_long", 70.0)
        min_long_pct_for_short = self._cfg_float("min_long_pct_for_short", 70.0)

        global_short_ok = snap.ls_global_account.short_pct >= min_short_pct_for_long
        top_pos_short_ok = snap.ls_top_position.short_pct >= min_short_pct_for_long
        top_acc_short_ok = snap.ls_top_account.short_pct >= min_short_pct_for_long

        global_long_ok = snap.ls_global_account.long_pct >= min_long_pct_for_short
        top_pos_long_ok = snap.ls_top_position.long_pct >= min_long_pct_for_short
        top_acc_long_ok = snap.ls_top_account.long_pct >= min_long_pct_for_short

        vwap_long_ok = (
            sum(1 for tf in ("15m", "1h", "4h") if tf_alignments[tf] == "LONG") >= 2
        )
        vwap_short_ok = (
            sum(1 for tf in ("15m", "1h", "4h") if tf_alignments[tf] == "SHORT") >= 2
        )

        strict_reasons = [
            (
                "Strict gate thresholds "
                f"short_for_long>={min_short_pct_for_long:.1f}% "
                f"long_for_short>={min_long_pct_for_short:.1f}%"
            )
        ]

        long_gate = (
            snap.funding_rate < 0
            and global_short_ok
            and top_pos_short_ok
            and top_acc_short_ok
            and vwap_long_ok
        )

        short_gate = (
            snap.funding_rate > 0
            and global_long_ok
            and top_pos_long_ok
            and top_acc_long_ok
            and vwap_short_ok
        )

        if long_gate:
            result.bias_direction = "BULLISH"
            result.state = MarketState.SHORT_SQUEEZE_SETUP
            result.description = (
                "Strict gate passed: funding negative, shorts crowded across all LS sources, "
                "price above VWAP on 2 of 3 timeframes"
            )
            result.bias_strength = 0.95
            result.confidence = 0.95
            result.crowding_side = "SHORT"
            result.squeeze_type = "SHORT_SQUEEZE"
            strict_reasons.extend(
                [
                    "Funding < 0",
                    "Global account shorts exceed configured threshold",
                    "Top position shorts exceed configured threshold",
                    "Top account shorts exceed configured threshold",
                    "Price > VWAP on at least 2 of 3 timeframes",
                ]
            )

        elif short_gate:
            result.bias_direction = "BEARISH"
            result.state = MarketState.CROWDED_LONG_TRAP
            result.description = (
                "Strict gate passed: funding positive, longs crowded across all LS sources, "
                "price below VWAP on 2 of 3 timeframes"
            )
            result.bias_strength = 0.95
            result.confidence = 0.95
            result.crowding_side = "LONG"
            result.squeeze_type = "LONG_LIQUIDATION"
            strict_reasons.extend(
                [
                    "Funding > 0",
                    "Global account longs exceed configured threshold",
                    "Top position longs exceed configured threshold",
                    "Top account longs exceed configured threshold",
                    "Price < VWAP on at least 2 of 3 timeframes",
                ]
            )

        else:
            result.bias_direction = "NEUTRAL"
            result.state = MarketState.NO_SETUP
            result.description = "Strict gate not passed"
            result.bias_strength = 0.0
            result.confidence = 0.0
            result.crowding_side = "BALANCED"
            result.squeeze_type = "NONE"
            strict_reasons.extend(
                [
                    f"Funding={snap.funding_rate * 100:.4f}%",
                    (
                        "Global account "
                        f"L={snap.ls_global_account.long_pct:.1f}% "
                        f"S={snap.ls_global_account.short_pct:.1f}%"
                    ),
                    (
                        "Top position "
                        f"L={snap.ls_top_position.long_pct:.1f}% "
                        f"S={snap.ls_top_position.short_pct:.1f}%"
                    ),
                    (
                        "Top account "
                        f"L={snap.ls_top_account.long_pct:.1f}% "
                        f"S={snap.ls_top_account.short_pct:.1f}%"
                    ),
                    (
                        "VWAP votes "
                        f"15m={tf_alignments['15m']}, "
                        f"1h={tf_alignments['1h']}, "
                        f"4h={tf_alignments['4h']}"
                    ),
                ]
            )

        result.reasoning.append("---- STRICT GATE ----")
        result.reasoning.extend(strict_reasons)
        return result

    def analyze(
        self,
        snap: FullSnapshot,
        oi_change_4h_pct: float = 0.0,
    ) -> PositioningResult:
        if oi_change_4h_pct == 0.0 and getattr(snap, "oi_change_4h_pct", None) is not None:
            oi_change_4h_pct = snap.oi_change_4h_pct

        near_vwap_pct = self._cfg_float("vwap_near_threshold_pct", 0.25) / 100.0

        vwap_15m = self._calc_vwap(snap.klines_15m)
        vwap_1h = self._calc_vwap(snap.klines_1h)
        vwap_4h = self._calc_vwap(snap.klines_4h)

        tf_15m, dist_15m = self._price_vs_vwap(snap.price, vwap_15m, near_vwap_pct)
        tf_1h, dist_1h = self._price_vs_vwap(snap.price, vwap_1h, near_vwap_pct)
        tf_4h, dist_4h = self._price_vs_vwap(snap.price, vwap_4h, near_vwap_pct)

        tf_alignments = {"15m": tf_15m, "1h": tf_1h, "4h": tf_4h}
        vwap_alignment = self._resolve_vwap_alignment([tf_15m, tf_1h, tf_4h])

        (
            old_state,
            old_confidence,
            old_description,
            old_bias_direction,
            old_bias_strength,
            old_crowding_side,
            old_squeeze_type,
            old_extra_reasons,
        ) = self._legacy_telemetry(snap, oi_change_4h_pct, vwap_alignment)

        reasoning = self._build_base_reasoning(snap.funding_rate, tf_alignments, snap)
        reasoning.append("---- LEGACY TELEMETRY ----")
        reasoning.extend(old_extra_reasons)

        result = PositioningResult(
            symbol=snap.symbol,

            # Final operational state starts neutral.
            # Strict gate below is the only layer allowed to change these.
            state=MarketState.NO_SETUP,
            confidence=0.0,
            description="Strict gate not evaluated yet",
            bias_direction="NEUTRAL",
            bias_strength=0.0,
            crowding_side="BALANCED",
            squeeze_type="NONE",

            vwap_alignment=vwap_alignment,
            vwap_distance_15m_pct=dist_15m,
            vwap_distance_1h_pct=dist_1h,
            vwap_distance_4h_pct=dist_4h,
            reasoning=reasoning,

            # Old logic stored only as telemetry
            old_bias_direction=old_bias_direction,
            old_bias_strength=max(0.0, min(old_bias_strength, 1.0)),
            old_state=old_state.name if hasattr(old_state, "name") else str(old_state),
            old_description=old_description,

            ls_global_long_pct=snap.ls_global_account.long_pct,
            ls_global_short_pct=snap.ls_global_account.short_pct,
            ls_top_position_long_pct=snap.ls_top_position.long_pct,
            ls_top_position_short_pct=snap.ls_top_position.short_pct,
            ls_top_account_long_pct=snap.ls_top_account.long_pct,
            ls_top_account_short_pct=snap.ls_top_account.short_pct,
        )

        if self.strict_gate_enabled:
            return self._apply_strict_strategy_gate(snap, result, tf_alignments)

        return self._apply_authority_score_strategy(
            snap=snap,
            result=result,
            oi_change_4h_pct=oi_change_4h_pct,
            tf_alignments=tf_alignments,
        )
