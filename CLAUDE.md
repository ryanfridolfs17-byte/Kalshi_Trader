# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Git & Deployment

- **Working branch:** `master` — day-to-day development happens here.
- **Production branch:** `main` — Railway auto-deploys from this branch.
- **Deploy workflow:** After committing to `master`, merge into `main` and push to trigger a Railway deploy:
  ```bash
  git checkout main && git merge master && git push && git checkout master
  ```
- When the user says "push to Railway" or "deploy", this means merge `master` → `main` and push `main`.
- Never force-push to `main`.

## Running the Bot

```bash
python kalshi_bot.py
```

No build step, no test framework, no linter configured. All configuration is in `config.py` (not environment variables or .env files).

**Progression levels** (set in `config.py`):
1. `DRY_RUN=True` — analysis only, no orders placed
2. `DRY_RUN=False, ENVIRONMENT="demo"` — practice money on Kalshi demo server
3. `DRY_RUN=False, ENVIRONMENT="production"` — real money, requires interactive confirmation

## Dependencies

Core: `requests`, `cryptography`, `numpy`, `scipy`, `pandas`, `yfinance`. Listed in `requirements.txt`.

External APIs (all free, no keys needed): Open-Meteo (ensemble forecasts + historical archive), NWS (settlement source + live observations), yfinance (SPY/VIX data for S&P 500 strategy).

Kalshi API requires RSA key-pair auth — credentials set via `API_KEY_ID` and `PRIVATE_KEY_PATH` in `config.py`.

## Architecture

Multi-market prediction market trading bot for Kalshi. It runs a continuous loop (every 2 minutes, 1 minute during peak hours 12-5 PM ET) that scans enabled market types, detects mispricing, and places limit orders.

**Market types** are toggleable via env vars in `config.py` (`MARKET_TYPES` dict). Weather is ON by default; S&P 500 is OFF by default (`ENABLE_SP500=true` to activate). New market types should follow this pattern.

### Decision Flow

```
market_scanner.py → strategy.py → confirmer → trade_scorecard.py → risk_manager.py → maker_strategy.py → kalshi_client.py
   (discover)       (evaluate)    (validate)   (8-criteria gate)    (safety check)    (maker pricing)     (execute)
```

Weather uses `signal_confirmer.py` (5-source voting: 4 deterministic models + NWS station point forecast). S&P 500 uses `spx_confirmer.py` (momentum/vol/historical). All trades pass through `trade_scorecard.py` recursive evaluation (confirmed outcomes and arbitrage bypass).

**Edge-priority execution:** Both weather and S&P 500 scan loops use a two-phase design. Phase 1 evaluates all markets and collects actionable signals. Phase 2 sorts signals by edge descending, then processes through risk checks and execution. This ensures the highest-edge trade gets first shot at per-city and per-position caps.

### Module Responsibilities

