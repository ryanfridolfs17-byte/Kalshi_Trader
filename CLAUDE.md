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

Weather uses `signal_confirmer.py` (4-model voting). S&P 500 uses `spx_confirmer.py` (momentum/vol/historical).

### Module Responsibilities

- **`kalshi_bot.py`** — Entry point and main loop. Orchestrates scan→analyze→trade cycle per market type. Handles exits and settlement tracking.
- **`kalshi_client.py`** — Kalshi API wrapper. RSA-PSS signature auth, dual environment support (demo vs production URLs), market fetching, order placement, position queries.
- **`config.py`** — All tunable parameters: credentials, risk limits, strategy thresholds, scan intervals, market type toggles. Values are in cents (100 cents = $1.00).
- **`market_scanner.py`** — Queries Kalshi for weather series + S&P 500 brackets. `scan_all_enabled_markets()` respects `MARKET_TYPES` toggles.
- **`strategy.py`** — Three strategies: **S1 (Weather Edge)** ensemble vs market prices; **S2 (Spread Arbitrage)** YES+NO < $0.98; **S3 (SP500 Brackets)** VIX-implied distributions vs bracket prices. S3 components only initialized when sp500 is enabled.
- **`weather_engine.py`** — Aggregates 143 ensemble members from 4 sources (GFS 31, ECMWF 51, ICON-EPS 40, GEM 21) via Open-Meteo. Builds probability distributions across temperature buckets.
- **`volatility_engine.py`** — VIX-based price distributions for S&P 500. Uses yfinance for SPY/VIX data, scipy.stats.norm CDF for bracket probability calculations. Includes intraday vol adjustment.
- **`signal_confirmer.py`** — Weather voting system from 4 deterministic models (HRRR, ECMWF, ICON, GEM). Outputs: STRONG (3+ agree, 1.5x), CONFIRM (2, 1.0x), WEAK (0.5x), REJECT.
- **`spx_confirmer.py`** — S&P 500 signal confirmation: intraday momentum, realized vs implied vol ratio, historical bracket hit rate. Same verdict/multiplier interface as weather confirmer.
- **`risk_manager.py`** — 9 safety layers: daily loss limit, total exposure cap, position count, per-city/market caps, correlated position cap, consecutive loss pause, daily trade count, cooldown, settlement proximity. State persisted in `risk_state.json`.
- **`trade_intelligence.py`** — Position exit logic, forecast bias learning per NWS station, time-of-day sizing adjustments, intraday observation tracking, settlement P&L recording.
- **`quant_analytics.py`** — Backtesting against 90 days of historical data, per-model accuracy weighting, regime detection (stable vs volatile), smart order placement, correlation-aware position sizing.
- **`market_quality.py`** — Liquidity filter (max 15c spread, min 1 contract volume), probability guardrails (rejects <12c longshots and >88c near-certainties).
- **`dashboard.py`** — Flask-based web dashboard with health monitoring, email alerts, position/trade display with market type badges.

### Data Files (Runtime State)

All state is persisted as JSON in `STATE_DIR` (defaults to `.`, set to `/data` on Railway for volume persistence): `trade_history.json`, `risk_state.json`, `pnl_history.json`, `backtest_results.json`, `edge_attribution.json`, `bot_status.json`, `pending_trades.json`.

### Key Design Decisions

- **Quarter-Kelly sizing** — Position sizes use Kelly criterion divided by 4 for safety, multiplied by the signal confirmation level.
- **Maker strategy** — Prefers limit orders (not market orders) for better fills.
- **NWS settlement** — Weather markets settle on NWS Daily Climate Reports from specific stations (KNYC, KMDW, KMIA, KAUS), not model forecasts.
- **Longshot avoidance** — Never buys contracts priced below 12c, per academic research on prediction market biases.
- **Market type isolation** — Each market type is gated behind a toggle. New market code paths only execute when explicitly enabled. SP500 uses `city_code="SP500"` in the risk manager for per-market exposure tracking.
