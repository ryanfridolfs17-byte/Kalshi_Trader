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
from settlement_lock import SettlementLockPaper
from observation_paper import ObservationPaperTrader


def _reconcile_positions(client, risk):
    """Sync risk_state positions with Kalshi's actual portfolio.

    Removes phantom positions (in risk_state but not on Kalshi) and adds
    missing positions (on Kalshi but not in risk_state). Runs every cycle
    so the dashboard always shows accurate data.
    """
    try:
        resp = client.get_positions()
        if not resp or "market_positions" not in resp:
            return
        kalshi_positions = resp["market_positions"]
        # Build set of tickers with non-zero position on Kalshi
        kalshi_tickers = {}
        for mp in kalshi_positions:
            ticker = mp.get("ticker", "")
            # get_positions() normalizes position_fp -> position (int)
            pos_count = mp.get("position", 0)
            if pos_count != 0 and ticker:
                kalshi_tickers[ticker] = mp

        local_tickers = set(risk.state.get("positions", {}).keys())

        # Remove phantoms: in risk_state but not on Kalshi
        phantoms = local_tickers - set(kalshi_tickers.keys())
        for ticker in phantoms:
            print("  [RECONCILE] Removing phantom position: %s" % ticker)
            risk.close_position(ticker)

        # Add missing: on Kalshi but not in risk_state
        missing = set(kalshi_tickers.keys()) - local_tickers
        for ticker in missing:
            mp = kalshi_tickers[ticker]
            pos_count = mp.get("position", 0)
            # Determine side and contracts
            side = "yes" if pos_count > 0 else "no"
            contracts = abs(pos_count)
            # total_traded normalized to int cents by _normalize_position()
            total_traded = mp.get("total_traded", 0)
            avg_price = int(total_traded / contracts) if contracts > 0 else 0
            cost = total_traded if total_traded else avg_price * contracts
            # Extract city code from ticker (e.g., KXHIGHTATL-26MAR19-T71 -> ATL)
            city_code = ""
            for city in ["NYC", "CHI", "MIA", "AUS", "LAX", "DEN", "PHI", "ATL",
                         "BOS", "DAL", "HOU", "LV", "MIN", "NOLA", "OKC", "PHX",
                         "SATX", "SEA", "SFO", "DC"]:
                if city in ticker.upper():
                    city_code = city
                    break
            print("  [RECONCILE] Adding missing position: %s (%d %s @ %dc)" % (
                ticker, contracts, side, avg_price))
            risk.state["positions"][ticker] = {
                "ticker": ticker,
                "side": side,
                "contracts": contracts,
                "price_cents": avg_price,
                "cost_cents": cost,
                "city_code": city_code,
                "order_status": "executed",
                "peak_price_cents": avg_price,
            }
            risk._refresh_exposure()
            risk._save_state()

        # Update contract counts on existing positions to match Kalshi
        for ticker in local_tickers & set(kalshi_tickers.keys()):
            mp = kalshi_tickers[ticker]
            kalshi_count = abs(mp.get("position", 0))
            local_pos = risk.state["positions"].get(ticker)
            if local_pos and kalshi_count > 0:
                local_count = int(local_pos.get("contracts", 0) or 0)
                if local_count != kalshi_count:
                    print("  [RECONCILE] Updating %s contracts: %d -> %d" % (
                        ticker, local_count, kalshi_count))
                    local_pos["contracts"] = kalshi_count
                    avg_price = int(local_pos.get("price_cents", 0) or 0)
                    local_pos["cost_cents"] = avg_price * kalshi_count
                    risk._refresh_exposure()
                    risk._save_state()
    except Exception as e:
        print("  [RECONCILE] Position sync failed: %s" % e)