- **`kalshi_bot.py`** — Entry point and main loop. Orchestrates scan→analyze→trade cycle per market type. Two-phase scan: evaluate all markets first, then sort by edge and execute highest-edge trades first. Handles exits and settlement tracking.
- **`kalshi_client.py`** — Kalshi API wrapper. RSA-PSS signature auth, dual environment support (demo vs production URLs), market fetching, order placement, position queries.
- **`config.py`** — All tunable parameters: credentials, risk limits, strategy thresholds, scan intervals, market type toggles. Values are in cents (100 cents = $1.00).
- **`market_scanner.py`** — Queries Kalshi for weather series + S&P 500 brackets. `scan_all_enabled_markets()` respects `MARKET_TYPES` toggles.
- **`strategy.py`** — Three strategies: **S1 (Weather Edge)** ensemble vs market prices; **S2 (Spread Arbitrage)** YES+NO < $0.98; **S3 (SP500 Brackets)** VIX-implied distributions vs bracket prices. S3 components only initialized when sp500 is enabled.
- **`weather_engine.py`** — Aggregates 143 ensemble members from 4 sources (GFS 31, ECMWF 51, ICON-EPS 40, GEM 21) via Open-Meteo. Builds weighted probability distributions across temperature buckets. Accepts `model_weights` dict from `quant_analytics.get_model_weights()` — each ensemble member is weighted by its source model's inverse-RMSE accuracy score. With no weights (or insufficient accuracy data), falls back to equal weighting.
- **`volatility_engine.py`** — VIX-based price distributions for S&P 500. Uses yfinance for SPY/VIX data, scipy.stats.norm CDF for bracket probability calculations. Includes intraday vol adjustment.
- **`signal_confirmer.py`** — Weather voting system from 5 independent sources: 4 deterministic models (GFS/HRRR, ECMWF, ICON, GEM) via Open-Meteo + NWS station point forecast (api.weather.gov). Outputs: STRONG (3+ agree AND NWS agrees, 1.5x), CONFIRM (2+, NWS abstains, 1.0x), REJECT. **WEAK signals are now hard-rejected** (0% historical win rate). NWS source uses 2°F abstain zone on both underpriced and overpriced paths. **NWS DISAGREE = hard REJECT** — NWS is the settlement source, so if it explicitly contradicts the signal, the trade is blocked regardless of how many models agree. **NWS ABSTAIN = cap at CONFIRM** (never STRONG). **STRONG requires explicit NWS AGREE.**
- **`spx_confirmer.py`** — S&P 500 signal confirmation: intraday momentum, realized vs implied vol ratio, historical bracket hit rate. Same verdict/multiplier interface as weather confirmer.
- **`risk_manager.py`** — Safety layers (see Risk Parameters section below). Key checks: daily loss limit, dynamic exposure cap (60% bankroll), per-city cap (20% bankroll), per-ticker cap ($15), daily forecast trade cap (8/day, confirmed outcomes exempt), correlated position cap, loss streak pause, cooldown, settlement proximity, liquidity reserve, kill switch (Sharpe + consecutive loss). State persisted in `risk_state.json`.
- **`trade_intelligence.py`** — Position exit logic (including rounding-buffer exits for NO positions), forecast bias learning per NWS station (with 3-day streak detection), time-of-day sizing adjustments, intraday observation tracking, settlement P&L recording with `settled_at` timestamps.
- **`quant_analytics.py`** — Backtesting against 90 days of historical data, per-model accuracy weighting (inverse-RMSE, fed into `weather_engine._build_distribution()` via `model_weights` parameter), regime detection (stable vs volatile), smart order placement, correlation-aware position sizing. Model accuracy is recorded on each settlement via `_update_model_accuracy_from_settlement()` in `trade_intelligence.py`, which prints a per-model error summary.
- **`seasonal_confidence.py`** — Monthly position sizing multipliers (0.5–1.3x) per city based on NWP model verification and weather regime. Desert cities get boost in summer (predictable), spring transition cities get penalty (frontal chaos). Regime detection compares forecast to NOAA climatological normals. Learned weights persisted in `seasonal_weights.json`.
- **`market_quality.py`** — Liquidity filter (max 20c spread, min 1 contract volume), probability guardrails (rejects <5c longshots and >88c near-certainties).
- **`trade_scorecard.py`** — v4.0 recursive evaluation loop. 8 criteria (data integrity, forecast convergence, edge magnitude, timing window, liquidity, portfolio correlation, position sizing, adversarial check). Max 3 iterations of diagnose→fix→retry. Confirmed outcomes and arbitrage bypass. Actions: execute/reject/defer.
- **`maker_strategy.py`** — v4.0 maker execution engine. Posts limit orders at fair_value - spread_buffer (default 2¢). Manages order lifecycle: placement, fill tracking, stale order cancellation (30min), adverse selection detection. State persisted in `maker_orders.json`.
- **`trade_analyzer.py`** — End-of-day post-mortem analysis. Analyzes settled trades: entry edge vs outcome, per-model accuracy scorecard, guard effectiveness review, exit timing analysis, Kelly sizing review. Called on day change, writes to `trade_analysis.json`.
- **`dashboard.py`** — Flask-based web dashboard with health monitoring, email alerts, position/trade display with market type badges.

### Data Files (Runtime State)

All state is persisted as JSON in `STATE_DIR` (defaults to `.`, set to `/data` on Railway for volume persistence): `trade_history.json`, `risk_state.json`, `pnl_history.json`, `backtest_results.json`, `edge_attribution.json`, `bot_status.json`, `pending_trades.json`, `scan_log.json` (per-market evaluation log, 7-day rolling retention, exposed via `/api/state`), `maker_orders.json` (tracked open maker orders), `model_accuracy.json` (per-model forecast error tracking), `daily_reports.json` (daily P&L summaries), `trade_analysis.json` (post-settlement trade analysis), `bias_history.json` (NWS station forecast bias history), `seasonal_weights.json` (learned seasonal sizing adjustments).

