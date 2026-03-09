# CLAUDE.md

## Workflow Orchestration

- **Plan mode** for ANY non-trivial task (3+ steps). STOP and re-plan if something goes sideways.
- **Subagents** liberally to keep main context clean. One task per subagent.
- **Verify before done**: Prove it works. Run tests, check logs, demonstrate correctness.
- **Simplicity first**: Minimal changes. Find root causes. Senior developer standards.

## Git & Deployment

- **Working branch:** `master` -- **Production branch:** `main` (Railway auto-deploys)
- **Deploy:** `git checkout main && git merge master && git push && git checkout master`
- "Push to Railway" or "deploy" = merge `master` -> `main` and push. Never force-push `main`.

## Running the Bot

`python kalshi_bot.py` -- No build step, no tests, no linter. Config in `config.py`.

**Levels:** `DRY_RUN=True` (analysis only) -> `ENVIRONMENT="demo"` (practice) -> `ENVIRONMENT="production"` (real money)

## Dependencies

Core: `requests`, `cryptography`, `numpy`, `scipy` (`requirements.txt`).
APIs (free, no keys): Open-Meteo (ensemble forecasts), NWS (settlement source).
Kalshi API: RSA key-pair auth via `API_KEY_ID` + `PRIVATE_KEY_PATH` in `config.py`.

## Architecture (v4.0 -- Rebuilt March 2026)

Weather + arbitrage trading bot for Kalshi. ~4,000 lines across 11 files (down from 15K/18 files).
Continuous loop (2min, 1min peak 12-5 PM ET). Scans weather markets, detects mispricing, places limit orders.

```
market_scanner.py -> strategy.py (edge + confirmer + sizing) -> risk_manager.py -> maker_strategy.py -> kalshi_client.py
   (discover)        (evaluate + confirm + size)                  (10 safety checks)  (limit pricing)     (execute)
```

**Edge-priority execution:** Evaluate all markets, sort by edge descending, execute highest first.

### Modules (11 files)

| Module | Lines | Role |
|--------|-------|------|
| `kalshi_bot.py` | ~430 | Entry point, main loop, observation fetching, exit execution |
| `kalshi_client.py` | ~300 | Kalshi API wrapper, RSA-PSS auth, dual environment |
| `config.py` | ~170 | ~70 parameters (down from ~450). Values in cents. |
| `market_scanner.py` | ~130 | Queries Kalshi for weather series (20 cities) |
| `strategy.py` | ~700 | S1 Weather Edge + S2 Arbitrage. CASE 1/3 confirmed outcomes. Quarter-Kelly sizing. |
| `weather_engine.py` | ~750 | 143 ensemble members (GFS 31, ECMWF 51, ICON 40, GEM 21) via Open-Meteo |
| `signal_confirmer.py` | ~265 | 5-source voting. STRONG/CONFIRM/REJECT (no WEAK). NWS veto power. |
| `risk_manager.py` | ~280 | 10 safety checks + SIZE-DOWN logic. State in `risk_state.json`. |
| `trade_intelligence.py` | ~760 | Exit logic, settlement P&L sync (single writer), NWS observations |
| `maker_strategy.py` | ~260 | Limit orders at fair_value - spread_buffer. State in `maker_orders.json`. |
| `dashboard.py` | ~780 | Web dashboard. `/api/health`, `/api/state`, `/api/force-exit` |

### Deleted Modules (v3 -> v4)
`trade_scorecard.py`, `self_improver.py`, `quant_analytics.py`, `seasonal_confidence.py`,
`trade_analyzer.py`, `volatility_engine.py`, `spx_confirmer.py`, `market_quality.py`

### State Files (6, down from 17)

`trade_history.json`, `risk_state.json`, `pnl_history.json`, `bot_status.json`, `maker_orders.json`, `scan_log.json`

### Key Design Decisions

- **Quarter-Kelly sizing** (Kelly/4 x confirmation multiplier)
- **Maker strategy** -- limit orders, not market orders
- **NWS settlement** -- Weather markets settle on NWS Daily Climate Reports
- **NWS rounding** -- +/-1F DOS-era conversion error. Buffer: +/-1F = no trade, +/-2F = 50% size.
- **NO-side separation** -- Dynamic: `max(3.0F, std_dev * 0.8)` from expanded boundary. CONFIRM gets 1.5x penalty.
- **Fee-adjusted edge** -- 7% fee drag. Side-aware formula.
- **Only CASE 1 = CONFIRMED_OUTCOME** -- temp exceeded bucket, can't un-happen. CASE 2 DELETED. CASE 3 = STRONG.
- **Binary exits** -- HOLD or EXIT. No PARE/HEDGE/TAKE_PROFIT.
- **Single-writer P&L** -- Only `sync_pnl_from_kalshi()` writes `pnl_history.json`. No exceptions.
- **Size-down, not reject** -- When caps are exceeded, risk manager reduces contracts to fit instead of blocking.
- **Kill switch** -- Daily loss = stop for day (auto-resume). 5 consecutive = 4h pause. No Sharpe-based shutdown.

## NWS Station Mappings (20 Cities)

