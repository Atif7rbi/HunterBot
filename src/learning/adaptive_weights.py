"""Layer 13 — Adaptive Weights Engine."""
from __future__ import annotations
from pathlib import Path
import yaml

CONFIG_FILE = Path(__file__).resolve().parents[2] / 'config.yaml'
MIN_WEIGHT = 0.05
MAX_WEIGHT = 0.50

SETUP_PRIMARY_COMPONENT = {
    'SMART_MONEY_DIVERGENCE': 'positioning_extremity',
    'CROWDED_LONG_TRAP': 'funding_extreme',
    'SHORT_SQUEEZE_SETUP': 'funding_extreme',
    'EXHAUSTION': 'oi_behavior',
    'ACCUMULATION': 'liquidity_imbalance',
}

class AdaptiveWeightsEngine:
    def _load(self) -> dict:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)

    def _save(self, cfg: dict) -> None:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)

    @staticmethod
    def _normalize(weights: dict) -> dict:
        total = sum(weights.values())
        return {k: round(v / total, 4) for k, v in weights.items()}

    def adapt(self, matrix: dict, dry_run: bool = False) -> dict:
        cfg = self._load()
        weights = dict(cfg['decision_engine']['weights'])
        learning = cfg.setdefault('learning', {})
        thresholds = learning.setdefault('setup_threshold_overrides', {})
        disabled = learning.setdefault('disabled_setup_regimes', [])
        changes = []
        for (setup, regime), cell in matrix.items():
            verdict = cell.verdict()
            component = SETUP_PRIMARY_COMPONENT.get(setup)
            key = f'{setup}:{regime}'
            if cell.trades >= 50 and cell.win_rate < 0.30 and key not in disabled:
                disabled.append(key)
                changes.append(f'⛔ DISABLED {key} (WR={cell.win_rate*100:.1f}% n={cell.trades})')
            if not component or verdict in ('INSUFFICIENT_DATA', 'NEUTRAL', 'VALID'):
                continue
            if verdict == 'STRONG_EDGE':
                step, thresh_delta = 0.03, -3
            else:
                step, thresh_delta = -0.03, 5
            old_w = weights[component]
            new_w = max(MIN_WEIGHT, min(MAX_WEIGHT, old_w + step))
            if abs(new_w - old_w) >= 0.001:
                weights[component] = new_w
                changes.append(f"{'▲' if step > 0 else '▼'} {component:<28} {old_w:.4f} → {new_w:.4f}  [{key}]")
            old_t = thresholds.get(key, cfg['decision_engine']['min_score_to_signal'])
            new_t = max(45, min(85, old_t + thresh_delta))
            if new_t != old_t:
                thresholds[key] = new_t
                changes.append(f'  threshold {key}: {old_t} → {new_t}')
        cfg['decision_engine']['weights'] = self._normalize(weights)
        if not dry_run and changes:
            self._save(cfg)
        return {'changes': changes, 'weights': cfg['decision_engine']['weights'], 'thresholds': thresholds, 'disabled': disabled}
