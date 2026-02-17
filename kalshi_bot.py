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


def _check_environment_switch():
    """Detect environment change (e.g. demo → production) and wipe stale state.

    Reads the last-known environment from bot_status.json. If it differs from
    the current config.ENVIRONMENT, all runtime state files are reset so that
    demo positions/trades don't pollute a production session (or vice-versa).
    """
    prev_env = None
    try:
        if os.path.exists(config.BOT_STATUS_FILE):
            with open(config.BOT_STATUS_FILE) as f:
                prev_status = json.load(f)
                prev_env = prev_status.get("environment")
    except Exception:
        pass

    if prev_env is None or prev_env == config.ENVIRONMENT:
        return  # First run or same environment — nothing to do

    print(f"\n  [ENV SWITCH] Detected environment change: {prev_env} → {config.ENVIRONMENT}")
    print(f"  [ENV SWITCH] Wiping stale state files to start clean...\n")

    # Define clean defaults for each state file
    clean_state = {
        config.TRADE_LOG_FILE: [],
        config.RISK_STATE_FILE: {
            "daily_loss_cents": 0,
            "daily_trade_count": 0,
            "last_reset_date": datetime.now().strftime("%Y-%m-%d"),
            "last_trade_time": None,
            "positions": [],
            "consecutive_losses": 0,
            "loss_pause_until": None,
            "city_exposure": {},
            "total_exposure_cents": 0,
        },
        config.PNL_HISTORY_FILE: {
            "trades": [],
            "total_invested_cents": 0,
            "total_returned_cents": 0,
            "total_profit_cents": 0,
            "wins": 0,
            "losses": 0,
        },
        config.PENDING_TRADES_FILE: [],
        config.BOT_STATUS_FILE: {},
    }

    for filepath, default in clean_state.items():
        try:
            with open(filepath, "w") as f:
                json.dump(default, f, indent=2)
            print(f"  [ENV SWITCH] Reset {os.path.basename(filepath)}")
        except Exception as e:
            print(f"  [ENV SWITCH] Failed to reset {os.path.basename(filepath)}: {e}")

    print(f"  [ENV SWITCH] Clean slate ready for {config.ENVIRONMENT}\n")


