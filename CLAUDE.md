# CLAUDE.md

## Workflow Orchestration

- **Plan mode** for ANY non-trivial task (3+ steps). STOP and re-plan if something goes sideways.
- **Subagents** liberally to keep main context clean. One task per subagent.
- **Self-improvement loop**: After ANY correction, update `tasks/lessons.md`.
- **Verify before done**: Prove it works. Run tests, check logs, demonstrate correctness.
- **Simplicity first**: Minimal changes. Find root causes. Senior developer standards.
- **Task management**: Plan in `tasks/todo.md`, track progress, capture lessons in `tasks/lessons.md`.

## Git & Deployment

- **Working branch:** `master` — **Production branch:** `main` (Railway auto-deploys)
- **Deploy:** `git checkout main && git merge master && git push && git checkout master`
- "Push to Railway" or "deploy" = merge `master` → `main` and push. Never force-push `main`.

## Running the Bot

`python kalshi_bot.py` — No build step, no tests, no linter. Config in `config.py`.

**Levels:** `DRY_RUN=True` (analysis only) → `ENVIRONMENT="demo"` (practice) → `ENVIRONMENT="production"` (real money)

## Dependencies

Core: `requests`, `cryptography`, `numpy`, `scipy`, `pandas`, `yfinance` (`requirements.txt`).
APIs (free, no keys): Open-Meteo (ensemble forecasts), NWS (settlement source), yfinance (SPY/VIX).
Kalshi API: RSA key-pair auth via `API_KEY_ID` + `PRIVATE_KEY_PATH` in `config.py`.

## Architecture

Multi-market prediction bot for Kalshi. Continuous loop (2min, 1min peak 12-5 PM ET). Scans weather markets, detects mispricing, places limit orders.

```
market_scanner.py → strategy.py → confirmer → trade_scorecard.py → risk_manager.py → maker_strategy.py → kalshi_client.py
   (discover)       (evaluate)    (validate)   (8-criteria gate)    (safety check)    (maker pricing)     (execute)
```

**Edge-priority execution:** Two-phase scan — evaluate all markets, sort by edge descending, execute highest first.

### Modules

| Module | Role |
|--------|------|
| `kalshi_bot.py` | Entry point, main loop, exit/settlement tracking. Dashboard starts before restart loop. |
| `kalshi_client.py` | Kalshi API wrapper, RSA-PSS auth, dual environment |
| `config.py` | All parameters. Values in cents. |
| `market_scanner.py` | Queries Kalshi for weather series + S&P brackets |
| `strategy.py` | S1 Weather Edge, S2 Spread Arbitrage, S3 SP500 Brackets |
| `weather_engine.py` | 143 ensemble members (GFS 31, ECMWF 51, ICON 40, GEM 21) via Open-Meteo |
| `signal_confirmer.py` | 5-source voting. STRONG (3+ agree + NWS agrees), CONFIRM (2+, NWS abstains), REJECT. WEAK = hard reject. NWS DISAGREE = hard REJECT. |
| `risk_manager.py` | 19 safety layers. State in `risk_state.json`. |
| `trade_intelligence.py` | Exit logic, bias learning, observation tracking, settlement P&L |
| `quant_analytics.py` | Backtesting, per-model accuracy weights + bias, regime detection |
| `trade_scorecard.py` | 8-criteria recursive eval (max 3 iterations). Confirmed outcomes + arb bypass. |
| `maker_strategy.py` | Limit orders at fair_value - spread_buffer. State in `maker_orders.json`. |
| `dashboard.py` | Web dashboard (BaseHTTPRequestHandler). `/api/health`, `/api/state`, `/api/force-exit` |
| `self_improver.py` | Weekly param tuning (Sun 11 PM ET). Overrides in `config_overrides.json` (7-day expiry). |
| `seasonal_confidence.py` | Monthly sizing multipliers per city |
| `market_quality.py` | Liquidity filter, probability guardrails |
| `trade_analyzer.py` | End-of-day post-mortem |

