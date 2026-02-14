# Contributing

## Development Workflow

1. **Branch from `main`** — create a feature branch for your changes
2. **Run tests locally** before pushing:
   ```bash
   cd bot
   make test       # unit tests
   make backtest   # offline backtest with CI thresholds
   ```
3. **Open a PR** — CI will run both test suites automatically
4. **All checks must pass** before merge

## CI Gate

Every PR runs:
- **Unit tests** (`bot/tests/`) — validate strategy, exchange, and weather engine logic
- **Offline backtest** (`bot/scripts/backtest.py`) — run deterministic backtest against stored fixture data

The backtest checks these thresholds (see `bot/configs/poc.yaml`):
- Minimum Sharpe ratio
- Maximum drawdown
- Minimum win rate
- Minimum number of trades
- Maximum ROI loss

If any threshold is violated, CI fails and the PR is blocked.

## Guardrails

- **No live API calls in tests** — all tests use `SimExchange` or mocks
- **No credentials in code** — use environment variables for API keys
- **Deterministic backtest** — seeded with `random.seed(42)` for reproducibility
- **Never commit** `.env`, `.pem`, or credential files

## Project Structure

```
bot/
  src/           # Source code (strategy, exchange, weather engine, etc.)
  tests/         # Unit tests
  scripts/       # Backtest runner and utilities
  configs/       # YAML configuration files
  data/fixtures/ # Stored market snapshots and historical data
```

## Adding a New Strategy Parameter

1. Add the parameter to `bot/configs/poc.yaml` under `backtest:`
2. Update `bot/scripts/backtest.py` to read and use the parameter
3. Run `make backtest` to verify thresholds still pass
4. Add a unit test if applicable