| Code | City | Station | | Code | City | Station |
|------|------|---------|-|------|------|---------|
| NYC | New York | KNYC | | HOU | Houston Hobby | KHOU |
| CHI | Chicago | KMDW | | LV | Las Vegas | KLAS |
| MIA | Miami | KMIA | | MIN | Minneapolis | KMSP |
| AUS | Austin | KAUS | | NOLA | New Orleans | KMSY |
| LAX | Los Angeles | KLAX | | OKC | Oklahoma City | KOKC |
| DEN | Denver | KDEN | | PHX | Phoenix | KPHX |
| PHI | Philadelphia | KPHL | | SATX | San Antonio | KSAT |
| ATL | Atlanta | KATL | | SEA | Seattle | KSEA |
| BOS | Boston | KBOS | | SFO | San Francisco | KSFO |
| DAL | Dallas | KDFW | | DC | Washington DC | KDCA |

**Houston = KHOU (Hobby), NOT KIAH.** Mappings in `weather_engine.py` CITIES dict.

## Risk Parameters (v4.0)

**IMPORTANT: Any changes to risk parameters MUST be reflected here.**

Bankroll ~$40. All values in `config.py`. Cents = 100 per $1.00.

### Core Limits
| Parameter | Value | Notes |
|-----------|-------|-------|
| `MIN_EDGE` | 10% | Morning (before noon): 12%. Next-day: 15%. |
| `CONFIRMED_MIN_EDGE` | 5% | CASE 1 confirmed outcomes |
| `FEE_ADJUSTED_MIN_EDGE` | 3% | After 7% fee drag |
| `MIN_PAYOUT_DOLLARS` | $0.25 | Minimum expected payout per trade |
| `DAILY_LOSS_LIMIT_CENTS` | 600 ($6) | ~15% of bankroll |
| `CONSECUTIVE_LOSS_PAUSE` | 3 losses / 60 min | |
| `KILL_SWITCH_CONSEC_LOSSES` | 5 | 4-hour pause, then auto-resume |

### Position Sizing & Exposure
| Parameter | Value | Notes |
|-----------|-------|-------|
| `MAX_POSITION_PCT` | 5% | Normal forecasts |
| `CONFIRMED_POSITION_PCT` | 10% | CASE 1 only |
| `ARB_POSITION_PCT` | 15% | Near risk-free arbitrage |
| `MAX_TOTAL_EXPOSURE_PCT` | 40% | Confirmed bypass |
| `MAX_PER_CITY_PCT` | 10% | Confirmed bypass |
| `MAX_PER_TICKER_CENTS` | 400 ($4) | Always enforced |
| `MAX_CONTRACTS_PER_TICKER` | 5 | Always enforced |
| `MAX_OPEN_POSITIONS` | 3 | Confirmed/arb bypass |
| `MAX_CORRELATED_POSITIONS` | 2 (3 confirmed) | Per city |
| `LIQUIDITY_RESERVE_PCT` | 20% | |
| `TRADE_COOLDOWN` | 120 sec | Same-cycle exempt |

### Strategy Guards
- **Rounding buffer**: +/-1F = no trade, +/-2F = 50% size
- **NO-side separation**: `max(3.0F, std_dev * 0.8)`. CONFIRM gets 1.5x penalty.
- **Model divergence**: YES >6F = skip, NO >8F = skip. <2F = 1.2x boost.
- **Longshot floor**: 5c. **Near-certainty cap**: 88c. **NO ceiling**: 50c.
- **NO sizing**: >=50c gets 40% normal sizing.
- **Next-day**: 1.5x edge threshold, 50% sizing.
- **Warm city bias defaults**: DEN +4F, Gulf/SE +3F, Desert +2F (hardcoded in weather_engine.py)

### Confirmed Outcomes
- **CASE 1** (high exceeded bucket): CONFIRMED_OUTCOME. Min 10 AM local. `obs_high > temp_high + 1F`.
- **CASE 2**: DELETED from codebase.
- **CASE 3** (gap too large): Returns STRONG (not confirmed). Cooling gate before 2 PM. Ensemble veto. Graduated gaps.

### Exit Logic (Binary: HOLD or EXIT)
| Trigger | Action |
|---------|--------|
| Obs confirms loss / approaching bucket | EXIT (high) |
| Rounding buffer after 2 PM | EXIT (high) |
| Thesis valid | HOLD to settlement |

### Maker Strategy
Limit orders at `fair_value - 2c`. Dynamic: high edge -> 1c, low edge -> 3c. Stale cancel 30min.

### Infrastructure Invariants
- **Dashboard outside restart loop**: `start_dashboard_server()` in `__main__` block, NOT inside `main()`.
- **Python 3.12**: `import traceback as _tb` to avoid shadowing.
- **Timezone-aware**: `datetime.now(timezone.utc)`, `ZoneInfo(tz_name)`.
- **Open-Meteo model names**: `gfs_seamless`, `ecmwf_ifs025`, `icon_seamless_eps`, `gem_global`.
- **Open-Meteo confirmer URLs**: `/v1/forecast` (GFS), `/v1/ecmwf`, `/v1/dwd-icon`, `/v1/gem`.
- **Single-writer P&L**: Only `sync_pnl_from_kalshi()` writes `pnl_history.json`.
- **CITIES dict field**: `nws_station` (NOT `station`). Used for observation fetching.
- **Daily reset at 6 AM ET**: Risk counters reset at 6 AM Eastern, not UTC midnight.
- **Balance cache**: Risk manager caches balance for 60s to avoid excessive API calls.