def main():
    """Main entry point."""

    # ─── START DASHBOARD FIRST ───
    # Must bind to PORT before anything else so Railway health checks pass.
    # Runs in a non-daemon thread so it survives if the bot crashes.
    from dashboard import start_dashboard_server
    port = int(os.environ.get("PORT", 8050))
    start_dashboard_server(port)

    # ─── CHECK FOR ENVIRONMENT SWITCH ───
    _check_environment_switch()

    # ─── STARTUP BANNER ───
    print()
    print("  " + "=" * 54)
    print("  |     KALSHI WEATHER BOT v3.0                      |")
    print("  |     Ensemble Forecast × Arbitrage Engine         |")
    print("  " + "=" * 54)
    print()

    # Show mode
    if config.DRY_RUN:
        print("  MODE: [DRY RUN] Analysis only, no orders")
    elif config.ENVIRONMENT == "demo":
        print("  MODE: [DEMO] Practice money, real orders")
    else:
        print("  MODE: [PRODUCTION] REAL MONEY")
        print("  WARNING: Trades will use REAL FUNDS")
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

    # ─── RECONCILE WITH EXCHANGE ───
    _reconcile_positions(client, risk)

    # ─── SYNC BALANCE ───
    _sync_balance(client, risk, strategy)

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
    active_markets = [k.upper() for k, v in config.MARKET_TYPES.items() if v]
    print(f"  Active markets: {', '.join(active_markets)}")
    if config.MARKET_TYPES.get("weather"):
        print(f"  Weather cities: {', '.join(config.WEATHER_CITIES)}")
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
            # KILL SWITCH CHECK
            # ═══════════════════════════════════════════════
            observation_mode, obs_reason = risk.check_kill_switch(trade_log)
            if observation_mode:
                print(f"\n  [OBSERVATION] Kill switch active: {obs_reason}")
                print(f"  [OBSERVATION] Bot will scan and log but NOT trade")

            # ═══════════════════════════════════════════════
            # SYNC POSITIONS & BALANCE WITH KALSHI
            # ═══════════════════════════════════════════════
            _reconcile_positions(client, risk)
            _sync_balance(client, risk, strategy)

            # Log settlement status
            breakdown = risk.get_exposure_breakdown()
            if breakdown["pending_positions"] > 0:
                print(f"  [SETTLEMENT] {breakdown['pending_positions']} positions pending settlement "
                      f"(${breakdown['pending_settlement_cents']/100:.2f} locked)")
                if breakdown["is_pre_settlement"]:
                    print(f"  [SETTLEMENT] Pre-settlement window — sizing reduced to {config.PRE_SETTLEMENT_SIZING_MULT:.0%}")

            # ═══════════════════════════════════════════════
            # STEP 0: CHECK SETTLEMENTS & EXITS
            # ═══════════════════════════════════════════════
            print("\n  [STEP 0a] Checking settlements...")
            settled = intel.check_settlements(trade_log, risk, quant=strategy.quant)
            if settled:
                print(f"  → {len(settled)} trades settled")
                _save_trade_log(trade_log)

            # Sync P&L from Kalshi AFTER check_settlements so it overwrites
            # the internal tracking with ground truth from the exchange.
            _sync_pnl_from_kalshi(client, intel)
            intel.print_pnl()

            # Daily report + analysis on day change
            today_str = datetime.now().strftime("%Y-%m-%d")
            if today_str != last_report_date:
                _generate_daily_report(trade_log, strategy)
                _run_daily_analysis(trade_log)
                last_report_date = today_str

            # Check for dashboard-approved pending trades
            _process_approved_trades(client, risk, trade_log)

            print("  [STEP 0b] Checking intraday temperatures...")
            intel.print_intraday_temps()

            print("  [STEP 0c] Portfolio review...")
            if config.PORTFOLIO_REVIEW_ENABLED:
                reviews = intel.review_portfolio(
                    risk.state.get("positions", []),
                    weather_engine=strategy.weather,
                    volatility_engine=strategy.vol_engine,
                    signal_confirmer=strategy.confirmer,
                    spx_confirmer=strategy.spx_confirmer,
                )
                for review in reviews:
                    _process_portfolio_action(client, risk, trade_log, review)
                # Batch save after all reviews (persists last_review* fields)
                if reviews:
                    risk._save_state()
            else:
                exits = intel.check_exits(risk.state.get("positions", []), strategy.weather)
                for exit_rec in exits:
                    print(f"  ⚡ EXIT: {exit_rec['ticker']} — {exit_rec['reason']}")
                    if config.DRY_RUN:
                        print(f"    [DRY RUN] Would exit {exit_rec['ticker']} ({exit_rec['urgency']} urgency)")
                        continue
                    should_exit = exit_rec["urgency"] == "high" or (
                        exit_rec["urgency"] == "medium" and not sys.stdin.isatty()
                    )
                    if should_exit:
                        _execute_exit(client, risk, trade_log, exit_rec)
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

                    # Kill switch: log but don't execute
                    if observation_mode:
                        cost = signal["price_cents"] * signal["suggested_contracts"]
                        print(f"    [OBSERVATION] Would trade {signal['suggested_contracts']}x {signal['side'].upper()} @ {signal['price_cents']}c = ${cost/100:.2f} but kill switch active")
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

                    # Pre-settlement sizing reduction
                    if risk.is_pre_settlement_window():
                        orig = signal["suggested_contracts"]
                        signal["suggested_contracts"] = max(1, int(orig * config.PRE_SETTLEMENT_SIZING_MULT))
                        if signal["suggested_contracts"] < orig:
                            print(f"    [SETTLE] Reduced {orig} → {signal['suggested_contracts']} contracts (pre-settlement)")

                    # Execute the trade
                    trade_result = _execute_trade(
                        client, risk, signal, trade_log, market
                    )

                    if trade_result:
                        trade_count_this_cycle += 1

            # ═══════════════════════════════════════════════
            # STEP 1b: SCAN S&P 500 BRACKET MARKETS
            # ═══════════════════════════════════════════════
            if config.MARKET_TYPES.get("sp500"):
                print("\n  [STEP 1b] Scanning S&P 500 bracket markets...")
                sp500_markets = scanner.scan_sp500_markets()

                if sp500_markets:
                    scanner.print_sp500_summary(sp500_markets)

                    for market in sp500_markets:
                        ticker = market.get("ticker", "")
                        title = market.get("title", "")

                        signal = strategy.evaluate_market(market)

                        if signal["signal"] == "skip":
                            reason = signal.get("reasoning") or "Dead market"
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

                        print(f"\n  [SP500] Evaluating: {ticker}")
                        print(f"          {title}")

                        print(f"\n  ★ SP500 SIGNAL ★")
                        print(f"    Strategy:    {signal.get('strategy', '?')}")
                        print(f"    Side:        {signal['side'].upper()}")
                        print(f"    Edge:        {signal['edge']:.1%}")
                        print(f"    Confidence:  {signal['confidence']:.1%}")
                        print(f"    Price:       {signal['price_cents']}c")
                        print(f"    Contracts:   {signal['suggested_contracts']}")
                        print(f"    Confirm:     {signal.get('confirmation_verdict', 'N/A')}")
                        print(f"    Reasoning:   {signal['reasoning']}")

                        # Use "SP500" as city_code for risk manager per-market tracking
                        signal["city_code"] = "SP500"
                        signal["market_type"] = "sp500"

                        if market.get("close_time"):
                            signal["close_time"] = market["close_time"]

                        approved, reason = risk.check_trade(signal)

                        if approved is False:
                            print(f"    x BLOCKED: {reason}")
                            continue

                        if observation_mode:
                            cost = signal["price_cents"] * signal["suggested_contracts"]
                            print(f"    [OBSERVATION] Would trade {signal['suggested_contracts']}x {signal['side'].upper()} @ {signal['price_cents']}c = ${cost/100:.2f} but kill switch active")
                            continue

                        if approved == "NEEDS_APPROVAL":
                            print(f"    ! {reason}")
                            if sys.stdin.isatty():
                                user_input = input("    Approve? (y/n): ").strip().lower()
                                if user_input != "y":
                                    print("    → Skipped by user")
                                    continue
                            else:
                                _add_pending_trade(signal, market)
                                print(f"    QUEUED for dashboard approval")
                                continue

                        if _should_require_approval(signal, risk):
                            _add_pending_trade(signal, market)
                            print(f"    QUEUED for dashboard approval (SP500)")
                            continue

                        # Pre-settlement sizing reduction
                        if risk.is_pre_settlement_window():
                            orig = signal["suggested_contracts"]
                            signal["suggested_contracts"] = max(1, int(orig * config.PRE_SETTLEMENT_SIZING_MULT))
                            if signal["suggested_contracts"] < orig:
                                print(f"    [SETTLE] Reduced {orig} → {signal['suggested_contracts']} contracts (pre-settlement)")

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
            _write_bot_status(cycle, skip_reasons, trade_count_this_cycle, strategy, client,
                              observation_mode=observation_mode, observation_reason=obs_reason,
                              risk_manager=risk)

            # Wait for next cycle — use faster scanning during peak temperature hours
            et_hour = (datetime.now(timezone.utc).hour - 5) % 24
            if config.PEAK_SCAN_START_ET <= et_hour < config.PEAK_SCAN_END_ET:
                scan_interval = config.PEAK_SCAN_INTERVAL
                print(f"  [PEAK] Afternoon peak hours ({config.PEAK_SCAN_START_ET}:00-{config.PEAK_SCAN_END_ET}:00 ET) — scanning every {scan_interval}s")
            else:
                scan_interval = config.SCAN_INTERVAL
                print(f"  Next scan in {scan_interval // 60} minutes...")
            time.sleep(scan_interval)

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

    # Calculate spread at entry
    yes_ask = market.get("yes_ask", 0) or 0
    yes_bid = market.get("yes_bid", 0) or 0
    spread_at_entry = (yes_ask - yes_bid) if (yes_ask > 0 and yes_bid > 0) else 0

    # Build human-readable description from ticker
    market_description = _build_market_description(ticker, signal.get("city_code", ""), market.get("title", ""))

    # Expected profit: edge * contracts * 100 (simplified expected value in cents)
    edge = signal.get("edge", 0)
    expected_profit_cents = round(edge * contracts * 100)

    # Record trade
    trade_entry = {
        "timestamp": datetime.now().isoformat(),
        "ticker": ticker,
        "title": market.get("title", ""),
        "market_description": market_description,
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
        "spread_at_entry_cents": spread_at_entry,
        "expected_profit_cents": expected_profit_cents,
    }

    trade_log.append(trade_entry)
    _save_trade_log(trade_log)

    # Record with risk manager (include extra fields for dashboard)
    city_code = signal.get("city_code", "")
    risk.record_trade(ticker, side, cost, contracts, city_code,
                      title=market.get("title", ""),
                      edge=signal.get("edge", 0),
                      expected_profit_cents=expected_profit_cents,
                      market_description=market_description)

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


