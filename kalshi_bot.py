"""
KALSHI BOT v4.0
=================
Main loop. Scans weather markets, evaluates edge, places limit orders.
Rebuilt from scratch. ~400 lines (was 3,130).

Usage:
  python kalshi_bot.py
"""

import json
import os
import sys
import time
import traceback as _tb
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import config
from kalshi_client import KalshiClient
from market_scanner import MarketScanner
from strategy import Strategy
from risk_manager_v2 import RiskManager
from trade_intelligence import TradeIntelligence
from maker_strategy import MakerStrategy
from trade_reviewer import TradeReviewer


def main(shutdown_event=None):
    """Main bot loop."""
    print()
    print("=" * 60)
    env = config.ENVIRONMENT
    dry = config.DRY_RUN
    print("  KALSHI BOT v4.0 - Weather + Arbitrage")
    print("  Environment: %s | DRY_RUN: %s" % (env, dry))
    print("=" * 60)
    print()

    # Initialize components
    client = KalshiClient()
    scanner = MarketScanner(kalshi_client=client)
    reviewer = TradeReviewer()
    strategy = Strategy(kalshi_client=client, reviewer=reviewer)
    risk = RiskManager(kalshi_client=client)
    intel = TradeIntelligence(kalshi_client=client, weather_engine=strategy.weather)
    maker = MakerStrategy(kalshi_client=client, risk_manager=risk)

    # Pass client to dashboard if running
    try:
        import dashboard
        if hasattr(dashboard, "set_kalshi_client"):
            dashboard.set_kalshi_client(client)
        if hasattr(dashboard, "set_trade_reviewer"):
            dashboard.set_trade_reviewer(reviewer)
    except Exception:
        pass

    cycle = 0
    while True:
        if shutdown_event and shutdown_event.is_set():
            break
        cycle += 1
        try:
            cycle_start = time.time()
            now_et = datetime.now(ZoneInfo("America/New_York"))
            hour_et = now_et.hour

            print()
            print("-" * 50)
            ts = now_et.strftime("%Y-%m-%d %H:%M:%S")
            _fetch_start = getattr(config, "OPEN_METEO_FETCH_START_ET", 8)
            _fetch_end = getattr(config, "OPEN_METEO_FETCH_END_ET", 18)
            _in_window = _fetch_start <= hour_et < _fetch_end
            _window_tag = "" if _in_window else " | OUTSIDE FETCH WINDOW"
            print("  Cycle %d | %s ET%s" % (cycle, ts, _window_tag))
            print("-" * 50)

            # Update strategy balance from risk manager (single source)
            balance = risk._get_balance_cents()
            strategy.balance_cents = balance
            print("  [BOT] Balance: $%.2f" % (balance / 100.0))

            # --- STEP 1: Sync P&L from settled markets ---
            pnl_summary = intel.sync_pnl_from_kalshi()
            # Invalidate risk manager's PNL cache so drawdown check reads fresh data
            risk._pnl_cache = None
            risk._pnl_cache_time = 0
            if pnl_summary and pnl_summary.get("trades", 0) > 0:
                pnl_trades = pnl_summary.get("trades", 0)
                pnl_total = pnl_summary.get("total_profit_cents", 0)
                print("  [BOT] P&L sync: %d trades, %dc total" % (pnl_trades, pnl_total))

            # --- STEP 1b: Reconcile settlements with risk state ---
            try:
                trade_log = _load_trade_log()
                settled = intel.check_settlements(trade_log, risk)
                if settled:
                    print("  [BOT] Settlements reconciled: %d trades" % len(settled))
                    config.atomic_json_save(config.TRADE_LOG_FILE, trade_log)
            except Exception as e:
                print("  [BOT] Settlement reconciliation error: %s" % e)

            # --- STEP 2: Check resting order fills ---
            filled = maker.check_fills()
            if filled:
                for f in filled:
                    ticker = f.get("ticker", "?")
                    print("  [BOT] Order filled: %s" % ticker)
                    risk.record_fill(f)
                    if f.get("action", "buy") == "buy":
                        f["signal"] = "buy"
                        reviewer.capture_forecast_snapshot(f)
                        _save_trade_log(f)

            # --- STEP 3: Evaluate exits for open positions ---
            positions_dict = {
                tk: pos for tk, pos in risk.get_positions().items()
                if pos.get("order_status", "executed") == "executed"
            }
            if positions_dict:
                # Convert to list format for check_exits
                positions_list = list(positions_dict.values())
                trade_log = _load_trade_log()
                exit_signals = intel.check_exits(positions_list, trade_log)
                for ex in exit_signals:
                    if ex.get("urgency") == "high":
                        ticker = ex.get("ticker", "")
                        reason = ex.get("reason", "")
                        print("  [EXIT] HIGH: %s - %s" % (ticker, reason))
                        _execute_exit(maker, risk, ticker, positions_dict.get(ticker, {}))

                # Enrich positions with live data for dashboard
                _review_positions(client, strategy, positions_dict, risk)

            # --- STEP 3b: Portfolio rebalancing ---
            _check_rebalancing(client, strategy, risk, maker, cycle)

            # --- STEP 4: Scan weather markets ---
            weather_markets = scanner.scan_weather_markets()
            if not weather_markets:
                print("  [BOT] No weather markets found")
                _write_bot_status(cycle, risk, intel, maker, 0, 0)
                if shutdown_event and shutdown_event.is_set():
                    break
                if shutdown_event:
                    shutdown_event.wait(config.SCAN_INTERVAL)
                else:
                    time.sleep(config.SCAN_INTERVAL)
                continue

            # --- STEP 5: Fetch today's observed highs per city ---
            obs_highs = _fetch_observed_highs(weather_markets, intel)

            # Debug: sample first market's key fields to verify normalization
            if weather_markets:
                _m0 = weather_markets[0]
                print(f"  [DIAG] Sample market: {_m0.get('ticker','?')} "
                      f"yes_ask={_m0.get('yes_ask')} no_ask={_m0.get('no_ask')} "
                      f"vol={_m0.get('volume')} vol24={_m0.get('volume_24h')} "
                      f"lp={_m0.get('last_price')} "
                      f"yes_ask_dollars={_m0.get('yes_ask_dollars','N/A')}")

            # --- STEP 6: Evaluate all markets for edge ---
            buy_signals = []
            all_evaluated = []
            _skip_counts = {}
            _null_count = 0
            for market in weather_markets:
                city_code = market.get("_city_code", "")
                todays_high = obs_highs.get(city_code)
                signal = strategy.evaluate_market(market, todays_high=todays_high)
                if signal and signal.get("signal") == "buy":
                    buy_signals.append(signal)
                # Capture all signals with forecast data for learning
                if signal and signal.get("city_code") and signal.get("predicted_high") is not None:
                    all_evaluated.append(signal)
                # Track skip reasons for diagnostics
                if signal is None:
                    _null_count += 1
                elif signal.get("skip_reason"):
                    r = signal.get("skip_reason", "?")
                    _skip_counts[r] = _skip_counts.get(r, 0) + 1

            # Diagnostic summary
            _top = sorted(_skip_counts.items(), key=lambda x: -x[1])[:5]
            _summary = ", ".join(f"{c}x {r}" for r, c in _top)
            print(f"  [DIAG] {len(weather_markets)} mkts: {len(buy_signals)} buys, "
                  f"{len(all_evaluated)} with forecasts, {_null_count} null. Skips: {_summary}")

            # Snapshot all evaluated signals for scan reconciliation learning
            try:
                reviewer.capture_scan_snapshot(all_evaluated)
            except Exception:
                pass

            # Bucket inconsistency detection (informational)
            try:
                inconsistencies = strategy.detect_bucket_inconsistencies(weather_markets)
                for inc in inconsistencies[:3]:
                    print("  [BUCKET] %s: %dc total (%+dc deviation, %d buckets)" % (
                        inc["event_ticker"], inc["total_yes_cents"],
                        inc["deviation_cents"], inc["num_buckets"]))
            except Exception:
                pass

            if not buy_signals:
                print("  [BOT] No actionable signals this cycle")
                _write_bot_status(cycle, risk, intel, maker, 0, 0)
                _save_scan_log(weather_markets, [], 0,
                               skip_counts=_skip_counts, null_count=_null_count,
                               evaluated_count=len(all_evaluated),
                               weather_error=strategy.weather.last_api_error)
                interval = config.SCAN_INTERVAL
                try:
                    reviewer.check_and_run()
                except Exception:
                    pass
                if config.PEAK_SCAN_START_ET <= hour_et <= config.PEAK_SCAN_END_ET:
                    interval = config.PEAK_SCAN_INTERVAL
                _sleep = max(10, interval - (time.time() - cycle_start))
                if shutdown_event and shutdown_event.is_set():
                    break
                if shutdown_event:
                    shutdown_event.wait(_sleep)
                else:
                    time.sleep(_sleep)
                continue

            # --- STEP 7: Sort by edge descending, execute best first ---
            buy_signals.sort(key=lambda s: s.get("edge", 0), reverse=True)
            print("  [BOT] Found %d actionable signals" % len(buy_signals))

            trades_this_cycle = 0
            for signal in buy_signals:
                ticker = signal.get("ticker", "")
                edge = signal.get("edge", 0)
                side = signal.get("side", "?")
                city_code = signal.get("city_code", "")
                contracts = signal.get("suggested_contracts", 1)
                price_cents = signal.get("price_cents", 0)
                is_confirmed = signal.get("confirmation_verdict") == "CONFIRMED_OUTCOME"
                is_arb = signal.get("strategy", "") == "S2-Arbitrage"

                cost_cents = price_cents * contracts

                # Build risk check signal
                risk_signal = {
                    "ticker": ticker,
                    "city_code": city_code,
                    "side": side,
                    "price_cents": price_cents,
                    "contracts": contracts,
                    "cost_cents": cost_cents,
                    "edge": edge,
                    "is_confirmed": is_confirmed,
                    "is_arb": is_arb,
                    "same_cycle": trades_this_cycle > 0,
                }

                approved, reason = risk.check_trade(risk_signal)
                if not approved:
                    print("  [RISK] BLOCKED %s: %s" % (ticker, reason))
                    continue

                # Re-read contracts (risk manager may have sized down)
                contracts = risk_signal.get("contracts", contracts)
                cost_cents = price_cents * contracts
                signal["suggested_contracts"] = contracts  # Update for trade log

                # Calculate maker limit price
                limit_price = maker.calculate_limit_price(signal)
                if not limit_price or limit_price <= 0:
                    continue

                # Re-check sizing at actual limit price (may be higher than market ask)
                if limit_price > price_cents and limit_price > 0:
                    max_by_ticker = config.MAX_PER_TICKER_CENTS // limit_price
                    max_by_ticker = min(max_by_ticker, config.MAX_CONTRACTS_PER_TICKER)
                    if max_by_ticker < contracts:
                        contracts = max(1, max_by_ticker)
                        signal["suggested_contracts"] = contracts
                    cost_cents = limit_price * contracts

                # Place order
                strat_name = signal.get("strategy", "?")
                print("  [TRADE] %s %s %s edge=%.1f%% @ %dc x%d" % (
                    strat_name, side.upper(), ticker, edge * 100,
                    limit_price, contracts))

                order_signal = dict(signal)
                order_signal["contracts"] = contracts
                order_signal["city_code"] = city_code
                order_signal["is_confirmed"] = is_confirmed
                order_signal["is_arb"] = is_arb
                signal["limit_price"] = limit_price  # For trade log accuracy

                # Taker mode for confirmed outcomes with strong edge
                if is_confirmed and edge > getattr(config, 'TAKER_MODE_MIN_EDGE', 0.15):
                    print("  [TRADE] TAKER MODE: confirmed %s edge=%.1f%%" % (ticker, edge * 100))
                    order = maker.place_market_order(order_signal)
                else:
                    order = maker.place_order(order_signal, limit_price=limit_price)
                if order:
                    trades_this_cycle += 1

            # --- STEP 8: Write status ---
            scan_interval = config.PEAK_SCAN_INTERVAL if config.PEAK_SCAN_START_ET <= hour_et <= config.PEAK_SCAN_END_ET else config.SCAN_INTERVAL
            _write_bot_status(cycle, risk, intel, maker,
                            len(buy_signals), trades_this_cycle, next_scan_seconds=scan_interval)
            _save_scan_log(weather_markets, buy_signals, trades_this_cycle,
                          skip_counts=_skip_counts, null_count=_null_count,
                          evaluated_count=len(all_evaluated),
                          weather_error=strategy.weather.last_api_error)

            # --- STEP 9: Daily learning review ---
            try:
                reviewer.check_and_run()
            except Exception as e:
                print("  [BOT] Reviewer error: %s" % e)

            # --- Sleep ---
            interval = config.SCAN_INTERVAL
            if config.PEAK_SCAN_START_ET <= hour_et <= config.PEAK_SCAN_END_ET:
                interval = config.PEAK_SCAN_INTERVAL

            elapsed = time.time() - cycle_start
            sleep_time = max(10, interval - elapsed)
            print("  [BOT] Cycle %d done in %.1fs. Next in %ds." % (
                cycle, elapsed, int(sleep_time)))
            if shutdown_event and shutdown_event.is_set():
                break
            if shutdown_event:
                shutdown_event.wait(sleep_time)
            else:
                time.sleep(sleep_time)

        except KeyboardInterrupt:
            print("\n  [BOT] Shutting down gracefully...")
            break
        except Exception as e:
            print("  [BOT] ERROR in cycle %d: %s" % (cycle, e))
            _tb.print_exc()
            if shutdown_event and shutdown_event.is_set():
                break
            if shutdown_event:
                shutdown_event.wait(30)
            else:
                time.sleep(30)

    # Cleanup: cancel pending orders on shutdown
    if shutdown_event and shutdown_event.is_set():
        try:
            print("  [BOT] Cancelling pending orders on shutdown...")
            maker.cancel_all()
        except Exception as e:
            print("  [BOT] Error cancelling orders: %s" % e)


