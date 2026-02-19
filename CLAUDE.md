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

Multi-market prediction market trading bot for Kalshi. It runs a continuous loop (every 5 minutes) that scans enabled market types, detects mispricing, and places limit orders.

**Market types** are toggleable via env vars in `config.py` (`MARKET_TYPES` dict). Weather is ON by default; S&P 500 is OFF by default (`ENABLE_SP500=true` to activate). New market types should follow this pattern.

### Decision Flow

```
market_scanner.py  →  strategy.py  →  confirmer  →  risk_manager.py  →  kalshi_client.py
   (discover)         (evaluate)     (validate)      (safety check)      (execute)
```

Weather uses `signal_confirmer.py` (5-source voting: 4 deterministic models + NWS station point forecast). S&P 500 uses `spx_confirmer.py` (momentum/vol/historical).

**Edge-priority execution:** Both weather and S&P 500 scan loops use a two-phase design. Phase 1 evaluates all markets and collects actionable signals. Phase 2 sorts signals by edge descending, then processes through risk checks and execution. This ensures the highest-edge trade gets first shot at per-city and per-position caps.

### Module Responsibilities

- **`kalshi_bot.py`** — Entry point and main loop. Orchestrates scan→analyze→trade cycle per market type. Two-phase scan: evaluate all markets first, then sort by edge and execute highest-edge trades first. Handles exits and settlement tracking.
- **`kalshi_client.py`** — Kalshi API wrapper. RSA-PSS signature auth, dual environment support (demo vs production URLs), market fetching, order placement, position queries.
- **`config.py`** — All tunable parameters: credentials, risk limits, strategy thresholds, scan intervals, market type toggles. Values are in cents (100 cents = $1.00).
- **`market_scanner.py`** — Queries Kalshi for weather series + S&P 500 brackets. `scan_all_enabled_markets()` respects `MARKET_TYPES` toggles.
- **`strategy.py`** — Three strategies: **S1 (Weather Edge)** ensemble vs market prices; **S2 (Spread Arbitrage)** YES+NO < $0.98; **S3 (SP500 Brackets)** VIX-implied distributions vs bracket prices. S3 components only initialized when sp500 is enabled.
- **`weather_engine.py`** — Aggregates 143 ensemble members from 4 sources (GFS 31, ECMWF 51, ICON-EPS 40, GEM 21) via Open-Meteo. Builds probability distributions across temperature buckets.
- **`volatility_engine.py`** — VIX-based price distributions for S&P 500. Uses yfinance for SPY/VIX data, scipy.stats.norm CDF for bracket probability calculations. Includes intraday vol adjustment.
- **`signal_confirmer.py`** — Weather voting system from 5 independent sources: 4 deterministic models (GFS/HRRR, ECMWF, ICON, GEM) via Open-Meteo + NWS station point forecast (api.weather.gov). Outputs: STRONG (3+ agree AND NWS agrees, 1.5x), CONFIRM (2+, or 3+ with NWS abstain/disagree, 1.0x), WEAK (0.5x), REJECT. NWS source uses 2°F abstain zone on both underpriced and overpriced paths. **STRONG requires explicit NWS AGREE** — if NWS abstains, disagrees, or API fails, verdict is capped at CONFIRM.
- **`spx_confirmer.py`** — S&P 500 signal confirmation: intraday momentum, realized vs implied vol ratio, historical bracket hit rate. Same verdict/multiplier interface as weather confirmer.
- **`risk_manager.py`** — 19 safety layers (see Risk Parameters section below). Key checks: daily loss limit, dynamic exposure cap (60% bankroll), per-city cap (30% bankroll), per-ticker cap ($15), daily forecast trade cap (4/day, confirmed outcomes exempt), correlated position cap, loss streak pause, cooldown, settlement proximity, liquidity reserve, kill switch (Sharpe + consecutive loss). State persisted in `risk_state.json`.
- **`trade_intelligence.py`** — Position exit logic (including rounding-buffer exits for NO positions), forecast bias learning per NWS station (with 3-day streak detection), time-of-day sizing adjustments, intraday observation tracking, settlement P&L recording with `settled_at` timestamps.
- **`quant_analytics.py`** — Backtesting against 90 days of historical data, per-model accuracy weighting, regime detection (stable vs volatile), smart order placement, correlation-aware position sizing.
- **`market_quality.py`** — Liquidity filter (max 15c spread, min 1 contract volume), probability guardrails (rejects <12c longshots and >88c near-certainties).
- **`dashboard.py`** — Flask-based web dashboard with health monitoring, email alerts, position/trade display with market type badges.

