"""
KALSHI TRADING BOT v3.0 — Weather-Focused Edition
=====================================================
Ensemble weather forecasting + spread arbitrage.

DECISION FLOW (every 5 minutes):
  1. Scan Kalshi for weather markets (4 cities)
  2. Fetch 143 ensemble forecasts → probability distribution
  3. Compare vs market prices → detect mispricing ≥8%
  4. Get second opinions from 4 independent sources
  5. Risk check (7 safety layers)
  6. Size with Quarter-Kelly × confirmation multiplier
  7. Execute as LIMIT order

PROGRESSION PATH:
  Level 1: DRY_RUN=True   → Analyzes only, no orders
  Level 2: ENVIRONMENT=demo → Practice money, real orders
  Level 3: ENVIRONMENT=prod → Real money (only when ready!)
"""

import time
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
import config


def main():
    """Main entry point."""

    # ─── START DASHBOARD FIRST ───
    # Must bind to PORT before anything else so Railway health checks pass.
    # Runs in a non-daemon thread so it survives if the bot crashes.
    from dashboard import start_dashboard_server
    port = int(os.environ.get("PORT", 8050))
    start_dashboard_server(port)

    # ─── STARTUP BANNER ───
    print()
    print("  " + "=" * 54)
    print("  |     KALSHI WEATHER BOT v3.0                      |")
    print("  |     Ensemble Forecast × Arbitrage Engine         |")
    print("  " + "=" * 54)
    print()

    # Show mode
    if config.DRY_RUN:
        print("  MODE: 🔍 DRY RUN (analysis only, no orders)")
    elif config.ENVIRONMENT == "demo":
        print("  MODE: 🧪 DEMO (practice money, real orders)")
    else:
        print("  MODE: 💰 PRODUCTION (REAL MONEY)")
        print("  ⚠️  WARNING: Trades will use REAL FUNDS")
        if sys.stdin.isatty():
            resp = input("  Type 'yes' to confirm: ")
            if resp.lower() != "yes":
                print("  Aborted. Switch to demo mode in config.py")
                return
        else:
            print("  (headless mode — skipping confirmation)")

    # ─── INITIALIZE COMPONENTS ───
    from kalshi_client import KalshiClient
    from risk_manager import RiskManager
    from market_scanner import MarketScanner
    from strategy import Strategy
    from trade_intelligence import TradeIntelligence

    client = KalshiClient()
    risk = RiskManager()
    scanner = MarketScanner(client)
    strategy = Strategy(kalshi_client=client)
    intel = strategy.intel  # Shared instance

    trade_log = _load_trade_log()

    # ─── STARTUP CLEANUP ───
    # If DRY_RUN, expire all unsettled positions from previous sessions.
    # Dry-run positions are never placed on the exchange, so they should
    # not persist across sessions and block new signals.
    if config.DRY_RUN and trade_log:
        expired_count = 0
        for trade in trade_log:
            if trade.get("settled"):
                continue
            if trade.get("status") == "dry_run":
                trade["settled"] = True
                trade["result"] = "expired_dry_run"
                trade["profit_cents"] = 0
                cost = trade.get("cost_cents", 0)
                city = trade.get("city_code", "")
                ticker = trade.get("ticker", "")
                risk.release_exposure(ticker, cost, city)
                expired_count += 1
        if expired_count > 0:
            _save_trade_log(trade_log)
            print(f"  [CLEANUP] Expired {expired_count} stale DRY_RUN positions")
            print(f"  [CLEANUP] Exposure freed — ready for fresh trading")

    # Print strategy info
    print(strategy.get_strategy_summary())

    # Offer backtest before live trading (only in interactive mode)
    if sys.stdin.isatty():
        print("  Would you like to run a backtest first? (recommended)")
        print("  Enter a city code (NYC/CHI/MIA/AUS) or press Enter to skip:")
        try:
            bt_input = input("  > ").strip().upper()
            if bt_input in ["NYC", "CHI", "MIA", "AUS"]:
                strategy.quant.run_backtest(bt_input, days_back=90, edge_threshold=config.MIN_EDGE)
        except (EOFError, KeyboardInterrupt):
            pass
    else:
        print("  (headless mode — skipping backtest prompt)")

    # Show model weights
    for city in config.WEATHER_CITIES:
        strategy.quant.print_model_weights(city)

    # Check balance if authenticated
    if config.API_KEY_ID != "YOUR_API_KEY_ID_HERE":
        try:
            balance = client.get_balance()
            if balance is not None:
                print(f"  Account balance: ${balance/100:.2f}")
        except Exception:
            print("  (Could not fetch balance — check API keys)")

    risk.print_status()

    print(f"  Scanning every {config.SCAN_INTERVAL // 60} minutes")
    print(f"  Cities: {', '.join(config.WEATHER_CITIES)}")
    print(f"  Press Ctrl+C to stop\n")

    # ─── MAIN LOOP ───
    cycle = 0
    last_report_date = datetime.now().strftime("%Y-%m-%d")
    while True:
        try:
            cycle += 1
            now = datetime.now().strftime("%H:%M:%S")
            print(f"\n{'='*60}")
            print(f"  Scan #{cycle} — {now}")
            print(f"{'='*60}")

            trade_count_this_cycle = 0

            # ═══════════════════════════════════════════════
            # STEP 0: CHECK SETTLEMENTS & EXITS
            # ═══════════════════════════════════════════════
            print("\n  [STEP 0a] Checking settlements...")
            settled = intel.check_settlements(trade_log, risk, quant=strategy.quant)
            if settled:
                print(f"  → {len(settled)} trades settled")
                _save_trade_log(trade_log)
            intel.print_pnl()

            # Daily report generation on day change
            today_str = datetime.now().strftime("%Y-%m-%d")
            if today_str != last_report_date:
                _generate_daily_report(trade_log, strategy)
                last_report_date = today_str

            # Check for dashboard-approved pending trades
            _process_approved_trades(client, risk, trade_log)

            print("  [STEP 0b] Checking intraday temperatures...")
            intel.print_intraday_temps()

            print("  [STEP 0c] Checking exit opportunities...")
            exits = intel.check_exits(risk.state.get("positions", []), strategy.weather)
            for exit_rec in exits:
                print(f"  ⚡ EXIT: {exit_rec['ticker']} — {exit_rec['reason']}")
                if not config.DRY_RUN and exit_rec["urgency"] == "high":
                    print(f"    → Auto-exiting (high urgency)")
                    # TODO: Place sell order via client
                elif exit_rec["urgency"] == "medium":
                    print(f"    → Consider exiting manually")

            # ═══════════════════════════════════════════════
            # STEP 1: SCAN WEATHER MARKETS
            # ═══════════════════════════════════════════════
            print("\n  [STEP 1] Scanning weather markets...")
            weather_markets = scanner.scan_weather_markets()
            skip_reasons = {}  # Track why markets are skipped
            signals_found = 0

            if weather_markets:
                scanner.print_weather_summary(weather_markets)

                # ═══════════════════════════════════════════
                # STEPS 2-6: EVALUATE EACH MARKET
                # ═══════════════════════════════════════════

                for market in weather_markets:
                    ticker = market.get("ticker", "")
                    title = market.get("title", "")

                    # Quick check: skip if this city is already maxed out
                    parsed_quick = strategy.weather.parse_market_bucket(market)
                    if parsed_quick:
                        city = parsed_quick.get("city_code", "")
                        city_exp = risk.state.get("city_exposure", {}).get(city, 0)
                        if city_exp >= config.MAX_PER_CITY_CENTS:
                            skip_reasons["City maxed out"] = skip_reasons.get("City maxed out", 0) + 1
                            continue

                    # Quick-evaluate to see if this market is even worth printing
                    signal = strategy.evaluate_market(market)

                    if signal["signal"] == "skip":
                        reason = signal.get("reasoning") or "Dead market"
                        # Categorize the skip reason
                        if "Dead market" in str(reason) or reason is None:
                            cat = "Dead/frozen market"
                        elif "Quality" in str(reason):
                            cat = "Quality filter"
                        elif "No signals" in str(reason):
                            cat = "No edge found"
                        elif "edge" in str(reason).lower():
                            cat = "Edge too small"
                        else:
                            cat = str(reason)[:40]
                        skip_reasons[cat] = skip_reasons.get(cat, 0) + 1
                        continue

                    # Market passed filters — print it
                    print(f"\n  [STEP 2-3] Evaluating: {ticker}")
                    print(f"            {title}")

                    # We have a signal!
                    print(f"\n  ★ SIGNAL FOUND ★")
                    print(f"    Strategy:    {signal.get('strategy', '?')}")
                    print(f"    Side:        {signal['side'].upper()}")
                    print(f"    Edge:        {signal['edge']:.1%}")
                    print(f"    Confidence:  {signal['confidence']:.1%}")
                    print(f"    Price:       {signal['price_cents']}¢")
                    print(f"    Contracts:   {signal['suggested_contracts']}")
                    print(f"    Confirm:     {signal.get('confirmation_verdict', 'N/A')}")
                    print(f"    Reasoning:   {signal['reasoning']}")

                    # ═══════════════════════════════════════
                    # STEP 5: RISK CHECK
                    # ═══════════════════════════════════════
                    # Add city info for per-city risk check
                    parsed = strategy.weather.parse_market_bucket(market)
                    if parsed:
                        signal["city_code"] = parsed["city_code"]

                    # Pass close_time for settlement proximity check
                    if market.get("close_time"):
                        signal["close_time"] = market["close_time"]

                    approved, reason = risk.check_trade(signal)

                    if approved is False:
                        print(f"    ✗ BLOCKED: {reason}")
                        continue

                    # ═══════════════════════════════════════
                    # STEP 6: EXECUTE (or queue for approval)
                    # ═══════════════════════════════════════
                    if approved == "NEEDS_APPROVAL":
                        print(f"    ⚠ {reason}")
                        if sys.stdin.isatty():
                            user_input = input("    Approve? (y/n): ").strip().lower()
                            if user_input != "y":
                                print("    → Skipped by user")
                                continue
                        else:
                            # Headless: queue for dashboard approval instead of blocking
                            _add_pending_trade(signal, market)
                            print(f"    ⏳ QUEUED for dashboard approval (needs manual approval)")
                            continue

                    # Only queue for approval if over position limit + exceptional edge + STRONG
                    if _should_require_approval(signal, risk):
                        _add_pending_trade(signal, market)
                        print(f"    ⏳ QUEUED for dashboard approval (over {config.MAX_OPEN_POSITIONS} positions, edge {signal['edge']:.1%}, STRONG)")
                        continue

                    # Execute the trade
                    trade_result = _execute_trade(
                        client, risk, signal, trade_log, market
                    )

                    if trade_result:
                        trade_count_this_cycle += 1

            # ═══════════════════════════════════════════════
            # ARBITRAGE SCAN (also checks weather markets)
            # ═══════════════════════════════════════════════
            if config.SCAN_ALL_FOR_ARBITRAGE:
                print("\n  [ARB] Scanning for arbitrage...")
                all_markets = weather_markets or []
                arb_list = scanner.scan_for_arbitrage(all_markets)

                for arb in arb_list:
                    market = arb["market"]
                    signal = strategy.evaluate_market(market)

                    if signal["signal"] != "skip" and signal.get("strategy") == "S2-Arbitrage":
                        print(f"\n  ★ ARBITRAGE ★ {market['ticker']}: {arb['gap_cents']}¢ free")

                        approved, reason = risk.check_trade(signal)
                        if approved is True:
                            _execute_trade(client, risk, signal, trade_log, market)
                            trade_count_this_cycle += 1

            # ═══════════════════════════════════════════════
            # CYCLE SUMMARY
            # ═══════════════════════════════════════════════
            print(f"\n  Cycle #{cycle} complete. Trades: {trade_count_this_cycle}")

            # Print diagnostic breakdown so we can see WHY markets were skipped
            if skip_reasons:
                total_skipped = sum(skip_reasons.values())
                print(f"\n  ┌─ Market Filter Breakdown ({total_skipped} skipped) ──")
                for reason, count in sorted(skip_reasons.items(), key=lambda x: -x[1]):
                    print(f"  │  {reason}: {count}")
                print(f"  └──────────────────────────────────────")

            risk.print_status()
            _print_performance(trade_log)

            # Write bot status for dashboard
            _write_bot_status(cycle, skip_reasons, trade_count_this_cycle, strategy, client)

            # Wait for next cycle
            print(f"  Next scan in {config.SCAN_INTERVAL // 60} minutes...")
            time.sleep(config.SCAN_INTERVAL)

        except KeyboardInterrupt:
            print("\n\n  Bot stopped by user.")
            print(f"  Total cycles: {cycle}")
            _print_performance(trade_log)
            sys.exit(0)

        except Exception as e:
            print(f"\n  ⚠ Error in cycle {cycle}: {e}")
            print(f"  Retrying in 60 seconds...")
            time.sleep(60)