def _write_bot_status(cycle, skip_reasons, trades_this_cycle, strategy, client=None,
                      observation_mode=False, observation_reason="", risk_manager=None):
    """Write bot status JSON for the dashboard to read."""
    now = datetime.now(tz=timezone.utc)
    model_weights = {}
    for city in config.WEATHER_CITIES:
        model_weights[city] = strategy.quant.get_model_weights(city)

    # Calculate total expected profit from open positions
    risk_state = {}
    try:
        if os.path.exists(config.RISK_STATE_FILE):
            with open(config.RISK_STATE_FILE) as f:
                risk_state = json.load(f)
    except Exception:
        pass
    positions = risk_state.get("positions", [])
    total_expected_profit_cents = sum(p.get("expected_profit_cents", 0) for p in positions)

    # Fetch live balance from Kalshi API
    balance_cents = None
    portfolio_value_cents = None
    if client and config.API_KEY_ID != "YOUR_API_KEY_ID_HERE":
        try:
            bal_resp = client.get_balance()
            if bal_resp and isinstance(bal_resp, dict):
                balance_cents = bal_resp.get("balance")
                portfolio_value_cents = bal_resp.get("portfolio_value")
        except Exception:
            pass

    # Exposure breakdown from risk manager
    exposure_breakdown = {}
    if risk_manager:
        try:
            exposure_breakdown = risk_manager.get_exposure_breakdown()
        except Exception:
            pass

    # Seasonal confidence multipliers for dashboard
    seasonal_confidence = {}
    try:
        from seasonal_confidence import get_all_multipliers
        seasonal_confidence = get_all_multipliers()
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
        "portfolio_value_cents": portfolio_value_cents,
        "observation_mode": observation_mode,
        "observation_reason": observation_reason,
        "total_expected_profit_cents": total_expected_profit_cents,
        "active_markets": [k for k, v in config.MARKET_TYPES.items() if v],
        "exposure_breakdown": exposure_breakdown,
        "seasonal_confidence": seasonal_confidence,
    }
    try:
        with open(config.BOT_STATUS_FILE, "w") as f:
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