### State Files (JSON in STATE_DIR, `/data` on Railway)

`trade_history.json`, `risk_state.json`, `pnl_history.json`, `maker_orders.json`, `scan_log.json`, `model_accuracy.json`, `forecast_log.json`, `config_overrides.json`, `bot_status.json`, `backtest_results.json`, `edge_attribution.json`, `pending_trades.json`, `daily_reports.json`, `trade_analysis.json`, `bias_history.json`, `seasonal_weights.json`, `improvement_log.json`

### Key Design Decisions

- **Quarter-Kelly sizing** (Kelly/4 × confirmation level)
- **Maker strategy** — limit orders, not market orders
- **NWS settlement** — Weather markets settle on NWS Daily Climate Reports, not model forecasts
- **NWS rounding** — ±1°F DOS-era conversion error. Buffer: ±1°F = no trade, ±2°F = 50% size. **NO-side expands bucket by ±1°F before separation check.**
- **Fee-adjusted edge** — 7% fee drag deducted before min edge check. Side-aware formula.
- **Only CASE 1 = CONFIRMED_OUTCOME** — temp can't un-happen. CASE 2/3 = STRONG (normal risk checks).
- **CASE 2 disabled** (recovery mode). Returns STRONG when re-enabled.
- **Thesis-based exits** — exit on loss confirmation/thesis broken, NOT edge erosion. Hold winners to settlement.
- **Only high-urgency exits auto-execute.** Medium urgency = manual review.
- **Single-writer P&L** — Only `_sync_pnl_from_kalshi()` writes `pnl_history.json`. No exceptions.
- **Duplicate order detection** — checks `risk.state["positions"]` + `trade_log` resting orders. Uses `order_status` field (not `status`).

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

## Risk Parameters — RECOVERY MODE

**IMPORTANT: Any changes to risk parameters MUST be reflected here.**

**RECOVERY MODE (Feb 2026):** Bankroll ~$48. Tightened params. Exit when bankroll > $80.

All values in `config.py`. Cents = 100 per $1.00.

### Core Limits
| Parameter | Value | Notes |
|-----------|-------|-------|
| `MIN_EDGE` | 7% | Lowered from 10% for high-freq small-edge strategy. Morning: 9%, overnight: 10%. |
| `FEE_ADJUSTED_MIN_EDGE` | 3% | After 7% fee drag. Lowered from 5% to enable more volume. |
| `MIN_PAYOUT_DOLLARS` | $1.00 | Was $2.00 — lowered for small bankroll |
| `DAILY_LOSS_LIMIT_CENTS` | $6.00 | 15% of bankroll |
| `MAX_DAILY_FORECAST_TRADES` | 5/day | Confirmed outcomes exempt |
| `CONSECUTIVE_LOSS_PAUSE` | 3 losses / 60 min | |
| `KILL_SWITCH_CONSECUTIVE_LOSSES` | 2 | Enters observation mode |

### Position Sizing & Exposure
| Parameter | Value | Notes |
|-----------|-------|-------|
| `MAX_POSITION_PCT` | 20% bankroll | Caps contracts down |
| `CONFIRMED_OUTCOME_POSITION_PCT` | 25% bankroll | CASE 1 only |
| `MAX_TOTAL_EXPOSURE_PCT` | 60% bankroll | Confirmed outcomes bypass |
| `MAX_PER_CITY_PCT` | 15% bankroll | Confirmed outcomes bypass |
| `MAX_PER_TICKER_CENTS` | $8.00 | Enforced always |
| `MAX_CONTRACTS_PER_TICKER` | 15 | Enforced always |
| `MAX_OPEN_POSITIONS` | 6 | Confirmed outcomes bypass |
| `MAX_CORRELATED_POSITIONS` | 2 (3 for confirmed) | Separate caps for normal vs CASE 1 |
| `MAX_DAILY_CITY_SPEND_CENTS` | $12.00 | Cumulative daily per-city (distinct from instantaneous MAX_PER_CITY_PCT). Confirmed outcomes bypass. |
| `LIQUIDITY_RESERVE_PCT` | 20% | |
| `TRADE_COOLDOWN` | 120 sec | Matches 2min scan interval. Same-cycle exempt. |
| `SETTLEMENT_PROXIMITY_HOURS` | 2 hrs | No new positions. 20% edge overrides. |
| Quarter-Kelly | Kelly/4 × confirmation | |