def _execute_trade(client, risk, signal, trade_log, market):
    """Execute a trade based on a signal."""
    ticker = signal["ticker"]
    side = signal["side"]
    price = signal["price_cents"]
    contracts = signal["suggested_contracts"]
    cost = price * contracts

    if config.DRY_RUN:
        status = "dry_run"
        print(f"    [DRY RUN] Would buy {contracts}x {side.upper()} @ {price}¢ = ${cost/100:.2f}")
    elif config.ENVIRONMENT == "demo":
        status = "demo_submitted"
        print(f"    [DEMO] Submitting: {contracts}x {side.upper()} @ {price}¢")
        try:
            result = client.create_order(
                ticker=ticker,
                side=side,
                count=contracts,
                type="limit",
                yes_price=price if side == "yes" else None,
                no_price=price if side == "no" else None,
            )
            if result:
                print(f"    ✓ Order submitted: {result.get('order', {}).get('order_id', 'OK')}")
                status = "demo_filled"
        except Exception as e:
            print(f"    ✗ Order failed: {e}")
            status = "demo_error"
    else:
        status = "live_submitted"
        print(f"    [LIVE] Submitting: {contracts}x {side.upper()} @ {price}¢")
        try:
            result = client.create_order(
                ticker=ticker,
                side=side,
                count=contracts,
                type="limit",
                yes_price=price if side == "yes" else None,
                no_price=price if side == "no" else None,
            )
            if result:
                print(f"    ✓ LIVE order submitted!")
                status = "live_filled"
        except Exception as e:
            print(f"    ✗ LIVE order failed: {e}")
            status = "live_error"

    # Record trade
    trade_entry = {
        "timestamp": datetime.now().isoformat(),
        "ticker": ticker,
        "title": market.get("title", ""),
        "side": side,
        "price_cents": price,
        "contracts": contracts,
        "cost_cents": cost,
        "strategy": signal.get("strategy", ""),
        "edge": signal.get("edge", 0),
        "confidence": signal.get("confidence", 0),
        "confirmation": signal.get("confirmation_verdict", ""),
        "predicted_high": signal.get("predicted_high"),
        "city_code": signal.get("city_code", ""),
        "status": status,
        "reasoning": signal.get("reasoning", ""),
        "settled": False,
    }

    trade_log.append(trade_entry)
    _save_trade_log(trade_log)

    # Record with risk manager
    city_code = signal.get("city_code", "")
    risk.record_trade(ticker, side, cost, contracts, city_code)

    return True