def _run_daily_analysis(trade_log):
    """Run end-of-day trade analysis for yesterday's settled trades."""
    try:
        from trade_analyzer import TradeAnalyzer
        analyzer = TradeAnalyzer()
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        result = analyzer.run_daily_analysis(trade_log, yesterday)

        if not result:
            print(f"  [ANALYSIS] No settled trades to analyze for {yesterday}")
            return

        summary = result["summary"]
        patterns = result["patterns"]
        print(f"\n  ┌─ End-of-Day Analysis ({yesterday}) ─────────────────")
        print(f"  │  Trades: {summary['total_trades']}  W/L: {summary['wins']}/{summary['losses']}  "
              f"P&L: ${summary['total_pnl_cents']/100:+.2f}")
        if summary['avg_edge_winners']:
            print(f"  │  Avg edge winners: {summary['avg_edge_winners']:.1%}  "
                  f"losers: {summary['avg_edge_losers']:.1%}")
        if patterns.get("guards_would_have_caught"):
            print(f"  │  Guards would have caught {patterns['guards_would_have_caught']} loser(s) "
                  f"(${patterns['savings_cents']/100:.2f} saved)")
        if patterns.get("best_model"):
            print(f"  │  Best model: {patterns['best_model']}  "
                  f"Worst: {patterns['worst_model']}")
        print(f"  │")

        # Print lessons for losers
        for ta in result.get("trade_analyses", []):
            if ta["result"] in ("loss", "exit_loss") and ta.get("lessons"):
                print(f"  │  {ta['ticker']}:")
                for lesson in ta["lessons"]:
                    print(f"  │    - {lesson}")

        print(f"  └──────────────────────────────────────────────────\n")

    except Exception as e:
        print(f"  [ANALYSIS] Error running daily analysis: {e}")


def _execute_exit(client, risk, trade_log, exit_rec):
    """Execute a position exit (sell order) on Kalshi."""
    ticker = exit_rec["ticker"]
    current_price = exit_rec.get("current_price")

    # Find the matching position to get side and contracts
    position = None
    for p in risk.state.get("positions", []):
        if p.get("ticker") == ticker:
            position = p
            break

    if not position:
        print(f"    ✗ EXIT: Position {ticker} not found in risk state")
        return

    side = position.get("side", "yes")
    contracts = position.get("contracts", 1)
    cost_cents = position.get("cost_cents", 0)
    city_code = position.get("city_code", "")

    # Use market order for high urgency, limit for medium
    if exit_rec["urgency"] == "high":
        order_type = "market"
        price_kwargs = {}
        print(f"    → Selling {contracts}x {side.upper()} @ MARKET (high urgency)")
    else:
        order_type = "limit"
        sell_price = current_price if current_price else 50
        price_kwargs = {"yes_price": sell_price} if side == "yes" else {"no_price": sell_price}
        print(f"    → Selling {contracts}x {side.upper()} @ {sell_price}c LIMIT")

    try:
        result = client.sell_order(
            ticker=ticker,
            side=side,
            count=contracts,
            type=order_type,
            **price_kwargs,
        )
        if result:
            order_id = result.get("order", {}).get("order_id", "OK")
            print(f"    ✓ Exit order submitted: {order_id}")

            # Calculate approximate P&L
            sell_value = (current_price or 50) * contracts
            profit_cents = sell_value - cost_cents

            # Update trade log — mark the original trade as exited
            for trade in trade_log:
                if trade.get("ticker") == ticker and not trade.get("settled"):
                    trade["settled"] = True
                    trade["result"] = "exit_win" if profit_cents > 0 else "exit_loss"
                    trade["profit_cents"] = profit_cents
                    trade["exit_reason"] = exit_rec.get("reason", "")
                    trade["exit_price_cents"] = current_price
                    break
            _save_trade_log(trade_log)

            # Release exposure in risk manager
            risk.release_exposure(ticker, cost_cents, city_code)
            if profit_cents > 0:
                risk.record_win(profit_cents)
            else:
                risk.record_loss(abs(profit_cents))

            print(f"    P&L: {'+'if profit_cents>=0 else ''}${profit_cents/100:.2f}")
        else:
            print(f"    ✗ Exit order returned no result")
    except Exception as e:
        print(f"    ✗ Exit order failed: {e}")


