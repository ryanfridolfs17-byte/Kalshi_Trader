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
APIs (free, no keys): Open-Meteo (ensemble forecasts), NWS (settlement source), AviationWeather METAR (primary observations).
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
| `signal_confirmer.py` | ~270 | 5-source voting. STRONG/CONFIRM/REJECT. NWS disagree + 2 models agree = CONFIRM (0.5x). 1 agree + 0 disagree = CONFIRM (0.8x). Gray zone 1.5F. High-edge (>25%) bypass at 0.4x sizing. |
| `risk_manager.py` | ~340 | 10 safety checks + SIZE-DOWN logic + settlement methods. State in `risk_state.json`. |
| `trade_reviewer.py` | ~1050 | Daily learning: per-city bias, CRPS model accuracy, NWS actual lookups, pattern analysis, scan reconciliation, guard effectiveness, probability calibration (Brier score + decomposition), profitability metrics (profit factor, expectancy), information decay curves. State in `learning_state.json`. |
| `trade_intelligence.py` | ~870 | Exit logic, settlement P&L sync (single writer), METAR primary + NWS fallback observations |
| `maker_strategy.py` | ~260 | Limit orders at fair_value - spread_buffer. State in `maker_orders.json`. |
| `dashboard.py` | ~900 | Web dashboard. `/api/health`, `/api/state`, `/api/positions`, `/api/risk`, `/api/pnl`, `/api/trades`, `/api/performance`, `/api/equity-curve`, `/api/force-exit` |

### Deleted Modules (v3 -> v4)
`trade_scorecard.py`, `self_improver.py`, `quant_analytics.py`, `seasonal_confidence.py`,
`trade_analyzer.py`, `volatility_engine.py`, `spx_confirmer.py`, `market_quality.py`

### State Files (8, down from 17)

`trade_history.json`, `risk_state.json`, `pnl_history.json`, `bot_status.json`, `maker_orders.json`, `scan_log.json`, `learning_state.json`, `learning_history.json`

### Key Design Decisions

