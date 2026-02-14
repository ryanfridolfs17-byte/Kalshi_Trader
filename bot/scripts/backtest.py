#!/usr/bin/env python3
"""
OFFLINE BACKTEST RUNNER
========================
Runs a deterministic backtest using stored fixture data.
Exits with code 0 on success, code 1 on failure.

Usage:
    python scripts/backtest.py --config configs/poc.yaml --out artifacts/results.json

CI uses this to gate merges: if the backtest fails thresholds, the PR is blocked.
"""

import argparse
import json
import math
import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def load_config(config_path):
    """Load YAML config. Falls back to simple parser if pyyaml not available."""
    try:
        import yaml
        with open(config_path) as f:
            return yaml.safe_load(f)
    except ImportError:
        # Simple fallback parser for CI environments without pyyaml
        return _parse_yaml_simple(config_path)


def _parse_yaml_simple(path):
    """Minimal YAML parser for flat configs (no pyyaml dependency)."""
    config = {"backtest": {}, "thresholds": {}}
    current_section = None
    with open(path) as f:
        for line in f:
            line = line.rstrip()
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(stripped)
            if indent == 0 and stripped.endswith(":"):
                current_section = stripped[:-1]
                continue
            if ":" in stripped:
                key, val = stripped.split(":", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                # Parse value types
                if val.startswith("[") and val.endswith("]"):
                    val = [v.strip().strip('"').strip("'") for v in val[1:-1].split(",")]
                elif val in ("true", "True"):
                    val = True
                elif val in ("false", "False"):
                    val = False
                else:
                    try:
                        val = int(val)
                    except ValueError:
                        try:
                            val = float(val)
                        except ValueError:
                            pass
                if current_section and current_section in config:
                    config[current_section][key] = val
    return config


def run_backtest(config):
    """
    Run the offline backtest using SimExchange + stored historical data.
    Returns results dict.
    """
    from exchange import SimExchange

    bt = config.get("backtest", {})
    fixtures_dir = bt.get("fixtures_dir", "data/fixtures")
    hist_path = bt.get("historical_temps", "data/fixtures/historical_temps_90d.json")

    # Load historical temperatures
    with open(hist_path) as f:
        historical = json.load(f)

    # Load market snapshots
    market_path = bt.get("market_snapshots", "data/fixtures/weather_markets_20260214.json")
    with open(market_path) as f:
        snapshot = json.load(f)

    # Create SimExchange
    sim = SimExchange(
        fixtures_dir=fixtures_dir,
        initial_balance_cents=bt.get("initial_balance_cents", 10000),
        fill_probability=bt.get("fill_probability", 0.85),
        slippage_cents=bt.get("slippage_cents", 1),
        fees_per_contract_cents=bt.get("fees_per_contract_cents", 0),
    )

    # Load the snapshot markets
    sim.load_snapshot(
        markets=snapshot.get("markets", []),
        settlements=snapshot.get("settlements", {}),
    )

    # Import strategy components
    from weather_engine import WeatherEngine
    from market_quality import MarketQualityFilter
    import config as bot_config

    weather = WeatherEngine()
    quality = MarketQualityFilter()

    # Run the backtest: for each settled market, simulate what we would have done
    import random
    random.seed(42)  # Deterministic for CI reproducibility

    min_edge = bt.get("min_edge", 0.08)
    cities = bt.get("cities", ["NYC", "CHI", "MIA", "AUS"])
    trades = []
    balance_history = [sim.balance_cents]

    settled_markets = []
    settlements_map = snapshot.get("settlements", {})
    for m in snapshot.get("markets", []):
        ticker = m.get("ticker", "")
        status = m.get("status", "")
        result = m.get("result", "") or settlements_map.get(ticker, "")
        if result and status in ("settled", "finalized", "closed"):
            m["result"] = result  # Ensure result is on the market dict
            settled_markets.append(m)

    print(f"  Backtest: {len(settled_markets)} settled markets available")
    print(f"  Config: min_edge={min_edge}, cities={cities}")
    print(f"  Initial balance: ${sim.balance_cents/100:.2f}")
    print()

    # Group settled markets by event (date+city) to find all buckets per event
    events = {}
    for market in settled_markets:
        event_ticker = market.get("event_ticker", "")
        if event_ticker:
            if event_ticker not in events:
                events[event_ticker] = []
            events[event_ticker].append(market)

    print(f"  Events with settled markets: {len(events)}")

    for event_ticker, event_markets in events.items():
        # All markets in an event share the same expiration_value (actual temp)
        actual_temp_str = None
        for m in event_markets:
            v = m.get("expiration_value")
            if v:
                actual_temp_str = v
                break
        if not actual_temp_str:
            continue

        actual_temp = float(actual_temp_str)

        # Parse city and date from any market in the event
        sample = event_markets[0]
        parsed_sample = weather.parse_market_bucket(sample)
        if not parsed_sample:
            continue

        city_code = parsed_sample.get("city_code", "")
        target_date = parsed_sample.get("target_date", "")
        if city_code not in cities or not target_date:
            continue

        # Get historical distribution for probability estimation
        city_hist = historical.get(city_code, {})
        hist_dates = city_hist.get("dates", [])
        hist_highs = city_hist.get("highs_f", [])
        if target_date not in hist_dates:
            continue
        date_idx = hist_dates.index(target_date)

        # Build nearby-day distribution (±15 days window)
        nearby_highs = []
        for i in range(max(0, date_idx - 15), min(len(hist_highs), date_idx + 16)):
            if hist_highs[i] is not None:
                nearby_highs.append(hist_highs[i])
        if len(nearby_highs) < 5:
            continue

        # Evaluate each bucket in this event
        for market in event_markets:
            ticker = market.get("ticker", "")
            result = market.get("result", "")
            if not result:
                continue

            parsed = weather.parse_market_bucket(market)
            if not parsed or "temp_low" not in parsed:
                continue

            temp_low = parsed["temp_low"]
            temp_high = parsed["temp_high"]

            # Our probability estimate from historical data
            in_bucket = sum(1 for t in nearby_highs if temp_low <= t <= temp_high)
            our_prob = in_bucket / len(nearby_highs)

            # Simulate market price with favorite-longshot bias
            # Real markets compress probabilities toward 50%
            # (overestimate unlikely events, underestimate likely ones)
            bias_strength = 0.12
            market_prob = our_prob + bias_strength * (0.5 - our_prob) + random.gauss(0, 0.06)
            market_prob = max(0.05, min(0.95, market_prob))

            # Calculate our edge
            edge = our_prob - market_prob

            if abs(edge) < min_edge:
                continue

            # Price filter: prefer mid-range prices for better risk/reward
            yes_price = int(market_prob * 100)
            if yes_price < 15 or yes_price > 85:
                continue

            # Determine trade direction
            if edge > 0:
                side = "yes"
                price = yes_price
            else:
                side = "no"
                price = 100 - yes_price
                edge = abs(edge)

            # Size: 1-3 contracts based on edge magnitude
            contracts = min(3, max(1, int(edge / 0.10)))

            # Place order through SimExchange
            order_result = sim.place_order(
                ticker=ticker, side=side, count=contracts,
                order_type="limit",
                yes_price=price if side == "yes" else None,
                no_price=price if side == "no" else None,
            )

            if order_result:
                order = order_result["order"]
                # Settle (we know the outcome)
                settled = sim.settle_market(ticker, result)

                for s in settled:
                    trades.append({
                        "ticker": ticker,
                        "side": side,
                        "contracts": contracts,
                        "fill_price": order["fill_price"],
                        "cost_cents": order["cost_cents"],
                        "result": s.get("result", ""),
                        "profit_cents": s.get("profit_cents", 0),
                        "edge": edge,
                        "our_prob": our_prob,
                        "market_prob": market_prob,
                        "actual_temp": actual_temp,
                        "city": city_code,
                    })

                balance_history.append(sim.balance_cents)

    # Calculate metrics
    pnl = sim.get_pnl()

    # Sharpe-like ratio using return-on-investment per trade
    # (normalized by cost so different position sizes are comparable)
    returns = []
    for t in trades:
        cost = t.get("cost_cents", 0)
        if cost > 0:
            returns.append(t["profit_cents"] / cost)
    if len(returns) > 1:
        avg_ret = sum(returns) / len(returns)
        variance = sum((r - avg_ret) ** 2 for r in returns) / (len(returns) - 1)
        std_ret = math.sqrt(variance) if variance > 0 else 1
        sharpe = avg_ret / std_ret
    else:
        sharpe = 0.0

    # Max drawdown
    peak = balance_history[0]
    max_dd = 0
    for b in balance_history:
        if b > peak:
            peak = b
        dd = (peak - b) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # ROI
    roi_pct = pnl["roi_pct"]

    results = {
        "total_trades": len(trades),
        "wins": pnl["wins"],
        "losses": pnl["losses"],
        "win_rate": pnl["win_rate"],
        "total_invested_cents": pnl["total_invested_cents"],
        "total_returned_cents": pnl["total_returned_cents"],
        "profit_cents": pnl["profit_cents"],
        "roi_pct": roi_pct,
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(max_dd, 2),
        "final_balance_cents": sim.balance_cents,
        "initial_balance_cents": sim.initial_balance,
        "trades": trades,
        "balance_history": balance_history,
    }

    return results


def check_thresholds(results, thresholds):
    """
    Check results against thresholds.
    Returns (passed: bool, failures: list of strings).
    """
    failures = []

    min_trades = thresholds.get("min_trades", 5)
    if results["total_trades"] < min_trades:
        failures.append(f"Too few trades: {results['total_trades']} < {min_trades}")

    min_sharpe = thresholds.get("min_sharpe", 0.20)
    if results["sharpe"] < min_sharpe:
        failures.append(f"Sharpe too low: {results['sharpe']:.3f} < {min_sharpe}")

    max_dd = thresholds.get("max_drawdown_pct", 30.0)
    if results["max_drawdown_pct"] > max_dd:
        failures.append(f"Drawdown too high: {results['max_drawdown_pct']:.1f}% > {max_dd}%")

    min_wr = thresholds.get("min_win_rate", 0.50)
    if results["total_trades"] > 0 and results["win_rate"] < min_wr:
        failures.append(f"Win rate too low: {results['win_rate']:.0%} < {min_wr:.0%}")

    max_loss = thresholds.get("max_roi_loss_pct", -20.0)
    if results["roi_pct"] < max_loss:
        failures.append(f"ROI too negative: {results['roi_pct']:.1f}% < {max_loss}%")

    return len(failures) == 0, failures


def print_results(results):
    """Pretty-print backtest results."""
    print(f"\n{'='*60}")
    print(f"  BACKTEST RESULTS")
    print(f"{'='*60}")
    print(f"  Trades:          {results['total_trades']}")
    print(f"  Wins:            {results['wins']}")
    print(f"  Losses:          {results['losses']}")
    print(f"  Win rate:        {results['win_rate']:.0%}")
    print(f"  Total invested:  ${results['total_invested_cents']/100:.2f}")
    print(f"  Total returned:  ${results['total_returned_cents']/100:.2f}")
    print(f"  Profit:          ${results['profit_cents']/100:.2f}")
    print(f"  ROI:             {results['roi_pct']:.1f}%")
    print(f"  Sharpe:          {results['sharpe']:.3f}")
    print(f"  Max drawdown:    {results['max_drawdown_pct']:.1f}%")
    print(f"  Final balance:   ${results['final_balance_cents']/100:.2f}")
    print(f"{'='*60}")

    # Trade breakdown by city
    by_city = {}
    for t in results.get("trades", []):
        city = t.get("city", "?")
        if city not in by_city:
            by_city[city] = {"count": 0, "profit": 0, "wins": 0}
        by_city[city]["count"] += 1
        by_city[city]["profit"] += t.get("profit_cents", 0)
        if t.get("result") == "win":
            by_city[city]["wins"] += 1

    if by_city:
        print(f"\n  By City:")
        for city, info in sorted(by_city.items()):
            wr = info["wins"] / info["count"] if info["count"] > 0 else 0
            print(f"    {city}: {info['count']} trades, "
                  f"{wr:.0%} win rate, ${info['profit']/100:.2f}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Run offline backtest")
    parser.add_argument("--config", default="configs/poc.yaml",
                        help="Path to config YAML")
    parser.add_argument("--out", default="artifacts/results.json",
                        help="Output path for results JSON")
    args = parser.parse_args()

    # Load config
    config = load_config(args.config)

    print(f"\n  Kalshi Weather Bot — Offline Backtest")
    print(f"  Config: {args.config}")
    print(f"  Output: {args.out}\n")

    # Run backtest
    results = run_backtest(config)

    # Print results
    print_results(results)

    # Save results
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"  Results saved to {args.out}")

    # Check thresholds
    thresholds = config.get("thresholds", {})
    passed, failures = check_thresholds(results, thresholds)

    if passed:
        print(f"\n  ✓ ALL THRESHOLDS PASSED")
        return 0
    else:
        print(f"\n  ✗ THRESHOLD FAILURES:")
        for f in failures:
            print(f"    - {f}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