def _process_portfolio_action(client, risk, trade_log, review):
    """Process a portfolio review recommendation."""
    ticker = review["ticker"]
    action = review["action"]

    # Persist review data on the matching position for the dashboard
    for p in risk.state.get("positions", []):
        if p.get("ticker") == ticker:
            p["last_review"] = review.get("review_detail", {})
            p["last_review_action"] = action
            p["last_review_reason"] = review.get("reason", "")
            break

    if action == "hold":
        if review.get("urgency") == "medium":
            print(f"  [REVIEW] HOLD {ticker} — {review['reason']}")
        return

    print(f"  [REVIEW] {action.upper()} {ticker} — {review['reason']}")

    if action == "pare":
        _execute_partial_sell(client, risk, trade_log, review)
    elif action == "full_exit":
        exit_rec = {
            "ticker": ticker,
            "reason": review["reason"],
            "action": "sell",
            "urgency": review["urgency"],
            "current_price": review["current_price"],
        }
        _execute_exit(client, risk, trade_log, exit_rec)
    elif action == "hedge":
        _execute_hedge(client, risk, trade_log, review)


def _execute_partial_sell(client, risk, trade_log, review):
    """Sell a portion of a position to reduce exposure."""
    ticker = review["ticker"]
    sell_count = review.get("sell_contracts", 1)
    current_price = review.get("current_price", 50)

    # Find position
    position = None
    for p in risk.state.get("positions", []):
        if p.get("ticker") == ticker:
            position = p
            break
    if not position:
        print(f"    Position {ticker} not found")
        return

    side = position["side"]
    total_contracts = position["contracts"]

    if config.DRY_RUN:
        print(f"    [DRY RUN] Would PARE {ticker}: sell {sell_count}/{total_contracts} {side.upper()} @ ~{current_price}c")
        return

    price_kwargs = {"yes_price": current_price} if side == "yes" else {"no_price": current_price}
    try:
        result = client.sell_order(
            ticker=ticker, side=side, count=sell_count,
            type="limit", **price_kwargs,
        )
        if result:
            order_id = result.get("order", {}).get("order_id", "OK")
            print(f"    ✓ Partial sell submitted: {sell_count}/{total_contracts} — {order_id}")

            cost_per_contract = position["cost_cents"] / max(total_contracts, 1)
            released_cost = int(cost_per_contract * sell_count)
            sell_value = current_price * sell_count
            partial_pnl = sell_value - released_cost

            risk.reduce_position(ticker, sell_count, released_cost)

            # Log the partial exit
            trade_log.append({
                "timestamp": datetime.now().isoformat(),
                "ticker": ticker,
                "side": side,
                "price_cents": current_price,
                "contracts": sell_count,
                "cost_cents": released_cost,
                "strategy": "PARE",
                "edge": review.get("new_edge", 0),
                "city_code": review.get("city_code", ""),
                "status": "partial_exit",
                "reasoning": review.get("reason", ""),
                "settled": True,
                "result": "exit_win" if partial_pnl > 0 else "exit_loss",
                "profit_cents": partial_pnl,
            })
            _save_trade_log(trade_log)

            if partial_pnl > 0:
                risk.record_win(partial_pnl)
            else:
                risk.record_loss(abs(partial_pnl))

            print(f"    P&L: {'+'if partial_pnl>=0 else ''}${partial_pnl/100:.2f} (kept {total_contracts - sell_count} contracts)")
        else:
            print(f"    ✗ Partial sell returned no result")
    except Exception as e:
        print(f"    ✗ Partial sell failed: {e}")