### Key Design Decisions

- **Quarter-Kelly sizing** — Position sizes use Kelly criterion divided by 4 for safety, multiplied by the signal confirmation level.
- **Maker strategy** — Prefers limit orders (not market orders) for better fills.
- **NWS settlement** — Weather markets settle on NWS Daily Climate Reports from specific stations, not model forecasts. See NWS Station Mappings below.
- **NWS rounding awareness** — NWS 5-minute stations have ±1°F+ error from DOS-era °F→°C→°F conversion. Settlement uses raw 1-minute readings (can be higher than displayed time series). Bot applies rounding buffer: ±1°F of strike = no trade, ±2°F = 50% size reduction.
- **Fee-adjusted edge** — Raw edge is reduced by estimated Kalshi fee drag (7% of profit) before checking minimum edge threshold. Net edge must survive at 5%+ after fees (was 4%). Fee formula is side-aware: YES uses `7% × (100 - yes_price)`, NO uses `7% × yes_price`.
- **Longshot avoidance** — Never buys contracts priced below 5¢ (was 12¢, lowered to capture confirmed outcomes at 5-11¢).
- **Market type isolation** — Each market type is gated behind a toggle. New market code paths only execute when explicitly enabled. SP500 uses `city_code="SP500"` in the risk manager for per-market exposure tracking.
- **Duplicate order detection** — Before executing any trade, checks `risk.state["positions"]` and `trade_log` resting orders for the same ticker. Prevents multi-cycle order stacking (e.g., 3 identical orders across 3 cycles).
- **Contract count cap** — Hard cap of 15 contracts per ticker (`MAX_CONTRACTS_PER_TICKER`, was 25), applied in Kelly sizing, confirmed outcome sizing, and risk manager. Prevents cheap contract explosion (e.g., 68 contracts at 22c each).
- **CASE 2 disabled (recovery mode)** — YES-side confirmed outcomes (`CASE2_ENABLED=False`) are disabled due to NWS observation staleness vulnerability. CASE 1 (NO on exceeded) and CASE 3 (NO on unreachable) remain active. Re-enable when bankroll exceeds $80.
- **Medium urgency exits never auto-execute** — Only `high` urgency exits (confirmed losses from observations) auto-sell. Medium urgency (thesis uncertain + profitable) logs recommendation for manual review.
- **Thesis-based exit logic** — Exits are driven by whether the model/observations still predict the position wins at settlement, NOT by edge erosion. Edge going to zero on a winning position means the market caught up (thesis confirmed) — hold for settlement. Exits only fire when: (1) observations confirm a loss, (2) NWS disagrees, (3) ensemble strongly contradicts position (<15% YES floor, >85% NO floor), or (4) thesis is broken (model flipped against us).

## NWS Station Mappings (20 Cities)

Weather markets settle on NWS Daily Climate Reports from these specific stations. The bot must forecast for the correct station coordinates — wrong station = wrong forecast.

| Code | City | NWS Station |
|------|------|-------------|
| NYC | New York Central Park | KNYC |
| CHI | Chicago Midway | KMDW |
| MIA | Miami International | KMIA |
| AUS | Austin-Bergstrom | KAUS |
| LAX | Los Angeles | KLAX |
| DEN | Denver | KDEN |
| PHI | Philadelphia | KPHL |
| ATL | Atlanta | KATL |
| BOS | Boston | KBOS |
| DAL | Dallas | KDFW |
| DC | Washington DC | KDCA |
| HOU | Houston Hobby | KHOU |
| LV | Las Vegas | KLAS |
| MIN | Minneapolis | KMSP |
| NOLA | New Orleans | KMSY |
| OKC | Oklahoma City | KOKC |
| PHX | Phoenix | KPHX |
| SATX | San Antonio | KSAT |
| SEA | Seattle | KSEA |
| SFO | San Francisco | KSFO |

**Critical note:** Houston uses KHOU (Hobby Airport), NOT KIAH (Intercontinental). These are 24 miles apart with different microclimates. Station mappings live in `weather_engine.py` CITIES dict.

## Actual P&L Reconciliation (Feb 15–23, 2026)

**Reconciled against Kalshi API fills + settlements. Matches account balance to the penny.**