- **Continuous Kelly sizing** -- Divisor: `max(3.0, min(6.0, 6.0 - (edge - 0.05) * 20.0))`. Edge 5% → divisor 6.0, edge 20%+ → divisor 3.0 (linear, no discontinuity). Dispersion multiplier: `1/(1 + model_spread/5)`. **Fee-adjusted payout**: Kelly uses `net_payout = gross_payout * (1 - 0.07)`.
- **Exact Kalshi fee** -- `fee = fee_rate * min(price, 100-price)` per contract. Applied in `_calculate_fee_adjusted_edge()` in probability space.
- **Maker strategy** -- limit orders default. CASE 1 confirmed with edge >15% uses taker (market) orders for guaranteed execution.
- **NWS settlement** -- Weather markets settle on NWS Daily Climate Reports
- **NWS rounding** -- +/-1F DOS-era conversion error. Buffer: +/-1F = no trade, +/-2F = 50% size.
- **NO-side separation** -- Dynamic: `max(2.0F, std_dev * 0.6)` from expanded boundary. CONFIRM gets 1.25x penalty.
- **Fee-adjusted edge** -- 7% fee drag. Side-aware formula.
- **Only CASE 1 = CONFIRMED_OUTCOME** -- temp exceeded bucket, can't un-happen. CASE 2 DELETED. CASE 3 = STRONG.
- **Binary exits** -- HOLD or EXIT. Profit protection added: forecast shift detection + peak drawdown trailing stop. CASE 1 confirmed outcomes bypass profit protection.
- **Single-writer P&L** -- Only `sync_pnl_from_kalshi()` writes `pnl_history.json`. No exceptions.
- **Size-down, not reject** -- When caps are exceeded, risk manager reduces contracts to fit instead of blocking.
- **Kill switch** -- Daily loss = stop for day (auto-resume). 3 consecutive = 4h pause. No Sharpe-based shutdown.
- **Rolling drawdown** -- 5-day rolling P&L check. Block trading if rolling P&L <= -25% of balance. Confirmed outcomes bypass.
- **Daily P&L time series** -- `daily_history` array in `pnl_history.json`, updated by single-writer `sync_pnl_from_kalshi()`. 90-day cap. `rolling_5d_pnl_cents` computed from last 5 entries.
- **SIGTERM graceful shutdown** -- `threading.Event` checked each cycle. On SIGTERM: breaks loop, prints shutdown message. `time.sleep()` replaced with `shutdown_event.wait()` for instant response.
- **Balance single source** -- `kalshi_bot.main()` reads balance from `risk._get_balance_cents()` (60s cached + fallback). No redundant API call.
- **Health endpoint sanitized** -- `/api/health` returns only `{status, timestamp}` when unauthenticated. Full details require auth token.
- **Regional correlation caps** -- 6 regions (northeast, southeast, south_central, mountain_west, west_coast, midwest). Max 15% of balance per region. Confirmed outcomes bypass.
- **Convergence calibration logging** -- `[CONV]` log every time convergence is computed. Shows score, tracking error, spread, hour, and trigger status. Set thresholds from percentile data after 2 weeks.
- **Deploy gate** -- `RUN python -c "import kalshi_bot"` in Dockerfile catches syntax errors before Railway deploys.
- **CORS origin restriction** -- `CORS_ORIGIN` env var (default `*`). Set to Railway URL in production.
- **Dead endpoints** -- `/api/pending` and `/api/reports` return 410 Gone.
- **State sync endpoint** -- `/api/sync` (GET, auth required) returns all 8 state files + `fill_tracking.json` as a single JSON blob with `synced_at` timestamp. Local pull: `python sync_local.py --token $DASHBOARD_TOKEN`.
- **Fee-adjusted Kelly** -- Kelly sizing uses fee-adjusted edge (not raw edge) across all trade paths (normal, CASE 1, CASE 3). Prevents oversizing on marginal trades.
- **CASE 1 fee gate** -- CASE 1 confirmed outcomes must pass both `CASE1_MIN_EDGE` (raw, 2%) and `CASE1_FEE_ADJUSTED_MIN_EDGE` (net, 1%). Lower than normal `FEE_ADJUSTED_MIN_EDGE` (3%) because outcome is near-guaranteed.
- **CASE 1/3 rejection logging** -- All rejection paths in `_check_confirmed_outcome()` print `[CASE1-SKIP]` or `[CASE3-SKIP]` with city, reason, and values. Eliminates blind spot where confirmed outcomes were silently rejected.
- **Forecast-aware profit protection** -- Two new exit rules protect unrealized gains. Rule 5: detects forecast shift (ensemble probability dropped 15%+ from entry) while position is profitable (50%+). Rule 6: peak drawdown safety net (price dropped 20%+ from tracked peak, min 20c peak). Both bypass for CASE 1 confirmed outcomes. `entry_prob` and `peak_price_cents` stored in position dict, peak updated every cycle before exit evaluation. Logs: `[PROFIT-EXIT]` and `[PEAK-EXIT]`.
- **Forecast-aware profit-taking (Rule 8)** -- Sells when position is up big AND forecast says we're on the wrong side. Tiered: up 100%+ with prob <50% = sell; up 200%+ with prob <65% = sell. Skips CASE 1 confirmed. Uses `_compute_current_prob()` for live forecast. Logs: `[PROFIT-TAKE]`. Example: bought YES at 6c, now 24c (300% gain), forecast prob 25% → immediate sell.
- **Exit pricing** -- ALL exit orders (YES and NO) use `limit_price=1` (accept any bid). For sell orders, limit_price = minimum acceptable price. 1c = "sell at any price" = fills at current bid.
- **Taker NO ceiling** -- `place_market_order()` rejects NO-side orders exceeding `NO_SIDE_MAX_PRICE_CENTS`. Matches limit order guard.
- **Multiplier cap** -- Combined `conf_mult * rounding_mult * conv_mult` capped at 2.0x before Kelly sizing.
- **PNL cache invalidation** -- `risk._pnl_cache` cleared immediately after `sync_pnl_from_kalshi()` so drawdown check uses fresh data.
- **SIGTERM order cleanup** -- `maker.cancel_all()` called before main loop exits on SIGTERM.
- **Exit pricing** -- ALL exit orders (YES and NO) use `limit_price=1` (accept any bid). For sell orders, limit_price = minimum acceptable price. 1c = "sell at any price" = fills at current bid.
- **Taker NO ceiling** -- `place_market_order()` rejects NO-side orders exceeding `NO_SIDE_MAX_PRICE_CENTS`. Matches limit order guard.
- **Multiplier cap** -- Combined `conf_mult * rounding_mult * conv_mult` capped at 2.0x before Kelly sizing.
- **PNL cache invalidation** -- `risk._pnl_cache` cleared immediately after `sync_pnl_from_kalshi()` so drawdown check uses fresh data.
- **SIGTERM order cleanup** -- `maker.cancel_all()` called before main loop exits on SIGTERM.
- **Dashboard auth hardened** -- Query parameter auth removed (Bearer header only). Production warning if `DASHBOARD_TOKEN` empty on Railway.
- **REJECT bypass deleted (March 22)** -- 0/6 win rate proved confirmer is correct at extreme edges. Ensemble overconfidence at >25% edge is a systematic failure. REJECT verdict = always reject.
- **City bias safety gate** -- if `|learned bias| > 5F` with 5+ data points, block trading for that city via `_get_learning_adjustments()` returning `blocked=True`. Model is fundamentally miscalibrated.
- **Per-ticker contract accumulation guard** -- `add_pending_order()` now checks `_ticker_contracts()` against `MAX_CONTRACTS_PER_TICKER` before storing. Defense-in-depth against cross-cycle accumulation. `_reconcile_positions()` also syncs contract counts with Kalshi.
- **Sniper mode (March 23)** -- Philosophy shift: stop trading everything with edge, focus on proven setups only. CASE 1 loosened (hour 10→9, NO cap 60→70, fee-adj edge 1%→0.5%). Convergence starts at noon city-local (was 2 PM), sizing boost 0.3→0.5, edge threshold lowering re-enabled (5% for convergence). Afternoon CONFIRM trades at 4% edge threshold (deferred check). Open-Meteo window 7AM-7PM ET (was 8-6).
- **YES-side block (March 24)** -- YES trades 1W/17L (-$23). NO trades 2W/1L (+$4). Favourite-longshot bias (Whelan 2025) confirmed across all verdicts and price tiers. All YES-side signals now blocked in normal pipeline. Only remaining trade paths: CASE 1 confirmed NO-side, normal NO-side with CONFIRM verdict (afternoon). CASE 3 STRONG also blocked (bypassed all safety guards, 1W/11L track record).
- **Adverse selection fix (March 23)** -- Both sides paused for 70+ hours due to 100% fill rate on 5 orders. Minimum raised to 10 maker orders. Taker orders excluded from fill rate calculation.
- **Strategy inversion (March 23)** -- Forensic analysis of 77 settled trades + Whelan et al. (2025) academic study of 300K+ Kalshi contracts. Favourite-longshot bias: cheap contracts win LESS than price implies. YES <30c was 4W/19L (-$14.71); NO at 75c+ was 29W/0L (+$20.52). High edge (20%+) was 5W/19L; low edge (0-10%) was 25W/1L. Morning 19W/24L; afternoon 18W/0L. STRONG verdict 2W/13L. Changes: `LONGSHOT_FLOOR_CENTS` 3→30, `NO_SIDE_MAX_PRICE_CENTS` 35→75, `NO_SIDE_SIZING_MULTIPLIER` 0.30→0.60, Kelly edge cap 0.40→0.15, high-edge (>20%) 50% sizing penalty, pre-noon directional block, STRONG verdict block.

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

Bankroll ~$48. All values in `config.py`. Cents = 100 per $1.00. `BALANCE_FALLBACK_CENTS=4800` used when API unavailable.