def _execute_hedge(client, risk, trade_log, review):
    """Exit a position by buying the opposite side (better liquidity path).
    On Kalshi, buying the opposite side when you hold a position closes it."""
    ticker = review["ticker"]
    hedge_side = review.get("hedge_side", "")
    hedge_price = review.get("hedge_price", 50)
    hedge_contracts = review.get("hedge_contracts", 1)
    original_side = review.get("side", "")
    city_code = review.get("city_code", "")
    cost_cents = review.get("cost_cents", 0)
    contracts = review.get("contracts", 1)

    if config.DRY_RUN:
        print(f"    [DRY RUN] Would HEDGE {ticker}: buy {hedge_contracts}x {hedge_side.upper()} @ {hedge_price}c")
        return

    try:
        result = client.create_order(
            ticker=ticker, side=hedge_side, count=hedge_contracts,
            type="limit",
            yes_price=hedge_price if hedge_side == "yes" else None,
            no_price=hedge_price if hedge_side == "no" else None,
        )
        if result:
            order_id = result.get("order", {}).get("order_id", "OK")
            print(f"    ✓ Hedge order submitted: {hedge_contracts}x {hedge_side.upper()} @ {hedge_price}c — {order_id}")

            # The hedge locks in value: we pay hedge_price per contract,
            # but one side is guaranteed to pay out 100c.
            # Net P&L per contract = 100 - entry_price_per_contract - hedge_price
            entry_per_contract = cost_cents / max(contracts, 1)
            net_per_contract = 100 - entry_per_contract - hedge_price
            total_pnl = int(net_per_contract * hedge_contracts)

            # Update risk state
            if hedge_contracts >= contracts:
                # Full hedge = position fully closed
                risk.release_exposure(ticker, cost_cents, city_code)
            else:
                # Partial hedge
                released_cost = int(entry_per_contract * hedge_contracts)
                risk.reduce_position(ticker, hedge_contracts, released_cost)

            # Log the hedge exit
            trade_log.append({
                "timestamp": datetime.now().isoformat(),
                "ticker": ticker,
                "side": hedge_side,
                "price_cents": hedge_price,
                "contracts": hedge_contracts,
                "cost_cents": hedge_price * hedge_contracts,
                "strategy": "HEDGE",
                "edge": review.get("new_edge", 0),
                "city_code": city_code,
                "status": "hedge_exit",
                "reasoning": review.get("reason", ""),
                "settled": True,
                "result": "exit_win" if total_pnl > 0 else "exit_loss",
                "profit_cents": total_pnl,
            })
            _save_trade_log(trade_log)

            if total_pnl > 0:
                risk.record_win(total_pnl)
            else:
                risk.record_loss(abs(total_pnl))

            print(f"    P&L: {'+'if total_pnl>=0 else ''}${total_pnl/100:.2f}")
        else:
            print(f"    ✗ Hedge order returned no result — falling back to direct sell")
            exit_rec = {
                "ticker": ticker, "reason": review["reason"],
                "action": "sell", "urgency": "high",
                "current_price": review["current_price"],
            }
            _execute_exit(client, risk, trade_log, exit_rec)
    except Exception as e:
        print(f"    ✗ Hedge order failed: {e} — falling back to direct sell")
        exit_rec = {
            "ticker": ticker, "reason": review["reason"],
            "action": "sell", "urgency": "high",
            "current_price": review["current_price"],
        }
        _execute_exit(client, risk, trade_log, exit_rec)


def _build_market_description(ticker, city_code, title):
    """Build a human-readable description from ticker."""
    city_names = {
        "NYC": "NYC", "CHI": "Chicago", "MIA": "Miami", "AUS": "Austin",
        "LAX": "LA", "DEN": "Denver", "PHI": "Philly", "ATL": "Atlanta",
        "BOS": "Boston", "DAL": "Dallas", "DC": "DC", "HOU": "Houston",
        "LV": "Vegas", "MIN": "Minneapolis", "NOLA": "NOLA", "OKC": "OKC",
        "PHX": "Phoenix", "SATX": "San Antonio", "SEA": "Seattle", "SFO": "SFO",
        "SP500": "S&P 500",
    }
    months = {"JAN":"Jan","FEB":"Feb","MAR":"Mar","APR":"Apr","MAY":"May","JUN":"Jun",
              "JUL":"Jul","AUG":"Aug","SEP":"Sep","OCT":"Oct","NOV":"Nov","DEC":"Dec"}
    try:
        parts = ticker.split("-")
        if len(parts) >= 3:
            date_str = parts[1]  # e.g. 26FEB15
            bucket = parts[2]    # e.g. B42.5 or B5600
            month = months.get(date_str[2:5].upper(), "")
            day = date_str[5:7].lstrip("0")

            # S&P 500 bracket
            if ticker.startswith(config.SP500_SERIES_TICKER.upper()) or city_code == "SP500":
                if bucket.startswith("B"):
                    price = bucket[1:]
                    return f"S&P {price}+ {month} {day}"
                elif bucket.startswith("T"):
                    price = bucket[1:]
                    return f"S&P {price} {month} {day}"
                return f"S&P {bucket} {month} {day}"

            # Weather market
            city_name = city_names.get(city_code, city_code)
            if bucket.startswith("B"):
                temp = bucket[1:]
                return f"{city_name} {temp}+F {month} {day}"
            elif bucket.startswith("T"):
                temp = bucket[1:]
                return f"{city_name} {temp}F {month} {day}"
            return f"{city_name} {bucket} {month} {day}"
    except Exception:
        pass
    return title or ticker