### Strategy Guards
- **Rounding buffer**: ±1°F = no trade, ±2°F = 50% size. **NO-side uses rounding-expanded boundaries** (bucket ± 1°F) for separation check — needs ≥3°F from expanded boundary.
- **Model divergence**: >4°F spread = skip. <2°F = 1.2x boost.
- **Longshot floor**: 5¢. **Near-certainty cap**: 88¢.
- **NO ceiling**: 60¢ (`NO_SIDE_MAX_PRICE_CENTS`). **NO sizing**: ≥50¢ gets 40% normal.
- **Per-city per-model bias correction**: Learned from `quant_analytics.get_model_bias()` (5+ dp). Defaults: DEN +4°F, Gulf +3°F, desert +2°F.
- **Next-day guard**: 1.5x edge threshold, 50% sizing. Confirmed outcomes exempt.
- **Same-cycle cooldown exempt**. 6-hour same-ticker re-entry cooldown (CASE 1 exempt only — CASE 3 returns STRONG, not CONFIRMED_OUTCOME).
- **Bias streak**: 3+ days same direction → immediate adjustment.
- **Convergence confidence boost**: Afternoon same-day: when sources agree, models tight, obs confirm forecast, boost `our_prob` by 6-12% to create synthetic edge. Score ≥0.75 required from 4 components: source agreement (30%), model convergence (25%), ensemble tightness (25%), obs confirmation (20%). Reduced sizing (65% Kelly). Two-pass: preliminary 3-component check avoids unnecessary API calls. Only activates when raw edge < MIN_EDGE (no double-dip). Config: `CONVERGENCE_BOOST_ENABLED`, `_MIN_SCORE=0.75`, `_MIN_BOOST_PCT=6%`, `_MAX_BOOST_PCT=12%`, `_SIZING_MULTIPLIER=0.65`.

### Confirmed Outcome Rules
- **CASE 1** (high exceeded bucket): `CONFIRMED_OUTCOME`. Min 10 AM local. Rounding: `todays_high > temp_high + 1°F` (obs must exceed strike by 1°F to confirm).
- **CASE 2** (YES on current bucket): **DISABLED** (`CASE2_ENABLED=False`). Returns STRONG when re-enabled. Ensemble veto: `mean > ceiling+2°F` = block. **No ensemble = blocked.**
- **CASE 3** (gap too large for bucket): Returns STRONG. Gap reduced by 1°F (real temp could be higher than NWS display).
  - **Cooling gate (MANDATORY before 2 PM):** `latest_temp <= todays_high - 3°F` (evidence peak has passed). Uses `get_temperature_trend()`. Without cooling evidence, CASE 3 blocked.
  - **Graduated gaps:** 10AM:15°F, 11AM:12°F, 12PM:8°F, 1PM:7°F, 2PM:6°F, 3PM:4°F, 4PM+:2°F (`CASE3_GAP_THRESHOLDS`)
  - **Ensemble veto (MANDATORY):** 3PM+: mean within 3°F of bucket floor = veto. Earlier: 5°F. **No ensemble = blocked.**
- **Confirmed bypass (CASE 1 only)**: Bypasses total/city/daily-city-spend/position caps. Still respects: daily loss, per-ticker, contracts, correlated (cap=3), cooldown.
- **SP500 convention**: Uses `city_code="SP500"` in risk manager for per-market exposure tracking. New market types must follow this pattern.