| Metric | Value |
|--------|-------|
| Deposited | $100.00 |
| Current Balance | $39.83 |
| **Account P&L** | **-$60.17** |
| Total Trades | 62 |
| Wins / Losses | 35W / 27L |
| **Win Rate** | **56.5%** |
| Avg Win | $1.68 |
| Avg Loss | $4.41 (2.6x avg win) |
| Total Won | $58.80 |
| Total Lost | $118.97 |
| Total Fees | $12.63 |
| Biggest Win | $8.87 (DAL T77 Feb 18) |
| Biggest Loss | -$22.75 (AUS B84.5 Feb 17) |

### Top 5 Losses (account for $70.07 — more than total drawdown)

| Trade | Side | Cost | P&L | Root Cause |
|-------|------|------|-----|------------|
| AUS B84.5 Feb 17 | NO @ 68c | $22.19 | **-$22.75** | Resting orders bypassed per-ticker cap; ensemble underestimated warm day |
| MIA B79.5 Feb 17 | NO @ 68c | $58.65 | **-$22.42** | $58 position on $100 bankroll — resting order accumulation bug |
| DAL B77.5 Feb 19 | NO @ 33c | $13.88 | **-$14.04** | Warm front missed by ensemble; no approaching-bucket exit |
| OKC B72.5 Feb 18 | YES @ 22c | $55.77 | **-$5.68** | Massive position, thin exit; ensemble overconfident on warm day |
| DEN B52.5 Feb 22 | NO @ 68c | $5.44 | **-$5.44** | Chinook wind event; DEN Feb seasonal multiplier too high |

### Root Cause Summary

1. **Resting orders bypassed risk caps** — Maker 1-at-a-time orders were invisible to per-ticker/per-city caps, allowing positions like MIA B79.5 to reach $58.65 (59% of bankroll)
2. **NO exit detection too late** — Exit only fires when obs_high is already inside bucket, not when approaching
3. **NWS REJECT didn't auto-exit** — Medium urgency on profitable positions = no action taken
4. **Ensemble cool bias in winter** — All 4 models systematically underpredict warm-city highs in Feb
5. **Seasonal confidence inflated** — MIA at 1.10x in Feb while losing $22 on MIA

### Daily P&L

| Date | W/L | Day P&L | Running |
|------|-----|---------|---------|
| Feb 17 | 2W/1L | +$4.63 | +$4.63 |
| Feb 18 | 6W/3L | -$36.01 | -$31.38 |
| Feb 19 | 3W/4L | +$0.05 | -$31.33 |
| Feb 20 | 1W/2L | -$12.11 | -$43.44 |
| Feb 21 | 12W/5L | -$8.60 | -$52.04 |
| Feb 22 | 3W/2L | -$1.21 | -$53.25 |
| Feb 23 | 8W/10L | -$6.92 | -$60.17 |

## Risk Parameters (Current Values) — RECOVERY MODE

**IMPORTANT: Any changes to risk parameters or strategy rules MUST be reflected here. This is the source of truth for risk documentation.**

**RECOVERY MODE ACTIVE (Feb 2026):** Bankroll is $40 (down from $100). All parameters tightened to protect remaining capital while generating consistent small profits. Strategy: focus on CASE 1/3 confirmed outcomes (near-guaranteed NO trades), disable risky CASE 2 (YES on current bucket), fewer speculative trades, tighter loss limits. Exit recovery mode when bankroll exceeds $80 — then gradually loosen parameters back toward normal.

All values are set in `config.py`. Values in cents (100 cents = $1.00).

### Position Sizing
| Parameter | Value | Notes |
|-----------|-------|-------|
| `MAX_POSITION_PCT` | 20% of bankroll | Max single position (caps contracts down instead of rejecting) |
| `CONFIRMED_OUTCOME_POSITION_PCT` | 25% of bankroll | CASE 1/3 confirmed (NO on exceeded/unreachable buckets, was 30%). Also hard-capped by `DAILY_LOSS_LIMIT_CENTS` — max loss from any single confirmed outcome cannot exceed daily loss limit |
| `CASE2_ENABLED` | **False** | CASE 2 DISABLED in recovery mode — observation staleness bug |
| `CASE2_POSITION_PCT` | 8% of bankroll | When re-enabled (was 10%) |
| Quarter-Kelly | Kelly/4 | Base sizing formula, multiplied by confirmation level |