def _check_rebalancing(client, strategy, risk, maker, cycle):
    """Every N cycles, check if low-edge positions should be exited for higher-edge opportunities."""
    interval = getattr(config, 'REBALANCE_INTERVAL_CYCLES', 15)
    if cycle % interval != 0:
        return
    
    positions = risk.get_positions()
    if len(positions) < config.MAX_OPEN_POSITIONS:
        return  # Still have capacity, no need to rebalance
    
    # Compute current edge for each position
    pos_edges = []
    weather = getattr(strategy, 'weather', None)
    for tk, pos in positions.items():
        if pos.get("order_status") == "exit_pending":
            continue
        city_code = pos.get("city_code", "")
        side = pos.get("side", "")
        entry_price = pos.get("price_cents", 0)
        if not city_code or not weather:
            continue
        try:
            mkt_dict = {"ticker": tk, "title": "", "subtitle": "", "event_ticker": tk}
            parsed = weather.parse_market_bucket(mkt_dict)
            if not parsed:
                continue
            target_date = parsed.get("target_date")
            dist = weather.get_temperature_distribution(city_code, target_date=target_date)
            if not dist:
                continue
            prob = weather.calculate_bucket_probability(dist, parsed["temp_low"], parsed["temp_high"])
            if prob is None:
                continue
            # Get live market price
            mkt = client.get_market(tk)
            if mkt and "market" in mkt:
                mkt = mkt["market"]
            if not mkt:
                continue
            if side == "yes":
                cur_price = mkt.get("yes_bid", 0) or 0
                cur_edge = prob - (cur_price / 100.0)
            else:
                cur_price = mkt.get("no_bid", 0) or 0
                cur_edge = (1 - prob) - (cur_price / 100.0)
            pos_edges.append((tk, cur_edge, pos))
        except Exception:
            continue
    
    if not pos_edges:
        return
    
    # Find the weakest position
    pos_edges.sort(key=lambda x: x[1])
    weakest_tk, weakest_edge, weakest_pos = pos_edges[0]
    
    max_old_edge = getattr(config, 'REBALANCE_MAX_OLD_EDGE', 0.03)
    if weakest_edge > max_old_edge:
        return  # All positions still have decent edge
    
    print("  [REBALANCE] Weakest position: %s edge=%.1f%% (threshold %.1f%%)" % (
        weakest_tk, weakest_edge * 100, max_old_edge * 100))
    print("  [REBALANCE] Exiting %s to free capacity" % weakest_tk)
    _execute_exit(maker, risk, weakest_tk, weakest_pos)