### Data Files (Runtime State)

All state is persisted as JSON in `STATE_DIR` (defaults to `.`, set to `/data` on Railway for volume persistence): `trade_history.json`, `risk_state.json`, `pnl_history.json`, `backtest_results.json`, `edge_attribution.json`, `bot_status.json`, `pending_trades.json`, `scan_log.json` (per-market evaluation log, 7-day rolling retention, exposed via `/api/state`).

### Key Design Decisions

- **Quarter-Kelly sizing** — Position sizes use Kelly criterion divided by 4 for safety, multiplied by the signal confirmation level.
- **Maker strategy** — Prefers limit orders (not market orders) for better fills.
- **NWS settlement** — Weather markets settle on NWS Daily Climate Reports from specific stations, not model forecasts. See NWS Station Mappings below.
- **NWS rounding awareness** — NWS 5-minute stations have ±1°F+ error from DOS-era °F→°C→°F conversion. Settlement uses raw 1-minute readings (can be higher than displayed time series). Bot applies rounding buffer: ±1°F of strike = no trade, ±2°F = 50% size reduction.
- **Fee-adjusted edge** — Raw edge is reduced by estimated Kalshi fee drag (7% of profit) before checking minimum edge threshold. Net edge must survive at 5%+ after fees.
- **Longshot avoidance** — Never buys contracts priced below 12c, per academic research on prediction market biases.
- **Market type isolation** — Each market type is gated behind a toggle. New market code paths only execute when explicitly enabled. SP500 uses `city_code="SP500"` in the risk manager for per-market exposure tracking.

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

## Risk Parameters (Current Values)

**IMPORTANT: Any changes to risk parameters or strategy rules MUST be reflected here. This is the source of truth for risk documentation.**

All values are set in `config.py`. Values in cents (100 cents = $1.00).

### Position Sizing
| Parameter | Value | Notes |
|-----------|-------|-------|
| `MAX_POSITION_PCT` | 20% of bankroll | Max single position (caps contracts down instead of rejecting) |
| `CONFIRMED_OUTCOME_POSITION_PCT` | 25% of bankroll | CASE 1 confirmed (NO on exceeded buckets) |
| `CASE2_POSITION_PCT` | 10% of bankroll | CASE 2 confirmed (YES on current bucket, riskier) |
| Quarter-Kelly | Kelly/4 | Base sizing formula, multiplied by confirmation level |

### Exposure Caps (Dynamic, Percentage-Based)
| Parameter | Value | Notes |
|-----------|-------|-------|
| `MAX_TOTAL_EXPOSURE_PCT` | 60% of bankroll | Was 80%, tightened |
| `MAX_TOTAL_EXPOSURE_CENTS` | $60.00 | Fallback if balance unknown |
| `MAX_PER_CITY_PCT` | 30% of bankroll | Per-city exposure limit |
| `MAX_PER_CITY_CENTS` | $30.00 | Fallback if balance unknown |
| `MAX_DAILY_CITY_SPEND_CENTS` | $35.00 | Cumulative daily per city (prevents sell-and-rebuy) |
| `MAX_PER_TICKER_CENTS` | $15.00 | Per-ticker concentration limit |
| `MAX_OPEN_POSITIONS` | 20 | Total open positions |
| `MAX_CORRELATED_POSITIONS` | 3 | Same city + same date |

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
| `FEE_ADJUSTED_MIN_EDGE` | 5% | Min net edge after fee drag |
| `MIN_EDGE` | 8% | Raw minimum edge before fee adjustment |

### Trade Limits
| Parameter | Value | Notes |
|-----------|-------|-------|
| `MAX_DAILY_FORECAST_TRADES` | 4/day | Confirmed outcomes + arbitrage exempt |
| `DAILY_LOSS_LIMIT_CENTS` | $20.00 | Stop trading if daily losses hit this |
| `CONSECUTIVE_LOSS_PAUSE` | 5 losses | Pause trading after 5 consecutive losses |
| `CONSECUTIVE_LOSS_PAUSE_MINUTES` | 30 min | Duration of loss pause |
| `TRADE_COOLDOWN` | 180 sec | Minimum time between trades |
| `RESTING_ORDER_TIMEOUT` | 15 min | Auto-cancel unfilled buy orders |
| `RESTING_EXIT_TIMEOUT` | 30 min | Auto-cancel unfilled exit/hedge orders |