def _print_performance(trade_log):
    """Print recent performance summary."""
    if not trade_log:
        return

    recent = trade_log[-20:]
    total_invested = sum(t.get("cost_cents", 0) for t in recent)

    by_strategy = {}
    for t in recent:
        s = t.get("strategy", "unknown")
        if s not in by_strategy:
            by_strategy[s] = {"count": 0, "cost": 0}
        by_strategy[s]["count"] += 1
        by_strategy[s]["cost"] += t.get("cost_cents", 0)

    print(f"\n  ┌─ Performance (last {len(recent)} trades) ──────────")
    print(f"  │  Total invested: ${total_invested/100:.2f}")
    for s, info in by_strategy.items():
        print(f"  │  {s}: {info['count']} trades, ${info['cost']/100:.2f}")
    print(f"  └──────────────────────────────────────────────\n")


def _should_require_approval(signal, risk):
    """Check if a signal needs dashboard approval instead of auto-execution.

    Only require approval when ALL three conditions are met:
      1. Adding this trade would exceed MAX_OPEN_POSITIONS
      2. Edge is exceptionally high (> 28%)
      3. Confirmation is STRONG

    All other trades auto-execute without approval.
    """
    positions = risk.state.get("positions", [])
    would_exceed = len(positions) >= config.MAX_OPEN_POSITIONS
    exceptional_edge = signal.get("edge", 0) > config.HIGH_EDGE_APPROVAL_THRESHOLD
    strong_confirm = signal.get("confirmation_verdict") == "STRONG"
    return would_exceed and exceptional_edge and strong_confirm