def _reconcile_position_prices(client, risk):
    """Correct position entry prices from Kalshi fills API.

    Fixes bug where check_fills() stored fair-value estimates instead of
    actual fill prices due to missing dollar-string field normalization.
    Runs at startup; only modifies positions whose stored price differs
    from the actual fill price.
    """
    positions = risk.state.get("positions", {})
    if not positions:
        return
    try:
        fills_resp = client.get_fills(limit=200)
        if not fills_resp or "fills" not in fills_resp:
            return
        fills = fills_resp["fills"]
        # Normalize fills using trade_intelligence's method
        for f in fills:
            TradeIntelligence._normalize_fill(f)

        corrected = 0
        for ticker, pos in positions.items():
            side = pos.get("side", "yes")
            price_field = "yes_price" if side == "yes" else "no_price"
            ticker_fills = [
                f for f in fills
                if f.get("ticker") == ticker and f.get("action") == "buy"
            ]
            if not ticker_fills:
                continue
            total_cost = sum(f.get(price_field, 0) * f.get("count", 0) for f in ticker_fills)
            total_contracts = sum(f.get("count", 0) for f in ticker_fills)
            if total_contracts <= 0:
                continue
            actual_avg = int(round(total_cost / total_contracts))
            old_price = pos.get("price_cents", 0)
            if actual_avg > 0 and actual_avg != old_price:
                print("  [RECONCILE] %s: price_cents %d -> %d, cost_cents %d -> %d" % (
                    ticker, old_price, actual_avg,
                    pos.get("cost_cents", 0), actual_avg * total_contracts))
                pos["price_cents"] = actual_avg
                pos["cost_cents"] = actual_avg * total_contracts
                pos["peak_price_cents"] = max(pos.get("peak_price_cents", 0), actual_avg)
                corrected += 1
        if corrected:
            risk._save_state()
            print("  [RECONCILE] Corrected %d position(s)" % corrected)
    except Exception as e:
        print("  [RECONCILE] Failed: %s" % e)


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
    paper_locks = SettlementLockPaper(kalshi_client=client, weather_engine=strategy.weather)
    paper_trader = ObservationPaperTrader(kalshi_client=client)

    auto_obs_reason = ""
    if getattr(config, "AUTO_OBSERVATION_MODE", False):
        auto_obs_reason = getattr(config, "AUTO_OBSERVATION_REASON", "Auto observation mode enabled")
    if config.ENVIRONMENT == "production" and not config.DASHBOARD_TOKEN:
        extra = "Production dashboard token missing."
        auto_obs_reason = (auto_obs_reason + " " + extra).strip() if auto_obs_reason else extra
    if auto_obs_reason:
        risk.set_observation_mode(True, auto_obs_reason)
        print("  [SAFETY] Observation mode enabled: %s" % auto_obs_reason)

    # Pass client to dashboard if running
    try:
        import dashboard
        if hasattr(dashboard, "set_kalshi_client"):
            dashboard.set_kalshi_client(client)
        if hasattr(dashboard, "set_trade_reviewer"):
            dashboard.set_trade_reviewer(reviewer)
    except Exception:
        pass

    # Reconcile position entry prices with actual Kalshi fill data
    _reconcile_position_prices(client, risk)

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

            # Morning retry: catch overnight NWS updates for West Coast
            if hour_et == 6:
                last_retry = getattr(reviewer, '_last_morning_retry', '')
                today_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
                if last_retry != today_str:
                    try:
                        reviewer._morning_retry()
                        reviewer._last_morning_retry = today_str
                    except Exception as e:
                        print("  [BOT] Morning retry error: %s" % e)

            # --- STEP 1: Reconcile settlements with risk state FIRST ---
            # Must run before P&L sync to prevent duplicate P&L recording
            # when a trade settles between settlement check and P&L write.
            try:
                trade_log = _load_trade_log()
                settled = intel.check_settlements(trade_log, risk)
                if settled:
                    print("  [BOT] Settlements reconciled: %d trades" % len(settled))
                    config.atomic_json_save(config.TRADE_LOG_FILE, trade_log)
            except Exception as e:
                print("  [BOT] Settlement reconciliation error: %s" % e)

            # --- STEP 1b: Sync P&L from Kalshi (single writer) ---
            pnl_summary = intel.sync_pnl_from_kalshi()
            # Invalidate risk manager's PNL cache so drawdown check reads fresh data
            risk._pnl_cache = None
            risk._pnl_cache_time = 0
            if pnl_summary and pnl_summary.get("trades", 0) > 0:
                pnl_trades = pnl_summary.get("trades", 0)
                pnl_total = pnl_summary.get("total_profit_cents", 0)
                print("  [BOT] P&L sync: %d trades, %dc total" % (pnl_trades, pnl_total))

            # --- STEP 1c: Reconcile positions with Kalshi portfolio ---
            try:
                _reconcile_positions(client, risk)
            except Exception as e:
                print("  [BOT] Position reconciliation error: %s" % e)

            try:
                paper_locks.reconcile_settlements()
            except Exception as e:
                print("  [PAPER] Settlement reconciliation error: %s" % e)
            try:
                paper_trader.reconcile_settlements()
            except Exception as e:
                print("  [PAPER] Observation settlement reconciliation error: %s" % e)

            if risk.is_observation_mode():
                cancelled = maker.cancel_open_entry_orders()
                if cancelled:
                    print("  [SAFETY] Observation mode canceled %d open entry order(s)" % cancelled)

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
                # Update peak prices BEFORE exit evaluation
                _update_peak_prices(client, risk, positions_dict)
                # Re-read positions after peak update (state may have changed)
                positions_dict = {
                    tk: pos for tk, pos in risk.get_positions().items()
                    if pos.get("order_status", "executed") == "executed"
                }
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
            paper_lock_signals = []
            all_evaluated = []
            _skip_counts = {}
            _null_count = 0
            for market in weather_markets:
                city_code = market.get("_city_code", "")
                todays_high = obs_highs.get(city_code)
                paper_lock = paper_locks.evaluate_market(market, todays_high=todays_high)
                if paper_lock:
                    paper_lock_signals.append(paper_lock)
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

            try:
                paper_locks.record_candidates(paper_lock_signals)
                if paper_lock_signals:
                    top_paper = paper_locks.top_active(limit=3)
                    print("  [PAPER] %d hard-lock candidate(s): %s" % (
                        len(paper_lock_signals),
                        ", ".join(
                            "%s %s@%dc" % (
                                c.get("ticker", "?"),
                                c.get("lock_side", "?").upper(),
                                c.get("price_cents", 0),
                            )
                            for c in top_paper
                        )
                    ))
            except Exception as e:
                print("  [PAPER] Candidate capture error: %s" % e)

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

            if risk.is_observation_mode():
                paper_summary = paper_trader.record_observation_cycle(
                    buy_signals,
                    cycle=cycle,
                    balance_cents=balance,
                    limit_price_fn=maker.calculate_limit_price,
                    max_per_cycle=max_per_cycle if 'max_per_cycle' in locals() else 3,
                )
                paper_entries = paper_summary.get("executed", [])
                blocked = paper_summary.get("blocked_reasons", {})
                if paper_entries:
                    print("  [PAPER] Observation mode recorded %d paper trade(s): %s" % (
                        len(paper_entries),
                        ", ".join(
                            "%s %s@%dc x%d" % (
                                row.get("ticker", "?"),
                                row.get("side", "?").upper(),
                                row.get("entry_price_cents", 0),
                                row.get("contracts", 0),
                            )
                            for row in paper_entries[:3]
                        )
                    ))
                if blocked:
                    top_blocked = ", ".join(
                        "%s=%d" % (reason, count)
                        for reason, count in sorted(blocked.items(), key=lambda item: -item[1])[:5]
                    )
                    print("  [PAPER] Blocked paper entries: %s" % top_blocked)
                print("  [SAFETY] Observation mode active - paper trading only, no live entries")
                _write_bot_status(cycle, risk, intel, maker,
                                len(buy_signals), 0, next_scan_seconds=scan_interval if 'scan_interval' in locals() else config.SCAN_INTERVAL)
                _save_scan_log(weather_markets, buy_signals, 0,
                              skip_counts=_skip_counts, null_count=_null_count,
                              evaluated_count=len(all_evaluated),
                              weather_error=strategy.weather.last_api_error)
                try:
                    reviewer.check_and_run()
                except Exception as e:
                    print("  [BOT] Reviewer error: %s" % e)
                interval = config.SCAN_INTERVAL
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

            trades_this_cycle = 0
            max_per_cycle = 3  # Prevent concentrated losses (data: 6 trades in 22min, all lost)
            for signal in buy_signals:
                if trades_this_cycle >= max_per_cycle:
                    print("  [BOT] Per-cycle limit reached (%d trades), deferring rest" % max_per_cycle)
                    break
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
                    print("  [MAKER] %s: invalid limit price %s — skipping" % (ticker, limit_price))
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

                # Taker mode: confirmed outcomes with strong edge, OR STRONG verdict with high fee-adj edge
                fee_adj_edge = signal.get("fee_adjusted_edge", 0)
                verdict = signal.get("confirmation_verdict", "")
                use_taker = (
                    (is_confirmed and edge > getattr(config, 'TAKER_MODE_MIN_EDGE', 0.15)
                     and fee_adj_edge > 0.10)  # Fee-adjusted gate: don't overpay on marginal confirmed
                    or (verdict == "STRONG" and fee_adj_edge > getattr(config, 'STRONG_TAKER_MIN_FEE_ADJ_EDGE', 0.20))
                )
                if use_taker:
                    reason = "confirmed" if is_confirmed else "STRONG+high_edge"
                    print("  [TRADE] TAKER MODE (%s): %s edge=%.1f%%" % (reason, ticker, edge * 100))
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
                # Log learning sync summary after nightly review
                if hasattr(reviewer, 'state') and reviewer.state.get("last_review_date"):
                    biases = reviewer.get_city_biases()
                    blocked_cities = [c for c, b in biases.items()
                                      if isinstance(b, dict) and abs(b.get("bias", 0)) > config.CITY_BIAS_BLOCK_THRESHOLD_F
                                      and b.get("count", 0) >= config.CITY_BIAS_BLOCK_MIN_COUNT]
                    if blocked_cities:
                        print("  [SYNC] Bias-blocked cities: %s" % ", ".join(blocked_cities))
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
            try:
                maker.cancel_all()
            except Exception:
                pass
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
            # Use stored bucket from position (avoids broken T-prefix fallback parser)
            stored_low = pos.get("temp_low")
            stored_high = pos.get("temp_high")
            if stored_low is not None and stored_high is not None:
                mkt_dict = {"ticker": tk, "title": "", "subtitle": "", "event_ticker": tk}
                parsed = weather.parse_market_bucket(mkt_dict)
                if parsed:
                    parsed["temp_low"] = stored_low
                    parsed["temp_high"] = stored_high
                else:
                    # Fallback: construct minimal parsed dict from stored data
                    import re
                    date_match = re.search(r'-(\d{2})([A-Z]{3})(\d{2})', tk.upper())
                    target_date = None
                    if date_match:
                        yr = 2000 + int(date_match.group(1))
                        months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                                  "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
                        mn = months.get(date_match.group(2), 0)
                        dy = int(date_match.group(3))
                        if mn: target_date = f"{yr}-{mn:02d}-{dy:02d}"
                    parsed = {"city_code": city_code, "temp_low": stored_low,
                              "temp_high": stored_high, "target_date": target_date}
            else:
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
    
    # Only exit if edge is negative (actively losing EV to hold)
    # Prevents blindly exiting 2% edge positions when nothing better exists
    if weakest_edge >= 0:
        print("  [REBALANCE] Weakest position: %s edge=%.1f%% — still positive, holding" % (
            weakest_tk, weakest_edge * 100))
        return

    print("  [REBALANCE] Weakest position: %s edge=%.1f%% (negative — exiting)" % (
        weakest_tk, weakest_edge * 100))
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
                import math
                obs_highs[city_code] = math.floor(max_temp)
        except Exception:
            continue

    if obs_highs:
        print("  [OBS] Observed highs: %s" % ", ".join(
            "%s=%dF" % (c, t) for c, t in sorted(obs_highs.items())))

    return obs_highs