### Exposure Caps (Dynamic, Percentage-Based)
| Parameter | Value | Notes |
|-----------|-------|-------|
| `MAX_TOTAL_EXPOSURE_PCT` | 60% of bankroll | Was 40% — confirmed outcomes bypass entirely |
| `MAX_TOTAL_EXPOSURE_CENTS` | $18.00 | Fallback |
| `MAX_PER_CITY_PCT` | 15% of bankroll | Confirmed outcomes bypass |
| `MAX_PER_CITY_CENTS` | $6.00 | Fallback (was $10.00) |
| `MAX_DAILY_CITY_SPEND_CENTS` | $12.00 | Confirmed outcomes bypass |
| `MAX_PER_TICKER_CENTS` | $8.00 | Still enforced for confirmed outcomes |
| `MAX_CONTRACTS_PER_TICKER` | 15 | Still enforced for confirmed outcomes |
| `MAX_OPEN_POSITIONS` | 6 | Confirmed outcomes bypass |
| `MAX_CORRELATED_POSITIONS` | 2 | Was 3 (3 for confirmed outcomes, was 4) |

**Confirmed outcome bypass:** CASE 1/3 trades (`CONFIRMED_OUTCOME` verdict) bypass: total exposure cap, per-city cap, daily city spend cap, max positions cap, and liquidity reserve. They still respect: daily loss limit, per-ticker cap, contract cap, correlated positions cap, consecutive loss pause, and cooldown.

### NWS Rounding Buffer
| Parameter | Value | Notes |
|-----------|-------|-------|
| `ROUNDING_BUFFER_HARD_F` | 1°F | Forecast within ±1°F of strike = NO TRADE |
| `ROUNDING_BUFFER_SOFT_F` | 2°F | Forecast within ±2°F of strike = 50% size |
| `MIN_FORECAST_STRIKE_SEPARATION_F` | 3°F | NO-side forecast must be ≥3°F from nearest strike |

### Model Divergence Gate
| Parameter | Value | Notes |
|-----------|-------|-------|
| `MAX_MODEL_DIVERGENCE_F` | 4°F | Model spread >4°F = NO TRADE (was 5°F) |
| `MODEL_CONVERGENCE_BOOST_F` | 2°F | Model spread <2°F = 1.2x confidence boost |

### Fee-Adjusted Edge
| Parameter | Value | Notes |
|-----------|-------|-------|
| `KALSHI_FEE_PCT` | 7% | Worst-case taker fee (maker is lower/zero) |
| `FEE_ADJUSTED_MIN_EDGE` | 5% | Min net edge after fee drag (was 4%) |
| `MIN_EDGE` | 10% | Raw minimum edge (was 8%) — recovery mode |

### Trade Limits — RECOVERY MODE
| Parameter | Value | Notes |
|-----------|-------|-------|
| `MAX_DAILY_FORECAST_TRADES` | 5/day | Was 8 — fewer, higher-conviction trades |
| `DAILY_LOSS_LIMIT_CENTS` | $6.00 | Was $20 — 15% of $40 bankroll, not 50% |
| `CONSECUTIVE_LOSS_PAUSE` | 3 losses | Was 5 — stop sooner |
| `CONSECUTIVE_LOSS_PAUSE_MINUTES` | 60 min | Was 30 — longer pause |
| `TRADE_COOLDOWN` | 120 sec | Was 180 — matches scan interval. Same-cycle signals exempt (different cities in one scan pass) |
| `RESTING_ORDER_TIMEOUT` | 25 min | Auto-cancel unfilled buy orders (was 15 min) |
| `RESTING_EXIT_TIMEOUT` | 30 min | Auto-cancel unfilled exit/hedge orders |

