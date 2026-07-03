"""Layer 1 â€” Scanner.

Scans ALL Binance Futures perpetuals, applies a layered filter:
  Layer A: Symbol quality filter (new in v1.2) â€” must pass
  Layer B: Base liquidity (volume + OI thresholds) â€” must pass
  Layer C: Extremity signal (funding | L/S | OI change) â€” any one passes
  Layer D: Rank by composite extremity score â€” keep top N

v1.2 changes:
  - Added _passes_symbol_quality() filter that rejects:
      1. Stablecoins (USDC, BUSD, TUSD, FDUSD, USDT, DAI, USDP, FRAX, SUSD, LUSD, GUSD)
      2. Symbols with numeric prefix like 1000PEPE, 1000SHIB, 100000LUNC, 10000X etc.
      3. Symbols containing non-ASCII / non-English characters
      4. Test / rarely-used suffixes (_PERP, redundant USDT combos)
  - ScanDiagnostics: added `excluded_quality` counter for the new filter stage
  - config.yaml: exclude_symbols list now only needs stablecoin base names â€”
    the numeric-prefix and non-ASCII rules are enforced in code automatically
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from src.core.config import settings
from src.core.database import AsyncSessionLocal, ScanSnapshot
from src.core.logger import logger
from src.exchange.binance_client import BinanceFuturesClient
from src.exchange.data_fetcher import fetch_all_tickers, fetch_premium_index_all


# ---------------------------------------------------------------------------
# Symbol quality rules
# ---------------------------------------------------------------------------

# Stablecoins and fiat-pegged bases â€” always exclude
_STABLE_BASES = {
    "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDP", "FRAX",
    "SUSD", "LUSD", "GUSD", "USTC", "USDN", "USDJ", "USDX",
    "CUSD", "CELO", "EURT", "EURS", "JEUR", "AGEUR", "XSGD",
    "BIDR", "BVND", "IDRT", "BRL", "NGN", "RUB", "TRY", "ZAR",
}

# Regex: symbol starts with one or more digits followed by letters
# Catches: 1000PEPE, 1000SHIB, 100000LUNC, 10000X, 1000000MOGUSDT, etc.
_NUMERIC_PREFIX_RE = re.compile(r"^\d+[A-Z]")

# Regex: only uppercase ASCII letters and digits allowed in the base name
# (after stripping the USDT suffix)
_CLEAN_BASE_RE = re.compile(r"^[A-Z0-9]+$")


def _extract_base(symbol: str) -> str:
    """Return base asset from a USDT-margined symbol.
    e.g. BTCUSDT â†’ BTC, 1000PEPEUSDT â†’ 1000PEPE
    """
    if symbol.endswith("USDT"):
        return symbol[:-4]
    if symbol.endswith("BUSD"):
        return symbol[:-4]
    return symbol


def _passes_symbol_quality(symbol: str, extra_excludes: set[str]) -> tuple[bool, str]:
    """
    Returns (passes: bool, reason: str).
    `reason` is filled only when the symbol is rejected.
    """
    base = _extract_base(symbol)

    # 1. Explicit exclude list from config
    if any(ex in symbol for ex in extra_excludes):
        return False, f"in exclude_symbols list"

    # 2. Stablecoin base
    if base in _STABLE_BASES:
        return False, f"stablecoin base ({base})"

    # 3. Numeric prefix  e.g. 1000PEPE, 100000LUNC
    if _NUMERIC_PREFIX_RE.match(base):
        return False, f"numeric prefix ({base})"

    # 4. Non-ASCII / non-English characters in symbol
    try:
        symbol.encode("ascii")
    except UnicodeEncodeError:
        return False, f"non-ASCII characters"

    if not _CLEAN_BASE_RE.match(base):
        return False, f"non-English chars in base ({base})"

    return True, ""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    symbol: str
    price: float
    volume_24h_usd: float
    open_interest_usd: float
    funding_rate: float

    # L/S sources:
    # long_short_ratio  = global account ratio
    # ls_position_ratio = top trader position ratio
    # ls_account_ratio  = top trader account ratio
    long_short_ratio: float
    ls_position_ratio: float = 1.0
    ls_account_ratio: float = 1.0

    oi_change_4h_pct: float = 0.0
    extremity_score: float = 0.0
    reasons: list[str] = field(default_factory=list)


@dataclass
class ScanDiagnostics:
    """Funnel stats â€” how many symbols passed each filter stage."""
    total_symbols: int = 0
    excluded_quality: int = 0      # v1.2: NEW â€” numeric prefix, stablecoin, non-ASCII
    passed_volume: int = 0
    passed_oi: int = 0
    passed_extremity: int = 0
    final_shortlist: int = 0
    # Legacy alias kept for UI compatibility
    @property
    def excluded_stablecoins(self) -> int:
        return self.excluded_quality


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class Scanner:
    def __init__(self) -> None:
        self.cfg = settings.scanner
        self.last_diagnostics: Optional[ScanDiagnostics] = None

    def _passes_base_liquidity(self, ticker_volume: float, oi_usd: float) -> bool:
        return (
            ticker_volume >= self.cfg["min_quote_volume_24h_usd"]
            and oi_usd >= self.cfg["min_open_interest_usd"]
        )

    def _check_extremity(self, r: ScanResult) -> bool:
        """Returns True if any extremity signal is present. Builds reasons list."""
        passed = False

        f_thresh = self.cfg["funding_extreme_threshold"]
        if abs(r.funding_rate) >= f_thresh:
            sign = "+" if r.funding_rate > 0 else "-"
            r.reasons.append(f"funding_extreme({sign}{abs(r.funding_rate)*100:.3f}%)")
            r.extremity_score += min(abs(r.funding_rate) / f_thresh, 3.0) * 25
            passed = True

        # Capital-positioning extremity gate.
        # Hunter should scan where money is actually positioned, not only how
        # many accounts are long/short. Therefore Scanner uses LS_POSIT
        # (top-trader position ratio) as the primary L/S extremity source.
        # Legacy ls_ratio_* config keys remain as fallback only.
        ls_pos_long_extreme = float(
            self.cfg.get("ls_position_long_extreme", self.cfg.get("ls_ratio_long_extreme", 1.5))
        )
        ls_pos_short_extreme = float(
            self.cfg.get("ls_position_short_extreme", self.cfg.get("ls_ratio_short_extreme", 0.67))
        )

        if (
            r.ls_position_ratio >= ls_pos_long_extreme
            or r.ls_position_ratio <= ls_pos_short_extreme
        ):
            r.reasons.append(f"ls_posit_extreme({r.ls_position_ratio:.2f})")
            distance = abs(r.ls_position_ratio - 1.0)
            r.extremity_score += min(distance, 3.0) * 20
            passed = True

        oi_thresh = self.cfg["oi_change_4h_threshold"]
        if abs(r.oi_change_4h_pct) >= oi_thresh:
            sign = "+" if r.oi_change_4h_pct > 0 else "-"
            r.reasons.append(f"oi_change_4h({sign}{abs(r.oi_change_4h_pct)*100:.1f}%)")
            r.extremity_score += min(abs(r.oi_change_4h_pct) / oi_thresh, 3.0) * 15
            passed = True

        r.extremity_score += min(r.volume_24h_usd / 1e9, 5.0) * 2
        return passed

    async def _enrich_oi_change(
        self, client: BinanceFuturesClient, symbols: list[str]
    ) -> dict[str, float]:
        async def fetch(sym: str) -> tuple[str, float]:
            try:
                hist = await client.open_interest_hist(sym, period="1h", limit=5)
                if hist and len(hist) >= 5:
                    now  = float(hist[-1]["sumOpenInterest"])
                    past = float(hist[0]["sumOpenInterest"])
                    return sym, (now - past) / past if past > 0 else 0.0
            except Exception as e:
                logger.debug(f"OI hist fail {sym}: {e}")
            return sym, 0.0

        sem = asyncio.Semaphore(10)
        async def bounded(sym: str) -> tuple[str, float]:
            async with sem:
                return await fetch(sym)

        return dict(await asyncio.gather(*[bounded(s) for s in symbols]))

    async def _enrich_ls_ratios(
        self, client: BinanceFuturesClient, symbols: list[str]
    ) -> dict[str, dict[str, float]]:
        """Fetch all L/S ratio sources used by dashboard and scanner telemetry.

        global       = Global Long/Short Account Ratio
        top          = Top Trader Long/Short Position Ratio
        top_accounts = Top Trader Long/Short Account Ratio
        """
        async def fetch_one(sym: str, scope: str) -> float:
            try:
                data = await client.long_short_ratio(sym, period="5m", limit=1, scope=scope)
                if data:
                    return float(data[0]["longShortRatio"])
            except Exception as e:
                logger.debug(f"L/S fail {sym} scope={scope}: {e}")
            return 1.0

        sem = asyncio.Semaphore(10)

        async def fetch(sym: str) -> tuple[str, dict[str, float]]:
            async with sem:
                global_ratio, position_ratio, account_ratio = await asyncio.gather(
                    fetch_one(sym, "global"),
                    fetch_one(sym, "top"),
                    fetch_one(sym, "top_accounts"),
                )
                return sym, {
                    "long_short_ratio": global_ratio,
                    "ls_position_ratio": position_ratio,
                    "ls_account_ratio": account_ratio,
                }

        return dict(await asyncio.gather(*[fetch(s) for s in symbols]))

    async def scan(self) -> list[ScanResult]:
        """Run a full scan cycle. Returns top-N shortlisted ScanResults."""
        diag = ScanDiagnostics()
        self.last_diagnostics = diag
        logger.info("Scanner: starting cycle")

        async with BinanceFuturesClient() as client:
            tickers, premium = await asyncio.gather(
                fetch_all_tickers(client),
                fetch_premium_index_all(client),
            )
            diag.total_symbols = len(tickers)

            extra_excludes = set(self.cfg.get("exclude_symbols", []))
            base_passed: list[ScanResult] = []

            for t in tickers:
                # v1.2: quality gate first
                ok, reason = _passes_symbol_quality(t.symbol, extra_excludes)
                if not ok:
                    logger.debug(f"Excluded {t.symbol}: {reason}")
                    diag.excluded_quality += 1
                    continue

                p = premium.get(t.symbol)
                if not p:
                    continue

                if t.volume_24h_usd < self.cfg["min_quote_volume_24h_usd"]:
                    continue

                price = float(p["markPrice"])
                base_passed.append(ScanResult(
                    symbol=t.symbol,
                    price=price,
                    volume_24h_usd=t.volume_24h_usd,
                    open_interest_usd=0.0,
                    funding_rate=float(p["lastFundingRate"]),
                    long_short_ratio=1.0,
                    ls_position_ratio=1.0,
                    ls_account_ratio=1.0,
                    oi_change_4h_pct=0.0,
                ))

            diag.passed_volume = len(base_passed)
            logger.info(
                f"Scanner funnel: {diag.total_symbols} total â†’ "
                f"{diag.excluded_quality} excluded (quality+stable+numeric) â†’ "
                f"{diag.passed_volume} passed volume"
            )

            if not base_passed:
                return []

            symbols = [r.symbol for r in base_passed]

            async def fetch_oi(sym: str) -> tuple[str, float]:
                try:
                    oi = await client.open_interest(sym)
                    return sym, float(oi["openInterest"])
                except Exception:
                    return sym, 0.0

            sem = asyncio.Semaphore(15)
            async def bounded_oi(sym: str) -> tuple[str, float]:
                async with sem:
                    return await fetch_oi(sym)

            oi_results, ls_results, oi_change_results = await asyncio.gather(
                asyncio.gather(*[bounded_oi(s) for s in symbols]),
                self._enrich_ls_ratios(client, symbols),
                self._enrich_oi_change(client, symbols),
            )
            oi_map = dict(oi_results)

            shortlist: list[ScanResult] = []
            oi_passed: list[ScanResult] = []
            for r in base_passed:
                oi_amount = oi_map.get(r.symbol, 0.0)
                r.open_interest_usd = oi_amount * r.price

                ls_pack = ls_results.get(r.symbol, {}) or {}
                r.long_short_ratio = float(ls_pack.get("long_short_ratio", 1.0) or 1.0)
                r.ls_position_ratio = float(ls_pack.get("ls_position_ratio", 1.0) or 1.0)
                r.ls_account_ratio = float(ls_pack.get("ls_account_ratio", 1.0) or 1.0)

                r.oi_change_4h_pct = oi_change_results.get(r.symbol, 0.0)

                if r.open_interest_usd < self.cfg["min_open_interest_usd"]:
                    continue
                oi_passed.append(r)
                if not self._check_extremity(r):
                    continue
                shortlist.append(r)

            diag.passed_oi        = len(oi_passed)
            diag.passed_extremity = len(shortlist)

            shortlist.sort(key=lambda x: x.extremity_score, reverse=True)
            top = shortlist[: self.cfg["top_n_to_monitor"]]
            diag.final_shortlist = len(top)

            logger.info(
                f"Scanner funnel: {diag.passed_volume} â†’ {diag.passed_oi} OI â†’ "
                f"{diag.passed_extremity} extremity â†’ {diag.final_shortlist} final"
            )
            for r in top:
                logger.info(
                    f"  â†’ {r.symbol}: score={r.extremity_score:.1f} | "
                    f"vol=${r.volume_24h_usd/1e6:.0f}M | "
                    f"OI=${r.open_interest_usd/1e6:.0f}M | "
                    f"LS(g/p/a)={r.long_short_ratio:.2f}/{r.ls_position_ratio:.2f}/{r.ls_account_ratio:.2f} | "
                    f"reasons={','.join(r.reasons)}"
                )

            await self._persist(top, all_results=base_passed)
            return top

    async def _persist(
        self, top: list[ScanResult], all_results: list[ScanResult]
    ) -> None:
        """Save scan results. Top symbols get passed_filters=True."""
        top_set = {r.symbol for r in top}
        async with AsyncSessionLocal() as session:
            ts = datetime.utcnow()
            for r in all_results:
                session.add(ScanSnapshot(
                    symbol=r.symbol,
                    timestamp=ts,
                    price=r.price,
                    volume_24h_usd=r.volume_24h_usd,
                    open_interest_usd=r.open_interest_usd,
                    funding_rate=r.funding_rate,
                    long_short_ratio=r.long_short_ratio,
                    ls_position_ratio=r.ls_position_ratio,
                    ls_account_ratio=r.ls_account_ratio,
                    oi_change_4h_pct=r.oi_change_4h_pct,
                    passed_filters=r.symbol in top_set,
                    extremity_score=r.extremity_score,
                ))
            await session.commit()