_TICKER_TO_CITY = {
    "KXHIGHNY": "NYC",
    "KXHIGHCHI": "CHI",
    "KXHIGHMIA": "MIA",
    "KXHIGHAUS": "AUS",
    "KXHIGHLAX": "LAX",
    "KXHIGHDEN": "DEN",
    "KXHIGHPHIL": "PHI",
    "KXHIGHTATL": "ATL",
    "KXHIGHTBOS": "BOS",
    "KXHIGHTDAL": "DAL",
    "KXHIGHTDC": "DC",
    "KXHIGHTHOU": "HOU",
    "KXHIGHTLV": "LV",
    "KXHIGHTMIN": "MIN",
    "KXHIGHTNOLA": "NOLA",
    "KXHIGHTOKC": "OKC",
    "KXHIGHTPHX": "PHX",
    "KXHIGHTSATX": "SATX",
    "KXHIGHTSEA": "SEA",
    "KXHIGHTSFO": "SFO",
}


def _city_from_ticker(ticker):
    """Extract city code from a Kalshi weather ticker like KXHIGHNY-26FEB16-B41.5."""
    if not ticker:
        return ""
    prefix = ticker.split("-")[0] if "-" in ticker else ticker
    if prefix.startswith("KXINX"):
        return "SP500"
    return _TICKER_TO_CITY.get(prefix, "")


def _sync_balance(client, risk, strategy):
    """Fetch current Kalshi balance and pass it to risk manager and strategy for dynamic sizing."""
    if config.API_KEY_ID == "YOUR_API_KEY_ID_HERE" or config.DRY_RUN:
        return
    try:
        bal_resp = client.get_balance()
        if bal_resp and isinstance(bal_resp, dict):
            balance_cents = bal_resp.get("balance")
            if balance_cents is not None:
                risk.update_balance(balance_cents)
                strategy.balance_cents = balance_cents
    except Exception:
        pass


def _sync_pnl_from_kalshi(client, intel=None):
    """Compute realized P&L from actual Kalshi fills and settlements.

    Only counts P&L from CLOSED tickers (settled markets or early exits).
    Open positions are excluded — their buy cost is not a realized loss.

    Per-ticker P&L = (sell fill revenue + settlement revenue) - buy fill cost
    A ticker is "closed" if it has a settlement OR sell fills.

    This is the source of truth — it replaces any internal P&L tracking.
    Must run AFTER check_settlements() so it overwrites the file last.
    """
    if config.API_KEY_ID == "YOUR_API_KEY_ID_HERE" or config.DRY_RUN:
        return

    try:
        # Fetch all fills (paginated)
        all_fills = []
        cursor = None
        while True:
            result = client.get_fills(limit=200, cursor=cursor)
            if not result:
                break
            fills = result.get("fills", [])
            all_fills.extend(fills)
            cursor = result.get("cursor")
            if not cursor or not fills:
                break

        # Fetch all settlements (paginated)
        all_settlements = []
        cursor = None
        while True:
            result = client.get_settlements(limit=200, cursor=cursor)
            if not result:
                break
            settlements = result.get("settlements", [])
            all_settlements.extend(settlements)
            cursor = result.get("cursor")
            if not cursor or not settlements:
                break

        # Group fills by ticker
        ticker_flows = {}  # ticker → {buy_cost, sell_revenue}
        for fill in all_fills:
            ticker = fill.get("ticker", "")
            if not ticker:
                continue
            if ticker not in ticker_flows:
                ticker_flows[ticker] = {"buy_cost": 0, "sell_revenue": 0}

            action = fill.get("action", "")
            side = fill.get("side", "")
            count = fill.get("count", 0)
            price = fill.get("yes_price", 0) if side == "yes" else fill.get("no_price", 0)

            if action == "buy":
                ticker_flows[ticker]["buy_cost"] += price * count
            elif action == "sell":
                ticker_flows[ticker]["sell_revenue"] += price * count

        # Build settlement revenue lookup
        settle_rev = {}  # ticker → revenue
        for s in all_settlements:
            ticker = s.get("ticker", "")
            if ticker:
                settle_rev[ticker] = (s.get("revenue", 0) or 0)

        # Compute realized P&L: only for tickers that are closed
        # (have a settlement or have sell fills)
        settled_tickers = set(settle_rev.keys())
        total_invested = 0
        total_returned = 0
        wins = 0
        losses = 0

        for ticker, flows in ticker_flows.items():
            has_settlement = ticker in settled_tickers
            has_sells = flows["sell_revenue"] > 0

            if not has_settlement and not has_sells:
                continue  # Open position — skip

            buy_cost = flows["buy_cost"]
            sell_revenue = flows["sell_revenue"]
            settlement_revenue = settle_rev.pop(ticker, 0)

            ticker_pnl = sell_revenue + settlement_revenue - buy_cost
            total_invested += buy_cost
            total_returned += sell_revenue + settlement_revenue

            if ticker_pnl > 0:
                wins += 1
            elif buy_cost > 0:
                losses += 1

        # Include any settlements we didn't have fills for (edge case)
        for ticker, revenue in settle_rev.items():
            if revenue > 0:
                total_returned += revenue
                wins += 1

        total_pnl = total_returned - total_invested

        # Update pnl_history.json with Kalshi ground truth
        pnl_data = {
            "trades": [],
            "total_invested_cents": total_invested,
            "total_returned_cents": total_returned,
            "total_profit_cents": total_pnl,
            "wins": wins,
            "losses": losses,
            "kalshi_synced": True,
            "kalshi_fills": len(all_fills),
            "kalshi_settlements": len(all_settlements),
            "last_sync": datetime.now(timezone.utc).isoformat(),
        }

        with open(config.PNL_HISTORY_FILE, "w") as f:
            json.dump(pnl_data, f, indent=2)

        # Also update TradeIntelligence in-memory state so print_pnl()
        # and future check_settlements() calls start from Kalshi truth.
        if intel:
            intel.pnl_data = pnl_data

        print(f"  [P&L SYNC] Kalshi: {len(all_fills)} fills, {len(all_settlements)} settlements → "
              f"P&L ${total_pnl/100:+.2f} ({wins}W/{losses}L)")

    except Exception as e:
        print(f"  [P&L SYNC] Error: {e}")


