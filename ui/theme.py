"""Professional dark theme — HunterBot Dashboard v2.0 (Sniper Radar Style)."""

COLORS = {
    "bg": "#0a0b0d",
    "surface": "#13151a",
    "surface_alt": "#181b21",
    "border": "rgba(255,255,255,0.07)",
    "text": "#e2e4e9",
    "text_muted": "#6b7280",
    "text_faint": "#374151",
    "accent": "#00d084",
    "accent_dim": "rgba(0,208,132,0.12)",
    "success": "#00d084",
    "danger": "#ff4d6d",
    "warning": "#fbbf24",
    "info": "#22d3ee",
    "long_bg": "rgba(0,208,132,0.08)",
    "short_bg": "rgba(255,77,109,0.08)",
}

GLOBAL_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700;800&display=swap');

:root {
  --bg: #0a0b0d;
  --bg2: #0f1012;
  --surface: #13151a;
  --surface-alt: #181b21;
  --border: rgba(255,255,255,0.07);
  --text: #e2e4e9;
  --text-muted: #6b7280;
  --text-faint: #374151;
  --accent: #00d084;
  --accent-dim: rgba(0,208,132,0.12);
  --success: #00d084;
  --danger: #ff4d6d;
  --warning: #fbbf24;
  --info: #22d3ee;
  --long-bg: rgba(0,208,132,0.08);
  --short-bg: rgba(255,77,109,0.08);
  --r: 6px;
  --font-mono: 'JetBrains Mono', monospace;
  --font-body: 'Inter', sans-serif;
}