### Core Limits
| Parameter | Value | Notes |
|-----------|-------|-------|
| `MIN_EDGE` | 6% | Morning (before noon): 7.2% (1.2x). Next-day: 9% (1.5x). Early morning (6-9 AM local): 12% (2.0x). |
| `CONFIRMED_MIN_EDGE` | 5% | CASE 3 and convergence trades |
| `CASE1_MIN_EDGE` | 2% | CASE 1 confirmed outcomes only (obs already exceeded bucket) |
| `FEE_ADJUSTED_MIN_EDGE` | 3% | After 7% fee drag |
| `CASE1_FEE_ADJUSTED_MIN_EDGE` | 1% | CASE 1 only: lower bar since outcome is confirmed |
| `MIN_PAYOUT_DOLLARS` | $0.10 | Minimum expected payout per trade |
| `DAILY_LOSS_LIMIT_CENTS` | 600 ($6) | ~15% of bankroll |
| `CONSECUTIVE_LOSS_PAUSE` | 3 losses / 60 min | |
| `KILL_SWITCH_CONSEC_LOSSES` | 3 | 4-hour pause, then auto-resume |
| `ROLLING_DRAWDOWN_LIMIT_PCT` | 25% | Block trading if 5-day rolling P&L <= -25% of balance. Confirmed bypass. |

### Position Sizing & Exposure
| Parameter | Value | Notes |
|-----------|-------|-------|
| `MAX_POSITION_PCT` | 5% | Normal forecasts |
| `CONFIRMED_POSITION_PCT` | 10% | CASE 1 only |
| `ARB_POSITION_PCT` | 15% | Near risk-free arbitrage |
| `ARB_MIN_SPREAD_CENTS` | 7 | Min gap for arb trades (covers 7% fee) |
| `MAX_TOTAL_EXPOSURE_PCT` | 50% | Confirmed bypass |
| `MAX_PER_CITY_PCT` | 10% | Confirmed bypass |
| `MAX_PER_REGION_PCT` | 15% | Per weather region. Confirmed bypass. |
| `MAX_PER_TICKER_CENTS` | 400 ($4) | Always enforced |
| `MAX_CONTRACTS_PER_TICKER` | 5 | Always enforced |
| `MAX_OPEN_POSITIONS` | 6 | Confirmed/arb bypass |
| `MAX_CORRELATED_POSITIONS` | 2 (3 confirmed) | Per city |
| `LIQUIDITY_RESERVE_PCT` | 20% | |
| `TRADE_COOLDOWN` | 120 sec | Same-cycle exempt |

### Strategy Guards
- **Rounding buffer**: +/-1F = no trade, +/-2F = 50% size. Expensive contracts (>50c) in the soft buffer (1-2F) are BLOCKED (0.0) — a 1F NWS rounding error on an 80c contract = 80c loss.
- **NO-side separation**: `max(2.0F, std_dev * 0.6)`. CONFIRM gets 1.25x penalty.
- **Model divergence**: YES >8F = skip, NO >10F = skip. <2F = 1.2x boost.
- **Longshot floor**: 3c. **Near-certainty cap**: 80c (was 93c — buying YES >80c is terrible risk/reward with 5-8F model MAE). **NO ceiling**: 50c (CASE1 bypasses).
- **NO sizing**: >=50c gets 40% normal sizing.
- **Next-day**: 1.5x edge threshold, 50% sizing.
- **Same-day before 6 AM local**: BLOCKED. Overnight forecasts are stale.
- **Same-day 6-9 AM local**: 2.0x edge threshold (14%). Stale forecast penalty. NO-side gets additional 50% sizing reduction.
- **Warm city bias defaults**: DEN +4F, Gulf/SE +3F, Desert +2F, PNW +2F/+1.5F (hardcoded in weather_engine.py). Applied in winter months (Dec-Mar).

### Confirmed Outcomes
- **CASE 1** (high exceeded bucket): CONFIRMED_OUTCOME. Min 10 AM local. `obs_high > temp_high + 1F`. Edge = `0.99 - market_prob`. Min edge 2% (`CASE1_MIN_EDGE`). NO price cap 98c. Kelly with `is_confirmed=True` (10% max position). Taker mode if edge >15%.
- **CASE 2**: DELETED from codebase.
- **CASE 3** (gap too large): Returns STRONG (not confirmed). `confirmation_multiplier=1.0`. Cooling gate before 2 PM. Ensemble veto. Graduated gaps.

### Exit Logic (Binary: HOLD or EXIT)
| Trigger | Action |
|---------|--------|
| Next-day positions: Rules 5+6 only (price-based, no obs) | EXIT if triggered |
| Obs confirms loss / approaching bucket (noon-7 PM, gap>5 noon-2PM, gap>3 after) | EXIT (high) |
| Threshold market approaching within 5F (10 AM-1 PM) | EXIT (high) |
| Forecast divergence: obs high > forecast mean + 2F (10 AM+) | EXIT (high) |
| Rounding buffer after 2 PM | EXIT (high) |
| Edge deterioration: current edge < -10% AND (underwater OR cost > $1) (10 AM+, cached for post-6PM) | EXIT (high) |
| YES bucket unreachable: gap > remaining heat potential (noon+, all buckets) | EXIT (high) |
| Near settlement: within 2h of close AND underwater | EXIT (high) |
| Forecast shift: entry_prob - current_prob >= 15% AND profitable (50%+) | EXIT (high) |
| Peak drawdown: price dropped 20%+ from peak (min 20c) AND profitable | EXIT (high) |
| Profit-take: up 100%+ AND forecast prob < 50%; up 200%+ AND prob < 65% | EXIT (high) |
| Thesis valid | HOLD to settlement |

### Maker Strategy
Limit orders at `fair_value - 2c`. Dynamic: high edge -> 1c, low edge -> 3c. Stale cleanup every cycle with time-decay threshold (30min morning, 15min afternoon, 5min after 3 PM ET). NaN guard on ensemble data.
- **Taker mode**: CASE 1 confirmed with edge >15% (`TAKER_MODE_MIN_EDGE`) uses market orders for guaranteed fill
- **Adverse selection**: Tracks 24h rolling fill rate per side. >70% fill rate = widen spread by 1c. >85% = pause maker on that side. State in `fill_tracking.json`.
- **Order book imbalance**: `get_book_imbalance(ticker)` returns OBI in [-1, 1]. Available for spread adjustment.

