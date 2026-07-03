export function createMarketEngine() {
  const stateHistory = [22,35,28,54,42,67,46,73,59,32,49,26];

  const clamp = (v,a,b) => Math.max(a, Math.min(b, v));
  const fmt2 = new Intl.NumberFormat('en-US',{maximumFractionDigits:2});

  function trueRange(curr, prevClose) {
    const a = curr.h - curr.l;
    const b = Math.abs(curr.h - prevClose);
    const c = Math.abs(curr.l - prevClose);
    return Math.max(a, b, c);
  }

  function calcATR(data, period=14) {
    if (!data || data.length < period + 1) return null;
    const trs = [];
    for (let i = 1; i < data.length; i++) trs.push(trueRange(data[i], data[i - 1].c));
    let atr = trs.slice(0, period).reduce((a,b)=>a+b,0) / period;
    for (let i = period; i < trs.length; i++) atr = ((atr * (period - 1)) + trs[i]) / period;
    return atr;
  }

  function calcADX(data, period=14) {
    if (!data || data.length < period * 2 + 2) return null;
    const trs = [], plusDM = [], minusDM = [];
    for (let i = 1; i < data.length; i++) {
      const curr = data[i], prev = data[i - 1];
      const upMove = curr.h - prev.h;
      const downMove = prev.l - curr.l;
      plusDM.push(upMove > downMove && upMove > 0 ? upMove : 0);
      minusDM.push(downMove > upMove && downMove > 0 ? downMove : 0);
      trs.push(trueRange(curr, prev.c));
    }
    let tr14 = trs.slice(0, period).reduce((a,b)=>a+b,0);
    let plus14 = plusDM.slice(0, period).reduce((a,b)=>a+b,0);
    let minus14 = minusDM.slice(0, period).reduce((a,b)=>a+b,0);
    const dxs = [];
    for (let i = period; i < trs.length; i++) {
      const plusDI = tr14 ? (plus14 / tr14) * 100 : 0;
      const minusDI = tr14 ? (minus14 / tr14) * 100 : 0;
      const dx = (plusDI + minusDI) ? (Math.abs(plusDI - minusDI) / (plusDI + minusDI)) * 100 : 0;
      dxs.push(dx);
      tr14 = tr14 - (tr14 / period) + trs[i];
      plus14 = plus14 - (plus14 / period) + plusDM[i];
      minus14 = minus14 - (minus14 / period) + minusDM[i];
    }
    if (!dxs.length) return null;
    let adx = dxs.slice(0, period).reduce((a,b)=>a+b,0) / Math.min(period, dxs.length);
    for (let i = period; i < dxs.length; i++) adx = ((adx * (period - 1)) + dxs[i]) / period;
    return adx;
  }

  function slopePct(data, lookback=10) {
    if (!data || data.length < lookback + 1) return null;
    const a = data[data.length - lookback - 1].c;
    const b = data[data.length - 1].c;
    return a ? ((b - a) / a) * 100 : null;
  }

  function choppinessProxy(data, period=14) {
    const atr = calcATR(data, period);
    if (!atr || data.length < period + 1) return null;
    const slice = data.slice(-period);
    const hh = Math.max(...slice.map(x=>x.h));
    const ll = Math.min(...slice.map(x=>x.l));
    const range = hh - ll;
    if (!range) return 100;
    return clamp((Math.log10((atr * period) / range) / Math.log10(period)) * 100, 0, 100);
  }

  function scoreState(metrics) {
    const trendPower = clamp(((metrics.adx1m||0)*0.2 + (metrics.adx5m||0)*0.35 + (metrics.adx15m||0)*0.45), 0, 100);
    const slopePower = Math.abs((metrics.slope1m||0)*0.2 + (metrics.slope5m||0)*0.35 + (metrics.slope15m||0)*0.45);
    const volPower = (metrics.atrPct1m||0)*0.2 + (metrics.atrPct5m||0)*0.35 + (metrics.atrPct15m||0)*0.45;
    const chopPower = (metrics.chop1m||55);

    const scores = {
      TRENDING: clamp(trendPower*1.2 + slopePower*18 - chopPower*0.5 + Math.max(0, metrics.breadthTrending-55)*0.35, 0, 100),
      RANGING: clamp(chopPower*1.0 + Math.max(0, 22-trendPower)*1.6 + Math.max(0, 1.2-volPower)*20, 0, 100),
      EXPANSION: clamp(volPower*26 + trendPower*0.6 + Math.abs(metrics.slope15m||0)*8, 0, 100),
      COMPRESSION: clamp(Math.max(0, 1.0-volPower)*55 + chopPower*0.5, 0, 100),
      DISTRIBUTION: clamp(Math.max(0, metrics.fundingPct)*1500 + Math.max(0, metrics.breadthFunding-65)*0.5 + Math.max(0, -metrics.oiPressure)*15 + chopPower*0.2, 0, 100),
      ACCUMULATION: clamp(Math.max(0, -metrics.fundingPct)*1500 + Math.max(0, 45-metrics.breadthFunding)*0.5 + Math.max(0, -metrics.oiPressure)*15 + chopPower*0.2, 0, 100)
    };

    if (volPower > 2.8 && trendPower > 24) scores.EXPANSION += 15;
    if (trendPower > 25 && slopePower > 1.2) scores.TRENDING += 12;
    if (volPower < 0.9) scores.COMPRESSION += 10;

    const sorted = Object.entries(scores).sort((a,b)=>b[1]-a[1]);
    const [state, top] = sorted[0];
    const second = sorted[1][1];
    const confidence = clamp(Math.round(55 + (top - second)), 50, 95);
    return { state, confidence, scores };
  }

  function regimeFromState(state, metrics) {
    if (state === 'TRENDING' && (metrics.adx15m||0) > 25) return { key:'MOMENTUM', color:'#ff3366', conf:76, impact:'UNFAVORABLE', impactColor:'#ff3366', chars:['ADX confirms directional strength','Trend persistence is high','Fades are low expectancy','Continuation logic dominates'], note:'Reduce countertrend entries and prefer continuation or wait states.' };
    if (state === 'EXPANSION' && ((metrics.atrPct5m||0) > 2.4 || (metrics.atrPct15m||0) > 3.2)) return { key:'PANIC', color:'#ff3366', conf:89, impact:'DANGEROUS', impactColor:'#ff3366', chars:['Volatility shock is active','Stop cascades become more likely','Execution quality can degrade fast','Block or heavily reduce entries'], note:'Best handled by blocking new trades until volatility cools.' };
    if ((state === 'RANGING' || state === 'DISTRIBUTION' || state === 'ACCUMULATION') && (metrics.chop1m||55) > 48) return { key:'MANIPULATION', color:'#a855f7', conf:71, impact:'FAVORABLE', impactColor:'#00ff88', chars:['Liquidity sweeps dominate','False breaks are common','Mean reversion improves','Market-maker behavior is likely'], note:'This is typically the best environment for sweep-based HunterBot logic.' };
    if (state === 'COMPRESSION') return { key:'CHOPPY', color:'#ffd700', conf:64, impact:'MODERATE', impactColor:'#ffd700', chars:['Low-volatility chop detected','Breakouts need confirmation','Signal quality is mixed','Execution should be selective'], note:'Keep frequency low until expansion confirms.' };
    return { key:'BALANCED', color:'#00d4ff', conf:58, impact:'MODERATE', impactColor:'#ffd700', chars:['Mixed conditions across timeframes','No dominant edge detected','Use neutral risk and tighter filters'], note:'Allow only high-quality setups with stronger confluence.' };
  }

  function permissionFromState(state, regime, confidence, riskScore) {
    if (regime.key === 'PANIC' || state === 'EXPANSION' || riskScore >= 78) return 'blocked';
    if (state === 'TRENDING' && regime.key === 'MOMENTUM') return 'blocked';
    if (state === 'COMPRESSION' || riskScore >= 55) return 'caution';
    if ((state === 'RANGING' || state === 'ACCUMULATION' || state === 'DISTRIBUTION') && regime.key === 'MANIPULATION' && confidence >= 65) return 'allowed';
    return 'caution';
  }

  function analyze(snapshot) {
    const { klines, live, breadth, fundingPct, oiPressure, rest } = snapshot;
    if (!klines['1m']?.length || !klines['5m']?.length || !klines['15m']?.length || !breadth || !rest) {
      return { loading: true };
    }

    const atr1 = calcATR(klines['1m']);
    const atr5 = calcATR(klines['5m']);
    const atr15 = calcATR(klines['15m']);
    const last1 = klines['1m'][klines['1m'].length-1]?.c;
    const last5 = klines['5m'][klines['5m'].length-1]?.c;
    const last15 = klines['15m'][klines['15m'].length-1]?.c;

    const metrics = {
      atr1m: atr1,
      atr5m: atr5,
      atr15m: atr15,
      atrPct1m: last1 ? (atr1/last1)*100 : null,
      atrPct5m: last5 ? (atr5/last5)*100 : null,
      atrPct15m: last15 ? (atr15/last15)*100 : null,
      adx1m: calcADX(klines['1m']),
      adx5m: calcADX(klines['5m']),
      adx15m: calcADX(klines['15m']),
      slope1m: slopePct(klines['1m'], 10),
      slope5m: slopePct(klines['5m'], 8),
      slope15m: slopePct(klines['15m'], 6),
      chop1m: choppinessProxy(klines['1m']),
      fundingPct,
      breadthTrending: breadth.trending,
      breadthFunding: breadth.fundingPositive,
      breadthAboveVwap: breadth.aboveVwap,
      oiPressure
    };

    const stateRes = scoreState(metrics);
    const regime = regimeFromState(stateRes.state, metrics);
    const riskScore = clamp(Math.round((metrics.atrPct1m||0)*18 + (metrics.atrPct5m||0)*12 + live.spreadPct*120000 + Math.abs(metrics.fundingPct||0)*800 + (metrics.adx15m||0)*0.35), 15, 94);
    const permission = permissionFromState(stateRes.state, regime, stateRes.confidence, riskScore);

    stateHistory.push(clamp(Math.round((metrics.adx15m||0)*1.6 + (metrics.atrPct5m||0)*16), 12, 92));
    while (stateHistory.length > 40) stateHistory.shift();

    return {
      loading: false,
      state: stateRes.state,
      confidence: stateRes.confidence,
      regime,
      permission,
      riskScore,
      riskLevel: riskScore >= 72 ? 'HIGH' : riskScore >= 46 ? 'MODERATE' : 'LOW',
      metrics,
      stateHistory: [...stateHistory],
      helperText: `ADX(15m) ${fmt2.format(metrics.adx15m||0)} | ATR(5m) ${fmt2.format(metrics.atrPct5m||0)}% | Slope(15m) ${fmt2.format(metrics.slope15m||0)}%`
    };
  }

  return { analyze };
}
