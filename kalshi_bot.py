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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config
from kalshi_client import KalshiClient
from market_scanner import MarketScanner
from strategy import Strategy
from risk_manager import RiskManager
from trade_intelligence import TradeIntelligence
from maker_strategy import MakerStrategy


def main():
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
    strategy = Strategy(kalshi_client=client)
    risk = RiskManager(kalshi_client=client)
    intel = TradeIntelligence(kalshi_client=client, weather_engine=strategy.weather)
    maker = MakerStrategy(kalshi_client=client, risk_manager=risk)

    # Pass client to dashboard if running
    try:
        import dashboard
        if hasattr(dashboard, "set_kalshi_client"):
            dashboard.set_kalshi_client(client)
    except Exception:
        pass

    cycle = 0
    while True:
        cycle += 1
        try:
            cycle_start = time.time()
            now_et = datetime.now(ZoneInfo("America/New_York"))
            hour_et = now_et.hour

            print()
            print("-" * 50)
            ts = now_et.strftime("%Y-%m-%d %H:%M:%S")
            print("  Cycle %d | %s ET" % (cycle, ts))
            print("-" * 50)

            # Update strategy balance from Kalshi
            try:
                bal = client.get_balance()
                if bal and "balance" in bal:
                    strategy.balance_cents = bal["balance"]
                    print("  [BOT] Balance: $%.2f" % (bal["balance"] / 100.0))
            except Exception:
                pass

            # --- STEP 1: Sync P&L from settled markets ---
            pnl_summary = intel.sync_pnl_from_kalshi()
            if pnl_summary and pnl_summary.get("trades", 0) > 0:
                pnl_trades = pnl_summary.get("trades", 0)
                pnl_total = pnl_summary.get("total_profit_cents", 0)
                print("  [BOT] P&L sync: %d trades, %dc total" % (pnl_trades, pnl_total))

            # --- STEP 2: Check resting order fills ---
            filled = maker.check_fills()
            if filled:
                for f in filled:
                    ticker = f.get("ticker", "?")
                    print("  [BOT] Order filled: %s" % ticker)
                    risk.record_trade({
                        "ticker": ticker,
                        "city_code": f.get("city_code", ""),
                        "side": f.get("side", "yes"),
                        "price_cents": f.get("price_cents", 0),
                        "contracts": f.get("contracts", 1),
                        "cost_cents": f.get("price_cents", 0) * f.get("contracts", 1),
                        "order_id": f.get("order_id", ""),
                        "order_status": "executed",
                    })

            # --- STEP 3: Evaluate exits for open positions ---
            positions_dict = risk.get_positions()
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
                        _execute_exit(client, risk, ticker, positions_dict.get(ticker, {}))

            # --- STEP 4: Scan weather markets ---
            weather_markets = scanner.scan_weather_markets()
            if not weather_markets:
                print("  [BOT] No weather markets found")
                _write_bot_status(cycle, risk, intel, maker, 0, 0)
                time.sleep(config.SCAN_INTERVAL)
                continue

            # --- STEP 5: Fetch today's observed highs per city ---
            obs_highs = _fetch_observed_highs(weather_markets)

            # --- STEP 6: Evaluate all markets for edge ---
            buy_signals = []
            for market in weather_markets:
                city_code = market.get("_city_code", "")
                todays_high = obs_highs.get(city_code)
                signal = strategy.evaluate_market(market, todays_high=todays_high)
                if signal and signal.get("signal") == "buy":
                    buy_signals.append(signal)

            if not buy_signals:
                print("  [BOT] No actionable signals this cycle")
                _write_bot_status(cycle, risk, intel, maker, 0, 0)
                _save_scan_log(weather_markets, [], 0)
                interval = config.SCAN_INTERVAL
                if config.PEAK_SCAN_START_ET <= hour_et <= config.PEAK_SCAN_END_ET:
                    interval = config.PEAK_SCAN_INTERVAL
                time.sleep(max(10, interval - (time.time() - cycle_start)))
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

                # Calculate maker limit price
                limit_price = maker.calculate_limit_price(signal)
                if not limit_price or limit_price <= 0:
                    continue

                # Place order
                strat_name = signal.get("strategy", "?")
                print("  [TRADE] %s %s %s edge=%.1f%% @ %dc x%d" % (
                    strat_name, side.upper(), ticker, edge * 100,
                    limit_price, contracts))

                order_signal = dict(signal)
                order_signal["contracts"] = contracts
                order_signal["city_code"] = city_code

                order = maker.place_order(order_signal, limit_price=limit_price)
                if order:
                    trades_this_cycle += 1
                    _save_trade_log(signal, order)

            # --- STEP 8: Write status ---
            _write_bot_status(cycle, risk, intel, maker,
                            len(buy_signals), trades_this_cycle)
            _save_scan_log(weather_markets, buy_signals, trades_this_cycle)

            # --- Sleep ---
            interval = config.SCAN_INTERVAL
            if config.PEAK_SCAN_START_ET <= hour_et <= config.PEAK_SCAN_END_ET:
                interval = config.PEAK_SCAN_INTERVAL

            elapsed = time.time() - cycle_start
            sleep_time = max(10, interval - elapsed)
            print("  [BOT] Cycle %d done in %.1fs. Next in %ds." % (
                cycle, elapsed, int(sleep_time)))
            time.sleep(sleep_time)

        except KeyboardInterrupt:
            print("\n  [BOT] Shutting down gracefully...")
            break
        except Exception as e:
            print("  [BOT] ERROR in cycle %d: %s" % (cycle, e))
            _tb.print_exc()
            time.sleep(30)