def _update_peak_prices(client, risk, positions_dict):
    """Fetch current bid for each position and update peak_price_cents in risk state.
    Must run BEFORE check_exits() so profit-protection has fresh peak data."""
    for tk, pos in positions_dict.items():
        if pos.get("order_status") == "exit_pending":
            continue
        side = pos.get("side", "yes")
        try:
            mkt = client.get_market(tk)
            if mkt and "market" in mkt:
                mkt = mkt["market"]
            if not mkt:
                continue
            if side == "yes":
                cur_bid = mkt.get("yes_bid", 0) or 0
            else:
                cur_bid = mkt.get("no_bid", 0) or 0
            if cur_bid > 0:
                risk.update_peak_price(tk, cur_bid)
        except Exception:
            continue


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
                # Use stored bucket from position when available
                stored_low = pos.get("temp_low")
                stored_high = pos.get("temp_high")
                mkt_dict = {"ticker": tk, "title": "", "subtitle": "", "event_ticker": tk}
                parsed = weather.parse_market_bucket(mkt_dict)
                if parsed and stored_low is not None and stored_high is not None:
                    parsed["temp_low"] = stored_low
                    parsed["temp_high"] = stored_high
                target_date = parsed.get("target_date") if parsed else None
                dist = weather.get_temperature_distribution(city_code, target_date=target_date)
                if dist:
                    review["forecast_mean"] = dist.get("forecasted_high_mean", dist.get("raw_forecast_mean"))
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

    # Exit price: sell at floor (1c) for both sides — accept any bid
    # For sell orders, limit_price = minimum we'll accept. 1c = "sell at any price".
    side = position.get("side", "yes")
    exit_price = 1

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
            "limit_price_cents": fill_info.get("limit_price_cents", fill_info.get("price_cents", 0)),
            "execution_edge_cents": fill_info.get("execution_edge_cents", 0),
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
        # Use risk manager's cached balance (60s cache, avoids redundant API call)
        balance = risk._get_balance_cents()
        account_pnl = balance - config.TOTAL_DEPOSITS_CENTS
        runtime_fingerprint = {
            "bot_version": getattr(config, "BOT_VERSION", "4.0"),
            "allow_yes_side_trades": bool(getattr(config, "ALLOW_YES_SIDE_TRADES", False)),
            "allow_strong_verdicts": bool(getattr(config, "ALLOW_STRONG_VERDICTS", False)),
            "allow_next_day_directional_trades": bool(getattr(config, "ALLOW_NEXT_DAY_DIRECTIONAL_TRADES", False)),
            "allow_threshold_directional_trades": bool(getattr(config, "ALLOW_THRESHOLD_DIRECTIONAL_TRADES", False)),
            "no_side_max_price_cents": getattr(config, "NO_SIDE_MAX_PRICE_CENTS", None),
            "longshot_floor_cents": getattr(config, "LONGSHOT_FLOOR_CENTS", None),
            "railway_git_commit_sha": os.environ.get("RAILWAY_GIT_COMMIT_SHA", ""),
            "source_version": os.environ.get("SOURCE_VERSION", ""),
        }
        status = {
            "version": runtime_fingerprint["bot_version"],
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
            "observation_mode": rs.get("observation_mode", False),
            "observation_reason": rs.get("observation_reason", ""),
            "open_orders": maker.get_open_order_count(),
            "signals_found": signals_count,
            "trades_placed": trades_count,
            "next_scan": (datetime.now(timezone.utc) + timedelta(seconds=next_scan_seconds)).isoformat(),
            "runtime_fingerprint": runtime_fingerprint,
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
