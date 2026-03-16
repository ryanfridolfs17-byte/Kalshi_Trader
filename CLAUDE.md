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
| `dashboard.py` | ~780 | Web dashboard. `/api/health`, `/api/state`, `/api/force-exit` |

### Deleted Modules (v3 -> v4)
`trade_scorecard.py`, `self_improver.py`, `quant_analytics.py`, `seasonal_confidence.py`,
`trade_analyzer.py`, `volatility_engine.py`, `spx_confirmer.py`, `market_quality.py`

### State Files (7, down from 17)

`trade_history.json`, `risk_state.json`, `pnl_history.json`, `bot_status.json`, `maker_orders.json`, `scan_log.json`, `learning_state.json`

### Key Design Decisions

- **Graduated Kelly sizing** -- Edge 5-10%: Kelly/6, Edge 10-20%: Kelly/4, Edge 20%+: Kelly/3. Dispersion multiplier: `1/(1 + model_spread/5)`. Confirmation multiplier still applied.
- **Maker strategy** -- limit orders default. CASE 1 confirmed with edge >15% uses taker (market) orders for guaranteed execution.
- **NWS settlement** -- Weather markets settle on NWS Daily Climate Reports
- **NWS rounding** -- +/-1F DOS-era conversion error. Buffer: +/-1F = no trade, +/-2F = 50% size.
- **NO-side separation** -- Dynamic: `max(3.0F, std_dev * 0.6)` from expanded boundary. CONFIRM gets 1.25x penalty.
- **Fee-adjusted edge** -- 7% fee drag. Side-aware formula.
- **Only CASE 1 = CONFIRMED_OUTCOME** -- temp exceeded bucket, can't un-happen. CASE 2 DELETED. CASE 3 = STRONG.
- **Binary exits** -- HOLD or EXIT. No PARE/HEDGE/TAKE_PROFIT.
- **Single-writer P&L** -- Only `sync_pnl_from_kalshi()` writes `pnl_history.json`. No exceptions.
- **Size-down, not reject** -- When caps are exceeded, risk manager reduces contracts to fit instead of blocking.
- **Kill switch** -- Daily loss = stop for day (auto-resume). 3 consecutive = 4h pause. No Sharpe-based shutdown.

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
| `MIN_EDGE` | 7% | Morning (before noon): 8.4% (1.2x). Next-day: 10.5% (1.5x). Early morning (6-9 AM local): 14% (2.0x). |
| `CONFIRMED_MIN_EDGE` | 5% | CASE 1 confirmed outcomes |
| `FEE_ADJUSTED_MIN_EDGE` | 3% | After 7% fee drag |
| `MIN_PAYOUT_DOLLARS` | $0.25 | Minimum expected payout per trade |
| `DAILY_LOSS_LIMIT_CENTS` | 600 ($6) | ~15% of bankroll |
| `CONSECUTIVE_LOSS_PAUSE` | 3 losses / 60 min | |
| `KILL_SWITCH_CONSEC_LOSSES` | 3 | 4-hour pause, then auto-resume |

### Position Sizing & Exposure
| Parameter | Value | Notes |
|-----------|-------|-------|
| `MAX_POSITION_PCT` | 5% | Normal forecasts |
| `CONFIRMED_POSITION_PCT` | 10% | CASE 1 only |
| `ARB_POSITION_PCT` | 15% | Near risk-free arbitrage |
| `ARB_MIN_SPREAD_CENTS` | 7 | Min gap for arb trades (covers 7% fee) |
| `MAX_TOTAL_EXPOSURE_PCT` | 50% | Confirmed bypass |
| `MAX_PER_CITY_PCT` | 10% | Confirmed bypass |
| `MAX_PER_TICKER_CENTS` | 400 ($4) | Always enforced |
| `MAX_CONTRACTS_PER_TICKER` | 5 | Always enforced |
| `MAX_OPEN_POSITIONS` | 3 | Confirmed/arb bypass |
| `MAX_CORRELATED_POSITIONS` | 2 (3 confirmed) | Per city |
| `LIQUIDITY_RESERVE_PCT` | 20% | |
| `TRADE_COOLDOWN` | 120 sec | Same-cycle exempt |

### Strategy Guards
- **Rounding buffer**: +/-1F = no trade, +/-2F = 50% size
- **NO-side separation**: `max(3.0F, std_dev * 0.6)`. CONFIRM gets 1.25x penalty.
- **Model divergence**: YES >8F = skip, NO >10F = skip. <2F = 1.2x boost.
- **Longshot floor**: 3c. **Near-certainty cap**: 88c. **NO ceiling**: 50c (CASE1 bypasses).
- **NO sizing**: >=50c gets 40% normal sizing.
- **Next-day**: 1.5x edge threshold, 50% sizing.
- **Same-day before 6 AM local**: BLOCKED. Overnight forecasts are stale.
- **Same-day 6-9 AM local**: 2.0x edge threshold (14%). Stale forecast penalty. NO-side gets additional 50% sizing reduction.
- **Warm city bias defaults**: DEN +4F, Gulf/SE +3F, Desert +2F, PNW +2F/+1.5F (hardcoded in weather_engine.py). Applied in winter months (Dec-Mar).