### Confirmed Outcome Rules (CASE 1, 2 & 3)
| Parameter | Value | Notes |
|-----------|-------|-------|
| `CASE1_MIN_LOCAL_HOUR` | 10 AM local | CASE 1 & 3 earliest detection — observation-based, rounding buffer guards (was 11 AM) |
| `CASE2_ENABLED` | **False** | CASE 2 disabled in recovery mode |
| `CASE2_MIN_LOCAL_HOUR` | 6 PM local | When re-enabled (was 4 PM — too early for reliable observations) |
| `CASE2_NARROW_MIN_LOCAL_HOUR` | 5 PM local | Stricter for narrow buckets (≤5°F) |
| `CASE2_NARROW_BUCKET_WIDTH` | 5°F | Definition of "narrow" bucket |
| CASE 1 rounding buffer | +1°F | `todays_high > temp_high + 1°F` before confirming |
| CASE 3 rounding buffer | -1°F | Gap reduced by 1°F (real temp could be higher) |
| CASE 3 cooling gate | Before 2 PM local | **MANDATORY** — CASE 3 before `CASE3_COOLING_REQUIRED_BEFORE_HOUR` (14) requires evidence of cooling: latest temp must be ≥3°F below today's high (cold front moved through, peak already set). Without cooling evidence, the temperature is still rising and the gap is meaningless. Uses `get_temperature_trend()`. |
| CASE 3 graduated gaps | 10AM:15°F, 11AM:12°F, 12PM:8°F, 1PM:7°F, 2PM:6°F, 3PM:4°F, 4PM+:2°F | Config-driven (`CASE3_GAP_THRESHOLDS`). Only checked AFTER cooling gate passes (before 2 PM) or unconditionally (2 PM+) |
| CASE 2 ensemble veto | mean > ceiling+2°F | **MANDATORY** — veto CASE 2 if ensemble predicts temp above bucket. If ensemble unavailable, CASE 2 blocked |
| CASE 3 ensemble veto | 3PM+:3°F, earlier:5°F | **MANDATORY** — ensemble mean within N°F of bucket floor = veto CASE 3. If ensemble unavailable, CASE 3 blocked |

### Settlement & Timing
| Parameter | Value | Notes |
|-----------|-------|-------|
| `SETTLEMENT_HOUR_ET` | 10 AM ET | When Kalshi processes settlements |
| `SETTLEMENT_PROXIMITY_HOURS` | 2 hrs | No new positions within 2hrs of close |
| `SETTLEMENT_PROXIMITY_EDGE_OVERRIDE` | 20% | Exceptional edge overrides proximity block |
| `LIQUIDITY_RESERVE_PCT` | 20% | Reserve cash (was 40% — confirmed outcomes now bypass caps directly) |
| `PRE_SETTLEMENT_SIZING_MULT` | 0.75x | 75% sizing before settlements clear (was 60%) |

### Account Tracking
| Parameter | Value | Notes |
|-----------|-------|-------|
| `TOTAL_DEPOSITS_CENTS` | $100.00 | Total deposited — for account P&L display |

### Kill Switch / Circuit Breakers
| Parameter | Value | Notes |
|-----------|-------|-------|
| `KILL_SWITCH_CONSECUTIVE_LOSSES` | 2 | Enters observation mode (was 3) |
| `KILL_SWITCH_MIN_SHARPE_7D` | 0.0 | 7-day Sharpe below this = observation mode |

### Portfolio Review (Intraday Exit Logic)

**Thesis-first design:** Exit decisions are based on whether the model/observations still predict the position wins at settlement. Edge erosion on winning positions = market caught up = hold for settlement.

| Priority | Check | Action | Urgency |
|----------|-------|--------|---------|
| 1 | Observation confirms loss (obs_high in bucket for NO, temp gap for YES) | EXIT | High |
| 2 | Rounding buffer (obs_high within 1°F of bucket, after 2 PM) | EXIT | High |
| 1b | Approaching bucket (NO): obs_high within 3°F of floor after 1 PM, 2°F after 2 PM | EXIT | High |
| 3 | NWS confirmer REJECT on re-check | Thesis override if model says >65% win prob (any time). Otherwise EXIT. Prevents false REJECT from exiting winning NO positions where confirmer direction flips. | High (no thesis) / Low (thesis override) |
| 4 | Ensemble probability floor (<15% YES, >85% NO) | EXIT | High |
| 5 | **Thesis valid** (model says >50% chance of winning) | **HOLD** | Low |
| 6 | Thesis weakening (edge decayed but still positive) | PARE | Medium |
| 7 | Thesis uncertain (edge near zero) + profitable | Take profit | Medium |
| 8 | Thesis broken (edge reversed hard, model flipped) | EXIT | High |

| Parameter | Value | Notes |
|-----------|-------|-------|
| `EDGE_DECAY_PARE_THRESHOLD` | 50% | Pare when edge drops below 50% of entry (only if thesis weak) |
| `EDGE_REVERSAL_THRESHOLD` | -5% | Thesis broken threshold |
| `TAKE_PROFIT_PCT` | 30% | Only triggers when thesis is uncertain (model ~50/50) |
| Rounding buffer exit | After 2 PM | NO positions exit if obs_high within 1°F of bucket floor |