html, body, .nicegui-content {
  background: var(--bg) !important;
  color: var(--text);
  font-family: var(--font-body);
  font-size: 15px;
  -webkit-font-smoothing: antialiased;
}
.q-page { background: var(--bg) !important; }
.app-shell { display:flex; min-height:100vh; background:var(--bg); }
.sidebar {
  width: 200px; min-width:200px; background: var(--bg2); border-right: 1px solid var(--border);
  display: flex; flex-direction:column; position: fixed; top:0; bottom:0; left:0; z-index:100; overflow-y: auto;
  transition: transform .25s ease, width .25s ease, min-width .25s ease;
}
.sidebar.sidebar--hidden { transform: translateX(-200px); width:0; min-width:0; border:none; overflow:hidden; }
.main-content { margin-left: 0; flex:1 1 auto; display:flex; flex-direction:column; min-height:100vh; width:100%; min-width:0; transition: margin-left .25s ease, width .25s ease; }
.logo-block { padding:16px 14px 12px; border-bottom:1px solid var(--border); }
.logo-title { font-family:var(--font-mono); font-size:13px; font-weight:700; color:var(--accent); letter-spacing:.04em; }
.logo-sub { font-size:9px; color:var(--text-muted); margin-top:3px; letter-spacing:.07em; text-transform:uppercase; }
.sidebar-section { padding:10px 0 4px; border-bottom:1px solid var(--border); }
.sidebar-label { font-size:9px; font-weight:700; letter-spacing:.1em; color:var(--text-faint); padding:0 14px 6px; text-transform:uppercase; }
.nav-item { display:flex; align-items:center; gap:8px; padding:7px 14px; cursor:pointer; font-size:12px; color:var(--text-muted); border-left:2px solid transparent; transition:all .15s; text-decoration:none; }
.nav-item:hover { color:var(--text); background:rgba(255,255,255,.03); }
.nav-item.active { color:var(--accent); background:var(--accent-dim); border-left-color:var(--accent); }
.topbar { background:var(--bg2); border-bottom:1px solid var(--border); padding:10px 20px; display:flex; align-items:center; justify-content:space-between; flex-shrink:0; gap:12px; position:sticky; top:0; z-index:90; }
.topbar-title { font-family:var(--font-mono); font-size:17px; font-weight:700; color:var(--accent); letter-spacing:.04em; }
.topbar-sub { font-size:12px; color:var(--text-muted); letter-spacing:.06em; text-transform:uppercase; margin-top:1px; }
.status-pill { display:flex; align-items:center; gap:5px; padding:4px 10px; background:var(--accent-dim); border:1px solid rgba(0,208,132,.2); border-radius:20px; font-size:11px; font-weight:600; color:var(--accent); font-family:var(--font-mono); }
.status-dot { width:6px; height:6px; border-radius:50%; background:var(--accent); animation: livepulse 1.5s infinite; }
@keyframes livepulse { 0%,100%{opacity:1;transform:scale(1);} 50%{opacity:.4;transform:scale(.7);} }
.uptime { font-family:var(--font-mono); font-size:11px; color:var(--text-muted); }
.page-body { flex:1; overflow-y:auto; padding:14px 18px; display:flex; flex-direction:column; gap:10px; }
.card { background:var(--surface); border:1px solid var(--border); border-radius:var(--r); padding:12px 14px; }
.card-title { font-size:12px; font-weight:700; letter-spacing:.07em; color:var(--text-muted); text-transform:uppercase; margin-bottom:10px; display:flex; align-items:center; justify-content:space-between; }
.kpi-row { display:grid; grid-template-columns:repeat(5,1fr); gap:10px; }
.kpi { background:var(--surface); border:1px solid var(--border); border-radius:var(--r); padding:12px 14px; }
.kpi-label { font-size:11px; font-weight:700; letter-spacing:.08em; color:var(--text-muted); text-transform:uppercase; margin-bottom:4px; }
.kpi-val { font-family:var(--font-mono); font-size:26px; font-weight:700; line-height:1; }
.kpi-val.green { color:var(--accent); }
.kpi-val.red { color:var(--danger); }
.kpi-val.cyan { color:var(--info); }
.kpi-val.yellow { color:var(--warning); }
.kpi-change { font-family:var(--font-mono); font-size:12px; margin-top:3px; }
.kpi-change.up { color:var(--accent); }
.kpi-change.down { color:var(--danger); }
.table-wrap { overflow-x:auto; }
.lh-table { width:100%; border-collapse:collapse; }
.lh-table thead tr { border-bottom:1px solid var(--border); }
.lh-table th { font-size:14px; font-weight:700; letter-spacing:.08em; color:var(--text-faint); text-align:left; padding:10px 12px; text-transform:uppercase; white-space:nowrap; background:var(--surface); position:sticky; top:0; }
.lh-table td { padding:10px 12px; font-family:var(--font-mono); font-size:15px; text-align:left; border-bottom:1px solid rgba(255,255,255,.03); white-space:nowrap; }
.lh-table tr:hover td { background:rgba(255,255,255,.02); }
.sym-cell { font-weight:700; color:var(--text); min-width:110px; font-size:15px; }
.mono { font-family:var(--font-mono) !important; }
.text-success { color:var(--accent) !important; }
.text-danger { color:var(--danger) !important; }
.text-muted { color:var(--text-muted); }
.text-faint { color:var(--text-faint); }
.tabular-nums { font-variant-numeric:tabular-nums; }
.pill { display:inline-flex; align-items:center; padding:4px 10px; border-radius:20px; font-size:12px; font-weight:700; letter-spacing:.04em; font-family:var(--font-mono); }
.pill-long { background:var(--long-bg); color:var(--accent); }
.pill-short { background:var(--short-bg); color:var(--danger); }
.pill-muted { background:rgba(255,255,255,.06); color:var(--text-muted); }
.pill-warn { background:rgba(251,191,36,.12); color:var(--warning); }
.pill-info-soft { background:rgba(34,211,238,.12); color:var(--info); }
.pill-open { background:rgba(0,208,132,.14); color:#34f5a2; border:1px solid rgba(0,208,132,.22); }
.pill-pending { background:rgba(251,191,36,.14); color:#ffd166; border:1px solid rgba(251,191,36,.22); }
.score-bar { height:4px; border-radius:2px; background:var(--text-faint); margin-top:4px; overflow:hidden; }
.score-bar-fill { height:100%; border-radius:2px; background:var(--accent); transition:width .4s; }
.divider { border:none; border-top:1px solid var(--border); margin:10px 0; }
.details-link { display:inline-flex; align-items:center; justify-content:center; padding:7px 12px; border-radius:999px; background:rgba(34,211,238,.10); border:1px solid rgba(34,211,238,.18); color:var(--info); text-decoration:none; font-size:12px; font-weight:700; letter-spacing:.04em; }
.details-link:hover { background:rgba(34,211,238,.16); }
.symbol-kpis { margin-bottom:2px; }
.trade-disclosure { background:var(--surface-alt); border:1px solid rgba(255,255,255,.05); border-radius:10px; margin-bottom:10px; overflow:hidden; }
.trade-disclosure summary { list-style:none; cursor:pointer; padding:14px 16px; }
.trade-disclosure summary::-webkit-details-marker { display:none; }
.trade-summary-row { display:flex; align-items:center; justify-content:space-between; gap:16px; flex-wrap:wrap; }
.trade-summary-main, .trade-summary-side { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.trade-symbol { font-family:var(--font-mono); font-size:16px; font-weight:700; color:var(--text); }
.trade-time { color:var(--text-muted); font-family:var(--font-mono); font-size:12px; }
.trade-detail-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; padding:0 16px 16px; }
.detail-block { background:rgba(255,255,255,.025); border:1px solid rgba(255,255,255,.05); border-radius:10px; padding:12px; }
.detail-block-wide { grid-column:1 / -1; }
.detail-label { color:var(--text-faint); text-transform:uppercase; letter-spacing:.06em; font-size:11px; margin-bottom:6px; }
.detail-value { color:var(--text); font-size:14px; line-height:1.7; }
.detail-chip-row { display:flex; flex-wrap:wrap; gap:8px; }
.detail-chip { display:inline-flex; gap:8px; align-items:center; border-radius:999px; padding:5px 10px; background:rgba(255,255,255,.04); color:var(--text); font-size:11px; }
.timeline-wrap { display:flex; flex-direction:column; gap:10px; }
.timeline-item { display:flex; gap:10px; align-items:flex-start; }
.timeline-dot { width:10px; height:10px; border-radius:50%; margin-top:6px; flex-shrink:0; }
.dot-created { background:var(--warning); }
.dot-open { background:var(--info); }
.dot-closed { background:var(--accent); }
.timeline-content { flex:1; }
.timeline-head { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:3px; }
.timeline-title { color:var(--text); font-weight:700; font-size:12px; }
.timeline-time { color:var(--text-muted); font-family:var(--font-mono); font-size:11px; }
.timeline-detail { color:var(--text-muted); font-size:12px; }
.signals-grid { display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:16px; width:100%; }
.signal-card {
  background: linear-gradient(180deg, rgba(12,15,24,.98) 0%, rgba(14,17,27,.98) 100%);
  border: 1px solid rgba(255,255,255,.08);
  border-top: 3px solid transparent;
  border-radius: 14px;
  padding: 14px 16px 16px;
  box-shadow: 0 12px 24px rgba(0,0,0,.18);
}
.signal-card-green { border-top-color:#00d97e; }
.signal-card-blue { border-top-color:#4ea1ff; }
.signal-card-red { border-top-color:#ff5b61; }
.signal-top { display:flex; align-items:flex-start; justify-content:space-between; gap:14px; }
.signal-head-left { display:flex; flex-direction:column; gap:8px; }
.signal-symbol-row { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
.signal-symbol { font-size:18px; font-weight:800; color:var(--text); letter-spacing:-0.01em; }
.signal-price { font-size:15px; color:#8b94a7; }
.signal-tags { display:flex; gap:8px; flex-wrap:wrap; }
.signal-score-box { min-width:110px; border:1px solid; border-radius:14px; padding:12px 14px; text-align:center; box-shadow: inset 0 0 0 1px rgba(255,255,255,.02); }
.signal-score-label { font-size:12px; letter-spacing:.10em; color:#8891a4; font-weight:700; margin-bottom:8px; }
.signal-score-value { font-size:26px; font-weight:800; line-height:1; }
.signal-divider { height:1px; background:rgba(255,255,255,.08); margin:14px 0; }
.signal-comp-grid { display:grid; grid-template-columns:repeat(5, minmax(0, 1fr)); gap:12px; }
.signal-comp-label { font-size:11px; letter-spacing:.08em; color:#8b94a7; font-weight:700; margin-bottom:8px; }
.signal-comp-value { font-size:16px; font-weight:800; margin-bottom:7px; }
.signal-comp-bar { height:4px; border-radius:999px; background:rgba(255,255,255,.09); overflow:hidden; }
.signal-comp-fill { height:100%; border-radius:999px; }
.signal-context-grid { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:12px; }
.signal-mini-label { font-size:11px; letter-spacing:.08em; color:#8b94a7; font-weight:700; margin-bottom:8px; }
.signal-mini-value { font-size:15px; color:var(--text); }
.signal-reasoning-title { font-size:12px; letter-spacing:.08em; color:#8b94a7; font-weight:800; margin-bottom:8px; }
.signal-reasoning-list { margin:0; padding-left:18px; color:#a8b0c2; font-size:14px; line-height:1.8; }
.signal-reasoning-list li { margin:0; }
::-webkit-scrollbar { width:4px; height:4px; }
::-webkit-scrollbar-track { background:var(--bg); }
::-webkit-scrollbar-thumb { background:#374151; border-radius:2px; }
@media (max-width: 1280px) {
  .signals-grid { grid-template-columns:1fr; }
}
@media (max-width: 1024px) {
  .kpi-row { grid-template-columns:repeat(2,1fr); }
  .trade-detail-grid { grid-template-columns:1fr; }
}
@media (max-width: 900px) {
  .signal-comp-grid { grid-template-columns:repeat(2, minmax(0, 1fr)); }
  .signal-context-grid { grid-template-columns:repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 640px) {
  .kpi-row { grid-template-columns:1fr; }
  .page-body { padding:12px; }
  .signal-top { flex-direction:column; }
  .signal-score-box { width:100%; }
  .signal-comp-grid, .signal-context-grid { grid-template-columns:1fr; }
}
"""
