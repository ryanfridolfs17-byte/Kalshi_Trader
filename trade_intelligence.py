"""
TRADE INTELLIGENCE v4.0
====================================
Exit logic, settlement tracking, P&L sync, NWS observation fetching.

CRITICAL INVARIANT: Only sync_pnl_from_kalshi() writes to pnl_history.json.
No other code path may write to that file. This prevents the "7 P&L writers" bug.
"""

import json
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
        3. Thesis still valid -> HOLD to settlement
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
                if current_temp is not None and now_hour >= 15:
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

        return exits

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
                            total_cost += f.get("yes_price", 0) * count
                            pair_revenue += can_pair * 100
                            no_held -= can_pair
                            yes_held += (count - can_pair)
                        elif fside == "no":
                            can_pair = min(count, yes_held)
                            total_cost += f.get("no_price", 0) * count
                            pair_revenue += can_pair * 100
                            yes_held -= can_pair
                            no_held += (count - can_pair)
                ticker_flows[ticker] = {
                    "total_cost": total_cost, "pair_revenue": pair_revenue,
                    "yes_held": yes_held, "no_held": no_held,
                }

            # Settlement revenue lookup
            settle_rev = {}
            for s in all_settlements:
                t = s.get("ticker", "")
                if t:
                    settle_rev[t] = s.get("revenue", 0) or 0
            settle_rev_copy = dict(settle_rev)

            # Realized P&L for closed tickers
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
                ticker_pnl = pair_rev + settle_revenue - total_cost
                total_invested += total_cost
                total_returned += pair_rev + settle_revenue
                if ticker_pnl > 0:
                    wins += 1
                elif ticker_pnl < 0:
                    losses += 1
            total_pnl = total_returned - total_invested

            # Today settlements
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
                    t_pnl = p_rev + revenue - t_cost
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
            }
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
            t_pnl = p_rev + s_revenue - t_cost

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
    # NWS OBSERVATION API
    # =====================================================

    def get_current_temperature(self, city_code):
        "Get current temperature from NWS. Cached 5 min. Returns F or None."
        station = self._get_station(city_code)
        if not station:
            return None
        cache_key = f"latest_{station}"
        cached = self._get_cached(cache_key, max_age_sec=300)
        if cached is not None:
            return cached
        try:
            url = NWS_LATEST_URL.format(station=station)
            resp = requests.get(url, headers=NWS_HEADERS, timeout=15)
            if resp.status_code != 200:
                return None
            props = resp.json().get("properties", {})
            temp_c = props.get("temperature", {}).get("value")
            if temp_c is None:
                return None
            temp_f = round(temp_c * 9 / 5 + 32)
            self._set_cached(cache_key, temp_f)
            return temp_f
        except Exception:
            return None

    def get_todays_high(self, city_code):
        "Get todays observed high so far. Cached 5 min. Returns F or None."
        station = self._get_station(city_code)
        if not station:
            return None
        cities = self._get_cities()
        if not cities or city_code not in cities:
            return None
        cache_key = f"obs_{station}"
        cached = self._get_cached(cache_key, max_age_sec=300)
        if cached is not None:
            return cached
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
                        temps.append(round(temp_c * 9 / 5 + 32))
            if temps:
                high = max(temps)
                self._set_cached(cache_key, high)
                return high
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
                return None
            market = market_data.get("market", {})
            if market.get("status") == "settled" and market.get("result"):
                return {"result": market["result"]}
        except Exception:
            pass
        return None

    def _get_cached(self, key, max_age_sec=300):
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