def _load_trade_log():
    """Load trade history for exit checking."""
    try:
        if os.path.exists(config.TRADE_LOG_FILE):
            with open(config.TRADE_LOG_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _fetch_observed_highs(markets, intel=None):
    """Fetch today's observed high for each city with markets."""
    # Prime METAR cache with one batch request (all 20 stations)
    if intel and getattr(config, "METAR_ENABLED", True):
        try:
            intel.fetch_metar_batch()
        except Exception:
            pass

    cities_seen = set()
    obs_highs = {}

    for m in markets:
        city_code = m.get("_city_code", "")
        if city_code in cities_seen:
            continue
        cities_seen.add(city_code)
        if not intel:
            continue

        try:
            max_temp = intel.get_todays_high(city_code)
            if max_temp is not None:
                obs_highs[city_code] = int(max_temp)
        except Exception:
            continue

    if obs_highs:
        print("  [OBS] Observed highs: %s" % ", ".join(
            "%s=%dF" % (c, t) for c, t in sorted(obs_highs.items())))

    return obs_highs



def _review_positions(client, strategy, positions_dict, risk):
    """Enrich positions with live market data for dashboard display."""
    now_iso = datetime.now(timezone.utc).isoformat()
    weather = getattr(strategy, "weather", None)

    for tk, pos in positions_dict.items():
        if tk not in risk.state["positions"]:
            continue
        review = {"reviewed_at": now_iso}

        entry_price = pos.get("price_cents", 0)
        side = pos.get("side", "")
        contracts = pos.get("contracts", 1)
        review["entry_price"] = entry_price
        review["entry_edge"] = pos.get("edge")

        # Fetch current market price from Kalshi
        try:
            mkt = client.get_market(tk)
            if mkt and "market" in mkt:
                mkt = mkt["market"]
            if mkt:
                if side == "yes":
                    cur_price = mkt.get("yes_bid", 0) or 0
                else:
                    cur_price = mkt.get("no_bid", 0) or 0
                if cur_price > 0:
                    review["current_price"] = cur_price
                    pnl_cents = (cur_price - entry_price) * contracts
                    review["pnl_pct"] = pnl_cents / max(entry_price * contracts, 1)
                    review["is_underwater"] = pnl_cents < 0
        except Exception as e:
            print("  [REVIEW] Market fetch error for %s: %s" % (tk, e))

        # Get current forecast for weather positions
        city_code = pos.get("city_code", "")
        if city_code and weather:
            try:
                mkt_dict = {"ticker": tk, "title": "", "subtitle": "", "event_ticker": tk}
                parsed = weather.parse_market_bucket(mkt_dict)
                target_date = parsed.get("target_date") if parsed else None
                dist = weather.get_temperature_distribution(city_code, target_date=target_date)
                if dist:
                    review["forecast_mean"] = dist.get("mean", dist.get("raw_forecast_mean"))
                    review["forecast_min"] = dist.get("min")
                    review["forecast_max"] = dist.get("max")
                    review["forecast_confidence"] = dist.get("confidence")
                    review["ensemble_members"] = dist.get("total_members")
                    if parsed:
                        prob = weather.calculate_bucket_probability(
                            dist, parsed["temp_low"], parsed["temp_high"])
                        if prob is not None:
                            mkt_price = review.get("current_price", entry_price)
                            if side == "yes":
                                cur_edge = prob - (mkt_price / 100.0)
                            else:
                                cur_edge = (1 - prob) - (mkt_price / 100.0)
                            review["current_edge"] = cur_edge
                            ent_edge = review.get("entry_edge")
                            if ent_edge and ent_edge > 0:
                                review["edge_decay_pct"] = 1 - (cur_edge / ent_edge)
            except Exception as e:
                print("  [REVIEW] Forecast error for %s: %s" % (tk, e))

        risk.state["positions"][tk]["last_review"] = review
    risk._save_state()


def _execute_exit(maker, risk, ticker, position):
    """Execute an exit order for a position. Retries once on failure."""
    if not maker or config.DRY_RUN:
        print("  [EXIT] DRY RUN: Would exit %s" % ticker)
        return

    # Dynamic exit price: YES sells at floor (1c), NO sells at ceiling (99c)
    side = position.get("side", "yes")
    exit_price = 99 if side == "no" else 1

    try:
        order = maker.place_exit_order(position, limit_price=exit_price)
        if order:
            print("  [EXIT] Exit order submitted for %s (%s side @ %dc)" % (ticker, side, exit_price))
            return
        # place_exit_order returned None — retry once
        print("  [EXIT] First attempt returned None for %s, retrying..." % ticker)
        time.sleep(2)
        order = maker.place_exit_order(position, limit_price=exit_price)
        if order:
            print("  [EXIT] Exit order submitted on retry for %s" % ticker)
        else:
            print("  [EXIT] WARNING: Exit failed after retry for %s — will retry next cycle" % ticker)
    except Exception as e:
        print("  [EXIT] Error exiting %s: %s" % (ticker, e))


def _save_trade_log(fill_info):
    """Append an executed buy fill to trade_history.json."""
    try:
        history = []
        if os.path.exists(config.TRADE_LOG_FILE):
            with open(config.TRADE_LOG_FILE, "r") as f:
                history = json.load(f)

        entry = {
            "entry_type": "buy_fill",
            "status": "executed",
            "ticker": fill_info.get("ticker", ""),
            "city_code": fill_info.get("city_code", ""),
            "side": fill_info.get("side", ""),
            "price_cents": fill_info.get("price_cents", 0),
            "contracts": fill_info.get("contracts", 1),
            "cost_cents": fill_info.get("cost_cents", 0),
            "edge": fill_info.get("edge", 0),
            "our_prob": fill_info.get("our_prob", 0),
            "strategy": fill_info.get("strategy", ""),
            "confirmation_verdict": fill_info.get("confirmation_verdict", ""),
            "order_id": fill_info.get("order_id", ""),
            "predicted_high": fill_info.get("predicted_high"),
            "model_means": fill_info.get("model_means", {}),
            "model_spread": fill_info.get("model_spread"),
            "target_date": fill_info.get("target_date", ""),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        history.append(entry)
        if len(history) > 500:
            history = history[-500:]
        config.atomic_json_save(config.TRADE_LOG_FILE, history)
    except Exception as e:
        print("  [BOT] Error saving trade log: %s" % e)


def _write_bot_status(cycle, risk, intel, maker, signals_count, trades_count, next_scan_seconds=120):
    """Write bot_status.json for dashboard."""
    try:
        rs = risk.get_state_summary()
        # Get balance directly from risk manager's client
        balance = 4000  # fallback ~$40 bankroll
        try:
            if hasattr(intel, 'client') and intel.client:
                bal = intel.client.get_balance()
                if bal and "balance" in bal:
                    balance = bal["balance"]
        except Exception:
            pass
        account_pnl = balance - config.TOTAL_DEPOSITS_CENTS
        status = {
            "version": "4.0",
            "cycle": cycle,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "environment": config.ENVIRONMENT,
            "dry_run": config.DRY_RUN,
            "balance_cents": balance,
            "account_pnl_cents": account_pnl,
            "daily_pnl_cents": rs.get("daily_pnl_cents", 0),
            "open_positions": rs.get("open_positions", 0),
            "total_exposure_cents": rs.get("total_exposure_cents", 0),
            "consecutive_losses": rs.get("consecutive_losses", 0),
            "kill_switch_until": rs.get("kill_switch_until"),
            "open_orders": maker.get_open_order_count(),
            "signals_found": signals_count,
            "trades_placed": trades_count,
            "next_scan": (datetime.now(timezone.utc) + timedelta(seconds=next_scan_seconds)).isoformat(),
        }
        config.atomic_json_save(config.BOT_STATUS_FILE, status)
    except Exception:
        pass


def _save_scan_log(markets, signals, trades, skip_counts=None,
                    null_count=0, evaluated_count=0, weather_error=None):
    """Write scan_log.json."""
    try:
        log = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "markets_scanned": len(markets),
            "signals_found": len(signals),
            "trades_placed": trades,
            "top_signals": [
                {
                    "ticker": s.get("ticker", ""),
                    "edge": round(s.get("edge", 0), 4),
                    "side": s.get("side", ""),
                    "strategy": s.get("strategy", ""),
                }
                for s in signals[:10]
            ],
            "diag_null": null_count,
            "diag_evaluated": evaluated_count,
            "diag_skips": skip_counts or {},
            "weather_api_error": weather_error,
        }
        config.atomic_json_save(config.SCAN_LOG_FILE, log)
    except Exception:
        pass


if __name__ == "__main__":
    import signal
    import threading

    shutdown_event = threading.Event()

    def _sigterm_handler(signum, frame):
        print("\n  [BOT] SIGTERM received — shutting down gracefully...")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _sigterm_handler)

    # Dashboard starts OUTSIDE the main loop (prevents port binding crash)
    try:
        from dashboard import start_dashboard_server
        start_dashboard_server()
        print("  [BOT] Dashboard server started")
    except Exception as e:
        print("  [BOT] Dashboard failed to start: %s" % e)

    # Main loop with restart on crash
    while not shutdown_event.is_set():
        try:
            main(shutdown_event=shutdown_event)
        except KeyboardInterrupt:
            print("\n  [BOT] Final shutdown.")
            break
        except Exception as e:
            if shutdown_event.is_set():
                break
            print("  [BOT] FATAL: %s" % e)
            _tb.print_exc()
            print("  [BOT] Restarting in 60s...")
            shutdown_event.wait(60)

    print("  [BOT] Graceful shutdown complete.")
