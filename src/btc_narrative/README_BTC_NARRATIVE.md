# BTC Narrative Strategy

This strategy is isolated from Hunter Original.

## Current Active Logic

BTC Narrative collects all BTC context, but currently uses only:

- BTC LS_POSIT
- BTC LS_RATIO
- BTC LS_ACCOUNT

Decision:

- If BTC long crowding is greater than BTC short crowding → SHORT followers
- If BTC short crowding is greater than BTC long crowding → LONG followers

Followers:

- ETHUSDT
- SOLUSDT
- DOGEUSDT
- XRPUSDT

## Not Part of Current Decision

The following are collected/logged for future research only:

- Funding
- Open Interest
- VWAP
- Liquidity
- BTC decision score

## Logs

Dedicated log file:

```bash
logs/btc_narrative.log
```

## Safety

Current router is dry-run only. It does not submit trades.