### Confirmed Outcomes
- **CASE 1** (high exceeded bucket): CONFIRMED_OUTCOME. Min 10 AM local. `obs_high > temp_high + 1F`.
- **CASE 2**: DELETED from codebase.
- **CASE 3** (gap too large): Returns STRONG (not confirmed). `confirmation_multiplier=1.0`. Cooling gate before 2 PM. Ensemble veto. Graduated gaps.

### Exit Logic (Binary: HOLD or EXIT)
| Trigger | Action |
|---------|--------|
| Obs confirms loss / approaching bucket (1-7 PM) | EXIT (high) |
| Threshold market approaching within 5F (10 AM-1 PM) | EXIT (high) |
| Forecast divergence: obs high > forecast mean + 2F (10 AM+) | EXIT (high) |
| Rounding buffer after 2 PM | EXIT (high) |
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
| `CONVERGENCE_SCORE_THRESHOLD` | 0.7 | Min score to trigger convergence trading |
| `CONVERGENCE_MIN_LOCAL_HOUR` | 14 | Only after 2 PM local |
| `CONVERGENCE_SIZING_BOOST` | 0.5 | Up to 1.5x sizing at score=1.0 |
| `CLOUD_COVER_THRESHOLD_PCT` | 70 | Above = apply temp bias |
| `CLOUD_COVER_TEMP_BIAS_F` | -1.5 | Overcast day bias |
| `PRECIP_THRESHOLD_MM` | 0.5 | Above = apply additional bias |
| `PRECIP_TEMP_BIAS_F` | -1.0 | Precipitation cooling bias |
| `REBALANCE_INTERVAL_CYCLES` | 15 | Check rebalancing every N cycles |
| `REBALANCE_MIN_NEW_EDGE` | 0.15 | Min edge for new opportunity |
| `REBALANCE_MAX_OLD_EDGE` | 0.03 | Max edge to consider for exit |
| `TAKER_MODE_MIN_EDGE` | 0.15 | Min edge for CASE 1 taker mode |
| `BUCKET_SUM_DEVIATION_CENTS` | 8 | Min deviation from 100c to flag bucket inconsistency |
| `BUCKET_SUM_MIN_MARKETS` | 5 | Min buckets in event to analyze |
| `METAR_CACHE_TTL_SEC` | 90 | METAR batch cache lifetime (under 2-min cycle) |
| `METAR_REQUEST_TIMEOUT` | 10 | HTTP timeout for METAR batch request |
| `METAR_HOURS_LOOKBACK` | 18 | Hours of METAR history per request |
| `METAR_ENABLED` | True | Kill switch for METAR (falls back to NWS-only) |
| `OPEN_METEO_FETCH_START_ET` | 8 | Start of Open-Meteo fetch window (8 AM ET) |
| `OPEN_METEO_FETCH_END_ET` | 18 | End of fetch window (6 PM ET) |
| `ENSEMBLE_CACHE_TTL` | 900 | 15 min ensemble per-model cache |
| `DISTRIBUTION_CACHE_TTL` | 900 | 15 min distribution cache |
| `CLOUD_COVER_CACHE_TTL` | 1800 | 30 min cloud cover cache |
| `EARLY_MORNING_EDGE_MULTIPLIER` | 2.0 | 6-9 AM local: 14% min edge (stale 00Z forecasts) |

### New State Files (v4.1)
- `fill_tracking.json` — Adverse selection: per-side order/fill counts (rolling 24h)