def _load_trade_log():
    """Load trade history for exit checking."""
    try:
        if os.path.exists(config.TRADE_LOG_FILE):
            with open(config.TRADE_LOG_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _fetch_observed_highs(markets):
    """Fetch today's observed high for each city with markets."""
    import requests
    from weather_engine import CITIES

    cities_seen = set()
    obs_highs = {}

    for m in markets:
        city_code = m.get("_city_code", "")
        if city_code in cities_seen:
            continue
        cities_seen.add(city_code)
        city_info = CITIES.get(city_code)
        if not city_info:
            continue

        station = city_info.get("station", "")
        if not station:
            continue

        try:
            url = "https://api.weather.gov/stations/%s/observations" % station
            headers = {"User-Agent": "KalshiBot/4.0", "Accept": "application/geo+json"}
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code != 200:
                continue

            features = resp.json().get("features", [])
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            max_temp = None

            for f in features:
                props = f.get("properties", {})
                ts = props.get("timestamp", "")
                if today not in ts:
                    continue
                temp_c = props.get("temperature", {}).get("value")
                if temp_c is not None:
                    temp_f = round(temp_c * 9 / 5 + 32)
                    if max_temp is None or temp_f > max_temp:
                        max_temp = temp_f

            if max_temp is not None:
                obs_highs[city_code] = max_temp
        except Exception:
            continue

    if obs_highs:
        print("  [OBS] Observed highs: %s" % ", ".join(
            "%s=%dF" % (c, t) for c, t in sorted(obs_highs.items())))

    return obs_highs


def _execute_exit(client, risk, ticker, position):
    """Execute an exit order for a position."""
    if not client or config.DRY_RUN:
        print("  [EXIT] DRY RUN: Would exit %s" % ticker)
        risk.close_position(ticker)
        return

    side = position.get("side", "yes")
    contracts = position.get("contracts", 1)

    try:
        if side == "yes":
            params = {
                "ticker": ticker, "action": "sell", "side": "yes",
                "type": "limit", "yes_price": 1, "count": contracts,
            }
        else:
            params = {
                "ticker": ticker, "action": "sell", "side": "no",
                "type": "limit", "no_price": 1, "count": contracts,
            }
        result = client.create_order(**params)
        if result:
            print("  [EXIT] Exit order placed for %s" % ticker)
            risk.close_position(ticker)
    except Exception as e:
        print("  [EXIT] Error exiting %s: %s" % (ticker, e))


def _save_trade_log(signal, order):
    """Append trade to trade_history.json."""
    try:
        history = []
        if os.path.exists(config.TRADE_LOG_FILE):
            with open(config.TRADE_LOG_FILE, "r") as f:
                history = json.load(f)

        entry = {
            "ticker": signal.get("ticker", ""),
            "city_code": signal.get("city_code", ""),
            "side": signal.get("side", ""),
            "price_cents": signal.get("price_cents", 0),
            "contracts": signal.get("suggested_contracts", 1),
            "cost_cents": signal.get("price_cents", 0) * signal.get("suggested_contracts", 1),
            "edge": signal.get("edge", 0),
            "our_prob": signal.get("our_prob", 0),
            "strategy": signal.get("strategy", ""),
            "confirmation_verdict": signal.get("confirmation_verdict", ""),
            "order_id": order.get("order_id", "") if isinstance(order, dict) else "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        history.append(entry)
        if len(history) > 500:
            history = history[-500:]
        config.atomic_json_save(config.TRADE_LOG_FILE, history)
    except Exception as e:
        print("  [BOT] Error saving trade log: %s" % e)


def _write_bot_status(cycle, risk, intel, maker, signals_count, trades_count):
    """Write bot_status.json for dashboard."""
    try:
        rs = risk.get_state_summary()
        # Get balance directly from risk manager's client
        balance = config.TOTAL_DEPOSITS_CENTS
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
        }
        config.atomic_json_save(config.BOT_STATUS_FILE, status)
    except Exception:
        pass


def _save_scan_log(markets, signals, trades):
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
        }
        config.atomic_json_save(config.SCAN_LOG_FILE, log)
    except Exception:
        pass


if __name__ == "__main__":
    # Dashboard starts OUTSIDE the main loop (prevents port binding crash)
    try:
        from dashboard import start_dashboard_server
        start_dashboard_server()
        print("  [BOT] Dashboard server started")
    except Exception as e:
        print("  [BOT] Dashboard failed to start: %s" % e)

    # Main loop with restart on crash
    while True:
        try:
            main()
        except KeyboardInterrupt:
            print("\n  [BOT] Final shutdown.")
            break
        except Exception as e:
            print("  [BOT] FATAL: %s" % e)
            _tb.print_exc()
            print("  [BOT] Restarting in 60s...")
            time.sleep(60)