### Resting Order Management (in `kalshi_bot.py`)
- **Order fill verification**: Kalshi API response `order.status` is checked — only `"executed"` or `remaining_count == 0` counts as filled. All other orders are tracked as "resting".
- **Pending order tracking in risk manager**: On placement, resting orders register cost/contracts/city via `risk.add_pending_order()`. On fill or cancel, `risk.clear_pending_order()` releases the reservation. All exposure cap checks (total, per-city, per-ticker, correlated) include pending order cost.
- **Resting exit/hedge/pare orders**: Do NOT release exposure or record P&L. Tagged with `pending_exit_order_id` etc. for tracking.
- **Auto-cancel**: Buy orders cancelled after 25 min (was 15 min). Exit/hedge/pare orders cancelled after 30 min.
- **`_check_resting_orders()`**: Runs each cycle (after reconciliation). Compares tracked order_ids against `client.get_orders(status="resting")`. Updates trade log when orders fill or are cancelled.

### Strategy Guards (in `strategy.py`)
- **Fee-adjusted edge**: `net_edge = raw_edge - fee_drag` must be ≥4% after Kalshi's 7% profit fee (was 5%)
- **Rounding buffer**: Forecast mean within ±1°F of any bucket strike = skip. Within ±2°F = 50% size.
- **Model divergence**: Ensemble spread >4°F = skip. Spread <2°F = 1.2x boost.
- **Longshot floor**: No contracts below 5¢ (market_quality.py) — was 12¢, lowered to capture confirmed outcomes
- **Near-certainty cap**: No contracts above 88¢
- **Minimum payout**: $2.00 total payout floor (was $1.50 — filter dust trades)
- **Narrow bucket guard**: Extra caution on ≤5°F buckets
- **NO-side price ceiling**: NO positions priced above 60c (`NO_SIDE_MAX_PRICE_CENTS`, was 70c) are hard-rejected — applies to ALL paths including CASE 1 and CASE 3 confirmed outcomes. DEN B52.5 at 68c lost $5.44 — ceiling lowered to prevent.
- **NO-side sizing reduction**: NO contracts priced ≥50c get 40% of normal sizing (`NO_SIDE_SIZING_MULTIPLIER`, was 70%). Caps max NO loss to ~$2.80 per position.
- **Per-city per-model bias correction**: Replaces flat `WINTER_WARM_CITY_BIAS_F`. Each model family (GFS/ECMWF/ICON/GEM) gets its own bias correction per city. Learned bias from `quant_analytics.get_model_bias()` takes priority (requires 5+ datapoints). Falls back to per-city winter defaults: DEN +4°F (chinook), Gulf cities +3°F, desert +2°F. Config: `WINTER_WARM_CITY_BIAS` dict, `MODEL_BIAS_MIN_DATAPOINTS=5`.
- **Next-day market guard**: Evening trades for tomorrow's markets (12-18h uncertainty) require 1.5x edge threshold (`NEXT_DAY_EDGE_MULTIPLIER`) and get 50% sizing (`NEXT_DAY_SIZING_MULTIPLIER`). Confirmed outcomes and arbitrage exempt.
- **Time-of-day edge thresholds**: Morning 12% (was 10%), overnight 12% (was 9%), afternoon/evening 10% base. Recovery mode: save capital for afternoon confirmed outcomes. Confirmed outcomes and arbitrage bypass.
- **Same-cycle cooldown exemption**: Signals from the same scan pass (different cities) skip the 120s cooldown. All other risk checks still apply.
- **6-hour same-ticker re-entry cooldown**: Prevents re-entering a ticker that was traded within the last 6 hours (`SAME_TICKER_REENTRY_HOURS`). Confirmed outcomes (CASE 1/3) exempt.
- **Bias streak detection**: 3+ consecutive days of same-direction bias (≥0.5°F each) triggers immediate adjustment without waiting for 5-datapoint minimum

### Trade Scorecard (v4.0, in `trade_scorecard.py`)
- **Recursive evaluation**: Max 3 iterations of diagnose→fix→retry. Actions: execute/reject/defer.
- **Bypass**: Confirmed outcomes (`CONFIRMED_OUTCOME`) and arbitrage (`S2-Arbitrage`) skip the scorecard entirely.
- **8 criteria** (all must pass):

