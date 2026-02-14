# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

Core: `requests`, `cryptography`, `numpy`, `scipy`, `pandas`. Installed via a local venv (not tracked in repo). No requirements.txt exists.

External APIs (all free, no keys needed): Open-Meteo (ensemble forecasts + historical archive), NWS (settlement source + live observations).

Kalshi API requires RSA key-pair auth — credentials set via `API_KEY_ID` and `PRIVATE_KEY_PATH` in `config.py`.

## Architecture

This is a weather prediction market trading bot for Kalshi. It runs a continuous loop (every 5 minutes) that scans weather markets for 4 cities (NYC, CHI, MIA, AUS), detects mispricing, and places limit orders.

### Decision Flow

```
market_scanner.py  →  strategy.py  →  signal_confirmer.py  →  risk_manager.py  →  kalshi_client.py
   (discover)         (evaluate)       (validate)              (safety check)      (execute)
```

### Module Responsibilities

- **`kalshi_bot.py`** — Entry point and main loop. Initializes all components, orchestrates the scan→analyze→trade cycle, handles position exits and settlement tracking.
- **`kalshi_client.py`** — Kalshi API wrapper. RSA-PSS signature auth, dual environment support (demo vs production URLs), market fetching, order placement, position queries.
- **`config.py`** — All tunable parameters: credentials, risk limits, strategy thresholds, scan intervals. Values are in cents (100 cents = $1.00).
- **`market_scanner.py`** — Queries Kalshi for weather series (KXHIGHNY, KXHIGHCHI, KXHIGHMIA, KXHIGHAUS). Filters to open markets and returns bid/ask/volume data.
- **`strategy.py`** — Two strategies: **S1 (Weather Edge)** compares ensemble probabilities vs market prices for ≥8% mispricing; **S2 (Spread Arbitrage)** finds YES+NO pairs costing < $0.98. Applies 7 rejection gates before evaluation.
- **`weather_engine.py`** — Aggregates 143 ensemble members from 4 sources (GFS 31, ECMWF 51, ICON-EPS 40, GEM 21) via Open-Meteo. Builds probability distributions across temperature buckets.
- **`signal_confirmer.py`** — Voting system from 4 deterministic models (HRRR, ECMWF, ICON, GEM). Outputs confirmation level: STRONG (3+ agree, 1.5x), CONFIRM (2, 1.0x), WEAK (0.5x), REJECT.
- **`risk_manager.py`** — 8 safety layers: daily loss limit, total exposure cap, position count, per-city caps, consecutive loss pause, daily trade count, cooldown timer, approval threshold. State persisted in `risk_state.json`.
- **`trade_intelligence.py`** — Position exit logic, forecast bias learning per NWS station, time-of-day sizing adjustments, intraday observation tracking, settlement P&L recording.
- **`quant_analytics.py`** — Backtesting against 90 days of historical data, per-model accuracy weighting, regime detection (stable vs volatile), smart order placement (bid inside spread), correlation-aware position sizing.
- **`market_quality.py`** — Liquidity filter (max 15¢ spread, min 1 contract volume), probability guardrails (rejects <12¢ longshots and >88¢ near-certainties).

### Data Files (Runtime State)

All state is persisted as JSON in the project root: `trade_history.json`, `risk_state.json`, `pnl_history.json`, `backtest_results.json`. These are read/written by the bot at runtime and should not be manually edited while the bot is running.

### Key Design Decisions

- **Quarter-Kelly sizing** — Position sizes use Kelly criterion divided by 4 for safety, multiplied by the signal confirmation level.
- **Maker strategy** — Prefers limit orders (not market orders) for better fills.
- **NWS settlement** — Markets settle on NWS Daily Climate Reports from specific stations (KNYC, KMDW, KMIA, KAUS), not model forecasts.
- **Longshot avoidance** — Never buys contracts priced below 12¢, per academic research on prediction market biases.
