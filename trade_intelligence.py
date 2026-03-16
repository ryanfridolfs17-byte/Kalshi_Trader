"""
TRADE INTELLIGENCE v4.0
====================================
Exit logic, settlement tracking, P&L sync, observation fetching.
METAR primary (batch all 20 stations in 1 request), NWS fallback.

CRITICAL INVARIANT: Only sync_pnl_from_kalshi() writes to pnl_history.json.
No other code path may write to that file. This prevents the "7 P&L writers" bug.
"""

import json
import math
import os
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict
import config

# NWS Observation API (free, no key needed)
NWS_OBS_URL = "https://api.weather.gov/stations/{station}/observations"
NWS_LATEST_URL = "https://api.weather.gov/stations/{station}/observations/latest"
NWS_HEADERS = {
    "User-Agent": "KalshiBot/4.0 (trading-bot@example.com)",
    "Accept": "application/geo+json",
}

PNL_DATA_FILE = config.PNL_HISTORY_FILE

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
    "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
    "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _date_from_ticker(ticker):
    """Extract market date from ticker like KXHIGHNY-26FEB17-B48.5 -> 2026-02-17."""
    try:
        parts = ticker.split("-")
        if len(parts) >= 2:
            dp = parts[1]
            if len(dp) >= 7:
                year = int("20" + dp[:2])
                month = _MONTHS.get(dp[2:5].upper())
                day = int(dp[5:7])
                if month:
                    return f"{year}-{month:02d}-{day:02d}"
    except Exception:
        pass
    return None