def _load_pending():
    try:
        if os.path.exists(config.PENDING_TRADES_FILE):
            with open(config.PENDING_TRADES_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_pending(pending):
    try:
        with open(config.PENDING_TRADES_FILE, "w") as f:
            json.dump(pending, f, indent=2)
    except Exception:
        pass


def _add_pending_trade(signal, market):
    """Queue a high-conviction signal for dashboard approval."""
    pending = _load_pending()
    pending.append({
        "id": str(uuid.uuid4())[:8],
        "timestamp": datetime.now().isoformat(),
        "ticker": signal.get("ticker", market.get("ticker", "")),
        "title": market.get("title", ""),
        "side": signal["side"],
        "price_cents": signal["price_cents"],
        "contracts": signal["suggested_contracts"],
        "cost_cents": signal["price_cents"] * signal["suggested_contracts"],
        "edge": signal.get("edge", 0),
        "confidence": signal.get("confidence", 0),
        "confirmation": signal.get("confirmation_verdict", ""),
        "city_code": signal.get("city_code", ""),
        "reasoning": signal.get("reasoning", ""),
        "strategy": signal.get("strategy", ""),
        "status": "pending",
    })
    _save_pending(pending)


def _process_approved_trades(client, risk, trade_log):
    """Execute trades approved via the dashboard and clean up rejected ones."""
    pending = _load_pending()
    if not pending:
        return

    remaining = []
    executed = 0
    for trade in pending:
        if trade.get("status") == "approved":
            # Build a signal-like dict for _execute_trade
            signal = {
                "ticker": trade["ticker"],
                "side": trade["side"],
                "price_cents": trade["price_cents"],
                "suggested_contracts": trade["contracts"],
                "strategy": trade.get("strategy", ""),
                "edge": trade.get("edge", 0),
                "confidence": trade.get("confidence", 0),
                "confirmation_verdict": trade.get("confirmation", ""),
                "predicted_high": None,
                "city_code": trade.get("city_code", ""),
                "reasoning": trade.get("reasoning", ""),
            }
            market = {"ticker": trade["ticker"], "title": trade.get("title", "")}

            # Re-check risk before executing
            approved, reason = risk.check_trade(signal)
            if approved is True or approved == "NEEDS_APPROVAL":
                print(f"  [APPROVED] Executing: {trade['ticker']} {trade['side'].upper()} x{trade['contracts']}")
                _execute_trade(client, risk, signal, trade_log, market)
                executed += 1
            else:
                print(f"  [APPROVED] Blocked by risk: {trade['ticker']} — {reason}")
        elif trade.get("status") == "rejected":
            print(f"  [REJECTED] {trade['ticker']} — removed from queue")
        else:
            remaining.append(trade)

    if executed > 0 or len(remaining) != len(pending):
        _save_pending(remaining)
        if executed:
            print(f"  [PENDING] Executed {executed} approved trade(s)")


def _write_bot_status(cycle, skip_reasons, trades_this_cycle, strategy, client=None):
    """Write bot status JSON for the dashboard to read."""
    now = datetime.now(tz=timezone.utc)
    model_weights = {}
    for city in config.WEATHER_CITIES:
        model_weights[city] = strategy.quant.get_model_weights(city)

    # Fetch live balance from Kalshi API
    balance_cents = None
    if client and config.API_KEY_ID != "YOUR_API_KEY_ID_HERE":
        try:
            balance_cents = client.get_balance()
        except Exception:
            pass

    status = {
        "cycle": cycle,
        "timestamp": now.isoformat(),
        "next_scan": (now + timedelta(seconds=config.SCAN_INTERVAL)).isoformat(),
        "scan_interval": config.SCAN_INTERVAL,
        "skip_reasons": skip_reasons,
        "model_weights": model_weights,
        "regime": "TRANSITIONAL",  # Updated per-market, show last known
        "trades_this_cycle": trades_this_cycle,
        "environment": config.ENVIRONMENT,
        "dry_run": config.DRY_RUN,
        "balance_cents": balance_cents,
    }
    try:
        with open("bot_status.json", "w") as f:
            json.dump(status, f, indent=2)
    except Exception:
        pass


def _generate_daily_report(trade_log, strategy):
    """
    Generate a daily learning report for the previous day.
    Called at day transition (first cycle of a new day).
    Writes to daily_reports.json.
    """
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    # Filter trades from yesterday
    day_trades = [
        t for t in trade_log
        if t.get("timestamp", "").startswith(yesterday)
    ]

    if not day_trades:
        return  # No trades yesterday, skip report

    # Compute stats
    settled = [t for t in day_trades if t.get("settled")]
    wins = [t for t in settled if t.get("result") == "win"]
    losses = [t for t in settled if t.get("result") == "loss"]
    total_pnl = sum(t.get("profit_cents", 0) for t in settled)
    edges = [t.get("edge", 0) for t in day_trades if t.get("edge")]
    avg_edge = sum(edges) / len(edges) if edges else 0

    # City breakdown
    city_perf = {}
    for t in day_trades:
        city = t.get("city_code", "OTHER")
        if city not in city_perf:
            city_perf[city] = {"trades": 0, "wins": 0, "pnl_cents": 0}
        city_perf[city]["trades"] += 1
        if t.get("result") == "win":
            city_perf[city]["wins"] += 1
        city_perf[city]["pnl_cents"] += t.get("profit_cents", 0)

    # Model accuracy snapshot from quant
    model_accuracy = {}
    if strategy and strategy.quant:
        acc_data = strategy.quant.model_accuracy
        for key, models in acc_data.items():
            for model_name, stats in models.items():
                if model_name not in model_accuracy:
                    model_accuracy[model_name] = {"count": 0, "mse_sum": 0}
                model_accuracy[model_name]["count"] += stats.get("count", 0)
                model_accuracy[model_name]["mse_sum"] += stats.get("mse_sum", 0)

    # Current model weights
    model_weights = {}
    for city in config.WEATHER_CITIES:
        model_weights[city] = strategy.quant.get_model_weights(city)

    report = {
        "date": yesterday,
        "trades_placed": len(day_trades),
        "wins": len(wins),
        "losses": len(losses),
        "total_pnl_cents": total_pnl,
        "avg_edge": round(avg_edge, 4),
        "city_performance": city_perf,
        "model_accuracy": model_accuracy,
        "regime_summary": "TRANSITIONAL",
        "model_weights_after": model_weights,
    }

    # Load existing reports and append
    reports = []
    try:
        if os.path.exists(config.DAILY_REPORTS_FILE):
            with open(config.DAILY_REPORTS_FILE) as f:
                reports = json.load(f)
    except Exception:
        reports = []

    # Don't duplicate if report for this date already exists
    if any(r.get("date") == yesterday for r in reports):
        return

    reports.append(report)
    try:
        with open(config.DAILY_REPORTS_FILE, "w") as f:
            json.dump(reports, f, indent=2)
    except Exception:
        pass

    print(f"  [REPORT] Daily report generated for {yesterday}")
    print(f"           Trades: {len(day_trades)}, W/L: {len(wins)}/{len(losses)}, P&L: ${total_pnl/100:.2f}")


def _load_trade_log():
    try:
        if os.path.exists(config.TRADE_LOG_FILE):
            with open(config.TRADE_LOG_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_trade_log(log):
    try:
        with open(config.TRADE_LOG_FILE, "w") as f:
            json.dump(log, f, indent=2)
    except Exception:
        pass


if __name__ == "__main__":
    main()