### Exit Logic (Thesis-First)
| Priority | Trigger | Action |
|----------|---------|--------|
| 1 | Obs confirms loss / approaching bucket | EXIT (high urgency) |
| 2 | Rounding buffer (±1°F after 2 PM) | EXIT |
| 3 | NWS REJECT | EXIT unless model says >65% win (thesis override) |
| 4 | Ensemble floor (<15% YES / >85% NO) | EXIT |
| 5 | Thesis valid (>50% win) | HOLD |
| 6-8 | Weakening/uncertain/broken | PARE / Take profit / EXIT |

### Scorecard (8 criteria, all must pass)
`data_integrity`, `forecast_convergence` (50%), `edge_magnitude` (4%), `timing_window` (0.5x), `liquidity` (bypassed for weather maker), `portfolio_correlation` (40%), `position_sizing` (20%), `adversarial_check` (<2 warnings). Confirmed outcomes + arb bypass entirely.

### Maker Strategy
Limit orders at `fair_value - 2¢`. Dynamic: high edge → 1¢, low edge → 3¢. Stale cancel 30min. Adverse selection pause on 3+ fills in 10min.

### Resting Orders
- Fill check: `order.status == "executed"` or `remaining_count == 0`. Else "resting".
- Pending orders tracked in risk manager via `add_pending_order()`/`clear_pending_order()`.
- Auto-cancel: buy 25min, exit/hedge 30min.

### Self-Improver (Phase 4 — Live)
Weekly (Sun 11 PM). Safe params: MIN_EDGE, MAKER_SPREAD_BUFFER_CENTS, MAX_MODEL_DIVERGENCE_F, NEXT_DAY_SIZING_MULTIPLIER, ROUNDING_BUFFER_SOFT_F, PRE_SETTLEMENT_SIZING_MULT, NO_SIDE_SIZING_MULTIPLIER, NO_SIDE_MAX_PRICE_CENTS. Max 3 changes/week, 25% max change, 5% improvement required. Never touches risk limits or kill switch.

### Dashboard Force-Exit
`POST /api/force-exit` — `{"ticker": "KXHIGH..."}` or `{"ticker": "all"}`. Uses shared KalshiClient via `set_kalshi_client()`. Price floor: `yes_price: 1` or `no_price: 1`.

### Model Accuracy Flow
`strategy.py` fetches weights+biases → `weather_engine.get_temperature_distribution()` applies per-model bias correction then accuracy weights in `_build_distribution()`. On settlement: `trade_intelligence._update_model_accuracy_from_settlement()` → `quant_analytics.record_model_accuracy()` → `model_accuracy.json`. Daily: `log_daily_forecasts()` logs all 20 cities → `reconcile_forecast_log()` records accuracy (builds data 10x faster than settlements alone).

### Infrastructure Invariants (landmines if refactoring)
- **Dashboard outside restart loop**: `start_dashboard_server()` in `__main__` block, NOT inside `main()`. Prevents port binding crash loop.
- **Python 3.12 import alias**: `import traceback as _tb` inside `main()` to avoid shadowing global `traceback`.
- **Timezone-aware everywhere**: `datetime.now(timezone.utc)`, `_parse_ts()` helper handles mixed naive/aware.
- **Cache key split**: `get_latest_observation()` uses `latest_{station}`, `_fetch_todays_observations()` uses `obs_{station}`. Different formats.
- **Open-Meteo model names**: `gfs_seamless`, `ecmwf_ifs025`, `icon_seamless_eps`, `gem_global`. Silently renamed Feb 24 — returns None on wrong names (zero trades, no error logged).

## Future Roadmap
- **Phase 2**: Becker dataset integration (400M+ trades, calibration surface, optimal timing)
- **Phase 3**: Empirical Kelly with Monte Carlo (replace quarter-Kelly)
- **Phase 4**: DONE (self_improver.py)
