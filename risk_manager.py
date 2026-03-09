"""
RISK MANAGER v4.0
====================
10 safety checks (down from 19). Simplified kill switch.
Daily loss = stop for day (auto-resume). 5 consecutive = 4h pause.
No Sharpe-based shutdown.

SIZE-DOWN LOGIC: When a trade exceeds caps, reduce contracts to fit
instead of rejecting entirely. Only hard-reject for non-sizable checks.
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
        self.state = self._load_state()
        self._ensure_state_defaults()

    def _ensure_state_defaults(self):
        defaults = {
            "positions": {},
            "daily_pnl_cents": 0,
            "daily_date": "",
            "consecutive_losses": 0,
            "kill_switch_until": None,
            "last_trade_time": None,
            "trade_count_today": 0,
            "total_exposure_cents": 0,
        }
        for k, v in defaults.items():
            if k not in self.state:
                self.state[k] = v

    def check_trade(self, signal):
        """
        Run 10 safety checks. Returns (approved, reason).
        SIZE-DOWN: If caps are exceeded, reduce signal["contracts"] and
        signal["cost_cents"] to fit. Only reject if even 1 contract is too many.
        """
        ticker = signal.get("ticker", "")
        city = signal.get("city_code", "")
        price_cents = signal.get("price_cents", 0)
        contracts = signal.get("contracts", signal.get("suggested_contracts", 1))
        is_confirmed = signal.get("is_confirmed", False)
        is_arb = signal.get("is_arb", False)

        self._check_daily_reset()

        # --- HARD CHECKS (cannot size down, binary pass/fail) ---

        # 1. Kill switch
        if self.state.get("kill_switch_until"):
            until = self.state["kill_switch_until"]
            if datetime.now(timezone.utc).isoformat() < until:
                return False, "Kill switch active until " + until
            else:
                self.state["kill_switch_until"] = None
                self.state["consecutive_losses"] = 0
                self._save_state()

        # 2. Daily loss limit
        dpnl = self.state["daily_pnl_cents"]
        if dpnl <= -config.DAILY_LOSS_LIMIT_CENTS:
            return False, "Daily loss limit hit: %dc" % dpnl

        # 3. Consecutive loss pause — set kill switch ONCE, don't reset timer
        if self.state["consecutive_losses"] >= config.KILL_SWITCH_CONSEC_LOSSES:
            if not self.state.get("kill_switch_until"):
                pause_until = datetime.now(timezone.utc) + timedelta(
                    hours=config.KILL_SWITCH_PAUSE_HOURS
                )
                self.state["kill_switch_until"] = pause_until.isoformat()
                self._save_state()
            return False, "%d consecutive losses -> %dh pause" % (
                config.KILL_SWITCH_CONSEC_LOSSES, config.KILL_SWITCH_PAUSE_HOURS)

        # 4. Trade cooldown (same-cycle trades exempt)
        last = self.state.get("last_trade_time")
        if last and not signal.get("same_cycle", False):
            if isinstance(last, str):
                last = 0
            elapsed = time.time() - last
            if elapsed < config.TRADE_COOLDOWN:
                return False, "Cooldown: %ds remaining" % int(config.TRADE_COOLDOWN - elapsed)

        # 5. Max open positions (confirmed/arb bypass)
        if not is_confirmed and not is_arb:
            open_count = len(self.state["positions"])
            if open_count >= config.MAX_OPEN_POSITIONS:
                return False, "Max %d open positions (%d active)" % (
                    config.MAX_OPEN_POSITIONS, open_count)

        # --- SIZABLE CHECKS (reduce contracts to fit) ---

        balance = self._get_balance_cents()
        max_contracts = contracts  # Start with requested amount

        # 6. Total exposure (confirmed bypass)
        if not is_confirmed:
            max_exposure = int(balance * config.MAX_TOTAL_EXPOSURE_PCT)
            cur_exp = self.state["total_exposure_cents"]
            room = max_exposure - cur_exp
            if room <= 0:
                return False, "Exposure limit: %dc used of %dc" % (cur_exp, max_exposure)
            if price_cents > 0:
                max_by_exposure = room // price_cents
                max_contracts = min(max_contracts, max_by_exposure)

        # 7. Per-ticker limit
        ticker_exp = self._ticker_exposure(ticker)
        ticker_room = config.MAX_PER_TICKER_CENTS - ticker_exp
        if ticker_room <= 0:
            return False, "Per-ticker limit reached for %s" % ticker
        if price_cents > 0:
            max_by_ticker = ticker_room // price_cents
            max_contracts = min(max_contracts, max_by_ticker)

        # 8. Per-city concentration (confirmed bypass)
        if not is_confirmed:
            max_city = int(balance * config.MAX_PER_CITY_PCT)
            city_exp = self._city_exposure(city)
            city_room = max_city - city_exp
            if city_room <= 0:
                return False, "City %s limit reached" % city
            if price_cents > 0:
                max_by_city = city_room // price_cents
                max_contracts = min(max_contracts, max_by_city)

        # 9. Correlated positions
        max_corr = config.MAX_CORRELATED_POSITIONS
        if is_confirmed:
            max_corr += 1
        city_positions = sum(1 for p in self.state["positions"].values()
                           if p.get("city_code") == city)
        if city_positions >= max_corr:
            return False, "Correlated limit: %d positions in %s (max %d)" % (
                city_positions, city, max_corr)

        # 10. Max contracts per ticker
        existing = self.state["positions"].get(ticker, {})
        existing_contracts = existing.get("contracts", 0)
        max_by_contract_limit = config.MAX_CONTRACTS_PER_TICKER - existing_contracts
        if max_by_contract_limit <= 0:
            return False, "Contract limit reached for %s" % ticker
        max_contracts = min(max_contracts, max_by_contract_limit)

        # --- Apply size-down ---
        if max_contracts < 1:
            return False, "All caps exceeded — cannot fit even 1 contract"

        if max_contracts < contracts:
            signal["contracts"] = max_contracts
            signal["suggested_contracts"] = max_contracts
            signal["cost_cents"] = price_cents * max_contracts

        return True, "Approved (%d contracts)" % max_contracts

    def record_trade(self, trade_info):
        """Record a new trade in risk state."""
        ticker = trade_info.get("ticker", "")
        cost_cents = trade_info.get("cost_cents", 0)

        # If position already exists, subtract old cost before overwriting
        if ticker in self.state["positions"]:
            old_cost = self.state["positions"][ticker].get("cost_cents", 0)
            self.state["total_exposure_cents"] = max(
                0, self.state["total_exposure_cents"] - old_cost
            )

        self.state["positions"][ticker] = {
            "ticker": ticker,
            "city_code": trade_info.get("city_code", ""),
            "side": trade_info.get("side", "yes"),
            "price_cents": trade_info.get("price_cents", 0),
            "contracts": trade_info.get("contracts", 1),
            "cost_cents": cost_cents,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "order_id": trade_info.get("order_id", ""),
            "order_status": trade_info.get("order_status", "resting"),
            "is_confirmed": trade_info.get("is_confirmed", False),
            "is_arb": trade_info.get("is_arb", False),
        }
        self.state["total_exposure_cents"] += cost_cents
        self.state["last_trade_time"] = time.time()
        self.state["trade_count_today"] += 1
        self._save_state()

    def record_settlement(self, ticker, pnl_cents):
        """Record settlement result."""
        cost = 0
        if ticker in self.state["positions"]:
            cost = self.state["positions"][ticker].get("cost_cents", 0)
            del self.state["positions"][ticker]
        self.state["daily_pnl_cents"] += pnl_cents
        self.state["total_exposure_cents"] = max(
            0, self.state["total_exposure_cents"] - cost
        )
        if pnl_cents < 0:
            self.state["consecutive_losses"] += 1
        else:
            self.state["consecutive_losses"] = 0
        self._save_state()

    def close_position(self, ticker):
        """Remove a position from tracking."""
        if ticker in self.state["positions"]:
            cost = self.state["positions"][ticker].get("cost_cents", 0)
            del self.state["positions"][ticker]
            self.state["total_exposure_cents"] = max(
                0, self.state["total_exposure_cents"] - cost
            )
            self._save_state()

    def add_pending_order(self, ticker, order_info):
        """Track a pending/resting order. Adds to exposure tracking."""
        cost = order_info.get("cost_cents", 0)
        self.state["positions"][ticker] = order_info
        self.state["total_exposure_cents"] += cost
        self._save_state()

    def clear_pending_order(self, ticker):
        """Remove a pending order that was cancelled."""
        if ticker in self.state["positions"]:
            cost = self.state["positions"][ticker].get("cost_cents", 0)
            del self.state["positions"][ticker]
            self.state["total_exposure_cents"] = max(
                0, self.state["total_exposure_cents"] - cost
            )
            self._save_state()

    def get_positions(self):
        return dict(self.state.get("positions", {}))

    def get_state_summary(self):
        return {
            "open_positions": len(self.state["positions"]),
            "daily_pnl_cents": self.state["daily_pnl_cents"],
            "consecutive_losses": self.state["consecutive_losses"],
            "total_exposure_cents": self.state["total_exposure_cents"],
            "kill_switch_until": self.state.get("kill_switch_until"),
            "trade_count_today": self.state["trade_count_today"],
        }

    def _ticker_exposure(self, ticker):
        pos = self.state["positions"].get(ticker, {})
        return pos.get("cost_cents", 0)

    def _city_exposure(self, city):
        total = 0
        for p in self.state["positions"].values():
            if p.get("city_code") == city:
                total += p.get("cost_cents", 0)
        return total

    def _get_balance_cents(self):
        """Get balance with 60s cache to avoid excessive API calls."""
        now = time.time()
        if self._cached_balance and now - self._balance_cache_time < 60:
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
        if self._cached_balance:
            return self._cached_balance
        return config.TOTAL_DEPOSITS_CENTS

    def _check_daily_reset(self):
        """Reset daily counters at 6 AM ET (approximate trading day start)."""
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
            config.atomic_json_save(config.RISK_STATE_FILE, self.state)
        except Exception as e:
            print("  [RISK] Error saving state: %s" % e)