def _reconcile_positions(client, risk):
    """Full sync: replace local positions with Kalshi's actual positions.

    Kalshi is the source of truth. This corrects any drift from state
    wipes, failed tracking, or missed trades.
    """
    if config.API_KEY_ID == "YOUR_API_KEY_ID_HERE" or config.DRY_RUN:
        return

    print("  [SYNC] Syncing positions with Kalshi exchange...")
    try:
        result = client.get_positions()
        if result is None:
            print("  [SYNC] Could not fetch positions — skipping")
            return

        api_positions = result.get("market_positions", [])

        # Build local lookup for preserving metadata
        local_lookup = {}
        for p in risk.state.get("positions", []):
            key = (p.get("ticker", ""), p.get("side", ""))
            local_lookup[key] = p

        # Replace local positions entirely from Kalshi data
        new_positions = []
        for pos in api_positions:
            ticker = pos.get("ticker", "")
            position_count = pos.get("position", 0)
            if not ticker or position_count == 0:
                continue

            side = "yes" if position_count > 0 else "no"
            contracts = abs(position_count)
            cost_cents = abs(pos.get("market_exposure", 0))
            city_code = _city_from_ticker(ticker)

            # Preserve local metadata (title, edge, etc.) if we have it
            local = local_lookup.get((ticker, side), {})

            pos_entry = {
                "ticker": ticker,
                "side": side,
                "cost_cents": cost_cents,
                "contracts": contracts,
                "city_code": city_code,
                "timestamp": local.get("timestamp", datetime.now().isoformat()),
                "title": local.get("title", ""),
                "edge": local.get("edge", 0),
                "expected_profit_cents": local.get("expected_profit_cents", 0),
                "market_description": local.get("market_description", ""),
            }
            # Preserve review metadata from previous cycles
            if local.get("last_review"):
                pos_entry["last_review"] = local["last_review"]
                pos_entry["last_review_action"] = local.get("last_review_action", "")
                pos_entry["last_review_reason"] = local.get("last_review_reason", "")
            new_positions.append(pos_entry)

        old_count = len(risk.state.get("positions", []))
        old_tickers = {p.get("ticker") for p in risk.state.get("positions", [])}
        new_tickers = {p["ticker"] for p in new_positions}

        # Log changes
        added = new_tickers - old_tickers
        removed = old_tickers - new_tickers
        for t in added:
            p = next(x for x in new_positions if x["ticker"] == t)
            print(f"  [SYNC] +Added: {t} {p['side'].upper()} x{p['contracts']} (${p['cost_cents']/100:.2f})")
        for t in removed:
            print(f"  [SYNC] -Removed stale: {t}")
        for p in new_positions:
            if p["ticker"] in old_tickers:
                old_p = local_lookup.get((p["ticker"], p["side"]))
                if old_p and (old_p.get("contracts") != p["contracts"] or old_p.get("cost_cents") != p["cost_cents"]):
                    print(f"  [SYNC] ~Updated: {p['ticker']} {p['side'].upper()} "
                          f"x{old_p.get('contracts')}→x{p['contracts']} "
                          f"${old_p.get('cost_cents',0)/100:.2f}→${p['cost_cents']/100:.2f}")

        # Replace positions and recalculate exposure
        risk.state["positions"] = new_positions
        risk.state["city_exposure"] = {}
        for p in new_positions:
            city = p.get("city_code", "")
            if city:
                risk.state["city_exposure"][city] = risk.state["city_exposure"].get(city, 0) + p.get("cost_cents", 0)

        risk._save_state()
        print(f"  [SYNC] Done: {len(new_positions)} positions "
              f"(was {old_count}, +{len(added)} -{len(removed)})")

    except Exception as e:
        print(f"  [SYNC] Error: {e}")


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