### Quant Fund Upgrades (v4.1)

#### Graduated Kelly Sizing (strategy.py)
Edge 5-10%: Kelly/6 (conservative). Edge 10-20%: Kelly/4 (standard). Edge 20%+: Kelly/3 (aggressive).
Dispersion multiplier: `fraction *= 1.0 / (1.0 + model_spread / 5.0)` — high model disagreement = lower sizing.
Model convergence boost (<2F spread) still applied as 1.2x on top.

#### Convergence Confidence Trading (strategy.py + trade_intelligence.py)
After 2 PM local, when obs_high tracks ensemble mean closely AND model spread is low, reduce edge threshold to `CONFIRMED_MIN_EDGE` (5%).
Convergence score: `max(0, 1 - tracking_error/5) * max(0, 1 - spread/8) * hour_factor`. Score > 0.7 triggers.
Sizing boost: `1.0 + CONVERGENCE_SIZING_BOOST * score` (up to 1.5x).

#### Asymmetric P/L Ratio (risk_manager_v2.py)
Tracks rolling win/loss amounts (last 50 each). Computes `avg_win / avg_loss`.
P/L ratio < 2.0 (with 5+ trades): tighten max position to 3%. P/L ratio > 3.0: loosen to 7%.
Self-correcting: losing streaks auto-reduce exposure, winning streaks auto-expand.