class TradeIntelligence:
    """Exit logic, settlement tracking, P&L sync (single writer), NWS observations."""

    def __init__(self, kalshi_client=None, weather_engine=None):
        self.client = kalshi_client
        self.weather = weather_engine
        self._obs_cache = {}
        self.pnl_data = self._load_pnl()

    # =====================================================
    # EXIT LOGIC
    # =====================================================

    def check_exits(self, positions, trade_log):
        """Check all open positions for exit signals.

        Returns list of exit recommendations. Only high-urgency exits
        auto-execute; medium/low require manual review.

        Exit rules (binary: hold or exit, no partial):
        1. Observation confirms loss -> EXIT (high urgency)
        2. Temp in rounding buffer after 2 PM local -> EXIT
        3. Forecast edge deterioration (edge < -15% after 10 AM) -> EXIT
        4. YES threshold unreachable after noon -> EXIT
        5. Thesis still valid -> HOLD to settlement
        """
        exits = []
        for pos in positions:
            ticker = pos.get("ticker", "")
            side = pos.get("side", "")
            city_code = pos.get("city_code", "")
            if not ticker or not city_code:
                continue

            cities = self._get_cities()
            if not cities or city_code not in cities:
                continue

            city_info = cities[city_code]
            tz_name = city_info.get("timezone", "America/New_York")
            city_now = datetime.now(ZoneInfo(tz_name))
            now_hour = city_now.hour
            today_str = city_now.strftime("%Y-%m-%d")

            parsed = self._parse_bucket(ticker)
            if not parsed:
                continue
            temp_low = parsed["temp_low"]
            temp_high = parsed["temp_high"]
            target_date = parsed.get("target_date")
            is_today = (target_date == today_str) if target_date else True
            if not is_today:
                continue

            obs_high = self.get_todays_high(city_code)
            current_temp = self.get_current_temperature(city_code)
            if obs_high is None and current_temp is None:
                continue

            # --- EXIT RULE 1: Observation confirms loss ---
            if side == "yes":
                if obs_high is not None and obs_high > temp_high + config.ROUNDING_BUFFER_HARD_F:
                    exits.append({
                        "ticker": ticker, "action": "sell", "urgency": "high",
                        "side": side,
                        "reason": (f"High {obs_high:.0f}F exceeded bucket ceiling "
                                   f"{temp_high}F + {config.ROUNDING_BUFFER_HARD_F}F rounding"),
                    })
                    continue
                if current_temp is not None and now_hour >= 14:
                    gap = temp_low - current_temp
                    if gap > 3:
                        exits.append({
                            "ticker": ticker, "action": "sell", "urgency": "high",
                            "side": side,
                            "reason": (f"Temp {current_temp:.0f}F at {now_hour}:00, "
                                       f"{gap:.0f}F below bucket floor {temp_low}F"),
                        })
                        continue

            elif side == "no":
                if obs_high is not None and temp_low <= obs_high <= temp_high:
                    exits.append({
                        "ticker": ticker, "action": "sell", "urgency": "high",
                        "side": side,
                        "reason": (f"Daily high {obs_high:.0f}F is IN bucket "
                                   f"{temp_low}-{temp_high}F -- NO loses"),
                    })
                    continue
                if obs_high is not None and 13 <= now_hour < 19:
                    gap_to_bucket = temp_low - obs_high
                    threshold = 2 if now_hour >= 14 else 3
                    if 0 < gap_to_bucket <= threshold:
                        exits.append({
                            "ticker": ticker, "action": "sell", "urgency": "high",
                            "side": side,
                            "reason": (f"Approaching: high {obs_high:.0f}F only "
                                       f"{gap_to_bucket:.0f}F below bucket floor "
                                       f"{temp_low}F"),
                        })
                        continue

                # Early approaching for threshold markets (10 AM - 1 PM)
                # Threshold markets (T-format) have temp_high >= 200.
                # NO loses everything at threshold -- need earlier detection.
                if obs_high is not None and 10 <= now_hour < 13 and temp_high >= 200:
                    gap_to_threshold = temp_low - obs_high
                    if 0 < gap_to_threshold <= 5:
                        exits.append({
                            "ticker": ticker, "action": "sell", "urgency": "high",
                            "side": side,
                            "reason": (f"Early approaching threshold: high {obs_high:.0f}F "
                                       f"only {gap_to_threshold:.0f}F below {temp_low}F "
                                       f"(10AM-1PM window)"),
                        })
                        continue

                # Forecast divergence: obs exceeds our entry forecast mean
                # If observations already disprove our thesis, exit early.
                if obs_high is not None and now_hour >= 10:
                    forecast_mean = self._get_forecast_mean_for_ticker(ticker)
                    if forecast_mean is not None and obs_high > forecast_mean + 2:
                        exits.append({
                            "ticker": ticker, "action": "sell", "urgency": "high",
                            "side": side,
                            "reason": (f"Forecast divergence: obs high {obs_high:.0f}F "
                                       f"exceeds forecast mean {forecast_mean:.1f}F by "
                                       f"{obs_high - forecast_mean:.0f}F -- thesis wrong"),
                        })
                        continue

            # --- EXIT RULE 2: Rounding buffer after 2 PM ---
            if 14 <= now_hour < 19 and obs_high is not None:
                buf = config.ROUNDING_BUFFER_HARD_F
                if side == "yes" and obs_high > temp_high - buf and obs_high <= temp_high:
                    exits.append({
                        "ticker": ticker, "action": "sell", "urgency": "high",
                        "side": side,
                        "reason": (f"Rounding buffer: high {obs_high:.0f}F within "
                                   f"{buf}F of ceiling {temp_high}F after 2 PM"),
                    })
                    continue
                if side == "no" and obs_high >= temp_low - buf and obs_high < temp_low:
                    exits.append({
                        "ticker": ticker, "action": "sell", "urgency": "high",
                        "side": side,
                        "reason": (f"Rounding buffer: high {obs_high:.0f}F within "
                                   f"{buf}F of floor {temp_low}F after 2 PM"),
                    })
                    continue

            # --- EXIT RULE 3: Forecast edge deterioration ---
            # Only exit on negative edge if we're also UNDERWATER (losing money).
            # Negative edge + profit = market agrees with our thesis (hold).
            # Negative edge + loss = our thesis is wrong (exit).
            entry_price = pos.get("price_cents", 0)
            if now_hour >= 10 and entry_price > 0:
                try:
                    dist = self.weather.get_temperature_distribution(
                        city_code, target_date=target_date
                    ) if self.weather else None
                    if dist:
                        prob = self.weather.calculate_bucket_probability(
                            dist, temp_low, temp_high
                        )
                        if prob is not None:
                            cur_mkt = self._get_current_market_price(ticker, side)
                            if cur_mkt is not None and cur_mkt > 0:
                                if side == "yes":
                                    cur_edge = prob - (cur_mkt / 100.0)
                                else:
                                    cur_edge = (1 - prob) - (cur_mkt / 100.0)
                                # Only exit if edge deeply negative AND underwater
                                is_underwater = cur_mkt < entry_price
                                if cur_edge < -0.15 and is_underwater:
                                    exits.append({
                                        "ticker": ticker, "action": "sell", "urgency": "high",
                                        "side": side,
                                        "reason": (f"Edge deterioration: edge "
                                                   f"{cur_edge:.1%} (prob={prob:.1%}, "
                                                   f"mkt={cur_mkt}c, entry={entry_price}c) "
                                                   f"-- underwater + thesis wrong"),
                                    })
                                    continue
                except Exception as e:
                    print(f"  [EXIT] Edge check error for {ticker}: {e}")

            # --- EXIT RULE 4: YES threshold too far below after noon ---
            if side == "yes" and now_hour >= 12 and temp_high >= 200:
                if obs_high is not None:
                    gap_below = temp_low - obs_high
                    # After noon, need realistic heating to reach threshold
                    max_remaining_heat = max(0, (18 - now_hour)) * 1.5  # ~1.5F/hour max
                    if gap_below > max_remaining_heat + 2:  # 2F rounding buffer
                        exits.append({
                            "ticker": ticker, "action": "sell", "urgency": "high",
                            "side": side,
                            "reason": (f"Threshold unreachable: {obs_high:.0f}F needs "
                                       f"+{gap_below:.0f}F to reach {temp_low}F, "
                                       f"max remaining heat ~{max_remaining_heat:.0f}F"),
                        })
                        continue

        return exits

    def _get_forecast_mean_for_ticker(self, ticker):
        """Look up the entry-time forecast mean for a ticker from learning state."""
        try:
            learning = {}
            if os.path.exists(config.LEARNING_STATE_FILE):
                with open(config.LEARNING_STATE_FILE, "r", encoding="utf-8") as f:
                    learning = json.load(f)
            snapshots = learning.get("forecast_snapshots", [])
            # Find the earliest snapshot for this ticker (entry-time forecast)
            for snap in snapshots:
                if snap.get("ticker") == ticker:
                    return snap.get("forecast_mean")
        except Exception:
            pass
        return None

    def _get_current_market_price(self, ticker, side):
        """Get current market bid price for a position's side."""
        if not self.client:
            return None
        try:
            data = self.client.get_market(ticker)
            if not data:
                return None
            market = data.get("market", data)
            if side == "yes":
                return market.get("yes_bid", 0) or 0
            else:
                return market.get("no_bid", 0) or 0
        except Exception:
            return None

    # =====================================================
    # SETTLEMENT TRACKING
    # =====================================================

    def check_settlements(self, trade_log, risk_manager):
        """Check if any positions have settled on Kalshi.

        NOTE: Does NOT write to pnl_history.json.
        sync_pnl_from_kalshi() is the single P&L writer.
        """
        settled = []
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        for trade in trade_log:
            if trade.get("settled"):
                continue
            entry_type = trade.get("entry_type", "")
            if entry_type and entry_type != "buy_fill":
                continue
            ticker = trade.get("ticker", "")
            if not ticker:
                continue
            status = trade.get("status", "")
            if any(x in status for x in ("resting", "cancelled", "error", "submitted")):
                continue

            if config.DRY_RUN:
                trade_date = _date_from_ticker(ticker)
                if trade_date and trade_date < today:
                    trade["settled"] = True
                    trade["settled_at"] = datetime.now(timezone.utc).isoformat()
                    trade["result"] = "expired_dry_run"
                    trade["profit_cents"] = 0
                    cost_cents = trade.get("cost_cents", 0)
                    city_code = trade.get("city_code", "")
                    risk_manager.release_exposure(ticker, cost_cents, city_code)
                    print(f"  [SETTLE] Expired (dry run): {ticker}")
                    settled.append(trade)
                continue

            settlement = self._check_market_settlement(ticker)
            if not settlement:
                # Fallback: if market date is 2+ days old, it must have settled.
                # Release the stuck position so we don't block new trades forever.
                trade_date = _date_from_ticker(ticker)
                if trade_date and trade_date < today:
                    from datetime import date as _date
                    try:
                        td = _date.fromisoformat(trade_date)
                        age_days = (_date.fromisoformat(today) - td).days
                    except Exception:
                        age_days = 0
                    if age_days >= 2:
                        cost_cents = trade.get("cost_cents", 0)
                        city_code = trade.get("city_code", "")
                        trade["settled"] = True
                        trade["settled_at"] = datetime.now(timezone.utc).isoformat()
                        trade["result"] = "expired_unknown"
                        trade["profit_cents"] = 0
                        risk_manager.release_exposure(ticker, cost_cents, city_code)
                        print(f"  [SETTLE] Expired fallback ({age_days}d old): {ticker}")
                        settled.append(trade)
                continue
            side = trade.get("side", "")
            contracts = trade.get("contracts", 0)
            cost_cents = trade.get("cost_cents", 0)
            result = settlement.get("result", "")
            city_code = trade.get("city_code", "")
            if contracts <= 0 or cost_cents <= 0:
                continue

            if (side == "yes" and result == "yes") or (side == "no" and result == "no"):
                payout = contracts * 100
                profit = payout - cost_cents
                trade["settled"] = True
                trade["settled_at"] = datetime.now(timezone.utc).isoformat()
                trade["result"] = "win"
                trade["payout_cents"] = payout
                trade["profit_cents"] = profit
                risk_manager.record_win(profit)
                risk_manager.release_exposure(ticker, cost_cents, city_code)
                print(f"  [SETTLE] WIN: {ticker} -> +${profit / 100:.2f}")
            else:
                trade["settled"] = True
                trade["settled_at"] = datetime.now(timezone.utc).isoformat()
                trade["result"] = "loss"
                trade["payout_cents"] = 0
                trade["profit_cents"] = -cost_cents
                risk_manager.record_loss(cost_cents)
                risk_manager.release_exposure(ticker, cost_cents, city_code)
                print(f"  [SETTLE] LOSS: {ticker} -> -${cost_cents / 100:.2f}")
            settled.append(trade)
        return settled

    # =====================================================
    # P&L SYNC -- SINGLE WRITER FOR pnl_history.json
    # =====================================================

    def sync_pnl_from_kalshi(self, trade_log=None):
        """SINGLE WRITER for pnl_history.json.

        Fetches fills and settlements from Kalshi API.
        Computes realized P&L using fill-pairing model.
        Reconciles trade_log with Kalshi ground truth.
        """
        if config.DRY_RUN or not self.client:
            return {"total_profit_cents": 0, "wins": 0, "losses": 0, "trades": 0}

        try:
            MAX_PAGES = 50

            # Fetch all fills
            all_fills = []
            cursor = None
            for _ in range(MAX_PAGES):
                result = self.client.get_fills(limit=200, cursor=cursor)
                if not result:
                    break
                fills = result.get("fills", [])
                all_fills.extend(fills)
                cursor = result.get("cursor")
                if not cursor or not fills:
                    break

            # Fetch all settlements
            all_settlements = []
            cursor = None
            for _ in range(MAX_PAGES):
                result = self.client.get_settlements(limit=200, cursor=cursor)
                if not result:
                    break
                settlements = result.get("settlements", [])
                all_settlements.extend(settlements)
                cursor = result.get("cursor")
                if not cursor or not settlements:
                    break

            # Fetch current open positions
            open_tickers = set()
            positions_fetched = False
            try:
                pos_cursor = None
                for _ in range(MAX_PAGES):
                    pos_result = self.client.get_positions(limit=200, cursor=pos_cursor)
                    if not pos_result:
                        break
                    positions_fetched = True
                    for pos in pos_result.get("market_positions", []):
                        if pos.get("position", 0) != 0:
                            open_tickers.add(pos.get("ticker", ""))
                    pos_cursor = pos_result.get("cursor")
                    if not pos_cursor:
                        break
            except Exception:
                pass

            # Debug: log first fill's fields so we can diagnose price field names
            if all_fills:
                sample = all_fills[0]
                print("  [PNL] Sample fill fields: %s" % sorted(sample.keys()))
                print("  [PNL] Sample fill: action=%s side=%s yes_price=%s no_price=%s count=%s" % (
                    sample.get("action"), sample.get("side"),
                    sample.get("yes_price"), sample.get("no_price"),
                    sample.get("count")))

            # Normalize fills: handle both old (yes_price/no_price int cents) and
            # new Kalshi API formats (dollar strings, or renamed fields)
            for fill in all_fills:
                self._normalize_fill(fill)

            # Group fills by ticker, process with pairing model
            ticker_fill_lists = defaultdict(list)
            for fill in all_fills:
                t = fill.get("ticker", "")
                if t:
                    ticker_fill_lists[t].append(fill)

            ticker_flows = {}
            for ticker, fills in ticker_fill_lists.items():
                fills.sort(key=lambda f: f.get("created_time", ""))
                yes_held, no_held = 0, 0
                total_cost, pair_revenue = 0, 0
                for f in fills:
                    count = f.get("count", 0)
                    action = f.get("action", "")
                    fside = f.get("side", "")
                    if action == "buy":
                        if fside == "yes":
                            yes_held += count
                            total_cost += f.get("yes_price", 0) * count
                        else:
                            no_held += count
                            total_cost += f.get("no_price", 0) * count
                    elif action == "sell":
                        if fside == "yes":
                            can_pair = min(count, no_held)
                            pair_revenue += f.get("yes_price", 0) * count  # Sell = revenue
                            pair_revenue += can_pair * 100
                            no_held -= can_pair
                            yes_held = max(0, yes_held - (count - can_pair))  # Floor at 0
                        elif fside == "no":
                            can_pair = min(count, yes_held)
                            pair_revenue += f.get("no_price", 0) * count  # Sell = revenue
                            pair_revenue += can_pair * 100
                            yes_held -= can_pair
                            no_held = max(0, no_held - (count - can_pair))  # Floor at 0
                ticker_flows[ticker] = {
                    "total_cost": total_cost, "pair_revenue": pair_revenue,
                    "yes_held": yes_held, "no_held": no_held,
                }

            # Debug: log first settlement's fields
            if all_settlements:
                sample_s = all_settlements[0]
                print("  [PNL] Sample settlement fields: %s" % sorted(sample_s.keys()))

            # Settlement revenue lookup
            # Handle multiple possible field names for revenue
            settle_rev = {}
            for s in all_settlements:
                t = s.get("ticker", "")
                if t:
                    rev = s.get("revenue", None)
                    if rev is None:
                        rev = s.get("revenue_cents", None)
                    if rev is None:
                        # Try dollar string
                        rev_str = s.get("revenue_dollars")
                        if rev_str is not None:
                            try:
                                rev = int(round(float(rev_str) * 100))
                            except (ValueError, TypeError):
                                rev = 0
                    settle_rev[t] = int(rev or 0)
            settle_rev_copy = dict(settle_rev)

            # Realized P&L for closed tickers
            # NOTE: Kalshi settlement creates sell-fills at payout price
            # (100c for winners). Using BOTH pair_revenue (from those fills)
            # AND settle_revenue would double-count. So: if a ticker has
            # sell fills (pair_revenue > 0), the fills already capture all
            # revenue -- skip settlement revenue. Only use settlement revenue
            # for tickers with no sell fills (rare edge case).
            settled_tickers = set(settle_rev.keys())
            total_invested, total_returned = 0, 0
            wins, losses = 0, 0
            for ticker, flows in ticker_flows.items():
                if ticker in open_tickers:
                    continue
                if ticker not in settled_tickers and not positions_fetched:
                    continue
                total_cost = flows["total_cost"]
                pair_rev = flows["pair_revenue"]
                settle_revenue = settle_rev.pop(ticker, 0)
                if pair_rev > 0:
                    # Fills include sell revenue (manual exits + settlement
                    # auto-closes). Don't add settlement revenue on top.
                    ticker_returned = pair_rev
                else:
                    # No sell fills -- use settlement revenue directly
                    ticker_returned = settle_revenue
                ticker_pnl = ticker_returned - total_cost
                total_invested += total_cost
                total_returned += ticker_returned
                if ticker_pnl > 0:
                    wins += 1
                elif ticker_pnl < 0:
                    losses += 1
            total_pnl = total_returned - total_invested

            # Today settlements (same double-count guard as above)
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            today_wins, today_losses, today_pnl = 0, 0, 0
            filled_tickers = set(ticker_flows.keys())
            for s in all_settlements:
                sticker = s.get("ticker", "")
                if sticker not in filled_tickers:
                    continue
                settle_ts = (s.get("settled_time", "") or
                             s.get("settle_ts", "") or "")
                if settle_ts and today_str in settle_ts:
                    t_cost = ticker_flows.get(sticker, {}).get("total_cost", 0)
                    p_rev = ticker_flows.get(sticker, {}).get("pair_revenue", 0)
                    revenue = s.get("revenue", 0) or 0
                    if p_rev > 0:
                        t_returned = p_rev
                    else:
                        t_returned = revenue
                    t_pnl = t_returned - t_cost
                    today_pnl += t_pnl
                    if t_pnl > 0:
                        today_wins += 1
                    elif t_pnl < 0:
                        today_losses += 1

            # Reconcile trade_log
            if trade_log:
                self._reconcile_trade_log(
                    trade_log, ticker_flows, settle_rev_copy,
                    open_tickers, filled_tickers, positions_fetched)

            # Open position cost
            open_cost = 0
            for ticker, flows in ticker_flows.items():
                if ticker in open_tickers:
                    open_cost += flows["total_cost"] - flows["pair_revenue"]

            # Account balance and P&L
            account_balance = None
            try:
                bal_resp = self.client.get_balance()
                if bal_resp and isinstance(bal_resp, dict):
                    account_balance = bal_resp.get("balance")
            except Exception:
                pass

            deposits = getattr(config, "TOTAL_DEPOSITS_CENTS", 0)
            account_pnl = None
            if account_balance is not None and deposits > 0:
                account_pnl = account_balance - deposits

            # Diagnostic: expose first fill's raw fields for debugging
            _diag_fill_keys = sorted(all_fills[0].keys()) if all_fills else []
            _diag_fill_sample = {}
            if all_fills:
                _s = all_fills[0]
                _diag_fill_sample = {
                    k: str(v)[:50] for k, v in _s.items()
                    if k in ("yes_price", "no_price", "price", "action",
                             "side", "count", "ticker", "yes_price_dollars",
                             "no_price_dollars", "yes_price_cents",
                             "no_price_cents", "price_cents")
                }
            _diag_settle_keys = sorted(all_settlements[0].keys()) if all_settlements else []

            # Build and save -- THE ONLY WRITE to pnl_history.json
            self.pnl_data = {
                "total_invested_cents": total_invested,
                "total_returned_cents": total_returned,
                "total_profit_cents": total_pnl,
                "wins": wins, "losses": losses, "trades": [],
                "today_wins": today_wins, "today_losses": today_losses,
                "today_pnl_cents": today_pnl,
                "open_position_cost_cents": open_cost,
                "open_positions": len(open_tickers),
                "account_balance_cents": account_balance,
                "deposits_cents": deposits,
                "account_pnl_cents": account_pnl,
                "kalshi_synced": True,
                "kalshi_fills": len(all_fills),
                "kalshi_settlements": len(all_settlements),
                "last_sync": datetime.now(timezone.utc).isoformat(),
                "_diag_fill_keys": _diag_fill_keys,
                "_diag_fill_sample": _diag_fill_sample,
                "_diag_settle_keys": _diag_settle_keys,
            }

            # --- Daily P&L time series tracking ---
            daily_history = []
            if os.path.exists(PNL_DATA_FILE):
                try:
                    with open(PNL_DATA_FILE, "r") as _f:
                        _existing = json.load(_f)
                        daily_history = _existing.get("daily_history", [])
                except Exception:
                    daily_history = []

            today_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
            today_entry = {
                "date": today_str,
                "pnl_cents": today_pnl,
                "wins": today_wins,
                "losses": today_losses,
                "account_balance_cents": account_balance,
                "account_pnl_cents": account_pnl,
            }

            if daily_history and daily_history[-1].get("date") == today_str:
                daily_history[-1] = today_entry
            else:
                daily_history.append(today_entry)

            # Keep last 90 days
            if len(daily_history) > 90:
                daily_history = daily_history[-90:]

            # Rolling 5-day P&L
            last_5 = daily_history[-5:]
            rolling_5d_pnl_cents = sum(e.get("pnl_cents", 0) for e in last_5)

            self.pnl_data["daily_history"] = daily_history
            self.pnl_data["rolling_5d_pnl_cents"] = rolling_5d_pnl_cents

            self._save_pnl(self.pnl_data)

            print(f"  [P&L SYNC] {len(all_fills)} fills, "
                  f"{len(all_settlements)} settlements, {len(open_tickers)} open")
            if account_pnl is not None:
                print(f"  [P&L SYNC] Account P&L: {account_pnl / 100:+.2f}")
            print(f"  [P&L SYNC] Realized: {total_pnl / 100:+.2f}  "
                  f"({wins}W/{losses}L)")
            return {
                "total_profit_cents": total_pnl, "wins": wins,
                "losses": losses, "trades": wins + losses,
            }

        except Exception as e:
            print(f"  [P&L SYNC] Error: {e}")
            return {"total_profit_cents": 0, "wins": 0, "losses": 0, "trades": 0}

    def _reconcile_trade_log(self, trade_log, ticker_flows, settle_rev,
                             open_tickers, filled_tickers, positions_fetched):
        """Reconcile trade_log entries with Kalshi bulk data."""
        changed = False
        for trade in trade_log:
            t_ticker = trade.get("ticker", "")
            if not t_ticker or trade.get("settled"):
                continue
            entry_type = trade.get("entry_type", "")
            if entry_type and entry_type != "buy_fill":
                continue
            status = trade.get("status", "")
            if any(x in status for x in
                   ("resting", "cancelled", "error", "submitted")):
                continue
            if t_ticker not in filled_tickers or t_ticker in open_tickers:
                continue
            if t_ticker not in set(settle_rev.keys()) and not positions_fetched:
                continue

            flows = ticker_flows.get(t_ticker, {})
            t_cost = flows.get("total_cost", 0)
            p_rev = flows.get("pair_revenue", 0)
            s_revenue = settle_rev.get(t_ticker, 0)
            # Use pair_revenue if available, otherwise settlement (not both)
            t_returned = p_rev if p_rev > 0 else s_revenue
            t_pnl = t_returned - t_cost

            already_settled_pnl = sum(
                t2.get("profit_cents", 0) for t2 in trade_log
                if t2.get("ticker") == t_ticker and t2.get("settled")
                and t2.get("strategy") in ("EXIT_PARTIAL", "PARE", "HEDGE"))
            remaining_pnl = t_pnl - already_settled_pnl
            trade_cost = trade.get("cost_cents", 0)

            unsettled_cost = sum(
                t2.get("cost_cents", 0) for t2 in trade_log
                if t2.get("ticker") == t_ticker and not t2.get("settled")
                and not any(x in t2.get("status", "") for x in
                            ("resting", "cancelled", "error", "submitted")))

            if unsettled_cost > 0 and trade_cost > 0:
                trade["profit_cents"] = round(
                    remaining_pnl * (trade_cost / unsettled_cost))
            else:
                trade["profit_cents"] = 0
            trade["result"] = "win" if trade["profit_cents"] >= 0 else "loss"
            trade["settled"] = True
            trade["settled_at"] = datetime.now(timezone.utc).isoformat()
            changed = True

        if changed:
            try:
                config.atomic_json_save(config.TRADE_LOG_FILE, trade_log)
            except Exception:
                pass

    # =====================================================
    # FILL / SETTLEMENT NORMALIZATION
    # =====================================================

    @staticmethod
    def _normalize_fill(f):
        """Normalize Kalshi fill response fields to internal int format.

        Kalshi API (March 2026) returns:
        - yes_price / no_price as STRING cents ("5", "95")
        - yes_price_dollars / no_price_dollars as STRING dollars ("0.0500")
        - count_fp as STRING float ("5.00") instead of count (int)
        This method converts all to integer cents/counts.
        """
        if not isinstance(f, dict):
            return

        def _to_int(val):
            """Convert any numeric representation to int."""
            if val is None:
                return 0
            if isinstance(val, (int, float)):
                return int(val)
            if isinstance(val, str):
                try:
                    return int(float(val))
                except (ValueError, TypeError):
                    return 0
            return 0

        def _dollars_to_cents(val):
            """Convert dollar string to int cents."""
            if val is None:
                return 0
            try:
                return int(round(float(val) * 100))
            except (ValueError, TypeError):
                return 0

        # Normalize count: count_fp (string) -> count (int)
        if "count" not in f or not isinstance(f.get("count"), int):
            f["count"] = _to_int(f.get("count_fp", f.get("count", 0)))

        # Normalize prices to int cents
        # Priority: dollar strings (most reliable) > string cents > raw values
        if "yes_price_dollars" in f or "no_price_dollars" in f:
            f["yes_price"] = _dollars_to_cents(f.get("yes_price_dollars"))
            f["no_price"] = _dollars_to_cents(f.get("no_price_dollars"))
        else:
            # yes_price/no_price may be strings ("5") or ints (5)
            f["yes_price"] = _to_int(f.get("yes_price"))
            f["no_price"] = _to_int(f.get("no_price"))

    # =====================================================
    # METAR OBSERVATION API (primary, batch all stations)
    # =====================================================

    def fetch_metar_batch(self):
        """Fetch METAR observations for ALL 20 stations in one request.

        Returns dict: {station_icao: [obs_dicts]} sorted by obs_time.
        Cached for METAR_CACHE_TTL_SEC (90s). Returns {} on failure.
        """
        cache_key = "metar_batch"
        cached = self._get_cached(
            cache_key,
            max_age_sec=getattr(config, "METAR_CACHE_TTL_SEC", 90))
        if cached is not None:
            return cached

        cities = self._get_cities()
        if not cities:
            return {}

        stations = [info["nws_station"] for info in cities.values()
                    if info.get("nws_station")]
        ids_param = ",".join(stations)

        try:
            resp = requests.get(
                getattr(config, "METAR_API_URL",
                        "https://aviationweather.gov/api/data/metar"),
                params={
                    "ids": ids_param,
                    "format": "json",
                    "hours": getattr(config, "METAR_HOURS_LOOKBACK", 18),
                },
                timeout=getattr(config, "METAR_REQUEST_TIMEOUT", 10),
            )
            if resp.status_code != 200:
                print("  [METAR] Batch fetch failed: HTTP %d" % resp.status_code)
                return {}

            raw = resp.json()
            if not isinstance(raw, list):
                return {}

            result = {}
            for obs in raw:
                icao = obs.get("icaoId", "")
                temp_c = obs.get("temp")
                obs_time_unix = obs.get("obsTime")
                if not icao or temp_c is None or obs_time_unix is None:
                    continue
                try:
                    # floor() instead of round() -- conservative: never
                    # overestimate temp. Prevents false CASE 1 triggers
                    # from C->F rounding up vs NWS's DOS-era conversion.
                    temp_f = math.floor(float(temp_c) * 9.0 / 5.0 + 32.0)
                except (ValueError, TypeError):
                    continue
                obs_dt = datetime.fromtimestamp(
                    int(obs_time_unix), tz=timezone.utc)

                entry = {
                    "temp_f": temp_f,
                    "obs_time": obs_dt,
                    "cloud_cover": obs.get("cover", ""),
                    "clouds": obs.get("clouds", []),
                    "precip": obs.get("precip"),
                }
                result.setdefault(icao, []).append(entry)

            # Sort each station's obs by time (oldest first)
            for station_obs in result.values():
                station_obs.sort(key=lambda o: o["obs_time"])

            self._set_cached(cache_key, result)
            print("  [METAR] Batch: %d obs across %d stations"
                  % (len(raw), len(result)))
            return result

        except Exception as e:
            print("  [METAR] Batch fetch error: %s" % e)
            return {}

    def _get_metar_todays_high(self, city_code, station):
        """Extract today's max temp from METAR batch data for one station."""
        metar_data = self.fetch_metar_batch()
        if not metar_data or station not in metar_data:
            return None

        cities = self._get_cities()
        if not cities or city_code not in cities:
            return None

        tz_name = cities[city_code].get("timezone", "America/New_York")
        tz = ZoneInfo(tz_name)
        local_date = datetime.now(tz).strftime("%Y-%m-%d")

        temps = []
        for obs in metar_data[station]:
            obs_local_date = obs["obs_time"].astimezone(tz).strftime("%Y-%m-%d")
            if obs_local_date == local_date:
                temps.append(obs["temp_f"])

        return max(temps) if temps else None

    def _get_metar_current_temp(self, station):
        """Get latest temperature from METAR batch for one station."""
        metar_data = self.fetch_metar_batch()
        if not metar_data or station not in metar_data:
            return None
        obs_list = metar_data[station]
        if not obs_list:
            return None
        # Already sorted oldest-first, pick last
        return obs_list[-1]["temp_f"]

    def get_metar_cloud_cover(self, city_code):
        """Get real-time cloud cover from METAR for a city.

        Returns {"cloud_cover_pct": float, "precipitation_mm": float} or None.
        Cover mapping: CLR=0%, FEW=20%, SCT=40%, BKN=70%, OVC=95%.
        """
        station = self._get_station(city_code)
        if not station:
            return None
        metar_data = self.fetch_metar_batch()
        if not metar_data or station not in metar_data:
            return None
        obs_list = metar_data[station]
        if not obs_list:
            return None

        latest = obs_list[-1]
        cover_map = {
            "CLR": 0, "SKC": 0, "FEW": 20, "SCT": 40,
            "BKN": 70, "OVC": 95,
        }
        cover_str = (latest.get("cloud_cover") or "").upper()
        cloud_pct = cover_map.get(cover_str, 50)

        precip_mm = 0.0
        if latest.get("precip") is not None:
            try:
                precip_mm = float(latest["precip"])
            except (ValueError, TypeError):
                pass

        return {"cloud_cover_pct": cloud_pct, "precipitation_mm": precip_mm}

    # =====================================================
    # NWS OBSERVATION API (fallback)
    # =====================================================

    def get_current_temperature(self, city_code):
        """Get current temperature. METAR primary, NWS fallback. Returns F or None."""
        station = self._get_station(city_code)
        if not station:
            return None
        cache_key = f"latest_{station}"
        cached = self._get_cached(cache_key, max_age_sec=120)
        if cached is not None:
            return cached

        # --- METAR primary ---
        if getattr(config, "METAR_ENABLED", True):
            temp_f = self._get_metar_current_temp(station)
            if temp_f is not None:
                self._set_cached(cache_key, temp_f)
                return temp_f

        # --- NWS fallback ---
        try:
            url = NWS_LATEST_URL.format(station=station)
            resp = requests.get(url, headers=NWS_HEADERS, timeout=15)
            if resp.status_code != 200:
                return None
            props = resp.json().get("properties", {})
            temp_c = props.get("temperature", {}).get("value")
            if temp_c is None:
                return None
            temp_f = math.floor(temp_c * 9 / 5 + 32)
            self._set_cached(cache_key, temp_f)
            return temp_f
        except Exception:
            return None

    def get_todays_high(self, city_code):
        """Get today's observed high so far. Cross-checks METAR + NWS.

        Uses conservative (lower) value when both sources available and
        disagree by >2F.  Prevents METAR spikes from triggering false
        CASE 1 confirmations — Kalshi settles on NWS, not METAR.
        """
        station = self._get_station(city_code)
        if not station:
            return None
        cities = self._get_cities()
        if not cities or city_code not in cities:
            return None
        cache_key = f"obs_{station}"
        cached = self._get_cached(cache_key, max_age_sec=120)
        if cached is not None:
            return cached

        metar_high = None
        nws_high = None

        # --- METAR (fast, batch-cached) ---
        if getattr(config, "METAR_ENABLED", True):
            metar_high = self._get_metar_todays_high(city_code, station)

        # --- NWS (separate cache, settlement source) ---
        nws_cache_key = f"nws_obs_{station}"
        nws_high = self._get_cached(nws_cache_key, max_age_sec=120)
        if nws_high is None:
            nws_high = self._fetch_nws_todays_high(city_code, station)
            if nws_high is not None:
                self._set_cached(nws_cache_key, nws_high)

        # Cross-check: use conservative value when sources disagree
        if metar_high is not None and nws_high is not None:
            if metar_high > nws_high + 2:
                # METAR spike not reflected in NWS — use NWS (settlement source)
                result = nws_high
            else:
                result = metar_high  # Sources agree, use METAR (more frequent)
        elif metar_high is not None:
            result = metar_high
        elif nws_high is not None:
            result = nws_high
        else:
            return None

        self._set_cached(cache_key, result)
        return result

    def _fetch_nws_todays_high(self, city_code, station):
        """Fetch today's high from NWS observations. Returns F or None."""
        cities = self._get_cities()
        if not cities or city_code not in cities:
            return None
        try:
            tz_name = cities[city_code].get("timezone", "America/New_York")
            tz = ZoneInfo(tz_name)
            midnight_local = datetime.now(tz).replace(
                hour=0, minute=0, second=0, microsecond=0)
            midnight_utc = midnight_local.astimezone(timezone.utc)
            local_date = datetime.now(tz).strftime("%Y-%m-%d")
            url = NWS_OBS_URL.format(station=station)
            params = {"start": midnight_utc.isoformat()}
            resp = requests.get(url, headers=NWS_HEADERS,
                                params=params, timeout=15)
            if resp.status_code != 200:
                return None
            features = resp.json().get("features", [])
            temps = []
            for obs in features:
                props = obs.get("properties", {})
                timestamp = props.get("timestamp", "")
                try:
                    obs_utc = datetime.fromisoformat(
                        timestamp.replace("Z", "+00:00"))
                    obs_local_date = obs_utc.astimezone(tz).strftime("%Y-%m-%d")
                except Exception:
                    continue
                if obs_local_date == local_date:
                    temp_c = props.get("temperature", {}).get("value")
                    if temp_c is not None:
                        temps.append(math.floor(temp_c * 9 / 5 + 32))
            if temps:
                return max(temps)
        except Exception:
            pass
        return None

    def get_temperature_trend(self, city_code):
        "Return todays high AND latest temp for cooling detection."
        station = self._get_station(city_code)
        if not station:
            return None
        cities = self._get_cities()
        if not cities or city_code not in cities:
            return None
        try:
            tz_name = cities[city_code].get("timezone", "America/New_York")
            tz = ZoneInfo(tz_name)
            midnight_local = datetime.now(tz).replace(
                hour=0, minute=0, second=0, microsecond=0)
            midnight_utc = midnight_local.astimezone(timezone.utc)
            local_date = datetime.now(tz).strftime("%Y-%m-%d")
            url = NWS_OBS_URL.format(station=station)
            params = {"start": midnight_utc.isoformat()}
            resp = requests.get(url, headers=NWS_HEADERS,
                                params=params, timeout=15)
            if resp.status_code != 200:
                return None
            features = resp.json().get("features", [])
            obs_list = []
            for obs in features:
                props = obs.get("properties", {})
                timestamp = props.get("timestamp", "")
                try:
                    obs_utc = datetime.fromisoformat(
                        timestamp.replace("Z", "+00:00"))
                    obs_local_date = obs_utc.astimezone(tz).strftime("%Y-%m-%d")
                except Exception:
                    continue
                if obs_local_date == local_date:
                    temp_c = props.get("temperature", {}).get("value")
                    if temp_c is not None:
                        obs_list.append(
                            (obs_utc, round(temp_c * 9 / 5 + 32)))
            if obs_list:
                high = max(t for _, t in obs_list)
                obs_list.sort(key=lambda x: x[0], reverse=True)
                latest = obs_list[0][1]
                drop = high - latest
                return {
                    "high": high, "latest": latest,
                    "cooling": drop >= 3.0, "drop": drop,
                }
        except Exception:
            pass
        return None

    # =====================================================
    # P&L DISPLAY
    # =====================================================

    def get_convergence_score(self, city_code):
        """Compute convergence score for a city.

        Returns 0.0-1.0 indicating how well observations track forecasts.
        Higher scores mean models + observations agree -- late-day confidence.
        Only meaningful after 2 PM local (returns 0.0 before that).
        """
        cities = self._get_cities()
        if not cities or city_code not in cities:
            return 0.0
        tz_name = cities[city_code].get("timezone", "America/New_York")
        local_now = datetime.now(ZoneInfo(tz_name))
        local_hour = local_now.hour
        if local_hour < 14:
            return 0.0

        obs_high = self.get_todays_high(city_code)
        if obs_high is None:
            return 0.0

        if not self.weather:
            return 0.0
        target_date = local_now.strftime("%Y-%m-%d")
        distribution = self.weather.get_temperature_distribution(city_code, target_date)
        if not distribution:
            return 0.0

        forecast_mean = distribution.get("forecasted_high_mean", 0)
        tracking_error = abs(obs_high - forecast_mean)
        model_spread = distribution.get("model_spread", 5.0)

        score = max(0.0, 1.0 - (tracking_error / 5.0)) * max(0.0, 1.0 - (model_spread / 8.0))
        score *= min(1.0, (local_hour - 13) / 5.0)
        score = min(1.0, score)

        if score > 0.5:
            print("  [CONVERGENCE] %s score=%.2f (track_err=%.1fF, spread=%.1fF)" % (city_code, score, tracking_error, model_spread))
        return score

    def print_pnl(self):
        "Print P&L summary to console."
        d = self.pnl_data
        total = d.get("wins", 0) + d.get("losses", 0)
        if total == 0:
            print("  [P&L] No settled trades yet")
            return
        win_rate = d["wins"] / total
        profit = d.get("total_profit_cents", 0)
        ab = d.get("account_balance_cents")
        ap = d.get("account_pnl_cents")
        oc = d.get("open_position_cost_cents", 0)
        on_ = d.get("open_positions", 0)
        tw = d.get("today_wins", 0)
        tl = d.get("today_losses", 0)
        tp = d.get("today_pnl_cents", 0)

        print(f"  -- Profit & Loss --------------------------")
        if ab is not None:
            print(f"  |  Balance:        {ab / 100:.2f}")
        if ap is not None:
            print(f"  |  Account P&L:    {ap / 100:+.2f}")
        if oc > 0:
            print(f"  |  Open positions:  {on_} ({oc / 100:.2f} at risk)")
        if tw + tl > 0:
            print(f"  |  Today:          {tw}W/{tl}L  P&L: {tp / 100:+.2f}")
        print(f"  |  Realized:       {d['wins']}W/{d['losses']}L ({win_rate:.0%})")
        print(f"  |  Realized P&L:   {profit / 100:+.2f}")
        print(f"  --------------------------------------------")

    # =====================================================
    # INTERNAL HELPERS
    # =====================================================

    def _get_cities(self):
        "Get CITIES dict from weather engine."
        if self.weather:
            return getattr(self.weather, "CITIES", None)
        try:
            from weather_engine import CITIES
            return CITIES
        except ImportError:
            return None

    def _get_station(self, city_code):
        "Get NWS station ID for a city code."
        cities = self._get_cities()
        if cities and city_code in cities:
            return cities[city_code].get("nws_station")
        return None

    def _parse_bucket(self, ticker):
        "Parse ticker to extract city_code, date, temp bounds."
        if self.weather and hasattr(self.weather, "parse_market_bucket"):
            market_dict = {
                "ticker": ticker, "title": "", "subtitle": "",
                "event_ticker": ticker,
            }
            parsed = self.weather.parse_market_bucket(market_dict)
            if parsed:
                return parsed
            if self.client:
                try:
                    data = self.client.get_market(ticker)
                    if data:
                        market = data.get("market", {})
                        market_dict = {
                            "ticker": ticker,
                            "title": market.get("title", ""),
                            "subtitle": market.get("subtitle", ""),
                            "event_ticker": market.get("event_ticker", ticker),
                        }
                        return self.weather.parse_market_bucket(market_dict)
                except Exception:
                    pass
        return None

    def _check_market_settlement(self, ticker):
        "Check if a market has settled via Kalshi API."
        if not self.client:
            return None
        try:
            market_data = self.client.get_market(ticker)
            if not market_data:
                print(f"  [SETTLE] get_market({ticker}) returned None")
                return None
            market = market_data.get("market", market_data)
            status = market.get("status", "")
            result = market.get("result", "")
            # Kalshi uses "settled", "finalized", or "closed" for resolved markets
            if status in ("settled", "finalized", "closed") and result:
                return {"result": result}
            if status and status not in ("active", "open", "trading"):
                print(f"  [SETTLE] {ticker}: status={status}, result={result}")
        except Exception as e:
            print(f"  [SETTLE] get_market({ticker}) error: {e}")

    def _get_cached(self, key, max_age_sec=120):
        "Return cached value if fresh enough, else None."
        if key in self._obs_cache:
            entry = self._obs_cache[key]
            age = (datetime.now() - entry["fetched_at"]).total_seconds()
            if age < max_age_sec:
                return entry["data"]
        return None

    def _set_cached(self, key, data):
        "Store data in observation cache."
        self._obs_cache[key] = {"data": data, "fetched_at": datetime.now()}
        # Prune stale entries to prevent unbounded memory growth
        if len(self._obs_cache) > 200:
            cutoff = datetime.now() - timedelta(hours=6)
            stale = [k for k, v in self._obs_cache.items() if v["fetched_at"] < cutoff]
            for k in stale:
                del self._obs_cache[k]

    def _load_pnl(self):
        "Load P&L data from file."
        try:
            if os.path.exists(PNL_DATA_FILE):
                with open(PNL_DATA_FILE) as f:
                    return json.load(f)
        except Exception:
            pass
        return {
            "total_profit_cents": 0, "wins": 0, "losses": 0,
            "trades": [], "last_sync": None,
        }

    def _save_pnl(self, data):
        "Save P&L data -- ONLY called from sync_pnl_from_kalshi."
        try:
            config.atomic_json_save(PNL_DATA_FILE, data)
        except Exception:
            pass

    def compute_performance_metrics(self):
        """Compute performance metrics from daily P&L history.

        Returns dict with sharpe_ratio, max_drawdown_cents, max_drawdown_pct,
        win_rate, profit_factor, winning_days, losing_days, cumulative_pnl_cents.
        """
        # Load daily_history from in-memory pnl_data or file
        daily_history = None
        if hasattr(self, "pnl_data") and self.pnl_data:
            daily_history = self.pnl_data.get("daily_history")
        if not daily_history:
            try:
                with open(PNL_DATA_FILE, "r") as _f:
                    daily_history = json.load(_f).get("daily_history", [])
            except Exception:
                daily_history = []

        if not daily_history:
            return {
                "sharpe_ratio": None, "max_drawdown_cents": 0,
                "max_drawdown_pct": 0.0, "win_rate": None,
                "profit_factor": None, "winning_days": 0,
                "losing_days": 0, "cumulative_pnl_cents": 0,
            }

        daily_pnls = [e.get("pnl_cents", 0) for e in daily_history]
        n = len(daily_pnls)

        # Cumulative P&L
        cumulative_pnl = sum(daily_pnls)

        # Winning / losing days
        winning_days = sum(1 for p in daily_pnls if p > 0)
        losing_days = sum(1 for p in daily_pnls if p < 0)
        win_rate = winning_days / n if n > 0 else None

        # Sharpe ratio (annualized) -- need at least 5 days
        sharpe_ratio = None
        if n >= 5:
            mean_pnl = cumulative_pnl / n
            variance = sum((p - mean_pnl) ** 2 for p in daily_pnls) / (n - 1)
            std_pnl = math.sqrt(variance) if variance > 0 else 0
            if std_pnl > 0:
                sharpe_ratio = round((mean_pnl / std_pnl) * math.sqrt(252), 3)

        # Max drawdown
        peak = 0
        max_dd_cents = 0
        max_dd_pct = 0.0
        running = 0
        for p in daily_pnls:
            running += p
            if running > peak:
                peak = running
            dd = peak - running
            if dd > max_dd_cents:
                max_dd_cents = dd
                max_dd_pct = round(dd / peak * 100, 2) if peak > 0 else 0.0

        # Profit factor
        gross_profit = sum(p for p in daily_pnls if p > 0)
        gross_loss = abs(sum(p for p in daily_pnls if p < 0))
        profit_factor = round(gross_profit / gross_loss, 3) if gross_loss > 0 else None

        return {
            "sharpe_ratio": sharpe_ratio,
            "max_drawdown_cents": max_dd_cents,
            "max_drawdown_pct": max_dd_pct,
            "win_rate": round(win_rate, 3) if win_rate is not None else None,
            "profit_factor": profit_factor,
            "winning_days": winning_days,
            "losing_days": losing_days,
            "cumulative_pnl_cents": cumulative_pnl,
        }