### Confirmed Outcome Rules (CASE 1 & 2)
| Parameter | Value | Notes |
|-----------|-------|-------|
| `CASE2_MIN_LOCAL_HOUR` | 4 PM local | CASE 2 time gate (YES on current bucket) |
| `CASE2_NARROW_MIN_LOCAL_HOUR` | 5 PM local | Stricter for narrow buckets (≤5°F) |
| `CASE2_NARROW_BUCKET_WIDTH` | 5°F | Definition of "narrow" bucket |
| CASE 1 rounding buffer | +1°F | `todays_high > temp_high + 1°F` before confirming |
| CASE 3 rounding buffer | -1°F | Gap reduced by 1°F (real temp could be higher) |

### Settlement & Timing
| Parameter | Value | Notes |
|-----------|-------|-------|
| `SETTLEMENT_HOUR_ET` | 10 AM ET | When Kalshi processes settlements |
| `SETTLEMENT_PROXIMITY_HOURS` | 2 hrs | No new positions within 2hrs of close |
| `SETTLEMENT_PROXIMITY_EDGE_OVERRIDE` | 20% | Exceptional edge overrides proximity block |
| `LIQUIDITY_RESERVE_PCT` | 50% | Reserve cash for confirmed outcome arb trades |
| `PRE_SETTLEMENT_SIZING_MULT` | 0.6x | 60% sizing before settlements clear |

### Account Tracking
| Parameter | Value | Notes |
|-----------|-------|-------|
| `TOTAL_DEPOSITS_CENTS` | $100.00 | Total deposited — for account P&L display |

### Kill Switch / Circuit Breakers
| Parameter | Value | Notes |
|-----------|-------|-------|
| `KILL_SWITCH_CONSECUTIVE_LOSSES` | 3 | Enters observation mode |
| `KILL_SWITCH_MIN_SHARPE_7D` | 0.0 | 7-day Sharpe below this = observation mode |

### Portfolio Review (Intraday Exit Logic)
| Parameter | Value | Notes |
|-----------|-------|-------|
| `EDGE_DECAY_PARE_THRESHOLD` | 50% | Pare when edge drops below 50% of entry |
| `EDGE_REVERSAL_THRESHOLD` | -5% | Full exit when edge flips negative |
| `TAKE_PROFIT_PCT` | 30% | Take profit on 30%+ unrealized gain |
| Rounding buffer exit | After 2 PM | NO positions exit if obs_high within 1°F of bucket floor |

### Resting Order Management (in `kalshi_bot.py`)
- **Order fill verification**: Kalshi API response `order.status` is checked — only `"executed"` or `remaining_count == 0` counts as filled. All other orders are tracked as "resting".
- **Resting buy orders**: NOT recorded as positions in risk manager. `_reconcile_positions()` picks them up when they fill on Kalshi's side.
- **Resting exit/hedge/pare orders**: Do NOT release exposure or record P&L. Tagged with `pending_exit_order_id` etc. for tracking.
- **Auto-cancel**: Buy orders cancelled after 15 min. Exit/hedge/pare orders cancelled after 30 min.
- **`_check_resting_orders()`**: Runs each cycle (after reconciliation). Compares tracked order_ids against `client.get_orders(status="resting")`. Updates trade log when orders fill or are cancelled.

### Strategy Guards (in `strategy.py`)
- **Fee-adjusted edge**: `net_edge = raw_edge - fee_drag` must be ≥5% after Kalshi's 7% profit fee
- **Rounding buffer**: Forecast mean within ±1°F of any bucket strike = skip. Within ±2°F = 50% size.
- **Model divergence**: Ensemble spread >4°F = skip. Spread <2°F = 1.2x boost.
- **Longshot floor**: No contracts below 12¢ (market_quality.py)
- **Near-certainty cap**: No contracts above 88¢
- **Narrow bucket guard**: Extra caution on ≤5°F buckets
- **Bias streak detection**: 3+ consecutive days of same-direction bias (≥0.5°F each) triggers immediate adjustment without waiting for 5-datapoint minimum

## Deferred Features (NOT YET IMPLEMENTED)
- **wethr.net API**: Needs Pro API key. Would be 6th confirmation source.
- **DST timing logic**: Hardcoded UTC offsets will be wrong after March 9 DST. Fix: use Python `zoneinfo`.
- **Dead bracket scalping**: Contradicts 12¢ longshot floor. Fee drag too high on 1-3¢ contracts.
- **Certainty bias exploitation**: Needs multi-contract position management.