| Criterion | Threshold | Fixable | Description |
|-----------|-----------|---------|-------------|
| `data_integrity` | pass/fail | No | NWS station correct for settlement? |
| `forecast_convergence` | 50% | No | Ensemble members agree within 2°F? (was 60%, redundant with 4°F model divergence gate) |
| `edge_magnitude` | 4% net | Yes | Edge survives fees + uncertainty? (was 5%) |
| `timing_window` | 0.5x mult | Yes | Favorable entry timing (hours to settlement)? |
| `liquidity` | Bypassed (weather maker) / 3 (other) | Yes | Weather maker orders bypass liquidity check (empty book = ideal for limit orders). Non-weather requires 3+ contracts. |
| `portfolio_correlation` | 40% | Yes | Not over-concentrated in correlated markets? |
| `position_sizing` | 20% bankroll | Yes | Within per-position and total exposure caps? |
| `adversarial_check` | <2 warnings | Yes | Passes devil's advocate stress test? |

- **Fix engine**: Fixable failures trigger automatic adjustments (reduce_size, wait, retarget, adjust_price). Unfixable failures → immediate reject.
- **Timing multipliers** (default, before Becker calibration): >72h: 0.7x, 48-72h: 0.9x, 24-48h: 1.2x, 12-24h: 1.0x, 6-12h: 0.8x, <6h: 0.5x
- **Config**: `SCORECARD_ENABLED`, `SCORECARD_MAX_ITERATIONS`, `MIN_FORECAST_CONVERGENCE`, `MIN_TIMING_MULTIPLIER`, `MIN_LIQUIDITY_CONTRACTS`, `MAX_CORRELATION_EXPOSURE`

### Maker Execution Strategy (v4.0, in `maker_strategy.py`)
- **Spread buffer**: Posts limit orders at `fair_value - MAKER_SPREAD_BUFFER_CENTS` (default 2¢ below)
- **Dynamic buffer**: High edge (>15%) → tighter buffer (1¢). Low edge (<8%) → wider buffer (3¢).
- **Order lifecycle**: place → monitor fills → cancel stale (30min) → track adverse selection
- **Bypass**: Confirmed outcomes and arbitrage skip maker pricing (need fast fills)
- **Adverse selection**: 3+ fills in <10min window → caution (reduce sizes 50%)
- **Config**: `MAKER_STRATEGY_ENABLED`, `MAKER_SPREAD_BUFFER_CENTS`, `STALE_ORDER_MINUTES`, `MAX_OPEN_ORDERS`, `ADVERSE_SELECTION_PAUSE_MINUTES`
- **State file**: `maker_orders.json` in STATE_DIR

### Model Accuracy Weighting
- **Flow**: On each settlement, `_update_model_accuracy_from_settlement()` fetches what each deterministic model (GFS, ECMWF, ICON, GEM) predicted and records the error in `model_accuracy.json` via `quant_analytics.record_model_accuracy()`. Now tracks both squared error (`mse_sum`) and signed error (`error_sum`). Prints a summary: `[MODEL ACCURACY] DC: ecmwf_ifs predicted 51°F (err: -3°F), gfs_ensemble predicted 49°F (err: -5°F)`.
- **Weight calculation**: `quant_analytics.get_model_weights(city_code)` returns inverse-RMSE weights per model per city per season. Requires 5+ datapoints before activating (was 10, `MODEL_BIAS_MIN_DATAPOINTS`). Falls back to all 1.0.
- **Bias calculation**: `quant_analytics.get_model_bias(city_code)` returns mean signed error per model. Negative = underpredicts. Used by `weather_engine` to apply per-model correction (shift = -bias). Requires 5+ datapoints. Falls back to per-city winter defaults from `WINTER_WARM_CITY_BIAS` dict.
- **Weight application**: `strategy.py` fetches weights and biases early in `_strategy_weather()` and passes them to `weather_engine.get_temperature_distribution()`. The engine applies per-model bias correction first, then accuracy weights in `_build_distribution()`: weighted mean, weighted bucket probability, weighted variance.
- **Trade logging**: Each trade entry in `trade_history.json` records `model_predictions` (per-model means), `model_weights_used` (accuracy weights applied), and `model_biases_used` (bias corrections applied).

## Deferred Features (NOT YET IMPLEMENTED)
- **wethr.net API**: Needs Pro API key. Would be 6th confirmation source.
- ~~**DST timing logic**~~: DONE — Replaced hardcoded UTC offsets with `zoneinfo.ZoneInfo` in strategy.py, trade_intelligence.py, and risk_manager.py.
- **Dead bracket scalping**: Fee drag too high on 1-3¢ contracts (floor is 5¢).
- **Certainty bias exploitation**: Needs multi-contract position management.
