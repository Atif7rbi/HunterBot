from __future__ import annotations

from collections import Counter
from html import escape as esc
from typing import Any

from nicegui import ui

from ui.components.widgets import fmt_money_short, fmt_price


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _base(symbol: str) -> str:
    return str(symbol or "—").replace("USDT", "").replace("USDC", "")


def _direction(d: dict | None) -> str:
    return str((d or {}).get("direction") or "WAIT").upper()


def _decision_map(decisions: list[dict]) -> dict[str, dict]:
    return {str(d.get("symbol") or "").upper(): d for d in decisions if d.get("symbol")}


def _raw_heat(row: dict, decision: dict | None = None) -> float:
    return max(_num((row or {}).get("extremity_score")), _num((decision or {}).get("score")))


def _heat(row: dict, decision: dict | None = None) -> float:
    return max(0.0, min(100.0, _raw_heat(row, decision)))


def _heat_label(score: float) -> tuple[str, str]:
    if score >= 80:
        return "EXTREME", "extreme"
    if score >= 60:
        return "CROWDED", "crowded"
    if score >= 40:
        return "AGGRESSIVE", "aggressive"
    if score >= 20:
        return "ACTIVE", "active"
    return "CALM", "calm"


def _funding(row: dict) -> float:
    return _num(row.get("funding_rate")) * 100.0


def _funding_cls(row: dict) -> str:
    f = _funding(row)
    if f >= 0.010:
        return "pos"
    if f <= -0.010:
        return "neg"
    return "blue"


def _ls(row: dict) -> float:
    return _num(row.get("long_short_ratio"))


def _oi_chg(row: dict) -> float:
    return _num(row.get("oi_change_4h_pct")) * 100.0


def _price(row: dict) -> float:
    return _num(row.get("price"))


def _vol(row: dict) -> float:
    return _num(row.get("volume_24h_usd"))


def _oi_usd(row: dict) -> float:
    return _num(row.get("open_interest_usd"))


def _ratio_to_pct(ratio: float | None) -> tuple[float | None, float | None]:
    if ratio is None:
        return None, None
    try:
        r = float(ratio)
    except Exception:
        return None, None
    if r <= 0:
        return None, None
    long_pct = r / (1.0 + r) * 100.0
    return long_pct, 100.0 - long_pct


def _pct_text(value: float | None) -> str:
    return "—" if value is None else f"{float(value):.2f}%"