### Infrastructure Invariants
- **Kalshi API field normalization (March 2026)**: Kalshi changed response field names: `yes_ask` → `yes_ask_dollars` (string), `volume` → `volume_fp` (string). Response `status` field changed to `"active"` but **query parameter** still uses `status=open`. `kalshi_client._normalize_market()` converts new dollar-string fields to integer cents for all internal code. `market_scanner.py` has its own `_normalize_scanner_market()` since it uses direct `requests.get`. All other files use cents integers unchanged.
- **Open-Meteo rate limiting (March 2026)**: Free tier = 10K requests/day per IP. Railway shared IPs exhaust this. Fix: `_in_fetch_window()` in `weather_engine.py` gates ALL Open-Meteo calls to `OPEN_METEO_FETCH_START_ET` (8) – `OPEN_METEO_FETCH_END_ET` (18). Outside window: stale cache returned (any age) or None. Cache TTLs extended: ensemble 15 min (`ENSEMBLE_CACHE_TTL=900`), distribution 15 min (`DISTRIBUTION_CACHE_TTL=900`), cloud cover 30 min (`CLOUD_COVER_CACHE_TTL=1800`, was uncached!). `signal_confirmer.py` imports `_in_fetch_window` and gates deterministic model fetches. Strategy returns `"outside_fetch_window"` skip reason (not `"ensemble_fetch_failed"`). Exits use METAR/NWS (not Open-Meteo), so work 24/7. Set `OPEN_METEO_API_KEY` env var to bypass all limits (switches to `customer-api.open-meteo.com`). Budget: ~4,700 calls/day (53% margin).
- **Early-morning guard tightening (March 2026)**: 6-9 AM local trades use 2.0x edge multiplier (14% threshold, was 1.5x/10.5%). NO-side trades in this window get additional 50% sizing penalty. Winter bias months extended to Dec-Mar (was Dec-Feb) to cover March transition month. SEA (+2F) and SFO (+1.5F) added to `_WARM_CITY_BIAS`. `NO_SIDE_MAX_PRICE_CENTS` lowered to 50 (was 60). `NO_SEPARATION_FLOOR_F` raised to 3.0 (was 2.0).
- **Confirmation & longshot loosening (March 2026)**: Guard effectiveness showed `confirmation_reject` blocking 63% winners and `longshot_floor` blocking profitable trades. Fixes: `LONGSHOT_FLOOR_CENTS` 5→3. Gray zone 2.0F→1.5F (more decisive votes). NWS veto softened: NWS DISAGREE + 2 models agree = CONFIRM at 0.5x sizing (was hard REJECT). High-edge bypass: edge ≥25% + same-day + after 9 AM local allows REJECT trades at 0.4x conf_mult.
- **METAR primary, NWS fallback**: `fetch_metar_batch()` fetches all 20 stations in one request via AviationWeather API. `get_todays_high()` and `get_current_temperature()` try METAR first, fall back to NWS on failure. Same ICAO station codes (KNYC, KMDW, etc.). METAR cache key: `metar_batch` (90s TTL). Per-station keys `obs_{station}` and `latest_{station}` shared between METAR and NWS paths. `METAR_ENABLED=False` in config disables METAR entirely.
- **Dashboard outside restart loop**: `start_dashboard_server()` in `__main__` block, NOT inside `main()`.
- **Python 3.12**: `import traceback as _tb` to avoid shadowing.
- **Timezone-aware**: `datetime.now(timezone.utc)`, `ZoneInfo(tz_name)`.
- **Open-Meteo model names**: `gfs_seamless`, `ecmwf_ifs025`, `icon_seamless_eps`, `gem_global`.
- **Open-Meteo confirmer URLs**: `/v1/forecast` (GFS), `/v1/ecmwf`, `/v1/dwd-icon`, `/v1/gem`.
- **Single-writer P&L**: Only `sync_pnl_from_kalshi()` writes `pnl_history.json`.
- **Single-writer learning**: Only `trade_reviewer._save_state()` writes `learning_state.json`.
- **Learning = informational only**: Learned biases/weights are NOT auto-applied. Displayed in dashboard + daily report.
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
- **Graduated Kelly sizing**: `_kelly_size()` uses graduated divisor (6/4/3) based on edge + dispersion multiplier. `model_spread` parameter added.
- **Taker mode**: CASE 1 confirmed outcomes with edge > 15% bypass maker limit pricing, use `place_market_order()`.
- **Time-decay stale cleanup**: `_get_stale_threshold_minutes()` returns 30/15/5 based on ET hour. Near settlement = faster cancellation.
- **Adverse selection**: `fill_tracking.json` tracks per-side fill rates. >70% = widen, >85% = pause. Integrated into `calculate_limit_price()`.
- **Convergence confidence**: After 2 PM, if obs tracks ensemble mean + low spread, edge threshold drops to 5% and sizing boosted up to 1.5x.
- **P/L ratio sizing**: Risk manager tracks rolling win/loss amounts. Ratio < 2.0 = 3% max position. Ratio > 3.0 = 7% max position.
- **Bayesian obs update**: `update_distribution_with_observation()` shifts ensemble based on NWS observations, weight scaling with hour.
- **Cloud cover bias**: `_fetch_cloud_cover()` gets cloud/precip data from Open-Meteo. High overcast = negative temp bias.
- **Portfolio rebalancing**: `_check_rebalancing()` runs every 15 cycles. Exits weakest position if at max capacity and edge < 3%.
