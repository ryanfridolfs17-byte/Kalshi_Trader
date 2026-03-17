"""
RISK MANAGER v4.1
====================
Tracks filled positions separately from resting buy orders so exposure,
partial fills, and exits remain accurate across restarts.
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import config


class RiskManager:

    def __init__(self, kalshi_client=None):
        self.client = kalshi_client
        self._cached_balance = None
        self._balance_cache_time = 0
        self._pnl_cache = None
        self._pnl_cache_time = 0
        self.state = self._load_state()
        self._ensure_state_defaults()

    def _ensure_state_defaults(self):
        defaults = {
            "positions": {},
            "pending_orders": {},
            "daily_pnl_cents": 0,
            "daily_date": "",
            "consecutive_losses": 0,
            "kill_switch_until": None,
            "last_trade_time": None,
            "trade_count_today": 0,
            "total_exposure_cents": 0,
            "win_amounts": [],
            "loss_amounts": [],
        }
        for key, value in defaults.items():
            if key not in self.state:
                if isinstance(value, dict):
                    self.state[key] = dict(value)
                elif isinstance(value, list):
                    self.state[key] = list(value)
                else:
                    self.state[key] = value

        migrated = {}
        for ticker, pos in list(self.state["positions"].items()):
            status = pos.get("order_status", "")
            if status and status not in ("executed", "filled"):
                order_id = pos.get("order_id") or "legacy_%s" % ticker
                migrated[order_id] = {
                    "order_id": order_id,
                    "ticker": ticker,
                    "city_code": pos.get("city_code", ""),
                    "side": pos.get("side", "yes"),
                    "price_cents": pos.get("price_cents", 0),
                    "requested_contracts": int(pos.get("contracts", 1) or 1),
                    "remaining_contracts": int(pos.get("contracts", 1) or 1),
                    "filled_contracts": 0,
                    "remaining_cost_cents": int(pos.get("cost_cents", 0) or 0),
                    "placed_at": pos.get("timestamp", datetime.now(timezone.utc).isoformat()),
                    "order_status": status,
                    "action": "buy",
                    "is_confirmed": pos.get("is_confirmed", False),
                    "is_arb": pos.get("is_arb", False),
                }
                del self.state["positions"][ticker]
        if migrated:
            self.state["pending_orders"].update(migrated)

        self._refresh_exposure()

    def check_trade(self, signal):
        """
        Run 10 safety checks. Returns (approved, reason).
        SIZE-DOWN: If caps are exceeded, reduce signal["contracts"] and
        signal["cost_cents"] to fit. Only reject if even 1 contract is too many.
        """
        ticker = signal.get("ticker", "")
        city = signal.get("city_code", "")
        side = signal.get("side", "")
        price_cents = int(signal.get("price_cents", 0) or 0)
        contracts = int(signal.get("contracts", signal.get("suggested_contracts", 1)) or 0)
        is_confirmed = signal.get("is_confirmed", False)
        is_arb = signal.get("is_arb", False)

        self._check_daily_reset()
        self._refresh_exposure()

        if self.state.get("kill_switch_until"):
            until_str = self.state["kill_switch_until"]
            try:
                until_dt = datetime.fromisoformat(until_str.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) < until_dt:
                    return False, "Kill switch active until " + until_str
            except (ValueError, AttributeError):
                pass  # Malformed timestamp — clear it below
            self.state["kill_switch_until"] = None
            self.state["consecutive_losses"] = 0
            self._save_state()

        if self.state["daily_pnl_cents"] <= -config.DAILY_LOSS_LIMIT_CENTS:
            return False, "Daily loss limit hit: %dc" % self.state["daily_pnl_cents"]

        if self.state["consecutive_losses"] >= config.KILL_SWITCH_CONSEC_LOSSES:
            if not self.state.get("kill_switch_until"):
                pause_until = datetime.now(timezone.utc) + timedelta(
                    hours=config.KILL_SWITCH_PAUSE_HOURS
                )
                self.state["kill_switch_until"] = pause_until.isoformat()
                self._save_state()
            return False, "%d consecutive losses -> %dh pause" % (
                config.KILL_SWITCH_CONSEC_LOSSES, config.KILL_SWITCH_PAUSE_HOURS)

        # Rolling 5-day drawdown check (confirmed outcomes bypass)
        if not signal.get("is_confirmed", False):
            now = time.time()
            if self._pnl_cache is None or now - self._pnl_cache_time >= 60:
                try:
                    if os.path.exists(config.PNL_HISTORY_FILE):
                        with open(config.PNL_HISTORY_FILE, "r") as f:
                            self._pnl_cache = json.load(f)
                    else:
                        self._pnl_cache = {}
                except Exception:
                    self._pnl_cache = {}
                self._pnl_cache_time = now
            rolling_5d = self._pnl_cache.get("rolling_5d_pnl_cents") if isinstance(self._pnl_cache, dict) else None
            if rolling_5d is not None:
                balance = self._get_balance_cents()
                drawdown_limit = -(balance * config.ROLLING_DRAWDOWN_LIMIT_PCT)
                if rolling_5d <= drawdown_limit:
                    return False, "Rolling 5-day drawdown limit: %dc (limit %dc)" % (
                        int(rolling_5d), int(drawdown_limit))

        # Regional correlation cap (confirmed outcomes bypass)
        if not signal.get("is_confirmed", False):
            city_code = signal.get("city_code")
            if city_code:
                region = self._get_region(city_code)
                if region:
                    region_exp = self._region_exposure(region)
                    signal_cost = signal.get("cost_cents", 0)
                    balance = self._get_balance_cents()
                    region_limit = balance * config.MAX_PER_REGION_PCT
                    if region_exp + signal_cost > region_limit:
                        return False, "Region %s exposure %dc + %dc > limit %dc" % (
                            region, region_exp, signal_cost, int(region_limit))

        last = self.state.get("last_trade_time")
        if last and not signal.get("same_cycle", False):
            if isinstance(last, str):
                last = 0
            elapsed = time.time() - last
            if elapsed < config.TRADE_COOLDOWN:
                return False, "Cooldown: %ds remaining" % int(config.TRADE_COOLDOWN - elapsed)

        if not is_confirmed and not is_arb:
            open_count = len(self.state["positions"])
            if open_count >= config.MAX_OPEN_POSITIONS:
                return False, "Max %d open positions (%d active)" % (
                    config.MAX_OPEN_POSITIONS, open_count)

        existing_sides = self._ticker_sides(ticker)
        if existing_sides and side not in existing_sides:
            return False, "Opposite-side exposure already exists for %s" % ticker

        balance = self._get_balance_cents()
        max_contracts = contracts

        # P/L ratio dynamic position cap
        if not is_confirmed and not is_arb:
            pl_ratio = self.get_pl_ratio()
            if (pl_ratio < 2.0
                    and len(self.state.get("win_amounts", [])) >= 5
                    and len(self.state.get("loss_amounts", [])) >= 5):
                dynamic_pct = 0.03  # Tighten to 3%
            elif pl_ratio > 3.0:
                dynamic_pct = 0.07  # Loosen to 7%
            else:
                dynamic_pct = config.MAX_POSITION_PCT
            dynamic_max = int(balance * dynamic_pct)
            if price_cents > 0:
                max_contracts = min(max_contracts, max(1, dynamic_max // price_cents))

        if not is_confirmed:
            max_exposure = int(balance * config.MAX_TOTAL_EXPOSURE_PCT)
            room = max_exposure - self.state["total_exposure_cents"]
            if room <= 0:
                return False, "Exposure limit: %dc used of %dc" % (
                    self.state["total_exposure_cents"], max_exposure)
            if price_cents > 0:
                max_contracts = min(max_contracts, room // price_cents)

        ticker_room = config.MAX_PER_TICKER_CENTS - self._ticker_exposure(ticker)
        if ticker_room <= 0:
            return False, "Per-ticker limit reached for %s" % ticker
        if price_cents > 0:
            max_contracts = min(max_contracts, ticker_room // price_cents)

        if not is_confirmed:
            max_city = int(balance * config.MAX_PER_CITY_PCT)
            city_room = max_city - self._city_exposure(city)
            if city_room <= 0:
                return False, "City %s limit reached" % city
            if price_cents > 0:
                max_contracts = min(max_contracts, city_room // price_cents)

        max_corr = config.MAX_CORRELATED_POSITIONS + (1 if is_confirmed else 0)
        city_positions = self._city_position_count(city)
        if city_positions >= max_corr:
            return False, "Correlated limit: %d positions in %s (max %d)" % (
                city_positions, city, max_corr)

        max_by_contract_limit = config.MAX_CONTRACTS_PER_TICKER - self._ticker_contracts(ticker)
        if max_by_contract_limit <= 0:
            return False, "Contract limit reached for %s" % ticker
        max_contracts = min(max_contracts, max_by_contract_limit)

        if max_contracts < 1:
            return False, "All caps exceeded - cannot fit even 1 contract"

        if max_contracts < contracts:
            signal["contracts"] = max_contracts
            signal["suggested_contracts"] = max_contracts
            signal["cost_cents"] = price_cents * max_contracts

        return True, "Approved (%d contracts)" % max_contracts

    def record_fill(self, fill_info):
        """Apply a newly observed fill to risk state."""
        action = fill_info.get("action", "buy")
        ticker = fill_info.get("ticker", "")
        contracts = int(fill_info.get("contracts", 0) or 0)
        price_cents = int(fill_info.get("price_cents", 0) or 0)
        order_id = fill_info.get("order_id", "")

        if not ticker or contracts <= 0 or price_cents <= 0:
            return

        if action == "buy":
            self._consume_pending_fill(order_id, contracts)
            self._apply_buy_fill(fill_info, contracts, price_cents)
            self.state["trade_count_today"] += 1
        elif action == "sell":
            self._apply_sell_fill(fill_info, contracts)

        self.state["last_trade_time"] = time.time()
        self._refresh_exposure()
        self._save_state()

    def record_settlement(self, ticker, pnl_cents):
        """Record settlement result."""
        self.release_exposure(ticker)
        self.state["daily_pnl_cents"] += pnl_cents
        if pnl_cents < 0:
            self.state["consecutive_losses"] += 1
            self.state["loss_amounts"].append(abs(pnl_cents))
            self.state["loss_amounts"] = self.state["loss_amounts"][-50:]
        else:
            self.state["consecutive_losses"] = 0
            if pnl_cents > 0:
                self.state["win_amounts"].append(pnl_cents)
                self.state["win_amounts"] = self.state["win_amounts"][-50:]
        self._save_state()

    def release_exposure(self, ticker, cost_cents=0, city_code=""):
        """Remove position and any remaining pending buy orders for this ticker."""
        if ticker in self.state["positions"]:
            del self.state["positions"][ticker]

        for order_id, order in list(self.state["pending_orders"].items()):
            if order.get("ticker") == ticker:
                del self.state["pending_orders"][order_id]

        self._refresh_exposure()
        self._save_state()

    def record_win(self, profit_cents):
        self.state["daily_pnl_cents"] += profit_cents
        self.state["consecutive_losses"] = 0
        self._save_state()

    def record_loss(self, cost_cents):
        self.state["daily_pnl_cents"] -= cost_cents
        self.state["consecutive_losses"] += 1
        self._save_state()

    def close_position(self, ticker):
        if ticker in self.state["positions"]:
            del self.state["positions"][ticker]
            self._refresh_exposure()
            self._save_state()

    def add_pending_order(self, order_info):
        """Track an unfilled buy order separately from filled positions."""
        order_id = order_info.get("order_id", "")
        if not order_id:
            return

        requested = int(order_info.get("contracts", 0) or 0)
        price_cents = int(order_info.get("price_cents", 0) or 0)
        self.state["pending_orders"][order_id] = {
            "order_id": order_id,
            "ticker": order_info.get("ticker", ""),
            "city_code": order_info.get("city_code", ""),
            "side": order_info.get("side", "yes"),
            "price_cents": price_cents,
            "requested_contracts": requested,
            "remaining_contracts": int(order_info.get("remaining_contracts", requested) or requested),
            "filled_contracts": int(order_info.get("filled_contracts", 0) or 0),
            "remaining_cost_cents": int(order_info.get("cost_cents", price_cents * requested) or 0),
            "placed_at": order_info.get("timestamp", datetime.now(timezone.utc).isoformat()),
            "order_status": order_info.get("order_status", "resting"),
            "action": "buy",
            "is_confirmed": order_info.get("is_confirmed", False),
            "is_arb": order_info.get("is_arb", False),
        }
        self._refresh_exposure()
        self._save_state()

    def clear_pending_order(self, order_id=None, ticker=None):
        """Remove the unfilled remainder of one or more pending buy orders."""
        if order_id and order_id in self.state["pending_orders"]:
            del self.state["pending_orders"][order_id]
        elif ticker:
            for oid, order in list(self.state["pending_orders"].items()):
                if order.get("ticker") == ticker:
                    del self.state["pending_orders"][oid]
        self._refresh_exposure()
        self._save_state()

    def mark_exit_pending(self, ticker, order_id=""):
        pos = self.state["positions"].get(ticker)
        if not pos:
            return
        pos["order_status"] = "exit_pending"
        if order_id:
            pos["exit_order_id"] = order_id
        self._save_state()

    def clear_exit_pending(self, ticker, order_id=""):
        pos = self.state["positions"].get(ticker)
        if not pos:
            return
        if order_id and pos.get("exit_order_id") not in ("", order_id):
            return
        pos["order_status"] = "executed"
        pos.pop("exit_order_id", None)
        self._save_state()

    def get_positions(self):
        return dict(self.state.get("positions", {}))

    def get_pending_orders(self):
        return dict(self.state.get("pending_orders", {}))

    def get_state_summary(self):
        self._refresh_exposure()
        return {
            "open_positions": len(self.state["positions"]),
            "pending_orders": len(self.state["pending_orders"]),
            "daily_pnl_cents": self.state["daily_pnl_cents"],
            "consecutive_losses": self.state["consecutive_losses"],
            "total_exposure_cents": self.state["total_exposure_cents"],
            "kill_switch_until": self.state.get("kill_switch_until"),
            "trade_count_today": self.state["trade_count_today"],
            "pl_ratio": self.get_pl_ratio(),
        }

    def get_pl_ratio(self):
        """Compute rolling P/L ratio (avg win / avg loss). Target > 2.0.
        Returns neutral 2.0 if fewer than 5 trades on either side to avoid
        over-leveraging or under-leveraging on tiny sample sizes."""
        wins = self.state.get("win_amounts", [])
        losses = self.state.get("loss_amounts", [])
        if len(wins) < 5 or len(losses) < 5:
            return 2.0  # Neutral — don't adjust sizing on small sample
        avg_win = sum(wins) / len(wins)
        avg_loss = sum(losses) / len(losses)
        return avg_win / max(avg_loss, 1)

    def _ticker_exposure(self, ticker):
        total = 0
        pos = self.state["positions"].get(ticker)
        if pos:
            total += int(pos.get("cost_cents", 0) or 0)
        for order in self.state["pending_orders"].values():
            if order.get("ticker") == ticker:
                total += int(order.get("remaining_cost_cents", 0) or 0)
        return total

    def _ticker_contracts(self, ticker):
        total = 0
        pos = self.state["positions"].get(ticker)
        if pos:
            total += int(pos.get("contracts", 0) or 0)
        for order in self.state["pending_orders"].values():
            if order.get("ticker") == ticker:
                total += int(order.get("remaining_contracts", 0) or 0)
        return total

    def _ticker_sides(self, ticker):
        sides = set()
        pos = self.state["positions"].get(ticker)
        if pos and int(pos.get("contracts", 0) or 0) > 0:
            sides.add(pos.get("side", ""))
        for order in self.state["pending_orders"].values():
            if order.get("ticker") == ticker and int(order.get("remaining_contracts", 0) or 0) > 0:
                sides.add(order.get("side", ""))
        return {side for side in sides if side}

    def _get_region(self, city_code):
        """Look up which weather region a city belongs to."""
        for region, cities in config.CITY_REGIONS.items():
            if city_code in cities:
                return region
        return None

    def _region_exposure(self, region):
        """Sum exposure (cost_cents) for all positions/pending orders in a region."""
        region_cities = set(config.CITY_REGIONS.get(region, []))
        total = 0
        for pos in self.state["positions"].values():
            if pos.get("city_code") in region_cities:
                total += int(pos.get("cost_cents", 0) or 0)
        for order in self.state["pending_orders"].values():
            if order.get("city_code") in region_cities:
                total += int(order.get("remaining_cost_cents", 0) or 0)
        return total

    def _city_exposure(self, city):
        total = 0
        for pos in self.state["positions"].values():
            if pos.get("city_code") == city:
                total += int(pos.get("cost_cents", 0) or 0)
        for order in self.state["pending_orders"].values():
            if order.get("city_code") == city:
                total += int(order.get("remaining_cost_cents", 0) or 0)
        return total

    def _city_position_count(self, city):
        total = sum(1 for pos in self.state["positions"].values()
                    if pos.get("city_code") == city)
        total += sum(1 for order in self.state["pending_orders"].values()
                     if order.get("city_code") == city and int(order.get("remaining_contracts", 0) or 0) > 0)
        return total

    def _refresh_exposure(self):
        positions = sum(int(pos.get("cost_cents", 0) or 0)
                        for pos in self.state.get("positions", {}).values())
        pending = sum(int(order.get("remaining_cost_cents", 0) or 0)
                      for order in self.state.get("pending_orders", {}).values())
        self.state["total_exposure_cents"] = positions + pending

    def _consume_pending_fill(self, order_id, filled_contracts):
        if not order_id:
            return
        order = self.state["pending_orders"].get(order_id)
        if not order:
            return

        remaining = int(order.get("remaining_contracts", order.get("requested_contracts", 0)) or 0)
        filled_now = min(max(0, filled_contracts), remaining)
        remaining -= filled_now
        order["filled_contracts"] = int(order.get("filled_contracts", 0) or 0) + filled_now
        order["remaining_contracts"] = remaining
        order["remaining_cost_cents"] = remaining * int(order.get("price_cents", 0) or 0)
        order["order_status"] = "executed" if remaining == 0 else "partially_filled"

        if remaining == 0:
            del self.state["pending_orders"][order_id]

    def _apply_buy_fill(self, fill_info, contracts, price_cents):
        ticker = fill_info.get("ticker", "")
        side = fill_info.get("side", "yes")
        cost_cents = int(fill_info.get("cost_cents", price_cents * contracts) or 0)
        existing = self.state["positions"].get(ticker)

        if existing and existing.get("side") == side:
            total_contracts = int(existing.get("contracts", 0) or 0) + contracts
            total_cost = int(existing.get("cost_cents", 0) or 0) + cost_cents
            avg_price = int(round(float(total_cost) / float(total_contracts))) if total_contracts else price_cents
            # Preserve existing peak and entry_prob on incremental fills
            existing_peak = existing.get("peak_price_cents", int(existing.get("price_cents", 0) or 0))
            existing_entry_prob = existing.get("entry_prob")
            existing_entry_forecast = existing.get("entry_forecast_mean")
        else:
            total_contracts = contracts
            total_cost = cost_cents
            avg_price = price_cents
            existing_peak = avg_price
            existing_entry_prob = None
            existing_entry_forecast = None

        self.state["positions"][ticker] = {
            "ticker": ticker,
            "city_code": fill_info.get("city_code", ""),
            "side": side,
            "price_cents": avg_price,
            "contracts": total_contracts,
            "cost_cents": total_cost,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "order_id": fill_info.get("order_id", ""),
            "order_status": "executed",
            "is_confirmed": fill_info.get("is_confirmed", False),
            "is_arb": fill_info.get("is_arb", False),
            "entry_prob": fill_info.get("our_prob") or existing_entry_prob,
            "entry_forecast_mean": fill_info.get("predicted_high") or existing_entry_forecast,
            "peak_price_cents": max(existing_peak, avg_price),
        }

    def update_peak_price(self, ticker, current_bid_cents):
        """Update peak market price for a position. Called every cycle before exit eval."""
        pos = self.state["positions"].get(ticker)
        if not pos:
            return
        current_peak = pos.get("peak_price_cents", pos.get("price_cents", 0))
        if current_bid_cents > current_peak:
            pos["peak_price_cents"] = current_bid_cents
            self._save_state()

    def _apply_sell_fill(self, fill_info, contracts):
        ticker = fill_info.get("ticker", "")
        existing = self.state["positions"].get(ticker)
        if not existing:
            return

        cur_contracts = int(existing.get("contracts", 0) or 0)
        if contracts >= cur_contracts:
            del self.state["positions"][ticker]
            return

        avg_cost = 0.0
        if cur_contracts > 0:
            avg_cost = float(existing.get("cost_cents", 0) or 0) / float(cur_contracts)
        remaining_contracts = cur_contracts - contracts
        existing["contracts"] = remaining_contracts
        existing["cost_cents"] = int(round(avg_cost * remaining_contracts))
        # Preserve exit_pending if this is a partial fill of an exit order
        # (the remaining contracts still have an open exit order)
        if existing.get("order_status") != "exit_pending":
            existing["order_status"] = "executed"
            existing.pop("exit_order_id", None)

    def _get_balance_cents(self):
        now = time.time()
        if self._cached_balance is not None and now - self._balance_cache_time < 60:
            return self._cached_balance
        if self.client:
            try:
                bal = self.client.get_balance()
                if bal and "balance" in bal:
                    self._cached_balance = bal["balance"]
                    self._balance_cache_time = now
                    return self._cached_balance
            except Exception:
                pass
        if self._cached_balance is not None:
            return self._cached_balance
        return getattr(config, "BALANCE_FALLBACK_CENTS", 4800)

    def _check_daily_reset(self):
        et_now = datetime.now(ZoneInfo("America/New_York"))
        today = et_now.strftime("%Y-%m-%d")
        if self.state["daily_date"] != today and et_now.hour >= 6:
            self.state["daily_date"] = today
            self.state["daily_pnl_cents"] = 0
            self.state["trade_count_today"] = 0
            self._save_state()

    def _load_state(self):
        try:
            if os.path.exists(config.RISK_STATE_FILE):
                with open(config.RISK_STATE_FILE, "r") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        return loaded
        except Exception as e:
            print("  [RISK] Warning: corrupt state file, starting fresh: %s" % e)
        return {}

    def _save_state(self):
        try:
            self._refresh_exposure()
            config.atomic_json_save(config.RISK_STATE_FILE, self.state)
        except Exception as e:
            print("  [RISK] Error saving state: %s" % e)