def _ratio_value(row: dict, *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except Exception:
            continue
    return None


def _pct_pair_from_fields(row: dict, long_key: str, short_key: str) -> tuple[float | None, float | None]:
    long_value = row.get(long_key)
    short_value = row.get(short_key)
    if long_value is None or short_value is None:
        return None, None
    try:
        lp = float(long_value)
        sp = float(short_value)
    except Exception:
        return None, None
    if lp < 0 or sp < 0:
        return None, None
    total = lp + sp
    if total <= 0:
        return None, None
    if total <= 1.5:
        lp *= 100.0
        sp *= 100.0
    else:
        lp = lp / total * 100.0
        sp = sp / total * 100.0
    return lp, sp


def _ls_pack(row: dict) -> dict[str, float | None]:
    ratio = _ratio_value(row, "long_short_ratio", "ls_ratio")
    pos_ratio = _ratio_value(row, "ls_position_ratio", "long_short_position_ratio", "top_position_ratio", "ls_top_position_ratio")
    acc_ratio = _ratio_value(row, "ls_account_ratio", "long_short_account_ratio", "top_account_ratio", "ls_top_account_ratio")

    ratio_long, ratio_short = _ratio_to_pct(ratio)

    pos_long, pos_short = _ratio_to_pct(pos_ratio)
    if pos_long is None or pos_short is None:
        pos_long, pos_short = _pct_pair_from_fields(row, "ls_top_position_long_pct", "ls_top_position_short_pct")

    acc_long, acc_short = _ratio_to_pct(acc_ratio)
    if acc_long is None or acc_short is None:
        acc_long, acc_short = _pct_pair_from_fields(row, "ls_top_account_long_pct", "ls_top_account_short_pct")

    return {
        "ratio_long": ratio_long,
        "ratio_short": ratio_short,
        "pos_long": pos_long,
        "pos_short": pos_short,
        "acc_long": acc_long,
        "acc_short": acc_short,
    }


def _trap(row: dict, decision: dict | None = None) -> tuple[str, str]:
    ls = _ls(row)
    funding = _funding(row)
    oi = _oi_chg(row)
    direction = _direction(decision)
    if ls >= 1.5 and funding > 0 and oi > 0:
        return "LONG TRAP RISK", "neg"
    if 0 < ls <= 0.75 and funding < 0 and oi > 0:
        return "SHORT TRAP RISK", "pos"
    if direction in {"LONG", "SHORT"}:
        return direction, "short" if direction == "SHORT" else "long"
    if oi > 10:
        return "OI BUILDUP", "yellow"
    if abs(funding) >= 0.10:
        return "FUNDING STRESS", "yellow"
    return "NORMAL", "muted"


def _crowd(row: dict) -> tuple[str, str]:
    ratio = _ls(row)
    if ratio >= 2.5:
        return "EXTREME LONGS", "neg"
    if ratio >= 1.5:
        return "CROWDED LONGS", "neg"
    if 0 < ratio <= 0.55:
        return "EXTREME SHORTS", "pos"
    if 0 < ratio <= 0.75:
        return "CROWDED SHORTS", "pos"
    return "BALANCED", "muted"


def _coin_icon(symbol: str) -> str:
    base = _base(symbol)
    icons = {"ETH": "Ξ", "SOL": "◎", "DOGE": "Ð", "XRP": "✕", "RAVE": "R", "ICP": "∞", "PAXG": "P", "PRL": "P", "ESPORTS": "🎮"}
    return icons.get(base.upper(), base[:2].upper() or "•")


def _ls_metric_card(label: str, long_pct: float | None = None, short_pct: float | None = None) -> str:
    if long_pct is None or short_pct is None:
        body = '<b><em class="blue">—</em></b>'
    else:
        body = f'<b><em class="pos">{long_pct:.2f}%</em> / <em class="neg">{short_pct:.2f}%</em></b>'
    return f"""
    <div class="mh-ls-card">
      <span>{esc(label)}</span>
      {body}
    </div>
    """


def _top_tile(rank: int, row: dict, decision: dict | None) -> str:
    symbol = str(row.get("symbol") or "—")
    base = _base(symbol)
    heat = _heat(row, decision)
    label, label_cls = _heat_label(heat)
    trap, trap_cls = _trap(row, decision)
    crowd, crowd_cls = _crowd(row)
    ls = _ls_pack(row)
    return f"""
    <div class="mh-top-tile {label_cls}">
      <div class="mh-rank mh-coin-icon">{_coin_icon(symbol)}</div>
      <div class="mh-top-main">
        <div class="mh-coin-row">
          <div>
            <div class="mh-coin">{esc(base)}</div>
            <div class="mh-price mono">{fmt_price(_price(row)) if _price(row) else "—"}</div>
          </div>
          <div class="mh-heat-badge {label_cls}">{heat:.0f}</div>
        </div>
        <div class="mh-tags">
          <span class="mh-pill {crowd_cls}">{esc(crowd)}</span>
          <span class="mh-pill {trap_cls}">{esc(trap)}</span>
        </div>
        <div class="mh-data-list">
          <div><span>Funding</span><b class="{_funding_cls(row)}">{_funding(row):+.3f}%</b></div>
          <div><span>OI Change</span><b class="{'pos' if _oi_chg(row) >= 0 else 'neg'}">{_oi_chg(row):+.1f}%</b></div>
          <div><span>Volume</span><b>{fmt_money_short(_vol(row))}</b></div>
          <div><span>Open Interest</span><b>{fmt_money_short(_oi_usd(row))}</b></div>
        </div>
        <div class="mh-ls-grid">
          {_ls_metric_card("L/S POSIT", ls["pos_long"], ls["pos_short"])}
          {_ls_metric_card("L/S RATIO", ls["ratio_long"], ls["ratio_short"])}
          {_ls_metric_card("L/S ACCOUNT", ls["acc_long"], ls["acc_short"])}
        </div>
      </div>
    </div>
    """


def _summary_bar(scans: list[dict], decisions: list[dict]) -> str:
    dmap = _decision_map(decisions)
    avg_heat = sum(_heat(r, dmap.get(str(r.get("symbol") or "").upper())) for r in scans) / len(scans) if scans else 0.0
    label, cls = _heat_label(avg_heat)
    counts = Counter(_heat_label(_heat(r, dmap.get(str(r.get("symbol") or "").upper())))[1] for r in scans)
    return f"""
    <div class="mh-global">
      <div><span>Market Overall Heat</span><b>{avg_heat:.0f}<em>/100</em></b><strong class="{cls}">{esc(label)}</strong></div>
      <div class="mh-scale"><i style="left:{avg_heat:.1f}%"></i></div>
      <div class="mh-counts">
        <span class="calm">Calm {counts.get('calm',0)}</span>
        <span class="active">Active {counts.get('active',0)}</span>
        <span class="aggressive">Aggressive {counts.get('aggressive',0)}</span>
        <span class="crowded">Crowded {counts.get('crowded',0)}</span>
        <span class="extreme">Extreme {counts.get('extreme',0)}</span>
      </div>
    </div>
    """


def _heatmap_panel(scans: list[dict], decisions: list[dict], *, full: bool = False) -> str:
    dmap = _decision_map(decisions)
    rows = sorted(
        scans,
        key=lambda r: (_heat(r, dmap.get(str(r.get("symbol") or "").upper())), abs(_funding(r)), abs(_oi_chg(r)), _oi_usd(r)),
        reverse=True,
    )[:24 if full else 5]
    tiles = "".join(_top_tile(idx, row, dmap.get(str(row.get("symbol") or "").upper())) for idx, row in enumerate(rows, start=1))
    if not tiles:
        tiles = '<div class="mh-empty">No market scan data yet. Run a scan first.</div>'
    title = "FULL HEATMAP" if full else "HUNTER MARKET HEATMAP"
    sub = "Top scanner pressure names" if full else "Top 5 scanner pressure names"
    return f"""
    <div class="mh-panel mh-full-panel">
      <div class="mh-panel-title">{title} <span>{sub}</span></div>
      <div class="mh-panel-sub" style="margin-bottom:12px">Funding + positioning + OI + decision pressure. BTC/Follower narrative panels removed from Hunter UI.</div>
      <div class="mh-full-grid">{tiles}</div>
    </div>
    """


def _css() -> str:
    return r'''
<style>
.mh-wrap{width:100%;color:var(--text)}.mh-wrap input[type=radio]{display:none}.mh-topbar{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:12px}.mh-title{font-size:20px;font-weight:900;letter-spacing:.02em}.mh-title span{display:block;color:var(--text-muted);font-size:12px;font-weight:600;margin-top:3px}.mh-switch{display:flex;align-items:center;gap:8px}.mh-switch label{cursor:pointer;border:1px solid rgba(148,163,184,.20);background:rgba(15,23,42,.70);color:var(--text-muted);padding:8px 12px;border-radius:9px;font-size:12px;font-weight:900;letter-spacing:.04em}#mh-mode-panel:checked~.mh-topbar label[for=mh-mode-panel],#mh-mode-full:checked~.mh-topbar label[for=mh-mode-full]{color:#53a7ff;border-color:rgba(83,167,255,.55);background:rgba(83,167,255,.13)}.mh-view-full{display:none}#mh-mode-full:checked~.mh-view-panel{display:none}#mh-mode-full:checked~.mh-view-full{display:block}.mh-global{flex:1;display:grid;grid-template-columns:210px 1fr 420px;align-items:center;gap:14px;border:1px solid rgba(148,163,184,.18);background:rgba(15,23,42,.55);border-radius:12px;padding:10px 14px}.mh-global span{color:var(--text-muted);font-size:11px;font-weight:700}.mh-global b{font-size:25px;font-family:var(--font-mono);margin-right:6px}.mh-global em{color:var(--text-muted);font-style:normal;font-size:13px}.mh-global strong{font-size:11px;padding:5px 8px;border-radius:999px;background:rgba(255,255,255,.06)}.mh-scale{position:relative;height:14px;border-radius:999px;background:linear-gradient(90deg,#22c55e 0%,#facc15 35%,#f97316 60%,#ef4444 100%);box-shadow:0 0 22px rgba(239,68,68,.18)}.mh-scale i{position:absolute;top:-5px;width:6px;height:24px;border-radius:999px;background:white;box-shadow:0 0 10px rgba(255,255,255,.8)}.mh-counts{display:flex;justify-content:flex-end;gap:10px;flex-wrap:wrap;font-size:11px}.mh-panel{border:1px solid rgba(148,163,184,.20);border-radius:14px;background:linear-gradient(180deg,rgba(17,24,39,.96),rgba(8,13,23,.96));padding:14px;box-shadow:0 20px 60px rgba(0,0,0,.25)}.mh-panel-title{font-size:14px;font-weight:900;letter-spacing:.04em;margin-bottom:12px}.mh-panel-title span,.mh-panel-sub{color:var(--text-muted);font-size:11px;font-weight:600}.mh-full-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:11px}.mh-top-tile{position:relative;display:flex;gap:10px;min-height:190px;border:1px solid rgba(148,163,184,.18);border-radius:12px;background:rgba(15,23,42,.58);padding:12px;overflow:hidden}.mh-top-tile.extreme{border-color:rgba(239,68,68,.65);box-shadow:inset 0 0 0 1px rgba(239,68,68,.18)}.mh-top-tile.crowded{border-color:rgba(249,115,22,.48)}.mh-top-tile.aggressive{border-color:rgba(250,204,21,.40)}.mh-rank{width:28px;height:28px;flex-shrink:0;display:grid;place-items:center;border-radius:50%;background:radial-gradient(circle at 35% 30%,rgba(255,255,255,.20),rgba(83,167,255,.18) 45%,rgba(15,23,42,.85) 100%);border:1px solid rgba(83,167,255,.35);color:#f8fafc;font-weight:900;font-family:var(--font-mono);box-shadow:0 0 18px rgba(83,167,255,.18)}.mh-top-main{flex:1;min-width:0}.mh-coin-row{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.mh-coin{font-size:17px;font-weight:900;color:#f8fafc}.mh-price{color:var(--text-muted);font-size:11px;margin-top:4px}.mh-heat-badge{width:54px;height:54px;display:grid;place-items:center;border-radius:50%;font-family:var(--font-mono);font-size:21px;font-weight:900;border:3px solid rgba(249,115,22,.65);box-shadow:0 0 22px rgba(249,115,22,.25)}.mh-heat-badge.extreme{border-color:#ef4444;color:#ff5b61}.mh-heat-badge.crowded{border-color:#f97316;color:#fb923c}.mh-heat-badge.aggressive{border-color:#facc15;color:#facc15}.mh-heat-badge.active{border-color:#53a7ff;color:#53a7ff}.mh-heat-badge.calm{border-color:#22c55e;color:#25e783}.mh-tags{display:flex;flex-wrap:wrap;gap:7px;margin-top:10px}.mh-pill{display:inline-flex;align-items:center;border-radius:999px;padding:4px 8px;font-size:10px;font-weight:900;border:1px solid rgba(148,163,184,.16)}.mh-pill.neg,.mh-pill.short{color:#ff5b61;background:rgba(239,68,68,.11)}.mh-pill.pos,.mh-pill.long{color:#25e783;background:rgba(34,197,94,.11)}.mh-pill.yellow{color:#facc15;background:rgba(250,204,21,.11)}.mh-pill.blue{color:#53a7ff;background:rgba(83,167,255,.11)}.mh-pill.muted,.mh-pill.wait{color:#94a3b8;background:rgba(148,163,184,.10)}.mh-data-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px 10px;margin-top:11px;border-top:1px solid rgba(148,163,184,.10);padding-top:10px}.mh-data-list div{display:flex;justify-content:space-between;gap:8px;font-size:11px}.mh-data-list span{color:var(--text-muted)}.mh-data-list b{font-family:var(--font-mono);color:#f8fafc}.mh-ls-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:10px}.mh-ls-card{border:1px solid rgba(148,163,184,.15);background:rgba(15,23,42,.52);border-radius:10px;padding:9px;min-height:70px}.mh-ls-card span{display:block;color:var(--text-muted);font-size:10px;font-weight:900;letter-spacing:.05em;margin-bottom:7px}.mh-ls-card b{display:block;font-family:var(--font-mono);font-size:14px;line-height:1.2;white-space:nowrap}.mh-ls-card b em{font-style:normal}.mh-empty{color:var(--text-muted);text-align:center;padding:24px}.calm{color:#25e783!important}.active{color:#53a7ff!important}.aggressive{color:#facc15!important}.crowded{color:#f97316!important}.extreme{color:#ff5b61!important}.pos{color:#25e783!important}.neg{color:#ff5b61!important}.blue{color:#53a7ff!important}.yellow{color:#facc15!important}.mono{font-family:var(--font-mono)}
@media(max-width:900px){.mh-topbar{flex-direction:column;align-items:stretch}.mh-global{grid-template-columns:1fr}.mh-full-grid,.mh-ls-grid{grid-template-columns:1fr}}
</style>
'''


def render_market_heatmap(*, bot: Any, limit: int = 24) -> None:
    scans = list(getattr(bot, "last_scan_results", []) or [])
    decisions = list((getattr(bot, "last_decisions", {}) or {}).values())

    ui.add_head_html(_css())
    html = f'''
    <div class="mh-wrap">
      <input type="radio" name="mh-view-mode" id="mh-mode-panel" checked>
      <input type="radio" name="mh-view-mode" id="mh-mode-full">
      <div class="mh-topbar">
        <div class="mh-title">MARKET HEATMAP <span>Hunter Original only</span></div>
        {_summary_bar(scans, decisions)}
        <div class="mh-switch">
          <label for="mh-mode-panel">HUNTER HEAT</label>
          <label for="mh-mode-full">FULL HEATMAP</label>
        </div>
      </div>
      <div class="mh-view-panel">{_heatmap_panel(scans[:limit if limit else None], decisions, full=False)}</div>
      <div class="mh-view-full">{_heatmap_panel(scans[:limit if limit else None], decisions, full=True)}</div>
    </div>'''
    ui.html(html)