#### Bayesian Observation Update (weather_engine.py)
`update_distribution_with_observation(distribution, obs_high, local_hour)`: Shifts ensemble distribution based on real-time NWS observations.
Observation weight scales with hour: `min(0.8, local_hour / 18)`. Later in day = more weight to observations.
Truncates ensemble members below obs_high for high prediction (temp can't go backwards).

#### Cloud Cover / Precipitation Adjustment (weather_engine.py)
`_fetch_cloud_cover(city_code, target_date)`: Fetches daily cloud_cover_mean and precipitation_sum from Open-Meteo.
Cloud cover > 70%: apply -1.5F bias. Precipitation > 0.5mm: apply additional -1.0F bias.
Biases stored in distribution: `cloud_cover_adj_f`, `precip_adj_f`.

#### Portfolio Rebalancing (kalshi_bot.py)
Every 15 cycles (`REBALANCE_INTERVAL_CYCLES`), re-ranks open positions by current edge.
If weakest position edge < 3% (`REBALANCE_MAX_OLD_EDGE`) AND at max capacity: exit to free slot.

### New Config Parameters (v4.1)
| Parameter | Value | Notes |
|-----------|-------|-------|
| `CONVERGENCE_SCORE_THRESHOLD` | 0.7 | Was 0.5. Require tighter obs tracking (no perf data). |
| `CONVERGENCE_MIN_LOCAL_HOUR` | 14 | Only after 2 PM local |
| `CONVERGENCE_SIZING_BOOST` | 0.3 | Was 0.7. Max 1.3x (was 1.7x). No longer lowers edge threshold. |
| `WEATHER_BIAS_CAP_F` | -2.0 | Max total cloud+precip+wind bias. Prevents -3.5F stacking. |
| `CASE1_NO_PRICE_CAP` | 60 | Was 98c. Max NO price for CASE 1 confirmed outcomes. |
| `CLOUD_COVER_THRESHOLD_PCT` | 70 | Above = apply temp bias |
| `CLOUD_COVER_TEMP_BIAS_F` | -1.5 | Overcast day bias |
| `PRECIP_THRESHOLD_MM` | 0.5 | Above = apply additional bias |
| `PRECIP_TEMP_BIAS_F` | -1.0 | Precipitation cooling bias |
| `REBALANCE_INTERVAL_CYCLES` | 15 | Check rebalancing every N cycles |
| `REBALANCE_MIN_NEW_EDGE` | 0.15 | Min edge for new opportunity |
| `REBALANCE_MAX_OLD_EDGE` | 0.03 | Max edge to consider for exit |
| `TAKER_MODE_MIN_EDGE` | 0.15 | Min edge for CASE 1 taker mode |
| `STRONG_TAKER_MIN_FEE_ADJ_EDGE` | 0.20 | STRONG verdict taker mode (was 0.12 hardcoded) |
| `CITY_BIAS_BLOCK_THRESHOLD_F` | 5.0 | Block city if |learned bias| > 5F |
| `CITY_BIAS_BLOCK_MIN_COUNT` | 5 | Require >= 5 data points before blocking |
| `BUCKET_SUM_DEVIATION_CENTS` | 8 | Min deviation from 100c to flag bucket inconsistency |
| `BUCKET_SUM_MIN_MARKETS` | 5 | Min buckets in event to analyze |
| `METAR_CACHE_TTL_SEC` | 90 | METAR batch cache lifetime (under 2-min cycle) |
| `METAR_REQUEST_TIMEOUT` | 10 | HTTP timeout for METAR batch request |
| `METAR_HOURS_LOOKBACK` | 18 | Hours of METAR history per request |
| `METAR_ENABLED` | True | Kill switch for METAR (falls back to NWS-only) |
| `OPEN_METEO_FETCH_START_ET` | 8 | Start of Open-Meteo fetch window (8 AM ET) |
| `OPEN_METEO_FETCH_END_ET` | 18 | End of fetch window (6 PM ET) |
| `ENSEMBLE_CACHE_TTL` | 3600 | 60 min ensemble per-model cache (models update every 6h) |
| `DISTRIBUTION_CACHE_TTL` | 3600 | 60 min distribution cache (matches ensemble) |
| `CLOUD_COVER_CACHE_TTL` | 7200 | 120 min cloud cover cache (daily data) |
| `EARLY_MORNING_EDGE_MULTIPLIER` | 2.0 | 6-9 AM local: 14% min edge (stale 00Z forecasts) |
| `PROFIT_EXIT_PROB_DROP` | 0.15 | Exit if current_prob drops 15%+ below entry_prob |
| `PROFIT_EXIT_MIN_PROFIT_PCT` | 0.50 | Position must be up >= 50% from entry to trigger |
| `PROFIT_EXIT_PEAK_DROP_PCT` | 0.20 | Safety net: exit if price drops 20% from peak |
| `PROFIT_EXIT_MIN_PEAK_CENTS` | 20 | Peak must reach 20c+ (ignore penny noise) |
| `PROFIT_TAKE_MIN_GAIN_PCT` | 1.0 | 100% gain minimum to consider profit-taking |
| `PROFIT_TAKE_PROB_TIER1` | 0.50 | Up 100%+: sell if forecast prob < 50% (wrong side) |
| `PROFIT_TAKE_PROB_TIER2` | 0.65 | Up 200%+: sell if forecast prob < 65% (marginal) |

### New State Files (v4.1)
- `fill_tracking.json` — Adverse selection: per-side order/fill counts (rolling 72h, was 24h)

### v4.5 Data-Driven Fixes (March 18, 2026)

Based on comprehensive trade history analysis (89 positions, 200 fills, 104 settlements):
- **Account: -$57.12 from $100 deposits**. Recent 5-day rolling P&L positive (+$1.45). Profit factor 2.07.
- **NO trades lost $140 total; YES trades earned +$138.** NO-side strategy was fundamentally broken at 50c pricing.
- **60%+ edge trades went 0-4.** Ensemble systematically overconfident at extreme values.
- **6 trades in 22min on Mar 16, all lost.** Concentration risk with no per-cycle limit.
- **Morning entries went 0-6.** All morning-entered settled trades lost.
- **ATL, SEA, OKC, HOU profitable; DAL, AUS, DEN, PHX, DC losing.**

#### Changes
- **NO_SIDE_MAX_PRICE_CENTS**: 50 → 35. NO at 50c is a coin flip minus fees.
- **NO_SIDE_SIZING_MULTIPLIER**: 0.40 → 0.30. Further reduce NO exposure.
- **Per-cycle entry limit**: Max 3 new trades per scan cycle. Prevents concentrated losses.
- **Kelly edge cap**: `divisor_edge = min(divisor_edge, 0.40)`. Prevents oversizing on overconfident signals.
- **City-specific edge multipliers**: `CITY_EDGE_MULTIPLIERS` in config.py. DAL/AUS: 1.5x, PHX/DEN/DC: 1.3x, CHI: 1.2x. Applied in `_get_edge_threshold()`.
- **Model spread → maker buffer**: `buffer += max(0, int(model_spread / 4))`. Wide model disagreement = wider spread (1c per 4F).
- **Expanded taker mode**: STRONG verdict + fee_adj_edge > 12% now uses taker (not just CASE 1 confirmed). Fill certainty worth the ~1c price improvement lost.
- **Adverse selection window**: 24h → 72h. With ~5 trades/day, 72h gives ~15 data points. Min order threshold 3 → 5.
- **Wind speed bias**: `wind_speed_10m_max` fetched from Open-Meteo (0 extra API calls, added to existing daily request). Wind > 32 km/h: -1F bias. Boundary layer mixing suppresses peak highs.
- **Datetime UTC consistency**: ALL `datetime.now()` across weather_engine, trade_intelligence, dashboard, trade_reviewer replaced with `datetime.now(timezone.utc)`. Completes incomplete v4.2 fix.
- **Default target_date**: weather_engine uses ET (not UTC) for default target_date. Prevents wrong-day fetches midnight-5AM.
- **Observed high truncation**: `math.floor()` instead of `int()` in `_fetch_observed_highs()`. Matches NWS conservative C→F approach.
- **KeyboardInterrupt cleanup**: `maker.cancel_all()` now called on Ctrl+C (was only SIGTERM).

#### New Config Parameters (v4.5)
| Parameter | Value | Notes |
|-----------|-------|-------|
| `CITY_EDGE_MULTIPLIERS` | dict | Per-city edge threshold multiplier. DAL/AUS: 1.5x, PHX/DEN/DC: 1.3x, CHI: 1.2x |
| `WIND_SPEED_THRESHOLD_KMH` | 32 | ~20 mph; above this, boundary layer mixing suppresses highs |
| `WIND_SPEED_TEMP_BIAS_F` | -1.0 | Applied when wind exceeds threshold |

### Infrastructure Invariants
- **Kalshi API field normalization (March 2026)**: Kalshi changed response field names: `yes_ask` → `yes_ask_dollars` (string), `volume` → `volume_fp` (string), `count` → `count_fp` (string), `position` → `position_fp` (string). Response `status` field changed to `"active"` but **query parameter** still uses `status=open`. **Three normalizers** in `kalshi_client.py` convert API responses to internal int format: `_normalize_market()` (markets), `_normalize_position()` (positions), `_normalize_order()` (orders). `market_scanner.py` has its own `_normalize_scanner_market()` since it uses direct `requests.get`. **Positions API** (`/portfolio/positions`): `_normalize_position()` converts `position_fp` → `position` (int), `total_traded_dollars` → `total_traded` (cents), `market_exposure_dollars` → `market_exposure` (cents), `fees_paid_dollars` → `fees_paid` (cents), `realized_pnl_dollars` → `realized_pnl` (cents). Applied inside `get_positions()` — all callers use `mp.get("position")` directly. **Fills API** (`/portfolio/fills`): `yes_price`/`no_price` are STRING cents ("5"), `yes_price_dollars`/`no_price_dollars` are STRING dollars ("0.0500"), `count_fp` is STRING ("5.00"). `trade_intelligence._normalize_fill()` converts all to int cents. **Orders API** (`/portfolio/orders/{id}`): `yes_price_dollars`/`no_price_dollars` are STRING dollars. `kalshi_client._normalize_order()` converts to int cents. `maker_strategy.check_fills()` also tries dollar fields directly as belt-and-suspenders. **Settlements API** (`/portfolio/settlements`): `revenue` is int cents (no normalization needed). Market settlement status: check for `"settled"`, `"finalized"`, or `"closed"`. **Rule: ALL new Kalshi API consumers must use normalized fields. If Kalshi renames another field, fix it in the normalizer — NOT in every caller.**
- **Position price reconciliation**: `_reconcile_position_prices()` runs at startup in `kalshi_bot.py`. Fetches recent fills from Kalshi API, computes actual avg fill price per open position, corrects `price_cents`/`cost_cents` in risk_state if they differ. Logs `[RECONCILE]` for each correction. Permanent safety net against entry price drift.
- **Live position reconciliation**: `_reconcile_positions()` runs every cycle (STEP 1c) in main loop. Fetches `client.get_positions()` from Kalshi, removes phantom positions from risk_state, adds missing positions. Ensures risk_state always matches Kalshi's actual portfolio. Dashboard `/api/positions` also fetches live from Kalshi API (returns `source: "live"` or `"cached"` fallback).
- **Open-Meteo rate limiting (March 2026)**: Free tier = 10K requests/day per IP. Railway shared IPs exhaust this. Fix: `_in_fetch_window()` in `weather_engine.py` gates ALL Open-Meteo calls to `OPEN_METEO_FETCH_START_ET` (8) – `OPEN_METEO_FETCH_END_ET` (18). Outside window: stale cache returned (any age) or None. Cache TTLs aligned to model update frequency (6-hourly): ensemble 60 min (`ENSEMBLE_CACHE_TTL=3600`), distribution 60 min (`DISTRIBUTION_CACHE_TTL=3600`), cloud cover 120 min (`CLOUD_COVER_CACHE_TTL=7200`). Confirmer deterministic cache: 120 min (`_MODEL_CACHE_TTL=7200`). `signal_confirmer.py` imports `_in_fetch_window` and gates deterministic model fetches. Strategy returns `"outside_fetch_window"` skip reason (not `"ensemble_fetch_failed"`). Exits use METAR/NWS (not Open-Meteo), so work 24/7. Set `OPEN_METEO_API_KEY` env var to bypass all limits (switches to `customer-api.open-meteo.com`). Budget: ~1,200 calls/day (88% margin).
- **Early-morning guard tightening (March 2026)**: 6-9 AM local trades use 2.0x edge multiplier (14% threshold, was 1.5x/10.5%). NO-side trades in this window get additional 50% sizing penalty. Winter bias months extended to Dec-Mar (was Dec-Feb) to cover March transition month. SEA (+2F) and SFO (+1.5F) added to `_WARM_CITY_BIAS`. `NO_SIDE_MAX_PRICE_CENTS` lowered to 50 (was 60). `NO_SEPARATION_FLOOR_F` reverted to 2.0 (guard blocking 70% winners).
- **Confirmation & longshot loosening (March 2026)**: Guard effectiveness showed `confirmation_reject` blocking 63% winners and `longshot_floor` blocking profitable trades. Fixes: `LONGSHOT_FLOOR_CENTS` 5→3. Gray zone 2.0F→1.5F (more decisive votes). **NWS DISAGREE = hard REJECT (March 22)**: NWS is settlement authority. Trading against NWS is trading against the settlement source. **REJECT high-edge bypass DELETED (March 22)**: was 0/6 win rate (-$11.47). REJECT verdict = always reject, no bypass.
- **v4.6 data-driven fixes (March 22, 2026)**: Based on March 17-21 analysis (3W/21L, -$19). REJECT bypass deleted (0/6 win rate). `NEAR_CERTAINTY_CAP_CENTS` 93→80. PHI (2.0x) and HOU (1.5x) added to `CITY_EDGE_MULTIPLIERS`. City bias safety gate: `|bias| > 5F` with 5+ data points blocks trading for that city. Per-ticker contract accumulation guard added to `add_pending_order()`. STRONG taker threshold raised from 12% to 20% (`STRONG_TAKER_MIN_FEE_ADJ_EDGE`). Contract count reconciliation added to `_reconcile_positions()`. Learning sync logging after nightly review.
- **v4.7 comprehensive audit fixes (March 22, 2026)**: 13-fix audit of untested logic. (1) NWS DISAGREE = hard REJECT, no exceptions. (2) Convergence no longer lowers edge threshold (sizing boost only, capped at 1.3x, score threshold 0.5→0.7). (3) CASE 1 NO price cap 98c→60c. (4) Weather bias stacking capped at -2.0F. (5) Settlement check runs before P&L sync (prevents duplicate recording). (6) CASE 3 prob capped at 0.90. (7) Model convergence 1.2x boost moved inside 2.0x multiplier cap. (8) Exit Rule 5 falls back to market price when forecast unavailable. (9) Taker mode requires fee-adjusted edge >10% for confirmed. (10) 1-model CONFIRM requires ≥2 total voters. (11) March/Nov get 50% winter bias (shoulder decay). (12) Adverse selection minimum 5→3 orders. (13) Stale order afternoon threshold 5→10 minutes.
- **v5.0 backtest-driven model weights + bias corrections (March 22, 2026)**: 365-day × 20-city backtest (7,300 data points) from IEM actuals + Open-Meteo historical forecasts. (1) GFS gets 37.5% default weight (1.8F MAE, 0.0 bias) vs ECMWF/GEM at 20.8% each (3.0F and 2.8F MAE). `DEFAULT_MODEL_WEIGHTS` in config.py used when no per-city learned weights available. (2) Winter biases completely replaced with backtest-validated values: DEN 4.0→0.0, HOU 3.0→0.2, ATL 3.0→1.7, NOLA 3.0→1.1, SFO 1.5→0.1. Added NYC +2.2, BOS +1.4, PHI +1.9. (3) LAX ECMWF hard cap at 10% weight (6.6F MAE, 3x worse than GFS 1.1F). (4) Seasonal edge multipliers: March 1.2x, April/May 1.1x (spring MAE 2.4F vs fall 2.1F). (5) Per-city backtest weights seeded into learning_state for all 20 cities via `seed_backtest_weights()`. Live learning overrides when sufficient data accumulates.
- **v4.9 automatic daily learning pipeline (March 22, 2026)**: Scores ALL scan predictions (not just traded ones) against NWS actuals every night. (1) `_cache_actual_temps()` permanently caches NWS observations before 7-day API window expires. (2) `_compress_daily_record()` boils raw scan snapshots into compact `learning_history.json` with per-city MAE/bias, per-model errors, per-prediction outcomes. (3) Morning retry at 6 AM ET catches West Coast overnight NWS updates. (4) `_retry_missing_actuals()` re-scores recent dates where actuals were unavailable. (5) Backfilled existing 7 days: 1,618 predictions, 323 scored. File grows ~5KB/month (vs 1.1MB/week raw snapshots). Cumulative stats auto-recomputed: Brier score, per-city/model MAE, per-hour accuracy. Future opportunity boosters will read from this compact dataset.
- **v4.8 conservative learning auto-application (March 22, 2026)**: Safety-gate-only approach: learning can block trades but never boost them. (1) Per-city pattern blocking: cities with ≥5 trades and <20% win rate auto-blocked (currently: CHI 0/5). (2) City bias requires `LEARNING_MIN_DATA_POINTS` (3) before applying (was 1). (3) Model weights require 3+ errors per model before computing. (4) `LEARNING_AUTO_APPLY` kill switch in config. (5) Scan snapshot retention 7→30 days. (6) `get_losing_patterns()` API for strategy consumption.
- **v4.2 bug fixes (March 2026)**: Morning edge premium now uses local_hour (was ET, broke West Coast). Convergence sizing boost applied pre-Kelly-caps via multiplier (was post-caps, getting wasted). Cloud/precip bias applied to ensemble members before distribution build (was only adjusting mean, bucket probs were stale). NWS actual temp query widened to 06:00Z-08:00Z+1d to capture West Coast late highs. Exit retry: `_execute_exit()` retries once on failure. P/L ratio returns neutral 2.0 with <5 trades (prevents wild sizing swings). `forecast_mean=None` guard in strategy. Cloud cover NaN/Inf filtered. Cache timezone fixed to UTC.
- **v4.3 institutional review fixes (March 2026)**: CASE 1/3 edge was `(100-price)/100` (payout ratio) instead of `our_prob - market_prob` — inflated edge, wrong Kelly tier, oversized positions ~33%. Fixed. Bayesian observation update was double-shifting ensemble members already above obs — fixed to keep them as-is. Kill switch reset on every restart due to `isinstance(value, type(None))` check — fixed to only set defaults for missing keys. DRY_RUN exit was clearing real positions from risk state — removed `close_position()` call. Trade reconcile double-counted revenue (pair + settlement) — now uses max of the two. Market scanner lacked pagination — now follows cursor. CASE 3 Kelly omitted `model_spread` — added. Dashboard `_write_json` now uses `atomic_json_save`. Bucket probs recomputed after Bayesian update. Adverse selection fill_rate capped at 1.0. REJECT override no longer stacks with convergence boost. CORS: Authorization header added. CASE 1 min edge lowered to 2% (`CASE1_MIN_EDGE`), NO price cap raised to 98c (was 95c) to catch profitable confirmed outcomes.
- **v4.4 debug pass (March 2026)**: Balance cache falsy check fixed (`is not None` instead of truthiness — 0 balance was treated as cache miss). NWS temp C→F conversion changed from `round()` to `math.floor()` (matches METAR conservative approach, prevents false CASE 1 triggers from rounding up). P/L ratio sizing check now requires BOTH wins≥5 AND losses≥5 (was only checking wins). CASE 1 fee-adjusted threshold lowered to 1% (`CASE1_FEE_ADJUSTED_MIN_EDGE`) since confirmed outcomes are near-guaranteed. Cloud/precip bias now applied to per-model means before computing `model_spread` (was inflating spread on cloudy days). Model convergence boost (1.2x) now requires ≥2 models and ≥80 ensemble members (was triggering on single-model with spread=0). CASE 3 defaults to conservative 8F model_spread when ensemble unavailable (was None → no dispersion penalty).
- **Dashboard auth (March 2026)**: Bearer token auth via `DASHBOARD_TOKEN` env var. Protects `/api/state`, `/api/fills`, `/api/balance`, `/api/learning`, and all POST endpoints. `/api/health` stays public for Railway health checks. Bearer header only (query param auth removed). Empty token = no auth (local dev).
- **METAR primary, NWS fallback**: `fetch_metar_batch()` fetches all 20 stations in one request via AviationWeather API. `get_todays_high()` and `get_current_temperature()` try METAR first, fall back to NWS on failure. Same ICAO station codes (KNYC, KMDW, etc.). METAR cache key: `metar_batch` (90s TTL). Per-station keys `obs_{station}` and `latest_{station}` shared between METAR and NWS paths. `METAR_ENABLED=False` in config disables METAR entirely.
- **Dashboard outside restart loop**: `start_dashboard_server()` in `__main__` block, NOT inside `main()`.
- **Python 3.12**: `import traceback as _tb` to avoid shadowing.
- **Timezone-aware**: `datetime.now(timezone.utc)`, `ZoneInfo(tz_name)`.
- **Open-Meteo model names**: `gfs_seamless`, `ecmwf_ifs025`, `icon_seamless_eps`, `gem_global`.
- **Open-Meteo confirmer URLs**: `/v1/forecast` (GFS), `/v1/ecmwf`, `/v1/dwd-icon`, `/v1/gem`.
- **Single-writer P&L**: Only `sync_pnl_from_kalshi()` writes `pnl_history.json`.
- **Single-writer learning**: Only `trade_reviewer._save_state()` writes `learning_state.json`.
- **Learning auto-application (conservative gates only)**: City biases auto-applied when `count >= LEARNING_MIN_DATA_POINTS` (3). Model weights auto-applied when 3+ errors per model. City bias blocks at `|bias| > 5F` with 5+ data points. Per-city pattern blocking: cities with ≥5 settled trades and <20% win rate auto-blocked. `LEARNING_AUTO_APPLY=False` disables all auto-application. Profitability metrics, calibration, guard stats, information decay = informational only (dashboard + daily report). Scan snapshot retention extended from 7 to 30 days.
- **Scan reconciliation**: `capture_scan_snapshot()` called every cycle with ALL evaluated signals (buy + skip). At 11 PM ET, `_reconcile_scans()` compares predictions against NWS actuals to classify correct_skips, missed_opportunities, correct_trades, bad_trades. `_analyze_guard_effectiveness()` tracks per-guard block accuracy. `_analyze_calibration()` computes Brier score across all probability predictions.
- **Rich skip signals**: `strategy._skip()` accepts `**kwargs` to attach forecast data. `_strategy_weather()` returns enriched skip dicts (not None) at every rejection point post-distribution-fetch. Includes `skip_reason`, `edge`, `our_prob`, `predicted_high`, `model_means`, etc.
- **Scan snapshot storage**: `learning_state.json["scan_snapshots"]` keyed by date, deduped by ticker. 7-day retention. Saved every 10th cycle to avoid I/O overhead.
- **CRPS model weighting**: `_learn_model_accuracy()` computes CRPS per model (Gaussian approximation via scipy.stats.norm) when `model_stds` available. Prefers CRPS over MAE for weight computation. Falls back to MAE for old snapshots.
- **Model stds propagation**: `weather_engine` computes `model_stds` per ensemble family. Stored in distribution dict, propagated through strategy signals into forecast snapshots.
- **Profitability metrics**: `_compute_profitability_metrics()` computes profit_factor and expectancy_cents from settled trades during nightly review. Stored in `learning_state.json["profitability"]`.
- **Brier decomposition**: `_analyze_calibration()` decomposes Brier into reliability (calibration error), resolution (discrimination), uncertainty (base rate). Lower reliability = better. Higher resolution = better.
- **Information decay curves**: `_analyze_information_decay()` groups predictions by local hour bucket (6-9, 9-12, 12-15, 15-18, 18+) and computes accuracy, edge realization, Brier per bucket. Stored in `learning_state.json["information_decay"]`.
- **Bucket inconsistency detection**: `strategy.detect_bucket_inconsistencies(markets)` checks if bucket YES prices sum to ~100c per event. Logs deviations > `BUCKET_SUM_DEVIATION_CENTS`. Informational only.
- **Learning NWS lookup**: `trade_reviewer._get_actual_temp()` fetches actual highs from NWS (cached, 7-day limit). Enables bias/accuracy learning + scan reconciliation.
- **CITIES dict field**: `nws_station` (NOT `station`). Used for observation fetching.
- **Daily reset at 6 AM ET**: Risk counters reset at 6 AM Eastern, not UTC midnight.
- **NWS observation cache**: 2-minute TTL (matches cycle interval). Used for exits and CASE 1.
- **Balance cache**: Risk manager caches balance for 60s to avoid excessive API calls.
- **Pending order overwrite**: `add_pending_order()` subtracts old cost before adding new. Prevents exposure drift.
- **Settlement reconciliation**: `check_settlements()` called every cycle in main loop (STEP 1b). Updates risk_state (record_win/loss, release_exposure). Saves trade_log after marking settled.
- **Exit fill check**: `_execute_exit()` only closes position in risk_state after confirmed fill (`status==executed` or `remaining_count==0`). Resting exits stay tracked.
- **Observed highs ET date**: `_fetch_observed_highs()` uses ET date (not UTC) for NWS observation queries. West Coast evening fix.
- **is_confirmed propagation**: `order_signal` includes `is_confirmed` and `is_arb` flags so maker tracks bypass privileges after fill.
- **Stale order cleanup**: Runs every cycle (not just when MAX_OPEN_ORDERS reached). Prevents stale orders blocking exposure capacity.
- **NaN/Inf guard**: `weather_engine._fetch_ensemble()` filters NaN/Inf from Open-Meteo API responses before building distribution.
- **Ensemble fetch logging**: Failed API calls logged with model name and error (was silently swallowed).
- **Sell P&L fix**: `sync_pnl_from_kalshi()` correctly treats sell proceeds as revenue (was adding to cost). Position direction corrected for unpaired sells.
- **Reviewer dedup**: `_learn_forecast_bias()` and `_learn_model_accuracy()` rebuild from scratch each night (was appending to existing, duplicating errors).
- **Graduated Kelly sizing**: `_kelly_size()` uses graduated divisor (6/4/3) based on **raw** edge (not fee-adjusted) + dispersion multiplier. `raw_edge` parameter selects divisor tier; fee-adjusted edge drives Kelly fraction. Convergence boost re-capped to `MAX_CONTRACTS_PER_TICKER` after 1.2x.
- **Taker mode**: CASE 1 confirmed outcomes with edge > 15% bypass maker limit pricing, use `place_market_order()`.
- **Time-decay stale cleanup**: `_get_stale_threshold_minutes()` returns 30/15/5 based on ET hour. Near settlement = faster cancellation.
- **Adverse selection**: `fill_tracking.json` tracks per-side fill rates. >70% = widen, >85% = pause. Integrated into `calculate_limit_price()`.
- **Convergence confidence**: After 2 PM, if obs tracks ensemble mean + low spread, edge threshold drops to 5% and sizing boosted up to 1.5x.
- **P/L ratio sizing**: Risk manager tracks rolling win/loss amounts. Ratio < 2.0 = 3% max position. Ratio > 3.0 = 7% max position.
- **Bayesian obs update**: `update_distribution_with_observation()` shifts ensemble based on NWS observations, weight scaling with hour.
- **Cloud cover bias**: `_fetch_cloud_cover()` gets cloud/precip data from Open-Meteo. High overcast = negative temp bias.
- **Portfolio rebalancing**: `_check_rebalancing()` runs every 15 cycles. Only exits weakest position if edge is negative (actively losing EV). Positive-edge positions held even at max capacity.
